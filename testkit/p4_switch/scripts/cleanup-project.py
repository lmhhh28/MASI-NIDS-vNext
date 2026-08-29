from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def read_regular(path: Path, maximum_bytes: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise ValueError(f"unsafe bounded cleanup input: {path}")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise ValueError("cleanup input exceeds its byte limit")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("cleanup input changed while being read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def run(command: list[str], timeout_seconds: int) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
        )
        return {
            "command": command,
            "exit_code": completed.returncode,
            "output": completed.stdout[-4096:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {
            "command": command,
            "exit_code": 124,
            "output": output[-4096:],
            "timed_out": True,
        }


def list_resources(kind: str, project: str) -> list[str]:
    command = ["docker", kind, "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]
    if kind == "ps":
        command = ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]
    completed = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=30,
    )
    return sorted(value for value in completed.stdout.splitlines() if value)


def write_new(path: Path, document: dict[str, object]) -> None:
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o660,
            dir_fd=directory,
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-identity", type=Path, required=True)
    parser.add_argument("--mininet-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--standalone-name", action="append", default=[])
    args = parser.parse_args()

    source = json.loads(read_regular(args.source_identity, 16 * 1024).decode("utf-8"))
    commands: list[dict[str, object]] = []
    for name in args.standalone_name:
        inspected = run(["docker", "container", "inspect", name], 30)
        if inspected["exit_code"] == 0:
            commands.append(run(["docker", "rm", "-f", name], 60))
    commands.append(
        run(
            [
                "docker",
                "compose",
                "-p",
                args.project,
                "-f",
                str(args.compose_file),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            180,
        )
    )
    query_error = ""
    remaining: dict[str, list[str]] = {"containers": [], "networks": [], "volumes": []}
    try:
        remaining["containers"] = list_resources("ps", args.project)
        remaining["networks"] = list_resources("network", args.project)
        remaining["volumes"] = list_resources("volume", args.project)
    except (OSError, subprocess.SubprocessError) as error:
        query_error = str(error)[:1024]
    host_error = ""
    host_checks = {
        "mininet_phase_cleanup_valid": False,
        "mininet_host_processes_absent": False,
        "mininet_switch_interfaces_absent": False,
        "host_interface_masi_s1_absent": False,
        "host_interface_masi_s2_absent": False,
    }
    try:
        mininet = json.loads(read_regular(args.mininet_evidence, 4 * 1024 * 1024).decode("utf-8"))
        tests = mininet.get("tests", []) if isinstance(mininet, dict) else []
        evidence = tests[0].get("evidence", {}) if isinstance(tests, list) and tests else {}
        phase_cleanup = evidence.get("cleanup", {}) if isinstance(evidence, dict) else {}
        host_checks["mininet_phase_cleanup_valid"] = (
            isinstance(phase_cleanup, dict)
            and phase_cleanup.get("container_absent") is True
            and phase_cleanup.get("errors") == []
        )
        host_checks["mininet_host_processes_absent"] = (
            isinstance(phase_cleanup, dict)
            and phase_cleanup.get("mininet_host_processes_absent") is True
        )
        host_checks["mininet_switch_interfaces_absent"] = (
            isinstance(phase_cleanup, dict)
            and phase_cleanup.get("switch_interfaces_absent") is True
        )
        for interface in ("masi-s1", "masi-s2"):
            observed = subprocess.run(
                ["ip", "link", "show", "dev", interface],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            host_checks[f"host_interface_{interface.replace('-', '_')}_absent"] = (
                observed.returncode != 0
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, subprocess.SubprocessError) as error:
        host_error = str(error)[:1024]
    passed = (
        not query_error
        and not host_error
        and all(command["exit_code"] == 0 for command in commands)
        and all(not values for values in remaining.values())
        and all(host_checks.values())
    )
    document = {
        "schema_version": "p4-global-cleanup/v1",
        "run_id": args.run_id,
        **source,
        "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "attempted": True,
        "result": "PASS" if passed else "FAIL",
        "qualification": "QUALIFIED" if passed else "NOT_QUALIFIED",
        "commands": commands,
        "remaining_resources": remaining,
        "host_checks": host_checks,
        "query_error": query_error,
        "host_error": host_error,
    }
    write_new(args.output, document)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
