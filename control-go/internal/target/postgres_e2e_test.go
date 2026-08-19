package target

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

func TestTargetLifecycleAuditPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if dsn == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("target lifecycle PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for target lifecycle PostgreSQL E2E")
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

	actor := security.Actor{Issuer: "https://issuer.example", Subject: "target-admin-e2e"}
	const idempotencyKey = "target-register-idempotency-e2e"
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		var targetIDs []string
		rows, err := pool.Query(cleanCtx, `SELECT target_id FROM targets WHERE actor_issuer=$1 AND actor_subject=$2 AND idempotency_key=$3`, actor.Issuer, actor.Subject, idempotencyKey)
		if err == nil {
			for rows.Next() {
				var id string
				_ = rows.Scan(&id)
				targetIDs = append(targetIDs, id)
			}
			rows.Close()
		}
		for _, targetID := range targetIDs {
			_, _ = pool.Exec(cleanCtx, `DELETE FROM target_lifecycle_events WHERE target_id=$1`, targetID)
			_, _ = pool.Exec(cleanCtx, `DELETE FROM targets WHERE target_id=$1`, targetID)
		}
	}
	cleanup()
	defer cleanup()

	digest := "sha256:" + strings.Repeat("b", 64)
	target := Target{DisplayName: "target lifecycle e2e", P4RuntimeEndpoint: "https://127.0.0.1:9560",
		DeviceID: 42, Role: "primary", DesiredProfileDigest: digest, CredentialRef: "credential-target-e2e",
		TLS:   TLSIdentity{ServerName: "target-e2e.example", IdentityRef: "credential-target-e2e"},
		Scope: "scope-target-lifecycle-e2e", Actor: actor, TraceID: "trace-target-register-e2e",
		IdempotencyKey: idempotencyKey}
	auth := LifecycleAuthorization{Scope: target.Scope, PlatformAdmin: true, StepUpFresh: true, CSRFVerified: true}
	service := NewRegistryService(pool)
	registered, err := service.RegisterTarget(ctx, target, auth)
	if err != nil {
		t.Fatal(err)
	}
	replayed, err := service.RegisterTarget(ctx, target, auth)
	if err != nil || replayed.TargetID != registered.TargetID {
		t.Fatalf("idempotent target replay=%+v err=%v", replayed, err)
	}
	changed := target
	changed.DisplayName = "changed target identity"
	if _, err := service.RegisterTarget(ctx, changed, auth); err == nil || !strings.Contains(err.Error(), "idempotency key reused") {
		t.Fatalf("changed registration must conflict, got %v", err)
	}
	if err := service.Activate(ctx, registered.TargetID, actor, auth, "TARGET_ACTIVATED", "trace-target-activate-e2e"); err != nil {
		t.Fatal(err)
	}
	if err := service.Retire(ctx, registered.TargetID, actor, auth, "TARGET_RETIRED", "trace-target-retire-e2e"); err != nil {
		t.Fatal(err)
	}
	if err := service.Activate(ctx, registered.TargetID, actor, auth, "TARGET_REACTIVATE", "trace-target-reactivate-e2e"); err == nil {
		t.Fatal("retired target must never reactivate")
	}
	var status string
	var events, wrongActor int
	if err := pool.QueryRow(ctx, `SELECT status FROM targets WHERE target_id=$1`, registered.TargetID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*),count(*) FILTER(WHERE actor_issuer<>$2 OR actor_subject<>$3)
		FROM target_lifecycle_events WHERE target_id=$1`, registered.TargetID, actor.Issuer, actor.Subject).
		Scan(&events, &wrongActor); err != nil {
		t.Fatal(err)
	}
	if status != string(StatusRetired) || events != 3 || wrongActor != 0 {
		t.Fatalf("target lifecycle status/events/actor=%s/%d/%d", status, events, wrongActor)
	}
}

// TestCapabilityObservationPostgres exercises the append-only history/current
// CAS against a real PostgreSQL 18 test database. It is opt-in for ordinary unit
// runs and mandatory when MASI_CONTROL_E2E_REQUIRED=1.
func TestCapabilityObservationPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	dsn := os.Getenv("MASI_CONTROL_E2E_DSN")
	if dsn == "" || configPath == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("target observation PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for target observation PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PostgreSQLDSN != dsn {
		t.Fatal("target observation PostgreSQL E2E DSN differs from validated config")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()

	const targetID = "target-observation-e2e"
	cleanup := func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanCancel()
		for _, statement := range []string{
			`DELETE FROM target_capability_observation_events WHERE target_id=$1`,
			`DELETE FROM target_capability_observations WHERE target_id=$1`,
			`DELETE FROM target_assignments WHERE target_id=$1`,
			`DELETE FROM targets WHERE target_id=$1`,
		} {
			_, _ = pool.Exec(cleanCtx, statement, targetID)
		}
	}
	cleanup()
	defer cleanup()

	d := "sha256:" + strings.Repeat("a", 64)
	if _, err := pool.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,role,status,
		desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
			VALUES($1,'target observation e2e','https://127.0.0.1:9562',100,'masi','active',$2,'cred-e2e','scope-e2e','actor-e2e','trace-seed')`,
		targetID, d); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,
		edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
		actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES($1,1,'inc-e2e','edge-e2e','lease-e2e-1',1000,301000,1,10,'actor-epoch-1',1,
		'actor-e2e','trace-seed','https://issuer.example','admin-e2e')`, targetID); err != nil {
		t.Fatal(err)
	}

	observation := CapabilityObservation{
		ObservationID: "obs-e2e-1", TargetID: targetID, TargetControlIncarnationID: "inc-e2e",
		AssignmentGeneration: 1, ActorRuntimeEpoch: "actor-epoch-1", ApplicationGeneration: 1,
		P4InfoDigest: d, PipelineDigest: d, ProfileDigest: d, CapacityDigest: d,
		CapacityAvailable: true, LeaseValid: true, P4Connected: true, Primary: true, PipelineExact: true,
		Freshness: "fresh", ReasonCode: "READY", ObservedAtUnixMS: 2000, ExpiresAtUnixMS: 3000,
		TraceID: "trace-observation-e2e",
	}
	observation.ObservationDigest = ComputeCapabilityObservationDigest(observation)
	service := NewRegistryService(pool)
	results, err := service.RecordCapabilityObservations(ctx, []CapabilityObservation{observation})
	if err != nil || len(results) != 1 || !results[0].Accepted || results[0].Idempotent {
		t.Fatalf("first observation result=%+v err=%v", results, err)
	}
	results, err = service.RecordCapabilityObservations(ctx, []CapabilityObservation{observation})
	if err != nil || len(results) != 1 || !results[0].Accepted || !results[0].Idempotent {
		t.Fatalf("idempotent replay result=%+v err=%v", results, err)
	}

	conflict := observation
	conflict.ReasonCode = "DRIFT"
	conflict.ObservationDigest = ComputeCapabilityObservationDigest(conflict)
	if _, err := service.RecordCapabilityObservations(ctx, []CapabilityObservation{conflict}); err == nil {
		t.Fatal("same observation_id with different digest must conflict")
	}

	if _, err := pool.Exec(ctx, `UPDATE target_assignments SET revoked_at_unix_ms=2500 WHERE target_id=$1 AND assignment_generation=1`, targetID); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,
		edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
		actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES($1,2,'inc-e2e','edge-e2e','lease-e2e-2',3000,303000,11,20,'actor-epoch-2',2,
		'actor-e2e','trace-seed-2','https://issuer.example','admin-e2e')`, targetID); err != nil {
		t.Fatal(err)
	}
	stale := observation
	stale.ObservationID = "obs-e2e-stale"
	stale.ObservedAtUnixMS = 4000
	stale.ExpiresAtUnixMS = 5000
	stale.ObservationDigest = ComputeCapabilityObservationDigest(stale)
	results, err = service.RecordCapabilityObservations(ctx, []CapabilityObservation{stale})
	if err != nil || len(results) != 1 || results[0].Accepted || results[0].ReasonCode != "ASSIGNMENT_FENCE_MISMATCH" {
		t.Fatalf("stale assignment result=%+v err=%v", results, err)
	}

	var historyCount int
	var currentObservation string
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM target_capability_observation_events WHERE target_id=$1`, targetID).Scan(&historyCount); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT observation_id FROM target_capability_observations WHERE target_id=$1`, targetID).Scan(&currentObservation); err != nil {
		t.Fatal(err)
	}
	if historyCount != 1 || currentObservation != observation.ObservationID {
		t.Fatalf("history=%d current=%s, want 1/%s", historyCount, currentObservation, observation.ObservationID)
	}
}
