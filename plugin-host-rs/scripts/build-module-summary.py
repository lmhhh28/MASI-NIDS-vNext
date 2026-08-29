#!/usr/bin/env python3
"""Derive Plugin Host operational Module Complete from immutable gate evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from evidence_common import (
    current_snapshot,
    digest,
    load_json,
    reject_zero_digests,
    safe_output,
    safe_run_file,
    write_new_output,
)


def load(path: Path) -> Any:
    return load_json(path)


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
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            document
        ),
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


def reference_identity(
    run_dir: Path,
    producer_command_id: str,
    sidecars: dict[str, dict[str, Any]],
    source_revision: str,
    source_tree_digest: str,
    working_tree_status_digest: str,
) -> dict[str, str]:
    producer = sidecars[producer_command_id]
    if (
        producer.get("run_id") != run_dir.name
        or producer.get("source_tree_digest") != source_tree_digest
        or producer.get("working_tree_status_digest") != working_tree_status_digest
    ):
        raise ValueError(f"producer command identity mismatch: {producer_command_id}")
    return {
        "run_id": run_dir.name,
        "source_revision": source_revision,
        "source_tree_digest": source_tree_digest,
        "working_tree_status_digest": working_tree_status_digest,
        "producer_command_id": producer_command_id,
        "producer_command_digest": digest(
            safe_run_file(run_dir, f"{producer_command_id}.command.json")
        ),
    }


def json_evidence_ref(
    run_dir: Path,
    path: str,
    producer_command_id: str,
    sidecars: dict[str, dict[str, Any]],
    source_revision: str,
    source_tree_digest: str,
    working_tree_status_digest: str,
) -> dict[str, str]:
    target = safe_run_file(run_dir, path)
    document = load(target)
    result = document.get("result")
    qualification = document.get("qualification")
    if result != "PASS" or qualification not in {"QUALIFIED", "NOT_QUALIFIED"}:
        raise ValueError(f"required evidence is not a valid PASS: {path}")
    for key, expected in (
        ("source_revision", source_revision),
        ("source_tree_digest", source_tree_digest),
        ("working_tree_status_digest", working_tree_status_digest),
        ("run_id", run_dir.name),
    ):
        if key in document and document[key] != expected:
            raise ValueError(f"required evidence {key} mismatch: {path}")
    return {
        "evidence": path,
        "digest": digest(target),
        "result": result,
        "qualification": qualification,
        **reference_identity(
            run_dir,
            producer_command_id,
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
    }


def command_log_ref(
    run_dir: Path,
    path: str,
    producer_command_id: str,
    sidecars: dict[str, dict[str, Any]],
    source_revision: str,
    source_tree_digest: str,
    working_tree_status_digest: str,
) -> dict[str, str]:
    sidecar = sidecars[producer_command_id]
    if sidecar.get("result") != "PASS" or sidecar.get("qualification") != "QUALIFIED":
        raise ValueError(f"required command log is not PASS: {path}")
    return {
        "evidence": path,
        "digest": digest(safe_run_file(run_dir, path)),
        "result": str(sidecar["result"]),
        "qualification": str(sidecar["qualification"]),
        **reference_identity(
            run_dir,
            producer_command_id,
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    run_dir = args.run_dir.resolve(strict=True)
    output = safe_output(run_dir, args.output)
    manifest_path = repo / "plugin-host-rs/requirements-traceability.json"
    manifest = load(manifest_path)
    command_schema = repo / "contracts/evidence/command/v1/schema.json"
    command_ids = [item["command_id"] for item in manifest["execution_bindings"]]
    if (run_dir / "evidence-integrity.command.json").exists():
        command_ids.append("evidence-integrity")
    sidecars: dict[str, dict[str, Any]] = {}
    for command_id in command_ids:
        sidecar_path = safe_run_file(run_dir, f"{command_id}.command.json")
        sidecar = load(sidecar_path)
        validate(command_schema, sidecar, f"command {command_id}")
        if (
            sidecar["run_id"] != run_dir.name
            or sidecar["command_id"] != command_id
            or sidecar["result"] != "PASS"
        ):
            raise SystemExit(f"required command is not PASS: {command_id}")
        log = safe_run_file(run_dir, sidecar["log"]["path"])
        if digest(log) != sidecar["log"]["sha256"]:
            raise SystemExit(f"command log digest mismatch: {command_id}")
        sidecars[command_id] = sidecar
    source_digests = {sidecar["source_tree_digest"] for sidecar in sidecars.values()}
    status_digests = {
        sidecar["working_tree_status_digest"] for sidecar in sidecars.values()
    }
    if len(source_digests) != 1 or len(status_digests) != 1:
        raise SystemExit("command source/status digests disagree")
    source_tree_digest = next(iter(source_digests))
    working_tree_status_digest = next(iter(status_digests))

    deep = load(safe_run_file(run_dir, "deep/deep-checks-evidence.json"))
    fault = load(safe_run_file(run_dir, "fault/fault-evidence.json"))
    performance = load(safe_run_file(run_dir, "performance/performance-evidence.json"))
    oci = load(safe_run_file(run_dir, "oci/oci-smoke-evidence.json"))
    supply = load(safe_run_file(run_dir, "supply/supply-chain-evidence.json"))
    soak = load(safe_run_file(run_dir, "formal-soak/soak-evidence.json"))
    traceability = load(safe_run_file(run_dir, "traceability.json"))
    validate(
        repo / "contracts/evidence/plugin-runtime-host-deep/v1/schema.json",
        deep,
        "deep",
    )
    validate(
        repo / "contracts/evidence/plugin-runtime-host-fault/v1/schema.json",
        fault,
        "fault",
    )
    validate(
        repo / "contracts/evidence/plugin-runtime-host-performance/v1/schema.json",
        performance,
        "performance",
    )
    validate(
        repo / "contracts/evidence/plugin-runtime-host-oci/v1/schema.json", oci, "oci"
    )
    validate(
        repo / "contracts/evidence/plugin-runtime-host-supply/v1/schema.json",
        supply,
        "supply",
    )
    validate(
        repo / "contracts/evidence/plugin-runtime-host-soak/v1/schema.json",
        soak,
        "soak",
    )
    validate(
        repo / "contracts/evidence/traceability/v1/schema.json",
        traceability,
        "traceability",
    )
    evidence_documents = {
        "deep": deep,
        "fault": fault,
        "performance": performance,
        "oci": oci,
        "supply": supply,
        "soak": soak,
        "traceability": traceability,
    }
    for name, document in evidence_documents.items():
        if document.get("result") != "PASS" or document.get("qualification") not in {
            "QUALIFIED",
            "NOT_QUALIFIED",
        }:
            raise SystemExit(f"required {name} evidence is not PASS")
    if (
        oci["source_tree_digest"] != source_tree_digest
        or supply["source_tree_digest"] != source_tree_digest
    ):
        raise SystemExit("OCI/supply source digest does not match command snapshot")

    findings_path = repo / "plugin-host-rs/module-findings.json"
    findings = load(findings_path)
    validate(
        repo / "contracts/evidence/module-findings/v1/schema.json", findings, "findings"
    )
    open_findings = [item for item in findings["findings"] if item["status"] == "OPEN"]
    open_p0 = [item for item in open_findings if item["severity"] == "P0"]
    if open_p0:
        raise SystemExit("open P0 findings block operational completion")

    status_path = safe_run_file(run_dir, "working-tree-status.txt")
    if digest(status_path) != working_tree_status_digest:
        raise SystemExit("working tree status digest mismatch")
    (
        source_revision,
        current_status_digest,
        working_tree_dirty,
        current_source_digest,
    ) = current_snapshot(repo)
    if current_status_digest != working_tree_status_digest:
        raise SystemExit("working tree status changed during the module run")
    if current_source_digest != source_tree_digest:
        raise SystemExit("source tree changed during the module run")
    if (
        source_revision != oci["source_revision"]
        or source_revision != supply["source_revision"]
    ):
        raise SystemExit("source revision mismatch")

    artifacts = {
        "requirements_baseline": digest(
            repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md"
        ),
        "requirements_traceability_manifest": digest(manifest_path),
        "module_findings": digest(findings_path),
        "traceability_evidence": digest(safe_run_file(run_dir, "traceability.json")),
        "deep_checks_evidence": digest(
            safe_run_file(run_dir, "deep/deep-checks-evidence.json")
        ),
        "fault_recovery_evidence": digest(
            safe_run_file(run_dir, "fault/fault-evidence.json")
        ),
        "performance_evidence": digest(
            safe_run_file(run_dir, "performance/performance-evidence.json")
        ),
        "oci_evidence": digest(safe_run_file(run_dir, "oci/oci-smoke-evidence.json")),
        "supply_chain_evidence": digest(
            safe_run_file(run_dir, "supply/supply-chain-evidence.json")
        ),
        "formal_soak_evidence": digest(
            safe_run_file(run_dir, "formal-soak/soak-evidence.json")
        ),
        "host_binary": digest(repo / "plugin-host-rs/target/release/masi-plugin-host"),
        "hostctl_binary": digest(
            repo / "plugin-host-rs/target/release/masi-plugin-hostctl"
        ),
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
            safe_run_file(run_dir, f"{command_id}.command.json")
        )
        artifacts[f"log_{command_id}"] = digest(
            safe_run_file(run_dir, f"{command_id}.log")
        )
    executed_gates = {
        command_id: str(sidecar["result"])
        for command_id, sidecar in sorted(sidecars.items())
    }
    completion = {
        "formal_soak_executed": soak["formal_soak_executed"] is True,
        "no_required_not_run": all(
            result == "PASS" for result in executed_gates.values()
        )
        and all(
            document["result"] == "PASS" for document in evidence_documents.values()
        ),
        "open_p0_zero": len(open_p0) == 0,
        "operational_gates_pass": all(
            result == "PASS" for result in executed_gates.values()
        ),
        "real_runtime_started": oci["actual_oci_started"] is True,
    }
    overall_module_complete = all(completion.values()) and not open_findings
    if not overall_module_complete:
        raise SystemExit("Plugin Host operational completion could not be derived")
    qualification_gates = {
        "formal_soak_3600_seconds": soak["result"],
        "host_boundary_performance": performance["result"],
        "core_relative_degradation_pairwise": "HOLD",
        "protected_release_baseline": "HOLD",
        "production_ha_multi_failure_domain": "HOLD",
        "nine_module_global_gate": "HOLD",
    }
    summary = {
        "schema_version": "plugin-runtime-host-module-gates/v1",
        "module_id": "MOD-PLUGIN-001",
        "run_id": run_dir.name,
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "HOLD" if "HOLD" in qualification_gates.values() else "PASS",
        "qualification": "NOT_QUALIFIED",
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source_revision": source_revision,
        "source_tree_digest": source_tree_digest,
        "working_tree_dirty": working_tree_dirty,
        "working_tree_status_digest": working_tree_status_digest,
        "executed_gates": executed_gates,
        "completion": completion,
        "findings": {"open_total": len(open_findings), "open_p0": len(open_p0)},
        "blackbox_e2e": command_log_ref(
            run_dir,
            "release-blackbox.log",
            "release-blackbox",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "fault_recovery": json_evidence_ref(
            run_dir,
            "fault/fault-evidence.json",
            "release-blackbox",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "wasm_faults": command_log_ref(
            run_dir,
            "wasm-fault-matrix.log",
            "wasm-fault-matrix",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "deep_checks": json_evidence_ref(
            run_dir,
            "deep/deep-checks-evidence.json",
            "deep-checks",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "performance": json_evidence_ref(
            run_dir,
            "performance/performance-evidence.json",
            "performance",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "oci": json_evidence_ref(
            run_dir,
            "oci/oci-smoke-evidence.json",
            "oci-smoke",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "supply_chain": json_evidence_ref(
            run_dir,
            "supply/supply-chain-evidence.json",
            "supply-chain",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "formal_soak": json_evidence_ref(
            run_dir,
            "formal-soak/soak-evidence.json",
            "formal-soak",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "traceability": json_evidence_ref(
            run_dir,
            "traceability.json",
            "traceability",
            sidecars,
            source_revision,
            source_tree_digest,
            working_tree_status_digest,
        ),
        "conditional_applicability": manifest["conditional_applicability"],
        "requirement_ids": [
            item["requirement_id"] for item in manifest["requirements"]
        ],
        "artifact_digests": artifacts,
        "qualification_gates": qualification_gates,
        "remaining_holds": [
            "Formal Go Plugin Manager and PostgreSQL pairwise integration has not started because the nine-module gate remains HOLD.",
            "The current Go statistics producer has not yet been pairwise-qualified against the expanded strict Host input bundle; only public schema and adapter compilation are checked in this module run.",
            "Relative core p99/RSS degradation requires the later Plugin Platform and core peak-load qualification scope.",
            "The acceptance host is one failure domain and cannot claim production-ha qualification.",
            "A protected release commit/tag and its release-baseline digest are not established by this working-tree module run.",
            "Official Analysis business A2A/MCP qualification belongs to the independent Analysis module and does not pass through this Host.",
        ],
        "overall_module_complete": overall_module_complete,
    }
    zero_digests = reject_zero_digests(summary)
    if zero_digests:
        raise SystemExit(
            "module summary contains sentinel digests: " + "; ".join(zero_digests)
        )
    validate(
        repo / "contracts/evidence/plugin-runtime-host-module/v1/schema.json",
        summary,
        "module summary",
    )
    write_new_output(
        run_dir,
        output,
        (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode(),
    )
    print(
        f"plugin-runtime-host operational Module Complete: {str(overall_module_complete).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
