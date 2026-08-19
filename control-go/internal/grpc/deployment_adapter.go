package grpcapi

import (
	"context"
	"errors"
	"fmt"

	adapterv1 "masi-nids/control-go/internal/grpc/adapterv1"
	"masi-nids/control-go/internal/model"
)

// DeploymentAdapter maps the Model Manager's immutable pool envelope to the
// frozen deployment adapter gRPC boundary. Deployment observations never become
// current by themselves; RolloutService validates and persists them before CAS.
type DeploymentAdapter struct {
	client adapterv1.DeploymentAdapterClient
}

func NewDeploymentAdapter(client adapterv1.DeploymentAdapterClient) *DeploymentAdapter {
	return &DeploymentAdapter{client: client}
}

func (a *DeploymentAdapter) StagePool(ctx context.Context, pool model.PoolGeneration) error {
	if a == nil || a.client == nil {
		return errors.New("grpcapi: deployment adapter unavailable")
	}
	reply, err := a.client.StagePool(ctx, deploymentPoolRequest(pool))
	if err != nil {
		return fmt.Errorf("grpcapi: deployment stage pool: %w", err)
	}
	if reply.SchemaVersion != "control-deployment-action/v1" || reply.LogicalPoolId != pool.LogicalPoolID ||
		reply.PoolGeneration != uint64(pool.PoolGeneration) || reply.OperationId != pool.OperationID ||
		reply.Action != "pre-stage" || reply.Status != "accepted" || reply.ActionDigest == "" {
		return errors.New("grpcapi: deployment stage reply identity/status mismatch")
	}
	return nil
}

func (a *DeploymentAdapter) StartReplicas(ctx context.Context, pool model.PoolGeneration) (model.Readback, error) {
	if a == nil || a.client == nil {
		return model.Readback{}, errors.New("grpcapi: deployment adapter unavailable")
	}
	reply, err := a.client.StartReplicas(ctx, deploymentPoolRequest(pool))
	if err != nil {
		return model.Readback{}, fmt.Errorf("grpcapi: deployment start replicas: %w", err)
	}
	if reply.SchemaVersion != "control-pool-readback/v1" {
		return model.Readback{}, errors.New("grpcapi: deployment readback schema mismatch")
	}
	workers := make([]model.WorkerIdentity, 0, len(reply.EligibleWorkers))
	for _, worker := range reply.EligibleWorkers {
		if worker == nil {
			return model.Readback{}, errors.New("grpcapi: nil deployment worker identity")
		}
		workers = append(workers, model.WorkerIdentity{WorkerID: worker.WorkerId, WorkerDigest: worker.WorkerDigest})
	}
	return model.Readback{
		LogicalPoolID: reply.LogicalPoolId, PoolGeneration: int64(reply.PoolGeneration),
		BindingGeneration: int64(reply.BindingGeneration), ModelControlIncarnationID: reply.ModelControlIncarnationId,
		OperationID: reply.OperationId, ModelRevisionDigest: reply.ModelRevisionDigest,
		ModelBundleDigest: reply.ModelBundleDigest, FeatureContractDigest: reply.FeatureContractDigest,
		LabelContractDigest: reply.LabelContractDigest, OutputAdapterDigest: reply.OutputAdapterDigest,
		WireProfileDigest: reply.WireProfileDigest, RuntimeProfileDigest: reply.RuntimeProfileDigest,
		OptimizationProfileDigest: reply.OptimizationProfileDigest, WireProfile: reply.WireProfile,
		RuntimeProfile: reply.RuntimeProfile, AvailabilityProfile: reply.AvailabilityProfile,
		ReadyReplicas: int(reply.ReadyReplicas), ReadbackAttemptID: reply.ReadbackAttemptId,
		ObservedAtUnixMS: reply.ObservedAtUnixMs, StartupEnvelopeDigest: reply.StartupEnvelopeDigest,
		PoolObservationDigest: reply.PoolObservationDigest, BindingDigest: reply.BindingDigest,
		Endpoint: reply.Endpoint, TLSServerName: reply.TlsServerName, TLSIdentityRef: reply.TlsIdentityRef,
		EligibleWorkers: workers, Loaded: reply.Loaded,
	}, nil
}

func (a *DeploymentAdapter) WarmupAndCapacity(ctx context.Context, pool model.PoolGeneration) (model.CapacityResult, error) {
	if a == nil || a.client == nil {
		return model.CapacityResult{}, errors.New("grpcapi: deployment adapter unavailable")
	}
	reply, err := a.client.WarmupAndCapacity(ctx, deploymentPoolRequest(pool))
	if err != nil {
		return model.CapacityResult{}, fmt.Errorf("grpcapi: deployment capacity: %w", err)
	}
	if reply.SchemaVersion != "control-capacity-readback/v1" {
		return model.CapacityResult{}, errors.New("grpcapi: capacity readback schema mismatch")
	}
	return model.CapacityResult{LogicalPoolID: reply.LogicalPoolId, PoolGeneration: int64(reply.PoolGeneration),
		Qualified: reply.Qualified, MinReadyMet: reply.MinReadyMet, FailureDomain: reply.FailureDomain,
		ReasonCode: reply.ReasonCode}, nil
}

func deploymentPoolRequest(pool model.PoolGeneration) *adapterv1.PoolGenerationRequest {
	return &adapterv1.PoolGenerationRequest{
		SchemaVersion: "control-pool-generation/v1", LogicalPoolId: pool.LogicalPoolID,
		PoolGeneration: uint64(pool.PoolGeneration), ModelControlIncarnationId: pool.ModelControlIncarnationID,
		OperationId: pool.OperationID, Scope: pool.Scope, BindingGeneration: uint64(pool.BindingGeneration),
		ModelRevisionId: pool.ModelRevisionID, StartupEnvelopeDigest: pool.StartupEnvelopeDigest,
		PoolObservationDigest: pool.PoolObservationDigest, BindingDigest: pool.BindingDigest,
		MinReadyReplicas: uint32(pool.MinReadyReplicas), ModelRevisionDigest: pool.ModelRevisionDigest,
		ModelBundleDigest: pool.ModelBundleDigest, FeatureContractDigest: pool.FeatureContractDigest,
		LabelContractDigest: pool.LabelContractDigest, OutputAdapterDigest: pool.OutputAdapterDigest,
		WireProfileDigest: pool.WireProfileDigest, RuntimeProfileDigest: pool.RuntimeProfileDigest,
		OptimizationProfileDigest: pool.OptimizationProfileDigest, WireProfile: pool.WireProfile,
		RuntimeProfile: pool.RuntimeProfile, AvailabilityProfile: pool.AvailabilityProfile,
	}
}
