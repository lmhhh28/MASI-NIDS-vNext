package config

import (
	"strings"
	"testing"
	"time"
)

func testConfig() Config {
	d := "sha256:" + strings.Repeat("a", 64)
	return Config{ReleaseVersion: "test", RuntimeProfile: "test", ExternalClients: "test-fake", HTTPListen: "127.0.0.1:18080", GRPCListen: "127.0.0.1:19090", PublicOrigin: "http://127.0.0.1:18080", PostgreSQLDSN: "postgres://u:p@127.0.0.1/db_test", SessionCookieName: "masi_session", ContractRoot: "../contracts", SchemaMigrationDigest: d,
		ProfileDigests: ProfileDigests{OpenAPIRest: d, GRPCService: d, A2AAgent: d, MASIMCPReadonly: d, PluginStatistics: d, ModelRolloutPoolGeneration: d, P4TargetFleet: d, P4StatelessFirewall: d, P4RuleObservation: d, QualificationEvidence: d, DeploymentTier: d, Availability: d, E2ERunnerCompose: d, SupplyTooling: d, JSONSchema: d, PerformanceEnvironment: d, QualificationSoak3600s: d},
		Resource:       Resource{MaxPoolConnections: 4, MaxInFlightClaims: 2, MaxInFlightStatsRuns: 2, StatementTimeout: time.Second, LockTimeout: time.Second, IdleConnTimeout: time.Minute}}
}

func TestTestProfileValidation(t *testing.T) {
	c := testConfig()
	if err := c.validate(); err != nil {
		t.Fatal(err)
	}
	c.PostgreSQLDSN = "postgres://test:pw@127.0.0.1/production"
	if err := c.validate(); err == nil {
		t.Fatal("test in username must not make a production DB test-named")
	}
}

func TestAcceptanceProfileRequiresLoopbackMTLSAnalysis(t *testing.T) {
	c := testConfig()
	c.RuntimeProfile = "acceptance"
	c.TLS = TLS{GRPCCertFile: "grpc.crt", GRPCKeyFile: "grpc.key", GRPCClientCAFile: "clients.ca",
		GRPCAllowedClientSANs: []string{"edge-acceptance"}}
	c.Outbound.Analysis = []A2APeer{{
		PeerID: "analysis-acceptance", PluginID: "masi.analysis.langgraph",
		BaseURL: "https://127.0.0.1:17446", ServerName: "localhost",
		CAFile: "ca.pem", CertFile: "client.pem", KeyFile: "client.key",
		AllowedIPs: []string{"127.0.0.1"}, MaxResponseBytes: 128 << 10,
	}}
	if err := c.validate(); err != nil {
		t.Fatalf("bounded acceptance mTLS peer rejected: %v", err)
	}
	c.Outbound.Analysis[0].BaseURL = "http://127.0.0.1:17446"
	if err := c.validate(); err == nil {
		t.Fatal("acceptance plaintext Analysis peer must fail")
	}
	c = testConfig()
	c.RuntimeProfile = "acceptance"
	c.TLS = TLS{GRPCCertFile: "grpc.crt", GRPCKeyFile: "grpc.key", GRPCClientCAFile: "clients.ca",
		GRPCAllowedClientSANs: []string{"edge-acceptance"}}
	c.ExternalClients = "production-mtls"
	if err := c.validate(); err == nil {
		t.Fatal("acceptance must not enable the production outbound client set")
	}
}

func TestAcceptanceProfileRequiresInboundGRPCMTLS(t *testing.T) {
	c := testConfig()
	c.RuntimeProfile = "acceptance"
	if err := c.validate(); err == nil {
		t.Fatal("acceptance without inbound gRPC mTLS must fail")
	}
	c.TLS = TLS{GRPCCertFile: "grpc.crt", GRPCKeyFile: "grpc.key", GRPCClientCAFile: "clients.ca",
		GRPCAllowedClientSANs: []string{"edge-acceptance"}}
	if err := c.validate(); err != nil {
		t.Fatalf("acceptance inbound gRPC mTLS rejected: %v", err)
	}
	c.TLS.GRPCAllowedClientSANs = []string{"edge-acceptance", "edge-acceptance"}
	if err := c.validate(); err == nil {
		t.Fatal("acceptance duplicate gRPC client SAN must fail")
	}
}

func TestAcceptancePluginStatisticsRequiresLoopbackMTLS(t *testing.T) {
	c := testConfig()
	c.RuntimeProfile = "acceptance"
	c.TLS = TLS{GRPCCertFile: "grpc.crt", GRPCKeyFile: "grpc.key", GRPCClientCAFile: "clients.ca",
		GRPCAllowedClientSANs: []string{"edge-acceptance"}}
	c.Outbound.PluginStatistics = MTLSClient{Endpoint: "127.0.0.1:19443", ServerName: "plugin-host.test",
		CAFile: "ca.pem", CertFile: "manager.pem", KeyFile: "manager.key", MaxMessageBytes: 4 << 20}
	if err := c.validate(); err != nil {
		t.Fatalf("bounded acceptance statistics mTLS adapter rejected: %v", err)
	}
	c.Outbound.PluginStatistics.Endpoint = "192.0.2.1:19443"
	if err := c.validate(); err == nil {
		t.Fatal("acceptance non-loopback statistics adapter must fail")
	}
	c.Outbound.PluginStatistics.Endpoint = "127.0.0.1:not-a-port"
	if err := c.validate(); err == nil {
		t.Fatal("acceptance non-numeric statistics port must fail")
	}
}

func TestProductionFailsWithoutTLSAndOIDC(t *testing.T) {
	c := testConfig()
	c.RuntimeProfile = "production"
	c.ExternalClients = "production-mtls"
	c.PublicOrigin = "https://ctrl.example"
	if err := c.validate(); err == nil {
		t.Fatal("production without TLS/OIDC must fail")
	}
}

func TestProductionAcceptsCompleteOutboundMTLSProfile(t *testing.T) {
	c := testConfig()
	c.RuntimeProfile = "production"
	c.ExternalClients = "production-mtls"
	c.PublicOrigin = "https://ctrl.example"
	c.PostgreSQLDSN = "postgres://control:secret@db.example/masi?sslmode=verify-full"
	c.PostgreSQLRole = "masi_control_app"
	c.OIDCIssuer = "https://idp.example"
	c.OIDCClientID = "control-client"
	c.OIDCRedirectURL = "https://ctrl.example/oidc/callback"
	c.OIDCClientSecretRef = "/run/secrets/oidc"
	c.OIDCStepUpACRValues = []string{"urn:example:webauthn"}
	c.RoleMappingPath = "/etc/masi/roles.json"
	c.RoleMappingDigest = c.SchemaMigrationDigest
	c.SSEHMACKeyRef = "/run/secrets/cursor-hmac"
	c.TLS = TLS{HTTPCertFile: "http.crt", HTTPKeyFile: "http.key", HTTPClientCAFile: "clients.ca", GRPCCertFile: "grpc.crt",
		GRPCKeyFile: "grpc.key", GRPCClientCAFile: "clients.ca", GRPCAllowedClientSANs: []string{"edge-a"}}
	client := MTLSClient{Endpoint: "adapter.example:9443", ServerName: "adapter.example",
		CAFile: "ca.pem", CertFile: "client.pem", KeyFile: "client.key", MaxMessageBytes: 4 << 20}
	c.Outbound = OutboundClients{Edges: []EdgeMTLSClient{{WorkloadRef: "edge-a", MTLSClient: client}},
		Deployment: client, PluginStatistics: client,
		Analysis: []A2APeer{{PeerID: "analysis-a", PluginID: "masi.analysis.langgraph",
			BaseURL: "https://analysis.example", ServerName: "analysis.example", CAFile: "ca.pem",
			CertFile: "client.pem", KeyFile: "client.key", AllowedIPs: []string{"192.0.2.10"}, MaxResponseBytes: 128 << 10}}}
	if err := c.validate(); err != nil {
		t.Fatalf("complete production outbound mTLS profile rejected: %v", err)
	}
}

func TestProductionRejectsPostgresWithoutVerifyFull(t *testing.T) {
	c := testConfig()
	c.RuntimeProfile = "production"
	c.ExternalClients = "production-mtls"
	c.PostgreSQLDSN = "postgres://u:p@db.example/masi?sslmode=disable"
	if err := validateProductionPostgresDSN(c.PostgreSQLDSN); err == nil {
		t.Fatal("sslmode=disable must fail")
	}
	c.PostgreSQLDSN = "postgres://u:p@db.example/masi?sslmode=verify-full"
	if err := validateProductionPostgresDSN(c.PostgreSQLDSN); err != nil {
		t.Fatal(err)
	}
}

func TestTestProfileRejectsNonLoopbackListeners(t *testing.T) {
	c := testConfig()
	c.HTTPListen = "0.0.0.0:18080"
	if err := c.validate(); err == nil {
		t.Fatal("non-loopback HTTP listener must fail")
	}
	c = testConfig()
	c.GRPCListen = "[::]:19090"
	if err := c.validate(); err == nil {
		t.Fatal("non-loopback gRPC listener must fail")
	}
}
func TestIsTestDBParsesDatabaseName(t *testing.T) {
	if !IsTestDB("postgres://u:p@localhost/masi_test") {
		t.Fatal("test DB rejected")
	}
	if IsTestDB("postgres://test_user:p@localhost/masi") {
		t.Fatal("username must not satisfy test DB guard")
	}
}

func TestResourceProfileCannotRelaxFrozenBounds(t *testing.T) {
	tests := []struct {
		name string
		edit func(*Config)
	}{
		{"proposal-bytes", func(c *Config) { c.Resource.MaxProposalBytes = 32*1024 + 1 }},
		{"note-bytes", func(c *Config) { c.Resource.MaxNoteBytes = 2049 }},
		{"pending", func(c *Config) { c.Resource.MaxPendingPerTargetGen = 129 }},
		{"page", func(c *Config) { c.Resource.PageMax = 201 }},
		{"proposal-ttl", func(c *Config) { c.Resource.ProposalTTL = 24*time.Hour + time.Second }},
		{"authz-ttl", func(c *Config) { c.Resource.AuthzTTL = 15*time.Minute + time.Second }},
		{"sse-bytes", func(c *Config) { c.Resource.SSEEventMaxBytes = 65537 }},
		{"sse-heartbeat", func(c *Config) { c.Resource.SSEHeartbeatInterval = 14 * time.Second }},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			c := testConfig()
			c.applyDefaults()
			tc.edit(&c)
			if err := c.validate(); err == nil {
				t.Fatal("relaxed resource bound must fail closed")
			}
		})
	}
}
