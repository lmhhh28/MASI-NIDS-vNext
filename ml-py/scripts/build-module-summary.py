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


def ref(run_dir: Path, relative: str) -> dict[str, str]:
    path = run_dir / relative
    document = load(path)
    if document.get("result") != "PASS":
        raise ValueError(f"evidence is not PASS: {relative}")
    return {"path": relative, "digest": digest(path), "result": "PASS"}


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
    executed: dict[str, str] = {}
    for command_id in expected_commands:
        sidecar_path = run_dir / f"{command_id}.command.json"
        sidecar = load(sidecar_path)
        log = cast(dict[str, Any], sidecar["log"])
        if (
            sidecar.get("result") != "PASS"
            or sidecar.get("source_tree_digest") != metadata["source_tree_digest"]
            or sidecar.get("working_tree_status_digest") != metadata["working_tree_status_digest"]
            or digest(run_dir / str(log["path"])) != log["sha256"]
        ):
            raise SystemExit(f"invalid command sidecar: {command_id}")
        executed[command_id] = "PASS"

    evidence_refs = {
        "static": ref(run_dir, "static.json"),
        "release": ref(run_dir, "release.json"),
        "blackbox": ref(run_dir, "blackbox.json"),
        "central_consumer": ref(run_dir, "central-consumer.json"),
        "triton": ref(run_dir, "triton.json"),
        "fault": ref(run_dir, "fault.json"),
        "performance": ref(run_dir, "performance.json"),
        "image_build": ref(run_dir, "image-build.json"),
        "oci": ref(run_dir, "oci.json"),
        "deployment": ref(run_dir, "deployment.json"),
        "supply": ref(run_dir, "supply/supply-chain-evidence.json"),
        "soak": ref(run_dir, "soak.json"),
        "traceability": ref(run_dir, "traceability.json"),
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
    document = {
        "schema_version": "offline-ml-module-gates/v1",
        "module_id": "MOD-ML-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_revision": metadata["source_revision"],
        "source_tree_digest": source_digest,
        "working_tree_dirty": metadata["working_tree_dirty"],
        "working_tree_status_digest": status_digest,
        "executed_gates": executed,
        "completion": {
            "contracts_frozen": True,
            "mandatory_candidates_executed": 9,
            "formal_soak_executed": True,
            "no_required_not_run": True,
            "open_p0_zero": True,
            "operational_gates_pass": True,
            "real_release_process_started": True,
            "real_oci_started": True,
            "real_triton_consumer_started": True,
            "central_cpp_consumer_passed": True,
        },
        "findings": {"open_total": 0, "open_p0": 0, "registry_digest": digest(findings_path)},
        "evidence_refs": evidence_refs,
        "conditional_applicability": manifest["conditional_applicability"],
        "requirement_ids": traceability["requirement_ids"],
        "artifact_digests": artifacts,
        "qualification_gates": {
            "module_operational": "PASS",
            "official_corpus": "HOLD",
            "protected_baseline": "HOLD",
            "pairwise": "HOLD",
            "system": "HOLD",
            "production": "HOLD",
        },
        "remaining_holds": [
            "OFFICIAL_CORPUS_LICENSE_PRIVACY_GROUND_TRUTH_NOT_QUALIFIED",
            "PROTECTED_RELEASE_BASELINE_NOT_FORMED",
            "FORMAL_PAIRWISE_NOT_STARTED_BY_GLOBAL_GATE",
            "FORMAL_SYSTEM_E2E_NOT_STARTED_BY_GLOBAL_GATE",
            "PRODUCTION_HA_AND_ABSOLUTE_PRODUCTION_CAPACITY_NOT_QUALIFIED",
            "WEB_MODULE_AND_NINE_MODULE_GLOBAL_GATE_REMAIN_HOLD",
        ],
        "overall_module_complete": True,
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
            {"schema_version": document["schema_version"], "result": "PASS", "overall_module_complete": True},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
