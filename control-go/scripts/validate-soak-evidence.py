#!/usr/bin/env python3
"""Validate Go Control Core soak evidence against the frozen public soak profile.

Mirrors edge-rs/scripts/validate-soak-evidence.py with control-core constants:
- module is "control-core" (not rust-edge-agent) and there is no separate module
  profile; the control threshold status is the frozen const
  OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001 used by contracts/evidence/soak/v1.
- lease_renewals must be EMPTY (control holds no Edge leases), inverting edge's
  minItems 1 requirement.
- cleanup uses the generic checks (module_process_reaped/provider_tasks_joined/
  listeners_released), not the edge seven-check shape.
- resource limits are the controlResourceLimits fields (pg_connections/pool_size/
  queue_depth/transaction_bytes/wal_bytes + null rss/fd/thread).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


CONTROL_THRESHOLD_STATUS = "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001"
CONTROL_PROCESS_THRESHOLD_STATUS = "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001"
MAX_SAMPLE_SCHEDULING_JITTER_MS = 1_000


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def parse_timestamp(value: object, field: str, failures: list[str]) -> datetime | None:
    if not isinstance(value, str):
        failures.append(f"{field} is not a timestamp string")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        failures.append(f"{field} is not an RFC 3339 timestamp")
        return None
    if parsed.tzinfo is None:
        failures.append(f"{field} must include a timezone")
        return None
    return parsed


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def warmup_samples_span(
    phase_offsets: list[int], warmup_elapsed_ms: int | None, expected_interval_ms: int
) -> bool:
    """Return whether warmup samples cover the measured window with bounded jitter.

    ``time.Ticker`` is not an exact wall-clock scheduler: a nominal 10-second
    first tick can arrive a few milliseconds late under load.  Accept at most one
    second of that scheduling jitter without weakening the minimum sample count,
    tail coverage, monotonicity, or the full 60-second warmup requirement checked
    by the caller.
    """

    if warmup_elapsed_ms is None or not phase_offsets:
        return False
    return (
        phase_offsets[0]
        <= expected_interval_ms + MAX_SAMPLE_SCHEDULING_JITTER_MS
        and phase_offsets[-1] >= warmup_elapsed_ms - (2 * expected_interval_ms)
        and all(offset <= warmup_elapsed_ms for offset in phase_offsets)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    evidence = load(args.evidence)
    schema_path = repo / "contracts/evidence/soak/v1/schema.json"
    profile_path = repo / "contracts/profiles/v1/qualification-soak-3600s.json"
    schema = load(schema_path)
    profile = load(profile_path)

    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(evidence),
        key=lambda item: list(item.path),
    )
    failures = [f"{list(error.path)}: {error.message}" for error in errors]
    if evidence.get("module") != "control-core":
        failures.append("the control soak validator only accepts control-core evidence")
    if evidence.get("profile_digest") != sha256(profile_path):
        failures.append("profile_digest does not bind qualification-soak-3600s.json")
    phase_names = [item.get("name") for item in evidence.get("phases", [])]
    expected_phase_names = [item["name"] for item in profile["phases"]]
    if phase_names != expected_phase_names:
        failures.append("phase order does not match the frozen soak profile")
    if any(item.get("planned_ms") != 900_000 for item in evidence.get("phases", [])):
        failures.append("phase planned duration does not match the frozen soak profile")

    started_at = parse_timestamp(evidence.get("started_at"), "started_at", failures)
    finished_at = parse_timestamp(evidence.get("finished_at"), "finished_at", failures)
    wall_elapsed_ms: int | None = None
    if started_at is not None and finished_at is not None:
        wall_elapsed_ms = int((finished_at - started_at).total_seconds() * 1000)
        if wall_elapsed_ms <= 0:
            failures.append("finished_at must be later than started_at")
    monotonic_start_ns = integer(evidence.get("monotonic_start_ns"))
    monotonic_end_ns = integer(evidence.get("monotonic_end_ns"))
    monotonic_elapsed_ms: int | None = None
    if monotonic_start_ns is not None and monotonic_end_ns is not None:
        if monotonic_end_ns <= monotonic_start_ns:
            failures.append("monotonic_end_ns must be later than monotonic_start_ns")
        else:
            monotonic_elapsed_ms = (monotonic_end_ns - monotonic_start_ns) // 1_000_000
    if (
        wall_elapsed_ms is not None
        and monotonic_elapsed_ms is not None
        and abs(wall_elapsed_ms - monotonic_elapsed_ms) > 5_000
    ):
        failures.append("wall and monotonic elapsed clocks differ by more than 5 seconds")

    expected_artifacts = {
        "qualification-soak-3600s.json": (
            sha256(profile_path).removeprefix("sha256:"),
            profile_path.stat().st_size,
        ),
        "qualification-soak-v1-schema.json": (
            sha256(schema_path).removeprefix("sha256:"),
            schema_path.stat().st_size,
        ),
    }
    observed_artifacts = {
        item.get("name"): (item.get("sha256"), item.get("bytes"))
        for item in evidence.get("artifacts", [])
        if isinstance(item, dict)
    }
    # control-core may additionally bind its own binary artifact; require at least the
    # profile + schema bindings, and accept any extra named artifacts.
    for name, expected in expected_artifacts.items():
        if observed_artifacts.get(name) != expected:
            failures.append(f"artifacts do not bind {name} to the frozen file")

    _summary = evidence.get("summary")
    summary = _summary if isinstance(_summary, dict) else {}
    _metrics = summary.get("module_metrics")
    metrics = _metrics if isinstance(_metrics, dict) else {}
    if metrics.get("absolute_threshold_status") != CONTROL_THRESHOLD_STATUS:
        failures.append("evidence absolute_threshold_status does not match the control threshold")
    warmup_elapsed_ms = integer(evidence.get("warmup_elapsed_ms"))
    if metrics.get("warmup_actual_ms") != warmup_elapsed_ms:
        failures.append("summary warmup_actual_ms does not match warmup_elapsed_ms")

    _claim_scope = evidence.get("claim_scope")
    claim_scope = _claim_scope if isinstance(_claim_scope, dict) else {}
    if evidence.get("claim_scope_digest") != canonical_digest(claim_scope):
        failures.append("claim_scope_digest does not bind the canonical claim_scope")
    if claim_scope.get("module") != evidence.get("module"):
        failures.append("claim_scope module does not match evidence module")
    if claim_scope.get("scope") != "independent-module-soak":
        failures.append("claim_scope is not the independent module soak")
    if claim_scope.get("rule_counts") != [0, 128, 1024, 4096]:
        failures.append("claim_scope rule counts drifted")
    if claim_scope.get("target_counts") != [0, 1, 2, "N"]:
        failures.append("claim_scope target counts drifted")
    if claim_scope.get("concurrency") != [2, 8, 32, 128]:
        failures.append("claim_scope concurrency drifted")
    if claim_scope.get("threshold_status") != CONTROL_THRESHOLD_STATUS:
        failures.append("claim_scope threshold status does not match the control threshold")
    _resource_limits = claim_scope.get("resource_limits")
    resource_limits = _resource_limits if isinstance(_resource_limits, dict) else {}
    if resource_limits.get("process_threshold_status") != CONTROL_PROCESS_THRESHOLD_STATUS:
        failures.append("process threshold status does not match the control threshold")
    for name in ("pg_connections", "pool_size", "queue_depth", "transaction_bytes", "wal_bytes"):
        value = integer(resource_limits.get(name))
        if value is None or value <= 0:
            failures.append(f"claim_scope resource_limits.{name} must be a positive integer")
    for name in ("rss_bytes", "fd_count", "thread_count"):
        if resource_limits.get(name) is not None:
            failures.append(f"unfrozen process resource field {name} must remain an observational null")

    _samples = evidence.get("samples")
    samples = _samples if isinstance(_samples, list) else []
    offsets = [integer(item.get("offset_ms")) for item in samples if isinstance(item, dict)]
    valid_offsets = [offset for offset in offsets if offset is not None]
    if len(valid_offsets) != len(samples):
        failures.append("every soak sample must carry an integer offset_ms")
    elif valid_offsets != sorted(valid_offsets) or len(set(valid_offsets)) != len(valid_offsets):
        failures.append("soak sample offsets are not strictly monotonic")
    gap_samples = sum(
        1 for item in samples if isinstance(item, dict) and item.get("quality") == "gap"
    )
    valid_samples = sum(
        1 for item in samples if isinstance(item, dict) and item.get("quality") == "valid"
    )
    if summary.get("unclassified_gap_count") != gap_samples:
        failures.append("summary gap count does not match recorded gap samples")
    if metrics.get("successful_status_samples") != valid_samples:
        failures.append("successful_status_samples does not match valid samples")

    # control holds no Edge leases: lease_renewals must be empty (schema enforces
    # maxItems 0 for control-core MODULE; this is the explicit cross-check).
    _lease_renewals = evidence.get("lease_renewals")
    lease_renewals = _lease_renewals if isinstance(_lease_renewals, list) else []
    if lease_renewals:
        failures.append("control-core soak must not contain Edge lease renewals")
    if metrics.get("lease_renewals") not in (None, 0):
        failures.append("summary lease_renewals must be zero for control-core")

    queue_depth_limit = integer(resource_limits.get("queue_depth"))
    observed_maxima: dict[str, int] = {}
    for summary_field, sample_field in (
        ("max_rss_bytes", "rss_bytes"),
        ("max_fd_count", "fd_count"),
        ("max_thread_count", "thread_count"),
        ("max_queue_depth", "queue_depth"),
    ):
        values = [
            integer(item.get(sample_field))
            for item in samples
            if isinstance(item, dict) and item.get("quality") == "valid"
            and integer(item.get(sample_field)) is not None
        ]
        observed = max(values, default=0)
        observed_maxima[summary_field] = observed
        if metrics.get(summary_field) != observed:
            failures.append(f"summary {summary_field} does not match recorded samples")
    if metrics.get("queue_depth_limit") != queue_depth_limit:
        failures.append("summary queue_depth_limit does not match claim_scope")
    if queue_depth_limit is not None:
        expected_violations = sum(
            1
            for item in samples
            if isinstance(item, dict)
            and item.get("quality") == "valid"
            and (value := integer(item.get("queue_depth"))) is not None
            and value > queue_depth_limit
        )
        if summary.get("resource_limit_violations") != expected_violations:
            failures.append("resource_limit_violations does not match queue_depth samples")
    if metrics.get("process_resource_threshold_status") != CONTROL_PROCESS_THRESHOLD_STATUS:
        failures.append("summary process_resource_threshold_status drifted")

    if CONTROL_THRESHOLD_STATUS != "FROZEN" and evidence.get("result") == "PASS":
        failures.append("PASS is forbidden while control thresholds are not owner-frozen")
    if evidence.get("result") == "PASS" and evidence.get("level") == "REHEARSAL":
        failures.append("short rehearsal cannot qualify")

    successful_formal = evidence.get("level") == "MODULE" and evidence.get("result") in {"PASS", "HOLD"}
    if successful_formal:
        if metrics.get("firewall_rule_matrix") != "[0 128 1024 4096]" or metrics.get("firewall_rule_matrix_completed") is not True:
            failures.append("formal control soak did not execute the 0/128/1024/4096 firewall matrix")
        if metrics.get("target_count_matrix") != "[0 1 2 32]" or metrics.get("target_count_matrix_completed") is not True:
            failures.append("formal control soak did not execute the 0/1/2/N target matrix")
        if metrics.get("metrics_scrape_errors") != 0:
            failures.append("formal control soak observed a /metrics scrape error")
        expected_warmup_ms = int(profile["warmup_seconds"]) * 1000
        expected_qualified_ms = int(profile["qualified_duration_seconds"]) * 1000
        expected_interval_ms = int(profile["sample_interval_seconds"]) * 1000
        expected_total_ms = expected_warmup_ms + expected_qualified_ms
        qualified_elapsed_ms = integer(evidence.get("qualified_elapsed_ms"))
        if warmup_elapsed_ms is None or warmup_elapsed_ms < expected_warmup_ms:
            failures.append("formal soak did not execute the full warmup")
        if qualified_elapsed_ms is None or qualified_elapsed_ms < expected_qualified_ms:
            failures.append("formal soak did not execute 3600 qualified seconds")
        if evidence.get("sample_interval_ms") != expected_interval_ms:
            failures.append("formal soak sample interval is not the frozen 10 seconds")
        if monotonic_elapsed_ms is None or monotonic_elapsed_ms < expected_total_ms:
            failures.append("monotonic clock does not prove the complete formal schedule")
        if wall_elapsed_ms is None or wall_elapsed_ms < expected_total_ms:
            failures.append("wall clock does not prove the complete formal schedule")
        phase_bounds: dict[str, tuple[int, int]] = {}
        previous_phase_end = warmup_elapsed_ms
        first_phase_start: int | None = None
        last_phase_end: int | None = None
        for phase in evidence.get("phases", []):
            if (
                integer(phase.get("elapsed_ms")) is None
                or int(phase["elapsed_ms"]) < 900_000
                or phase.get("result") != "PASS"
                or phase.get("errors") != 0
            ):
                failures.append(f"formal phase {phase.get('name')} is incomplete or failed")
            _phase_metrics = phase.get("module_metrics")
            phase_metrics = _phase_metrics if isinstance(_phase_metrics, dict) else {}
            phase_start = integer(phase_metrics.get("phase_start_offset_ms"))
            phase_end = integer(phase_metrics.get("phase_end_offset_ms"))
            phase_elapsed = integer(phase.get("elapsed_ms"))
            phase_name = phase.get("name")
            requests = integer(phase_metrics.get("requests"))
            committed = integer(phase_metrics.get("committed"))
            achieved = phase.get("achieved_rate_pps")
            if requests is None or requests < 1 or committed != requests:
                failures.append(f"formal phase {phase_name} request/commit counts are incomplete")
            if phase_elapsed is not None and committed is not None and phase_elapsed > 0 and isinstance(achieved, (int, float)):
                calculated_rate = committed / (phase_elapsed / 1000.0)
                if abs(float(achieved) - calculated_rate) > 0.001:
                    failures.append(f"formal phase {phase_name} achieved rate is not derived from committed requests")
            else:
                failures.append(f"formal phase {phase_name} achieved rate evidence is malformed")
            if (
                not isinstance(phase_name, str)
                or phase_start is None
                or phase_end is None
                or phase_elapsed is None
                or phase_end <= phase_start
                or abs((phase_end - phase_start) - phase_elapsed) > 1_000
            ):
                failures.append(f"formal phase {phase_name} has invalid monotonic boundaries")
                continue
            if previous_phase_end is not None and not (
                previous_phase_end <= phase_start <= previous_phase_end + 5_000
            ):
                failures.append(f"formal phase {phase_name} is not contiguous with its predecessor")
            phase_bounds[phase_name] = (phase_start, phase_end)
            first_phase_start = phase_start if first_phase_start is None else first_phase_start
            last_phase_end = phase_end
            previous_phase_end = phase_end
        if (
            qualified_elapsed_ms is not None
            and first_phase_start is not None
            and last_phase_end is not None
            and abs((last_phase_end - first_phase_start) - qualified_elapsed_ms) > 5_000
        ):
            failures.append("qualified_elapsed_ms does not match the four phase boundaries")
        expected_sample_minimums = {
            "warmup": 5,
            "steady": 85,
            "peak": 85,
            "saturation": 85,
            "recovery-or-activation": 85,
        }
        for phase_name, minimum in expected_sample_minimums.items():
            phase_samples = [
                item
                for item in samples
                if isinstance(item, dict)
                and item.get("phase") == phase_name
                and item.get("quality") == "valid"
            ]
            observed = len(phase_samples)
            if observed < minimum:
                failures.append(
                    f"formal phase {phase_name} has only {observed} valid samples; expected at least {minimum}"
                )
            phase_offsets = [
                offset
                for item in phase_samples
                if (offset := integer(item.get("offset_ms"))) is not None
            ]
            if phase_name == "warmup":
                if not warmup_samples_span(
                    phase_offsets, warmup_elapsed_ms, expected_interval_ms
                ):
                    failures.append("warmup samples do not span the measured warmup window")
            elif phase_name in phase_bounds:
                phase_start, phase_end = phase_bounds[phase_name]
                if (
                    not phase_offsets
                    or phase_offsets[0] > phase_start + (2 * expected_interval_ms)
                    or phase_offsets[-1] < phase_end - (2 * expected_interval_ms)
                    or any(not (phase_start <= offset <= phase_end) for offset in phase_offsets)
                ):
                    failures.append(f"formal phase {phase_name} samples do not span its boundary")
        zero_summary_fields = (
            "error_count",
            "unclassified_gap_count",
            "oom_events",
            "container_restarts",
            "oracle_mismatches",
            "resource_limit_violations",
        )
        if any(summary.get(field) != 0 for field in zero_summary_fields):
            failures.append("successful formal soak contains errors, gaps, or limit violations")
        _cleanup = evidence.get("cleanup")
        cleanup = _cleanup if isinstance(_cleanup, dict) else {}
        if cleanup != {
            "attempted": True,
            "completed": True,
            "exit_code": 0,
            "remaining_resources": [],
            "checks": {
                "module_process_reaped": True,
                "provider_tasks_joined": True,
                "listeners_released": True,
            },
        }:
            failures.append("successful formal soak cleanup is incomplete")
        if evidence.get("interruption") != "NONE":
            failures.append("successful formal soak was interrupted")
        if metrics.get("formal_schedule_executed") is not True:
            failures.append("formal_schedule_executed is not true")
        # control thresholds are owner-unfrozen: a successful formal schedule must remain
        # HOLD/NOT_QUALIFIED (the module-complete candidate state).
        if evidence.get("result") != "HOLD" or evidence.get("qualification") != "NOT_QUALIFIED":
            failures.append("unfrozen control thresholds require formal evidence to remain HOLD/NOT_QUALIFIED")

    if evidence.get("level") == "REHEARSAL":
        if lease_renewals:
            failures.append("short rehearsal unexpectedly contains a formal lease renewal")
        if metrics.get("formal_schedule_executed") is not False:
            failures.append("rehearsal must not claim that the formal schedule executed")
        if evidence.get("result") == "HOLD":
            if evidence.get("qualification") != "NOT_QUALIFIED":
                failures.append("successful rehearsal must remain NOT_QUALIFIED")
            if any(
                phase.get("result") != "HOLD" or phase.get("errors") != 0
                for phase in evidence.get("phases", [])
            ):
                failures.append("successful rehearsal phases must be explicitly HOLD")
            if any(
                integer(phase.get("elapsed_ms")) is not None
                and int(phase["elapsed_ms"]) >= 900_000
                for phase in evidence.get("phases", [])
            ):
                failures.append("rehearsal unexpectedly contains a formal-duration phase")

    if failures:
        raise SystemExit("\n".join(failures))
    print(
        json.dumps(
            {
                "evidence": str(args.evidence.resolve()),
                "profile_digest": evidence["profile_digest"],
                "result": evidence["result"],
                "qualification": evidence["qualification"],
                "schema_valid": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
