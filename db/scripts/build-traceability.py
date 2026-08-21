#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 67_108_864:
        raise ValueError(f"unsafe input: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not an object: {path}")
    return value


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-directory", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = read(args.manifest)
    bindings = {item["command_id"]: item for item in manifest["execution_bindings"]}
    sidecars: dict[str, dict[str, Any]] = {}
    for command_id, binding in bindings.items():
        path = args.run_directory / f"{command_id}.command.json"
        if binding["required"] and not path.is_file():
            raise ValueError(f"required command evidence missing: {command_id}")
        if path.is_file():
            sidecars[command_id] = read(path)
    requirements = []
    all_pass = True
    for item in manifest["requirements"]:
        targets, evidence = [], []
        for command_id in item["command_ids"]:
            sidecar = sidecars.get(command_id)
            if sidecar is None:
                raise ValueError(f"requirement references missing command: {command_id}")
            result, qualification = sidecar["result"], sidecar["qualification"]
            all_pass = all_pass and result == "PASS"
            targets.append({
                "target": ",".join(bindings[command_id]["targets"]),
                "command_id": command_id,
                "result": result,
                "qualification": qualification,
            })
            evidence.append({
                "path": f"{command_id}.command.json",
                "kind": "json-evidence",
                "schema_version": sidecar["schema_version"],
                "test_id": item["scenario_ids"][0],
                "requirement_ids": [item["requirement_id"]],
                "result": result,
                "qualification": qualification,
            })
        requirements.append({
            "requirement_id": item["requirement_id"],
            "scenario_ids": item["scenario_ids"],
            "test_targets": targets,
            "evidence": evidence,
            "evidence_result": "PASS" if all(target["result"] == "PASS" for target in targets) else "FAIL",
            "qualification": "QUALIFIED" if all(target["result"] == "PASS" for target in targets) else "NOT_QUALIFIED",
            "qualification_limit": item["qualification_limit"],
        })
    artifacts = [{
        "path": "db/requirements-traceability.json",
        "sha256": digest(args.manifest),
        "bytes": args.manifest.stat().st_size,
        "media_type": "application/json",
    }]
    for command_id in sorted(sidecars):
        path = args.run_directory / f"{command_id}.command.json"
        artifacts.append({
            "path": f"{command_id}.command.json",
            "sha256": digest(path),
            "bytes": path.stat().st_size,
            "media_type": "application/json",
        })
    document = {
        "schema_version": "postgresql-state-requirement-traceability/v1",
        "module_id": "MOD-DB-001",
        "manifest": "db/requirements-traceability.json",
        "manifest_digest": digest(args.manifest),
        "evidence_root": str(args.run_directory),
        "result": "PASS" if all_pass else "FAIL",
        "qualification": "QUALIFIED" if all_pass else "NOT_QUALIFIED",
        "requirements": requirements,
        "requirement_ids": [item["requirement_id"] for item in manifest["requirements"]],
        "conditional_applicability": manifest["conditional_applicability"],
        "artifacts": artifacts,
        "failures": [] if all_pass else ["one or more required command gates did not pass"],
        "valid": all_pass,
    }
    if args.output.exists() or args.output.is_symlink() or not args.output.is_absolute():
        raise ValueError("output must be a fresh absolute path")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
