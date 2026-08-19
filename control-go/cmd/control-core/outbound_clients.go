package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"os"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/health/grpc_health_v1"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	grpcapi "masi-nids/control-go/internal/grpc"
	adapterv1 "masi-nids/control-go/internal/grpc/adapterv1"
	"masi-nids/control-go/internal/pluginstat"
)

type outboundRuntime struct {
	edge        *grpcapi.RoutedEdgeAdapter
	deployment  *grpcapi.DeploymentAdapter
	statistics  pluginstat.Executor
	connections []*grpc.ClientConn
}

func buildOutboundRuntime(ctx context.Context, cfg *config.Config, pool *db.Pool) (*outboundRuntime, error) {
	if cfg == nil || cfg.RuntimeProfile != "production" || cfg.ExternalClients != "production-mtls" {
		return nil, errors.New("runtime: production outbound runtime requested for non-production profile")
	}
	runtime := &outboundRuntime{}
	closeOnError := func(cause error) (*outboundRuntime, error) {
		runtime.Close()
		return nil, cause
	}
	edgeClients := make(map[string]*grpcapi.EdgeControlClient, len(cfg.Outbound.Edges))
	for _, endpoint := range cfg.Outbound.Edges {
		conn, err := dialMTLSClient(ctx, endpoint.MTLSClient)
		if err != nil {
			return closeOnError(fmt.Errorf("runtime: dial Edge workload %s: %w", endpoint.WorkloadRef, err))
		}
		runtime.connections = append(runtime.connections, conn)
		edgeClients[endpoint.WorkloadRef] = grpcapi.NewEdgeControlClient(conn)
	}
	edge, err := grpcapi.NewRoutedEdgeAdapter(pool, edgeClients)
	if err != nil {
		return closeOnError(err)
	}
	runtime.edge = edge

	deploymentConn, err := dialMTLSClient(ctx, cfg.Outbound.Deployment)
	if err != nil {
		return closeOnError(fmt.Errorf("runtime: dial deployment adapter: %w", err))
	}
	runtime.connections = append(runtime.connections, deploymentConn)
	runtime.deployment = grpcapi.NewDeploymentAdapter(adapterv1.NewDeploymentAdapterClient(deploymentConn))

	statisticsConn, err := dialMTLSClient(ctx, cfg.Outbound.PluginStatistics)
	if err != nil {
		return closeOnError(fmt.Errorf("runtime: dial plugin statistics executor: %w", err))
	}
	runtime.connections = append(runtime.connections, statisticsConn)
	runtime.statistics = grpcapi.NewStatisticsExecutor(adapterv1.NewPluginStatisticsExecutorClient(statisticsConn))
	return runtime, nil
}

func dialMTLSClient(ctx context.Context, cfg config.MTLSClient) (*grpc.ClientConn, error) {
	caPEM, err := os.ReadFile(cfg.CAFile)
	if err != nil {
		return nil, fmt.Errorf("read CA: %w", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("outbound CA contains no certificates")
	}
	identity, err := tls.LoadX509KeyPair(cfg.CertFile, cfg.KeyFile)
	if err != nil {
		return nil, fmt.Errorf("load client identity: %w", err)
	}
	tlsConfig := &tls.Config{
		MinVersion: tls.VersionTLS13, RootCAs: roots, Certificates: []tls.Certificate{identity},
		ServerName: cfg.ServerName,
	}
	conn, err := grpc.DialContext(ctx, cfg.Endpoint,
		grpc.WithTransportCredentials(credentials.NewTLS(tlsConfig)), grpc.WithBlock(), grpc.WithDisableRetry(),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(cfg.MaxMessageBytes), grpc.MaxCallSendMsgSize(cfg.MaxMessageBytes)))
	if err != nil {
		return nil, err
	}
	if _, err := grpc_health_v1.NewHealthClient(conn).Check(ctx, &grpc_health_v1.HealthCheckRequest{}); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("gRPC health check: %w", err)
	}
	return conn, nil
}

func (r *outboundRuntime) Close() {
	if r == nil {
		return
	}
	for _, conn := range r.connections {
		if conn != nil {
			_ = conn.Close()
		}
	}
}
