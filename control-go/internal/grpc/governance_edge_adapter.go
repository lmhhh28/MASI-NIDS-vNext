package grpcapi

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"

	"google.golang.org/protobuf/proto"

	"masi-nids/control-go/internal/governance"
	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
)

// GovernanceEdgeAdapter is the real public EdgeControl adapter for the sole
// effect dispatcher. It performs read-only preflight immediately before the
// mutation and never retries an ambiguous ExecuteEffect call.
type GovernanceEdgeAdapter struct {
	client *EdgeControlClient
}

func NewGovernanceEdgeAdapter(client *EdgeControlClient) *GovernanceEdgeAdapter {
	return &GovernanceEdgeAdapter{client: client}
}

func (a *GovernanceEdgeAdapter) ExecuteEffect(ctx context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	if a == nil || a.client == nil {
		return governance.EdgeEffectResult{}, errors.New("grpcapi: governance Edge client unavailable")
	}
	wire, err := governance.ToEdgeEffectIntent(intent)
	if err != nil {
		return governance.EdgeEffectResult{}, err
	}
	if wire.EffectDigest != intent.EffectDigest {
		return governance.EdgeEffectResult{}, errors.New("grpcapi: effect digest differs from canonical protobuf")
	}
	preflight, err := a.client.PreflightEffect(ctx, &edgev1.PreflightEffectRequest{Intent: wire})
	if err != nil {
		return governance.EdgeEffectResult{}, err
	}
	if preflight.TargetId != intent.TargetID || preflight.EffectIntentId != intent.EffectIntentID ||
		preflight.PreflightToken == "" || preflight.PlanDigest == "" || preflight.PhysicalEntries > 4096 ||
		preflight.Admission != edgev1.AdmissionResult_ADMISSION_RESULT_ACCEPTED || preflight.Result != "accepted" {
		return governance.EdgeEffectResult{}, errors.New("grpcapi: Edge preflight identity/admission mismatch")
	}
	reply, err := a.client.ExecuteEffect(ctx, &edgev1.ExecuteEffectRequest{Intent: wire, PreflightToken: preflight.PreflightToken})
	if err != nil {
		return governance.EdgeEffectResult{}, err
	}
	return mapEffectResult(intent, reply)
}

// QueryEffectReadback uses Edge's exact-operation idempotency branch. The
// empty preflight token can never start a new mutation: it only succeeds when
// Edge still has the same pending journal result.
func (a *GovernanceEdgeAdapter) QueryEffectReadback(ctx context.Context, intent governance.Intent) (governance.EdgeEffectResult, error) {
	if a == nil || a.client == nil {
		return governance.EdgeEffectResult{}, errors.New("grpcapi: governance Edge client unavailable")
	}
	wire, err := governance.ToEdgeEffectIntent(intent)
	if err != nil {
		return governance.EdgeEffectResult{}, err
	}
	reply, err := a.client.ExecuteEffect(ctx, &edgev1.ExecuteEffectRequest{Intent: wire})
	if err != nil {
		return governance.EdgeEffectResult{}, err
	}
	return mapEffectResult(intent, reply)
}

func (a *GovernanceEdgeAdapter) AcknowledgeEffect(ctx context.Context, intent governance.Intent, result governance.EdgeEffectResult, canonicalReference string, committedAtUnixMS int64) error {
	if a == nil || a.client == nil || result.ResultDigest == "" || canonicalReference == "" || committedAtUnixMS < 1 {
		return errors.New("grpcapi: effect acknowledgement identity malformed")
	}
	wire, err := governance.ToEdgeEffectIntent(intent)
	if err != nil {
		return err
	}
	ack, err := a.client.AcknowledgeEffect(ctx, &edgev1.AcknowledgeEffectRequest{
		SchemaVersion: "effect-canonical-ack/v1", TargetId: intent.TargetID,
		EffectIntentId: intent.EffectIntentID, OperationId: intent.OperationID, Fence: wire.Fence,
		ResultDigest: result.ResultDigest, CanonicalEffectReference: canonicalReference,
		CommittedAtUnixMs: committedAtUnixMS, TraceId: intent.TraceID,
	})
	if err != nil {
		return err
	}
	if ack.StatusCode != edgev1.PublishStatus_PUBLISH_STATUS_CHECKPOINTED || ack.Identity != intent.OperationID ||
		ack.Digest != result.ResultDigest {
		return errors.New("grpcapi: Edge effect acknowledgement mismatch")
	}
	return nil
}

func mapEffectResult(intent governance.Intent, reply *edgev1.EffectResult) (governance.EdgeEffectResult, error) {
	if reply == nil || reply.TargetId != intent.TargetID || reply.EffectIntentId != intent.EffectIntentID ||
		reply.OperationId != intent.OperationID || !sameEdgeFence(reply.Fence, intent.Fence) {
		return governance.EdgeEffectResult{}, errors.New("grpcapi: Edge effect result identity mismatch")
	}
	outcome := "unknown"
	switch reply.Status {
	case edgev1.EffectStatus_EFFECT_STATUS_APPLIED:
		outcome = "applied"
	case edgev1.EffectStatus_EFFECT_STATUS_HOLD:
		outcome = "hold"
	case edgev1.EffectStatus_EFFECT_STATUS_RECONCILING:
		outcome = "reconciling"
	case edgev1.EffectStatus_EFFECT_STATUS_UNKNOWN:
		outcome = "unknown"
	default:
		return governance.EdgeEffectResult{}, fmt.Errorf("grpcapi: unknown Edge effect status %d", reply.Status)
	}
	if reply.ExpectedEntries > 4096 || reply.ObservedEntries > 4096 || reply.MismatchedEntries > 4096 || reply.ActiveBank > 1 {
		return governance.EdgeEffectResult{}, errors.New("grpcapi: Edge effect result exceeds bounds")
	}
	return governance.EdgeEffectResult{
		OperationID: reply.OperationId, TargetID: reply.TargetId, EffectDigest: intent.EffectDigest,
		Outcome: outcome, ReadbackDigest: reply.ReadbackDigest, ResultDigest: protoDigest(reply),
		ExpectedEntries: int(reply.ExpectedEntries), ObservedEntries: int(reply.ObservedEntries),
		MismatchedEntries: int(reply.MismatchedEntries), ActiveBank: int(reply.ActiveBank),
	}, nil
}

func sameEdgeFence(wire *edgev1.Fence, domain governance.Fence) bool {
	return wire != nil && wire.TargetControlIncarnationId == domain.TargetControlIncarnationID &&
		wire.TargetAssignmentGeneration == uint64(domain.TargetAssignmentGeneration) &&
		wire.ActorRuntimeEpoch == domain.ActorRuntimeEpoch && wire.ApplicationGeneration == uint64(domain.ApplicationGeneration) &&
		wire.ElectionIdHigh == domain.ElectionIDHigh && wire.ElectionIdLow == domain.ElectionIDLow
}

func protoDigest(message proto.Message) string {
	raw, _ := proto.MarshalOptions{Deterministic: true}.Marshal(message)
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}
