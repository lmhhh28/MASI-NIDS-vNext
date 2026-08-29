#!/usr/bin/env python3
"""Derive Offline ML operational Module Complete from current digest-bound evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not an ordinary file: {path}")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate(schema_path: Path, document: dict[str, Any], label: str) -> None:
    schema = load(schema_path)
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document))
    if errors:
        raise ValueError(
            f"{label} schema rejected: "
            + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors)
        )


def ref(repo: Path, run_dir: Path, relative: str, schema_relative: str) -> dict[str, str]:
    path = run_dir / relative
    document = load(path)
    validate(repo / "contracts/evidence" / schema_relative, document, relative)
    result = document.get("result")
    qualification = document.get("qualification")
    if result != "PASS" or qualification not in {"QUALIFIED", "NOT_QUALIFIED"}:
        raise ValueError(f"evidence is not a valid PASS: {relative}")
    return {
        "path": relative,
        "digest": digest(path),
        "result": result,
        "qualification": qualification,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    run_dir = arguments.run_dir.resolve(strict=True)
    if arguments.output.exists() or arguments.output.is_symlink():
        raise SystemExit("module summary output already exists")
    metadata = load(run_dir / "run-metadata.json")
    manifest = load(repo / "ml-py/requirements-traceability.json")
    expected_commands = [str(item["command_id"]) for item in manifest["execution_bindings"] if item["required"]]
    command_schema = repo / "contracts/evidence/command/v1/schema.json"
    executed: dict[str, str] = {}
    for command_id in expected_commands:
        sidecar_path = run_dir / f"{command_id}.command.json"
        sidecar = load(sidecar_path)
        validate(command_schema, sidecar, f"command {command_id}")
        log = cast(dict[str, Any], sidecar["log"])
        if (
            sidecar.get("result") != "PASS"
            or sidecar.get("source_tree_digest") != metadata["source_tree_digest"]
            or sidecar.get("working_tree_status_digest") != metadata["working_tree_status_digest"]
            or digest(run_dir / str(log["path"])) != log["sha256"]
        ):
            raise SystemExit(f"invalid command sidecar: {command_id}")
        executed[command_id] = str(sidecar["result"])

    evidence_refs = {
        "static": ref(repo, run_dir, "static.json", "offline-ml-static/v1/schema.json"),
        "release": ref(repo, run_dir, "release.json", "offline-ml-release/v1/schema.json"),
        "blackbox": ref(repo, run_dir, "blackbox.json", "offline-ml-blackbox/v1/schema.json"),
        "central_consumer": ref(repo, run_dir, "central-consumer.json", "offline-ml-central-consumer/v1/schema.json"),
        "triton": ref(repo, run_dir, "triton.json", "offline-ml-triton/v1/schema.json"),
        "fault": ref(repo, run_dir, "fault.json", "offline-ml-fault/v1/schema.json"),
        "performance": ref(repo, run_dir, "performance.json", "offline-ml-performance/v1/schema.json"),
        "image_build": ref(repo, run_dir, "image-build.json", "offline-ml-image-build/v1/schema.json"),
        "oci": ref(repo, run_dir, "oci.json", "offline-ml-oci/v1/schema.json"),
        "deployment": ref(repo, run_dir, "deployment.json", "offline-ml-deployment/v1/schema.json"),
        "supply": ref(repo, run_dir, "supply/supply-chain-evidence.json", "offline-ml-supply/v1/schema.json"),
        "soak": ref(repo, run_dir, "soak.json", "offline-ml-soak/v1/schema.json"),
        "traceability": ref(repo, run_dir, "traceability.json", "traceability/v1/schema.json"),
    }
    static = load(run_dir / "static.json")
    release = load(run_dir / "release.json")
    blackbox = load(run_dir / "blackbox.json")
    central = load(run_dir / "central-consumer.json")
    triton = load(run_dir / "triton.json")
    fault = load(run_dir / "fault.json")
    performance = load(run_dir / "performance.json")
    image = load(run_dir / "image-build.json")
    oci = load(run_dir / "oci.json")
    supply = load(run_dir / "supply/supply-chain-evidence.json")
    soak = load(run_dir / "soak.json")
    traceability = load(run_dir / "traceability.json")
    findings_path = repo / "ml-py/module-findings.json"
    findings = load(findings_path)

    source_digest = str(metadata["source_tree_digest"])
    status_digest = str(metadata["working_tree_status_digest"])
    if any(
        value != source_digest
        for value in (
            static["source_tree_digest"],
            release["source_tree_digest"],
            image["source_tree_digest"],
            oci["source_tree_digest"],
            supply["source_tree_digest"],
            soak["source_tree_digest_start"],
            soak["source_tree_digest_end"],
        )
    ):
        raise SystemExit("source-tree digest differs across evidence")
    if any(
        value != status_digest
        for value in (
            release["working_tree_status_digest"],
            image["working_tree_status_digest"],
            supply["working_tree_status_digest"],
        )
    ):
        raise SystemExit("working-tree status digest differs across evidence")
    if not (image["image_id"] == oci["image_id"] == supply["image_id"]):
        raise SystemExit("image identity differs across evidence")

    immutable = cast(dict[str, str], blackbox["immutable_identity"])
    model_digest = immutable["model_digest"]
    closure_digest = immutable["repository_closure_digest"]
    archive_digest = immutable["archive_digest"]
    expected_oci_identity = cast(dict[str, str], oci["expected_immutable_identity"])
    if oci.get("cross_runtime_identity_match") is not True or expected_oci_identity != {
        "archive_digest": archive_digest,
        "model_digest": model_digest,
        "repository_closure_digest": closure_digest,
    }:
        raise SystemExit("OCI/release cross-runtime identity evidence is invalid")
    if (
        cast(dict[str, Any], central["consumer_result"])["model_digest"] != model_digest
        or cast(dict[str, Any], central["consumer_result"])["repository_closure_digest"] != closure_digest
        or triton["model_digest"] != model_digest
        or triton["repository_closure_digest"] != closure_digest
        or oci["model_digest"] != model_digest
        or oci["repository_closure_digest"] != closure_digest
        or oci["archive_digest"] != archive_digest
        or cast(dict[str, Any], fault["artifact_identity"])["model_digest"] != model_digest
        or cast(dict[str, Any], fault["artifact_identity"])["repository_closure_digest"] != closure_digest
        or cast(dict[str, Any], fault["artifact_identity"])["archive_digest"] != archive_digest
        or cast(dict[str, Any], soak["artifact_identity"])["model_digest"] != model_digest
        or cast(dict[str, Any], soak["artifact_identity"])["repository_closure_digest"] != closure_digest
        or cast(dict[str, Any], soak["artifact_identity"])["archive_digest"] != archive_digest
    ):
        raise SystemExit("immutable artifact identity differs across evidence")
    for run in performance["runs"]:
        if (
            run["model_digest"] != model_digest
            or run["repository_closure_digest"] != closure_digest
            or run["archive_digest"] != archive_digest
        ):
            raise SystemExit("performance run artifact identity drift")
    if soak.get("mode") != "formal" or soak.get("configured_qualified_seconds") != 3600:
        raise SystemExit("formal soak evidence is absent or shortened")
    opened = [item for item in findings["findings"] if item["status"] == "OPEN"]
    if opened:
        raise SystemExit("open findings prevent operational completion")
    if traceability.get("valid") is not True or len(traceability["requirement_ids"]) != len(manifest["requirements"]):
        raise SystemExit("traceability evidence is incomplete")

    artifacts = {
        "source_tree": source_digest,
        "working_tree_status": status_digest,
        "lock": digest(repo / "ml-py/uv.lock"),
        "findings_registry": digest(findings_path),
        "requirements_manifest": digest(repo / "ml-py/requirements-traceability.json"),
        "dataset_profile": digest(repo / "contracts/profiles/v1/dataset-p4-window-binary.json"),
        "p4_target_profile": digest(repo / "contracts/profiles/v1/p4-stateless-firewall-bmv2.json"),
        "p4_source": digest(repo / "p4/src/masi_switch.p4"),
        "explanation_profile": digest(repo / "contracts/profiles/v1/model-explanation-evidence.json"),
        "runtime_profile": digest(repo / "contracts/profiles/v1/offline-ml-runtime.json"),
        "performance_profile": digest(repo / "contracts/profiles/v1/offline-ml-performance.json"),
        "soak_profile": digest(repo / "contracts/profiles/v1/offline-ml-soak.json"),
        "release_wheel": release["wheel_digest"],
        "entrypoint": release["entrypoint_digest"],
        "image": image["image_id"],
        "model": model_digest,
        "model_revision": immutable["model_revision_digest"],
        "repository_closure": closure_digest,
        "repository_archive": archive_digest,
        "central_validator": central["validator_digest"],
        "triton_image": triton["triton_image_id"],
        "supply_manifest": supply["manifest_digest"],
    }
    contracts_frozen = all(static["checks"].values()) and static["contract_validation"]["result"] == "PASS"
    mandatory_candidates = int(blackbox["candidate_executions_per_run"])
    completion = {
        "contracts_frozen": contracts_frozen,
        "mandatory_candidates_executed": mandatory_candidates,
        "formal_soak_executed": soak["mode"] == "formal"
        and soak["configured_qualified_seconds"] == 3600
        and soak["result"] == "PASS",
        "no_required_not_run": all(result == "PASS" for result in executed.values())
        and all(reference["result"] == "PASS" for reference in evidence_refs.values()),
        "open_p0_zero": not any(item["severity"] == "P0" for item in opened),
        "operational_gates_pass": all(result == "PASS" for result in executed.values()),
        "real_release_process_started": blackbox["real_release_process"] is True,
        "real_oci_started": isinstance(oci.get("container_id"), str)
        and bool(oci["container_id"])
        and oci["full_pipeline_exit_code"] == 0,
        "real_triton_consumer_started": isinstance(triton.get("container_id"), str)
        and bool(triton["container_id"])
        and triton["strict_readiness"] is True,
        "central_cpp_consumer_passed": central["consumer_result"]["result"] == "PASS",
    }
    overall_module_complete = (
        all(
            value == 9 if key == "mandatory_candidates_executed" else value is True for key, value in completion.items()
        )
        and not opened
    )
    if not overall_module_complete:
        raise SystemExit("Offline ML operational completion could not be derived")
    qualification_gates = {
        "module_operational": "PASS" if completion["operational_gates_pass"] else "HOLD",
        "official_corpus": "HOLD",
        "protected_baseline": "HOLD",
        "pairwise": "HOLD",
        "system": "HOLD",
        "production": "HOLD",
    }
    document = {
        "schema_version": "offline-ml-module-gates/v1",
        "module_id": "MOD-ML-001",
        "run_id": str(metadata["run_id"]),
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "HOLD" if "HOLD" in qualification_gates.values() else "PASS",
        "qualification": "NOT_QUALIFIED",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_revision": metadata["source_revision"],
        "source_tree_digest": source_digest,
        "working_tree_dirty": metadata["working_tree_dirty"],
        "working_tree_status_digest": status_digest,
        "executed_gates": executed,
        "completion": completion,
        "findings": {
            "open_total": len(opened),
            "open_p0": sum(item["severity"] == "P0" for item in opened),
            "registry_digest": digest(findings_path),
        },
        "evidence_refs": evidence_refs,
        "conditional_applicability": manifest["conditional_applicability"],
        "requirement_ids": traceability["requirement_ids"],
        "artifact_digests": artifacts,
        "qualification_gates": qualification_gates,
        "remaining_holds": [
            "OFFICIAL_CORPUS_LICENSE_PRIVACY_GROUND_TRUTH_NOT_QUALIFIED",
            "PROTECTED_RELEASE_BASELINE_NOT_FORMED",
            "FORMAL_PAIRWISE_NOT_STARTED_BY_GLOBAL_GATE",
            "FORMAL_SYSTEM_E2E_NOT_STARTED_BY_GLOBAL_GATE",
            "PRODUCTION_HA_AND_ABSOLUTE_PRODUCTION_CAPACITY_NOT_QUALIFIED",
            "WEB_MODULE_AND_NINE_MODULE_GLOBAL_GATE_REMAIN_HOLD",
        ],
        "overall_module_complete": overall_module_complete,
    }
    schema = load(repo / "contracts/evidence/offline-ml-module/v1/schema.json")
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document))
    if errors:
        raise SystemExit(
            "module summary schema rejected: "
            + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors)
        )
    current_source = (
        "sha256:"
        + subprocess.run(
            [
                str(repo / "ml-py/.venv/bin/python"),
                str(repo / "ml-py/scripts/source-tree-digest.py"),
                "--repo",
                str(repo),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
    )
    if current_source != source_digest:
        raise SystemExit("current source changed before summary publication")
    arguments.output.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "schema_version": document["schema_version"],
                "result": document["result"],
                "overall_module_complete": overall_module_complete,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
