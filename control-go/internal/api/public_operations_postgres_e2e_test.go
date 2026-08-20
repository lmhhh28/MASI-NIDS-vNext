package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/firewall"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

type firewallCompileFake struct {
	intent governance.Intent
}

func (f *firewallCompileFake) PreflightEffect(_ context.Context, intent governance.Intent) (governance.EdgePreflightResult, error) {
	f.intent = intent
	if _, err := governance.ToEdgeEffectIntent(intent); err != nil {
		return governance.EdgePreflightResult{}, err
	}
	return governance.EdgePreflightResult{TargetID: intent.TargetID, EffectIntentID: intent.EffectIntentID,
		PlanDigest: "sha256:" + strings.Repeat("9", 64), PhysicalEntries: 1,
		ExpiresAtUnixMS: time.Now().Add(20 * time.Second).UnixMilli(), Result: "accepted",
		ReasonCode: "PREFLIGHT_ACCEPTED", TraceID: intent.TraceID, PreflightToken: "secret-token"}, nil
}

func TestFirewallInspectionUsesReadOnlyEdgeCompileAndHidesTokenPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if configPath == "" || dsn == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("firewall inspection PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for firewall inspection PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const (
		targetID   = "api-compile-target-e2e"
		revisionID = "api-compile-revision-e2e"
		scope      = "scope-api-compile-e2e"
	)
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM firewall_bindings WHERE target_id='api-compile-target-e2e'`,
			`DELETE FROM firewall_revisions WHERE target_id='api-compile-target-e2e'`,
			`DELETE FROM target_capability_observation_events WHERE target_id='api-compile-target-e2e'`,
			`DELETE FROM target_capability_observations WHERE target_id='api-compile-target-e2e'`,
			`DELETE FROM target_assignments WHERE target_id='api-compile-target-e2e'`,
			`DELETE FROM targets WHERE target_id='api-compile-target-e2e'`,
		} {
			_, _ = pool.Exec(cleanCtx, statement)
		}
	}
	cleanup()
	defer cleanup()
	digest := "sha256:" + strings.Repeat("a", 64)
	nowMS := time.Now().UnixMilli()
	if _, err := pool.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,
	 role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
	 VALUES($1,'compile target','https://127.0.0.1:9559',44,'primary','active',$2,'cred-compile',$3,
	 'actor-compile','trace-compile-target')`, targetID, digest, scope); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,
	 edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
	 actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
	 VALUES($1,1,'target-inc-compile','edge-compile','lease-compile',$2,$3,100,199,
	 'actor-epoch-compile',1,'actor-compile','trace-compile-assignment','https://idp.example','admin-compile')`,
		targetID, nowMS-1000, nowMS+120_000); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_capability_observations(observation_id,target_id,
	 target_control_incarnation_id,assignment_generation,actor_runtime_epoch,application_generation,
	 p4info_digest,pipeline_digest,profile_digest,capacity_digest,capacity_available,lease_valid,
	 p4_connected,primary_actor,pipeline_exact,high_priority_queue_depth,telemetry_queue_depth,
	 observation_queue_depth,source_wal_bytes,input_wal_bytes,result_wal_bytes,freshness,reason_code,
	 observed_at_unix_ms,expires_at_unix_ms,trace_id)
	 VALUES('api-compile-observation',$1,'target-inc-compile',1,'actor-epoch-compile',1,$2,$2,$2,$2,
	 true,true,true,true,true,0,0,0,0,0,0,'fresh','READY',$3,$4,'trace-compile-observation')`,
		targetID, digest, nowMS, nowMS+120_000); err != nil {
		t.Fatal(err)
	}
	rule := firewall.Rule{RuleID: "compile-rule", RuleRevision: 1, Priority: 100,
		SourceIPv4:      firewall.IPv4Prefix{Address: "192.0.2.0", PrefixLength: 24},
		DestinationIPv4: firewall.IPv4Prefix{Address: "198.51.100.10", PrefixLength: 32},
		Protocol:        firewall.OptionalUint32{Present: true, Value: 6},
		L4Present:       firewall.OptionalBool{Present: true, Value: true},
		DestinationPort: firewall.OptionalUint32{Present: true, Value: 443},
		FragmentClass:   firewall.FragmentNone, Action: "drop", Enabled: true,
		ActorRef: "https://idp.example#admin-compile", ReasonCode: "REVISION_CREATED"}
	rule.CanonicalRuleDigest = firewall.ComputeRuleDigest(rule)
	revision, err := firewall.NewRevisionService(pool).Create(ctx, firewall.Revision{RevisionID: revisionID,
		TargetID: targetID, DefaultAction: firewall.DefaultPermitAndContinue, Rules: []firewall.Rule{rule},
		Scope: scope, ActorRef: "https://idp.example#admin-compile"})
	if err != nil {
		t.Fatal(err)
	}

	actor := security.Actor{Issuer: "https://idp.example", Subject: "reader-compile"}
	mapping := &security.RoleScopeMapping{Version: "v1", Digest: digest, DefaultDeny: true,
		ActorScopes: map[string][]security.Scope{actor.String(): {{ScopeID: scope, Levels: []security.AuthzContextLevel{security.LevelAnalyst}}}}}
	compiler := &firewallCompileFake{}
	handler := chi.NewRouter()
	handler.Get("/api/firewalls/revisions/{revisionID}/inspection", handleInspectFirewallRevision(Deps{
		Pool: pool, Mapping: mapping, FirewallCompiler: compiler,
	}))
	req := httptest.NewRequest(http.MethodGet, "/api/firewalls/revisions/"+revisionID+"/inspection", nil)
	req = req.WithContext(context.WithValue(req.Context(), sessionKey{}, &Session{Actor: actorClaims{
		Issuer: actor.Issuer, Subject: actor.Subject,
	}}))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("inspection status=%d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["physical_compile_status"] != "accepted" || body["physical_plan_digest"] == "" ||
		body["physical_entry_count"] != float64(1) || strings.Contains(rec.Body.String(), "secret-token") {
		t.Fatalf("compile projection malformed or leaked token: %s", rec.Body.String())
	}
	if compiler.intent.Payload.PolicyRevisionDigest != revision.RevisionDigest ||
		compiler.intent.Fence.TargetControlIncarnationID != "target-inc-compile" ||
		compiler.intent.AuthorizationDigest == "" || compiler.intent.EffectDigest == "" {
		t.Fatalf("compile intent not bound to canonical revision/fence: %+v", compiler.intent)
	}
}
