package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	"masi-nids/control-go/internal/config"
	grpcapi "masi-nids/control-go/internal/grpc"
)

func grpcServerOptions(cfg *config.Config) ([]grpc.ServerOption, error) {
	if cfg.RuntimeProfile == "test" {
		return nil, nil
	}
	cert, err := tls.LoadX509KeyPair(cfg.TLS.GRPCCertFile, cfg.TLS.GRPCKeyFile)
	if err != nil {
		return nil, fmt.Errorf("runtime: load gRPC server identity: %w", err)
	}
	caPEM, err := os.ReadFile(cfg.TLS.GRPCClientCAFile)
	if err != nil {
		return nil, fmt.Errorf("runtime: read gRPC client CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("runtime: gRPC client CA contains no certificate")
	}
	tlsCfg := &tls.Config{
		MinVersion:   tls.VersionTLS13,
		Certificates: []tls.Certificate{cert},
		ClientCAs:    pool,
		ClientAuth:   tls.RequireAndVerifyClientCert,
	}
	return []grpc.ServerOption{grpc.Creds(credentials.NewTLS(tlsCfg)),
		grpc.ChainUnaryInterceptor(grpcapi.UnaryPeerIdentityInterceptor(cfg.TLS.GRPCAllowedClientSANs))}, nil
}

func httpTLSConfig(cfg *config.Config) (*tls.Config, error) {
	if cfg.RuntimeProfile == "test" {
		return nil, nil
	}
	caPEM, err := os.ReadFile(cfg.TLS.HTTPClientCAFile)
	if err != nil {
		return nil, fmt.Errorf("runtime: read HTTP client CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("runtime: HTTP client CA contains no certificate")
	}
	// Browser/API clients are allowed without a client certificate. The /mcp
	// handler independently requires a verified workload certificate and exact
	// active plugin binding.
	return &tls.Config{MinVersion: tls.VersionTLS13, ClientCAs: pool, ClientAuth: tls.VerifyClientCertIfGiven}, nil
}

func readSecretReference(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("runtime: read secret reference: %w", err)
	}
	if len(raw) == 0 || len(raw) > 64*1024 {
		return "", fmt.Errorf("runtime: secret reference size outside 1..65536 bytes")
	}
	secret := strings.TrimSpace(string(raw))
	if secret == "" {
		return "", fmt.Errorf("runtime: secret reference is empty")
	}
	return secret, nil
}
