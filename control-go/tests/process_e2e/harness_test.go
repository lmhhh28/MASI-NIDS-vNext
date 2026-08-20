package process_e2e

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	edgev1 "masi-nids/control-go/internal/grpc/edgev1"
)

// e2eDigest is the canonical digest used across the e2e seed/fixtures.
var e2eDigest = "sha256:" + strings.Repeat("a", 64)

// e2eActors are the two identities granted by testdata/role-mapping-e2e.json.
const (
	e2eIssuer    = "https://idp.example"
	e2eMakerSub  = "operator-a" // levels: operator, scoped-operator, platform-admin, analyst
	e2eCheckerSub = "operator-b" // levels: operator, scoped-operator, analyst
)

// requireE2E skips unless the real-process E2E env is configured. When
// MASI_CONTROL_E2E_REQUIRED=1 a missing env is fatal rather than a skip.
func requireE2E(t *testing.T) (dsn, binary, configPath string) {
	t.Helper()
	dsn = os.Getenv("MASI_CONTROL_E2E_DSN")
	binary = os.Getenv("MASI_CONTROL_E2E_BINARY")
	configPath = os.Getenv("MASI_CONTROL_E2E_CONFIG")
	if dsn == "" || binary == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatalf("real-process E2E required but DSN/BINARY/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/BINARY/CONFIG for real-process E2E")
	}
	return dsn, binary, configPath
}

type proc struct {
	t        *testing.T
	baseURL  string
	grpcAddr string
	cc       *grpc.ClientConn
	db       *pgx.Conn
	cmd      *exec.Cmd
	logs     bytes.Buffer
	done     chan error
}

// startControlCore launches the real control-core binary, waits for /readyz,
// opens a DB connection, and returns a proc. t.Cleanup stops the process and
// closes connections. Tests are serial (no t.Parallel) and share the fixed
// e2e config ports, so the port is free once the previous test's process drains.
func startControlCore(t *testing.T) *proc {
	t.Helper()
	dsn, binary, configPath := requireE2E(t)
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var cfg struct {
		HTTPListen string `json:"http_listen"`
		GRPCListen string `json:"grpc_listen"`
	}
	if err := json.Unmarshal(raw, &cfg); err != nil || cfg.HTTPListen == "" || cfg.GRPCListen == "" {
		t.Fatalf("parse runtime endpoints: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	p := &proc{
		t:        t,
		baseURL:  "http://" + cfg.HTTPListen,
		grpcAddr: cfg.GRPCListen,
		db:       conn,
		cmd:      exec.Command(binary, "--config", configPath),
		done:     make(chan error, 1),
	}
	// The binary resolves relative config paths (role_mapping_path, contract_root)
	// from its working directory. Run it from the control-go root so the e2e
	// config's "testdata/..." and "../contracts" paths resolve regardless of the
	// test binary's cwd.
	p.cmd.Dir = filepath.Dir(filepath.Dir(configPath))
	p.cmd.Stdout = &p.logs
	p.cmd.Stderr = &p.logs
	if err := p.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() { p.done <- p.cmd.Wait() }()
	stopped := false
	t.Cleanup(func() {
		if !stopped {
			_ = p.cmd.Process.Signal(syscall.SIGTERM)
			select {
			case <-p.done:
			case <-time.After(8 * time.Second):
				_ = p.cmd.Process.Kill()
			}
		}
		_ = p.db.Close(context.Background())
		if p.cc != nil {
			_ = p.cc.Close()
		}
	})
	// On failure, dump the real process logs so blackbox assertion failures are
	// diagnosable without importing internal packages or using logs as an oracle.
	t.Cleanup(func() {
		if t.Failed() {
			t.Log("control-core process logs:\n" + p.logs.String())
		}
	})
	for i := 0; i < 50; i++ {
		resp, err := http.Get(p.baseURL + "/readyz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				break
			}
		}
		select {
		case err := <-p.done:
			t.Fatalf("process exited before ready: %v\n%s", err, p.logs.String())
		default:
		}
		time.Sleep(100 * time.Millisecond)
	}
	cc, err := grpc.NewClient(p.grpcAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	p.cc = cc
	return p
}

func (p *proc) sink() edgev1.ControlSinkClient { return edgev1.NewControlSinkClient(p.cc) }

// jsonBody marshals v to an io.Reader for hand-built requests (used for
// negative-case requests that bypass the mutate helper).
func jsonBody(v any) *bytes.Reader {
	b, _ := json.Marshal(v)
	return bytes.NewReader(b)
}

// errNotReady is a sentinel error used by waitFor conditions to signal "keep
// polling" without failing the test.
type errNotReady string

func (e errNotReady) Error() string { return string(e) }

// login mints a test session via POST /oidc/test-login and returns the
// "masi_session=<id>" cookie header value.
func (p *proc) login(t *testing.T, subject string) string {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"issuer": e2eIssuer, "subject": subject})
	resp, err := http.Post(p.baseURL+"/oidc/test-login", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("test-login: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("test-login status=%d", resp.StatusCode)
	}
	for _, c := range resp.Cookies() {
		if c.Name == "masi_session" {
			return "masi_session=" + c.Value
		}
	}
	t.Fatal("test-login: no masi_session cookie")
	return ""
}

// csrf fetches the CSRF token bound to the session via GET /api/session.
func (p *proc) csrf(t *testing.T, cookie string) string {
	t.Helper()
	req, _ := http.NewRequest(http.MethodGet, p.baseURL+"/api/session", nil)
	req.Header.Set("Cookie", cookie)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("session: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("session status=%d", resp.StatusCode)
	}
	var out struct {
		CSRFToken string `json:"csrf_token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.CSRFToken == "" {
		t.Fatal("no csrf_token")
	}
	return out.CSRFToken
}

// session logs in and returns (cookie, csrf).
func (p *proc) session(t *testing.T, subject string) (string, string) {
	cookie := p.login(t, subject)
	return cookie, p.csrf(t, cookie)
}

// mutate POSTs a JSON mutation with session/CSRF/Origin. It returns the status
// code and response body. Origin must match public_origin (the loopback baseURL).
func (p *proc) mutate(t *testing.T, cookie, csrf, path string, payload any) (int, string) {
	t.Helper()
	body, _ := json.Marshal(payload)
	req, _ := http.NewRequest(http.MethodPost, p.baseURL+path, bytes.NewReader(body))
	req.Header.Set("Cookie", cookie)
	req.Header.Set("X-CSRF-Token", csrf)
	req.Header.Set("Origin", p.baseURL)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("mutate %s: %v", path, err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

// get performs an authenticated read for oracle assertions.
func (p *proc) get(t *testing.T, cookie, path string) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodGet, p.baseURL+path, nil)
	req.Header.Set("Cookie", cookie)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("get %s: %v", path, err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

// ---- DB helpers ----

func (p *proc) exec(ctx context.Context, sql string, args ...any) {
	if _, err := p.db.Exec(ctx, sql, args...); err != nil {
		p.t.Fatalf("exec: %v\nsql=%s", err, sql)
	}
}

func (p *proc) queryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	return p.db.QueryRow(ctx, sql, args...)
}

// resetE2E clears all business tables (the DB is a dedicated *_test DB),
// preserving the migration-seeded model_control_state singleton (reset to
// inactive) and masi_schema_meta. Used to give every test a clean known state.
func (p *proc) resetE2E(ctx context.Context) {
	tables := []string{
		"api_mutation_idempotency", "effect_acknowledgements", "effect_attempts",
		"effect_decisions", "effect_intents", "effect_proposals", "event_identities",
		"events", "evidence_refs", "firewall_activations", "firewall_bindings",
		"firewall_overlays", "firewall_revisions", "fleet_child_intents", "fleet_operations",
		"incident_projection_events", "incidents", "logical_pools",
		"model_control_incarnations", "model_pool_observation_events",
		"model_pool_observations_current", "model_revision_events", "model_revisions",
		"model_rollout_groups", "model_rollout_group_shards", "model_rollout_operations",
		"plugin_audit_events", "plugin_bindings", "plugin_manifests", "plugin_qualifications",
		"plugin_statistic_artifacts", "plugin_statistic_input_bundles", "plugin_statistic_runs",
		"plugin_statistic_schedules", "plugin_statistics_current", "plugin_statistics_definitions",
		"plugin_statistics_history", "pool_generations", "rule_observation_epochs",
		"rule_observations", "shard_bindings", "target_assignments",
		"target_capability_observation_events", "target_capability_observations",
		"target_lifecycle_events", "targets",
	}
	// TRUNCATE handles FK ordering within the set; CASCADE covers any dependent.
	// NOTE: CASCADE also empties model_control_state (it FK-references
	// model_control_incarnations), so the migration-seeded singleton must be
	// restored here — the event-ingest binding fence query and seedBaseline both
	// assume it exists.
	stmt := "TRUNCATE TABLE " + strings.Join(tables, ", ") + " RESTART IDENTITY CASCADE"
	if _, err := p.db.Exec(ctx, stmt); err != nil {
		p.t.Fatalf("resetE2E truncate: %v", err)
	}
	p.exec(ctx, `INSERT INTO model_control_state (singleton, writer_enabled) VALUES (true, false) ON CONFLICT (singleton) DO NOTHING`)
}

// seedBaseline inserts the canonical e2e target + model pool/binding that
// governance/firewall/model/fleet tests depend on. Mirrors the seed in the
// existing TestRealControlProcessCommitResults.
func (p *proc) seedBaseline(ctx context.Context) {
	d := e2eDigest
	p.exec(ctx, `INSERT INTO model_revisions(model_revision_id,model_revision_digest,model_bundle_digest,feature_contract_digest,label_contract_digest,output_adapter_digest,qualification_status,qualified_at_unix_ms,reader_runtime_profile,actor_ref,trace_id,scope) VALUES('rev-e2e',$1,$1,$1,$1,$1,'qualified',1,'model-runtime-central-cpu/v1','e2e','trace-e2e','scope-e2e') ON CONFLICT DO NOTHING`, d)
	p.exec(ctx, `INSERT INTO model_control_incarnations(incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id) VALUES('inc-e2e','initial',1,'e2e','trace-e2e') ON CONFLICT DO NOTHING`)
	p.exec(ctx, `UPDATE model_control_state SET active_incarnation_id='inc-e2e', writer_enabled=true`)
	p.exec(ctx, `INSERT INTO logical_pools(logical_pool_id,current_generation,availability_profile,runtime_profile,actor_ref,trace_id) VALUES('pool-e2e',1,'availability-single/v1','model-runtime-central-cpu/v1','e2e','trace-e2e') ON CONFLICT DO NOTHING`)
	p.exec(ctx, `INSERT INTO pool_generations(logical_pool_id,pool_generation,model_revision_id,startup_envelope_digest,pool_observation_digest,binding_digest,status,min_ready_replicas,capacity_qualified,model_revision_digest,model_bundle_digest,feature_contract_digest,label_contract_digest,output_adapter_digest,wire_profile_digest,runtime_profile_digest,optimization_profile_digest) VALUES('pool-e2e',1,'rev-e2e',$1,$1,$1,'active',1,true,$1,$1,$1,$1,$1,$1,$1,$1) ON CONFLICT DO NOTHING`, d)
	p.exec(ctx, `INSERT INTO shard_bindings(shard_id,logical_pool_id,model_control_incarnation_id,current_generation,current_binding_generation,current_revision_id,route_epoch,resume_state,loaded,ready,cas_digest,scope) VALUES('shard-e2e','pool-e2e','inc-e2e',1,1,'rev-e2e',1,'current',true,true,$1,'scope-e2e') ON CONFLICT(shard_id) DO UPDATE SET model_control_incarnation_id='inc-e2e',current_generation=1,current_binding_generation=1,current_revision_id='rev-e2e',route_epoch=1,resume_state='current',scope='scope-e2e'`, d)
	nowMS := time.Now().UnixMilli()
	p.exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id) VALUES('target-e2e','target e2e','https://127.0.0.1:9559',1,'masi','active',$1,'cred-e2e','scope-e2e','actor-e2e','trace-e2e')`, d)
	p.exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject) VALUES('target-e2e',1,'target-inc-e2e','edge-e2e','lease-e2e',$1,$2,1,10,'actor-epoch-e2e',1,'actor-e2e','trace-e2e','https://issuer.example','admin-e2e')`, nowMS-1000, nowMS+299000)
}

// waitFor polls up to timeout for cond to return (true, nil), failing the test
// with the last error if it never does. Used for maintenance-loop-driven
// outcomes (effect dispatch, overlay expiry, retention sweeps).
func waitFor(t *testing.T, timeout time.Duration, msg string, cond func() error) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		if err := cond(); err == nil {
			return
		} else {
			last = err
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatalf("%s: %v", msg, last)
}