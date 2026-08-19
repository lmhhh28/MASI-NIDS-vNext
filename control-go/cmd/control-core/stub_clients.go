// stub_clients.go defines fail-closed stand-in clients for the cross-module
// boundaries (Go → Edge, Go → deployment adapter) that MOD-CTRL-001 depends on
// but cannot reach until the pairwise-integration stage. They let the subdomain
// services that require an Edge/deployment client be constructed and held in
// the live process; any actual effect/rollout/activation operation fails
// closed (returns errEdgeNotConfigured) rather than crashing or silently
// succeeding. Real mTLS clients are wired in the pairwise stage (ADR-0004
// boundaries 5 and 12). These stubs are NOT a second writer/scheduler and never
// perform a real dataplane effect.
package main

import (
	"context"
	"errors"

	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/model"
)

// errEdgeNotConfigured is the single fail-closed reason returned by every stub
// method. It makes every cross-module operation fail closed until pairwise
// integration wires the real Edge/deployment clients.
var errEdgeNotConfigured = errors.New("edge/deployment client not configured: real wiring is pairwise integration")

// stubDeploymentAdapter is a fail-closed model.DeploymentAdapterClient.
type stubDeploymentAdapter struct{}

func (stubDeploymentAdapter) StagePool(ctx context.Context, pool model.PoolGeneration) error {
	_ = ctx
	_ = pool
	return errEdgeNotConfigured
}
func (stubDeploymentAdapter) StartReplicas(ctx context.Context, pool model.PoolGeneration) (model.Readback, error) {
	_ = ctx
	_ = pool
	return model.Readback{}, errEdgeNotConfigured
}
func (stubDeploymentAdapter) WarmupAndCapacity(ctx context.Context, pool model.PoolGeneration) (model.CapacityResult, error) {
	_ = ctx
	_ = pool
	return model.CapacityResult{}, errEdgeNotConfigured
}

// stubEdgeRoute is a fail-closed model.EdgeRouteClient.
type stubEdgeRoute struct{}

func (stubEdgeRoute) PrepareRoute(ctx context.Context, prepare model.RoutePrepare) (model.RouteReadback, error) {
	_ = ctx
	_ = prepare
	return model.RouteReadback{}, errEdgeNotConfigured
}
func (stubEdgeRoute) CommitRoute(ctx context.Context, route model.InferenceRoute, traceID string) (model.RouteReadback, error) {
	_ = ctx
	_ = route
	_ = traceID
	return model.RouteReadback{}, errEdgeNotConfigured
}
func (stubEdgeRoute) ResumeRoute(ctx context.Context, hs model.CommittedBindingHandshake, traceID string) (model.RouteReadback, error) {
	_ = ctx
	_ = hs
	_ = traceID
	return model.RouteReadback{}, errEdgeNotConfigured
}

// stubEdgeEffect is a fail-closed governance.EdgeEffectClient.
type stubEdgeEffect struct{}

func (stubEdgeEffect) ExecuteEffect(ctx context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	_ = ctx
	_ = intent
	return governance.EdgeEffectResult{}, errEdgeNotConfigured
}

func (stubEdgeEffect) AcknowledgeEffect(ctx context.Context, intent governance.Intent, result governance.EdgeEffectResult, canonicalReference string, committedAtUnixMS int64) error {
	_ = ctx
	_ = intent
	_ = result
	_ = canonicalReference
	_ = committedAtUnixMS
	return errEdgeNotConfigured
}

func (stubEdgeEffect) QueryEffectReadback(ctx context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	_ = ctx
	_ = intent
	return governance.EdgeEffectResult{}, errEdgeNotConfigured
}
