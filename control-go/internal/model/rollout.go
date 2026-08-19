package model

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"sort"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
)

// RolloutService executes the durable rollout/rollback/recovery sequence.
//
// Phase order (go-control-core-design §9, ADR-0017):
//
//	durable operation (short txn, planned)
//	→ [outside tx] deployment adapter: StagePool → StartReplicas(readback) →
//	  WarmupAndCapacity
//	→ readback digest/capacity verification (fail → operation failed, old
//	  current untouched)
//	→ [outside tx] Edge route-withdraw for the CURRENT generation
//	→ short PG CAS: pool generation active + shard current/previous swap +
//	  route_epoch bump + cas_digest (status cas-committing)
//	→ [outside tx] Edge committed-binding handshake + drain of the old
//	  generation
//	→ handshake ok → operation applied; handshake failed AFTER CAS → the new
//	  current is KEPT with resume_pending and reconciliation proceeds along the
//	  original identity (no silent rollback, never auto-switch on error).
//
// CRITICAL INVARIANT: every DeploymentAdapterClient / EdgeRouteClient call
// happens OUTSIDE any DB transaction. Only the durable operation append, the
// CAS swap, and the terminal status transition are short transactions.
type RolloutService struct {
	pool       *db.Pool
	deployment DeploymentAdapterClient
	edge       EdgeRouteClient
	now        func() time.Time
}

func NewRolloutService(pool *db.Pool, deployment DeploymentAdapterClient, edge EdgeRouteClient) *RolloutService {
	return &RolloutService{pool: pool, deployment: deployment, edge: edge, now: time.Now}
}

// RolloutOutcome is the terminal result of a Rollout call.
type RolloutOutcome struct {
	OperationID   string          `json:"operation_id"`
	ShardID       string          `json:"shard_id"`
	Status        OperationStatus `json:"status"`
	RouteEpoch    int64           `json:"route_epoch"`
	NewGeneration int64           `json:"new_generation"`
	ResumeState   ResumeState     `json:"resume_state"`
	ReasonCode    string          `json:"reason_code"`
}

// RolloutRequest is the input to Rollout. For kind=rollback the TargetRevisionID
// MUST equal the shard's exact previous_revision_id and TargetGeneration is
// derived from the binding (never caller-chosen).
type RolloutRequest struct {
	OperationID               string
	ShardID                   string
	LogicalPoolID             string
	TargetGeneration          int64
	TargetRevisionID          string
	Kind                      OperationKind
	ActorRef                  string
	ReasonCode                string
	TraceID                   string
	Scope                     string
	WireProfile               string
	WireProfileDigest         string
	RuntimeProfileDigest      string
	OptimizationProfileDigest string
	AvailabilityProfile       string
	MinReadyReplicas          int
	ModelControlIncarnationID string
	EdgeWorkloadRef           string
}

// Rollout runs one shard's rollout to the target generation/revision.
func (s *RolloutService) Rollout(ctx context.Context, req RolloutRequest) (*RolloutOutcome, error) {
	if req.Kind != OpRollout && req.Kind != OpRollback {
		return nil, fmt.Errorf("model: unsupported kind %q (use Rollout; recovery is reconcile-driven)", req.Kind)
	}
	if req.Kind == OpRollout && req.TargetGeneration < 1 {
		return nil, errors.New("model: target_generation >= 1 required")
	}
	if req.OperationID == "" || req.ShardID == "" || req.LogicalPoolID == "" || req.Scope == "" || req.ModelControlIncarnationID == "" ||
		req.WireProfile != "inference-central-grpc-batch/v1" ||
		!digestRE.MatchString(req.WireProfileDigest) || !digestRE.MatchString(req.RuntimeProfileDigest) || !digestRE.MatchString(req.OptimizationProfileDigest) {
		return nil, errors.New("model: operation/shard/pool/scope and exact wire/runtime profiles required")
	}
	if req.MinReadyReplicas < 1 {
		return nil, errors.New("model: min_ready_replicas >=1 required")
	}

	// 1) Freeze the exact assigned Edge workload, then load revision + binding
	// state (read-only, no txn held after).
	workload, err := s.currentEdgeWorkload(ctx, req.ShardID)
	if err != nil {
		return nil, err
	}
	if req.EdgeWorkloadRef != "" && req.EdgeWorkloadRef != workload {
		return nil, errors.New("model: caller Edge workload differs from current assignment")
	}
	req.EdgeWorkloadRef = workload
	rev, binding, err := s.loadRevisionAndBinding(ctx, req)
	if err != nil {
		return nil, err
	}
	// Rollback must target the EXACT previous of the binding.
	if req.Kind == OpRollback {
		if binding.PreviousRevisionID == "" || req.TargetRevisionID != binding.PreviousRevisionID {
			return nil, errors.New("model: rollback must target the exact previous revision")
		}
		if binding.PreviousGeneration < 1 {
			return nil, errors.New("model: rollback has no exact previous generation")
		}
		req.TargetGeneration = binding.PreviousGeneration
	}
	// Cross-incarnation fence: the operation must carry the binding's
	// incarnation. A rotated (PITR) incarnation invalidates stale plans.
	if req.Kind == OpRollout && req.TargetGeneration < binding.CurrentGeneration {
		return nil, errors.New("model: rollout generation must advance monotonically")
	}
	if req.Kind == OpRollout && req.TargetGeneration == binding.CurrentGeneration && req.TargetRevisionID != binding.CurrentRevisionID {
		return nil, errors.New("model: same pool generation cannot change model identity")
	}
	if req.Kind == OpRollout && req.TargetGeneration == binding.CurrentGeneration &&
		req.TargetRevisionID == binding.CurrentRevisionID {
		// Same generation + same revision = idempotent no-op.
		return &RolloutOutcome{
			OperationID: req.OperationID, ShardID: req.ShardID, Status: OpApplied,
			RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration,
			ResumeState: ResumeCurrent, ReasonCode: "ALREADY_CURRENT",
		}, nil
	}

	// 2) Durable operation (short txn, planned → staging).
	op := RolloutOperation{
		OperationID: req.OperationID, ShardID: req.ShardID, LogicalPoolID: req.LogicalPoolID,
		TargetGeneration: req.TargetGeneration, PreviousGeneration: binding.CurrentGeneration,
		OperationKind: req.Kind, Status: OpPlanned,
		StartedAtUnixMS: s.now().UnixMilli(), ActorRef: req.ActorRef,
		ReasonCode: req.ReasonCode, TraceID: req.TraceID,
		RequestDigest:             rolloutRequestDigest(req, binding.ModelControlIncarnationID),
		ModelControlIncarnationID: binding.ModelControlIncarnationID, Scope: req.Scope,
		TargetRevisionID: req.TargetRevisionID, EdgeWorkloadRef: req.EdgeWorkloadRef,
	}
	if err := s.appendOperation(ctx, &op); err != nil {
		return nil, err
	}
	fail := func(status OperationStatus, reason string, cause error) (*RolloutOutcome, error) {
		_ = s.finishOperation(ctx, req.OperationID, status, reason)
		o := &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID,
			Status: status, RouteEpoch: binding.RouteEpoch,
			NewGeneration: binding.CurrentGeneration, ResumeState: binding.ResumeState, ReasonCode: reason}
		if cause != nil {
			return o, fmt.Errorf("model: %s: %w", reason, cause)
		}
		return o, nil
	}

	// 3) Pool generation row for the target (short txn; staged before external
	// calls so the envelope identity is durable).
	pool, err := s.upsertPoolGeneration(ctx, req, rev, binding.CurrentBindingGeneration+1)
	if err != nil {
		return fail(OpFailed, "POOL_GENERATION_ERROR", err)
	}
	if err := s.setOperationStatus(ctx, req.OperationID, OpStaging, "STAGED"); err != nil {
		return fail(OpFailed, "OPERATION_STATUS_ERROR", err)
	}

	// Reuse an already qualified exact pool generation for later shards in the
	// same ordered rollout. Otherwise perform the external deployment sequence.
	observationID, readback, capacity, reused, err := s.loadReusablePoolObservation(ctx, *pool)
	if err != nil {
		return fail(OpFailed, "POOL_OBSERVATION_LOAD_ERROR", err)
	}
	if !reused {
		// ---- External: deployment adapter (NO DB txn open) ----
		if err := s.deployment.StagePool(ctx, *pool); err != nil {
			return fail(OpFailed, "STAGE_ERROR", err)
		}
		if err := s.setOperationStatus(ctx, req.OperationID, OpWarming, "WARMING"); err != nil {
			return fail(OpFailed, "OPERATION_STATUS_ERROR", err)
		}
		readback, err = s.deployment.StartReplicas(ctx, *pool)
		if err != nil {
			return fail(OpFailed, "START_REPLICAS_ERROR", err)
		}
		capacity, err = s.deployment.WarmupAndCapacity(ctx, *pool)
		if err != nil {
			return fail(OpFailed, "WARMUP_CAPACITY_ERROR", err)
		}
	}
	if !readback.Loaded || readback.LogicalPoolID != pool.LogicalPoolID ||
		readback.PoolGeneration != pool.PoolGeneration || readback.ModelRevisionDigest != pool.ModelRevisionDigest ||
		(!reused && (readback.BindingGeneration != pool.BindingGeneration || readback.ModelControlIncarnationID != pool.ModelControlIncarnationID ||
			readback.OperationID != pool.OperationID)) ||
		readback.ModelBundleDigest != pool.ModelBundleDigest || readback.FeatureContractDigest != pool.FeatureContractDigest ||
		readback.LabelContractDigest != pool.LabelContractDigest || readback.OutputAdapterDigest != pool.OutputAdapterDigest ||
		readback.WireProfileDigest != pool.WireProfileDigest ||
		readback.RuntimeProfileDigest != pool.RuntimeProfileDigest || readback.OptimizationProfileDigest != pool.OptimizationProfileDigest ||
		readback.WireProfile != pool.WireProfile || readback.RuntimeProfile != pool.RuntimeProfile ||
		readback.AvailabilityProfile != req.AvailabilityProfile ||
		readback.ReadyReplicas < req.MinReadyReplicas {
		return fail(OpFailed, "READBACK_NOT_LOADED", nil)
	}
	if readback.StartupEnvelopeDigest != pool.StartupEnvelopeDigest ||
		readback.PoolObservationDigest != pool.PoolObservationDigest ||
		readback.BindingDigest != pool.BindingDigest {
		return fail(OpFailed, "READBACK_DIGEST_MISMATCH", nil)
	}
	if err := validatePoolReadback(readback, req.MinReadyReplicas); err != nil {
		return fail(OpFailed, "READBACK_ROUTE_IDENTITY_MISSING", nil)
	}
	if !capacity.Qualified || !capacity.MinReadyMet || capacity.LogicalPoolID != pool.LogicalPoolID ||
		capacity.PoolGeneration != pool.PoolGeneration {
		return fail(OpFailed, "CAPACITY_NOT_QUALIFIED", nil)
	}
	if !reused {
		observationID, err = s.recordPoolObservation(ctx, req, *pool, readback, capacity)
		if err != nil {
			return fail(OpFailed, "OBSERVATION_PERSIST_ERROR", err)
		}
	}

	// ---- External: Edge prepare (withdraw/drain/WAL) and exact route install
	// readback (NO DB txn open). CommitRoute leaves the new route READY but not
	// active; only ResumeRoute after the PostgreSQL CAS can reopen admission. ----
	if err := s.setOperationStatus(ctx, req.OperationID, OpRouteWithdrawing, "ROUTE_WITHDRAWING"); err != nil {
		return fail(OpFailed, "OPERATION_STATUS_ERROR", err)
	}
	proposedRouteEpoch := binding.RouteEpoch
	if ShouldRaiseRouteEpoch(binding.CurrentGeneration, req.TargetGeneration, binding.CurrentRevisionID, req.TargetRevisionID) {
		proposedRouteEpoch++
	}
	prepared, err := s.edge.PrepareRoute(ctx, RoutePrepare{
		SchemaVersion:                    "inference-route-prepare/v1",
		ShardID:                          req.ShardID,
		CurrentModelControlIncarnationID: binding.ModelControlIncarnationID,
		CurrentRouteEpoch:                binding.RouteEpoch,
		ProposedRouteEpoch:               proposedRouteEpoch,
		TraceID:                          req.TraceID,
		EdgeWorkloadRef:                  req.EdgeWorkloadRef,
	})
	if err != nil {
		_ = s.markUnavailable(ctx, req, *binding, "ROUTE_WITHDRAW_UNKNOWN")
		_ = s.finishOperation(ctx, req.OperationID, OpResumePending, "ROUTE_WITHDRAW_UNKNOWN")
		return &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID, Status: OpResumePending, RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration, ResumeState: ResumeUnavailable, ReasonCode: "ROUTE_WITHDRAW_UNKNOWN"}, err
	}
	if prepared.ShardID != req.ShardID || prepared.RouteEpoch != proposedRouteEpoch || prepared.State != "draining" {
		_ = s.markUnavailable(ctx, req, *binding, "ROUTE_PREPARE_READBACK_MISMATCH")
		_ = s.finishOperation(ctx, req.OperationID, OpResumePending, "ROUTE_PREPARE_READBACK_MISMATCH")
		return &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID, Status: OpResumePending, RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration, ResumeState: ResumeUnavailable, ReasonCode: "ROUTE_PREPARE_READBACK_MISMATCH"}, nil
	}
	route := InferenceRoute{
		SchemaVersion: "inference-route/v1", ShardID: req.ShardID,
		ModelControlIncarnationID: binding.ModelControlIncarnationID,
		LogicalPoolID:             req.LogicalPoolID, PoolGeneration: req.TargetGeneration,
		BindingGeneration: binding.CurrentBindingGeneration + 1, RouteEpoch: proposedRouteEpoch,
		ModelRevisionDigest: rev.ModelRevisionDigest, FeatureContractDigest: rev.FeatureContractDigest,
		LabelContractDigest: rev.LabelContractDigest, OutputAdapterDigest: rev.OutputAdapterDigest,
		WireProfile: req.WireProfile, RuntimeProfile: rev.ReaderRuntimeProfile,
		Endpoint: readback.Endpoint, TLS: TLSClientIdentity{ServerName: readback.TLSServerName, IdentityRef: readback.TLSIdentityRef},
		OperationID: req.OperationID, Scope: req.Scope, EdgeWorkloadRef: req.EdgeWorkloadRef,
		ExpectedBindingGeneration: binding.CurrentBindingGeneration,
		ProposedBindingGeneration: binding.CurrentBindingGeneration + 1,
		CurrentBindingGeneration:  binding.CurrentBindingGeneration + 1,
		StartupEnvelopeDigest:     pool.StartupEnvelopeDigest, PoolObservationDigest: pool.PoolObservationDigest,
		BindingDigest: pool.BindingDigest, ModelBundleDigest: rev.ModelBundleDigest,
		WireProfileDigest: req.WireProfileDigest, RuntimeProfileDigest: req.RuntimeProfileDigest,
		OptimizationProfileDigest: req.OptimizationProfileDigest,
	}
	committed, err := s.edge.CommitRoute(ctx, route, req.TraceID)
	if err != nil {
		_ = s.markUnavailable(ctx, req, *binding, "ROUTE_COMMIT_UNKNOWN")
		_ = s.finishOperation(ctx, req.OperationID, OpResumePending, "ROUTE_COMMIT_UNKNOWN")
		return &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID, Status: OpResumePending, RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration, ResumeState: ResumeUnavailable, ReasonCode: "ROUTE_COMMIT_UNKNOWN"}, err
	}
	if committed.ShardID != req.ShardID || committed.RouteEpoch != proposedRouteEpoch || committed.State != "ready" ||
		committed.BindingDigest != pool.BindingDigest || committed.PendingInputs != 0 || committed.PendingResults != 0 {
		_ = s.markUnavailable(ctx, req, *binding, "ROUTE_COMMIT_READBACK_MISMATCH")
		_ = s.finishOperation(ctx, req.OperationID, OpResumePending, "ROUTE_COMMIT_READBACK_MISMATCH")
		return &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID, Status: OpResumePending, RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration, ResumeState: ResumeUnavailable, ReasonCode: "ROUTE_COMMIT_READBACK_MISMATCH"}, nil
	}

	// 4) Short PG CAS: swap current/previous, bump route epoch (a generation/
	// model/runtime-profile change MUST raise the route epoch), fence with
	// cas_digest.
	if err := s.setOperationStatus(ctx, req.OperationID, OpCASCommitting, "CAS_COMMITTING"); err != nil {
		_ = s.markUnavailable(ctx, req, *binding, "OPERATION_STATUS_ERROR")
		_ = s.finishOperation(ctx, req.OperationID, OpResumePending, "OPERATION_STATUS_ERROR_AFTER_DRAIN")
		return &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID, Status: OpResumePending, RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration, ResumeState: ResumeUnavailable, ReasonCode: "OPERATION_STATUS_ERROR_AFTER_DRAIN"}, err
	}
	casDigest := computeCASDigest(req.OperationID, req.ShardID, req.TargetGeneration, req.TargetRevisionID)
	newBinding, err := s.casSwapBinding(ctx, req, binding, casDigest, observationID, pool.PoolObservationDigest)
	if err != nil {
		_ = s.markUnavailable(ctx, req, *binding, "CAS_SWAP_ERROR")
		_ = s.finishOperation(ctx, req.OperationID, OpResumePending, "CAS_SWAP_ERROR_AFTER_DRAIN")
		return &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID, Status: OpResumePending, RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration, ResumeState: ResumeUnavailable, ReasonCode: "CAS_SWAP_ERROR_AFTER_DRAIN"}, err
	}

	// 5) Committed-binding handshake (external, AFTER the CAS).
	if err := s.setOperationStatus(ctx, req.OperationID, OpHandshakePending, "HANDSHAKE_PENDING"); err != nil {
		_ = s.markResumePending(ctx, req, *newBinding, "STATUS_RECORD_ERROR")
		return &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID,
			Status: OpResumePending, RouteEpoch: newBinding.RouteEpoch,
			NewGeneration: req.TargetGeneration, ResumeState: ResumePending, ReasonCode: "STATUS_RECORD_ERROR"}, err
	}
	hs := CommittedBindingHandshake{
		ModelControlIncarnationID: binding.ModelControlIncarnationID,
		OperationID:               req.OperationID,
		Scope:                     req.Scope,
		ShardID:                   req.ShardID,
		RouteEpoch:                newBinding.RouteEpoch,
		LogicalPoolID:             req.LogicalPoolID,
		PoolGeneration:            req.TargetGeneration,
		ExpectedBindingGeneration: newBinding.CurrentBindingGeneration - 1,
		ProposedBindingGeneration: newBinding.CurrentBindingGeneration,
		CurrentBindingGeneration:  newBinding.CurrentBindingGeneration,
		StartupEnvelopeDigest:     pool.StartupEnvelopeDigest,
		PoolObservationDigest:     pool.PoolObservationDigest,
		BindingDigest:             pool.BindingDigest,
		DeadlineUnixMS:            s.now().Add(30 * time.Second).UnixMilli(),
		EdgeWorkloadRef:           req.EdgeWorkloadRef,
	}
	resumed, err := s.edge.ResumeRoute(ctx, hs, req.TraceID)
	if err != nil || resumed.ShardID != req.ShardID || resumed.RouteEpoch != newBinding.RouteEpoch ||
		resumed.State != "active" || resumed.BindingDigest != pool.BindingDigest {
		// Handshake failed AFTER the CAS: KEEP the new current; resume_pending
		// reconciles along the original identity. No silent rollback.
		_ = s.markResumePending(ctx, req, *newBinding, "HANDSHAKE_NOT_CONFIRMED")
		_ = s.finishOperation(ctx, req.OperationID, OpResumePending, "HANDSHAKE_NOT_CONFIRMED")
		reason := "HANDSHAKE_NOT_CONFIRMED"
		if err != nil {
			reason = "HANDSHAKE_ERROR"
		}
		return &RolloutOutcome{OperationID: req.OperationID, ShardID: req.ShardID,
			Status: OpResumePending, RouteEpoch: newBinding.RouteEpoch,
			NewGeneration: req.TargetGeneration, ResumeState: ResumePending, ReasonCode: reason}, err
	}

	// 6) The old route was drained before the CAS; terminal applied now records
	// the completed committed-binding handshake.
	if err := s.finishOperation(ctx, req.OperationID, OpApplied, "APPLIED"); err != nil {
		return nil, err
	}
	return &RolloutOutcome{
		OperationID: req.OperationID, ShardID: req.ShardID, Status: OpApplied,
		RouteEpoch: newBinding.RouteEpoch, NewGeneration: req.TargetGeneration,
		ResumeState: ResumeCurrent, ReasonCode: "APPLIED",
	}, nil
}

// ShouldRaiseRouteEpoch is the pure decision for route-epoch bumps: a
// generation/model/runtime-profile change raises the epoch; a same-generation
// replica restart does NOT (same route).
func ShouldRaiseRouteEpoch(currentGen, targetGen int64, currentRev, targetRev string) bool {
	return targetGen != currentGen || currentRev != targetRev
}

// computeCASDigest derives the deterministic CAS fence digest for one swap.
func computeCASDigest(operationID, shardID string, targetGen int64, targetRev string) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%d|%s", operationID, shardID, targetGen, targetRev)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func (s *RolloutService) loadRevisionAndBinding(ctx context.Context, req RolloutRequest) (*ModelRevision, *ShardBinding, error) {
	var rev ModelRevision
	var qual string
	err := s.pool.Pool.QueryRow(ctx, `
			SELECT model_revision_id, model_revision_digest, model_bundle_digest,
			       feature_contract_digest,label_contract_digest,output_adapter_digest,
			       reader_runtime_profile, qualification_status, scope
			FROM model_revisions WHERE model_revision_id = $1`, req.TargetRevisionID).
		Scan(&rev.ModelRevisionID, &rev.ModelRevisionDigest, &rev.ModelBundleDigest,
			&rev.FeatureContractDigest, &rev.LabelContractDigest, &rev.OutputAdapterDigest,
			&rev.ReaderRuntimeProfile, &qual, &rev.Scope)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil, fmt.Errorf("model: revision %s not found", req.TargetRevisionID)
		}
		return nil, nil, fmt.Errorf("model: load revision: %w", err)
	}
	if qual != string(Qualified) {
		return nil, nil, fmt.Errorf("model: revision %s qualification is %s (must be qualified)", req.TargetRevisionID, qual)
	}
	var b ShardBinding
	var resume string
	var prevGen, prevBindingGen *int64
	var prevRevision *string
	err = s.pool.Pool.QueryRow(ctx, `
			SELECT shard_id, logical_pool_id, model_control_incarnation_id, current_generation,
			       previous_generation, current_binding_generation, current_revision_id,
			       previous_revision_id, previous_binding_generation, route_epoch, resume_state, cas_digest, scope
			FROM shard_bindings sb JOIN model_control_state mcs ON mcs.singleton=true
			WHERE shard_id = $1 AND mcs.writer_enabled
			  AND mcs.active_incarnation_id=sb.model_control_incarnation_id`, req.ShardID).
		Scan(&b.ShardID, &b.LogicalPoolID, &b.ModelControlIncarnationID, &b.CurrentGeneration,
			&prevGen, &b.CurrentBindingGeneration, &b.CurrentRevisionID,
			&prevRevision, &prevBindingGen, &b.RouteEpoch, &resume, &b.CASDigest, &b.Scope)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil, fmt.Errorf("model: shard binding %s not found (bootstrap required)", req.ShardID)
		}
		return nil, nil, fmt.Errorf("model: load shard binding: %w", err)
	}
	if rev.Scope != req.Scope {
		return nil, nil, fmt.Errorf("model: revision scope mismatch")
	}
	b.ResumeState = ResumeState(resume)
	if prevGen != nil {
		b.PreviousGeneration = *prevGen
	}
	if prevBindingGen != nil {
		b.PreviousBindingGeneration = *prevBindingGen
	}
	if prevRevision != nil {
		b.PreviousRevisionID = *prevRevision
	}
	if b.LogicalPoolID != req.LogicalPoolID {
		return nil, nil, fmt.Errorf("model: shard %s belongs to pool %s not %s", req.ShardID, b.LogicalPoolID, req.LogicalPoolID)
	}
	if b.Scope != req.Scope {
		return nil, nil, fmt.Errorf("model: shard scope mismatch")
	}
	if b.ModelControlIncarnationID != req.ModelControlIncarnationID {
		return nil, nil, fmt.Errorf("model: model-control incarnation fence mismatch")
	}
	// Runtime-profile compatibility: CPU<->CUDA requires a NEW pool
	// generation, and the revision's reader runtime must match the pool's.
	var poolRuntime, availability string
	if err := s.pool.Pool.QueryRow(ctx,
		`SELECT runtime_profile, availability_profile FROM logical_pools WHERE logical_pool_id = $1`, req.LogicalPoolID).
		Scan(&poolRuntime, &availability); err != nil {
		return nil, nil, fmt.Errorf("model: load pool runtime: %w", err)
	}
	if rev.ReaderRuntimeProfile != poolRuntime {
		return nil, nil, fmt.Errorf("model: revision runtime %s != pool runtime %s (new generation with matching profile required)", rev.ReaderRuntimeProfile, poolRuntime)
	}
	if availability != req.AvailabilityProfile {
		return nil, nil, fmt.Errorf("model: availability profile mismatch")
	}
	return &rev, &b, nil
}

func (s *RolloutService) upsertPoolGeneration(ctx context.Context, req RolloutRequest, rev *ModelRevision, proposedBindingGeneration int64) (*PoolGeneration, error) {
	envelope := PoolStartupEnvelope{
		LogicalPoolID: req.LogicalPoolID, PoolGeneration: req.TargetGeneration,
		ModelRevisionDigest:   rev.ModelRevisionDigest,
		StartupEnvelopeDigest: digestOf(envelopeString(req, rev)),
		PoolObservationDigest: digestOf(fmt.Sprintf("observation|%s|%d|%s|%s|%s", req.LogicalPoolID, req.TargetGeneration, rev.ModelRevisionDigest, req.RuntimeProfileDigest, req.OptimizationProfileDigest)),
		BindingDigest:         digestOf(fmt.Sprintf("pool-binding|%s|%d|%s|%s|%s|%s", req.LogicalPoolID, req.TargetGeneration, rev.ModelRevisionDigest, req.WireProfileDigest, req.RuntimeProfileDigest, req.OptimizationProfileDigest)),
		AvailabilityProfile:   req.AvailabilityProfile,
		RuntimeProfile:        rev.ReaderRuntimeProfile,
	}
	pg := PoolGeneration{
		LogicalPoolID: req.LogicalPoolID, PoolGeneration: req.TargetGeneration,
		ModelControlIncarnationID: req.ModelControlIncarnationID, OperationID: req.OperationID,
		Scope: req.Scope, BindingGeneration: proposedBindingGeneration,
		ModelRevisionID:       req.TargetRevisionID,
		StartupEnvelopeDigest: envelope.StartupEnvelopeDigest,
		PoolObservationDigest: envelope.PoolObservationDigest,
		BindingDigest:         envelope.BindingDigest,
		Status:                PoolWarming, MinReadyReplicas: req.MinReadyReplicas, CapacityQualified: false,
		ModelRevisionDigest: rev.ModelRevisionDigest, ModelBundleDigest: rev.ModelBundleDigest,
		FeatureContractDigest: rev.FeatureContractDigest, LabelContractDigest: rev.LabelContractDigest,
		OutputAdapterDigest: rev.OutputAdapterDigest, WireProfileDigest: req.WireProfileDigest,
		RuntimeProfileDigest:      req.RuntimeProfileDigest,
		OptimizationProfileDigest: req.OptimizationProfileDigest,
		WireProfile:               req.WireProfile, RuntimeProfile: rev.ReaderRuntimeProfile,
		AvailabilityProfile: req.AvailabilityProfile,
	}
	err := s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `
				INSERT INTO pool_generations (
					logical_pool_id, pool_generation, model_revision_id,
					startup_envelope_digest, pool_observation_digest, binding_digest,
					status, min_ready_replicas, capacity_qualified,
					model_revision_digest,model_bundle_digest,feature_contract_digest,
					label_contract_digest,output_adapter_digest,wire_profile_digest,runtime_profile_digest,optimization_profile_digest)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,false,$9,$10,$11,$12,$13,$14,$15,$16)
				ON CONFLICT (logical_pool_id, pool_generation) DO NOTHING`,
			pg.LogicalPoolID, pg.PoolGeneration, pg.ModelRevisionID,
			pg.StartupEnvelopeDigest, pg.PoolObservationDigest, pg.BindingDigest,
			string(pg.Status), pg.MinReadyReplicas, pg.ModelRevisionDigest, pg.ModelBundleDigest,
			pg.FeatureContractDigest, pg.LabelContractDigest, pg.OutputAdapterDigest, pg.WireProfileDigest, pg.RuntimeProfileDigest, pg.OptimizationProfileDigest)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 1 {
			return nil
		}
		var existing PoolGeneration
		var status string
		err = tx.QueryRow(ctx, `SELECT model_revision_id,startup_envelope_digest,pool_observation_digest,
			binding_digest,status,min_ready_replicas,capacity_qualified,model_revision_digest,
			model_bundle_digest,feature_contract_digest,label_contract_digest,output_adapter_digest,
			wire_profile_digest,runtime_profile_digest,optimization_profile_digest FROM pool_generations
			WHERE logical_pool_id=$1 AND pool_generation=$2 FOR UPDATE`, pg.LogicalPoolID, pg.PoolGeneration).
			Scan(&existing.ModelRevisionID, &existing.StartupEnvelopeDigest, &existing.PoolObservationDigest,
				&existing.BindingDigest, &status, &existing.MinReadyReplicas, &existing.CapacityQualified,
				&existing.ModelRevisionDigest, &existing.ModelBundleDigest, &existing.FeatureContractDigest,
				&existing.LabelContractDigest, &existing.OutputAdapterDigest, &existing.WireProfileDigest, &existing.RuntimeProfileDigest, &existing.OptimizationProfileDigest)
		if err != nil {
			return err
		}
		if existing.ModelRevisionID != pg.ModelRevisionID || existing.StartupEnvelopeDigest != pg.StartupEnvelopeDigest ||
			existing.PoolObservationDigest != pg.PoolObservationDigest || existing.BindingDigest != pg.BindingDigest ||
			existing.MinReadyReplicas != pg.MinReadyReplicas || existing.ModelRevisionDigest != pg.ModelRevisionDigest ||
			existing.ModelBundleDigest != pg.ModelBundleDigest || existing.FeatureContractDigest != pg.FeatureContractDigest ||
			existing.LabelContractDigest != pg.LabelContractDigest || existing.OutputAdapterDigest != pg.OutputAdapterDigest ||
			existing.WireProfileDigest != pg.WireProfileDigest || existing.RuntimeProfileDigest != pg.RuntimeProfileDigest || existing.OptimizationProfileDigest != pg.OptimizationProfileDigest {
			return errors.New("model: immutable pool generation identity conflict")
		}
		pg.Status = PoolGenerationStatus(status)
		pg.CapacityQualified = existing.CapacityQualified
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("model: upsert pool generation: %w", err)
	}
	return &pg, nil
}

func validatePoolReadback(readback Readback, minReadyReplicas int) error {
	endpoint, err := url.Parse(readback.Endpoint)
	if err != nil || endpoint.Scheme != "https" || endpoint.Host == "" || endpoint.User != nil || endpoint.Fragment != "" {
		return errors.New("model: readback endpoint must be an absolute credential-free HTTPS URL")
	}
	if readback.TLSServerName == "" || len(readback.TLSServerName) > 253 ||
		readback.TLSIdentityRef == "" || len(readback.TLSIdentityRef) > 128 ||
		readback.ReadbackAttemptID == "" || len(readback.ReadbackAttemptID) > 128 || readback.ObservedAtUnixMS < 1 {
		return errors.New("model: readback route/TLS/attempt identity missing or unbounded")
	}
	if len(readback.EligibleWorkers) < minReadyReplicas || len(readback.EligibleWorkers) > 256 ||
		readback.ReadyReplicas != len(readback.EligibleWorkers) {
		return errors.New("model: readback eligible worker vector does not match ready replicas")
	}
	workers := append([]WorkerIdentity(nil), readback.EligibleWorkers...)
	sort.Slice(workers, func(i, j int) bool { return workers[i].WorkerID < workers[j].WorkerID })
	for i, worker := range workers {
		if worker.WorkerID == "" || len(worker.WorkerID) > 128 || !digestRE.MatchString(worker.WorkerDigest) ||
			(i > 0 && workers[i-1].WorkerID == worker.WorkerID) {
			return errors.New("model: readback eligible worker identity malformed or duplicated")
		}
	}
	return nil
}

func (s *RolloutService) loadReusablePoolObservation(ctx context.Context, pool PoolGeneration) (string, Readback, CapacityResult, bool, error) {
	var observationID string
	var readbackJSON, capacityJSON []byte
	err := s.pool.Pool.QueryRow(ctx, `SELECT c.observation_id,e.readback_body,e.capacity_body
		FROM model_pool_observations_current c JOIN model_pool_observation_events e
		 ON e.observation_id=c.observation_id
		JOIN pool_generations pg ON pg.logical_pool_id=c.logical_pool_id AND pg.pool_generation=c.pool_generation
		WHERE c.logical_pool_id=$1 AND c.pool_generation=$2 AND c.scope=$3
		 AND c.pool_observation_digest=$4 AND pg.capacity_qualified
		 AND pg.status IN ('active','draining') AND pg.model_revision_id=$5
		 AND pg.startup_envelope_digest=$6 AND pg.binding_digest=$7
		 AND pg.model_revision_digest=$8 AND pg.model_bundle_digest=$9
		 AND pg.feature_contract_digest=$10 AND pg.label_contract_digest=$11
		 AND pg.output_adapter_digest=$12 AND pg.wire_profile_digest=$13
		 AND pg.runtime_profile_digest=$14 AND pg.optimization_profile_digest=$15`,
		pool.LogicalPoolID, pool.PoolGeneration, pool.Scope, pool.PoolObservationDigest,
		pool.ModelRevisionID, pool.StartupEnvelopeDigest, pool.BindingDigest, pool.ModelRevisionDigest,
		pool.ModelBundleDigest, pool.FeatureContractDigest, pool.LabelContractDigest,
		pool.OutputAdapterDigest, pool.WireProfileDigest, pool.RuntimeProfileDigest,
		pool.OptimizationProfileDigest).Scan(&observationID, &readbackJSON, &capacityJSON)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", Readback{}, CapacityResult{}, false, nil
	}
	if err != nil {
		return "", Readback{}, CapacityResult{}, false, err
	}
	var readback Readback
	var capacity CapacityResult
	if err := json.Unmarshal(readbackJSON, &readback); err != nil {
		return "", Readback{}, CapacityResult{}, false, fmt.Errorf("model: parse reusable pool readback: %w", err)
	}
	if err := json.Unmarshal(capacityJSON, &capacity); err != nil {
		return "", Readback{}, CapacityResult{}, false, fmt.Errorf("model: parse reusable capacity readback: %w", err)
	}
	return observationID, readback, capacity, true, nil
}

// recordPoolObservation appends the exact startup/readback/capacity fact and
// advances the pool-generation current observation by a deterministic
// (observed_at, observation_id) CAS. No external call occurs in this transaction.
func (s *RolloutService) recordPoolObservation(ctx context.Context, req RolloutRequest, pool PoolGeneration, readback Readback, capacity CapacityResult) (string, error) {
	nowMS := s.now().UnixMilli()
	if readback.ObservedAtUnixMS < nowMS-int64(5*time.Minute/time.Millisecond) ||
		readback.ObservedAtUnixMS > nowMS+int64(time.Minute/time.Millisecond) {
		return "", errors.New("model: pool readback freshness outside bounded window")
	}
	readback.EligibleWorkers = append([]WorkerIdentity(nil), readback.EligibleWorkers...)
	sort.Slice(readback.EligibleWorkers, func(i, j int) bool {
		return readback.EligibleWorkers[i].WorkerID < readback.EligibleWorkers[j].WorkerID
	})
	readbackJSON, err := json.Marshal(readback)
	if err != nil {
		return "", fmt.Errorf("model: marshal pool readback: %w", err)
	}
	capacityJSON, err := json.Marshal(capacity)
	if err != nil {
		return "", fmt.Errorf("model: marshal capacity readback: %w", err)
	}
	recordDigest := digestOf("pool-observation-record|" + string(readbackJSON) + "|" + string(capacityJSON) + "|" + req.Scope + "|" + req.TraceID)
	observationID := "mpo-" + shortModelEventID(req.OperationID+":"+req.LogicalPoolID+":"+fmt.Sprint(req.TargetGeneration)+":"+readback.ReadbackAttemptID)
	workersJSON, err := json.Marshal(readback.EligibleWorkers)
	if err != nil {
		return "", fmt.Errorf("model: marshal worker identities: %w", err)
	}
	err = s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `INSERT INTO model_pool_observation_events(
			observation_id,record_digest,pool_observation_digest,logical_pool_id,pool_generation,
			model_control_incarnation_id,operation_id,scope,readback_attempt_id,observed_at_unix_ms,
			ready_replicas,loaded,capacity_qualified,min_ready_met,failure_domain,endpoint,
			tls_server_name,tls_identity_ref,eligible_workers,readback_body,capacity_body,trace_id)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
			       $19::jsonb,$20::jsonb,$21::jsonb,$22) ON CONFLICT (observation_id) DO NOTHING`,
			observationID, recordDigest, readback.PoolObservationDigest, pool.LogicalPoolID, pool.PoolGeneration,
			req.ModelControlIncarnationID, req.OperationID, req.Scope, readback.ReadbackAttemptID,
			readback.ObservedAtUnixMS, readback.ReadyReplicas, readback.Loaded, capacity.Qualified,
			capacity.MinReadyMet, capacity.FailureDomain, readback.Endpoint, readback.TLSServerName,
			readback.TLSIdentityRef, string(workersJSON), string(readbackJSON), string(capacityJSON), req.TraceID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var existingDigest string
			if err := tx.QueryRow(ctx, `SELECT record_digest FROM model_pool_observation_events WHERE observation_id=$1 FOR UPDATE`, observationID).Scan(&existingDigest); err != nil {
				return err
			}
			if existingDigest != recordDigest {
				return errors.New("model: pool observation identity conflict")
			}
		}
		if _, err := tx.Exec(ctx, `INSERT INTO model_pool_observations_current(
			logical_pool_id,pool_generation,observation_id,record_digest,pool_observation_digest,
			observed_at_unix_ms,scope,updated_at)
			VALUES($1,$2,$3,$4,$5,$6,$7,now())
			ON CONFLICT (logical_pool_id,pool_generation) DO UPDATE SET
			 observation_id=EXCLUDED.observation_id,record_digest=EXCLUDED.record_digest,
			 pool_observation_digest=EXCLUDED.pool_observation_digest,
			 observed_at_unix_ms=EXCLUDED.observed_at_unix_ms,scope=EXCLUDED.scope,updated_at=now()
			WHERE (model_pool_observations_current.observed_at_unix_ms,model_pool_observations_current.observation_id)
			    <= (EXCLUDED.observed_at_unix_ms,EXCLUDED.observation_id)`,
			pool.LogicalPoolID, pool.PoolGeneration, observationID, recordDigest,
			readback.PoolObservationDigest, readback.ObservedAtUnixMS, req.Scope); err != nil {
			return err
		}
		var currentID string
		if err := tx.QueryRow(ctx, `SELECT observation_id FROM model_pool_observations_current
			WHERE logical_pool_id=$1 AND pool_generation=$2 FOR UPDATE`, pool.LogicalPoolID, pool.PoolGeneration).Scan(&currentID); err != nil {
			return err
		}
		if currentID != observationID {
			return errors.New("model: newer pool observation already current")
		}
		tag, err = tx.Exec(ctx, `UPDATE pool_generations SET capacity_qualified=true
			WHERE logical_pool_id=$1 AND pool_generation=$2 AND status IN ('warming','active','draining')`,
			pool.LogicalPoolID, pool.PoolGeneration)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("model: observed pool generation missing")
		}
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("model: persist pool observation: %w", err)
	}
	return observationID, nil
}

func (s *RolloutService) casSwapBinding(ctx context.Context, req RolloutRequest, old *ShardBinding, casDigest, observationID, poolObservationDigest string) (*ShardBinding, error) {
	routeEpoch := old.RouteEpoch
	if ShouldRaiseRouteEpoch(old.CurrentGeneration, req.TargetGeneration, old.CurrentRevisionID, req.TargetRevisionID) {
		routeEpoch++
	}
	var out ShardBinding
	err := s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		var observedID, observedDigest string
		if err := tx.QueryRow(ctx, `SELECT observation_id,pool_observation_digest
			FROM model_pool_observations_current WHERE logical_pool_id=$1 AND pool_generation=$2 FOR SHARE`,
			req.LogicalPoolID, req.TargetGeneration).Scan(&observedID, &observedDigest); err != nil {
			return fmt.Errorf("model: load current pool observation: %w", err)
		}
		if observedID != observationID || observedDigest != poolObservationDigest {
			return errors.New("model: pool observation fence mismatch")
		}
		// Serialize same-shard swaps.
		var curRev, curIncarnation, curScope string
		var curGen, curBindingGen, curEpoch int64
		err := tx.QueryRow(ctx, `
				SELECT sb.current_revision_id, sb.current_generation, sb.current_binding_generation, sb.route_epoch,
				       sb.model_control_incarnation_id,sb.scope
				FROM shard_bindings sb JOIN model_control_state mcs ON mcs.singleton=true
				WHERE sb.shard_id = $1 AND sb.scope=$2 AND mcs.writer_enabled
				  AND mcs.active_incarnation_id=sb.model_control_incarnation_id FOR UPDATE`, req.ShardID, req.Scope).
			Scan(&curRev, &curGen, &curBindingGen, &curEpoch, &curIncarnation, &curScope)
		if err != nil {
			return err
		}
		// CAS fence: if another writer already advanced the binding, abort.
		if curBindingGen != old.CurrentBindingGeneration || curRev != old.CurrentRevisionID ||
			curGen != old.CurrentGeneration || curEpoch != old.RouteEpoch || curIncarnation != old.ModelControlIncarnationID ||
			curIncarnation != req.ModelControlIncarnationID || curScope != req.Scope {
			return fmt.Errorf("model: CAS conflict on shard %s (binding advanced)", req.ShardID)
		}
		tag, err := tx.Exec(ctx, `
			UPDATE shard_bindings SET
				current_generation = $1, previous_generation = $2,
				current_binding_generation = $3, previous_binding_generation = $4,
				current_revision_id = $5, previous_revision_id = $6,
				route_epoch = $7, resume_state = 'current', loaded = true,
				ready = true, cas_digest = $8, updated_at = now()
				WHERE shard_id = $9 AND scope=$10 AND model_control_incarnation_id=$11
				  AND current_binding_generation=$12 AND current_generation=$13
				  AND route_epoch=$14 AND current_revision_id=$15`,
			req.TargetGeneration, curGen, curBindingGen+1, curBindingGen,
			req.TargetRevisionID, curRev, curEpoch+boolToInt64(routeEpoch != old.RouteEpoch),
			casDigest, req.ShardID, req.Scope, old.ModelControlIncarnationID,
			old.CurrentBindingGeneration, old.CurrentGeneration, old.RouteEpoch, old.CurrentRevisionID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("model: shard binding CAS lost")
		}
		// Mark pool generation active.
		tag, err = tx.Exec(ctx, `
			UPDATE pool_generations SET status = 'active', capacity_qualified = true
			WHERE logical_pool_id = $1 AND pool_generation = $2`,
			req.LogicalPoolID, req.TargetGeneration)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("model: target pool generation missing")
		}
		// Drain the previous generation row.
		if curGen != req.TargetGeneration {
			if _, err := tx.Exec(ctx, `
				UPDATE pool_generations SET status = 'draining'
				WHERE logical_pool_id = $1 AND pool_generation = $2 AND status = 'active'
				  AND NOT EXISTS (SELECT 1 FROM shard_bindings sb
				    WHERE sb.logical_pool_id=$1 AND sb.current_generation=$2)`,
				req.LogicalPoolID, curGen); err != nil {
				return err
			}
		}
		if _, err := tx.Exec(ctx, `UPDATE logical_pools SET current_generation=$1
			WHERE logical_pool_id=$2 AND NOT EXISTS(SELECT 1 FROM shard_bindings
			 WHERE logical_pool_id=$2 AND current_generation<>$1)`, req.TargetGeneration, req.LogicalPoolID); err != nil {
			return err
		}
		out = ShardBinding{
			ShardID: req.ShardID, LogicalPoolID: req.LogicalPoolID,
			ModelControlIncarnationID: old.ModelControlIncarnationID,
			CurrentGeneration:         req.TargetGeneration, PreviousGeneration: curGen,
			CurrentBindingGeneration: curBindingGen + 1, PreviousBindingGeneration: curBindingGen,
			CurrentRevisionID: req.TargetRevisionID, PreviousRevisionID: curRev,
			RouteEpoch:  curEpoch + boolToInt64(routeEpoch != old.RouteEpoch),
			ResumeState: ResumeCurrent, Loaded: true, Ready: true, CASDigest: casDigest,
			Scope: req.Scope,
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("model: cas swap binding: %w", err)
	}
	return &out, nil
}

func (s *RolloutService) markResumePending(ctx context.Context, req RolloutRequest, binding ShardBinding, reason string) error {
	tag, err := s.pool.Pool.Exec(ctx, `
			UPDATE shard_bindings SET resume_state = 'resume_pending', updated_at = now()
			WHERE shard_id=$1 AND scope=$2 AND model_control_incarnation_id=$3
			  AND current_binding_generation=$4 AND current_generation=$5 AND route_epoch=$6`,
		req.ShardID, req.Scope, binding.ModelControlIncarnationID, binding.CurrentBindingGeneration,
		binding.CurrentGeneration, binding.RouteEpoch)
	if err != nil {
		return fmt.Errorf("model: mark resume_pending (%s): %w", reason, err)
	}
	if tag.RowsAffected() != 1 {
		return errors.New("model: mark resume_pending CAS conflict")
	}
	return nil
}

func (s *RolloutService) markUnavailable(ctx context.Context, req RolloutRequest, binding ShardBinding, reason string) error {
	tag, err := s.pool.Pool.Exec(ctx, `UPDATE shard_bindings SET resume_state='unavailable',loaded=false,ready=false,updated_at=now()
		WHERE shard_id=$1 AND scope=$2 AND model_control_incarnation_id=$3
		  AND current_binding_generation=$4 AND current_generation=$5 AND route_epoch=$6`,
		req.ShardID, req.Scope, binding.ModelControlIncarnationID, binding.CurrentBindingGeneration,
		binding.CurrentGeneration, binding.RouteEpoch)
	if err != nil {
		return fmt.Errorf("model: mark unavailable (%s): %w", reason, err)
	}
	if tag.RowsAffected() != 1 {
		return errors.New("model: mark unavailable shard missing")
	}
	return nil
}

func (s *RolloutService) appendOperation(ctx context.Context, op *RolloutOperation) error {
	err := s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `
					INSERT INTO model_rollout_operations (
						operation_id, logical_pool_id, target_generation, previous_generation,
						operation_kind, status, started_at_unix_ms, actor_ref, reason_code, trace_id,
						request_digest,model_control_incarnation_id,scope,shard_id,target_revision_id,edge_workload_ref)
					VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
					ON CONFLICT (operation_id) DO NOTHING`,
			op.OperationID, op.LogicalPoolID, op.TargetGeneration, op.PreviousGeneration,
			string(op.OperationKind), string(op.Status), op.StartedAtUnixMS,
			op.ActorRef, op.ReasonCode, op.TraceID, op.RequestDigest, op.ModelControlIncarnationID, op.Scope,
			op.ShardID, op.TargetRevisionID, op.EdgeWorkloadRef)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var existing string
			if err := tx.QueryRow(ctx, `SELECT request_digest FROM model_rollout_operations WHERE operation_id=$1`, op.OperationID).Scan(&existing); err != nil {
				return err
			}
			if existing != op.RequestDigest {
				return errors.New("model: operation idempotency digest conflict")
			}
			return errors.New("model: operation already exists; resume/reconcile original identity")
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("model: append operation: %w", err)
	}
	return nil
}

func (s *RolloutService) setOperationStatus(ctx context.Context, opID string, status OperationStatus, reason string) error {
	tag, err := s.pool.Pool.Exec(ctx, `
		UPDATE model_rollout_operations SET status = $1, reason_code = $2
		WHERE operation_id = $3`, string(status), reason, opID)
	if err != nil {
		return fmt.Errorf("model: set status %s: %w", status, err)
	}
	if tag.RowsAffected() != 1 {
		return fmt.Errorf("model: operation %s not found", opID)
	}
	return nil
}

func (s *RolloutService) finishOperation(ctx context.Context, opID string, status OperationStatus, reason string) error {
	tag, err := s.pool.Pool.Exec(ctx, `
		UPDATE model_rollout_operations
		SET status = $1, reason_code = $2, finished_at_unix_ms = $3
		WHERE operation_id = $4`, string(status), reason, s.now().UnixMilli(), opID)
	if err != nil {
		return fmt.Errorf("model: finish operation %s: %w", status, err)
	}
	if tag.RowsAffected() != 1 {
		return fmt.Errorf("model: operation %s not found", opID)
	}
	return nil
}

func envelopeString(req RolloutRequest, rev *ModelRevision) string {
	return fmt.Sprintf("envelope|%s|%d|%s|%s|%s|%s|%s|%s", req.LogicalPoolID, req.TargetGeneration,
		rev.ModelRevisionDigest, req.WireProfileDigest, rev.ReaderRuntimeProfile, req.RuntimeProfileDigest,
		req.OptimizationProfileDigest, req.AvailabilityProfile)
}

func digestOf(s string) string {
	h := sha256.Sum256([]byte(s))
	return "sha256:" + hex.EncodeToString(h[:])
}

func (s *RolloutService) currentEdgeWorkload(ctx context.Context, shardID string) (string, error) {
	var workload string
	if err := s.pool.Pool.QueryRow(ctx, `SELECT edge_workload_ref FROM target_assignments
		WHERE target_id=$1 AND revoked_at_unix_ms IS NULL AND expires_at_unix_ms>$2
		ORDER BY assignment_generation DESC LIMIT 1`, shardID, s.now().UnixMilli()).Scan(&workload); err != nil {
		return "", fmt.Errorf("model: resolve current Edge assignment: %w", err)
	}
	if workload == "" || len(workload) > 128 {
		return "", errors.New("model: assigned Edge workload identity malformed")
	}
	return workload, nil
}

func rolloutRequestDigest(req RolloutRequest, incarnationID string) string {
	return digestOf(fmt.Sprintf("%s|%s|%s|%d|%s|%s|%s|%s|%s|%s|%d|%s|%s",
		req.OperationID, req.ShardID, req.LogicalPoolID, req.TargetGeneration, req.TargetRevisionID,
		req.Kind, incarnationID, req.Scope, req.WireProfileDigest, req.RuntimeProfileDigest+"|"+req.OptimizationProfileDigest, req.MinReadyReplicas, req.ModelControlIncarnationID, req.EdgeWorkloadRef))
}

func boolToInt64(b bool) int64 {
	if b {
		return 1
	}
	return 0
}
