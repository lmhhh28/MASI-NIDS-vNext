package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"

	"masi-nids/control-go/internal/config"
)

func TestDialMTLSClientRequiresMutualIdentityAndHealth(t *testing.T) {
	caCert, caKey, caPEM := testCA(t)
	serverCertPEM, serverKeyPEM := testLeaf(t, caCert, caKey, "adapter.test", true)
	clientCertPEM, clientKeyPEM := testLeaf(t, caCert, caKey, "control.test", false)
	dir := t.TempDir()
	caPath := writeTestPEM(t, dir, "ca.pem", caPEM)
	serverCertPath := writeTestPEM(t, dir, "server.pem", serverCertPEM)
	serverKeyPath := writeTestPEM(t, dir, "server-key.pem", serverKeyPEM)
	clientCertPath := writeTestPEM(t, dir, "client.pem", clientCertPEM)
	clientKeyPath := writeTestPEM(t, dir, "client-key.pem", clientKeyPEM)

	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		t.Fatal("append test CA")
	}
	serverIdentity, err := tls.LoadX509KeyPair(serverCertPath, serverKeyPath)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(&tls.Config{
		MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{serverIdentity},
		ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: roots,
	})))
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
	grpc_health_v1.RegisterHealthServer(server, healthServer)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); _ = listener.Close() })

	cfg := config.MTLSClient{Endpoint: listener.Addr().String(), ServerName: "adapter.test",
		CAFile: caPath, CertFile: clientCertPath, KeyFile: clientKeyPath, MaxMessageBytes: 1 << 20}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := dialMTLSClient(ctx, cfg)
	if err != nil {
		t.Fatalf("valid mutual TLS client rejected: %v", err)
	}
	_ = conn.Close()

	bad := cfg
	bad.ServerName = "wrong.test"
	badCtx, badCancel := context.WithTimeout(context.Background(), time.Second)
	defer badCancel()
	if conn, err := dialMTLSClient(badCtx, bad); err == nil {
		_ = conn.Close()
		t.Fatal("wrong server identity must fail closed")
	}
}

func testCA(t *testing.T) (*x509.Certificate, ed25519.PrivateKey, []byte) {
	t.Helper()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "MASI test CA"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour), IsCA: true,
		BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	raw, err := x509.CreateCertificate(rand.Reader, template, template, key.Public(), key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(raw)
	if err != nil {
		t.Fatal(err)
	}
	return cert, key, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: raw})
}

func testLeaf(t *testing.T, ca *x509.Certificate, caKey ed25519.PrivateKey, name string, server bool) ([]byte, []byte) {
	t.Helper()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	usage := x509.ExtKeyUsageClientAuth
	if server {
		usage = x509.ExtKeyUsageServerAuth
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: name},
		DNSNames: []string{name}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{usage}}
	raw, err := x509.CreateCertificate(rand.Reader, template, ca, key.Public(), caKey)
	if err != nil {
		t.Fatal(err)
	}
	privateRaw, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: raw}),
		pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: privateRaw})
}

func writeTestPEM(t *testing.T, dir, name string, data []byte) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}
