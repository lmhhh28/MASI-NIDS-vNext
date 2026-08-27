#!/usr/bin/env python3
"""Derive Analysis operational Module Complete from immutable gate evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular no-symlink file: {path}")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"JSON evidence exceeds 64 MiB: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def validate(schema_path: Path, document: dict[str, Any], name: str) -> None:
    errors = sorted(
        Draft202012Validator(load(schema_path), format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise SystemExit(name + " schema rejected: " + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors))


def evidence_ref(run_dir: Path, relative: str) -> dict[str, str]:
    return {"evidence": relative, "digest": digest(run_dir / relative), "result": "PASS"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    run_dir = args.run_dir.resolve(strict=True)
    if args.output.exists() or args.output.is_symlink():
        raise SystemExit("refusing to overwrite module summary")
    module = repo / "analysis-py"
    manifest_path = module / "requirements-traceability.json"
    manifest = load(manifest_path)
    command_schema = repo / "contracts/evidence/command/v1/schema.json"
    command_ids = [item["command_id"] for item in manifest["execution_bindings"]]
    if (run_dir / "evidence-integrity.command.json").exists():
        command_ids.append("evidence-integrity")
    sidecars: dict[str, dict[str, Any]] = {}
    for command_id in command_ids:
        sidecar = load(run_dir / f"{command_id}.command.json")
        validate(command_schema, sidecar, f"command {command_id}")
        if sidecar["command_id"] != command_id or sidecar["result"] != "PASS":
            raise SystemExit(f"required command is not PASS: {command_id}")
        log = run_dir / sidecar["log"]["path"]
        if digest(log) != sidecar["log"]["sha256"]:
            raise SystemExit(f"command log digest mismatch: {command_id}")
        sidecars[command_id] = sidecar
    source_digests = {sidecar["source_tree_digest"] for sidecar in sidecars.values()}
    status_digests = {sidecar["working_tree_status_digest"] for sidecar in sidecars.values()}
    if len(source_digests) != 1 or len(status_digests) != 1:
        raise SystemExit("command source/status digests disagree")
    source_tree_digest = next(iter(source_digests))
    working_tree_status_digest = next(iter(status_digests))

    evidence_specs = {
        "release": ("release/release-runtime-evidence.json", "analysis-release-runtime/v1/schema.json"),
        "blackbox": ("blackbox/blackbox-evidence.json", "analysis-blackbox/v1/schema.json"),
        "compatibility": ("compatibility/compatibility-evidence.json", "analysis-compatibility/v1/schema.json"),
        "performance": ("performance/performance-evidence.json", "analysis-performance/v1/schema.json"),
        "image": ("image/image-build-evidence.json", "analysis-image-build/v1/schema.json"),
        "oci": ("oci/oci-evidence.json", "analysis-oci/v1/schema.json"),
        "deployment": ("deployment/deployment-evidence.json", "analysis-deployment/v1/schema.json"),
        "supply": ("supply/supply-chain-evidence.json", "analysis-supply/v1/schema.json"),
        "soak": ("formal-soak/soak-evidence.json", "analysis-soak/v1/schema.json"),
        "traceability": ("traceability.json", "traceability/v1/schema.json"),
    }
    documents: dict[str, dict[str, Any]] = {}
    for name, (relative, schema_relative) in evidence_specs.items():
        document = load(run_dir / relative)
        validate(repo / "contracts/evidence" / schema_relative, document, name)
        documents[name] = document

    for name in ("release", "blackbox", "performance", "image", "oci", "supply", "soak"):
        if documents[name]["source_tree_digest"] != source_tree_digest:
            raise SystemExit(f"{name} source-tree digest mismatch")
    for name in ("release", "blackbox", "performance", "image", "supply"):
        if documents[name]["working_tree_status_digest"] != working_tree_status_digest:
            raise SystemExit(f"{name} working-tree status digest mismatch")
    if len({documents[name]["image_id"] for name in ("image", "oci", "supply")}) != 1:
        raise SystemExit("image identity disagrees across build/OCI/supply evidence")
    release_binary_digest = documents["release"]["entrypoint_digest"]
    if any(documents[name]["binary_digest"] != release_binary_digest for name in ("blackbox", "performance", "soak")):
        raise SystemExit("release process digest disagrees across blackbox/performance/soak")
    if documents["release"]["wheel_digest"] != documents["release"]["second_build_wheel_digest"]:
        raise SystemExit("release wheel reproducibility evidence disagrees")
    expected_scenarios = {
        "A2A_AGENT_CARD",
        "A2A_MCP_GROUNDED_SUCCESS",
        "A2A_OUTBOUND_DELEGATION_LOOP_FENCE",
        "A2A_IDEMPOTENCY_CONFLICT",
        "A2A_REQUESTER_TASK_ISOLATION",
        "A2A_VERSION_FRAMING_NEGATIVES",
        "LIMITED_TIMEOUT_SECURITY_XAI_NEGATIVES",
        "FOUR_SKILLS",
        "TRACE_METRICS_REDACTION",
        "TLS13_MTLS_IDENTITY",
        "MCP_BUDGET_SESSION_CLEANUP",
        "BINDING_REVOKE_LATE_FENCE",
        "ZERO_CORE_P4_EFFECT_MUTATION",
        "BOUNDED_DRAIN_SHUTDOWN",
    }
    if {item["scenario_id"] for item in documents["blackbox"]["scenarios"]} != expected_scenarios:
        raise SystemExit("blackbox scenario set is incomplete")
    soak = documents["soak"]
    if [item["phase_id"] for item in soak["phases"]] != ["steady", "peak", "saturation", "recovery"]:
        raise SystemExit("formal soak phase order drifted")
    monotonic_elapsed = (soak["qualified_end_monotonic_ns"] - soak["qualified_start_monotonic_ns"]) / 1_000_000
    if monotonic_elapsed < 3_600_000 or abs(monotonic_elapsed - soak["qualified_elapsed_ms"]) > 1:
        raise SystemExit("formal soak monotonic duration mismatch")
    if documents["compatibility"]["legacy_snapshot_verified"] is not True:
        raise SystemExit("formal legacy capability snapshot was not verified")

    findings_path = module / "module-findings.json"
    findings = load(findings_path)
    validate(repo / "contracts/evidence/module-findings/v1/schema.json", findings, "findings")
    open_findings = [item for item in findings["findings"] if item["status"] == "OPEN"]
    if open_findings:
        raise SystemExit("all reviewed Analysis findings must be closed for this completion run")
    status_path = run_dir / "working-tree-status.txt"
    if digest(status_path) != working_tree_status_digest:
        raise SystemExit("working-tree status digest mismatch")
    source_revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    if source_revision != documents["release"]["source_revision"] or source_revision != documents["supply"]["source_revision"]:
        raise SystemExit("source revision mismatch")

    artifact_digests = {
        "requirements_baseline": digest(repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md"),
        "requirements_traceability_manifest": digest(manifest_path),
        "module_findings": digest(findings_path),
        "compatibility_matrix": digest(module / "compatibility-matrix.json"),
        "golden_catalog": digest(repo / "contracts/analysis/v1/golden/catalog.json"),
        "uv_lock": digest(module / "uv.lock"),
        "dockerfile": digest(module / "Dockerfile"),
        "compose": digest(repo / "deploy/analysis/compose.acceptance.yaml"),
    }
    for name, (relative, _) in evidence_specs.items():
        artifact_digests[f"evidence_{name}"] = digest(run_dir / relative)
    for command_id in sorted(sidecars):
        artifact_digests[f"command_{command_id}"] = digest(run_dir / f"{command_id}.command.json")
        artifact_digests[f"log_{command_id}"] = digest(run_dir / f"{command_id}.log")

    summary = {
        "schema_version": "analysis-plugin-module-gates/v1",
        "module_id": "MOD-AGENT-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "qualification": "NOT_QUALIFIED",
        "generated_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "source_revision": source_revision,
        "source_tree_digest": source_tree_digest,
        "working_tree_dirty": status_path.stat().st_size > 0,
        "working_tree_status_digest": working_tree_status_digest,
        "executed_gates": {command_id: "PASS" for command_id in sorted(sidecars)},
        "completion": {
            "formal_soak_executed": True,
            "no_required_not_run": True,
            "open_p0_zero": True,
            "operational_gates_pass": True,
            "real_release_process_started": documents["blackbox"]["real_release_process"],
            "real_oci_started": documents["oci"]["actual_oci_started"],
        },
        "findings": {"open_total": 0, "open_p0": 0},
        "release_runtime": evidence_ref(run_dir, evidence_specs["release"][0]),
        "blackbox_e2e": evidence_ref(run_dir, evidence_specs["blackbox"][0]),
        "compatibility": evidence_ref(run_dir, evidence_specs["compatibility"][0]),
        "performance": evidence_ref(run_dir, evidence_specs["performance"][0]),
        "image_build": evidence_ref(run_dir, evidence_specs["image"][0]),
        "oci": evidence_ref(run_dir, evidence_specs["oci"][0]),
        "deployment": evidence_ref(run_dir, evidence_specs["deployment"][0]),
        "supply_chain": evidence_ref(run_dir, evidence_specs["supply"][0]),
        "formal_soak": evidence_ref(run_dir, evidence_specs["soak"][0]),
        "traceability": evidence_ref(run_dir, evidence_specs["traceability"][0]),
        "conditional_applicability": manifest["conditional_applicability"],
        "requirement_ids": [item["requirement_id"] for item in manifest["requirements"]],
        "artifact_digests": artifact_digests,
        "qualification_gates": {
            "formal_soak_3600_seconds": "PASS",
            "analysis_boundary_performance": "PASS",
            "real_external_provider": "HOLD",
            "go_analysis_pairwise": "HOLD",
            "protected_release_baseline": "HOLD",
            "production_ha_multi_failure_domain": "HOLD",
            "nine_module_global_gate": "HOLD",
        },
        "remaining_holds": [
            "A deterministic mTLS provider fixture proves the bounded adapter but does not qualify a real external "
            "LLM provider identity, quota, model or data policy.",
            "Formal Go Plugin Manager and direct A2A/MCP pairwise integration has not started because the nine-module gate remains HOLD.",
            "The acceptance runtime is one failure domain and cannot claim production-ha qualification.",
            "A protected release commit/tag, repository@sha256 publication and release-baseline digest are not established by this dirty-tree module run.",
            "Frontend Artifact display and complete Analysis-disabled core equivalence remain later Web and system qualification scope.",
        ],
        "overall_module_complete": True,
    }
    validate(repo / "contracts/evidence/analysis-module/v1/schema.json", summary, "module summary")
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("analysis-plugin operational Module Complete: true")


if __name__ == "__main__":
    main()
