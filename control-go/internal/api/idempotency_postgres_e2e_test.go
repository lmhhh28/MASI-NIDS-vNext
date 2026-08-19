package api

import (
	"context"
	"os"
	"testing"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

func TestMutationIdempotencyPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	if configPath == "" || os.Getenv("MASI_CONTROL_E2E_DSN") == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("API idempotency PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for API idempotency PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	actor := security.Actor{Issuer: "https://issuer.example", Subject: "api-idempotency-e2e"}
	const key = "api-idempotency-e2e"
	_, _ = pool.Exec(ctx, `DELETE FROM api_mutation_idempotency WHERE actor_issuer=$1 AND actor_subject=$2 AND idempotency_key=$3`, actor.Issuer, actor.Subject, key)
	defer pool.Exec(context.Background(), `DELETE FROM api_mutation_idempotency WHERE actor_issuer=$1 AND actor_subject=$2 AND idempotency_key=$3`, actor.Issuer, actor.Subject, key)

	parsedKey, digest, err := canonicalMutationIdentity([]byte(`{"idempotency_key":"api-idempotency-e2e","value":1}`))
	if err != nil || parsedKey != key {
		t.Fatalf("canonical mutation identity: key=%s err=%v", parsedKey, err)
	}
	disposition, _, err := beginMutation(ctx, pool, actor, key, digest)
	if err != nil || disposition != idempotencyExecute {
		t.Fatalf("first claim: disposition=%v err=%v", disposition, err)
	}
	disposition, _, err = beginMutation(ctx, pool, actor, key, digest)
	if err != nil || disposition != idempotencyInProgress {
		t.Fatalf("in-progress replay: disposition=%v err=%v", disposition, err)
	}
	body := []byte(`{"status":"created"}` + "\n")
	if err := completeMutation(ctx, pool, actor, key, digest, 201, body); err != nil {
		t.Fatal(err)
	}
	disposition, replay, err := beginMutation(ctx, pool, actor, key, digest)
	if err != nil || disposition != idempotencyReplay || replay.Status != 201 || string(replay.Body) != string(body) {
		t.Fatalf("completed replay: disposition=%v replay=%+v err=%v", disposition, replay, err)
	}
	_, otherDigest, _ := canonicalMutationIdentity([]byte(`{"value":2,"idempotency_key":"api-idempotency-e2e"}`))
	disposition, _, err = beginMutation(ctx, pool, actor, key, otherDigest)
	if err != nil || disposition != idempotencyConflict {
		t.Fatalf("same key/different request: disposition=%v err=%v", disposition, err)
	}
	if _, err := pool.Exec(ctx, `UPDATE api_mutation_idempotency SET completed_at=now()-interval '8 days'
	 WHERE actor_issuer=$1 AND actor_subject=$2 AND idempotency_key=$3`, actor.Issuer, actor.Subject, key); err != nil {
		t.Fatal(err)
	}
	deleted, err := SweepMutationIdempotency(ctx, pool, time.Now(), 7*24*time.Hour, 10)
	if err != nil || deleted < 1 {
		t.Fatalf("bounded retention: deleted=%d err=%v", deleted, err)
	}
}
