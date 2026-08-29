#!/usr/bin/env python3
"""Persist a formal-module preflight failure before the module runner starts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from verify_module_gate_output import sha256_bytes

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from testkit.p4_switch.lib.source_identity import current_source_identity


MODULE_PATHS = {
    "p4": ("evidence/p4-switch", "p4"),
    "edge": ("edge-rs/evidence/module-gates/runs", "edge-rs"),
    "inference": ("infer-cpp/evidence/module-gates/runs", "infer-cpp"),
    "control": ("control-go/evidence/module-gates/runs", "control-go"),
    "db": ("db/evidence/module-gates/runs", "db"),
    "plugin-host": (
        "plugin-host-rs/evidence/module-gates/runs",
        "plugin-host-rs",
    ),
    "analysis": ("analysis-py/evidence/module-gates/runs", "analysis-py"),
    "offline-ml": ("ml-py/evidence/module-gates/runs", "ml-py"),
    "web": ("web/evidence/module-gates/runs", "web"),
}
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
MAX_LOG_BYTES = 64 * 1024 * 1024


def run_git(repo: Path, arguments: list[str]) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        timeout=60,
    ).stdout


def read_external_log(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_LOG_BYTES:
            raise ValueError("preflight log is not a bounded regular file")
        payload = bytearray()
        while True:
            chunk = os.read(
                descriptor, min(1024 * 1024, MAX_LOG_BYTES + 1 - len(payload))
            )
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_LOG_BYTES:
                raise ValueError("preflight log exceeds 64 MiB")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("preflight log changed while being read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def write_new(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--module", choices=sorted(MODULE_PATHS), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    if not RUN_ID_PATTERN.fullmatch(arguments.run_id):
        raise ValueError("invalid preflight failure run id")
    if arguments.exit_code < 1 or arguments.exit_code > 255 or arguments.exit_code == 2:
        raise ValueError("preflight failure exit code must be 1..255 except 2")
    relative_root, working_directory = MODULE_PATHS[arguments.module]
    run_dir = repo / relative_root / arguments.run_id
    log_payload = read_external_log(arguments.log)
    status = run_git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if arguments.module == "p4":
        identity = current_source_identity(repo)
        source_tree_digest = str(identity["source_tree_digest"])
        status_digest = str(identity["working_tree_status_digest"])
    else:
        source_tree_digest = sha256_bytes(run_git(repo, ["ls-files", "-s", "-z"]))
        status_digest = sha256_bytes(status)
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    if run_dir.is_symlink():
        raise ValueError("preflight failure run directory cannot be a symlink")
    log_path = run_dir / "preflight.log"
    write_new(log_path, log_payload)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    command = {
        "schema_version": "edge-command-execution/v1",
        "run_id": arguments.run_id,
        "command_id": "preflight",
        "started_at": generated_at,
        "finished_at": generated_at,
        "duration_ms": 0,
        "working_directory": working_directory,
        "argv": ["module-gates-preflight", arguments.module],
        "exit_code": arguments.exit_code,
        "result": "FAIL",
        "qualification": "NOT_QUALIFIED",
        "stable_reason": "COMMAND_EXITED_FAILURE",
        "source_tree_digest": source_tree_digest,
        "working_tree_status_digest": status_digest,
        "log": {
            "path": "preflight.log",
            "sha256": sha256_bytes(log_payload),
            "bytes": len(log_payload),
            "media_type": "text/plain",
        },
    }
    sidecar = run_dir / "preflight.json"
    write_new(
        sidecar,
        (json.dumps(command, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("write_module_failure.py")),
            "--repo",
            str(repo),
            "--module",
            arguments.module,
            "--run-dir",
            str(run_dir),
            "--command-sidecar",
            sidecar.name,
            "--output",
            str(run_dir / "gate-failure.json"),
        ],
        check=True,
        timeout=60,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
