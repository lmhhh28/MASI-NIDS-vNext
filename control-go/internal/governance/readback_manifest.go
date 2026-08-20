package governance

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"

	"masi-nids/control-go/internal/db"
)

func ComputeReadbackManifestDigest(entries []AppliedRuleReadback) string {
	canonical := append([]AppliedRuleReadback(nil), entries...)
	sort.Slice(canonical, func(i, j int) bool {
		if canonical[i].EntityID != canonical[j].EntityID {
			return canonical[i].EntityID < canonical[j].EntityID
		}
		if canonical[i].RuleID != canonical[j].RuleID {
			return canonical[i].RuleID < canonical[j].RuleID
		}
		return canonical[i].CanonicalEntryDigest < canonical[j].CanonicalEntryDigest
	})
	raw, _ := json.Marshal(canonical)
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func validateAppliedReadbackManifest(intent Intent, result EdgeEffectResult) error {
	if intent.EffectKind == KindBoundedCapture {
		return validateBoundedCaptureReadback(intent, result)
	}
	if !validSHA256(result.ReadbackManifestDigest) ||
		result.ReadbackManifestDigest != ComputeReadbackManifestDigest(result.AppliedEntries) {
		return errors.New("dispatcher: Edge per-entry readback manifest digest mismatch")
	}
	if len(result.AppliedEntries) > 4096 ||
		(intent.Payload.Operation != "overlay-delete" && len(result.AppliedEntries) > result.ObservedEntries) {
		return errors.New("dispatcher: Edge per-entry readback manifest exceeds observed bound")
	}
	if intent.Payload.Operation == "overlay-delete" && (result.ExpectedEntries != 0 || result.ObservedEntries != 0) {
		return errors.New("dispatcher: overlay delete must prove exact absence")
	}
	allowedRules := map[string]bool{}
	var rules []struct {
		RuleID  string `json:"rule_id"`
		Enabled bool   `json:"enabled"`
	}
	if intent.Payload.Operation == "baseline-activate" {
		if err := json.Unmarshal(intent.Payload.BaselineRules, &rules); err != nil {
			return err
		}
	} else {
		if err := json.Unmarshal(intent.Payload.OverlayRules, &rules); err != nil {
			return err
		}
	}
	for _, rule := range rules {
		if rule.Enabled {
			allowedRules[rule.RuleID] = true
		}
	}
	seenRule := map[string]bool{}
	seenEntity := map[string]bool{}
	seenDigest := map[string]bool{}
	for _, entry := range result.AppliedEntries {
		if entry.EntityID == "" || len(entry.EntityID) > 128 || entry.RuleID == "" || len(entry.RuleID) > 128 ||
			!allowedRules[entry.RuleID] || !validSHA256(entry.CanonicalEntryDigest) ||
			!validSHA256(entry.MatchPriorityActionDigest) || entry.TableID == 0 || entry.DirectCounterID == 0 || entry.Bank > 1 {
			return errors.New("dispatcher: malformed/unbound applied rule readback entry")
		}
		key := entry.EntityID + "\x00" + entry.RuleID
		if seenEntity[key] || seenDigest[entry.CanonicalEntryDigest] {
			return errors.New("dispatcher: duplicate applied readback identity")
		}
		seenEntity[key] = true
		seenDigest[entry.CanonicalEntryDigest] = true
		seenRule[entry.RuleID] = true
	}
	for ruleID := range allowedRules {
		if !seenRule[ruleID] {
			return fmt.Errorf("dispatcher: applied readback missing logical rule %s", ruleID)
		}
	}
	return nil
}

func persistReadbackManifestTx(ctx context.Context, tx *db.Tx, attemptID string, intent *Intent, result EdgeEffectResult) error {
	if intent.EffectKind == KindBoundedCapture {
		return persistBoundedCaptureReadbackTx(ctx, tx, intent, result)
	}
	for index, entry := range result.AppliedEntries {
		if _, err := tx.Exec(ctx, `INSERT INTO effect_attempt_readback_entries(
			attempt_id,intent_id,operation_id,target_id,entry_index,entity_id,rule_id,
			canonical_entry_digest,match_priority_action_digest,table_id,direct_counter_id,bank)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`, attemptID, intent.EffectIntentID,
			intent.OperationID, intent.TargetID, index, entry.EntityID, entry.RuleID, entry.CanonicalEntryDigest,
			entry.MatchPriorityActionDigest, entry.TableID, entry.DirectCounterID, entry.Bank); err != nil {
			return err
		}
	}
	return nil
}

func validateBoundedCaptureReadback(intent Intent, result EdgeEffectResult) error {
	if intent.Payload.BoundedCapture == nil || result.BoundedCapture == nil || len(result.AppliedEntries) != 0 ||
		result.ReadbackManifestDigest != "" || result.ExpectedEntries != 0 || result.ObservedEntries != 0 ||
		result.MismatchedEntries != 0 {
		return errors.New("dispatcher: bounded capture returned firewall readback fields")
	}
	spec := intent.Payload.BoundedCapture
	readback := result.BoundedCapture
	if readback.SchemaVersion != "bounded-capture-readback/v1" || readback.CaptureID != spec.CaptureID ||
		readback.CaptureDigest != spec.CaptureDigest || readback.CaptureSessionID == "" || len(readback.CaptureSessionID) > 128 ||
		readback.StartedAtUnixMS < 1 || readback.FinishedAtUnixMS < readback.StartedAtUnixMS ||
		readback.FinishedAtUnixMS-readback.StartedAtUnixMS > int64(spec.DurationMS)+1000 ||
		readback.FinishedAtUnixMS > spec.ExpiresAtUnixMS || readback.ObservedSamples < 0 ||
		readback.ObservedSamples > spec.SampleLimit || readback.ObservedBytes < 0 || readback.ObservedBytes > spec.ByteLimit ||
		!validSHA256(readback.ContentDigest) || !validSHA256(readback.ObservedFlowDigest) ||
		!validSHA256(readback.CaptureWindowDigest) {
		return errors.New("dispatcher: bounded capture readback identity/window/resource bounds invalid")
	}
	if result.ReadbackDigest != ComputeCaptureReadbackDigest(*readback) {
		return errors.New("dispatcher: bounded capture readback digest mismatch")
	}
	return nil
}

func persistBoundedCaptureReadbackTx(ctx context.Context, tx *db.Tx, intent *Intent, result EdgeEffectResult) error {
	if result.Outcome != "applied" || result.BoundedCapture == nil {
		return nil
	}
	r := result.BoundedCapture
	evidenceID := "capture-evidence-" + shortID(r.CaptureID+":"+r.ContentDigest+":"+r.CaptureWindowDigest)
	if _, err := tx.Exec(ctx, `INSERT INTO bounded_capture_results(
		capture_id,effect_intent_id,operation_id,target_id,target_control_incarnation_id,
		target_assignment_generation,application_generation,actor_runtime_epoch,capture_digest,capture_session_id,
		started_at_unix_ms,finished_at_unix_ms,observed_samples,observed_bytes,content_digest,
		observed_flow_digest,capture_window_digest,readback_digest,result_digest,truncated,gap,evidence_id,trace_id)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)`,
		r.CaptureID, intent.EffectIntentID, intent.OperationID, intent.TargetID,
		intent.Fence.TargetControlIncarnationID, intent.Fence.TargetAssignmentGeneration,
		intent.Fence.ApplicationGeneration, intent.Fence.ActorRuntimeEpoch, r.CaptureDigest,
		r.CaptureSessionID, r.StartedAtUnixMS, r.FinishedAtUnixMS, r.ObservedSamples, r.ObservedBytes,
		r.ContentDigest, r.ObservedFlowDigest, r.CaptureWindowDigest, result.ReadbackDigest, result.ResultDigest,
		r.Truncated, r.Gap, evidenceID, intent.TraceID); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `INSERT INTO evidence_refs(evidence_id,kind,reference_digest,source,trace_id,scope)
		SELECT $1,'bounded-capture',$2,'edge-bounded-capture',$3,r.scope
		FROM bounded_capture_requests r WHERE r.capture_id=$4`, evidenceID, r.ContentDigest, intent.TraceID, r.CaptureID)
	return err
}
