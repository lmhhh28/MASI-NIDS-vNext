#!/usr/bin/env python3
"""Build exact Analysis requirement-to-command evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular no-symlink file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def artifact(path: Path, display: str) -> dict[str, Any]:
    return {
        "path": display,
        "sha256": digest(path),
        "bytes": path.stat().st_size,
        "media_type": "application/json" if path.suffix == ".json" else "text/plain",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    run_dir = args.run_dir.resolve(strict=True)
    manifest_path = args.manifest.resolve(strict=True)
    if args.output.exists() or args.output.is_symlink():
        raise SystemExit("refusing to overwrite traceability evidence")
    manifest = load(manifest_path)
    if manifest.get("module_id") != "MOD-AGENT-001":
        raise SystemExit("traceability manifest module mismatch")
    bindings = {item["command_id"]: item for item in manifest["execution_bindings"]}
    if len(bindings) != len(manifest["execution_bindings"]):
        raise SystemExit("duplicate traceability command id")
    baseline = (repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md").read_text(encoding="utf-8")
    requirement_ids = [item["requirement_id"] for item in manifest["requirements"]]
    if len(requirement_ids) != len(set(requirement_ids)) or any(value not in baseline for value in requirement_ids):
        raise SystemExit("traceability requirement identity set is duplicate or absent from baseline")

    commands: dict[str, dict[str, Any]] = {}
    for command_id, binding in bindings.items():
        if not binding.get("required"):
            continue
        sidecar = load(run_dir / f"{command_id}.command.json")
        if sidecar.get("command_id") != command_id or sidecar.get("result") != "PASS":
            raise SystemExit(f"required command is not PASS: {command_id}")
        log_path = run_dir / sidecar["log"]["path"]
        if digest(log_path) != sidecar["log"]["sha256"]:
            raise SystemExit(f"command log digest mismatch: {command_id}")
        commands[command_id] = sidecar

    requirements = []
    for item in manifest["requirements"]:
        targets = []
        evidence = []
        for command_id in item["command_ids"]:
            if command_id not in commands:
                raise SystemExit(f"unknown or missing command for {item['requirement_id']}: {command_id}")
            sidecar = commands[command_id]
            targets.extend(
                {"target": target, "command_id": command_id, "result": "PASS", "qualification": "QUALIFIED"} for target in bindings[command_id]["targets"]
            )
            evidence.append(
                {
                    "path": sidecar["log"]["path"],
                    "kind": "execution-log",
                    "schema_version": None,
                    "test_id": item["scenario_ids"][0],
                    "requirement_ids": [item["requirement_id"]],
                    "result": "PASS",
                    "qualification": "QUALIFIED",
                    "command_id": command_id,
                    "command_evidence_path": f"{command_id}.command.json",
                    "command_evidence_digest": digest(run_dir / f"{command_id}.command.json"),
                    "log_digest": sidecar["log"]["sha256"],
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

    artifacts = [
        artifact(manifest_path, "analysis-py/requirements-traceability.json"),
        artifact(repo / "analysis-py/module-findings.json", "analysis-py/module-findings.json"),
    ]
    for command_id in sorted(commands):
        artifacts.append(artifact(run_dir / f"{command_id}.command.json", f"{command_id}.command.json"))
        artifacts.append(artifact(run_dir / f"{command_id}.log", f"{command_id}.log"))
    document = {
        "schema_version": "analysis-plugin-requirement-traceability/v1",
        "module_id": "MOD-AGENT-001",
        "manifest": "analysis-py/requirements-traceability.json",
        "manifest_digest": digest(manifest_path),
        "evidence_root": str(run_dir.relative_to(repo)),
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
        "requirements": requirements,
        "requirement_ids": requirement_ids,
        "conditional_applicability": manifest["conditional_applicability"],
        "artifacts": artifacts,
        "failures": [],
        "valid": True,
    }
    schema = load(repo / "contracts/evidence/traceability/v1/schema.json")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise SystemExit("traceability schema rejected: " + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors))
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"analysis traceability requirements: {len(requirements)}")


if __name__ == "__main__":
    main()
