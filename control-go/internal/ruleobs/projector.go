package ruleobs

import (
	"context"
	"errors"
	"fmt"
	"strconv"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
)

// Projector persists rule observation facts. It is the ONLY writer of the
// rule-observation reference: immutable epochs (created after exact
// installation readback), per-epoch latest observations, and 5m/1h rollups
// whose window keys carry the epoch identity (never stitched across
// generation/reset boundaries). Retention is a bounded sweep.
type Projector struct {
	pool *db.Pool
}

func NewProjector(pool *db.Pool) *Projector {
	return &Projector{pool: pool}
}

// CreateEpoch durably records an immutable observation epoch. It is rejected
// unless the owning effect operation reached EXACT installation readback
// (invariant: epochs exist only after readback convergence).
func (p *Projector) CreateEpoch(ctx context.Context, r ObservationEpochRecord) error {
	if err := ValidateEpochForCreation(r); err != nil {
		return err
	}
	tag, err := p.pool.Pool.Exec(ctx, `
			INSERT INTO rule_observation_epochs (
			epoch_id, effect_intent_id, operation_id, entity_id, rule_id, target_id,
			canonical_entry_digest, match_priority_action_digest,
			observation_epoch, reset_epoch, installation_readback)
			SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'exact'
			FROM effect_intents i JOIN effect_attempts a ON a.intent_id=i.effect_intent_id
			WHERE i.effect_intent_id=$2 AND i.operation_id=$3 AND i.target_id=$6
			  AND i.claim_state='finalized' AND a.status='applied'
			  AND a.readback_digest IS NOT NULL AND a.expected_entries=a.observed_entries
			  AND a.mismatched_entries=0
			LIMIT 1`,
		r.EpochID, r.EffectIntentID, r.OperationID, r.EntityID, r.RuleID, r.TargetID,
		r.CanonicalEntryDigest, r.MatchPriorityActionDigest,
		r.EpochKey.ObservationEpoch, r.EpochKey.ResetEpoch)
	if err != nil {
		return fmt.Errorf("ruleobs: create epoch: %w", err)
	}
	if tag.RowsAffected() != 1 {
		return errors.New("ruleobs: exact finalized installation proof not found")
	}
	return nil
}

// IngestSample applies one cumulative sample to the epoch's latest reference
// and folds it into the 5m/1h rollup windows. A late sample (older epoch key)
// is rejected — it never crosses the epoch boundary and never mutates a newer
// epoch's facts.
func (p *Projector) IngestSample(ctx context.Context, targetID, entityID, ruleID string, key EpochKey, sample CounterSample) (Derived, error) {
	if targetID == "" || entityID == "" || ruleID == "" || sample.Sequence < 1 || sample.ReadCompletedAt.IsZero() {
		return Derived{}, errors.New("ruleobs: target/entity/rule/sequence/read time required")
	}
	var derived Derived
	err := p.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		var epochID string
		var baseline bool
		var err error
		derived, epochID, baseline, err = p.applyLatestTx(ctx, tx, targetID, entityID, ruleID, key, sample)
		if err != nil || baseline {
			return err
		}
		return p.foldRollupsTx(ctx, tx, epochID, ruleID, key, sample, derived)
	})
	if err != nil {
		return Derived{}, err
	}
	return derived, nil
}

func (p *Projector) applyLatestTx(ctx context.Context, tx *db.Tx, targetID, entityID, ruleID string, key EpochKey, sample CounterSample) (Derived, string, bool, error) {
	var epochID string
	var storedObs, storedReset int64
	if err := tx.QueryRow(ctx, `
				SELECT epoch_id, observation_epoch, reset_epoch
				FROM rule_observation_epochs
				WHERE target_id=$1 AND entity_id = $2 AND rule_id = $3
				ORDER BY observation_epoch DESC, reset_epoch DESC LIMIT 1
				FOR UPDATE`, targetID, entityID, ruleID).
		Scan(&epochID, &storedObs, &storedReset); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Derived{}, "", false, fmt.Errorf("ruleobs: no epoch for %s/%s/%s (create epoch after exact readback first)", targetID, entityID, ruleID)
		}
		return Derived{}, "", false, err
	}
	stored := EpochKey{ObservationEpoch: storedObs, ResetEpoch: storedReset}
	if stored != key {
		return Derived{}, "", false, fmt.Errorf("ruleobs: sample epoch %v does not equal current %v", key, stored)
	}
	// Load stored latest sample (if any) and derive.
	var prevSeq int64
	var prevP, prevB, prevE string
	havePrev := true
	err := tx.QueryRow(ctx, `
			SELECT sample_sequence, packets, bytes, eligible_packets
			FROM rule_observations WHERE epoch_id = $1`, epochID).
		Scan(&prevSeq, &prevP, &prevB, &prevE)
	if err != nil {
		if !errors.Is(err, pgx.ErrNoRows) {
			return Derived{}, "", false, err
		}
		havePrev = false
	}
	// First sample of an epoch establishes the baseline (no delta yet).
	if !havePrev {
		d0, err := insertBaseline(ctx, tx, epochID, sample)
		if err != nil {
			return Derived{}, "", false, err
		}
		return d0, epochID, true, nil
	}
	if sample.Sequence <= prevSeq {
		return Derived{}, "", false, fmt.Errorf("ruleobs: non-advancing sequence (stored=%d got=%d)", prevSeq, sample.Sequence)
	}
	prevPackets, err := parseCounter(prevP)
	if err != nil {
		return Derived{}, "", false, err
	}
	prevBytes, err := parseCounter(prevB)
	if err != nil {
		return Derived{}, "", false, err
	}
	prevEligible, err := parseCounter(prevE)
	if err != nil {
		return Derived{}, "", false, err
	}
	prev := CounterSample{Sequence: prevSeq, Packets: prevPackets, Bytes: prevBytes, EligiblePackets: prevEligible}
	d, err := ComputeDerived(prev, sample)
	if err != nil {
		return Derived{}, "", false, err
	}
	tag, err := tx.Exec(ctx, `
			UPDATE rule_observations SET
				sample_sequence = $1, read_completed_at_unix_ms = $2,
				packets = $3, bytes = $4, eligible_packets = $5,
				direct_delta_packets = $6, direct_delta_bytes = $7,
				eligible_delta_packets = $8, rate = $9, coverage = $10,
				quality_status = $11, quality_reasons = $12, updated_at = now()
			WHERE epoch_id = $13`,
		sample.Sequence, sample.ReadCompletedAt.UnixMilli(),
		counterString(sample.Packets), counterString(sample.Bytes), counterString(sample.EligiblePackets),
		counterString(d.DirectDeltaPackets), counterString(d.DirectDeltaBytes), counterString(d.EligibleDeltaPackets),
		d.Rate, d.Coverage, string(d.Quality), reasonsArray(d.Reasons), epochID)
	if err != nil {
		return Derived{}, "", false, fmt.Errorf("ruleobs: update latest: %w", err)
	}
	if tag.RowsAffected() != 1 {
		return Derived{}, "", false, errors.New("ruleobs: latest CAS conflict")
	}
	return d, epochID, false, nil
}

func insertBaseline(ctx context.Context, tx *db.Tx, epochID string, sample CounterSample) (Derived, error) {
	_, err := tx.Exec(ctx, `
		INSERT INTO rule_observations (
			epoch_id, sample_sequence, read_completed_at_unix_ms,
			packets, bytes, eligible_packets,
			direct_delta_packets, direct_delta_bytes, eligible_delta_packets,
			rate, coverage, quality_status, quality_reasons,
			outcome_status, outcome_expected, outcome_actual)
			VALUES ($1,$2,$3,$4,$5,$6,0,0,0,0,0,'not-measurable','{BASELINE_ESTABLISHED}',
			'not-observed','not-measurable','not-observed')`,
		epochID, sample.Sequence, sample.ReadCompletedAt.UnixMilli(),
		counterString(sample.Packets), counterString(sample.Bytes), counterString(sample.EligiblePackets))
	if err != nil {
		return Derived{}, fmt.Errorf("ruleobs: insert baseline: %w", err)
	}
	return Derived{Quality: QualityNotMeasurable, Reasons: []QualityReason{ReasonBaseline}}, nil
}

// foldRollups upserts the 5m/1h windows for one derived interval. The window
// key includes the epoch identity, so a window NEVER stitches across
// generation/reset boundaries.
func (p *Projector) foldRollupsTx(ctx context.Context, tx *db.Tx, epochID, ruleID string, key EpochKey, sample CounterSample, d Derived) error {
	windowStart := func(winUnixMS int64) int64 {
		return (sample.ReadCompletedAt.UnixMilli() / winUnixMS) * winUnixMS
	}
	if d.Quality == QualityReset || d.Quality == QualityGap || d.Quality == QualityInvalid {
		// A reset/gap/invalid interval is folded as its own status; deltas are
		// not silently accumulated (never collapse to 0%).
		for _, w := range []struct {
			table string
			size  int64
		}{
			{"rule_rollups_5m", Window5mUnixMS}, {"rule_rollups_1h", Window1hUnixMS},
		} {
			_, err := tx.Exec(ctx, fmt.Sprintf(`
				INSERT INTO %s (window_start_unix_ms, epoch_id, observation_epoch, reset_epoch,
					rule_id, direct_packets, direct_bytes, eligible_packets, sample_count,
					quality_status, quality_reasons)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,1,$9,$10)
				ON CONFLICT (window_start_unix_ms, epoch_id, rule_id) DO UPDATE SET
					sample_count = %s.sample_count + 1,
					quality_status = EXCLUDED.quality_status,
					quality_reasons = EXCLUDED.quality_reasons, updated_at = now()`, w.table, w.table),
				windowStart(w.size), epochID, key.ObservationEpoch, key.ResetEpoch, ruleID,
				0, 0, 0, string(d.Quality), reasonsArray(d.Reasons))
			if err != nil {
				return fmt.Errorf("ruleobs: %s fold: %w", w.table, err)
			}
		}
		return nil
	}
	for _, w := range []struct {
		table string
		size  int64
	}{
		{"rule_rollups_5m", Window5mUnixMS}, {"rule_rollups_1h", Window1hUnixMS},
	} {
		_, err := tx.Exec(ctx, fmt.Sprintf(`
			INSERT INTO %s (window_start_unix_ms, epoch_id, observation_epoch, reset_epoch,
				rule_id, direct_packets, direct_bytes, eligible_packets, sample_count,
				quality_status, quality_reasons)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,1,$9,$10)
			ON CONFLICT (window_start_unix_ms, epoch_id, rule_id) DO UPDATE SET
				direct_packets = %s.direct_packets + EXCLUDED.direct_packets,
				direct_bytes = %s.direct_bytes + EXCLUDED.direct_bytes,
				eligible_packets = %s.eligible_packets + EXCLUDED.eligible_packets,
				sample_count = %s.sample_count + 1,
				quality_status = EXCLUDED.quality_status,
				quality_reasons = EXCLUDED.quality_reasons, updated_at = now()`,
			w.table, w.table, w.table, w.table, w.table),
			windowStart(w.size), epochID, key.ObservationEpoch, key.ResetEpoch, ruleID,
			counterString(d.DirectDeltaPackets), counterString(d.DirectDeltaBytes), counterString(d.EligibleDeltaPackets),
			string(d.Quality), reasonsArray(d.Reasons))
		if err != nil {
			return fmt.Errorf("ruleobs: %s fold: %w", w.table, err)
		}
	}
	return nil
}

func counterString(v uint64) string { return strconv.FormatUint(v, 10) }

func parseCounter(v string) (uint64, error) {
	n, err := strconv.ParseUint(v, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("ruleobs: invalid persisted uint64 counter %q: %w", v, err)
	}
	return n, nil
}

// RecordOutcome updates the independent packet/action outcome dimension on
// the epoch's latest reference. It NEVER derives outcome from counter growth.
func (p *Projector) RecordOutcome(ctx context.Context, targetID, entityID, ruleID string, key EpochKey, status OutcomeStatus, expected OutcomeExpected, actual OutcomeActual) error {
	if targetID == "" || entityID == "" || ruleID == "" || key.ObservationEpoch < 1 || key.ResetEpoch < 1 {
		return errors.New("ruleobs: outcome identity/epoch required")
	}
	if !validOutcome(status, expected, actual) {
		return errors.New("ruleobs: outcome enum invalid")
	}
	tag, err := p.pool.Pool.Exec(ctx, `
		UPDATE rule_observations ro SET
			outcome_status = $1, outcome_expected = $2, outcome_actual = $3, updated_at = now()
		FROM rule_observation_epochs e
			WHERE ro.epoch_id = e.epoch_id AND e.target_id=$4 AND e.entity_id = $5 AND e.rule_id = $6
			  AND e.observation_epoch = $7 AND e.reset_epoch = $8`,
		string(status), string(expected), string(actual), targetID, entityID, ruleID,
		key.ObservationEpoch, key.ResetEpoch)
	if err != nil {
		return fmt.Errorf("ruleobs: record outcome: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return errors.New("ruleobs: record outcome: no matching epoch/latest row")
	}
	return nil
}

// SweepRetention deletes windows older than the bounded retention (5m: 7
// days; 1h: 30 days). The sweep is bounded (LIMIT) so it can never hold a
// transaction open unboundedly; callers repeat until below the budget.
func (p *Projector) SweepRetention(ctx context.Context, nowUnixMS int64) (deleted5m, deleted1h int64, err error) {
	cut5m := nowUnixMS - Retention5mWindows*Window5mUnixMS
	cut1h := nowUnixMS - Retention1hWindows*Window1hUnixMS
	err = p.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `
			DELETE FROM rule_rollups_5m
			WHERE ctid IN (SELECT ctid FROM rule_rollups_5m
				WHERE window_start_unix_ms < $1 LIMIT 1000)`, cut5m)
		if err != nil {
			return err
		}
		deleted5m = tag.RowsAffected()
		tag, err = tx.Exec(ctx, `
			DELETE FROM rule_rollups_1h
			WHERE ctid IN (SELECT ctid FROM rule_rollups_1h
				WHERE window_start_unix_ms < $1 LIMIT 1000)`, cut1h)
		if err != nil {
			return err
		}
		deleted1h = tag.RowsAffected()
		return nil
	})
	if err != nil {
		return 0, 0, fmt.Errorf("ruleobs: sweep retention: %w", err)
	}
	return deleted5m, deleted1h, nil
}

func reasonsArray(rs []QualityReason) []string {
	out := make([]string, 0, len(rs))
	for _, r := range rs {
		out = append(out, string(r))
	}
	return out
}
