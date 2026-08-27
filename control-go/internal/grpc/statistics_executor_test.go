package grpcapi

import (
	"context"
	"testing"

	"google.golang.org/grpc"

	adapterv1 "masi-nids/control-go/internal/grpc/adapterv1"
	"masi-nids/control-go/internal/pluginstat"
)

type nilStatisticsReplyClient struct{}

func (nilStatisticsReplyClient) ExecuteStatistics(
	context.Context,
	*adapterv1.StatisticsExecutionRequest,
	...grpc.CallOption,
) (*adapterv1.StatisticsExecutionReply, error) {
	return nil, nil
}

func TestStatisticsExecutorRejectsNilReply(t *testing.T) {
	executor := NewStatisticsExecutor(nilStatisticsReplyClient{})
	_, err := executor.ExecuteStatistics(
		context.Background(),
		pluginstat.ClaimToken{RunID: "run-1"},
		pluginstat.InputBundle{FrozenInputDigest: "sha256:frozen"},
	)
	if err == nil {
		t.Fatal("nil gRPC reply must fail closed")
	}
}
