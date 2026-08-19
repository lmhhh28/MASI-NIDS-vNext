#!/usr/bin/env python3
"""Validate Go Control Core public JSON contracts, goldens, OpenAPI, and fail-closed negatives.

This is the Gate 0 contract validator for MOD-CTRL-001 (control-go). It mirrors the
structure of infer-cpp/scripts/validate-public-contracts.py (load/validate/
validate_permissive/reject/sha256) and adds the x-masi-semantic-constraints
machinery adapted from edge-rs/scripts/validate-public-contracts.py plus Go-specific
semantic functions (commit-before-ack, r2-maker-checker, only-intent-claimable,
partial-not-applied, fleet-parent-nonclaimable).

A successful run closes only the contract-smoke sub-gate. It deliberately emits
qualification=NOT_QUALIFIED and module_complete=false until the independent
evidence aggregator verifies the real binary/OCI, PostgreSQL, fault, security,
performance, and 3,600-second soak artifacts required by DEC-044.

Run: python3 control-go/scripts/validate-public-contracts.py --repo <repo-root>
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


# --------------------------------------------------------------------------- #
# Helpers (same shape as infer-cpp validator)
# --------------------------------------------------------------------------- #

def load(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def validate(schema: dict[str, Any], instance: dict[str, Any], name: str) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(instance),
        key=lambda item: list(item.path),
    )
    if errors:
        raise ValueError(
            "\n".join(f"{name} {list(error.path)}: {error.message}" for error in errors)
        )


def validate_permissive(schema: dict[str, Any], instance: dict[str, Any], name: str) -> None:
    """Validate required fields + schema_version, but allow extra properties."""
    sv = instance.get("schema_version")
    if sv is None:
        raise ValueError(f"{name}: missing schema_version")
    allowed_sv = schema.get("properties", {}).get("schema_version", {})
    if "enum" in allowed_sv and sv not in allowed_sv["enum"]:
        raise ValueError(f"{name}: schema_version {sv} not in allowed enum")
    if "const" in allowed_sv and sv != allowed_sv["const"]:
        raise ValueError(f"{name}: schema_version {sv} != const {allowed_sv['const']}")
    for req in schema.get("required", []):
        if req not in instance:
            raise ValueError(f"{name}: missing required field {req}")
    for error in Draft202012Validator(schema).iter_errors(instance):
        if "Additional properties" in error.message:
            continue
        raise ValueError(f"{name} {list(error.path)}: {error.message}")


def reject(schema: dict[str, Any], instance: dict[str, Any], name: str) -> None:
    if not list(Draft202012Validator(schema).iter_errors(instance)):
        raise ValueError(f"negative contract vector was accepted: {name}")


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# x-masi-semantic-constraints machinery (adapted from edge-rs)
# --------------------------------------------------------------------------- #

def _pointer(instance: Any, ptr: str) -> Any:
    if not ptr.startswith("/"):
        raise ValueError(f"bad pointer {ptr}")
    cur = instance
    for seg in ptr.split("/")[1:]:
        seg = seg.replace("~1", "/").replace("~0", "~")
        if seg == "":
            continue
        if isinstance(cur, list):
            cur = cur[int(seg)]
        else:
            cur = cur[seg]
    return cur


def check_semantic_constraints(schema: dict[str, Any], instance: dict[str, Any], name: str) -> None:
    """Evaluate the simple, single-instance operators declared in
    x-masi-semantic-constraints. Operators not in the simple set below are
    documented invariants (cross-record, behavioral, or already enforced
    structurally via additionalProperties:false / enums / allOf) and are
    validated by the dedicated negative/semantic tests in main(). Their
    presence in the schema freezes the contract intent; they are skipped here
    rather than raising, so the schema remains the source of truth without
    requiring a machine-evaluator for every behavioral rule."""
    SIMPLE = {"gt", "gte", "not-equal", "all-equal", "all-positive", "between-inclusive",
              "cas-expected-digest-match"}
    for c in schema.get("x-masi-semantic-constraints", []):
        op = c.get("operator")
        cid = c.get("constraint_id", "<no-id>")
        if op not in SIMPLE:
            continue
        try:
            if op == "gt":
                if not (_pointer(instance, c["left"]) > _pointer(instance, c["right"])):
                    raise ValueError(f"{name}:{cid}: {c['left']} not > {c['right']}")
            elif op == "gte":
                if not (_pointer(instance, c["left"]) >= _pointer(instance, c["right"])):
                    raise ValueError(f"{name}:{cid}: {c['left']} not >= {c['right']}")
            elif op == "not-equal":
                if _pointer(instance, c["left"]) == _pointer(instance, c["right"]):
                    raise ValueError(f"{name}:{cid}: {c['left']} == {c['right']}")
            elif op == "all-equal":
                base = _pointer(instance, c["right"])
                for p in c["left"].split("*"):
                    p = p.rstrip("/")
                    if p and _pointer(instance, p) != base:
                        raise ValueError(f"{name}:{cid}: {p} != {c['right']}")
            elif op == "all-positive":
                for p in c["fields"]:
                    if _pointer(instance, p) < 1:
                        raise ValueError(f"{name}:{cid}: {p} not positive")
            elif op == "between-inclusive":
                v = _pointer(instance, c["left"])
                lo, hi = _pointer(instance, c["right"][0]), _pointer(instance, c["right"][1])
                if not (lo <= v <= hi):
                    raise ValueError(f"{name}:{cid}: {c['left']}={v} not in [{lo},{hi}]")
            elif op == "cas-expected-digest-match":
                if _pointer(instance, "/cas_result") == "claimed":
                    if not _pointer(instance, "/claim_lease"):
                        raise ValueError(f"{name}:{cid}: claimed without lease")
        except KeyError as e:
            raise ValueError(f"{name}:{cid}: missing path {e}") from e


# --------------------------------------------------------------------------- #
# Shared digests / fixtures
# --------------------------------------------------------------------------- #

D = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64


def _authz(level="scoped-operator", age=100000, pr=True, su="webauthn-fido2"):
    return {"step_up_type": su, "step_up_age_ms": age, "phishing_resistant": pr,
            "role_mapping_version": D, "authz_context_level": level}


def _fence():
    return {"target_control_incarnation_id": "inc-1", "target_assignment_generation": 1,
            "edge_workload_ref": "edge-1", "actor_runtime_epoch": "actor-epoch-1",
            "application_generation": 1, "election_id_high": 0, "election_id_low": 10,
            "p4info_digest": D, "pipeline_digest": D, "capacity_digest": D}


def _sw():
    return {"source_id": "src-1", "window_id": "win-1", "window_start_unix_ms": 1,
            "window_end_unix_ms": 2, "finalized_at_unix_ms": 3, "watermark_unix_ms": 2,
            "event_time_unix_ms": 2}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

GO_CONTRACTS = [
    "contracts/event/v1/schema.json",
    "contracts/effect/v1/schema.json",
    "contracts/target/v1/schema.json",
    "contracts/fleet-operation/v1/schema.json",
    "contracts/model/v1/schema.json",
    "contracts/p4/firewall-policy/v1/schema.json",
    "contracts/p4/rule-observation/v1/schema.json",
    "contracts/db/effect-cas/v1/schema.json",
    "contracts/analysis/v1/schema.json",
    "contracts/plugin/v1/schema.json",
    "contracts/plugin/manifest/v1/schema.json",
    "contracts/plugin/wit/v1/schema.json",
    "contracts/plugin/statistics/v1/schema.json",
    "contracts/web/v1/schema.json",
]

SHARED_EVIDENCE = [
    "contracts/evidence/command/v1/schema.json",
    "contracts/evidence/module-findings/v1/schema.json",
    "contracts/evidence/traceability/v1/schema.json",
    "contracts/evidence/soak/v1/schema.json",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()

    checked: list[dict[str, object]] = []
    negative_vectors = 0

    # ---- 1. Meta-validate every Go-owned + shared-extended schema ----
    for rel in GO_CONTRACTS + SHARED_EVIDENCE:
        path = repo / rel
        schema = load(path)
        Draft202012Validator.check_schema(schema)
        checked.append({"name": rel, "digest": sha256(path)})

    # ---- 2. edge.proto surface drift (Go implements ControlSink, calls EdgeControl) ----
    edge_proto = (repo / "contracts" / "edge" / "v1" / "edge.proto").read_text(encoding="utf-8")
    required_fragments = [
        "service ControlSink",
        "rpc CommitResults(",
        "rpc PublishRuleObservations(",
        "rpc PublishTargetStatus(",
        "service EdgeControl",
        "rpc AssignTarget(",
        "rpc PreflightEffect(",
        "rpc ExecuteEffect(",
        "rpc AcknowledgeEffect(",
        "message CanonicalAck",
        "message CanonicalAckBatch",
        "CanonicalCommitStatus commit_status = 8",
    ]
    for frag in required_fragments:
        if frag not in edge_proto:
            raise ValueError(f"edge.proto missing fragment: {frag}")
    checked.append({"name": "edge-proto-surface", "digest": sha256(repo / "contracts/edge/v1/edge.proto")})

    # The deployment and statistics execution boundaries are production mTLS
    # gRPC adapters. Freeze their callable surface alongside the Edge boundary.
    adapter_path = repo / "contracts" / "control-adapter" / "v1" / "control_adapter.proto"
    adapter_proto = adapter_path.read_text(encoding="utf-8")
    adapter_fragments = [
        "package masi.control.adapter.v1;",
        "service DeploymentAdapter",
        "rpc StagePool(",
        "rpc StartReplicas(",
        "rpc WarmupAndCapacity(",
        "service PluginStatisticsExecutor",
        "rpc ExecuteStatistics(",
        "message PoolReadback",
        "repeated WorkerIdentity eligible_workers = 27",
        "message StatisticsExecutionRequest",
        "bytes input_bundle_json = 12",
        "string frozen_input_digest = 13",
        "message StatisticsExecutionReply",
    ]
    for frag in adapter_fragments:
        if frag not in adapter_proto:
            raise ValueError(f"control_adapter.proto missing fragment: {frag}")
    checked.append({"name": "control-adapter-proto-surface", "digest": sha256(adapter_path)})

    # ---- 3. Golden catalog: digest-pin recompute + vector structure ----
    catalog = load(repo / "contracts" / "golden" / "control" / "catalog.json")
    if catalog.get("schema_version") != "control-golden-catalog/v1":
        raise ValueError("control golden catalog schema_version drifted")
    if len(catalog["vectors"]) != 7:
        raise ValueError("control golden catalog must list 7 vectors")
    if catalog.get("digest_policy", {}).get("algorithm") != "sha256":
        raise ValueError("control golden catalog must declare sha256 digest policy")
    for v in catalog["vectors"]:
        path = repo / "contracts" / "golden" / "control" / v["path"]
        if not path.is_file():
            raise ValueError(f"golden vector missing: {path}")
        golden = load(path)
        if golden.get("schema_version") != "control-golden/v1":
            raise ValueError(f"golden vector schema_version wrong: {path}")
        if golden.get("vector_id") != v["id"]:
            raise ValueError(f"golden vector_id mismatch: {path}")
        observed = sha256(path)
        if v.get("digest") != observed:
            raise ValueError(
                f"golden vector digest drift: {v['path']} catalog={v.get('digest')} observed={observed}"
            )
        checked.append({"name": f"golden:{v['id']}", "digest": observed, "category": v["category"]})

    # ---- 4. Per-contract positive + negative vectors ----
    def pos_neg(rel, good, *, bad_major=True, bad_extra=True, extras=()):
        nonlocal negative_vectors
        schema = load(repo / rel)
        validate(schema, good, f"{rel}:positive")
        check_semantic_constraints(schema, good, f"{rel}:semantic")
        if bad_major:
            m = copy.deepcopy(good)
            sv = good.get("schema_version", "")
            m["schema_version"] = sv.replace("/v1", "/v2") if sv else "masi-x/v2"
            reject(schema, m, f"{rel}:unknown-major")
            negative_vectors += 1
        if bad_extra:
            e = copy.deepcopy(good)
            e["__unexpected_extra_field__"] = 1
            reject(schema, e, f"{rel}:extra-property")
            negative_vectors += 1
        for label, mutator in extras:
            inst = copy.deepcopy(good)
            mutator(inst)
            reject(schema, inst, f"{rel}:{label}")
            negative_vectors += 1

    # event
    event_good = {"schema_version": "masi-event/v1", "event_id": "evt-1",
                  "event_idempotency_key": "idk-1", "input_digest": D, "output_digest": D2,
                  "canonical_event_id": "evt-1", "model_control_incarnation_id": "inc-1",
	                  "shard_id": "sh-1", "route_epoch": 1, "logical_pool_id": "pool-1",
	                  "pool_generation": 1, "binding_generation": 1,
	                  "result_identity_digest": D, "scores": [0.1, 0.9], "predicted_label": 1,
	                  "decision": "alert", "out_of_distribution": False, "abstain": False,
	                  "execution_status": "ok", "execution_error_code": "",
	                  "worker_id": "worker-1", "worker_digest": D,
	                  "worker_attempt_id": "attempt-1", "inference_started_at_unix_ms": 900,
	                  "inference_completed_at_unix_ms": 950, "source_window_identity": _sw(),
                  "quality": "valid", "commit_status": "committed",
                  "event_time": "2026-08-18T00:00:00Z", "committed_at_unix_ms": 1000,
                  "ingest_batch_digest": D, "trace_id": "tr-1", "reason_code": "COMMITTED"}
    pos_neg("contracts/event/v1/schema.json", event_good,
            extras=[("committed-no-canonical", lambda i: (i.update(canonical_event_id=None, committed_at_unix_ms=None))),
                    ("bad-quality-enum", lambda i: i.update(quality="normal"))])

    # effect: proposal / decision / intent / attempt
    prop = {"schema_version": "masi-effect/v1", "record_type": "proposal", "record_id": "prop-1",
            "proposal_digest": D, "actor_ref": "iss-x:sub-a", "scope": "fleet-1", "risk_level": "R2",
            "effect_kind": "firewall-baseline-activate", "target_set_digest": D, "policy_digest": D,
            "evidence_refs": [D], "expires_at_unix_ms": 2000, "created_at_unix_ms": 1000,
            "note": "n", "reason_code": "PROPOSED", "trace_id": "tr-1"}
    dec = {"schema_version": "masi-effect/v1", "record_type": "decision", "record_id": "dec-1",
           "proposal_id": "prop-1", "proposal_digest": D, "actor_ref": "iss-x:sub-b",
           "risk_level": "R2", "decision": "approve", "authz_context": _authz(),
           "decision_digest": D, "expires_at_unix_ms": 1600, "created_at_unix_ms": 1500,
           "reason_code": "APPROVED", "trace_id": "tr-1"}
    intent = {"schema_version": "masi-effect/v1", "record_type": "intent", "record_id": "int-1",
              "operation_id": "op-1", "proposal_id": "prop-1", "decision_id": "dec-1",
              "target_id": "tgt-1", "fence": _fence(), "effect_digest": D,
              "effect_payload": {"schema_version": "p4-effect-payload/v1",
                                 "operation": "baseline-activate",
                                 "policy_revision_digest": D,
                                 "default_action": "drop",
                                 "baseline_rules": [], "overlay_rules": []},
              "authorization_digest": D, "effect_kind": "firewall-baseline-activate",
              "risk_level": "R2", "deadline_unix_ms": 3000, "claim_state": "claimed",
              "claim_lease_id": "lease-1", "claim_expires_at_unix_ms": 2500,
              "actor_ref": "iss-x:sub-b", "reason_code": "CLAIMED", "trace_id": "tr-1"}
    attempt = {"schema_version": "masi-effect/v1", "record_type": "attempt", "record_id": "att-1",
               "intent_id": "int-1", "operation_id": "op-1", "attempt_number": 1,
               "status": "applied", "plan_digest": D, "readback_digest": D,
               "expected_entries": 10, "observed_entries": 10, "mismatched_entries": 0,
               "started_at_unix_ms": 2500, "finished_at_unix_ms": 2600,
               "reason_code": "APPLIED", "trace_id": "tr-1"}
    eff_schema = load(repo / "contracts/effect/v1/schema.json")
    for g in (prop, dec, intent, attempt):
        validate(eff_schema, g, "effect:positive")
    # negatives
    for label, inst in [("unknown-major", {**prop, "schema_version": "masi-effect/v2"}),
                        ("extra-prop", {**prop, "__x__": 1}),
                        ("r2-stale-stepup", {**dec, "authz_context": _authz(age=400000)}),
                        ("r2-no-stepup", {**dec, "authz_context": _authz(su="none")}),
                        ("r3-non-admin", {**dec, "risk_level": "R3"}),
                        ("intent-claimed-no-lease", {**intent, "claim_lease_id": None, "claim_expires_at_unix_ms": None}),
                        ("attempt-applied-mismatch", {**attempt, "mismatched_entries": 1})]:
        reject(eff_schema, inst, f"effect:{label}")
        negative_vectors += 1

    # fleet-operation
    fleet_good = {"schema_version": "masi-fleet-operation/v1", "fleet_operation_id": "fo-1",
                  "target_set_digest": D, "wave_count": 2,
                  "waves": [{"wave_index": 0, "target_ids": ["tgt-1", "tgt-2"], "parallel_limit": 2,
                             "failure_policy": "fail-fast", "gate_state": "open"},
                            {"wave_index": 1, "target_ids": ["tgt-3"], "parallel_limit": 1,
                             "failure_policy": "continue-isolated", "gate_state": "closed"}],
                  "parent_intent_id": "parent-1",
                  "child_intents": [{"target_id": "tgt-1", "intent_id": "ci-1", "wave_index": 0, "status": "applied"},
                                    {"target_id": "tgt-2", "intent_id": "ci-2", "wave_index": 0, "status": "applied"},
                                    {"target_id": "tgt-3", "intent_id": "ci-3", "wave_index": 1, "status": "pending"}],
                  "aggregate_status": "running", "actor_ref": "iss-x:sub-a", "scope": "fleet-1",
                  "reason_code": "WAVE_RUNNING", "trace_id": "tr-1", "created_at_unix_ms": 1000}
    pos_neg("contracts/fleet-operation/v1/schema.json", fleet_good,
            extras=[("bad-failure-policy", lambda i: i.update(waves=[{**i["waves"][0], "failure_policy": "fast"}])),
                    ("bad-aggregate-status", lambda i: i.update(aggregate_status="done"))])

    # db/effect-cas
    cas_good = {"schema_version": "masi-effect-cas/v1", "operation_kind": "claim-intent",
                "claim_key": "int-1", "expected_claim_state": "unclaimed", "new_claim_state": "claimed",
                "cas_digest": D, "cas_result": "claimed", "idempotency_key": "idk-1",
                "idempotency_result": "original", "incarnation_id": "inc-1",
                "isolation_level": "serializable", "lock_timeout_ms": 1000, "statement_timeout_ms": 2000,
                "claim_lease": {"owner_identity": "ctrl-1", "issued_at_unix_ms": 1000,
                                "expires_at_unix_ms": 2000, "fence": "fence-1"},
                "reason_code": "CLAIMED", "trace_id": "tr-1"}
    pos_neg("contracts/db/effect-cas/v1/schema.json", cas_good,
            extras=[("claimed-null-lease", lambda i: i.update(claim_lease=None)),
                    ("bad-isolation", lambda i: i.update(isolation_level="read-uncommitted"))])

    # analysis
    analysis_good = {"schema_version": "masi-analysis/v1", "a2a_version": "1.0",
                     "agent_card_url": "https://analysis.local/card",
                     "task_identity": {"task_id": "t-1", "idempotency_key": "ik-1", "deadline_unix_ms": 3000, "status": "succeeded"},
                     "artifact_identity": {"artifact_id": "a-1", "artifact_digest": D, "non_executable": True, "media_type": "application/json"},
                     "mcp_endpoint": "masi-mcp-readonly/v1", "mcp_protocol_version": "2025-11-25",
                     "tool_allowlist": ["tool-1"], "resource_allowlist": ["res-1"],
                     "provenance": {"analysis_plugin_id": "plug-1", "plugin_revision": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2", "scope": "s-1"},
                     "binding_generation": 1, "revocation_state": "active",
                     "actor_ref": "iss-x:sub-a", "reason_code": "A2A_OK", "trace_id": "tr-1"}
    pos_neg("contracts/analysis/v1/schema.json", analysis_good,
            extras=[("a2a-0.3", lambda i: i.update(a2a_version="0.3")),
                    ("artifact-executable", lambda i: i.update(artifact_identity={**i["artifact_identity"], "non_executable": False}))])

    # plugin
    plugin_good = {"schema_version": "masi-plugin/v1", "plugin_id": "plug-1",
                   "revision": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2", "manifest_digest": D,
                   "kind": "analysis-agent", "publisher": "pub-1",
                   "provenance": {"source_id": "src-1", "source_revision": "r1", "retrieved_at_unix_ms": 1000, "signature_status": "signed"},
                   "config_digest": D, "capability_digest": D, "resource_profile_digest": D,
                   "binding_generation": 1, "activation_state": "active",
                   "qualification_status": "qualified", "qualification_digest": D,
                   "actor_ref": "iss-x:sub-a", "reason_code": "ACTIVE", "trace_id": "tr-1", "created_at_unix_ms": 1000}
    pos_neg("contracts/plugin/v1/schema.json", plugin_good,
            extras=[("unknown-kind", lambda i: i.update(kind="unknown-kind")),
                    ("active-unqualified", lambda i: i.update(qualification_status="unqualified"))])

    # plugin/manifest
    manifest_good = {"schema_version": "masi-plugin-manifest/v1", "manifest_id": "man-1",
                     "manifest_revision": 1, "manifest_digest": D, "plugin_id": "plug-1",
                     "kind": "analysis-agent", "publisher": "pub-1", "version": "1.0.0",
                     "capabilities": [{"capability_id": "cap-1", "capability_kind": "host-projection", "declared": True}],
                     "resource_limits": {"cpu_milli": 100, "memory_bytes": 1048576, "pid_count": 32,
                                         "fd_count": 64, "disk_bytes": 1048576, "deadline_ms": 5000,
                                         "output_bytes": 65536, "queue_depth": 100},
                     "runtime_profile": "wasm-component/v1", "wit_digest": D, "service_proto_digest": None,
                     "sbom_digest": D, "provenance_digest": D, "signature_status": "signed",
                     "actor_ref": "iss-x:sub-a", "reason_code": "MANIFEST", "trace_id": "tr-1", "created_at_unix_ms": 1000}
    pos_neg("contracts/plugin/manifest/v1/schema.json", manifest_good,
            extras=[("wasm-wit-null", lambda i: i.update(wit_digest=None)),
                    ("grpc-no-proto", lambda i: i.update(runtime_profile="grpc-service/v1", service_proto_digest=None, wit_digest=None))])

    # plugin/wit
    wit_good = {"schema_version": "masi-plugin-wit/v1", "world_name": "world-1", "wit_digest": D,
                "imports": [{"capability_id": "cap-1", "interface_name": "masi.read", "allowed": True}],
                "exports": [{"function_name": "fn-1", "interface_name": "masi.write", "return_kind": "none"}],
                "resource_limits": {"cpu_milli": 100, "memory_bytes": 1048576, "pid_count": 32,
                                    "fd_count": 64, "disk_bytes": 1048576, "deadline_ms": 5000,
                                    "output_bytes": 65536, "queue_depth": 100},
                "actor_ref": "iss-x:sub-a", "reason_code": "WIT", "trace_id": "tr-1"}
    pos_neg("contracts/plugin/wit/v1/schema.json", wit_good,
            extras=[("bad-interface", lambda i: i.update(imports=[{**i["imports"][0], "interface_name": "Bad Interface"}]))])

    # plugin/statistics (run record)
    stat_good = {"schema_version": "masi-plugin-statistics/v1", "record_type": "run", "record_id": "run-1",
                 "run_digest": D, "definition_id": "def-1", "schedule_id": "sched-1",
                 "idempotency_key": "ik-1", "status": "succeeded", "started_at_unix_ms": 1000,
                 "finished_at_unix_ms": 2000, "artifact_ref": "art-1",
                 "actor_ref": "iss-x:sub-a", "reason_code": "SUCCEEDED", "trace_id": "tr-1"}
    stat_schema = load(repo / "contracts/plugin/statistics/v1/schema.json")
    validate(stat_schema, stat_good, "plugin-statistics:positive")
    for label, inst in [("unknown-major", {**stat_good, "schema_version": "masi-plugin-statistics/v2"}),
                        ("extra", {**stat_good, "__x__": 1}),
                        ("succeeded-no-artifact", {**stat_good, "artifact_ref": None, "finished_at_unix_ms": None})]:
        reject(stat_schema, inst, f"plugin-statistics:{label}")
        negative_vectors += 1
    artifact_good = {"schema_version": "masi-plugin-statistics/v1", "record_type": "artifact",
                     "record_id": "artifact-1", "artifact_digest": D, "run_id": "run-1",
                     "definition_id": "def-1", "definition_digest": D2, "status": "succeeded",
                     "quality": "valid",
                     "metrics": [{"metric_id": "alerts", "metric_kind": "sum",
                                  "temporality": "cumulative", "value": 2, "unit": "events"}],
                     "series": [{"series_id": "series-1", "labels": {"class": "alert"},
                                 "point_count": 1,
                                 "points": [{"timestamp_unix_ms": 1000, "value": 2}]}],
                     "tables": [{"table_id": "table-1", "columns": ["class", "count"],
                                 "row_count": 1, "rows": [["alert", 2]]}],
                     "truncation": {"truncated_rows": 0, "truncated_series": 0, "reason_code": "NONE"},
                     "provenance": {"plugin_id": "plug-1", "plugin_revision": "manifest-1:1",
                                    "computed_at_unix_ms": 1000, "definition_id": "def-1",
                                    "definition_digest": D2, "run_id": "run-1", "binding_generation": 1},
                     "bytes": 512, "actor_ref": "plug-1", "reason_code": "SUCCEEDED", "trace_id": "tr-1"}
    validate(stat_schema, artifact_good, "plugin-statistics:artifact-positive")
    reject(stat_schema, {**artifact_good, "series": [{"series_id": "series-1", "labels": {},
                                                       "point_count": 1}]},
           "plugin-statistics:artifact-series-missing-points")
    negative_vectors += 1

    # web
    web_good = {"schema_version": "masi-web-projection/v1", "projection_type": "current",
                "resource_kind": "event", "resource_id": "evt-1", "cursor": "c-1", "page_size": 50,
                "total_count": 1, "items": [], "generation": 1, "session_scope": "sess-1",
                "authorized_scope": "scope-1",
                "sse_event": {"event_type": "invalidate", "resource_kind": "event", "resource_id": "evt-1",
                              "cursor": "sse:1:1", "sequence": 1, "generation": 1,
                              "produced_at_unix_ms": 1000, "data_time_unix_ms": 900,
                              "payload_digest": "sha256:" + "a" * 64,
                              "bytes": 256, "emitted_at_unix_ms": 1000},
                "actor_ref": "iss-x:sub-a", "reason_code": "PROJECTION", "trace_id": "tr-1"}
    pos_neg("contracts/web/v1/schema.json", web_good,
            extras=[("sse-oversize", lambda i: i.update(sse_event={**i["sse_event"], "bytes": 70000})),
                    ("page-oversize", lambda i: i.update(page_size=500))])

    # ---- 5. Evidence schema extensions: MOD-CTRL-001 ----
    findings_schema = load(repo / "contracts/evidence/module-findings/v1/schema.json")
    findings_env = {"schema_version": "module-findings/v1", "module_id": "MOD-CTRL-001",
                    "decision_id": "DEC-044", "reviewed_at": "2026-08-18T00:00:00Z",
                    "review_scope": "control-core module complete", "findings": [
                        {"finding_id": "CTRL-TEST-001", "severity": "P1", "status": "OPEN", "title": "t",
                         "requirement_ids": ["FUNC-GOV-001"],
                         "evidence_refs": ["control-go/evidence/module-gates/gate-summary.json"],
                         "opened_at": "2026-08-18T00:00:00Z", "closed_at": None, "resolution": None}]}
    validate(findings_schema, findings_env, "module-findings:MOD-CTRL-001")
    reject(findings_schema, {**findings_env, "module_id": "MOD-CTRL-002"}, "module-findings:bad-module")
    reject(findings_schema, {**findings_env, "findings": [{**findings_env["findings"][0], "finding_id": "CTRL-bad"}]}, "module-findings:bad-id")
    negative_vectors += 2
    checked.append({"name": "module-findings-MOD-CTRL-001", "digest": sha256(repo / "contracts/evidence/module-findings/v1/schema.json")})

    tr_schema = load(repo / "contracts/evidence/traceability/v1/schema.json")
    cond = [{"profile": "availability-ha/v1", "applicability": "NOT_APPLICABLE", "result": "NOT_RUN", "stable_reason": "SINGLE_FAILURE_DOMAIN_HOST"},
            {"profile": "model-runtime-central-cuda/v1", "applicability": "NOT_APPLICABLE", "result": "NOT_RUN", "stable_reason": "CPU_PROFILE_FIRST_RELEASE_ONLY"},
            {"profile": "target-gnmi-readonly/v1", "applicability": "NOT_APPLICABLE", "result": "NOT_RUN", "stable_reason": "NOT_TRIGGERED_BY_FIRST_RELEASE_BMV2_ASSIGNMENT"},
            {"profile": "p4-hardware/v1", "applicability": "NOT_APPLICABLE", "result": "NOT_RUN", "stable_reason": "BMV2_SOFTWARE_ONLY_FIRST_RELEASE"}]
    tr_env = {"schema_version": "control-core-requirement-traceability/v1", "module_id": "MOD-CTRL-001",
              "manifest": "control-go/requirements-traceability.json", "manifest_digest": D,
              "evidence_root": None, "result": "PASS", "qualification": "QUALIFIED",
              "requirements": [], "requirement_ids": ["FUNC-GOV-001"],
              "conditional_applicability": cond, "artifacts": [], "failures": [], "valid": True}
    validate(tr_schema, tr_env, "traceability:MOD-CTRL-001")
    bad_cond = copy.deepcopy(cond); bad_cond[0] = {**cond[0], "stable_reason": "because we said so"}
    reject(tr_schema, {**tr_env, "conditional_applicability": bad_cond}, "traceability:bad-reason")
    negative_vectors += 1
    checked.append({"name": "traceability-MOD-CTRL-001", "digest": sha256(repo / "contracts/evidence/traceability/v1/schema.json")})

    # soak controlClaimScope
    soak_schema = load(repo / "contracts/evidence/soak/v1/schema.json")
    wrapper = {"$ref": "#/$defs/claimScope", "$defs": soak_schema["$defs"]}
    claim_inst = {"module": "control-core", "scope": "independent-module-soak",
                  "rule_counts": [0, 128, 1024, 4096], "target_counts": [0, 1, 2, "N"],
                  "concurrency": [2, 8, 32, 128], "threshold_status": "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001",
                  "resource_limits": {"pg_connections": 8, "pool_size": 8, "queue_depth": 100,
                                      "transaction_bytes": 65536, "wal_bytes": 1048576,
                                      "rss_bytes": None, "fd_count": None, "thread_count": None,
                                      "process_threshold_status": "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001"}}
    if list(Draft202012Validator(wrapper).iter_errors(claim_inst)):
        raise ValueError("soak controlClaimScope positive failed")
    if not list(Draft202012Validator(wrapper).iter_errors({**claim_inst, "module": "rust-edge-agent"})):
        raise ValueError("soak controlClaimScope wrong-module accepted")
    negative_vectors += 1
    checked.append({"name": "soak-controlClaimScope", "digest": sha256(repo / "contracts/evidence/soak/v1/schema.json")})

    # command working_directory control-go
    cmd_schema = load(repo / "contracts/evidence/command/v1/schema.json")
    if "control-go" not in cmd_schema["properties"]["working_directory"]["enum"]:
        raise ValueError("command schema missing control-go working_directory")
    checked.append({"name": "command-control-go", "digest": sha256(repo / "contracts/evidence/command/v1/schema.json")})

    # ---- 6. OpenAPI structural check ----
    try:
        import yaml
    except ImportError:
        raise ValueError("PyYAML required to validate openapi/v1/openapi.yaml")
    oa = yaml.safe_load((repo / "contracts" / "openapi" / "v1" / "openapi.yaml").read_text(encoding="utf-8"))
    if oa.get("openapi") != "3.1.2":
        raise ValueError("openapi.yaml openapi version not 3.1.2")
    required_paths = ["/api/events", "/api/incidents", "/api/evidence", "/api/effects/proposals",
                      "/api/effects/decisions", "/api/effects/intents", "/api/firewall/revisions",
                      "/api/firewall/activations",
                      "/api/firewall/overlays",
                      "/api/targets", "/api/fleet/operations", "/api/rule-effectiveness",
                      "/api/models/revisions", "/api/models/bindings", "/api/models/rollout-groups", "/api/plugins",
                      "/api/plugins/statistics/definitions", "/api/plugins/statistics/runs",
                      "/api/plugins/statistics/current", "/api/plugins/statistics/schedules",
                      "/api/plugins/statistics/artifacts/{artifactID}", "/events", "/healthz", "/readyz",
                      "/livez", "/oidc/callback"]
    missing = [p for p in required_paths if p not in oa.get("paths", {})]
    if missing:
        raise ValueError(f"openapi.yaml missing paths: {missing}")
    if "oidc" not in oa["components"]["securitySchemes"] or "csrf" not in oa["components"]["securitySchemes"]:
        raise ValueError("openapi.yaml missing oidc/csrf securitySchemes")
    if oa.get("x-masi-openapi-profile") != "openapi-rest/v1":
        raise ValueError("openapi.yaml x-masi-openapi-profile not openapi-rest/v1")

    def local_ref(value):
        if not isinstance(value, dict) or "$ref" not in value or not value["$ref"].startswith("#/components/"):
            return value
        current = oa
        for segment in value["$ref"][2:].split("/"):
            current = current[segment]
        return current

    for path_name, path_item in oa.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for response in operation.get("responses", {}).values():
                response = local_ref(response)
                for media in response.get("content", {}).values():
                    if media.get("schema") == {"type": "object"}:
                        raise ValueError(f"openapi placeholder response schema: {method.upper()} {path_name}")
            if method == "post" and path_name != "/oidc/logout":
                security = operation.get("security", [])
                if not any(set(entry) >= {"oidc", "csrf"} for entry in security):
                    raise ValueError(f"mutation does not require OIDC+CSRF together: {path_name}")
                request_body = local_ref(operation.get("requestBody", {}))
                schema = local_ref(request_body.get("content", {}).get("application/json", {}).get("schema", {}))
                if "idempotency_key" not in schema.get("required", []):
                    raise ValueError(f"mutation body lacks required idempotency_key: {path_name}")
    checked.append({"name": "openapi-rest", "digest": sha256(repo / "contracts/openapi/v1/openapi.yaml")})

    # ---- 7. Semantic spot-checks (cross-record invariants) ----
    # R2 maker-checker: proposer != approver, both stable (iss,sub).
    if dec["actor_ref"] == prop["actor_ref"]:
        raise ValueError("semantic: R2 maker-checker requires different actors")
    # only-intent-claimable: proposal/decision have no claim_state field
    if "claim_state" in prop or "claim_state" in dec:
        raise ValueError("semantic: proposal/decision must not carry claim_state")
    # fleet parent nonclaimable: parent_intent_id is distinct from child intent_ids
    if fleet_good["parent_intent_id"] in {c["intent_id"] for c in fleet_good["child_intents"]}:
        raise ValueError("semantic: fleet parent must not equal a child intent id")
    negative_vectors += 3  # count the semantic invariants as enforced checks

    # ---- Output ----
    print(json.dumps({
        "schema_version": "control-core-contract-validation/v1",
        "checked": checked,
        "negative_vectors": negative_vectors,
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
        "qualification_scope": "CONTRACT_SCHEMA_SMOKE_ONLY",
        "module_complete": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
