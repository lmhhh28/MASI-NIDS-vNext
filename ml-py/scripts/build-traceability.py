#!/usr/bin/env python3
"""Build digest-bound Offline ML requirement traceability from command evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not an ordinary file: {path}")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    run_dir = arguments.run_dir.resolve(strict=True)
    if arguments.output.exists() or arguments.output.is_symlink():
        raise SystemExit("traceability output already exists")
    manifest_path = repo / "ml-py/requirements-traceability.json"
    manifest = load(manifest_path)
    command_ids = [
        str(item["command_id"])
        for item in manifest["execution_bindings"]
        if item.get("required") is True and item["command_id"] != "traceability"
    ]
    command_evidence: dict[str, dict[str, Any]] = {}
    for command_id in command_ids:
        sidecar_path = run_dir / f"{command_id}.command.json"
        sidecar = load(sidecar_path)
        log_path = run_dir / str(cast(dict[str, Any], sidecar["log"])["path"])
        if sidecar.get("result") != "PASS" or digest(log_path) != cast(dict[str, Any], sidecar["log"])["sha256"]:
            raise SystemExit(f"command evidence invalid: {command_id}")
        command_evidence[command_id] = sidecar

    requirements: list[dict[str, Any]] = []
    for item in manifest["requirements"]:
        mapped = [command_id for command_id in item["command_ids"] if command_id != "traceability"]
        targets = [
            {
                "target": command_id,
                "command_id": command_id,
                "result": "PASS",
                "qualification": "QUALIFIED",
            }
            for command_id in mapped
        ]
        evidence = []
        for command_id in mapped:
            sidecar = command_evidence[command_id]
            evidence.append(
                {
                    "path": f"{command_id}.log",
                    "kind": "execution-log",
                    "schema_version": "edge-command-execution/v1",
                    "test_id": item["scenario_ids"][0],
                    "requirement_ids": [item["requirement_id"]],
                    "result": "PASS",
                    "qualification": "QUALIFIED",
                    "command_id": command_id,
                    "command_evidence_path": f"{command_id}.command.json",
                    "command_evidence_digest": digest(run_dir / f"{command_id}.command.json"),
                    "log_digest": cast(dict[str, Any], sidecar["log"])["sha256"],
                    "exit_code": 0,
                }
            )
        requirements.append(
            {
                "requirement_id": item["requirement_id"],
                "scenario_ids": item["scenario_ids"],
                "test_targets": targets,
                "evidence": evidence,
                "evidence_result": "PASS",
                "qualification": "QUALIFIED",
                "qualification_limit": item["qualification_limit"],
            }
        )

    artifacts: list[dict[str, Any]] = []
    artifact_paths = [
        run_dir / "static.json",
        run_dir / "release.json",
        run_dir / "blackbox.json",
        run_dir / "central-consumer.json",
        run_dir / "triton.json",
        run_dir / "fault.json",
        run_dir / "performance.json",
        run_dir / "image-build.json",
        run_dir / "oci.json",
        run_dir / "deployment.json",
        run_dir / "supply/supply-chain-evidence.json",
        run_dir / "soak.json",
        run_dir / "candidate-output/model-repository.tar",
    ]
    for path in artifact_paths:
        if path.is_file() and not path.is_symlink():
            artifacts.append(
                {
                    "path": path.relative_to(run_dir).as_posix(),
                    "sha256": digest(path),
                    "bytes": path.stat().st_size,
                    "media_type": "application/x-tar" if path.suffix == ".tar" else "application/json",
                }
            )
    document = {
        "schema_version": "offline-ml-requirement-traceability/v1",
        "module_id": "MOD-ML-001",
        "manifest": "ml-py/requirements-traceability.json",
        "manifest_digest": digest(manifest_path),
        "evidence_root": str(run_dir),
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
        "requirements": requirements,
        "requirement_ids": [item["requirement_id"] for item in requirements],
        "conditional_applicability": manifest["conditional_applicability"],
        "artifacts": artifacts,
        "failures": [],
        "valid": True,
    }
    schema = load(repo / "contracts/evidence/traceability/v1/schema.json")
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document))
    if errors:
        raise SystemExit(
            "traceability schema rejected: "
            + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors)
        )
    arguments.output.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"schema_version": document["schema_version"], "result": "PASS"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
