#!/usr/bin/env python3
"""Deterministic negative cases for the qualification-soak evidence validator."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def digest(path: Path, prefix: bool = True) -> str:
    value = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{value}" if prefix else value


def value_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def sample(offset_ms: int, phase: str) -> dict[str, Any]:
    return {
        "offset_ms": offset_ms,
        "phase": phase,
        "quality": "valid",
        "cpu_pct": 1.0,
        "rss_bytes": 1,
        "fd_count": 1,
        "thread_count": 1,
        "queue_depth": 0,
        "oom_events": 0,
        "container_restarts": 0,
        "oracle_errors": 0,
        "gap_count": 0,
        "module_metrics": {
            "fixture": "validator-self-test",
            "source_wal_bytes": 1,
            "input_wal_bytes": 1,
            "result_wal_bytes": 1,
        },
    }


def formal_hold_fixture(repo: Path) -> dict[str, Any]:
    profile_path = repo / "contracts/profiles/v1/qualification-soak-3600s.json"
    schema_path = repo / "contracts/evidence/soak/v1/schema.json"
    edge_profile = json.loads(
        (repo / "contracts/profiles/v1/rust-edge-agent.json").read_text(
            encoding="utf-8"
        )
    )
    phases = []
    samples = [sample(offset, "warmup") for offset in range(0, 60_000, 10_000)]
    for index, name in enumerate(
        ("steady", "peak", "saturation", "recovery-or-activation")
    ):
        start = 60_000 + (index * 900_000)
        end = start + 900_000
        phases.append(
            {
                "name": name,
                "planned_ms": 900_000,
                "elapsed_ms": 900_000,
                "result": "PASS",
                "requested_rate_pps": 0.0,
                "achieved_rate_pps": 0.0,
                "errors": 0,
                "module_metrics": {
                    "planned_duration_met": True,
                    "phase_start_offset_ms": start,
                    "phase_end_offset_ms": end,
                },
            }
        )
        for sample_index in range(90):
            offset = start + ((899_999 * sample_index) // 89)
            samples.append(sample(offset, name))
    threshold_status = edge_profile["performance_gate"]["absolute_threshold_status"]
    process_threshold_status = edge_profile["performance_gate"][
        "process_resource_threshold_status"
    ]
    resource_limits = {
        "aggregate_target_queue_depth": 292,
        "source_wal_bytes": 32_000_000,
        "input_wal_bytes": 32_000_000,
        "result_wal_bytes": 32_000_000,
        "rss_bytes": None,
        "fd_count": None,
        "thread_count": None,
        "process_threshold_status": process_threshold_status,
    }
    claim_scope = {
        "module": "rust-edge-agent",
        "rule_counts": [0, 128, 1024, 4096],
        "scope": "independent-module-soak",
        "target_counts": [0, 1, 2, "N"],
        "threshold_status": threshold_status,
        "resource_limits": resource_limits,
    }
    started_at = "2026-08-14T00:00:00Z"
    started_epoch_ms = int(
        datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp() * 1000
    )
    lease_renewals = []
    previous_expiry = started_epoch_ms + 300_000
    for index, offset in enumerate(range(120_000, 3_600_001, 120_000), start=1):
        observed_at = started_epoch_ms + offset
        requested_expiry = max(observed_at + 240_000, previous_expiry + 1)
        lease_renewals.append(
            {
                "offset_ms": offset,
                "observed_at_unix_ms": observed_at,
                "previous_expires_at_unix_ms": previous_expiry,
                "requested_expires_at_unix_ms": requested_expiry,
                "target_id": "target-0",
                "lease_id": "lease-target-0",
                "trace_id": f"soak-lease-renew-{index}",
                "state": "PRIMARY",
                "reason_code": "TARGET_RENEWED",
                "actor_runtime_epoch": "edge-runtime-validator",
            }
        )
        previous_expiry = requested_expiry
    return {
        "schema_version": "qualification-soak/v1",
        "run_id": "edge-soak-validator-self-test",
        "module": "rust-edge-agent",
        "requirement_ids": ["MOD-EDGE-001", "TEST-008"],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "HOLD" if threshold_status != "FROZEN" else "PASS",
        "qualification": "NOT_QUALIFIED"
        if threshold_status != "FROZEN"
        else "QUALIFIED",
        "profile_digest": digest(profile_path),
        "claim_scope": claim_scope,
        "claim_scope_digest": value_digest(claim_scope),
        "started_at": started_at,
        "finished_at": "2026-08-14T01:01:00Z",
        "monotonic_start_ns": 1_000_000_000,
        "monotonic_end_ns": 3_661_000_000_000,
        "warmup_elapsed_ms": 60_000,
        "duration_target_ms": 3_600_000,
        "qualified_elapsed_ms": 3_600_000,
        "sample_interval_ms": 10_000,
        "phases": phases,
        "samples": samples,
        "lease_renewals": lease_renewals,
        "summary": {
            "error_count": 0,
            "unclassified_gap_count": 0,
            "oom_events": 0,
            "container_restarts": 0,
            "oracle_mismatches": 0,
            "resource_limit_violations": 0,
            "module_metrics": {
                "formal_schedule_executed": True,
                "absolute_threshold_status": threshold_status,
                "warmup_actual_ms": 60_000,
                "successful_status_samples": len(samples),
                "lease_renewals": len(lease_renewals),
                "max_rss_bytes": 1,
                "max_fd_count": 1,
                "max_thread_count": 1,
                "max_queue_depth": 0,
                "max_source_wal_bytes": 1,
                "max_input_wal_bytes": 1,
                "max_result_wal_bytes": 1,
                "aggregate_target_queue_depth_limit": 292,
                "source_wal_bytes_limit": 32_000_000,
                "input_wal_bytes_limit": 32_000_000,
                "result_wal_bytes_limit": 32_000_000,
                "process_resource_threshold_status": process_threshold_status,
            },
        },
        "interruption": "NONE",
        "cleanup": {
            "attempted": True,
            "completed": True,
            "exit_code": 0,
            "remaining_resources": [],
            "checks": {
                "edge_process_reaped": True,
                "p4_server_task_joined": True,
                "inference_server_task_joined": True,
                "control_server_task_joined": True,
                "p4_listener_released": True,
                "inference_listener_released": True,
                "control_listener_released": True,
            },
        },
        "artifacts": [
            {
                "name": "qualification-soak-3600s.json",
                "sha256": digest(profile_path, prefix=False),
                "bytes": profile_path.stat().st_size,
            },
            {
                "name": "qualification-soak-v1-schema.json",
                "sha256": digest(schema_path, prefix=False),
                "bytes": schema_path.stat().st_size,
            },
        ],
    }


def validator_accepts(repo: Path, payload: dict[str, Any]) -> bool:
    with tempfile.TemporaryDirectory(prefix="masi-edge-soak-validator-") as directory:
        evidence = Path(directory) / "evidence.json"
        evidence.write_text(json.dumps(payload), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(repo / "edge-rs/scripts/validate-soak-evidence.py"),
                "--repo",
                str(repo),
                "--evidence",
                str(evidence),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    valid = formal_hold_fixture(repo)
    if not validator_accepts(repo, valid):
        raise SystemExit("validator rejected the valid deterministic formal fixture")

    mutations: dict[str, Any] = {
        "short-qualified-duration": lambda item: item.__setitem__(
            "qualified_elapsed_ms", 4_000
        ),
        "same-wall-timestamp": lambda item: item.__setitem__(
            "finished_at", item["started_at"]
        ),
        "incomplete-phase": lambda item: item["phases"][0].update(
            {"elapsed_ms": 1_000, "result": "HOLD"}
        ),
        "incomplete-cleanup": lambda item: item["cleanup"].update(
            {"completed": False, "remaining_resources": ["edge"]}
        ),
        "wrong-phase-sample-window": lambda item: item["samples"][6].__setitem__(
            "offset_ms", 1
        ),
        "formal-flag-false": lambda item: item["summary"]["module_metrics"].__setitem__(
            "formal_schedule_executed", False
        ),
        "no-lease-renewal": lambda item: item["summary"]["module_metrics"].__setitem__(
            "lease_renewals", 0
        ),
        "broken-renewal-chain": lambda item: item["lease_renewals"][1].__setitem__(
            "previous_expires_at_unix_ms",
            item["lease_renewals"][1]["previous_expires_at_unix_ms"] + 1,
        ),
        "late-renewal-cadence": lambda item: item["lease_renewals"][1].__setitem__(
            "offset_ms", item["lease_renewals"][0]["offset_ms"] + 130_001
        ),
        "short-final-lease-horizon": lambda item: item["lease_renewals"][
            -1
        ].__setitem__("requested_expires_at_unix_ms", 1_786_669_299_999),
        "wall-monotonic-clock-skew": lambda item: item.__setitem__(
            "monotonic_end_ns", item["monotonic_end_ns"] + 6_000_000_000
        ),
        "renewal-after-run-end": lambda item: item["lease_renewals"][-1].__setitem__(
            "observed_at_unix_ms", 1_786_669_301_001
        ),
        "forged-cleanup-check": lambda item: item["cleanup"]["checks"].__setitem__(
            "p4_listener_released", False
        ),
        "wal-limit-mismatch": lambda item: item["samples"][0][
            "module_metrics"
        ].__setitem__("source_wal_bytes", 32_000_001),
        "forged-claim-digest": lambda item: item["claim_scope"][
            "resource_limits"
        ].__setitem__("source_wal_bytes", 31_000_000),
        "invented-process-threshold": lambda item: item["claim_scope"][
            "resource_limits"
        ].__setitem__("rss_bytes", 268_435_456),
    }
    for name, mutate in mutations.items():
        candidate = copy.deepcopy(valid)
        mutate(candidate)
        if validator_accepts(repo, candidate):
            raise SystemExit(f"validator accepted forbidden mutation: {name}")
    print(
        json.dumps(
            {"valid_fixture": "accepted", "forbidden_mutations": sorted(mutations)}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
