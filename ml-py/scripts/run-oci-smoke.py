#!/usr/bin/env python3
"""Run the complete Offline ML pipeline in a hardened non-root OCI job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, cast


def command(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def inspect(image: str) -> dict[str, Any]:
    result = command(["docker", "image", "inspect", image])
    return cast(list[dict[str, Any]], json.loads(result.stdout))[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-blackbox", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.evidence.exists() or arguments.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    if (
        arguments.expected_blackbox.is_symlink()
        or not arguments.expected_blackbox.is_file()
        or arguments.expected_blackbox.stat().st_size > 16 * 1024 * 1024
    ):
        raise SystemExit("expected blackbox evidence is invalid")
    expected_document = cast(dict[str, Any], json.loads(arguments.expected_blackbox.read_text(encoding="utf-8")))
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
    os.chmod(temporary, 0o777)
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
        exit_code = int(command(["docker", "wait", container]).stdout.strip())
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
            "expected_blackbox_digest": "sha256:"
            + hashlib.sha256(arguments.expected_blackbox.read_bytes()).hexdigest(),
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
            },
            "service_health_endpoints": {
                "applicability": "NOT_APPLICABLE",
                "stable_reason": "BOUNDED_OFFLINE_JOB_EXITS_AFTER_ATOMIC_ARTIFACT_PUBLICATION",
            },
            "logs_digest": "sha256:" + hashlib.sha256(logs.encode()).hexdigest(),
        }
        arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
        arguments.evidence.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, sort_keys=True))
    finally:
        if container:
            command(["docker", "rm", "-f", container], check=False)
        shutil.rmtree(temporary, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
