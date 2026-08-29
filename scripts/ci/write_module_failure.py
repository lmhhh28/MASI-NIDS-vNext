#!/usr/bin/env python3
"""Create bounded, schema-validated failure evidence from a command sidecar."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from verify_module_gate_output import decode_json, read_file, sha256_bytes

MODULE_IDS = {
    "p4": "MOD-SW-001",
    "edge": "MOD-EDGE-001",
    "inference": "MOD-INF-001",
    "control": "MOD-CTRL-001",
    "db": "MOD-DB-001",
    "plugin-host": "MOD-PLUGIN-001",
    "analysis": "MOD-AGENT-001",
    "offline-ml": "MOD-ML-001",
    "web": "MOD-WEB-001",
}
ZERO_DIGEST = "sha256:" + "0" * 64


def safe_run_path(run_dir: Path, relative: str) -> Path:
    path = Path(relative)
    if (
        path.is_absolute()
        or relative != path.as_posix()
        or ".." in path.parts
        or "\\" in relative
    ):
        raise ValueError("evidence path escapes the run directory")
    return run_dir / path


def validate(schema: dict[str, Any], document: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            document
        ),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ValueError(
            "; ".join(
                f"{list(error.absolute_path)}: {error.message}" for error in errors
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--module", choices=sorted(MODULE_IDS), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--command-sidecar", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    run_dir = arguments.run_dir.resolve(strict=True)
    sidecar_path, sidecar_payload = read_file(
        repo, safe_run_path(run_dir, arguments.command_sidecar)
    )
    sidecar = decode_json(sidecar_path, sidecar_payload)
    command_schema_path, command_schema_payload = read_file(
        repo, repo / "contracts/evidence/command/v1/schema.json"
    )
    command_schema = decode_json(command_schema_path, command_schema_payload)
    validate(command_schema, sidecar)
    if sidecar.get("run_id") != run_dir.name:
        raise ValueError("command sidecar run_id differs from immutable run directory")
    exit_code = sidecar.get("exit_code")
    if not isinstance(exit_code, int) or exit_code == 0:
        raise ValueError("failure evidence requires a non-zero command exit code")
    log_relative = sidecar.get("log", {}).get("path")
    if not isinstance(log_relative, str):
        raise ValueError("command sidecar has no log path")
    log_path, log_payload = read_file(repo, safe_run_path(run_dir, log_relative))
    if sha256_bytes(log_payload) != sidecar["log"].get("sha256"):
        raise ValueError("command log digest mismatch")
    source_digest = sidecar.get("source_tree_digest")
    status_digest = sidecar.get("working_tree_status_digest")
    if source_digest == ZERO_DIGEST or status_digest == ZERO_DIGEST:
        raise ValueError("failure evidence cannot use all-zero snapshot digests")
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=30,
    ).stdout.strip()
    result = "HOLD" if exit_code == 2 and sidecar.get("result") == "HOLD" else "FAIL"
    stable_reason = sidecar.get("stable_reason") or (
        "COMMAND_EXITED_HOLD" if result == "HOLD" else "COMMAND_EXITED_FAILURE"
    )
    document = {
        "schema_version": "module-runner-failure/v1",
        "module": arguments.module,
        "module_id": MODULE_IDS[arguments.module],
        "run_id": run_dir.name,
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "failed_command_id": sidecar["command_id"],
        "exit_code": exit_code,
        "result": result,
        "qualification": "NOT_QUALIFIED",
        "stable_reason": stable_reason,
        "source_revision": revision,
        "source_tree_digest": source_digest,
        "working_tree_status_digest": status_digest,
        "command_evidence": {
            "path": arguments.command_sidecar,
            "digest": sha256_bytes(sidecar_payload),
            "bytes": len(sidecar_payload),
        },
        "log_evidence": {
            "path": log_relative,
            "digest": sha256_bytes(log_payload),
            "bytes": len(log_payload),
        },
        "overall_module_complete": False,
    }
    schema_path, schema_payload = read_file(
        repo, repo / "contracts/evidence/module-runner-failure/v1/schema.json"
    )
    schema = decode_json(schema_path, schema_payload)
    validate(schema, document)
    output = arguments.output.absolute()
    if output.parent != run_dir or output.exists() or output.is_symlink():
        raise ValueError(
            "failure evidence output must be a new direct child of the run directory"
        )
    descriptor = os.open(
        output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
    )
    try:
        payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
