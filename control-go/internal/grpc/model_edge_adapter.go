package grpcapi

import (
	"context"
	"errors"
	"fmt"

	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
	"masi-nids/control-go/internal/model"
)

// ModelEdgeRouteAdapter maps the Model Manager's domain types to the exact
// public EdgeControl protobuf. It owns no state and performs no retry: an
// ambiguous mutation is returned to the durable rollout/reconcile state
// machine under the original operation identity.
type ModelEdgeRouteAdapter struct {
	client *EdgeControlClient
}

func NewModelEdgeRouteAdapter(client *EdgeControlClient) *ModelEdgeRouteAdapter {
	return &ModelEdgeRouteAdapter{client: client}
}

func (a *ModelEdgeRouteAdapter) PrepareRoute(ctx context.Context, prepare model.RoutePrepare) (model.RouteReadback, error) {
	if a == nil || a.client == nil {
		return model.RouteReadback{}, errors.New("grpcapi: model Edge route client unavailable")
	}
	currentEpoch, err := nonnegativeUint64("current_route_epoch", prepare.CurrentRouteEpoch)
	if err != nil {
		return model.RouteReadback{}, err
	}
	proposedEpoch, err := nonnegativeUint64("proposed_route_epoch", prepare.ProposedRouteEpoch)
	if err != nil {
		return model.RouteReadback{}, err
	}
	reply, err := a.client.PrepareRoute(ctx, &edgev1.PrepareRouteRequest{
		SchemaVersion: prepare.SchemaVersion, ShardId: prepare.ShardID,
		CurrentModelControlIncarnationId: prepare.CurrentModelControlIncarnationID,
		CurrentRouteEpoch:                currentEpoch, ProposedRouteEpoch: proposedEpoch, TraceId: prepare.TraceID,
	})
	if err != nil {
		return model.RouteReadback{}, err
	}
	return modelRouteReadback(reply)
}

func (a *ModelEdgeRouteAdapter) CommitRoute(ctx context.Context, route model.InferenceRoute, traceID string) (model.RouteReadback, error) {
	if a == nil || a.client == nil {
		return model.RouteReadback{}, errors.New("grpcapi: model Edge route client unavailable")
	}
	poolGeneration, err := nonnegativeUint64("pool_generation", route.PoolGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	bindingGeneration, err := nonnegativeUint64("binding_generation", route.BindingGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	routeEpoch, err := nonnegativeUint64("route_epoch", route.RouteEpoch)
	if err != nil {
		return model.RouteReadback{}, err
	}
	expectedBinding, err := nonnegativeUint64("expected_binding_generation", route.ExpectedBindingGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	proposedBinding, err := nonnegativeUint64("proposed_binding_generation", route.ProposedBindingGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	currentBinding, err := nonnegativeUint64("current_binding_generation", route.CurrentBindingGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	reply, err := a.client.CommitRoute(ctx, &edgev1.CommitRouteRequest{
		Route: &edgev1.InferenceRoute{
			SchemaVersion: route.SchemaVersion, ShardId: route.ShardID,
			ModelControlIncarnationId: route.ModelControlIncarnationID,
			LogicalPoolId:             route.LogicalPoolID, PoolGeneration: poolGeneration,
			BindingGeneration: bindingGeneration, RouteEpoch: routeEpoch,
			ModelRevisionDigest: route.ModelRevisionDigest, FeatureContractDigest: route.FeatureContractDigest,
			LabelContractDigest: route.LabelContractDigest, OutputAdapterDigest: route.OutputAdapterDigest,
			WireProfile: route.WireProfile, RuntimeProfile: route.RuntimeProfile, Endpoint: route.Endpoint,
			Tls:         &edgev1.TlsClientIdentity{ServerName: route.TLS.ServerName, IdentityRef: route.TLS.IdentityRef},
			OperationId: route.OperationID, Scope: route.Scope,
			ExpectedBindingGeneration: expectedBinding, ProposedBindingGeneration: proposedBinding,
			CurrentBindingGeneration: currentBinding, StartupEnvelopeDigest: route.StartupEnvelopeDigest,
			PoolObservationDigest: route.PoolObservationDigest, BindingDigest: route.BindingDigest,
			ModelBundleDigest: route.ModelBundleDigest, WireProfileDigest: route.WireProfileDigest,
			RuntimeProfileDigest: route.RuntimeProfileDigest, OptimizationProfileDigest: route.OptimizationProfileDigest,
		},
		TraceId: traceID,
	})
	if err != nil {
		return model.RouteReadback{}, err
	}
	return modelRouteReadback(reply)
}

func (a *ModelEdgeRouteAdapter) ResumeRoute(ctx context.Context, hs model.CommittedBindingHandshake, traceID string) (model.RouteReadback, error) {
	if a == nil || a.client == nil {
		return model.RouteReadback{}, errors.New("grpcapi: model Edge route client unavailable")
	}
	routeEpoch, err := nonnegativeUint64("route_epoch", hs.RouteEpoch)
	if err != nil {
		return model.RouteReadback{}, err
	}
	poolGeneration, err := nonnegativeUint64("pool_generation", hs.PoolGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	expectedBinding, err := nonnegativeUint64("expected_binding_generation", hs.ExpectedBindingGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	proposedBinding, err := nonnegativeUint64("proposed_binding_generation", hs.ProposedBindingGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	currentBinding, err := nonnegativeUint64("current_binding_generation", hs.CurrentBindingGeneration)
	if err != nil {
		return model.RouteReadback{}, err
	}
	reply, err := a.client.ResumeRoute(ctx, &edgev1.ResumeRouteRequest{
		SchemaVersion: "inference-committed-binding/v1", ShardId: hs.ShardID,
		ModelControlIncarnationId: hs.ModelControlIncarnationID, RouteEpoch: routeEpoch,
		TraceId: traceID, OperationId: hs.OperationID, Scope: hs.Scope,
		LogicalPoolId: hs.LogicalPoolID, PoolGeneration: poolGeneration,
		ExpectedBindingGeneration: expectedBinding, ProposedBindingGeneration: proposedBinding,
		CurrentBindingGeneration: currentBinding, StartupEnvelopeDigest: hs.StartupEnvelopeDigest,
		PoolObservationDigest: hs.PoolObservationDigest, BindingDigest: hs.BindingDigest,
		DeadlineUnixMs: hs.DeadlineUnixMS,
	})
	if err != nil {
		return model.RouteReadback{}, err
	}
	return modelRouteReadback(reply)
}

func modelRouteReadback(reply *edgev1.RouteReply) (model.RouteReadback, error) {
	if reply == nil {
		return model.RouteReadback{}, errors.New("grpcapi: nil Edge route reply")
	}
	if reply.RouteEpoch > uint64(^uint64(0)>>1) || reply.PendingInputs > uint64(^uint64(0)>>1) || reply.PendingResults > uint64(^uint64(0)>>1) {
		return model.RouteReadback{}, errors.New("grpcapi: Edge route reply exceeds int64 domain")
	}
	return model.RouteReadback{
		ShardID: reply.ShardId, State: reply.State, RouteEpoch: int64(reply.RouteEpoch),
		PendingInputs: int64(reply.PendingInputs), PendingResults: int64(reply.PendingResults),
		ReasonCode: reply.ReasonCode, BindingDigest: reply.BindingDigest,
	}, nil
}

func nonnegativeUint64(field string, value int64) (uint64, error) {
	if value < 0 {
		return 0, fmt.Errorf("grpcapi: %s must be nonnegative", field)
	}
	return uint64(value), nil
}
