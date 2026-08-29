// fail_closed_clients.go defines the deliberate non-production policy for
// device/model outbound boundaries. Test and acceptance profiles must never
// execute a real effect or rollout accidentally; production instead constructs
// and validates all required mTLS clients before readiness.
package main

import (
	"context"
	"errors"

	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/model"
)

var errOutboundDisabled = errors.New("outbound effect/deployment boundary disabled by non-production profile")

type disabledDeploymentAdapter struct{}

func (disabledDeploymentAdapter) StagePool(ctx context.Context, pool model.PoolGeneration) error {
	_ = ctx
	_ = pool
	return errOutboundDisabled
}

func (disabledDeploymentAdapter) StartReplicas(ctx context.Context, pool model.PoolGeneration) (model.Readback, error) {
	_ = ctx
	_ = pool
	return model.Readback{}, errOutboundDisabled
}

func (disabledDeploymentAdapter) WarmupAndCapacity(ctx context.Context, pool model.PoolGeneration) (model.CapacityResult, error) {
	_ = ctx
	_ = pool
	return model.CapacityResult{}, errOutboundDisabled
}

type disabledEdgeRoute struct{}

func (disabledEdgeRoute) PrepareRoute(ctx context.Context, prepare model.RoutePrepare) (model.RouteReadback, error) {
	_ = ctx
	_ = prepare
	return model.RouteReadback{}, errOutboundDisabled
}

func (disabledEdgeRoute) CommitRoute(ctx context.Context, route model.InferenceRoute, traceID string) (model.RouteReadback, error) {
	_ = ctx
	_ = route
	_ = traceID
	return model.RouteReadback{}, errOutboundDisabled
}

func (disabledEdgeRoute) ResumeRoute(ctx context.Context, hs model.CommittedBindingHandshake, traceID string) (model.RouteReadback, error) {
	_ = ctx
	_ = hs
	_ = traceID
	return model.RouteReadback{}, errOutboundDisabled
}

type disabledEdgeEffect struct{}

func (disabledEdgeEffect) PreflightEffect(ctx context.Context, intent governance.Intent) (governance.EdgePreflightResult, error) {
	_ = ctx
	_ = intent
	return governance.EdgePreflightResult{}, &governance.PreflightRejectedError{Cause: errOutboundDisabled}
}

func (disabledEdgeEffect) ExecuteEffect(ctx context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	_ = ctx
	_ = intent
	return governance.EdgeEffectResult{}, errOutboundDisabled
}

func (disabledEdgeEffect) AcknowledgeEffect(ctx context.Context, intent governance.Intent, result governance.EdgeEffectResult, canonicalReference string, committedAtUnixMS int64) error {
	_ = ctx
	_ = intent
	_ = result
	_ = canonicalReference
	_ = committedAtUnixMS
	return errOutboundDisabled
}

func (disabledEdgeEffect) QueryEffectReadback(ctx context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	_ = ctx
	_ = intent
	return governance.EdgeEffectResult{}, errOutboundDisabled
}
