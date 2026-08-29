#!/usr/bin/env python3
"""Clean and verify the exact P9 Docker closure before publishing PASS evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def run(command: list[str], timeout: int) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return {
            "name": command[1] if len(command) > 1 else command[0],
            "exit_code": completed.returncode,
            "output_tail": completed.stdout[-2048:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {
            "name": command[1] if len(command) > 1 else command[0],
            "exit_code": 124,
            "output_tail": output[-2048:],
            "timed_out": True,
        }


def write_new(path: Path, payload: bytes) -> None:
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
    parser.add_argument("--project", required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("--container", action="append", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    log_names = ("control.log", "control-forwarder.log", "web.log")
    for container, log_name in zip(args.container, log_names, strict=True):
        observed = run(["docker", "logs", container], 30)
        write_new(
            args.evidence_root / log_name,
            str(observed["output_tail"]).encode("utf-8"),
        )
    commands = [
        run(["docker", "rm", "-f", *args.container], 60),
        run(["docker", "network", "rm", args.network], 30),
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
        ),
    ]
    commands[0]["name"] = "runtime-containers"
    commands[1]["name"] = "runtime-network"
    commands[2]["name"] = "postgresql-compose"
    remaining: list[str] = []
    for container in args.container:
        if run(["docker", "container", "inspect", container], 30)["exit_code"] == 0:
            remaining.append(f"container:{container}")
    if run(["docker", "network", "inspect", args.network], 30)["exit_code"] == 0:
        remaining.append(f"network:{args.network}")
    for kind, command in (
        (
            "container",
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={args.project}"],
        ),
        (
            "network",
            ["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={args.project}"],
        ),
        (
            "volume",
            ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={args.project}"],
        ),
    ):
        observed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        if observed.returncode != 0:
            remaining.append(f"query-error:{kind}")
        else:
            remaining.extend(f"{kind}:{identity}" for identity in observed.stdout.splitlines() if identity)
    passed = all(command["exit_code"] == 0 for command in commands) and not remaining
    document = {
        "schema_version": "p9-runtime-cleanup/v1",
        "attempted": True,
        "result": "PASS" if passed else "FAIL",
        "commands": commands,
        "remaining_resources": remaining,
    }
    write_new(args.output, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
