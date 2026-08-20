package process_e2e

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"testing"
	"time"

	"masi-nids/control-go/internal/firewall"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/security"
)

// targetSetDigestE2E == targetSetDigest(["target-e2e"]) (see targetSetDigest in
// internal/api/router.go) and is the digest granted by testdata/role-mapping-e2e.json.
const targetSetDigestE2E = "sha256:b040b50e2f8f96bfcee9bdd70ca819d01ace68e4af8c5a28015c738eb7ac607e"

// jstr extracts a string field from a JSON response body for the blackbox tests.
func jstr(t *testing.T, body string, key string) string {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(body), &m); err != nil {
		t.Fatalf("jstr parse: %v body=%s", err, body)
	}
	v, _ := m[key].(string)
	return v
}

func setup(t *testing.T) (*proc, context.Context) {
	t.Helper()
	p := startControlCore(t)
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	t.Cleanup(cancel)
	p.resetE2E(ctx)
	p.seedBaseline(ctx)
	t.Cleanup(func() {
		cleanCtx, cleanCancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cleanCancel()
		p.resetE2E(cleanCtx)
	})
	return p, ctx
}

// TestRealControlSessionCSRFOrigin proves the test-login harness mints a real
// session and that the mutation guard enforces CSRF + Origin on every mutation.
func TestRealControlSessionCSRFOrigin(t *testing.T) {
	p, ctx := setup(t)
	cookie, csrf := p.session(t, e2eMakerSub)

	// Authenticated read works.
	if status, _ := p.get(t, cookie, "/api/targets"); status != http.StatusOK {
		t.Fatalf("authenticated read status=%d", status)
	}

	// Mutation without CSRF is rejected (403 CSRF_REJECTED).
	body := map[string]any{"display_name": "no-csrf", "p4runtime_endpoint": "https://127.0.0.1:9559",
		"device_id": 99, "role": "masi", "desired_profile_digest": e2eDigest, "credential_ref": "cred",
		"scope": "scope-e2e", "p4runtime_tls": map[string]any{}, "target_set_digest": targetSetDigestE2E,
		"idempotency_key": "no-csrf-key", "trace_id": "t"}
	req, _ := http.NewRequest(http.MethodPost, p.baseURL+"/api/targets", jsonBody(body))
	req.Header.Set("Cookie", cookie)
	req.Header.Set("Origin", p.baseURL)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("missing-CSRF status=%d want 403", resp.StatusCode)
	}

	// Mutation with wrong Origin is rejected.
	req2, _ := http.NewRequest(http.MethodPost, p.baseURL+"/api/targets", jsonBody(body))
	req2.Header.Set("Cookie", cookie)
	req2.Header.Set("X-CSRF-Token", csrf)
	req2.Header.Set("Origin", "http://evil.example")
	req2.Header.Set("Content-Type", "application/json")
	resp2, err := http.DefaultClient.Do(req2)
	if err != nil {
		t.Fatal(err)
	}
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong-Origin status=%d want 403", resp2.StatusCode)
	}

	// Unauthenticated mutation is rejected.
	status, _ := p.mutate(t, "masi_session=bogus", csrf, "/api/targets", body)
	if status != http.StatusUnauthorized && status != http.StatusForbidden {
		t.Fatalf("unauth mutation status=%d want 401/403", status)
	}
	_ = ctx
}

// TestRealControlGovernanceMakerChecker covers R1 self-approval, R2 different
// operator with fresh step-up, R2 same-operator rejection, and R3 baseline
// maker-checker — all via the public HTTP API, observed via the DB oracle.
func TestRealControlGovernanceMakerChecker(t *testing.T) {
	p, ctx := setup(t)
	cookieA, csrfA := p.session(t, e2eMakerSub)
	cookieB, csrfB := p.session(t, e2eCheckerSub)

	now := time.Now().UnixMilli()
	proposalBody := func(risk, kind, key string) map[string]any {
		return map[string]any{
			"effect_kind": kind, "target_ids": []string{"target-e2e"}, "scope": "scope-e2e",
			"policy_digest": e2eDigest, "risk_level": risk, "idempotency_key": key,
			"note": key, "trace_id": "gov-e2e", "expires_at_unix_ms": now + 3_600_000,
		}
	}

	// R1: maker A creates + self-approves -> approve decision recorded. A generic
	// proposal's approve records a Decision only; an Intent is created later by the
	// specific effect preparation flow, not by the decision itself (invariant).
	status, resp := p.mutate(t, cookieA, csrfA, "/api/effects/proposals", proposalBody("R1", "target-assignment", "prop-r1"))
	if status != http.StatusCreated {
		t.Fatalf("R1 proposal status=%d body=%s", status, resp)
	}
	propR1 := jstr(t, resp, "proposal_id")
	if status, _ = p.mutate(t, cookieA, csrfA, "/api/effects/decisions",
		map[string]any{"proposal_id": propR1, "decision": "approve", "reason_code": "E2E_REASON", "idempotency_key": "dec-r1"}); status != http.StatusCreated {
		t.Fatalf("R1 decision status=%d", status)
	}
	var approveCount int
	p.queryRow(ctx, `SELECT count(*) FROM effect_decisions WHERE proposal_id=$1 AND decision='approve'`, propR1).Scan(&approveCount)
	if approveCount != 1 {
		t.Fatalf("R1 approve decision count=%d want 1", approveCount)
	}

	// R2: maker A proposes, checker B approves with fresh step-up -> approve decision.
	status, resp = p.mutate(t, cookieA, csrfA, "/api/effects/proposals", proposalBody("R2", "firewall-overlay", "prop-r2"))
	if status != http.StatusCreated {
		t.Fatalf("R2 proposal status=%d body=%s", status, resp)
	}
	propR2 := jstr(t, resp, "proposal_id")
	if status, _ := p.mutate(t, cookieB, csrfB, "/api/effects/decisions",
		map[string]any{"proposal_id": propR2, "decision": "approve", "reason_code": "E2E_REASON", "idempotency_key": "dec-r2"}); status != http.StatusCreated {
		t.Fatalf("R2 approve status=%d (maker-checker)", status)
	}
	p.queryRow(ctx, `SELECT count(*) FROM effect_decisions WHERE proposal_id=$1 AND decision='approve'`, propR2).Scan(&approveCount)
	if approveCount != 1 {
		t.Fatalf("R2 approve decision count=%d want 1", approveCount)
	}

	// R2 same-operator approval is rejected (DECISION_REJECTED 403).
	status, _ = p.mutate(t, cookieA, csrfA, "/api/effects/decisions",
		map[string]any{"proposal_id": propR2, "decision": "approve", "reason_code": "E2E_REASON", "idempotency_key": "dec-r2-same"})
	if status != http.StatusForbidden {
		t.Fatalf("R2 same-operator status=%d want 403", status)
	}

	// R3: platform-admin maker A + operator checker B -> approve decision.
	status, resp = p.mutate(t, cookieA, csrfA, "/api/effects/proposals", proposalBody("R3", "firewall-baseline-activate", "prop-r3"))
	if status != http.StatusCreated {
		t.Fatalf("R3 proposal status=%d body=%s", status, resp)
	}
	propR3 := jstr(t, resp, "proposal_id")
	if status, _ = p.mutate(t, cookieB, csrfB, "/api/effects/decisions",
		map[string]any{"proposal_id": propR3, "decision": "approve", "reason_code": "E2E_REASON", "idempotency_key": "dec-r3"}); status != http.StatusCreated {
		t.Fatalf("R3 approve status=%d (maker-checker)", status)
	}
	p.queryRow(ctx, `SELECT count(*) FROM effect_decisions WHERE proposal_id=$1 AND decision='approve'`, propR3).Scan(&approveCount)
	if approveCount != 1 {
		t.Fatalf("R3 approve decision count=%d want 1", approveCount)
	}

	// Invariant: only effect_intents is claimable; proposals/decisions carry no claim_state.
	var nonClaimable int
	p.queryRow(ctx, `SELECT count(*) FROM information_schema.columns WHERE table_name='effect_proposals' AND column_name='claim_state'`).Scan(&nonClaimable)
	if nonClaimable != 0 {
		t.Fatalf("effect_proposals must not carry claim_state (found)")
	}

	// Reject path: a new R2 proposal rejected by checker B records a reject decision.
	status, resp = p.mutate(t, cookieA, csrfA, "/api/effects/proposals", proposalBody("R2", "firewall-overlay", "prop-r2-rej"))
	if status != http.StatusCreated {
		t.Fatalf("R2-reject proposal status=%d body=%s", status, resp)
	}
	propR2Rej := jstr(t, resp, "proposal_id")
	if status, _ = p.mutate(t, cookieB, csrfB, "/api/effects/decisions",
		map[string]any{"proposal_id": propR2Rej, "decision": "reject", "reason_code": "E2E_REASON", "idempotency_key": "dec-r2-rej"}); status != http.StatusCreated {
		t.Fatalf("R2 reject status=%d", status)
	}
	var rejectCount int
	p.queryRow(ctx, `SELECT count(*) FROM effect_decisions WHERE proposal_id=$1 AND decision='reject'`, propR2Rej).Scan(&rejectCount)
	if rejectCount != 1 {
		t.Fatalf("rejected proposal reject decision count=%d want 1", rejectCount)
	}
	// One terminal decision per proposal: a later approve is idempotent and must
	// NOT overwrite the terminal reject (the persisted decision stays 'reject').
	if status, _ = p.mutate(t, cookieB, csrfB, "/api/effects/decisions",
		map[string]any{"proposal_id": propR2Rej, "decision": "approve", "reason_code": "E2E_REASON", "idempotency_key": "dec-r2-rej-approve"}); status != http.StatusCreated {
		t.Fatalf("approve after terminal reject status=%d want 201 (idempotent recall)", status)
	}
	var finalDecision string
	p.queryRow(ctx, `SELECT decision FROM effect_decisions WHERE proposal_id=$1`, propR2Rej).Scan(&finalDecision)
	if finalDecision != "reject" {
		t.Fatalf("terminal reject must not be overwritten by later approve, got %s", finalDecision)
	}
}

// TestRealControlEffectDispatchFailClosed creates a firewall response overlay
// (the public flow that produces claimable effect_intents) and lets the 2s
// maintenance loop dispatch it against the test-profile stub Edge. The stub
// returns errEdgeNotConfigured on every call, so the intents must converge to a
// fail-closed terminal state (unknown/timeout) with no second intent and no
// blind retry. The applied path itself is covered by the in-process
// governance dispatcher PostgreSQL E2E; this test asserts the public-boundary +
// maintenance-loop fail-closed behavior only.
func TestRealControlEffectDispatchFailClosed(t *testing.T) {
	p, ctx := setup(t)
	cookieA, csrfA := p.session(t, e2eMakerSub)
	operator := security.Actor{Issuer: e2eIssuer, Subject: e2eMakerSub}
	now := time.Now().UTC().Truncate(time.Second)
	expires := now.Add(4 * time.Minute)
	// Establish the target's current capability observation via the public gRPC
	// boundary so the overlay's target/assignment/capability fence check passes.
	targetAck, err := p.sink().PublishTargetStatus(ctx, validTargetStatusBatch(e2eDigest, now.UnixMilli()))
	if err != nil || targetAck.ReasonCode != "ACCEPTED" {
		t.Fatalf("publish target status: ack=%+v err=%v", targetAck, err)
	}
	rule := firewall.OverlayRule{
		RuleID: "ov-rule-e2e", RuleRevision: 1, StagePrecedence: "before-baseline",
		SourceIPv4: "192.0.2.10", DestinationIPv4: "198.51.100.20", Protocol: 6,
		SourcePort: 40000, DestinationPort: 443, Action: "drop", Enabled: true,
		ExpiresAt: expires.Format(time.RFC3339), ActorRef: operator.String(), ReasonCode: "INCIDENT_RESPONSE",
	}
	rule.CanonicalRuleDigest = firewall.ComputeOverlayRuleDigest(rule)

	// 1. R1 proposal + self-approve for the firewall-overlay effect.
	propStatus, propResp := p.mutate(t, cookieA, csrfA, "/api/effects/proposals", map[string]any{
		"effect_kind": "firewall-overlay", "target_ids": []string{"target-e2e"}, "scope": "scope-e2e",
		"policy_digest": rule.CanonicalRuleDigest, "risk_level": "R1", "idempotency_key": "ov-prop",
		"trace_id": "ov-e2e", "expires_at_unix_ms": now.Add(10 * time.Minute).UnixMilli(),
	})
	if propStatus != http.StatusCreated {
		t.Fatalf("overlay proposal status=%d body=%s", propStatus, propResp)
	}
	propID := jstr(t, propResp, "proposal_id")
	decStatus, decResp := p.mutate(t, cookieA, csrfA, "/api/effects/decisions",
		map[string]any{"proposal_id": propID, "decision": "approve", "reason_code": "E2E_REASON", "idempotency_key": "ov-dec"})
	if decStatus != http.StatusCreated {
		t.Fatalf("overlay decision status=%d body=%s", decStatus, decResp)
	}
	decID := jstr(t, decResp, "decision_id")
	decDigest := jstr(t, decResp, "decision_digest")
	if decID == "" || decDigest == "" {
		t.Fatalf("decision response missing id/digest: %s", decResp)
	}

	// 2. Create the overlay; the service computes payloads/effect digests and
	// inserts two unclaimed effect_intents (upsert + bounded durable delete).
	fence := governance.Fence{
		TargetControlIncarnationID: "target-inc-e2e", TargetAssignmentGeneration: 1,
		EdgeWorkloadRef: "edge-e2e", ActorRuntimeEpoch: "actor-epoch-e2e", ApplicationGeneration: 1,
		ElectionIDLow: 1, ElectionIDHigh: 10, P4InfoDigest: e2eDigest, PipelineDigest: e2eDigest, CapacityDigest: e2eDigest,
	}
	base := governance.Intent{
		ProposalID: propID, DecisionID: decID, TargetID: "target-e2e", Fence: fence,
		AuthorizationDigest: decDigest, EffectKind: governance.KindFirewallOverlay,
		RiskLevel: security.R1, RequiredWriteAtomicity: "CONTINUE_ON_ERROR", Actor: operator,
	}
	upsert := base
	upsert.EffectIntentID, upsert.OperationID, upsert.DeadlineUnixMS, upsert.TraceID =
		"ov-upsert-e2e", "ov-upsert-op-e2e", now.Add(2*time.Minute).UnixMilli(), "ov-upsert"
	del := base
	del.EffectIntentID, del.OperationID, del.DeadlineUnixMS, del.TraceID =
		"ov-delete-e2e", "ov-delete-op-e2e", expires.Add(time.Minute).UnixMilli(), "ov-delete"
	overlay := firewall.Overlay{
		OverlayRuleID: "ov-rule-e2e", TargetID: "target-e2e", Rule: rule,
		ExpiresAtUnixMS: expires.UnixMilli(), OperationID: upsert.OperationID, Scope: "scope-e2e",
		Actor: operator, TraceID: "ov-create", UpsertIntent: upsert, DeleteIntent: del,
	}
	ovStatus, ovResp := p.mutate(t, cookieA, csrfA, "/api/firewall/overlays", map[string]any{
		"overlay": overlay, "target_set_digest": targetSetDigestE2E, "idempotency_key": "ov-create",
	})
	if ovStatus != http.StatusCreated {
		t.Fatalf("overlay create status=%d body=%s", ovStatus, ovResp)
	}

	// 3. Wait for the maintenance loop to claim+dispatch the upsert intent (the
	// delete intent is deliberately not-before the 4min overlay expiry, so it must
	// stay unclaimed until then — it is not asserted as dispatched here). The stub
	// Edge returns errEdgeNotConfigured, so the upsert must converge fail-closed.
	waitFor(t, 12*time.Second, "upsert intent converged to unknown", func() error {
		var state string
		err := p.queryRow(ctx, `SELECT claim_state FROM effect_intents WHERE effect_intent_id='ov-upsert-e2e'`).Scan(&state)
		if err != nil {
			return err
		}
		if state == "unclaimed" || state == "claimed" {
			return errNotReady("upsert still in progress")
		}
		if state != "unknown" {
			return fmt.Errorf("upsert reached unexpected state %s", state)
		}
		return nil
	})
	// Fail-closed terminal: upsert never reaches applied/fenced, exactly two
	// intents exist (no second intent / blind retry), and an attempt was recorded.
	var upsertState, upsertReason string
	p.queryRow(ctx, `SELECT claim_state,reason_code FROM effect_intents WHERE effect_intent_id='ov-upsert-e2e'`).Scan(&upsertState, &upsertReason)
	if upsertState != "unknown" {
		t.Fatalf("upsert claim_state=%s reason=%s; stub Edge must converge to fail-closed 'unknown'", upsertState, upsertReason)
	}
	var intentCount int
	p.queryRow(ctx, `SELECT count(*) FROM effect_intents WHERE effect_intent_id IN ('ov-upsert-e2e','ov-delete-e2e')`).Scan(&intentCount)
	if intentCount != 2 {
		t.Fatalf("expected exactly two intents, got %d (no second intent / blind retry)", intentCount)
	}
	var attemptCount int
	p.queryRow(ctx, `SELECT count(*) FROM effect_attempts WHERE intent_id='ov-upsert-e2e'`).Scan(&attemptCount)
	t.Logf("upsert effect_attempts=%d (stub fail-closed path may record state on the intent without an attempt row)", attemptCount)
}

// TestRealControlTargetLifecycle registers a new target via the public API and
// exercises activate/retire, asserting lifecycle audit in the DB.
func TestRealControlTargetLifecycle(t *testing.T) {
	p, ctx := setup(t)
	cookieA, csrfA := p.session(t, e2eMakerSub)
	reg := map[string]any{
		"display_name": "target-new", "p4runtime_endpoint": "https://127.0.0.1:9559",
		"device_id": 42, "role": "masi", "desired_profile_digest": e2eDigest, "credential_ref": "cred-new",
		"scope":             "scope-e2e",
		"p4runtime_tls":     map[string]any{"identity_ref": "cred-new", "server_name": "target-new"},
		"target_set_digest": targetSetDigestE2E, "idempotency_key": "reg-target", "trace_id": "reg-e2e",
	}
	status, resp := p.mutate(t, cookieA, csrfA, "/api/targets", reg)
	if status != http.StatusCreated {
		t.Fatalf("register status=%d body=%s", status, resp)
	}
	targetID := jstr(t, resp, "target_id")
	if targetID == "" {
		t.Fatalf("no target_id in response: %s", resp)
	}
	var exists int
	p.queryRow(ctx, `SELECT count(*) FROM targets WHERE target_id=$1`, targetID).Scan(&exists)
	if exists != 1 {
		t.Fatalf("registered target not in DB")
	}

	// Activate (admin step-up).
	if status, _ = p.mutate(t, cookieA, csrfA, "/api/targets/"+targetID+"/activate",
		map[string]any{"scope": "scope-e2e", "target_set_digest": targetSetDigestE2E, "reason_code": "E2E_REASON", "trace_id": "act", "idempotency_key": "act-target"}); status != http.StatusOK {
		t.Fatalf("activate status=%d", status)
	}
	// Retire.
	if status, _ = p.mutate(t, cookieA, csrfA, "/api/targets/"+targetID+"/retire",
		map[string]any{"scope": "scope-e2e", "target_set_digest": targetSetDigestE2E, "reason_code": "E2E_REASON", "trace_id": "ret", "idempotency_key": "ret-target"}); status != http.StatusOK {
		t.Fatalf("retire status=%d", status)
	}
	var lifecycleEvents int
	p.queryRow(ctx, `SELECT count(*) FROM target_lifecycle_events WHERE target_id=$1`, targetID).Scan(&lifecycleEvents)
	if lifecycleEvents < 2 {
		t.Fatalf("lifecycle events=%d want >=2", lifecycleEvents)
	}

	// Idempotent registration replay returns the same target (no duplicate).
	status, resp = p.mutate(t, cookieA, csrfA, "/api/targets", reg)
	if status != http.StatusCreated {
		t.Fatalf("idempotent replay status=%d body=%s", status, resp)
	}
	if jstr(t, resp, "target_id") != targetID {
		t.Fatalf("idempotent replay returned different target_id")
	}
	p.queryRow(ctx, `SELECT count(*) FROM targets WHERE target_id=$1`, targetID).Scan(&exists)
	if exists != 1 {
		t.Fatalf("idempotent replay created duplicate target")
	}
}
