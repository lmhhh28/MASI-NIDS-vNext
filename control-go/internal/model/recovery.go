package model

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// ReconcileOperation resumes an original resume_pending rollout identity. It
// never creates another generation or operation. Before-CAS recovery repeats
// only Edge's exact idempotent CommitRoute; after-CAS recovery repeats only the
// committed-binding ResumeRoute handshake.
func (s *RolloutService) ReconcileOperation(ctx context.Context, operationID string) (*RolloutOutcome, error) {
	var req RolloutRequest
	var kind, status string
	if err := s.pool.Pool.QueryRow(ctx, `SELECT operation_id,shard_id,logical_pool_id,target_generation,
		target_revision_id,operation_kind,model_control_incarnation_id,scope,edge_workload_ref,status,trace_id
		FROM model_rollout_operations WHERE operation_id=$1`, operationID).Scan(&req.OperationID,
		&req.ShardID, &req.LogicalPoolID, &req.TargetGeneration, &req.TargetRevisionID, &kind,
		&req.ModelControlIncarnationID, &req.Scope, &req.EdgeWorkloadRef, &status, &req.TraceID); err != nil {
		return nil, fmt.Errorf("model: load recovery operation: %w", err)
	}
	if status == string(OpApplied) {
		var generation, epoch int64
		var resume string
		if err := s.pool.Pool.QueryRow(ctx, `SELECT current_generation,route_epoch,resume_state
				FROM shard_bindings WHERE shard_id=$1 AND model_control_incarnation_id=$2`,
			req.ShardID, req.ModelControlIncarnationID).Scan(&generation, &epoch, &resume); err != nil {
			return nil, err
		}
		return &RolloutOutcome{OperationID: operationID, ShardID: req.ShardID, Status: OpApplied,
			RouteEpoch: epoch, NewGeneration: generation, ResumeState: ResumeState(resume), ReasonCode: "ALREADY_APPLIED"}, nil
	}
	if status != string(OpResumePending) {
		return nil, fmt.Errorf("model: operation %s is %s, not resume_pending", operationID, status)
	}
	req.Kind = OperationKind(kind)
	if req.EdgeWorkloadRef == "" {
		return nil, errors.New("model: recovery operation lacks frozen Edge workload")
	}
	pool, readback, observationID, err := s.loadRecoveryPool(ctx, req)
	if err != nil {
		return nil, err
	}
	var binding ShardBinding
	var resume string
	var previousGeneration, previousBindingGeneration *int64
	var previousRevision *string
	if err := s.pool.Pool.QueryRow(ctx, `SELECT shard_id,logical_pool_id,model_control_incarnation_id,
		current_generation,previous_generation,current_binding_generation,previous_binding_generation,
		current_revision_id,previous_revision_id,route_epoch,resume_state,loaded,ready,cas_digest,scope
		FROM shard_bindings WHERE shard_id=$1`, req.ShardID).Scan(&binding.ShardID, &binding.LogicalPoolID,
		&binding.ModelControlIncarnationID, &binding.CurrentGeneration, &previousGeneration,
		&binding.CurrentBindingGeneration, &previousBindingGeneration, &binding.CurrentRevisionID,
		&previousRevision, &binding.RouteEpoch, &resume, &binding.Loaded, &binding.Ready,
		&binding.CASDigest, &binding.Scope); err != nil {
		return nil, err
	}
	binding.ResumeState = ResumeState(resume)
	if previousGeneration != nil {
		binding.PreviousGeneration = *previousGeneration
	}
	if previousBindingGeneration != nil {
		binding.PreviousBindingGeneration = *previousBindingGeneration
	}
	if previousRevision != nil {
		binding.PreviousRevisionID = *previousRevision
	}
	if binding.ModelControlIncarnationID != req.ModelControlIncarnationID || binding.Scope != req.Scope ||
		binding.LogicalPoolID != req.LogicalPoolID {
		return nil, errors.New("model: recovery binding fence mismatch")
	}
	newBinding := &binding
	if binding.CurrentGeneration != req.TargetGeneration || binding.CurrentRevisionID != req.TargetRevisionID {
		proposedEpoch := binding.RouteEpoch + 1
		route := InferenceRoute{
			SchemaVersion: "inference-route/v1", ShardID: req.ShardID,
			ModelControlIncarnationID: req.ModelControlIncarnationID, LogicalPoolID: req.LogicalPoolID,
			PoolGeneration: req.TargetGeneration, BindingGeneration: binding.CurrentBindingGeneration + 1,
			RouteEpoch: proposedEpoch, ModelRevisionDigest: pool.ModelRevisionDigest,
			FeatureContractDigest: pool.FeatureContractDigest, LabelContractDigest: pool.LabelContractDigest,
			OutputAdapterDigest: pool.OutputAdapterDigest, WireProfile: pool.WireProfile,
			RuntimeProfile: pool.RuntimeProfile, Endpoint: readback.Endpoint,
			TLS:         TLSClientIdentity{ServerName: readback.TLSServerName, IdentityRef: readback.TLSIdentityRef},
			OperationID: operationID, Scope: req.Scope, ExpectedBindingGeneration: binding.CurrentBindingGeneration,
			ProposedBindingGeneration: binding.CurrentBindingGeneration + 1,
			CurrentBindingGeneration:  binding.CurrentBindingGeneration + 1,
			StartupEnvelopeDigest:     pool.StartupEnvelopeDigest, PoolObservationDigest: pool.PoolObservationDigest,
			BindingDigest: pool.BindingDigest, ModelBundleDigest: pool.ModelBundleDigest,
			WireProfileDigest: pool.WireProfileDigest, RuntimeProfileDigest: pool.RuntimeProfileDigest,
			OptimizationProfileDigest: pool.OptimizationProfileDigest, EdgeWorkloadRef: req.EdgeWorkloadRef,
		}
		committed, err := s.edge.CommitRoute(ctx, route, req.TraceID)
		if err != nil {
			return &RolloutOutcome{OperationID: operationID, ShardID: req.ShardID, Status: OpResumePending,
				RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration,
				ResumeState: ResumeUnavailable, ReasonCode: "ROUTE_COMMIT_STILL_UNCONFIRMED"}, err
		}
		if committed.State != "ready" || committed.RouteEpoch != proposedEpoch || committed.ShardID != req.ShardID ||
			committed.BindingDigest != pool.BindingDigest || committed.PendingInputs != 0 || committed.PendingResults != 0 {
			return nil, errors.New("model: recovery route commit readback mismatch")
		}
		casDigest := computeCASDigest(operationID, req.ShardID, req.TargetGeneration, req.TargetRevisionID)
		newBinding, err = s.casSwapBinding(ctx, req, &binding, casDigest, observationID, pool.PoolObservationDigest)
		if err != nil {
			return &RolloutOutcome{OperationID: operationID, ShardID: req.ShardID, Status: OpResumePending,
				RouteEpoch: binding.RouteEpoch, NewGeneration: binding.CurrentGeneration,
				ResumeState: ResumeUnavailable, ReasonCode: "CAS_STILL_UNCONFIRMED"}, err
		}
	}
	hs := CommittedBindingHandshake{
		ModelControlIncarnationID: req.ModelControlIncarnationID, OperationID: operationID,
		Scope: req.Scope, ShardID: req.ShardID, RouteEpoch: newBinding.RouteEpoch,
		LogicalPoolID: req.LogicalPoolID, PoolGeneration: req.TargetGeneration,
		ExpectedBindingGeneration: newBinding.CurrentBindingGeneration - 1,
		ProposedBindingGeneration: newBinding.CurrentBindingGeneration,
		CurrentBindingGeneration:  newBinding.CurrentBindingGeneration,
		StartupEnvelopeDigest:     pool.StartupEnvelopeDigest, PoolObservationDigest: pool.PoolObservationDigest,
		BindingDigest: pool.BindingDigest, DeadlineUnixMS: s.now().Add(30 * time.Second).UnixMilli(),
		EdgeWorkloadRef: req.EdgeWorkloadRef,
	}
	resumed, err := s.edge.ResumeRoute(ctx, hs, req.TraceID)
	if err != nil || resumed.State != "active" || resumed.ShardID != req.ShardID ||
		resumed.RouteEpoch != newBinding.RouteEpoch || resumed.BindingDigest != pool.BindingDigest {
		_ = s.markResumePending(ctx, req, *newBinding, "RECOVERY_RESUME_UNCONFIRMED")
		return &RolloutOutcome{OperationID: operationID, ShardID: req.ShardID, Status: OpResumePending,
			RouteEpoch: newBinding.RouteEpoch, NewGeneration: newBinding.CurrentGeneration,
			ResumeState: ResumePending, ReasonCode: "RECOVERY_RESUME_UNCONFIRMED"}, err
	}
	tag, err := s.pool.Pool.Exec(ctx, `UPDATE shard_bindings SET resume_state='current',loaded=true,
		ready=true,updated_at=now() WHERE shard_id=$1 AND model_control_incarnation_id=$2
		AND current_generation=$3 AND current_binding_generation=$4 AND route_epoch=$5`, req.ShardID,
		req.ModelControlIncarnationID, req.TargetGeneration, newBinding.CurrentBindingGeneration, newBinding.RouteEpoch)
	if err != nil || tag.RowsAffected() != 1 {
		return nil, errors.New("model: recovery resume projection CAS conflict")
	}
	if err := s.finishOperation(ctx, operationID, OpApplied, "RECOVERED_APPLIED"); err != nil {
		return nil, err
	}
	return &RolloutOutcome{OperationID: operationID, ShardID: req.ShardID, Status: OpApplied,
		RouteEpoch: newBinding.RouteEpoch, NewGeneration: req.TargetGeneration,
		ResumeState: ResumeCurrent, ReasonCode: "RECOVERED_APPLIED"}, nil
}

func (s *RolloutService) loadRecoveryPool(ctx context.Context, req RolloutRequest) (*PoolGeneration, Readback, string, error) {
	var pool PoolGeneration
	var status string
	if err := s.pool.Pool.QueryRow(ctx, `SELECT pg.logical_pool_id,pg.pool_generation,pg.model_revision_id,
			pg.model_control_incarnation_id,pg.startup_envelope_digest,pg.pool_observation_digest,pg.binding_digest,pg.status,
		pg.min_ready_replicas,pg.capacity_qualified,pg.model_revision_digest,pg.model_bundle_digest,
		pg.feature_contract_digest,pg.label_contract_digest,pg.output_adapter_digest,
		pg.wire_profile_digest,pg.runtime_profile_digest,pg.optimization_profile_digest,
		lp.availability_profile,lp.runtime_profile
		FROM pool_generations pg JOIN logical_pools lp ON lp.logical_pool_id=pg.logical_pool_id
			WHERE pg.logical_pool_id=$1 AND pg.model_control_incarnation_id=$2
			 AND pg.pool_generation=$3 AND pg.model_revision_id=$4`,
		req.LogicalPoolID, req.ModelControlIncarnationID, req.TargetGeneration, req.TargetRevisionID).
		Scan(&pool.LogicalPoolID, &pool.PoolGeneration, &pool.ModelRevisionID,
			&pool.ModelControlIncarnationID, &pool.StartupEnvelopeDigest,
			&pool.PoolObservationDigest, &pool.BindingDigest, &status, &pool.MinReadyReplicas,
			&pool.CapacityQualified, &pool.ModelRevisionDigest, &pool.ModelBundleDigest,
			&pool.FeatureContractDigest, &pool.LabelContractDigest, &pool.OutputAdapterDigest,
			&pool.WireProfileDigest, &pool.RuntimeProfileDigest, &pool.OptimizationProfileDigest,
			&pool.AvailabilityProfile, &pool.RuntimeProfile); err != nil {
		return nil, Readback{}, "", err
	}
	pool.Status = PoolGenerationStatus(status)
	pool.WireProfile = "inference-central-grpc-batch/v1"
	pool.OperationID, pool.Scope = req.OperationID, req.Scope
	if !pool.CapacityQualified || (pool.Status != PoolWarming && pool.Status != PoolActive && pool.Status != PoolDraining) {
		return nil, Readback{}, "", errors.New("model: recovery pool is not qualified/available")
	}
	var observationID string
	var readbackJSON []byte
	if err := s.pool.Pool.QueryRow(ctx, `SELECT c.observation_id,e.readback_body
		FROM model_pool_observations_current c JOIN model_pool_observation_events e
			ON e.observation_id=c.observation_id WHERE c.logical_pool_id=$1
			AND c.model_control_incarnation_id=$2 AND c.pool_generation=$3
			AND c.pool_observation_digest=$4 AND c.scope=$5`, req.LogicalPoolID,
		req.ModelControlIncarnationID, req.TargetGeneration,
		pool.PoolObservationDigest, req.Scope).Scan(&observationID, &readbackJSON); err != nil {
		return nil, Readback{}, "", err
	}
	var readback Readback
	if err := json.Unmarshal(readbackJSON, &readback); err != nil {
		return nil, Readback{}, "", err
	}
	if err := validatePoolReadback(readback, pool.MinReadyReplicas); err != nil {
		return nil, Readback{}, "", err
	}
	return &pool, readback, observationID, nil
}
