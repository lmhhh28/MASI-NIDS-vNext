package grpcapi

import (
	"context"
	"fmt"

	"google.golang.org/grpc"

	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
)

// EdgeControlClient is Go's bounded wrapper over the EdgeControl surface.
// Calls carry the caller's deadline (no unbounded waits); Go is the only
// production P4Runtime write path owner via Edge (AGENTS.md).
type EdgeControlClient struct {
	raw edgev1.EdgeControlClient
}

func NewEdgeControlClient(cc grpc.ClientConnInterface) *EdgeControlClient {
	return &EdgeControlClient{raw: edgev1.NewEdgeControlClient(cc)}
}

// AssignTarget assigns one target actor on Edge with the frozen assignment
// envelope (lease/election floor-ceiling/pipeline identity).
func (c *EdgeControlClient) AssignTarget(ctx context.Context, assignment *edgev1.TargetAssignment) (*edgev1.TargetReply, error) {
	reply, err := c.raw.AssignTarget(ctx, &edgev1.AssignTargetRequest{Assignment: assignment})
	if err != nil {
		return nil, fmt.Errorf("grpcapi: assign target: %w", err)
	}
	return reply, nil
}

// RevokeTarget revokes the assignment (old actor becomes read-only).
func (c *EdgeControlClient) RevokeTarget(ctx context.Context, targetID, leaseID string, fence *edgev1.Fence) (*edgev1.TargetReply, error) {
	reply, err := c.raw.RevokeTarget(ctx, &edgev1.RevokeTargetRequest{
		SchemaVersion: "masi-target-assignment/v1",
		TargetId:      targetID, LeaseId: leaseID, Fence: fence,
	})
	if err != nil {
		return nil, fmt.Errorf("grpcapi: revoke target: %w", err)
	}
	return reply, nil
}

// ExecuteEffect performs the durable effect intent on Edge (journal/write/
// readback). It is called OUTSIDE any DB transaction by the dispatcher.
func (c *EdgeControlClient) PreflightEffect(ctx context.Context, req *edgev1.PreflightEffectRequest) (*edgev1.PreflightEffectReply, error) {
	reply, err := c.raw.PreflightEffect(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("grpcapi: preflight effect: %w", err)
	}
	return reply, nil
}

func (c *EdgeControlClient) ExecuteEffect(ctx context.Context, req *edgev1.ExecuteEffectRequest) (*edgev1.EffectResult, error) {
	res, err := c.raw.ExecuteEffect(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("grpcapi: execute effect: %w", err)
	}
	return res, nil
}

// AcknowledgeEffect confirms the canonical ACK reached Edge (after the PG
// Event commit, never instead of it).
func (c *EdgeControlClient) AcknowledgeEffect(ctx context.Context, req *edgev1.AcknowledgeEffectRequest) (*edgev1.PublishAck, error) {
	ack, err := c.raw.AcknowledgeEffect(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("grpcapi: acknowledge effect: %w", err)
	}
	return ack, nil
}

// PrepareRoute / CommitRoute / ResumeRoute implement the model route change
// handshake on Edge (per-shard canonical router).
func (c *EdgeControlClient) PrepareRoute(ctx context.Context, req *edgev1.PrepareRouteRequest) (*edgev1.RouteReply, error) {
	reply, err := c.raw.PrepareRoute(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("grpcapi: prepare route: %w", err)
	}
	return reply, nil
}

func (c *EdgeControlClient) CommitRoute(ctx context.Context, req *edgev1.CommitRouteRequest) (*edgev1.RouteReply, error) {
	reply, err := c.raw.CommitRoute(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("grpcapi: commit route: %w", err)
	}
	return reply, nil
}

func (c *EdgeControlClient) ResumeRoute(ctx context.Context, req *edgev1.ResumeRouteRequest) (*edgev1.RouteReply, error) {
	reply, err := c.raw.ResumeRoute(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("grpcapi: resume route: %w", err)
	}
	return reply, nil
}

// ConfigureRuleObservations installs the observable rule set for one target.
func (c *EdgeControlClient) ConfigureRuleObservations(ctx context.Context, req *edgev1.ConfigureRuleObservationsRequest) (*edgev1.PublishAck, error) {
	ack, err := c.raw.ConfigureRuleObservations(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("grpcapi: configure rule observations: %w", err)
	}
	return ack, nil
}

// GetStatus reads the Edge status (diagnostic; not a canonical fact source).
func (c *EdgeControlClient) GetStatus(ctx context.Context, targetID string) (*edgev1.EdgeStatus, error) {
	st, err := c.raw.GetStatus(ctx, &edgev1.GetStatusRequest{TargetId: targetID})
	if err != nil {
		return nil, fmt.Errorf("grpcapi: get status: %w", err)
	}
	return st, nil
}
