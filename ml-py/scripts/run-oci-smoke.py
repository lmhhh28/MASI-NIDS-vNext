#!/usr/bin/env python3
"""Run the complete Offline ML pipeline in a hardened non-root OCI job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, cast

if __name__ == "__main__" and os.environ.get("MASI_RUNTIME_SMOKE_WRAPPED") != "1":
    _repository = Path(__file__).resolve().parents[2]
    os.execv(
        sys.executable,
        [
            sys.executable,
            str(_repository / "scripts/ci/run_bounded_runtime_smoke.py"),
            "--repo",
            str(_repository),
            "--module",
            "offline-ml",
            "--timeout-seconds",
            os.environ.get("MASI_OFFLINE_ML_OCI_TOTAL_TIMEOUT_SECONDS", "7200"),
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            *sys.argv[1:],
        ],
    )


def command(arguments: list[str], *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def inspect(image: str) -> dict[str, Any]:
    result = command(["docker", "image", "inspect", image])
    return cast(list[dict[str, Any]], json.loads(result.stdout))[0]


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def safe_existing_file(path: Path, maximum_bytes: int) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("input file path traverses a symlink")
    if not absolute.is_file() or absolute.stat().st_size > maximum_bytes:
        raise ValueError("input is not a bounded regular file")
    return absolute


def prepare_new_output(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise ValueError("evidence output parent path is unsafe or absent")
    if absolute.exists() or absolute.is_symlink():
        raise ValueError("evidence output already exists")
    return absolute


def write_new_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
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
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-blackbox", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        evidence_path = prepare_new_output(arguments.evidence)
        expected_blackbox = safe_existing_file(arguments.expected_blackbox, 16 * 1024 * 1024)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    expected_document = cast(
        dict[str, Any],
        json.loads(
            expected_blackbox.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        ),
    )
    expected_identity = cast(dict[str, str], expected_document.get("immutable_identity", {}))
    required_identity = {"model_digest", "repository_closure_digest", "archive_digest"}
    if expected_document.get("result") != "PASS" or not required_identity.issubset(expected_identity):
        raise SystemExit("expected blackbox identity is incomplete")
    expected_identity = {name: expected_identity[name] for name in sorted(required_identity)}

    metadata = inspect(arguments.image)
    config = cast(dict[str, Any], metadata["Config"])
    labels = cast(dict[str, str], config.get("Labels", {}))
    if config.get("User") != "65532:65532":
        raise SystemExit("image user is not 65532:65532")
    if config.get("Entrypoint") != ["/usr/local/bin/masi-offline-ml"]:
        raise SystemExit("image entrypoint mismatch")

    temporary = Path(tempfile.mkdtemp(prefix="masi-offline-ml-oci."))
    os.chmod(temporary, 0o700)
    command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "16",
            "--memory",
            "64m",
            "--cpus",
            "0.25",
            "--mount",
            f"type=bind,src={temporary},dst=/output",
            "--entrypoint",
            "/usr/local/bin/python3.12",
            arguments.image,
            "-c",
            "import os; os.chmod('/output', 0o700); os.chown('/output', 65532, 65532)",
        ],
        timeout=60,
    )
    container = f"masi-offline-ml-smoke-{uuid.uuid4().hex[:12]}"
    try:
        version = command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "128",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=1g",
                arguments.image,
                "--version",
            ]
        ).stdout.strip()
        if version != "masi-offline-ml 1.0.0":
            raise SystemExit(f"version mismatch: {version}")

        run_command = [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "128",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=1g",
            "--mount",
            f"type=bind,src={temporary},dst=/output",
            arguments.image,
            "run",
            "--repo",
            "/opt/masi/repo",
            "--output",
            "/output/run",
        ]
        started = time.monotonic_ns()
        container_id = command(run_command).stdout.strip()
        running = cast(list[dict[str, Any]], json.loads(command(["docker", "inspect", container]).stdout))[0]
        host_config = cast(dict[str, Any], running["HostConfig"])
        if host_config.get("ReadonlyRootfs") is not True or host_config.get("NetworkMode") != "none":
            raise SystemExit("container isolation mismatch")
        if host_config.get("CapDrop") != ["ALL"] or host_config.get("PidsLimit") != 128:
            raise SystemExit("container capability or PID limit mismatch")
        exit_code = int(command(["docker", "wait", container], timeout=120).stdout.strip())
        elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
        logs = command(["docker", "logs", container]).stdout
        if exit_code != 0:
            raise SystemExit(f"OCI pipeline exited {exit_code}: {logs}")
        result = json.loads(logs.strip().splitlines()[-1])
        if result.get("result") != "PASS" or result.get("candidate_executions") != 9:
            raise SystemExit("OCI pipeline result invalid")
        command(["docker", "rm", container])
        container = ""

        verification = command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "128",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=1g",
                "--mount",
                f"type=bind,src={temporary},dst=/output,readonly",
                arguments.image,
                "verify",
                "--repo",
                "/opt/masi/repo",
                "--output",
                "/output/run",
            ]
        )
        verified = json.loads(verification.stdout.strip().splitlines()[-1])
        if verified.get("result") != "PASS":
            raise SystemExit("OCI output verification failed")
        actual_identity = {
            "archive_digest": result["archive"]["sha256"],
            "model_digest": result["bundle"]["model_digest"],
            "repository_closure_digest": result["bundle"]["repository_closure_digest"],
        }
        if actual_identity != expected_identity:
            raise SystemExit(
                "OCI/release immutable identity mismatch: "
                + json.dumps({"actual": actual_identity, "expected": expected_identity}, sort_keys=True)
            )

        evidence = {
            "schema_version": "offline-ml-oci-evidence/v1",
            "module_id": "MOD-ML-001",
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "image_ref": arguments.image,
            "image_id": metadata["Id"],
            "source_tree_digest": labels["io.masi-nids.source-tree.digest"],
            "runtime_profile": labels["io.masi-nids.runtime-profile"],
            "user": config["User"],
            "entrypoint": config["Entrypoint"],
            "version": version,
            "container_id": container_id,
            "full_pipeline_exit_code": exit_code,
            "full_pipeline_elapsed_ms": elapsed_ms,
            "candidate_executions": result["candidate_executions"],
            "dataset_revision": result["dataset_revision"],
            "model_digest": result["bundle"]["model_digest"],
            "repository_closure_digest": result["bundle"]["repository_closure_digest"],
            "archive_digest": result["archive"]["sha256"],
            "expected_blackbox_digest": "sha256:" + hashlib.sha256(expected_blackbox.read_bytes()).hexdigest(),
            "expected_immutable_identity": expected_identity,
            "cross_runtime_identity_match": True,
            "output_verification": verified,
            "isolation": {
                "network_none": True,
                "read_only_rootfs": True,
                "non_root": True,
                "cap_drop_all": True,
                "no_new_privileges": True,
                "pids_limit": 128,
                "memory_bytes": 2147483648,
                "nano_cpus": 2000000000,
                "bounded_tmpfs": True,
                "output_directory_world_writable": False,
            },
            "service_health_endpoints": {
                "applicability": "NOT_APPLICABLE",
                "stable_reason": "BOUNDED_OFFLINE_JOB_EXITS_AFTER_ATOMIC_ARTIFACT_PUBLICATION",
            },
            "logs_digest": "sha256:" + hashlib.sha256(logs.encode()).hexdigest(),
        }
        write_new_file(
            evidence_path,
            (json.dumps(evidence, sort_keys=True, indent=2) + "\n").encode(),
        )
        print(json.dumps(evidence, sort_keys=True))
    finally:
        if container:
            try:
                command(["docker", "rm", "-f", container], check=False, timeout=30)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(temporary, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
