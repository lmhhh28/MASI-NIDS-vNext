#!/usr/bin/env python3
"""Capture and verify that full-startup adds no durable runtime resources."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def command(arguments: list[str]) -> list[str]:
    completed = subprocess.run(
        arguments,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    return sorted(value for value in completed.stdout.splitlines() if value)


def docker_snapshot() -> dict[str, list[str]]:
    return {
        "containers": command(["docker", "ps", "-aq"]),
        "networks": command(["docker", "network", "ls", "-q"]),
        "volumes": command(["docker", "volume", "ls", "-q"]),
    }


def write_new(path: Path, document: dict[str, object]) -> None:
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
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


def ancestors() -> set[int]:
    values = {os.getpid()}
    current = os.getppid()
    while current > 1 and current not in values:
        values.add(current)
        try:
            fields = Path(f"/proc/{current}/stat").read_text(encoding="utf-8").split()
            current = int(fields[3])
        except (OSError, ValueError, IndexError):
            break
    return values


def run_identity_processes(run_id: str) -> list[str]:
    excluded = ancestors()
    observed: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in excluded:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command_line = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
        if run_id in command_line:
            observed.append(f"{entry.name}:{command_line[:512]}")
    return sorted(observed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--components-dir", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.capture:
        write_new(args.output, {"schema_version": "system-startup-resource-baseline/v1", **docker_snapshot()})
        return 0
    if args.baseline is None or args.components_dir is None or not args.run_id:
        raise SystemExit("verification requires baseline, components-dir, and run-id")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = docker_snapshot()
    new_resources = {
        kind: sorted(set(current[kind]) - set(baseline.get(kind, [])))
        for kind in ("containers", "networks", "volumes")
    }
    components = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(args.components_dir.glob("*.json"))]
    component_processes_reaped = (
        len(components) == 9 and all(item.get("process_reaped") is True for item in components)
    )
    process_residuals = run_identity_processes(args.run_id)
    passed = (
        component_processes_reaped
        and all(not values for values in new_resources.values())
        and not process_residuals
    )
    document = {
        "schema_version": "system-startup-cleanup/v1",
        "attempted": True,
        "result": "PASS" if passed else "FAIL",
        "component_processes_reaped": component_processes_reaped,
        "new_docker_resources": new_resources,
        "run_identity_process_residuals": process_residuals,
    }
    write_new(args.output, document)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
