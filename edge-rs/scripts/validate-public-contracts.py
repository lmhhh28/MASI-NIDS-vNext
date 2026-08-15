#!/usr/bin/env python3
"""Validate Edge-owned public JSON contracts, goldens, and fail-closed negatives."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def validate(schema: dict[str, Any], instance: dict[str, Any], name: str) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            instance
        ),
        key=lambda item: list(item.path),
    )
    if errors:
        raise ValueError(
            "\n".join(f"{name} {list(error.path)}: {error.message}" for error in errors)
        )


def reject(schema: dict[str, Any], instance: dict[str, Any], name: str) -> None:
    if not list(Draft202012Validator(schema).iter_errors(instance)):
        raise ValueError(f"negative contract vector was accepted: {name}")


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def semantic_target(value: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if value["expires_at_unix_ms"] <= value["issued_at_unix_ms"]:
        failures.append("TARGET-LEASE-MONOTONIC")
    if value["election_ceiling"] < value["election_floor"]:
        failures.append("TARGET-ELECTION-RANGE")
    election = value["fence"]["election_id_low"]
    if not value["election_floor"] <= election <= value["election_ceiling"]:
        failures.append("TARGET-ELECTION-IN-RANGE")
    return failures


def semantic_telemetry(value: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    profile = value["flow_identity_profile"]
    if len({value["target_id"], value["shard_id"], profile["shard_id"]}) != 1:
        failures.append("TELEMETRY-TARGET-SHARD")
    sequence = value["source_sequence"]
    if sequence["expected_start"] > sequence["expected_end"]:
        failures.append("TELEMETRY-SOURCE-SEQUENCE")
    if sequence["observed_start"] > sequence["observed_end"]:
        failures.append("TELEMETRY-OBSERVED-SEQUENCE")
    window = value["window"]
    times = [
        instant(window[field])
        for field in ("window_start", "window_end", "finalized_at", "produced_at")
    ]
    if not times[0] < times[1] <= times[2] <= times[3]:
        failures.append("TELEMETRY-WINDOW-TIME")
    snapshot = value["snapshot"]
    if not (
        value["bank"] == snapshot["frozen_bank"]
        and snapshot["active_bank_after_flip"] == 1 - value["bank"]
    ):
        failures.append("TELEMETRY-SNAPSHOT-BANK")
    if value["reported_nonzero_cells"] != len(value["cells"]):
        failures.append("TELEMETRY-CELL-CARDINALITY")
    if value["aggregate"] != {
        "packets": sum(cell["packets"] for cell in value["cells"]),
        "bytes": sum(cell["bytes"] for cell in value["cells"]),
    }:
        failures.append("TELEMETRY-AGGREGATE-SUM")
    return failures


def semantic_firewall(value: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    plan = value["compiled_plan"]
    selector = plan["selector"]
    if value["active_bank"] == value["bank"]:
        failures.append("FIREWALL-INACTIVE-BANK")
    if not (
        value["active_bank"] == plan["active_bank"] == selector["current_bank"]
        and value["bank"] == plan["inactive_bank"] == selector["expected_bank"]
    ):
        failures.append("FIREWALL-PLAN-BANKS")
    if any(entity["bank"] != value["bank"] for entity in plan["entities"]):
        failures.append("FIREWALL-ENTITY-BANK")
    activation = value["activation"]
    if (
        activation["expected_entries"] != len(plan["entities"])
        or activation["observed_entries"] + activation["mismatched_entries"]
        != activation["expected_entries"]
    ):
        failures.append("FIREWALL-ACTIVATION-CARDINALITY")
    if (plan["preflight_result"] == "accepted") != (plan["rejection_reason"] is None):
        failures.append("FIREWALL-PREFLIGHT-REJECTION")
    return failures


def semantic_rule_observation(value: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    sample = value["sample"]
    eligible = value["eligible_sample"]
    times = [
        instant(value["installed_at"]),
        instant(value["readback_at"]),
        instant(sample["read_started_at"]),
        instant(sample["read_completed_at"]),
    ]
    if not times[0] <= times[1] <= times[2] <= times[3]:
        failures.append("RULE-READBACK-TIME")
    if (
        sample["sample_sequence"] != eligible["sample_sequence"]
        or sample["trace_id"] != eligible["trace_id"]
    ):
        failures.append("RULE-SAMPLE-PAIR")
    if any(
        sample["value"][field] > eligible["value"][field]
        for field in ("packets", "bytes")
    ):
        failures.append("RULE-ELIGIBLE-DOMINATES")
    outcome = value["packet_action_outcome"]
    if instant(outcome["window_start"]) >= instant(outcome["window_end"]):
        failures.append("RULE-OUTCOME-WINDOW")
    if value["quality"]["status"] == "valid" and not (
        value["installation_readback"] == "exact"
        and sample["order_status"] == "in-order"
        and sample["gap_status"] == "none"
    ):
        failures.append("RULE-VALID-LAYERS")
    return failures


def require_semantic_failure(
    validator: Any, instance: dict[str, Any], constraint_id: str
) -> None:
    failures = validator(instance)
    if constraint_id not in failures:
        raise ValueError(f"semantic negative vector was accepted: {constraint_id}")


def constraint_ids(schema: dict[str, Any]) -> set[str]:
    constraints = schema.get("x-masi-semantic-constraints", [])
    if not isinstance(constraints, list):
        return set()
    return {
        str(item.get("constraint_id"))
        for item in constraints
        if isinstance(item, dict) and item.get("constraint_id")
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()

    pairs = [
        (
            "target",
            "contracts/target/v1/schema.json",
            "contracts/golden/target/assignment-v1.json",
        ),
        (
            "telemetry",
            "contracts/telemetry/v1/schema.json",
            "contracts/golden/telemetry/snapshot-v1.json",
        ),
        (
            "firewall",
            "contracts/p4/firewall-policy/v1/schema.json",
            "contracts/golden/p4/firewall-policy-v1.json",
        ),
        (
            "rule-observation",
            "contracts/p4/rule-observation/v1/schema.json",
            "contracts/golden/p4/rule-observation-v1.json",
        ),
        (
            "edge-command",
            "contracts/evidence/command/v1/schema.json",
            "contracts/golden/evidence/edge-command-execution-v1.json",
        ),
        (
            "edge-blackbox",
            "contracts/evidence/edge-blackbox/v1/schema.json",
            "contracts/golden/evidence/edge-blackbox-v1.json",
        ),
        (
            "edge-module",
            "contracts/evidence/v1/edge-module-schema.json",
            "contracts/golden/evidence/edge-module-v1.json",
        ),
        (
            "module-findings",
            "contracts/evidence/module-findings/v1/schema.json",
            "contracts/golden/evidence/module-findings-v1.json",
        ),
        (
            "edge-oci-evidence",
            "contracts/evidence/v1/edge-module-schema.json",
            "contracts/golden/evidence/edge-oci-startup-v1.json",
        ),
        (
            "edge-oci-not-run-evidence",
            "contracts/evidence/v1/edge-module-schema.json",
            "contracts/golden/evidence/edge-oci-not-run-v1.json",
        ),
        (
            "edge-deep-evidence",
            "contracts/evidence/v1/edge-module-schema.json",
            "contracts/golden/evidence/edge-deep-check-v1.json",
        ),
        (
            "edge-deep-not-run-evidence",
            "contracts/evidence/v1/edge-module-schema.json",
            "contracts/golden/evidence/edge-deep-not-run-v1.json",
        ),
        (
            "edge-supply-evidence",
            "contracts/evidence/v1/edge-module-schema.json",
            "contracts/golden/evidence/edge-supply-verification-v1.json",
        ),
        (
            "edge-supply-failure-evidence",
            "contracts/evidence/v1/edge-module-schema.json",
            "contracts/golden/evidence/edge-supply-failure-v1.json",
        ),
        (
            "edge-supply-not-run-evidence",
            "contracts/evidence/v1/edge-module-schema.json",
            "contracts/golden/evidence/edge-supply-not-run-v1.json",
        ),
        (
            "traceability",
            "contracts/evidence/traceability/v1/schema.json",
            "contracts/golden/evidence/edge-traceability-v1.json",
        ),
        (
            "edge-supply-release",
            "contracts/supply-chain/v1/rust-edge-agent-release.schema.json",
            "contracts/golden/supply-chain/rust-edge-agent-release-v1.json",
        ),
    ]
    checked: list[dict[str, object]] = []
    documents: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for name, schema_relative, golden_relative in pairs:
        schema_path = repo / schema_relative
        golden_path = repo / golden_relative
        schema = load(schema_path)
        golden = load(golden_path)
        validate(schema, golden, name)
        documents[name] = (schema, golden)
        checked.append(
            {
                "name": name,
                "schema_digest": sha256(schema_path),
                "golden_digest": sha256(golden_path),
            }
        )

    target_schema, target = documents["target"]
    unknown_major = copy.deepcopy(target)
    unknown_major["schema_version"] = "target-assignment/v2"
    reject(target_schema, unknown_major, "target unknown major")
    unknown_field = copy.deepcopy(target)
    unknown_field["fallback_endpoint"] = "http://127.0.0.1:9559"
    reject(target_schema, unknown_field, "target unknown/fallback field")
    plaintext = copy.deepcopy(target)
    plaintext["p4runtime_endpoint"] = "http://127.0.0.1:9559"
    reject(target_schema, plaintext, "target plaintext endpoint")
    wildcard = copy.deepcopy(target)
    wildcard["p4runtime_tls"]["server_name"] = "*.test"
    reject(target_schema, wildcard, "target wildcard TLS identity")
    target_constraint_ids = {
        "TARGET-LEASE-MONOTONIC",
        "TARGET-ELECTION-RANGE",
        "TARGET-ELECTION-IN-RANGE",
        "TARGET-LEASE-NONREUSE",
        "TARGET-HANDOFF-ELECTION-FLOOR",
    }
    if constraint_ids(target_schema) != target_constraint_ids or semantic_target(
        target
    ):
        raise ValueError("target semantic constraint registry or golden drifted")
    target_bad_lease = copy.deepcopy(target)
    target_bad_lease["expires_at_unix_ms"] = target_bad_lease["issued_at_unix_ms"]
    require_semantic_failure(
        semantic_target, target_bad_lease, "TARGET-LEASE-MONOTONIC"
    )
    target_bad_range = copy.deepcopy(target)
    target_bad_range["election_ceiling"] = target_bad_range["election_floor"] - 1
    require_semantic_failure(semantic_target, target_bad_range, "TARGET-ELECTION-RANGE")
    target_bad_election = copy.deepcopy(target)
    target_bad_election["fence"]["election_id_low"] = (
        target_bad_election["election_floor"] - 1
    )
    require_semantic_failure(
        semantic_target, target_bad_election, "TARGET-ELECTION-IN-RANGE"
    )

    telemetry_schema, telemetry = documents["telemetry"]
    telemetry_unknown = copy.deepcopy(telemetry)
    telemetry_unknown["raw_payload"] = "forbidden"
    reject(telemetry_schema, telemetry_unknown, "telemetry raw payload/unknown field")
    telemetry_major = copy.deepcopy(telemetry)
    telemetry_major["schema_version"] = "telemetry-p4-window/v2"
    reject(telemetry_schema, telemetry_major, "telemetry unknown major")
    telemetry_profile = repo / "contracts/profiles/v1/telemetry-p4-window-bmv2.json"
    if telemetry["source_profile_digest"] != sha256(telemetry_profile):
        raise ValueError("telemetry golden does not bind the exact source profile")
    telemetry_constraint_ids = {
        "TELEMETRY-TARGET-SHARD",
        "TELEMETRY-SOURCE-SEQUENCE",
        "TELEMETRY-OBSERVED-SEQUENCE",
        "TELEMETRY-WINDOW-TIME",
        "TELEMETRY-SNAPSHOT-BANK",
        "TELEMETRY-CELL-CARDINALITY",
        "TELEMETRY-AGGREGATE-SUM",
    }
    if constraint_ids(
        telemetry_schema
    ) != telemetry_constraint_ids or semantic_telemetry(telemetry):
        raise ValueError("telemetry semantic constraint registry or golden drifted")
    telemetry_bad_shard = copy.deepcopy(telemetry)
    telemetry_bad_shard["shard_id"] = "other-shard"
    require_semantic_failure(
        semantic_telemetry, telemetry_bad_shard, "TELEMETRY-TARGET-SHARD"
    )
    telemetry_bad_sequence = copy.deepcopy(telemetry)
    telemetry_bad_sequence["source_sequence"]["expected_start"] = 3
    require_semantic_failure(
        semantic_telemetry, telemetry_bad_sequence, "TELEMETRY-SOURCE-SEQUENCE"
    )
    telemetry_bad_window = copy.deepcopy(telemetry)
    telemetry_bad_window["window"]["window_end"] = telemetry_bad_window["window"][
        "window_start"
    ]
    require_semantic_failure(
        semantic_telemetry, telemetry_bad_window, "TELEMETRY-WINDOW-TIME"
    )
    telemetry_bad_bank = copy.deepcopy(telemetry)
    telemetry_bad_bank["snapshot"]["active_bank_after_flip"] = telemetry_bad_bank[
        "bank"
    ]
    require_semantic_failure(
        semantic_telemetry, telemetry_bad_bank, "TELEMETRY-SNAPSHOT-BANK"
    )
    telemetry_bad_cells = copy.deepcopy(telemetry)
    telemetry_bad_cells["reported_nonzero_cells"] = 0
    require_semantic_failure(
        semantic_telemetry, telemetry_bad_cells, "TELEMETRY-CELL-CARDINALITY"
    )
    telemetry_bad_aggregate = copy.deepcopy(telemetry)
    telemetry_bad_aggregate["aggregate"]["packets"] += 1
    require_semantic_failure(
        semantic_telemetry, telemetry_bad_aggregate, "TELEMETRY-AGGREGATE-SUM"
    )

    firewall_schema, firewall = documents["firewall"]
    firewall_constraint_ids = {
        "FIREWALL-INACTIVE-BANK",
        "FIREWALL-PLAN-BANKS",
        "FIREWALL-ENTITY-BANK",
        "FIREWALL-ACTIVATION-CARDINALITY",
        "FIREWALL-PREFLIGHT-REJECTION",
    }
    if constraint_ids(firewall_schema) != firewall_constraint_ids or semantic_firewall(
        firewall
    ):
        raise ValueError("firewall semantic constraint registry or golden drifted")
    firewall_bad_bank = copy.deepcopy(firewall)
    firewall_bad_bank["bank"] = firewall_bad_bank["active_bank"]
    require_semantic_failure(
        semantic_firewall, firewall_bad_bank, "FIREWALL-INACTIVE-BANK"
    )
    firewall_bad_plan = copy.deepcopy(firewall)
    firewall_bad_plan["compiled_plan"]["selector"]["current_bank"] = firewall_bad_plan[
        "bank"
    ]
    require_semantic_failure(
        semantic_firewall, firewall_bad_plan, "FIREWALL-PLAN-BANKS"
    )
    firewall_bad_entity = copy.deepcopy(firewall)
    firewall_bad_entity["compiled_plan"]["entities"][0]["bank"] = firewall_bad_entity[
        "active_bank"
    ]
    require_semantic_failure(
        semantic_firewall, firewall_bad_entity, "FIREWALL-ENTITY-BANK"
    )
    firewall_bad_count = copy.deepcopy(firewall)
    firewall_bad_count["activation"]["observed_entries"] = 0
    require_semantic_failure(
        semantic_firewall, firewall_bad_count, "FIREWALL-ACTIVATION-CARDINALITY"
    )
    firewall_bad_preflight = copy.deepcopy(firewall)
    firewall_bad_preflight["compiled_plan"]["rejection_reason"] = "RESOURCE_EXHAUSTED"
    require_semantic_failure(
        semantic_firewall, firewall_bad_preflight, "FIREWALL-PREFLIGHT-REJECTION"
    )

    rule_schema, rule = documents["rule-observation"]
    rule_constraint_ids = {
        "RULE-READBACK-TIME",
        "RULE-SAMPLE-PAIR",
        "RULE-ELIGIBLE-DOMINATES",
        "RULE-OUTCOME-WINDOW",
        "RULE-VALID-LAYERS",
    }
    if constraint_ids(rule_schema) != rule_constraint_ids or semantic_rule_observation(
        rule
    ):
        raise ValueError(
            "rule-observation semantic constraint registry or golden drifted"
        )
    rule_bad_time = copy.deepcopy(rule)
    rule_bad_time["sample"]["read_completed_at"] = "2029-12-31T23:59:59Z"
    require_semantic_failure(
        semantic_rule_observation, rule_bad_time, "RULE-READBACK-TIME"
    )
    rule_bad_pair = copy.deepcopy(rule)
    rule_bad_pair["eligible_sample"]["sample_sequence"] += 1
    require_semantic_failure(
        semantic_rule_observation, rule_bad_pair, "RULE-SAMPLE-PAIR"
    )
    rule_bad_eligible = copy.deepcopy(rule)
    rule_bad_eligible["sample"]["value"]["packets"] = 3
    require_semantic_failure(
        semantic_rule_observation, rule_bad_eligible, "RULE-ELIGIBLE-DOMINATES"
    )
    rule_bad_outcome = copy.deepcopy(rule)
    rule_bad_outcome["packet_action_outcome"]["window_end"] = rule_bad_outcome[
        "packet_action_outcome"
    ]["window_start"]
    require_semantic_failure(
        semantic_rule_observation, rule_bad_outcome, "RULE-OUTCOME-WINDOW"
    )
    rule_bad_layers = copy.deepcopy(rule)
    rule_bad_layers["installation_readback"] = "missing"
    require_semantic_failure(
        semantic_rule_observation, rule_bad_layers, "RULE-VALID-LAYERS"
    )

    traceability_schema, traceability = documents["traceability"]
    traceability_major = copy.deepcopy(traceability)
    traceability_major["schema_version"] = "rust-edge-agent-traceability-evidence/v2"
    reject(traceability_schema, traceability_major, "traceability unknown major")
    traceability_failure = copy.deepcopy(traceability)
    traceability_failure["failures"] = ["fabricated failure under PASS"]
    reject(traceability_schema, traceability_failure, "traceability PASS with failures")
    traceability_traversal = copy.deepcopy(traceability)
    traceability_traversal["requirements"][0]["evidence"][0]["path"] = "../outside.json"
    reject(traceability_schema, traceability_traversal, "traceability path traversal")
    traceability_duplicate = copy.deepcopy(traceability)
    traceability_duplicate["requirement_ids"].append("MOD-EDGE-001")
    reject(
        traceability_schema,
        traceability_duplicate,
        "traceability duplicate requirement",
    )
    traceability_media = copy.deepcopy(traceability)
    traceability_media["artifacts"][0]["media_type"] = "application/octet-stream"
    reject(traceability_schema, traceability_media, "traceability unknown media type")
    traceability_profile = copy.deepcopy(traceability)
    traceability_profile["conditional_applicability"][0]["profile"] = "fake-profile/v1"
    reject(traceability_schema, traceability_profile, "traceability unknown conditional profile")
    traceability_conditional_duplicate = copy.deepcopy(traceability)
    traceability_conditional_duplicate["conditional_applicability"][3] = copy.deepcopy(
        traceability_conditional_duplicate["conditional_applicability"][0]
    )
    reject(
        traceability_schema,
        traceability_conditional_duplicate,
        "traceability duplicate conditional profile",
    )

    command_schema, command = documents["edge-command"]
    command_major = copy.deepcopy(command)
    command_major["schema_version"] = "edge-command-execution/v2"
    reject(command_schema, command_major, "command execution unknown major")
    command_failed_zero = copy.deepcopy(command)
    command_failed_zero.update(
        {
            "result": "FAIL",
            "qualification": "NOT_QUALIFIED",
            "stable_reason": "COMMAND_EXITED_FAILURE",
        }
    )
    reject(command_schema, command_failed_zero, "failed command with zero exit")
    command_log_escape = copy.deepcopy(command)
    command_log_escape["log"]["path"] = "../tests.log"
    reject(command_schema, command_log_escape, "command log path traversal")

    blackbox_schema, blackbox = documents["edge-blackbox"]
    blackbox_major = copy.deepcopy(blackbox)
    blackbox_major["schema_version"] = "edge-module-e2e-evidence/v2"
    reject(blackbox_schema, blackbox_major, "blackbox unknown major")
    blackbox_false_pass = copy.deepcopy(blackbox)
    blackbox_false_pass["qualification"] = "NOT_QUALIFIED"
    reject(blackbox_schema, blackbox_false_pass, "blackbox PASS without qualification")
    blackbox_unknown = copy.deepcopy(blackbox)
    blackbox_unknown["unknown_public_field"] = True
    reject(blackbox_schema, blackbox_unknown, "blackbox unknown top-level field")

    edge_module_schema, edge_module = documents["edge-module"]
    edge_module_failure = copy.deepcopy(edge_module)
    edge_module_failure["result"] = "FAIL"
    edge_module_failure["overall_status"] = "FAIL"
    edge_module_failure["overall_module_complete"] = False
    edge_module_failure["completion"]["status"] = "INCOMPLETE"
    edge_module_failure["completion"]["real_startup_test_blocker_count"] = 1
    edge_module_failure["completion"]["criteria"]["release_binary"] = "FAIL"
    edge_module_failure["completion"]["blockers"] = ["release_binary"]
    for artifact in (
        "traceability_evidence",
        "release_binary",
        "oci_evidence",
        "deep_check_evidence",
        "supply_chain_evidence",
    ):
        edge_module_failure["artifact_digests"][artifact] = None
    validate(edge_module_schema, edge_module_failure, "edge-module structured failure")
    edge_module_unknown = copy.deepcopy(edge_module)
    edge_module_unknown["unknown_public_field"] = True
    reject(
        edge_module_schema, edge_module_unknown, "edge-module unknown top-level field"
    )
    edge_module_missing_artifact = copy.deepcopy(edge_module)
    edge_module_missing_artifact["artifact_digests"].pop("oci_evidence")
    reject(
        edge_module_schema,
        edge_module_missing_artifact,
        "edge-module missing frozen artifact digest",
    )
    edge_module_extra_artifact = copy.deepcopy(edge_module)
    edge_module_extra_artifact["artifact_digests"]["unbound_artifact"] = (
        "sha256:" + ("0" * 64)
    )
    reject(
        edge_module_schema,
        edge_module_extra_artifact,
        "edge-module unbound artifact digest",
    )
    edge_module_hold_with_missing_artifact = copy.deepcopy(edge_module)
    edge_module_hold_with_missing_artifact["artifact_digests"]["oci_evidence"] = None
    reject(
        edge_module_schema,
        edge_module_hold_with_missing_artifact,
        "edge-module HOLD with missing artifact",
    )
    edge_module_missing_execution = copy.deepcopy(edge_module)
    edge_module_missing_execution["executed_gates"].pop("supply_chain")
    reject(
        edge_module_schema,
        edge_module_missing_execution,
        "edge-module missing executed gate",
    )
    edge_module_missing_qualification = copy.deepcopy(edge_module)
    edge_module_missing_qualification["qualification_gates"].pop(
        "absolute_performance"
    )
    reject(
        edge_module_schema,
        edge_module_missing_qualification,
        "edge-module missing qualification gate",
    )
    edge_module_conditional_profile = copy.deepcopy(edge_module)
    edge_module_conditional_profile["conditional_applicability"][0]["profile"] = (
        "fake-profile/v1"
    )
    reject(
        edge_module_schema,
        edge_module_conditional_profile,
        "edge-module unknown conditional profile",
    )
    edge_module_false_overall = copy.deepcopy(edge_module)
    edge_module_false_overall["overall_module_complete"] = False
    reject(
        edge_module_schema,
        edge_module_false_overall,
        "edge-module COMPLETE with false overall flag",
    )
    edge_module_incomplete_status = copy.deepcopy(edge_module)
    edge_module_incomplete_status["completion"]["status"] = "INCOMPLETE"
    reject(
        edge_module_schema,
        edge_module_incomplete_status,
        "edge-module true overall flag with INCOMPLETE status",
    )
    edge_module_open_p0 = copy.deepcopy(edge_module)
    edge_module_open_p0["completion"]["known_p0_count"] = 1
    reject(edge_module_schema, edge_module_open_p0, "edge-module COMPLETE with open P0")
    edge_module_failed_criterion = copy.deepcopy(edge_module)
    edge_module_failed_criterion["completion"]["criteria"]["real_oci_startup"] = "FAIL"
    reject(
        edge_module_schema,
        edge_module_failed_criterion,
        "edge-module COMPLETE with failed startup criterion",
    )
    edge_module_completion_blocker = copy.deepcopy(edge_module)
    edge_module_completion_blocker["completion"]["blockers"] = ["formal_soak"]
    reject(
        edge_module_schema,
        edge_module_completion_blocker,
        "edge-module COMPLETE with blocker",
    )
    edge_module_missing_completion_digest = copy.deepcopy(edge_module)
    edge_module_missing_completion_digest["completion"]["evidence_digests"]["oci"] = None
    reject(
        edge_module_schema,
        edge_module_missing_completion_digest,
        "edge-module COMPLETE with missing evidence digest",
    )

    findings_schema, findings = documents["module-findings"]
    findings_major = copy.deepcopy(findings)
    findings_major["schema_version"] = "module-findings/v2"
    reject(findings_schema, findings_major, "module findings unknown major")
    findings_unknown = copy.deepcopy(findings)
    findings_unknown["implicit_success"] = True
    reject(findings_schema, findings_unknown, "module findings unknown success field")
    open_finding = {
        "finding_id": "EDGE-P0-001",
        "severity": "P0",
        "status": "OPEN",
        "title": "public contract negative fixture",
        "requirement_ids": ["MOD-EDGE-001"],
        "evidence_refs": ["edge-rs/module-findings.json"],
        "opened_at": "2026-08-14T10:00:00Z",
        "closed_at": None,
        "resolution": None,
    }
    findings_open = copy.deepcopy(findings)
    findings_open["findings"] = [open_finding]
    validate(findings_schema, findings_open, "module findings valid open finding")
    findings_open_resolved = copy.deepcopy(findings_open)
    findings_open_resolved["findings"][0]["closed_at"] = "2026-08-14T10:01:00Z"
    findings_open_resolved["findings"][0]["resolution"] = "invalid mixed state"
    reject(
        findings_schema,
        findings_open_resolved,
        "module findings OPEN with closure fields",
    )
    findings_closed_without_resolution = copy.deepcopy(findings_open)
    findings_closed_without_resolution["findings"][0]["status"] = "CLOSED"
    reject(
        findings_schema,
        findings_closed_without_resolution,
        "module findings CLOSED without resolution",
    )
    findings_traversal = copy.deepcopy(findings_open)
    findings_traversal["findings"][0]["evidence_refs"] = ["edge-rs/../outside.json"]
    reject(findings_schema, findings_traversal, "module findings evidence traversal")

    findings_source_path = repo / "edge-rs/module-findings.json"
    findings_source = load(findings_source_path)
    validate(findings_schema, findings_source, "module findings source")
    if findings_source != findings:
        raise ValueError("module findings source and public golden drifted")
    checked.append(
        {"name": "module-findings-source", "digest": sha256(findings_source_path)}
    )

    edge_oci_schema, edge_oci = documents["edge-oci-evidence"]
    edge_oci_missing_check = copy.deepcopy(edge_oci)
    edge_oci_missing_check["checks"].pop("archive_content_addressing")
    reject(edge_oci_schema, edge_oci_missing_check, "edge OCI missing frozen check")

    edge_deep_schema, edge_deep = documents["edge-deep-evidence"]
    edge_deep_missing_check = copy.deepcopy(edge_deep)
    edge_deep_missing_check["checks"].pop("thread_sanitizer")
    reject(
        edge_deep_schema, edge_deep_missing_check, "edge deep gate missing frozen check"
    )

    edge_supply_schema, edge_supply = documents["edge-supply-evidence"]
    edge_supply_missing_check = copy.deepcopy(edge_supply)
    edge_supply_missing_check["checks"]["scan"].pop("no_secrets")
    reject(
        edge_supply_schema,
        edge_supply_missing_check,
        "edge supply gate missing frozen check",
    )
    edge_supply_false_pass = copy.deepcopy(edge_supply)
    edge_supply_false_pass["checks"]["scan"]["no_secrets"] = False
    reject(
        edge_supply_schema,
        edge_supply_false_pass,
        "edge supply PASS with failed frozen check",
    )

    supply_schema, supply_release = documents["edge-supply-release"]
    supply_major = copy.deepcopy(supply_release)
    supply_major["schema_version"] = "rust-edge-agent-supply-release/v2"
    reject(supply_schema, supply_major, "supply release unknown major")
    supply_unknown = copy.deepcopy(supply_release)
    supply_unknown["fallback_registry"] = "https://registry.invalid"
    reject(supply_schema, supply_unknown, "supply release unknown/fallback field")
    supply_oversized = copy.deepcopy(supply_release)
    supply_oversized["offline_bundle"]["files"][0]["bytes"] = 4_294_967_297
    reject(supply_schema, supply_oversized, "supply release oversized bundle member")
    supply_mutable_image = copy.deepcopy(supply_release)
    supply_mutable_image["tools"]["trivy"]["image"] = "aquasec/trivy:latest"
    reject(supply_schema, supply_mutable_image, "supply release mutable tool image")
    supply_traversal = copy.deepcopy(supply_release)
    supply_traversal["offline_bundle"]["files"][0]["path"] = "../escape.tar"
    reject(supply_schema, supply_traversal, "supply release path traversal")

    edge_profile = load(repo / "contracts/profiles/v1/rust-edge-agent.json")
    for source in edge_profile["contract_sources"]:
        if not (repo / str(source)).is_file():
            raise ValueError(
                f"Edge profile references missing contract source {source}"
            )
    if "contracts/target/v1/schema.json" not in edge_profile["contract_sources"]:
        raise ValueError("Edge profile does not bind the stable target contract")
    if (
        "contracts/supply-chain/v1/rust-edge-agent-release.schema.json"
        not in edge_profile["contract_sources"]
    ):
        raise ValueError("Edge profile does not bind the supply release contract")
    wal_profile = edge_profile.get("wal", {})
    if (
        wal_profile.get("format") != "masi-edge-wal/v1"
        or wal_profile.get("header_bytes") != 36
        or wal_profile.get("max_age_seconds") != 86_400
        or wal_profile.get("age_expiry")
        != "sticky-target-HOLD-without-delete-overwrite-or-external-advance"
        or wal_profile.get("clock_regression_or_future_record")
        != "WAL_CLOCK_UNPROVABLE-and-sticky-target-HOLD"
        or wal_profile.get("overwrite_unacknowledged") is not False
    ):
        raise ValueError("Edge WAL header, age fence, or retention semantics drifted")

    inference_profile_path = repo / "contracts/inference/v1/profile.json"
    inference_profile = load(inference_profile_path)
    if (
        inference_profile.get("schema_version")
        != "inference-central-grpc-batch-profile/v1"
        or inference_profile.get("profile_id") != "inference-central-grpc-batch/v1"
    ):
        raise ValueError("central inference wire profile identity is not frozen")
    admission = inference_profile.get("admission", {})
    retry = inference_profile.get("retry", {})
    handshake = inference_profile.get("committed_binding_handshake", {})
    fallback = inference_profile.get("fallback", {})
    if (
        admission.get("maximum_records_per_batch") != 256
        or admission.get("maximum_request_bytes") != 4_194_304
        or admission.get("maximum_response_bytes") != 4_194_304
        or admission.get("maximum_in_flight_batches_per_target") != 1
        or admission.get("maximum_eligible_workers_per_pool_readback") != 64
        or admission.get("edge_coalescing_delay_ms") != 0
        or retry.get("maximum_same_generation_attempts") != 3
        or retry.get("retryable_grpc_codes")
        != ["UNAVAILABLE", "RESOURCE_EXHAUSTED", "ABORTED"]
        or retry.get("deadline_scope")
        != "one global monotonic budget shared by all attempts and backoffs"
        or retry.get("per_attempt_timeout") != "remaining global monotonic budget"
        or retry.get("cross_generation_retry") is not False
        or handshake.get("schema_version") != "inference-committed-binding/v1"
        or any(value is not False for value in fallback.values())
    ):
        raise ValueError(
            "central inference bounds, retry, handshake, or fallback policy drifted"
        )
    required_handshake_fields = {
        "model_control_incarnation_id",
        "operation_id",
        "scope",
        "shard_id",
        "route_epoch",
        "logical_pool_id",
        "pool_generation",
        "expected_binding_generation",
        "proposed_binding_generation",
        "current_binding_generation",
        "startup_envelope_digest",
        "pool_observation_digest",
        "binding_digest",
        "deadline_unix_ms",
    }
    if set(handshake.get("exact_fields", [])) != required_handshake_fields:
        raise ValueError("committed-binding exact field set drifted")
    proto = (repo / "contracts/edge/v1/edge.proto").read_text(encoding="utf-8")
    required_proto_fragments = [
        "message RouteResumeWatermark",
        "string operation_id = 16;",
        "string binding_digest = 23;",
        "string operation_id = 6;",
        "int64 deadline_unix_ms = 16;",
        "ResumeRouteRequest committed_binding_handshake = 8;",
        "string worker_attempt_id = 53;",
        "message InferenceWorkerIdentity",
        "repeated InferenceWorkerIdentity eligible_workers = 22;",
        "string readback_attempt_id = 20;",
    ]
    if any(fragment not in proto for fragment in required_proto_fragments):
        raise ValueError("committed-binding/result-fence protobuf surface drifted")
    checked.append(
        {
            "name": "inference-wire-profile",
            "profile_digest": sha256(inference_profile_path),
            "contract_digest": sha256(repo / "contracts/edge/v1/edge.proto"),
        }
    )

    print(
        json.dumps(
            {
                "schema_version": "rust-edge-agent-contract-validation/v1",
                "checked": checked,
                "negative_vectors": 65,
                "profile_digest": sha256(
                    repo / "contracts/profiles/v1/rust-edge-agent.json"
                ),
                "result": "PASS",
                "qualification": "QUALIFIED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
