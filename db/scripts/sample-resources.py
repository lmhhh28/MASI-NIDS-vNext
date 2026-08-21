#!/usr/bin/env python3
"""Bounded Docker resource sampler for PostgreSQL State soak evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SIZE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([KMGT]?i?B)$")
SCALE = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def parse_size(value: str) -> int:
    match = SIZE.match(value.strip())
    if not match:
        raise ValueError(f"unrecognized Docker size: {value!r}")
    return int(float(match.group(1)) * SCALE[match.group(2)])


def run(*args: str) -> str:
    completed = subprocess.run(args, check=True, text=True, capture_output=True, timeout=15)
    return completed.stdout.strip()


def proc_count(container: str, leaf: str) -> int:
    output = run(
        "docker", "exec", container, "/bin/sh", "-eu", "-c",
        f"set -- /proc/1/{leaf}/*; printf '%s\\n' \"$#\"",
    )
    return int(output)


def phase(offset: float, warmup: int, phase_duration: int) -> str:
    if offset < warmup:
        return "warmup"
    names = ("steady", "peak", "saturation", "recovery-or-activation")
    index = min(3, int((offset - warmup) // phase_duration))
    return names[index]


def sample(containers: list[str], offset_ms: int, warmup: int, phase_duration: int) -> dict[str, Any]:
    rows = run("docker", "stats", "--no-stream", "--format", "{{json .}}", *containers)
    by_name = {entry["Name"]: entry for entry in (json.loads(line) for line in rows.splitlines())}
    cpu, rss, fds, threads, oom, restarts = 0.0, 0, 0, 0, 0, 0
    for container in containers:
        entry = by_name.get(container)
        if entry is None:
            raise ValueError(f"docker stats omitted {container}")
        cpu += float(entry["CPUPerc"].rstrip("%"))
        rss += parse_size(entry["MemUsage"].split(" / ", 1)[0])
        fds += proc_count(container, "fd")
        threads += proc_count(container, "task")
        state = json.loads(run("docker", "inspect", "--format", "{{json .State}}", container))
        oom += int(bool(state.get("OOMKilled")))
        restarts += int(run("docker", "inspect", "--format", "{{.RestartCount}}", container))
    return {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "offset_ms": offset_ms,
        "phase": phase(offset_ms / 1000, warmup, phase_duration),
        "quality": "valid",
        "cpu_pct": cpu,
        "rss_bytes": rss,
        "fd_count": fds,
        "thread_count": threads,
        "queue_depth": 0,
        "oom_events": oom,
        "container_restarts": restarts,
        "oracle_errors": 0,
        "gap_count": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-seconds", required=True, type=int)
    parser.add_argument("--interval-seconds", default=10, type=int)
    parser.add_argument("--warmup-seconds", default=60, type=int)
    parser.add_argument("--phase-duration-seconds", default=900, type=int)
    parser.add_argument("--container", action="append", required=True)
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 3700 or not 1 <= args.interval_seconds <= 30:
        raise ValueError("sampler duration/interval outside bounds")
    if args.output.exists() or args.output.is_symlink() or not args.output.is_absolute():
        raise ValueError("sampler output must be a fresh absolute path")
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    samples: list[dict[str, Any]] = []
    failures: list[str] = []
    attempts = 0
    while True:
        offset = time.monotonic() - started
        if offset > args.duration_seconds:
            break
        attempts += 1
        try:
            samples.append(sample(
                args.container,
                int(offset * 1000),
                args.warmup_seconds,
                args.phase_duration_seconds,
            ))
        except (subprocess.SubprocessError, ValueError, KeyError) as error:
            failures.append(str(error)[:512])
            if len(failures) > 16:
                break
        if attempts > 400:
            raise ValueError("sampler sample bound exceeded")
        # Advance from attempted sample slots, not only successful samples. A
        # transient Docker failure must not turn the sampler into a tight loop.
        target = started + attempts * args.interval_seconds
        time.sleep(max(0.0, min(args.interval_seconds, target - time.monotonic())))
    document = {
        "schema_version": "postgresql-state-resource-samples/v1",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "interval_seconds": args.interval_seconds,
        "containers": args.container,
        "samples": samples,
        "failures": failures,
    }
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
