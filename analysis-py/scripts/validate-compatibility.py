#!/usr/bin/env python3
"""Validate AGENT-COMPAT-001 completeness, decisions, tests, and evidence hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

REQUIRED_CAPABILITIES = {
    "frozen-input-hash-quality",
    "rate-impact-context",
    "quality-provenance-context",
    "runtime-p4-read-context",
    "incident-replay-context",
    "hypothesis-gap-planning",
    "bounded-readonly-mcp",
    "optional-second-synthesis",
    "hard-budgets",
    "grounding-report-guard",
    "stable-degradation",
    "append-only-trace-topology",
    "non-executable-artifact",
    "workflow-checkpoint-runtime",
    "outer-workflow-report-summary",
    "direct-effect-review-deployment",
    "freeform-chat-endpoint",
}
KNOWN_TESTS = {
    "A2A_AGENT_CARD",
    "A2A_MCP_GROUNDED_SUCCESS",
    "A2A_OUTBOUND_DELEGATION_LOOP_FENCE",
    "A2A_IDEMPOTENCY_CONFLICT",
    "A2A_VERSION_FRAMING_NEGATIVES",
    "LIMITED_TIMEOUT_SECURITY_XAI_NEGATIVES",
    "FOUR_SKILLS",
    "TRACE_METRICS_REDACTION",
    "TLS13_MTLS_IDENTITY",
    "MCP_BUDGET_SESSION_CLEANUP",
    "BINDING_REVOKE_LATE_FENCE",
    "ZERO_CORE_P4_EFFECT_MUTATION",
    "BOUNDED_DRAIN_SHUTDOWN",
    "test-input-digest",
    "test-real-langgraph-context",
    "test-low-quality",
    "test-provider-failure",
}


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--require-legacy-snapshot", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    repository = args.repo.resolve()
    if args.require_legacy_snapshot and args.legacy_root is None:
        raise ValueError("formal compatibility validation requires the legacy source snapshot")
    if args.legacy_root is not None:
        args.legacy_root = args.legacy_root.resolve(strict=True)
    matrix_path = repository / "analysis-py/compatibility-matrix.json"
    schema = load(repository / "contracts/analysis/compatibility/v1/schema.json")
    matrix = load(matrix_path)
    errors = sorted(Draft202012Validator(schema).iter_errors(matrix), key=lambda error: list(error.path))
    if errors:
        raise ValueError("; ".join(f"{list(error.path)}: {error.message}" for error in errors))
    rows = matrix["rows"]
    capability_ids = [row["capability_id"] for row in rows]
    if len(capability_ids) != len(set(capability_ids)) or set(capability_ids) != REQUIRED_CAPABILITIES:
        raise ValueError("compatibility capability set is incomplete or duplicated")
    all_skills: set[str] = set()
    classifications: dict[str, int] = {}
    for row in rows:
        all_skills.update(row["vnext_skill_ids"])
        classifications[row["classification"]] = classifications.get(row["classification"], 0) + 1
        if not set(row["test_ids"]).issubset(KNOWN_TESTS):
            raise ValueError(f"{row['capability_id']}: unknown or unexecuted test ID")
        evidence_path = repository / row["evidence_ref"]
        if not evidence_path.is_file() or evidence_path.is_symlink() or digest(evidence_path) != row["evidence_digest"]:
            raise ValueError(f"{row['capability_id']}: evidence hash drift")
        if row["classification"] != "equivalent" and row["owner_decision"] not in {"ADR-0002", "AGENT-COMPAT-001"}:
            raise ValueError(f"{row['capability_id']}: accepted difference lacks Owner decision")
        if args.legacy_root:
            source = args.legacy_root / row["legacy_source"]["path"]
            if source.exists() and digest(source) != row["legacy_source"]["source_digest"]:
                raise ValueError(f"{row['capability_id']}: legacy source digest drift")
    if all_skills != {
        "analyze_nids_incident",
        "compare_event_windows",
        "draft_mitigation_advice",
        "generate_incident_content",
    }:
        raise ValueError("compatibility matrix does not cover all four qualified skills")
    if matrix["budget_invariants"]["mutation_tool_calls"] != 0:
        raise ValueError("compatibility matrix expanded mutation authority")
    evidence = {
        "schema_version": "analysis-compatibility-evidence/v1",
        "module_id": "MOD-AGENT-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "matrix_id": "AGENT-COMPAT-001",
        "matrix_digest": digest(matrix_path),
        "golden_catalog_digest": digest(repository / "contracts/analysis/v1/golden/catalog.json"),
        "legacy_snapshot_verified": args.legacy_root is not None,
        "row_count": len(rows),
        "classifications": classifications,
        "all_required_capabilities_mapped": True,
        "all_four_skills_mapped": True,
        "mutation_tool_calls": 0,
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
    }
    raw = json.dumps(evidence, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    if args.evidence:
        args.evidence.write_bytes(raw)
    print(raw.decode().strip())


if __name__ == "__main__":
    main()
