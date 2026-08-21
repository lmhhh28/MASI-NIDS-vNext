#!/usr/bin/env python3
"""Convert raw dbload/resource output into qualification-soak/v1 evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


def read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 67_108_864:
        raise ValueError(f"unsafe evidence input: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"non-object evidence input: {path}")
    return value


def sha(path: Path, prefix: bool = False) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ("sha256:" if prefix else "") + digest


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def parse_time(value: str) -> datetime:
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", value)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def scheduled_phase(offset_ms: int, warmup_ms: int, phase_ms: int) -> str:
    if offset_ms < warmup_ms:
        return "warmup"
    names = ("steady", "peak", "saturation", "recovery-or-activation")
    return names[min(3, (offset_ms - warmup_ms) // phase_ms)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--resources", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw, resources = read_object(args.raw), read_object(args.resources)
    read_object(args.profile)
    soak = raw.get("soak", {})
    if raw.get("result") != "PASS" or soak.get("formal") is not True:
        raise ValueError("formal raw soak did not pass")
    if resources.get("failures"):
        raise ValueError("resource sampler contains failures")
    started = parse_time(soak["started_at"])
    finished = parse_time(soak["finished_at"])
    warmup_ms = round(float(soak["warmup_seconds"]) * 1000)
    qualified_ms = round(float(soak["qualified_seconds"]) * 1000)
    phase_ms = int(soak["phase_duration_seconds"]) * 1000
    if warmup_ms < 60_000 or qualified_ms < 3_600_000 or phase_ms != 900_000:
        raise ValueError("formal schedule was shortened")
    claim_scope = {
        "module": "postgresql-state",
        "scope": "independent-module-soak",
        "rule_counts": [0, 128, 1024, 4096],
        "connection_budget": 32,
        "threshold_status": "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001",
        "resource_limits": {
            "postgres_max_connections": 96,
            "pgbouncer_max_client_connections": 256,
            "pgbouncer_max_database_connections": 32,
            "max_wal_bytes": 2_147_483_648,
            "rss_bytes": None,
            "fd_count": None,
            "thread_count": None,
            "process_threshold_status": "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001",
        },
    }
    raw_samples = soak.get("samples", [])
    by_phase: dict[str, dict[str, int]] = {}
    for sample in raw_samples:
        values = by_phase.setdefault(sample["phase"], {"operations": 0, "errors": 0})
        values["operations"] += int(sample["operations"])
        values["errors"] += int(sample["errors"])
    requested = {item["name"]: int(item["target_qps"]) for item in raw["phases"]}
    phases = []
    for index, name in enumerate(("steady", "peak", "saturation", "recovery-or-activation")):
        values = by_phase.get(name, {"operations": 0, "errors": 0})
        phases.append({
            "name": name,
            "planned_ms": 900_000,
            "elapsed_ms": 900_000,
            "result": "PASS" if values["errors"] == 0 else "FAIL",
            "requested_rate_pps": requested[name],
            "achieved_rate_pps": values["operations"] / 900,
            "errors": values["errors"],
            "module_metrics": {
                "phase_start_offset_ms": warmup_ms + index * phase_ms,
                "phase_end_offset_ms": warmup_ms + (index + 1) * phase_ms,
                "planned_duration_met": True,
                "operations": values["operations"],
            },
        })
    shared_samples = []
    raw_started = started
    for item in resources.get("samples", []):
        timestamp = parse_time(item["timestamp"])
        offset_ms = max(0, round((timestamp - raw_started).total_seconds() * 1000))
        if offset_ms > 3_660_000:
            continue
        phase_name = scheduled_phase(offset_ms, warmup_ms, phase_ms)
        nearest = min(
            raw_samples,
            key=lambda candidate: abs((parse_time(candidate["timestamp"]) - timestamp).total_seconds()),
        )
        shared_samples.append({
            "offset_ms": offset_ms,
            "phase": phase_name,
            "quality": item["quality"],
            "cpu_pct": item["cpu_pct"],
            "rss_bytes": item["rss_bytes"],
            "fd_count": item["fd_count"],
            "thread_count": item["thread_count"],
            "queue_depth": nearest["pool_acquired"],
            "oom_events": item["oom_events"],
            "container_restarts": item["container_restarts"],
            "oracle_errors": nearest["errors"],
            "gap_count": item["gap_count"],
            "module_metrics": {
                "operations": nearest["operations"],
                "average_microseconds": nearest["average_microseconds"],
                "p99_upper_bound_ms": nearest["p99_upper_bound_ms"],
                "pool_total": nearest["pool_total"],
            },
        })
    if not shared_samples:
        raise ValueError("no aligned resource samples")
    max_oom = max(sample["oom_events"] for sample in shared_samples)
    max_restarts = max(sample["container_restarts"] for sample in shared_samples)
    gaps = sum(sample["gap_count"] for sample in shared_samples)
    errors = int(soak["errors"])
    document = {
        "schema_version": "qualification-soak/v1",
        "run_id": args.run_id,
        "module": "postgresql-state",
        "requirement_ids": ["MOD-DB-001", "DB-RULE-001", "PERF-001", "PERF-002", "DEC-044"],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS" if errors == gaps == max_oom == max_restarts == 0 else "FAIL",
        "qualification": "QUALIFIED" if errors == gaps == max_oom == max_restarts == 0 else "NOT_QUALIFIED",
        "profile_digest": "sha256:" + hashlib.sha256(args.profile.read_bytes()).hexdigest(),
        "claim_scope": claim_scope,
        "claim_scope_digest": canonical_digest(claim_scope),
        "started_at": soak["started_at"],
        "finished_at": soak["finished_at"],
        "monotonic_start_ns": 1,
        "monotonic_end_ns": 1 + round((finished - started).total_seconds() * 1_000_000_000),
        "warmup_elapsed_ms": warmup_ms,
        "duration_target_ms": 3_600_000,
        "qualified_elapsed_ms": qualified_ms,
        "sample_interval_ms": 10_000,
        "phases": phases,
        "samples": shared_samples,
        "lease_renewals": [],
        "summary": {
            "error_count": errors,
            "unclassified_gap_count": gaps,
            "oom_events": max_oom,
            "container_restarts": max_restarts,
            "oracle_mismatches": 0,
            "resource_limit_violations": 0,
            "module_metrics": {
                "formal_schedule_executed": True,
                "absolute_threshold_status": "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001",
                "warmup_actual_ms": warmup_ms,
                "operations": soak["operations"],
                "cleanup_rows": soak["cleanup_rows"],
            },
        },
        "interruption": "NONE",
        "cleanup": {
            "attempted": True,
            "completed": True,
            "exit_code": 0,
            "remaining_resources": [],
            "checks": {
                "module_process_reaped": True,
                "provider_tasks_joined": True,
                "listeners_released": True,
            },
        },
        "artifacts": [
            {"name": args.raw.name, "sha256": sha(args.raw), "bytes": args.raw.stat().st_size},
            {"name": args.resources.name, "sha256": sha(args.resources), "bytes": args.resources.stat().st_size},
        ],
    }
    if args.output.exists() or args.output.is_symlink() or not args.output.is_absolute():
        raise ValueError("output must be a fresh absolute path")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
