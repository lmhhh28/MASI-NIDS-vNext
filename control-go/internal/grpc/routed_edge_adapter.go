package grpcapi

import (
	"context"
	"errors"
	"fmt"
	"time"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/model"
)

// RoutedEdgeAdapter routes by the Go-owned durable target assignment to an
// immutable map of mTLS Edge clients. It never treats connection status as a
// target assignment/current fact.
type RoutedEdgeAdapter struct {
	pool    *db.Pool
	clients map[string]*EdgeControlClient
}

func NewRoutedEdgeAdapter(pool *db.Pool, clients map[string]*EdgeControlClient) (*RoutedEdgeAdapter, error) {
	if pool == nil || len(clients) == 0 {
		return nil, errors.New("grpcapi: routed Edge adapter requires pool and clients")
	}
	copyClients := make(map[string]*EdgeControlClient, len(clients))
	for workload, client := range clients {
		if workload == "" || client == nil {
			return nil, errors.New("grpcapi: routed Edge client identity malformed")
		}
		copyClients[workload] = client
	}
	return &RoutedEdgeAdapter{pool: pool, clients: copyClients}, nil
}

func (r *RoutedEdgeAdapter) ExecuteEffect(ctx context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	client, err := r.clientForWorkload(intent.Fence.EdgeWorkloadRef)
	if err != nil {
		return governance.EdgeEffectResult{}, err
	}
	return NewGovernanceEdgeAdapter(client).ExecuteEffect(ctx, intent)
}

func (r *RoutedEdgeAdapter) QueryEffectReadback(ctx context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	client, err := r.clientForWorkload(intent.Fence.EdgeWorkloadRef)
	if err != nil {
		return governance.EdgeEffectResult{}, err
	}
	return NewGovernanceEdgeAdapter(client).QueryEffectReadback(ctx, intent)
}

func (r *RoutedEdgeAdapter) AcknowledgeEffect(ctx context.Context, intent governance.Intent, result governance.EdgeEffectResult, canonicalReference string, committedAtUnixMS int64) error {
	client, err := r.clientForWorkload(intent.Fence.EdgeWorkloadRef)
	if err != nil {
		return err
	}
	return NewGovernanceEdgeAdapter(client).AcknowledgeEffect(ctx, intent, result, canonicalReference, committedAtUnixMS)
}

func (r *RoutedEdgeAdapter) PrepareRoute(ctx context.Context, prepare model.RoutePrepare) (model.RouteReadback, error) {
	client, err := r.clientForWorkload(prepare.EdgeWorkloadRef)
	if err != nil {
		return model.RouteReadback{}, err
	}
	return NewModelEdgeRouteAdapter(client).PrepareRoute(ctx, prepare)
}

func (r *RoutedEdgeAdapter) CommitRoute(ctx context.Context, route model.InferenceRoute, traceID string) (model.RouteReadback, error) {
	client, err := r.clientForWorkload(route.EdgeWorkloadRef)
	if err != nil {
		return model.RouteReadback{}, err
	}
	return NewModelEdgeRouteAdapter(client).CommitRoute(ctx, route, traceID)
}

func (r *RoutedEdgeAdapter) ResumeRoute(ctx context.Context, hs model.CommittedBindingHandshake, traceID string) (model.RouteReadback, error) {
	client, err := r.clientForWorkload(hs.EdgeWorkloadRef)
	if err != nil {
		return model.RouteReadback{}, err
	}
	return NewModelEdgeRouteAdapter(client).ResumeRoute(ctx, hs, traceID)
}

func (r *RoutedEdgeAdapter) clientForTarget(ctx context.Context, targetID string) (*EdgeControlClient, error) {
	var workload string
	err := r.pool.Pool.QueryRow(ctx, `SELECT edge_workload_ref FROM target_assignments
		WHERE target_id=$1 AND revoked_at_unix_ms IS NULL AND expires_at_unix_ms>$2
		ORDER BY assignment_generation DESC LIMIT 1`, targetID, time.Now().UnixMilli()).Scan(&workload)
	if err != nil {
		return nil, fmt.Errorf("grpcapi: resolve target Edge assignment: %w", err)
	}
	return r.clientForWorkload(workload)
}

func (r *RoutedEdgeAdapter) clientForWorkload(workload string) (*EdgeControlClient, error) {
	client, ok := r.clients[workload]
	if !ok || client == nil {
		return nil, fmt.Errorf("grpcapi: Edge workload %q not configured", workload)
	}
	return client, nil
}
