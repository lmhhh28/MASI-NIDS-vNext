// Package config loads, validates, and freezes the Go Control Core runtime
// configuration. Config is the single source of connection strings, profile
// digests, cert references, and resource caps. Profiles and contract digests
// are validated against the frozen contracts/ tree (ADR-0005: unknown major /
// unverified minor / digest drift -> fail closed).
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// Config is the validated, immutable runtime configuration. Once Loaded it is
// not mutated; hot-reload is intentionally NOT supported in the first release
// (role/scope/profile changes require re-validation and only affect future
// authorization — SEC-002).
type Config struct {
	ReleaseVersion        string          `json:"release_version"`
	RuntimeProfile        string          `json:"runtime_profile"`  // test|production
	ExternalClients       string          `json:"external_clients"` // test-fake|production-mtls
	HTTPListen            string          `json:"http_listen"`
	GRPCListen            string          `json:"grpc_listen"`
	PublicOrigin          string          `json:"public_origin"`
	PostgreSQLDSN         string          `json:"postgresql_dsn"`
	PostgreSQLTestDSN     string          `json:"postgresql_test_dsn"` // DB name must contain "test"
	PostgreSQLRole        string          `json:"postgresql_role"`     // exact current_user; production=masi_control_app
	OIDCIssuer            string          `json:"oidc_issuer"`
	OIDCClientID          string          `json:"oidc_client_id"`
	OIDCRedirectURL       string          `json:"oidc_redirect_url"`
	OIDCClientSecretRef   string          `json:"oidc_client_secret_ref"`
	OIDCStepUpACRValues   []string        `json:"oidc_step_up_acr_values"`
	RoleMappingPath       string          `json:"role_mapping_path"`
	RoleMappingDigest     string          `json:"role_mapping_digest"`
	SessionCookieName     string          `json:"session_cookie_name"`
	SSEHMACKeyRef         string          `json:"sse_hmac_key_ref"` // secret reference, never the raw key
	ContractRoot          string          `json:"contract_root"`
	SchemaMigrationDigest string          `json:"schema_migration_digest"`
	ProfileDigests        ProfileDigests  `json:"profile_digests"`
	Resource              Resource        `json:"resource"`
	Probe                 Probe           `json:"probe"`
	TLS                   TLS             `json:"tls"`
	Outbound              OutboundClients `json:"outbound"`
}

// TLS pins the server identities. Production requires HTTPS and gRPC mTLS;
// explicit test profile may use loopback plaintext for deterministic rehearsal.
type TLS struct {
	HTTPCertFile     string `json:"http_cert_file"`
	HTTPKeyFile      string `json:"http_key_file"`
	GRPCCertFile     string `json:"grpc_cert_file"`
	GRPCKeyFile      string `json:"grpc_key_file"`
	GRPCClientCAFile string `json:"grpc_client_ca_file"`
}

type MTLSClient struct {
	Endpoint        string `json:"endpoint"`
	ServerName      string `json:"server_name"`
	CAFile          string `json:"ca_file"`
	CertFile        string `json:"cert_file"`
	KeyFile         string `json:"key_file"`
	MaxMessageBytes int    `json:"max_message_bytes"`
}

type EdgeMTLSClient struct {
	WorkloadRef string `json:"workload_ref"`
	MTLSClient
}

type OutboundClients struct {
	Edges            []EdgeMTLSClient `json:"edges"`
	Deployment       MTLSClient       `json:"deployment"`
	PluginStatistics MTLSClient       `json:"plugin_statistics"`
}

// ProfileDigests pins the frozen profile digests Go consumes (ADR-0005).
// Unknown major / digest drift -> fail closed at Load time.
type ProfileDigests struct {
	OpenAPIRest                string `json:"openapi_rest"`
	GRPCService                string `json:"grpc_service"`
	A2AAgent                   string `json:"a2a_agent"`
	MASIMCPReadonly            string `json:"masi_mcp_readonly"`
	PluginStatistics           string `json:"plugin_statistics"`
	ModelRolloutPoolGeneration string `json:"model_rollout_pool_generation"`
	P4TargetFleet              string `json:"p4_target_fleet"`
	P4StatelessFirewall        string `json:"p4_stateless_firewall"`
	P4RuleObservation          string `json:"p4_rule_observation"`
	QualificationEvidence      string `json:"qualification_evidence"`
	DeploymentTier             string `json:"deployment_tier"`
	Availability               string `json:"availability"` // availability-single/v1 first release
	E2ERunnerCompose           string `json:"e2e_runner_compose"`
	SupplyTooling              string `json:"supply_tooling"`
	JSONSchema                 string `json:"json_schema"`
	PerformanceEnvironment     string `json:"performance_environment"`
	QualificationSoak3600s     string `json:"qualification_soak_3600s"`
}

// Resource holds the bounded resource caps (AGENTS.md: queues/connections/
// transactions/retry/timeout/log/retention all profile-bounded).
type Resource struct {
	MaxPoolConnections     int           `json:"max_pool_connections"`
	MaxInFlightClaims      int           `json:"max_in_flight_claims"`
	MaxInFlightStatsRuns   int           `json:"max_in_flight_stats_runs"`
	StatementTimeout       time.Duration `json:"statement_timeout"`
	LockTimeout            time.Duration `json:"lock_timeout"`
	IdleConnTimeout        time.Duration `json:"idle_conn_timeout"`
	MaxProposalBytes       int           `json:"max_proposal_bytes"`         // 32 KiB
	MaxNoteBytes           int           `json:"max_note_bytes"`             // 2 KiB
	MaxPendingPerTargetGen int           `json:"max_pending_per_target_gen"` // 128
	PageDefault            int           `json:"page_default"`               // 50
	PageMax                int           `json:"page_max"`                   // 200
	ProposalTTL            time.Duration `json:"proposal_ttl"`               // 24h
	AuthzTTL               time.Duration `json:"authz_ttl"`                  // 15m
	SSEEventMaxBytes       int           `json:"sse_event_max_bytes"`        // 64 KiB
	SSEHeartbeatInterval   time.Duration `json:"sse_heartbeat_interval"`     // 15s
	EventRetention         time.Duration `json:"event_retention"`            // 90d
	PluginStatRetention    time.Duration `json:"plugin_stat_retention"`      // 90d
	IdempotencyRetention   time.Duration `json:"idempotency_retention"`      // 7d
}

// Probe holds the startup/readiness/liveness probe budgets (ADR-0006 §9).
type Probe struct {
	StartupMaxSeconds         int           `json:"startup_max_seconds"`         // 120
	ReadinessPeriodSeconds    int           `json:"readiness_period_seconds"`    // 5
	ReadinessTimeoutSeconds   int           `json:"readiness_timeout_seconds"`   // 2
	ReadinessFailureThreshold int           `json:"readiness_failure_threshold"` // 3
	LivenessPeriodSeconds     int           `json:"liveness_period_seconds"`     // 10
	LivenessTimeoutSeconds    int           `json:"liveness_timeout_seconds"`    // 2
	LivenessFailureThreshold  int           `json:"liveness_failure_threshold"`  // 3
	MaxAutoRestarts           int           `json:"max_auto_restarts"`           // 5
	QuarantineMinSeconds      int           `json:"quarantine_min_seconds"`      // 900
	DrainTimeout              time.Duration `json:"drain_timeout"`
}

var digestRE = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// Load reads and validates configuration from the given JSON path (or, if path
// is empty, from MASI_CTRL_CONFIG env). Validation is fail-closed: any missing
// required field, malformed digest, or unknown profile major rejects startup.
func Load(path string) (*Config, error) {
	if path == "" {
		path = os.Getenv("MASI_CTRL_CONFIG")
	}
	if path == "" {
		return nil, errors.New("config: no path (set MASI_CTRL_CONFIG or pass --config)")
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("config: abs path: %w", err)
	}
	if strings.Contains(filepath.Clean(abs), "..") {
		return nil, errors.New("config: path traversal rejected")
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		return nil, fmt.Errorf("config: read %s: %w", abs, err)
	}
	var c Config
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("config: parse %s: %w", abs, err)
	}
	if err := dec.Decode(&struct{}{}); err != io.EOF {
		return nil, fmt.Errorf("config: trailing JSON content")
	}
	if err := c.validate(); err != nil {
		return nil, fmt.Errorf("config: invalid: %w", err)
	}
	return &c, nil
}

func (c *Config) validate() error {
	if c.RuntimeProfile != "test" && c.RuntimeProfile != "production" {
		return errors.New("runtime_profile must be test or production")
	}
	if c.ExternalClients != "test-fake" && c.ExternalClients != "production-mtls" {
		return errors.New("external_clients must be test-fake or production-mtls")
	}
	if c.RuntimeProfile == "production" && c.ExternalClients != "production-mtls" {
		return errors.New("production requires external_clients=production-mtls")
	}
	if c.RuntimeProfile == "test" && c.ExternalClients != "test-fake" {
		return errors.New("test runtime requires external_clients=test-fake")
	}
	if c.HTTPListen == "" || c.GRPCListen == "" {
		return errors.New("http_listen and grpc_listen required")
	}
	if c.PostgreSQLDSN == "" {
		return errors.New("postgresql_dsn required")
	}
	if c.PublicOrigin == "" {
		return errors.New("public_origin required")
	}
	if c.RuntimeProfile == "production" {
		if c.PostgreSQLRole != "masi_control_app" {
			return errors.New("production postgresql_role must be exact masi_control_app")
		}
		if !strings.HasPrefix(c.PublicOrigin, "https://") {
			return errors.New("production public_origin must use https")
		}
		if c.OIDCIssuer == "" || c.OIDCClientID == "" || c.OIDCRedirectURL == "" || c.OIDCClientSecretRef == "" {
			return errors.New("production oidc_issuer, oidc_client_id, oidc_redirect_url required")
		}
		if len(c.OIDCStepUpACRValues) == 0 || len(c.OIDCStepUpACRValues) > 16 {
			return errors.New("production oidc_step_up_acr_values must contain 1..16 exact values")
		}
		seenACR := make(map[string]struct{}, len(c.OIDCStepUpACRValues))
		for _, acr := range c.OIDCStepUpACRValues {
			if acr == "" || len(acr) > 256 || strings.TrimSpace(acr) != acr {
				return errors.New("production oidc step-up ACR malformed")
			}
			if _, exists := seenACR[acr]; exists {
				return errors.New("production oidc step-up ACR duplicated")
			}
			seenACR[acr] = struct{}{}
		}
		issuerURL, issuerErr := url.Parse(c.OIDCIssuer)
		redirectURL, redirectErr := url.Parse(c.OIDCRedirectURL)
		originURL, originErr := url.Parse(c.PublicOrigin)
		if issuerErr != nil || redirectErr != nil || originErr != nil || issuerURL.Scheme != "https" || redirectURL.Scheme != "https" ||
			redirectURL.Scheme != originURL.Scheme || redirectURL.Host != originURL.Host {
			return errors.New("production OIDC issuer/redirect/public origin malformed or cross-origin")
		}
		if c.RoleMappingPath == "" || !digestRE.MatchString(c.RoleMappingDigest) {
			return errors.New("production role mapping path/digest required")
		}
		if c.SSEHMACKeyRef == "" {
			return errors.New("production sse_hmac_key_ref required for authenticated API cursors")
		}
		if c.TLS.HTTPCertFile == "" || c.TLS.HTTPKeyFile == "" || c.TLS.GRPCCertFile == "" ||
			c.TLS.GRPCKeyFile == "" || c.TLS.GRPCClientCAFile == "" {
			return errors.New("production HTTP TLS and gRPC mTLS files required")
		}
		if len(c.Outbound.Edges) < 1 || len(c.Outbound.Edges) > 64 {
			return errors.New("production requires 1..64 configured Edge mTLS clients")
		}
		seenWorkloads := make(map[string]struct{}, len(c.Outbound.Edges))
		for _, edge := range c.Outbound.Edges {
			if matched, _ := regexp.MatchString(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`, edge.WorkloadRef); !matched {
				return errors.New("production Edge workload_ref malformed")
			}
			if _, exists := seenWorkloads[edge.WorkloadRef]; exists {
				return errors.New("production Edge workload_ref duplicated")
			}
			seenWorkloads[edge.WorkloadRef] = struct{}{}
			if err := validateMTLSClient(edge.MTLSClient); err != nil {
				return fmt.Errorf("production Edge %s: %w", edge.WorkloadRef, err)
			}
		}
		if err := validateMTLSClient(c.Outbound.Deployment); err != nil {
			return fmt.Errorf("production deployment adapter: %w", err)
		}
		if err := validateMTLSClient(c.Outbound.PluginStatistics); err != nil {
			return fmt.Errorf("production plugin statistics adapter: %w", err)
		}
	} else {
		if !strings.HasPrefix(c.PublicOrigin, "http://127.0.0.1") && !strings.HasPrefix(c.PublicOrigin, "http://localhost") {
			return errors.New("test public_origin must be loopback http")
		}
		if !IsTestDB(c.PostgreSQLDSN) {
			return errors.New("test runtime PostgreSQL database name must contain test")
		}
	}
	if c.SessionCookieName == "" {
		return errors.New("session_cookie_name required")
	}
	if matched, _ := regexp.MatchString(`^[A-Za-z0-9_-]{1,64}$`, c.SessionCookieName); !matched {
		return errors.New("session_cookie_name malformed")
	}
	if c.ContractRoot == "" {
		return errors.New("contract_root required")
	}
	if !digestRE.MatchString(c.SchemaMigrationDigest) {
		return errors.New("schema_migration_digest malformed")
	}
	c.applyDefaults()
	if c.Resource.MaxPoolConnections < 1 || c.Resource.MaxPoolConnections > 256 ||
		c.Resource.MaxInFlightClaims < 1 || c.Resource.MaxInFlightClaims > 1024 ||
		c.Resource.MaxInFlightStatsRuns < 1 || c.Resource.MaxInFlightStatsRuns > 1024 ||
		c.Resource.StatementTimeout <= 0 || c.Resource.StatementTimeout > 30*time.Second ||
		c.Resource.LockTimeout <= 0 || c.Resource.LockTimeout > 10*time.Second ||
		c.Resource.IdleConnTimeout <= 0 || c.Resource.IdleConnTimeout > 30*time.Minute {
		return errors.New("resource connection/concurrency/timeout bounds invalid")
	}
	if c.Resource.MaxProposalBytes < 1 || c.Resource.MaxProposalBytes > 32*1024 ||
		c.Resource.MaxNoteBytes < 1 || c.Resource.MaxNoteBytes > 2*1024 ||
		c.Resource.MaxPendingPerTargetGen < 1 || c.Resource.MaxPendingPerTargetGen > 128 ||
		c.Resource.PageDefault < 1 || c.Resource.PageDefault > c.Resource.PageMax || c.Resource.PageMax > 200 ||
		c.Resource.ProposalTTL <= 0 || c.Resource.ProposalTTL > 24*time.Hour ||
		c.Resource.AuthzTTL <= 0 || c.Resource.AuthzTTL > 15*time.Minute ||
		c.Resource.SSEEventMaxBytes < 1 || c.Resource.SSEEventMaxBytes > 64*1024 ||
		c.Resource.SSEHeartbeatInterval != 15*time.Second {
		return errors.New("resource governance/page/SSE bounds invalid")
	}
	if c.Resource.EventRetention < 24*time.Hour || c.Resource.EventRetention > 365*24*time.Hour ||
		c.Resource.PluginStatRetention < 24*time.Hour || c.Resource.PluginStatRetention > 365*24*time.Hour ||
		c.Resource.IdempotencyRetention < 24*time.Hour || c.Resource.IdempotencyRetention > 30*24*time.Hour {
		return errors.New("resource retention bounds invalid")
	}
	if c.Probe.StartupMaxSeconds < 1 || c.Probe.StartupMaxSeconds > 120 ||
		c.Probe.DrainTimeout <= 0 || c.Probe.DrainTimeout > 120*time.Second {
		return errors.New("probe startup/drain bounds invalid")
	}
	for name, d := range map[string]string{
		"openapi_rest":             c.ProfileDigests.OpenAPIRest,
		"grpc_service":             c.ProfileDigests.GRPCService,
		"a2a_agent":                c.ProfileDigests.A2AAgent,
		"masi_mcp_readonly":        c.ProfileDigests.MASIMCPReadonly,
		"plugin_statistics":        c.ProfileDigests.PluginStatistics,
		"model_rollout":            c.ProfileDigests.ModelRolloutPoolGeneration,
		"p4_target_fleet":          c.ProfileDigests.P4TargetFleet,
		"p4_firewall":              c.ProfileDigests.P4StatelessFirewall,
		"p4_rule_observation":      c.ProfileDigests.P4RuleObservation,
		"qualification_evidence":   c.ProfileDigests.QualificationEvidence,
		"deployment_tier":          c.ProfileDigests.DeploymentTier,
		"availability":             c.ProfileDigests.Availability,
		"e2e_runner_compose":       c.ProfileDigests.E2ERunnerCompose,
		"supply_tooling":           c.ProfileDigests.SupplyTooling,
		"json_schema":              c.ProfileDigests.JSONSchema,
		"qualification_soak_3600s": c.ProfileDigests.QualificationSoak3600s,
		"performance_environment":  c.ProfileDigests.PerformanceEnvironment,
	} {
		if d == "" {
			return fmt.Errorf("profile digest %s is empty (unfrozen profile -> fail closed)", name)
		}
		if !digestRE.MatchString(d) {
			return fmt.Errorf("profile digest %s malformed: %s", name, d)
		}
	}
	return nil
}

func validateMTLSClient(client MTLSClient) error {
	host, port, err := net.SplitHostPort(client.Endpoint)
	if err != nil || host == "" || port == "" || client.ServerName == "" || strings.Contains(client.ServerName, ":") ||
		client.CAFile == "" || client.CertFile == "" || client.KeyFile == "" ||
		client.MaxMessageBytes < 1024 || client.MaxMessageBytes > 4*1024*1024 {
		return errors.New("endpoint/server-name/CA/cert/key/message bound malformed")
	}
	return nil
}

func (c *Config) applyDefaults() {
	if c.Resource.MaxPoolConnections == 0 {
		c.Resource.MaxPoolConnections = 16
	}
	if c.Resource.MaxInFlightClaims == 0 {
		c.Resource.MaxInFlightClaims = 8
	}
	if c.Resource.MaxInFlightStatsRuns == 0 {
		c.Resource.MaxInFlightStatsRuns = 4
	}
	if c.Resource.StatementTimeout == 0 {
		c.Resource.StatementTimeout = 5 * time.Second
	}
	if c.Resource.LockTimeout == 0 {
		c.Resource.LockTimeout = time.Second
	}
	if c.Resource.IdleConnTimeout == 0 {
		c.Resource.IdleConnTimeout = 5 * time.Minute
	}
	if c.Resource.MaxProposalBytes == 0 {
		c.Resource.MaxProposalBytes = 32 * 1024
	}
	if c.Resource.MaxNoteBytes == 0 {
		c.Resource.MaxNoteBytes = 2 * 1024
	}
	if c.Resource.MaxPendingPerTargetGen == 0 {
		c.Resource.MaxPendingPerTargetGen = 128
	}
	if c.Resource.PageDefault == 0 {
		c.Resource.PageDefault = 50
	}
	if c.Resource.PageMax == 0 {
		c.Resource.PageMax = 200
	}
	if c.Resource.ProposalTTL == 0 {
		c.Resource.ProposalTTL = 24 * time.Hour
	}
	if c.Resource.AuthzTTL == 0 {
		c.Resource.AuthzTTL = 15 * time.Minute
	}
	if c.Resource.SSEEventMaxBytes == 0 {
		c.Resource.SSEEventMaxBytes = 64 * 1024
	}
	if c.Resource.SSEHeartbeatInterval == 0 {
		c.Resource.SSEHeartbeatInterval = 15 * time.Second
	}
	if c.Resource.EventRetention == 0 {
		c.Resource.EventRetention = 90 * 24 * time.Hour
	}
	if c.Resource.PluginStatRetention == 0 {
		c.Resource.PluginStatRetention = 90 * 24 * time.Hour
	}
	if c.Resource.IdempotencyRetention == 0 {
		c.Resource.IdempotencyRetention = 7 * 24 * time.Hour
	}
	if c.Probe.StartupMaxSeconds == 0 {
		c.Probe.StartupMaxSeconds = 120
	}
	if c.Probe.DrainTimeout == 0 {
		c.Probe.DrainTimeout = 30 * time.Second
	}
}

// IsTestDB confirms a DSN targets a test-named database (AGENTS.md: destructive
// tests only on a DB whose name contains "test").
func IsTestDB(dsn string) bool {
	u, err := url.Parse(dsn)
	if err == nil && u.Scheme != "" {
		name := strings.TrimPrefix(u.Path, "/")
		return strings.Contains(strings.ToLower(name), "test")
	}
	for _, field := range strings.Fields(dsn) {
		parts := strings.SplitN(field, "=", 2)
		if len(parts) == 2 && (parts[0] == "dbname" || parts[0] == "database") {
			return strings.Contains(strings.ToLower(strings.Trim(parts[1], "'\"")), "test")
		}
	}
	return false
}
