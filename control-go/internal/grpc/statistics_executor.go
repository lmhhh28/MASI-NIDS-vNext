package grpcapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	adapterv1 "masi-nids/control-go/internal/grpc/adapterv1"
	"masi-nids/control-go/internal/pluginstat"
)

// StatisticsExecutor is the real Host/direct typed adapter. It transports only
// the versioned plugin-statistics JSON records inside a bounded protobuf
// envelope and owns no queue, schedule, or current projection.
type StatisticsExecutor struct {
	client adapterv1.PluginStatisticsExecutorClient
}

func NewStatisticsExecutor(client adapterv1.PluginStatisticsExecutorClient) *StatisticsExecutor {
	return &StatisticsExecutor{client: client}
}

func (e *StatisticsExecutor) ExecuteStatistics(ctx context.Context, token pluginstat.ClaimToken, bundle pluginstat.InputBundle) (pluginstat.Artifact, error) {
	if e == nil || e.client == nil {
		return pluginstat.Artifact{}, errors.New("grpcapi: statistics executor unavailable")
	}
	if bundle.FrozenInputDigest == "" {
		return pluginstat.Artifact{}, errors.New("grpcapi: statistics input is not frozen")
	}
	raw, err := json.Marshal(bundle)
	if err != nil || len(raw) > 2*1024*1024 {
		return pluginstat.Artifact{}, errors.New("grpcapi: statistics input serialization/size invalid")
	}
	reply, err := e.client.ExecuteStatistics(ctx, &adapterv1.StatisticsExecutionRequest{
		SchemaVersion: "control-plugin-statistics-execution/v1", RunId: token.RunID,
		LeaseId: token.LeaseID, ClaimGeneration: uint64(token.ClaimGeneration),
		ResultFence: token.ResultFence, BindingGeneration: uint64(token.BindingGeneration),
		DefinitionId: token.DefinitionID, DefinitionDigest: token.DefinitionDigest, Scope: token.Scope,
		ExpiresAtUnixMs: token.ExpiresAtUnixMS, DeadlineMs: uint32(token.DeadlineMS),
		InputBundleJson: raw, FrozenInputDigest: bundle.FrozenInputDigest, TraceId: "statistics:" + token.RunID,
	})
	if err != nil {
		return pluginstat.Artifact{}, fmt.Errorf("grpcapi: execute statistics: %w", err)
	}
	if reply == nil || reply.SchemaVersion != "control-plugin-statistics-result/v1" || reply.RunId != token.RunID ||
		reply.ResultFence != token.ResultFence || reply.Status != "succeeded" ||
		len(reply.ArtifactJson) == 0 || len(reply.ArtifactJson) > pluginstat.MaxArtifactBytes {
		return pluginstat.Artifact{}, errors.New("grpcapi: statistics result identity/status/size mismatch")
	}
	var artifact pluginstat.Artifact
	decoder := json.NewDecoder(bytes.NewReader(reply.ArtifactJson))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&artifact); err != nil {
		return pluginstat.Artifact{}, fmt.Errorf("grpcapi: decode statistics artifact: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return pluginstat.Artifact{}, errors.New("grpcapi: trailing statistics artifact content")
	}
	if artifact.Bytes != len(reply.ArtifactJson) || artifact.ArtifactDigest != reply.ArtifactDigest ||
		pluginstat.ComputeArtifactDigest(artifact) != artifact.ArtifactDigest {
		return pluginstat.Artifact{}, errors.New("grpcapi: statistics artifact digest mismatch")
	}
	return artifact, nil
}
