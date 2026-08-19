package model

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"

	"masi-nids/control-go/internal/db"
)

// IncarnationService rotates the model-control incarnation. PITR/restore/clone/
// rewind MUST rotate a NEVER-USED incarnation before reopening a writer; old
// assignments/envelope/action/readback/handshake under the same numeric
// generation are invalidated (MIG-INF-001, ADR-0017).
//
// The rotation itself is a short PG txn; the per-shard readback/CAS that follows
// is a separate operation. No external call occurs during the rotation txn.
type IncarnationService struct {
	pool *db.Pool
	now  func() time.Time
}

func NewIncarnationService(pool *db.Pool) *IncarnationService {
	return &IncarnationService{pool: pool, now: time.Now}
}

// RotateIncarnation creates a never-used incarnation recording the source
// (pitr/clone/rewind) and the incarnation it replaces. The new incarnation id
// is deterministic over (replaced, source, time, trace) and guaranteed unused
// by the UNIQUE primary key.
func (s *IncarnationService) RotateIncarnation(ctx context.Context, replacedIncarnationID string, source IncarnationSource, actorRef, traceID string) (*ModelControlIncarnation, error) {
	if err := validateSource(source); err != nil {
		return nil, err
	}
	now := s.now()
	inc := &ModelControlIncarnation{
		IncarnationID:         newIncarnationID(replacedIncarnationID, string(source), now, traceID),
		Source:                source,
		RotatedAtUnixMS:       now.UnixMilli(),
		ReplacedIncarnationID: replacedIncarnationID,
		ActorRef:              actorRef,
		TraceID:               traceID,
	}
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		if replacedIncarnationID != "" {
			var active *string
			if err := tx.QueryRow(ctx, `SELECT active_incarnation_id FROM model_control_state WHERE singleton=true FOR UPDATE`).Scan(&active); err != nil {
				return err
			}
			if active == nil || *active != replacedIncarnationID {
				return fmt.Errorf("model: replaced incarnation is not active")
			}
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO model_control_incarnations (
				incarnation_id, source, rotated_at_unix_ms, replaced_incarnation_id,
				actor_ref, trace_id)
			VALUES ($1,$2,$3,$4,$5,$6)`,
			inc.IncarnationID, string(inc.Source), inc.RotatedAtUnixMS,
			inc.ReplacedIncarnationID, inc.ActorRef, inc.TraceID); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `UPDATE model_control_state
			SET active_incarnation_id=$1,writer_enabled=false,updated_at=now() WHERE singleton=true`, inc.IncarnationID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return fmt.Errorf("model: model_control_state missing")
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("model: rotate incarnation: %w", err)
	}
	return inc, nil
}

// EnableWriter opens ingest/model-control only after every existing shard has
// been revalidated under the newly active incarnation. Restore/clone/rewind
// therefore cannot make an old numeric generation valid again.
func (s *IncarnationService) EnableWriter(ctx context.Context, incarnationID string) error {
	tag, err := s.pool.Pool.Exec(ctx, `
		UPDATE model_control_state m SET writer_enabled=true,updated_at=now()
		WHERE singleton=true AND active_incarnation_id=$1
		  AND NOT EXISTS (SELECT 1 FROM shard_bindings sb
		                  WHERE sb.model_control_incarnation_id<>$1 OR sb.resume_state<>'current')`, incarnationID)
	if err != nil {
		return fmt.Errorf("model: enable writer: %w", err)
	}
	if tag.RowsAffected() != 1 {
		return fmt.Errorf("model: incarnation not active or shard revalidation incomplete")
	}
	return nil
}

func validateSource(s IncarnationSource) error {
	switch s {
	case IncarnationInitial, IncarnationPITR, IncarnationClone, IncarnationRewind:
		return nil
	}
	return fmt.Errorf("model: bad incarnation source %q", s)
}

func newIncarnationID(replaced, source string, t time.Time, trace string) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%d|%s", replaced, source, t.UnixNano(), trace)
	return "inc-" + hex.EncodeToString(h.Sum(nil))[:24]
}
