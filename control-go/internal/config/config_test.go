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
	c.PostgreSQLDSN = "postgres://control:secret@db.example/masi"
	c.PostgreSQLRole = "masi_control_app"
	c.OIDCIssuer = "https://idp.example"
	c.OIDCClientID = "control-client"
	c.OIDCRedirectURL = "https://ctrl.example/oidc/callback"
	c.OIDCClientSecretRef = "/run/secrets/oidc"
	c.OIDCStepUpACRValues = []string{"urn:example:webauthn"}
	c.RoleMappingPath = "/etc/masi/roles.json"
	c.RoleMappingDigest = c.SchemaMigrationDigest
	c.SSEHMACKeyRef = "/run/secrets/cursor-hmac"
	c.TLS = TLS{HTTPCertFile: "http.crt", HTTPKeyFile: "http.key", GRPCCertFile: "grpc.crt",
		GRPCKeyFile: "grpc.key", GRPCClientCAFile: "clients.ca"}
	client := MTLSClient{Endpoint: "adapter.example:9443", ServerName: "adapter.example",
		CAFile: "ca.pem", CertFile: "client.pem", KeyFile: "client.key", MaxMessageBytes: 4 << 20}
	c.Outbound = OutboundClients{Edges: []EdgeMTLSClient{{WorkloadRef: "edge-a", MTLSClient: client}},
		Deployment: client, PluginStatistics: client}
	if err := c.validate(); err != nil {
		t.Fatalf("complete production outbound mTLS profile rejected: %v", err)
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
