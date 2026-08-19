package event

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
)

// IngestService validates and durably persists canonical Events from Edge
// InferenceResult batches. It is the ONLY writer of the events table. The
// service enforces commit-before-ACK: an ACK is constructed ONLY after the
// ingest transaction has durably committed (go-control-core-design §4).
type IngestService struct {
	pool            *db.Pool
	now             func() time.Time
	maxBatchRecords int
	maxBatchBytes   int
}

const (
	// These bounds are frozen by contracts/profiles/v1/rust-edge-agent.json
	// control_sink. The service repeats the boundary check so callers that do not
	// enter through gRPC cannot create an unbounded transaction or ACK slice.
	DefaultMaxIngestBatchRecords = 256
	DefaultMaxIngestBatchBytes   = 4 * 1024 * 1024
)

// NewIngestService constructs the ingest service over the given pool.
func NewIngestService(pool *db.Pool) *IngestService {
	return &IngestService{
		pool: pool, now: time.Now,
		maxBatchRecords: DefaultMaxIngestBatchRecords,
		maxBatchBytes:   DefaultMaxIngestBatchBytes,
	}
}

// IngestBatch validates and persists a bounded batch of InferenceResults in ONE
// short PostgreSQL transaction, then returns canonical ACKs. No external call
// occurs during the transaction. An ACK is returned for every input record:
// committed (new), idempotent (replay, same digests), conflict (same key,
// different digest), or rejected (invalid fence / low-quality non-normal).
func (s *IngestService) IngestBatch(ctx context.Context, results []InferenceResult) ([]CanonicalACK, error) {
	if err := db.AssertPoolNotNil(s.pool); err != nil {
		return nil, err
	}
	if len(results) == 0 {
		return nil, nil
	}
	if err := s.validateBatchBounds(results); err != nil {
		return nil, err
	}
	acks := make([]CanonicalACK, 0, len(results))
	// One short transaction for the whole batch; commit before any ACK is
	// materialized for return.
	err := s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		for _, r := range results {
			ack, err := s.ingestOne(ctx, tx, r)
			if err != nil {
				return err
			}
			acks = append(acks, ack)
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("event: ingest batch: %w", err)
	}
	// ACKs are only returned here, AFTER the transaction committed.
	return acks, nil
}

func (s *IngestService) validateBatchBounds(results []InferenceResult) error {
	maxRecords := s.maxBatchRecords
	if maxRecords <= 0 {
		maxRecords = DefaultMaxIngestBatchRecords
	}
	maxBytes := s.maxBatchBytes
	if maxBytes <= 0 {
		maxBytes = DefaultMaxIngestBatchBytes
	}
	if len(results) > maxRecords {
		return fmt.Errorf("event: batch records %d exceed %d", len(results), maxRecords)
	}
	total := 0
	for i := range results {
		sz := estimatedResultBytes(results[i])
		if sz > maxBytes-total {
			return fmt.Errorf("event: batch bytes exceed %d", maxBytes)
		}
		total += sz
	}
	return nil
}

// estimatedResultBytes is a conservative allocation-independent bound over the
// domain object. The public protobuf handler additionally checks proto.Size,
// which accounts for fields (for example scores) that are intentionally not
// persisted in the Event hot table.
func estimatedResultBytes(r InferenceResult) int {
	return 512 + len(r.EventIDempotencyKey) + len(r.InputDigest) + len(r.OutputDigest) +
		len(r.ModelControlIncarnationID) + len(r.ShardID) + len(r.LogicalPoolID) +
		len(r.ModelRevisionDigest) + len(r.ModelBundleDigest) + len(r.FeatureContractDigest) +
		len(r.LabelContractDigest) + len(r.OutputAdapterDigest) + len(r.WireProfileDigest) +
		len(r.RuntimeProfileDigest) + len(r.OptimizationProfileDigest) + len(r.StartupEnvelopeDigest) +
		len(r.PoolObservationDigest) + len(r.BindingDigest) + len(r.Scope) +
		len(r.SourceWindow.SourceID) + len(r.SourceWindow.WindowID) + len(r.Quality) +
		len(r.IngestBatchDigest) + len(r.TraceID) + len(r.ErrorCode) + len(r.WorkerID) +
		len(r.WorkerDigest) + len(r.WorkerAttemptID) + len(r.Scores)*8
}

func (s *IngestService) ingestOne(ctx context.Context, tx *db.Tx, r InferenceResult) (CanonicalACK, error) {
	// Validate the fence dimensions first (no DB access needed).
	if err := validateFence(r); err != nil {
		return CanonicalACK{
			EventIDempotencyKey: r.EventIDempotencyKey,
			InputDigest:         r.InputDigest,
			OutputDigest:        r.OutputDigest,
			CommitStatus:        StatusRejected,
			ReasonCode:          "INVALID_FENCE",
		}, nil
	}
	// Resolve and fence the exact durable binding inside this SAME transaction.
	// A DB error aborts the entire batch; missing or mismatched facts reject the
	// record fail-closed and never become a canonical Event.
	var bindingExact bool
	err := tx.QueryRow(ctx, `
		SELECT EXISTS (
		 SELECT 1 FROM shard_bindings sb
		 JOIN model_control_state mcs ON mcs.singleton=true
		 JOIN pool_generations pg ON pg.logical_pool_id=sb.logical_pool_id AND pg.pool_generation=sb.current_generation
		 WHERE sb.shard_id=$1 AND sb.scope=$2 AND sb.model_control_incarnation_id=$3
		   AND mcs.writer_enabled AND mcs.active_incarnation_id=sb.model_control_incarnation_id
		   AND sb.logical_pool_id=$4 AND sb.current_generation=$5
		   AND sb.current_binding_generation=$6 AND sb.route_epoch=$7 AND sb.resume_state='current'
		   AND pg.status='active' AND pg.capacity_qualified
		   AND pg.model_revision_digest=$8 AND pg.model_bundle_digest=$9
		   AND pg.feature_contract_digest=$10 AND pg.label_contract_digest=$11
		   AND pg.output_adapter_digest=$12 AND pg.wire_profile_digest=$13
		   AND pg.runtime_profile_digest=$14 AND pg.optimization_profile_digest=$15
		   AND pg.startup_envelope_digest=$16 AND pg.pool_observation_digest=$17 AND pg.binding_digest=$18)`,
		r.ShardID, r.Scope, r.ModelControlIncarnationID, r.LogicalPoolID, r.PoolGeneration,
		r.BindingGeneration, r.RouteEpoch, r.ModelRevisionDigest, r.ModelBundleDigest,
		r.FeatureContractDigest, r.LabelContractDigest, r.OutputAdapterDigest,
		r.WireProfileDigest, r.RuntimeProfileDigest, r.OptimizationProfileDigest, r.StartupEnvelopeDigest,
		r.PoolObservationDigest, r.BindingDigest).Scan(&bindingExact)
	if err != nil {
		return CanonicalACK{}, fmt.Errorf("event: resolve exact binding: %w", err)
	}
	if !bindingExact {
		return CanonicalACK{
			EventIDempotencyKey: r.EventIDempotencyKey,
			InputDigest:         r.InputDigest,
			OutputDigest:        r.OutputDigest,
			CommitStatus:        StatusRejected,
			ReasonCode:          "BINDING_FENCE_MISMATCH",
		}, nil
	}
	canonicalID, err := CanonicalEventID(r)
	if err != nil {
		return CanonicalACK{EventIDempotencyKey: r.EventIDempotencyKey, CommitStatus: StatusRejected, ReasonCode: "IDENTITY_ERROR"}, err
	}
	committedAt := s.now().UnixMilli()
	swJSON, err := SourceWindowJSON(r.SourceWindow)
	if err != nil {
		return CanonicalACK{EventIDempotencyKey: r.EventIDempotencyKey, CommitStatus: StatusRejected, ReasonCode: "SERIALIZE_ERROR"}, err
	}
	scoresJSON, err := json.Marshal(r.Scores)
	if err != nil {
		return CanonicalACK{EventIDempotencyKey: r.EventIDempotencyKey, CommitStatus: StatusRejected, ReasonCode: "SERIALIZE_ERROR"}, err
	}
	resultIdentityDigest := ResultIdentityDigest(r)

	// Low-quality results are projected explicitly, NOT as normal committed
	// events. They are still durably recorded with their quality status so the
	// projection namespace is explicit (abstain/OOD/low-quality — §4).
	commitStatus := StatusCommitted
	reasonCode := ReasonForQuality(r.Quality)
	if isLowQuality(r.Quality) {
		// Low-quality records are stored with canonical_event_id set but a
		// distinct reason_code; they are never reported as normal committed.
		reasonCode = ReasonForQuality(r.Quality)
	}

	// Claim the global identity before inserting the time-partitioned Event. A
	// PostgreSQL range-partitioned table cannot own a global uniqueness constraint
	// that omits event_time, so event_identities is the narrow global registry.
	// The registry row and full Event row commit in this SAME transaction.
	identityTag, err := tx.Exec(ctx, `
		INSERT INTO event_identities(event_id,event_idempotency_key,input_digest,output_digest,
		 event_time,committed_at_unix_ms)
		VALUES($1,$2,$3,$4,$5,$6)
		ON CONFLICT DO NOTHING`, canonicalID, r.EventIDempotencyKey, r.InputDigest, r.OutputDigest,
		time.UnixMilli(r.EventTimeUnixMS).UTC(), committedAt)
	if err != nil {
		return CanonicalACK{EventIDempotencyKey: r.EventIDempotencyKey, CommitStatus: StatusRejected, ReasonCode: "IDENTITY_INSERT_ERROR"}, err
	}
	if identityTag.RowsAffected() == 1 {
		_, err = tx.Exec(ctx, `
			INSERT INTO events (
			event_id, event_idempotency_key, input_digest, output_digest,
			canonical_event_id, model_control_incarnation_id, shard_id,
			route_epoch, logical_pool_id, pool_generation, binding_generation,
				source_window_identity, quality, commit_status, event_time,
				committed_at_unix_ms, ingest_batch_digest, trace_id, reason_code,
			scope, feature_contract_digest, label_contract_digest,
				output_adapter_digest, wire_contract_digest, runtime_profile_digest,
					model_revision_digest,model_bundle_digest,optimization_profile_digest,
						startup_envelope_digest,pool_observation_digest,binding_digest,
						result_identity_digest,scores,predicted_label,decision,out_of_distribution,
						abstain,execution_status,execution_error_code,worker_id,worker_digest,
						worker_attempt_id,inference_started_at_unix_ms,inference_completed_at_unix_ms)
					VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,
						        $32,$33::jsonb,$34,$35,$36,$37,$38,$39,$40,$41,$42,$43,$44)`,
			canonicalID, r.EventIDempotencyKey, r.InputDigest, r.OutputDigest,
			canonicalID, r.ModelControlIncarnationID, r.ShardID,
			r.RouteEpoch, r.LogicalPoolID, r.PoolGeneration, r.BindingGeneration,
			swJSON, r.Quality, string(commitStatus),
			time.UnixMilli(r.EventTimeUnixMS).UTC(), committedAt,
			r.IngestBatchDigest, r.TraceID, reasonCode, r.Scope, r.FeatureContractDigest,
			r.LabelContractDigest, r.OutputAdapterDigest, r.WireProfileDigest, r.RuntimeProfileDigest,
			r.ModelRevisionDigest, r.ModelBundleDigest, r.OptimizationProfileDigest,
			r.StartupEnvelopeDigest, r.PoolObservationDigest, r.BindingDigest,
			resultIdentityDigest, string(scoresJSON), int64(r.PredictedLabel), string(r.Decision), r.OutOfDistribution,
			r.Abstain, string(r.ExecutionStatus), r.ErrorCode, r.WorkerID, r.WorkerDigest,
			r.WorkerAttemptID, r.InferenceStartedAtUnixMS, r.InferenceCompletedAtUnixMS)
		if err != nil {
			return CanonicalACK{EventIDempotencyKey: r.EventIDempotencyKey, CommitStatus: StatusRejected, ReasonCode: "INSERT_ERROR"}, err
		}
		if err := projectIncident(ctx, tx, r, canonicalID, committedAt); err != nil {
			return CanonicalACK{EventIDempotencyKey: r.EventIDempotencyKey, CommitStatus: StatusRejected, ReasonCode: "INCIDENT_PROJECTION_ERROR"}, err
		}
		return CanonicalACK{
			EventIDempotencyKey: r.EventIDempotencyKey,
			InputDigest:         r.InputDigest,
			OutputDigest:        r.OutputDigest,
			CanonicalEventID:    canonicalID,
			CommitStatus:        StatusCommitted,
			CommittedAtUnixMS:   committedAt,
			ReasonCode:          reasonCode,
		}, nil
	}
	// No identity inserted -> the idempotency key or canonical event ID already
	// exists. Recall by the idempotency key and compare the entire fence; a
	// canonical-ID collision under a different key fails closed.
	var existingInput, existingOutput, existingCanonical, existingIncarnation, existingShard, existingPool string
	var existingModelRevision, existingModelBundle, existingFeature, existingLabel, existingAdapter, existingWire, existingRuntime, existingScope string
	var existingOptimization, existingStartup, existingPoolObservation, existingBindingDigest string
	var existingResultIdentityDigest string
	var existingRouteEpoch, existingPoolGeneration, existingBindingGeneration int64
	var existingCommitted int64
	err = tx.QueryRow(ctx, `
				SELECT e.input_digest,e.output_digest,e.canonical_event_id,e.committed_at_unix_ms,
				 e.model_control_incarnation_id,e.shard_id,e.route_epoch,e.logical_pool_id,e.pool_generation,e.binding_generation,
				 e.model_revision_digest,e.model_bundle_digest,e.feature_contract_digest,e.label_contract_digest,
					 e.output_adapter_digest,e.wire_contract_digest,e.runtime_profile_digest,e.scope,
						 e.optimization_profile_digest,e.startup_envelope_digest,e.pool_observation_digest,e.binding_digest,
						 e.result_identity_digest
				FROM event_identities ei
				JOIN events e ON e.event_id=ei.event_id AND e.event_time=ei.event_time
				WHERE ei.event_idempotency_key = $1`, r.EventIDempotencyKey).
		Scan(&existingInput, &existingOutput, &existingCanonical, &existingCommitted, &existingIncarnation, &existingShard,
			&existingRouteEpoch, &existingPool, &existingPoolGeneration, &existingBindingGeneration, &existingModelRevision,
			&existingModelBundle, &existingFeature, &existingLabel, &existingAdapter, &existingWire, &existingRuntime, &existingScope,
			&existingOptimization, &existingStartup, &existingPoolObservation, &existingBindingDigest,
			&existingResultIdentityDigest)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return CanonicalACK{EventIDempotencyKey: r.EventIDempotencyKey, CommitStatus: StatusRejected, ReasonCode: "IDENTITY_CONFLICT"}, nil
		}
		return CanonicalACK{EventIDempotencyKey: r.EventIDempotencyKey, CommitStatus: StatusRejected, ReasonCode: "RECALL_ERROR"}, err
	}
	if existingInput == r.InputDigest && existingOutput == r.OutputDigest && existingIncarnation == r.ModelControlIncarnationID &&
		existingShard == r.ShardID && existingRouteEpoch == r.RouteEpoch && existingPool == r.LogicalPoolID &&
		existingPoolGeneration == r.PoolGeneration && existingBindingGeneration == r.BindingGeneration &&
		existingModelRevision == r.ModelRevisionDigest && existingModelBundle == r.ModelBundleDigest &&
		existingFeature == r.FeatureContractDigest && existingLabel == r.LabelContractDigest && existingAdapter == r.OutputAdapterDigest &&
		existingWire == r.WireProfileDigest && existingRuntime == r.RuntimeProfileDigest && existingScope == r.Scope &&
		existingOptimization == r.OptimizationProfileDigest && existingStartup == r.StartupEnvelopeDigest &&
		existingPoolObservation == r.PoolObservationDigest && existingBindingDigest == r.BindingDigest &&
		existingResultIdentityDigest == resultIdentityDigest {
		return CanonicalACK{
			EventIDempotencyKey: r.EventIDempotencyKey,
			InputDigest:         r.InputDigest,
			OutputDigest:        r.OutputDigest,
			CanonicalEventID:    existingCanonical,
			CommitStatus:        StatusIdempotent,
			CommittedAtUnixMS:   existingCommitted,
			ReasonCode:          "IDEMPOTENT",
		}, nil
	}
	// Same key, different digest -> stable conflict. No canonical Event for
	// the new submission; the original fact is preserved.
	return CanonicalACK{
		EventIDempotencyKey: r.EventIDempotencyKey,
		InputDigest:         r.InputDigest,
		OutputDigest:        r.OutputDigest,
		CommitStatus:        StatusConflict,
		ReasonCode:          "DIGEST_CONFLICT",
	}, nil
}

// projectIncident derives a rebuildable one-alert/one-incident projection. It
// never mutates the Event and never turns abstain/OOD/low-quality/error output
// into an actionable incident. Later rollups may group these stable projection
// events without changing the originating Event identity.
func projectIncident(ctx context.Context, tx *db.Tx, r InferenceResult, eventID string, committedAtUnixMS int64) error {
	if r.Decision != DecisionAlert || r.ExecutionStatus != ExecutionOK || r.Quality != QualityValid ||
		r.Abstain || r.OutOfDistribution {
		return nil
	}
	incidentID := "inc-" + eventID[len("evt-"):]
	if _, err := tx.Exec(ctx, `INSERT INTO incidents(incident_id,severity,status,first_event_id,last_event_id,
		scope,actor_ref,trace_id,reason_code,created_at,updated_at)
		VALUES($1,'high','open',$2,$2,$3,NULL,$4,'DETECTION_ALERT',to_timestamp($5::double precision/1000.0),
		       to_timestamp($5::double precision/1000.0)) ON CONFLICT (incident_id) DO NOTHING`,
		incidentID, eventID, r.Scope, r.TraceID, committedAtUnixMS); err != nil {
		return fmt.Errorf("event: project incident current: %w", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO incident_projection_events(
		projection_event_id,incident_id,event_id,decision,predicted_label,severity,scope,
		reason_code,trace_id,projected_at_unix_ms)
		VALUES($1,$2,$3,$4,$5,'high',$6,'DETECTION_ALERT',$7,$8)
		ON CONFLICT (projection_event_id) DO NOTHING`, "ipe-"+eventID[len("evt-"):], incidentID,
		eventID, string(r.Decision), int64(r.PredictedLabel), r.Scope, r.TraceID, committedAtUnixMS); err != nil {
		return fmt.Errorf("event: append incident projection event: %w", err)
	}
	return nil
}
