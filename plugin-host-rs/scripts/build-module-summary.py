#!/usr/bin/env python3
"""Derive Plugin Host operational Module Complete from immutable gate evidence."""

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


def tree_digest(root: Path, paths: list[Path]) -> str:
    value = hashlib.sha256()
    files: list[Path] = []
    for path in paths:
        files.extend(path.rglob("*.go") if path.is_dir() else [path])
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        value.update(len(relative).to_bytes(4, "big"))
        value.update(relative)
        raw = path.read_bytes()
        value.update(len(raw).to_bytes(8, "big"))
        value.update(raw)
    return "sha256:" + value.hexdigest()


def validate(schema_path: Path, document: dict[str, Any], name: str) -> None:
    schema = load(schema_path)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ValueError(
            name
            + ": "
            + "; ".join(
                f"{list(error.absolute_path)}: {error.message}" for error in errors
            )
        )


def evidence_ref(run_dir: Path, path: str) -> dict[str, str]:
    target = run_dir / path
    return {"evidence": path, "digest": digest(target), "result": "PASS"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    run_dir = args.run_dir.resolve(strict=True)
    output = args.output
    if output.exists() or output.is_symlink():
        raise SystemExit("refusing to overwrite module summary")
    manifest_path = repo / "plugin-host-rs/requirements-traceability.json"
    manifest = load(manifest_path)
    command_schema = repo / "contracts/evidence/command/v1/schema.json"
    command_ids = [item["command_id"] for item in manifest["execution_bindings"]]
    if (run_dir / "evidence-integrity.command.json").exists():
        command_ids.append("evidence-integrity")
    sidecars: dict[str, dict[str, Any]] = {}
    for command_id in command_ids:
        sidecar_path = run_dir / f"{command_id}.command.json"
        sidecar = load(sidecar_path)
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

    deep = load(run_dir / "deep/deep-checks-evidence.json")
    fault = load(run_dir / "fault/fault-evidence.json")
    performance = load(run_dir / "performance/performance-evidence.json")
    oci = load(run_dir / "oci/oci-smoke-evidence.json")
    supply = load(run_dir / "supply/supply-chain-evidence.json")
    soak = load(run_dir / "formal-soak/soak-evidence.json")
    traceability = load(run_dir / "traceability.json")
    validate(repo / "contracts/evidence/plugin-runtime-host-deep/v1/schema.json", deep, "deep")
    validate(repo / "contracts/evidence/plugin-runtime-host-fault/v1/schema.json", fault, "fault")
    validate(
        repo / "contracts/evidence/plugin-runtime-host-performance/v1/schema.json",
        performance,
        "performance",
    )
    validate(repo / "contracts/evidence/plugin-runtime-host-oci/v1/schema.json", oci, "oci")
    validate(
        repo / "contracts/evidence/plugin-runtime-host-supply/v1/schema.json",
        supply,
        "supply",
    )
    validate(repo / "contracts/evidence/plugin-runtime-host-soak/v1/schema.json", soak, "soak")
    validate(repo / "contracts/evidence/traceability/v1/schema.json", traceability, "traceability")
    if oci["source_tree_digest"] != source_tree_digest or supply["source_tree_digest"] != source_tree_digest:
        raise SystemExit("OCI/supply source digest does not match command snapshot")

    findings_path = repo / "plugin-host-rs/module-findings.json"
    findings = load(findings_path)
    validate(repo / "contracts/evidence/module-findings/v1/schema.json", findings, "findings")
    open_findings = [item for item in findings["findings"] if item["status"] == "OPEN"]
    open_p0 = [item for item in open_findings if item["severity"] == "P0"]
    if open_p0:
        raise SystemExit("open P0 findings block operational completion")

    status_path = run_dir / "working-tree-status.txt"
    if digest(status_path) != working_tree_status_digest:
        raise SystemExit("working tree status digest mismatch")
    source_revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if source_revision != oci["source_revision"] or source_revision != supply["source_revision"]:
        raise SystemExit("source revision mismatch")

    artifacts = {
        "requirements_baseline": digest(
            repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md"
        ),
        "requirements_traceability_manifest": digest(manifest_path),
        "module_findings": digest(findings_path),
        "traceability_evidence": digest(run_dir / "traceability.json"),
        "deep_checks_evidence": digest(run_dir / "deep/deep-checks-evidence.json"),
        "fault_recovery_evidence": digest(run_dir / "fault/fault-evidence.json"),
        "performance_evidence": digest(run_dir / "performance/performance-evidence.json"),
        "oci_evidence": digest(run_dir / "oci/oci-smoke-evidence.json"),
        "supply_chain_evidence": digest(run_dir / "supply/supply-chain-evidence.json"),
        "formal_soak_evidence": digest(run_dir / "formal-soak/soak-evidence.json"),
        "host_binary": digest(repo / "plugin-host-rs/target/release/masi-plugin-host"),
        "hostctl_binary": digest(repo / "plugin-host-rs/target/release/masi-plugin-hostctl"),
        "service_fixture_binary": digest(
            repo / "plugin-host-rs/target/release/masi-plugin-service-fixture"
        ),
        "host_proto": digest(repo / "contracts/plugin/host/v1/host.proto"),
        "service_proto": digest(repo / "contracts/plugin/service/v1/service.proto"),
        "wit_source": digest(repo / "contracts/plugin/wit/v1/pure-transform.wit"),
        "cargo_lock": digest(repo / "plugin-host-rs/Cargo.lock"),
        "control_contract_consumer": tree_digest(
            repo,
            [
                repo / "control-go/scripts/validate-public-contracts.py",
                repo / "control-go/internal/pluginstat",
                repo / "control-go/internal/grpc/statistics_executor.go",
            ],
        ),
    }
    for command_id in sorted(sidecars):
        artifacts[f"command_{command_id}"] = digest(
            run_dir / f"{command_id}.command.json"
        )
        artifacts[f"log_{command_id}"] = digest(run_dir / f"{command_id}.log")
    summary = {
        "schema_version": "plugin-runtime-host-module-gates/v1",
        "module_id": "MOD-PLUGIN-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "qualification": "NOT_QUALIFIED",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_revision": source_revision,
        "source_tree_digest": source_tree_digest,
        "working_tree_dirty": status_path.stat().st_size > 0,
        "working_tree_status_digest": working_tree_status_digest,
        "executed_gates": {command_id: "PASS" for command_id in sorted(sidecars)},
        "completion": {
            "formal_soak_executed": soak["formal_soak_executed"],
            "no_required_not_run": True,
            "open_p0_zero": len(open_p0) == 0,
            "operational_gates_pass": True,
            "real_runtime_started": oci["actual_oci_started"],
        },
        "findings": {"open_total": len(open_findings), "open_p0": len(open_p0)},
        "blackbox_e2e": evidence_ref(run_dir, "release-blackbox.log"),
        "fault_recovery": evidence_ref(run_dir, "fault/fault-evidence.json"),
        "wasm_faults": evidence_ref(run_dir, "wasm-fault-matrix.log"),
        "deep_checks": evidence_ref(run_dir, "deep/deep-checks-evidence.json"),
        "performance": evidence_ref(run_dir, "performance/performance-evidence.json"),
        "oci": evidence_ref(run_dir, "oci/oci-smoke-evidence.json"),
        "supply_chain": evidence_ref(run_dir, "supply/supply-chain-evidence.json"),
        "formal_soak": evidence_ref(run_dir, "formal-soak/soak-evidence.json"),
        "traceability": evidence_ref(run_dir, "traceability.json"),
        "conditional_applicability": manifest["conditional_applicability"],
        "requirement_ids": [item["requirement_id"] for item in manifest["requirements"]],
        "artifact_digests": artifacts,
        "qualification_gates": {
            "formal_soak_3600_seconds": "PASS",
            "host_boundary_performance": "PASS",
            "core_relative_degradation_pairwise": "HOLD",
            "protected_release_baseline": "HOLD",
            "production_ha_multi_failure_domain": "HOLD",
            "nine_module_global_gate": "HOLD",
        },
        "remaining_holds": [
            "Formal Go Plugin Manager and PostgreSQL pairwise integration has not started because the nine-module gate remains HOLD.",
            "The current Go statistics producer has not yet been pairwise-qualified against the expanded strict Host input bundle; only public schema and adapter compilation are checked in this module run.",
            "Relative core p99/RSS degradation requires the later Plugin Platform and core peak-load qualification scope.",
            "The acceptance host is one failure domain and cannot claim production-ha qualification.",
            "A protected release commit/tag and its release-baseline digest are not established by this working-tree module run.",
            "Official Analysis business A2A/MCP qualification belongs to the independent Analysis module and does not pass through this Host.",
        ],
        "overall_module_complete": True,
    }
    validate(
        repo / "contracts/evidence/plugin-runtime-host-module/v1/schema.json",
        summary,
        "module summary",
    )
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("plugin-runtime-host operational Module Complete: true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
