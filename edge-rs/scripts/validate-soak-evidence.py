#!/usr/bin/env python3
"""Validate Rust Edge soak evidence against its frozen public profile."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    evidence = load(args.evidence)
    schema_path = repo / "contracts/evidence/soak/v1/schema.json"
    profile_path = repo / "contracts/profiles/v1/qualification-soak-3600s.json"
    edge_profile_path = repo / "contracts/profiles/v1/rust-edge-agent.json"
    schema = load(schema_path)
    profile = load(profile_path)
    edge_profile = load(edge_profile_path)

    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            evidence
        ),
        key=lambda item: list(item.path),
    )
    failures = [f"{list(error.path)}: {error.message}" for error in errors]
    if evidence.get("module") != "rust-edge-agent":
        failures.append(
            "the Rust Edge soak validator only accepts rust-edge-agent evidence"
        )
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
        failures.append(
            "wall and monotonic elapsed clocks differ by more than 5 seconds"
        )

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
    if observed_artifacts != expected_artifacts:
        failures.append("artifacts do not exactly bind the soak profile and schema")

    threshold_status = edge_profile["performance_gate"]["absolute_threshold_status"]
    process_threshold_status = edge_profile["performance_gate"][
        "process_resource_threshold_status"
    ]
    summary = (
        evidence.get("summary") if isinstance(evidence.get("summary"), dict) else {}
    )
    metrics = (
        summary.get("module_metrics")
        if isinstance(summary.get("module_metrics"), dict)
        else {}
    )
    if metrics.get("absolute_threshold_status") != threshold_status:
        failures.append(
            "evidence threshold status does not exactly match the Edge profile"
        )
    warmup_elapsed_ms = integer(evidence.get("warmup_elapsed_ms"))
    if metrics.get("warmup_actual_ms") != warmup_elapsed_ms:
        failures.append("summary warmup_actual_ms does not match warmup_elapsed_ms")

    claim_scope = (
        evidence.get("claim_scope")
        if isinstance(evidence.get("claim_scope"), dict)
        else {}
    )
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
    if claim_scope.get("threshold_status") != threshold_status:
        failures.append("claim_scope threshold status does not match the Edge profile")
    resource_limits = (
        claim_scope.get("resource_limits")
        if isinstance(claim_scope.get("resource_limits"), dict)
        else {}
    )
    if resource_limits.get("process_threshold_status") != process_threshold_status:
        failures.append("process threshold status does not match the Edge profile")
    expected_unfrozen_process_limits = {
        "rss_bytes": None,
        "fd_count": None,
        "thread_count": None,
    }
    if any(
        resource_limits.get(name) is not value
        for name, value in expected_unfrozen_process_limits.items()
    ):
        failures.append(
            "unfrozen process resource fields must remain observational nulls"
        )
    profile_resource_maxima = {
        "source_wal_bytes": edge_profile["wal"]["source_max_bytes_per_target"],
        "input_wal_bytes": edge_profile["wal"]["input_max_bytes_per_target"],
        "result_wal_bytes": edge_profile["wal"]["result_max_bytes_per_target"],
        "aggregate_target_queue_depth": edge_profile["targets"][
            "high_priority_queue_per_target"
        ]
        + edge_profile["targets"]["pending_digest_ack_queue_per_target"]
        + edge_profile["targets"]["observation_queue_per_target"],
    }
    for field, maximum in profile_resource_maxima.items():
        value = integer(resource_limits.get(field))
        if value is None or value <= 0 or value > maximum:
            failures.append(f"claim_scope {field} is outside the Edge profile bound")

    samples = (
        evidence.get("samples") if isinstance(evidence.get("samples"), list) else []
    )
    offsets = [
        integer(item.get("offset_ms")) for item in samples if isinstance(item, dict)
    ]
    valid_offsets = [offset for offset in offsets if offset is not None]
    if len(valid_offsets) != len(samples):
        failures.append("every soak sample must carry an integer offset_ms")
    elif valid_offsets != sorted(valid_offsets):
        failures.append("soak sample offsets are not monotonic")
    gap_samples = sum(
        1 for item in samples if isinstance(item, dict) and item.get("quality") == "gap"
    )
    valid_samples = sum(
        1
        for item in samples
        if isinstance(item, dict) and item.get("quality") == "valid"
    )
    if summary.get("unclassified_gap_count") != gap_samples:
        failures.append("summary gap count does not match recorded gap samples")
    if metrics.get("successful_status_samples") != valid_samples:
        failures.append("successful_status_samples does not match valid samples")

    lease_renewals = (
        evidence.get("lease_renewals")
        if isinstance(evidence.get("lease_renewals"), list)
        else []
    )
    if metrics.get("lease_renewals") != len(lease_renewals):
        failures.append("summary lease_renewals does not match recorded renewal events")
    renewal_offsets: list[int] = []
    previous_requested_expiry: int | None = None
    renewal_target: str | None = None
    renewal_lease: str | None = None
    started_epoch_ms = (
        int(started_at.timestamp() * 1000) if started_at is not None else None
    )
    finished_epoch_ms = (
        int(finished_at.timestamp() * 1000) if finished_at is not None else None
    )
    for index, renewal in enumerate(lease_renewals):
        if not isinstance(renewal, dict):
            failures.append(f"lease renewal {index} is not an object")
            continue
        offset = integer(renewal.get("offset_ms"))
        observed_at = integer(renewal.get("observed_at_unix_ms"))
        previous_expiry = integer(renewal.get("previous_expires_at_unix_ms"))
        requested_expiry = integer(renewal.get("requested_expires_at_unix_ms"))
        if None in {offset, observed_at, previous_expiry, requested_expiry}:
            failures.append(f"lease renewal {index} omits an integer time field")
            continue
        assert offset is not None
        assert observed_at is not None
        assert previous_expiry is not None
        assert requested_expiry is not None
        renewal_offsets.append(offset)
        if previous_expiry <= observed_at:
            failures.append(f"lease renewal {index} occurred after lease expiry")
        if (
            requested_expiry <= previous_expiry
            or requested_expiry < observed_at + 240_000
        ):
            failures.append(
                f"lease renewal {index} did not strictly extend the lease horizon"
            )
        if (
            previous_requested_expiry is not None
            and previous_expiry != previous_requested_expiry
        ):
            failures.append(
                f"lease renewal {index} does not chain from its predecessor"
            )
        previous_requested_expiry = requested_expiry
        target_id = renewal.get("target_id")
        lease_id = renewal.get("lease_id")
        if index == 0:
            renewal_target = target_id if isinstance(target_id, str) else None
            renewal_lease = lease_id if isinstance(lease_id, str) else None
        elif target_id != renewal_target or lease_id != renewal_lease:
            failures.append(f"lease renewal {index} changed target or lease identity")
        if (
            started_epoch_ms is not None
            and abs((observed_at - started_epoch_ms) - offset) > 5_000
        ):
            failures.append(f"lease renewal {index} wall/monotonic observation drifted")
        if monotonic_elapsed_ms is not None and offset > monotonic_elapsed_ms + 100:
            failures.append(
                f"lease renewal {index} occurs after the monotonic run boundary"
            )
        if finished_epoch_ms is not None and observed_at > finished_epoch_ms + 1_000:
            failures.append(
                f"lease renewal {index} occurs after the wall-clock run boundary"
            )
    if renewal_offsets != sorted(renewal_offsets) or len(set(renewal_offsets)) != len(
        renewal_offsets
    ):
        failures.append("lease renewal offsets are not strictly increasing")

    observed_resource_fields = {
        "max_queue_depth": ("queue_depth", "aggregate_target_queue_depth"),
        "max_source_wal_bytes": ("source_wal_bytes", "source_wal_bytes"),
        "max_input_wal_bytes": ("input_wal_bytes", "input_wal_bytes"),
        "max_result_wal_bytes": ("result_wal_bytes", "result_wal_bytes"),
    }
    observed_maxima: dict[str, int] = {}
    for summary_field, (sample_field, limit_field) in observed_resource_fields.items():
        values: list[int] = []
        for item in samples:
            if not isinstance(item, dict) or item.get("quality") != "valid":
                continue
            if sample_field == "queue_depth":
                value = integer(item.get(sample_field))
            else:
                sample_metrics = item.get("module_metrics")
                value = (
                    integer(sample_metrics.get(sample_field))
                    if isinstance(sample_metrics, dict)
                    else None
                )
            if value is None:
                failures.append(f"valid soak sample omits {sample_field}")
            else:
                values.append(value)
        observed = max(values, default=0)
        observed_maxima[summary_field] = observed
        if metrics.get(summary_field) != observed:
            failures.append(f"summary {summary_field} does not match recorded samples")
        expected_limit_summary_field = (
            "aggregate_target_queue_depth_limit"
            if limit_field == "aggregate_target_queue_depth"
            else f"{limit_field}_limit"
        )
        if metrics.get(expected_limit_summary_field) != resource_limits.get(
            limit_field
        ):
            failures.append(
                f"summary {expected_limit_summary_field} does not match claim_scope"
            )
    for process_summary, sample_field in (
        ("max_rss_bytes", "rss_bytes"),
        ("max_fd_count", "fd_count"),
        ("max_thread_count", "thread_count"),
    ):
        observed = max(
            (
                value
                for item in samples
                if isinstance(item, dict) and item.get("quality") == "valid"
                if (value := integer(item.get(sample_field))) is not None
            ),
            default=0,
        )
        if metrics.get(process_summary) != observed:
            failures.append(
                f"summary {process_summary} does not match recorded samples"
            )
    if metrics.get("process_resource_threshold_status") != process_threshold_status:
        failures.append("summary process resource threshold status drifted")
    expected_resource_violations = sum(
        int(observed_maxima[summary_field] > int(resource_limits[limit_field]))
        for summary_field, (_, limit_field) in observed_resource_fields.items()
        if integer(resource_limits.get(limit_field)) is not None
    )
    if summary.get("resource_limit_violations") != expected_resource_violations:
        failures.append("resource_limit_violations does not match queue/WAL samples")

    if threshold_status != "FROZEN" and evidence.get("result") == "PASS":
        failures.append(
            "PASS is forbidden while DEC-001 absolute thresholds are not frozen"
        )
    if evidence.get("result") == "PASS" and evidence.get("level") == "REHEARSAL":
        failures.append("short rehearsal cannot qualify")

    successful_formal = evidence.get("level") == "MODULE" and evidence.get(
        "result"
    ) in {
        "PASS",
        "HOLD",
    }
    if successful_formal:
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
            failures.append(
                "monotonic clock does not prove the complete formal schedule"
            )
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
                failures.append(
                    f"formal phase {phase.get('name')} is incomplete or failed"
                )
            phase_metrics = (
                phase.get("module_metrics")
                if isinstance(phase.get("module_metrics"), dict)
                else {}
            )
            phase_start = integer(phase_metrics.get("phase_start_offset_ms"))
            phase_end = integer(phase_metrics.get("phase_end_offset_ms"))
            phase_elapsed = integer(phase.get("elapsed_ms"))
            phase_name = phase.get("name")
            if (
                not isinstance(phase_name, str)
                or phase_start is None
                or phase_end is None
                or phase_elapsed is None
                or phase_end <= phase_start
                or abs((phase_end - phase_start) - phase_elapsed) > 1_000
            ):
                failures.append(
                    f"formal phase {phase_name} has invalid monotonic boundaries"
                )
                continue
            if previous_phase_end is not None and not (
                previous_phase_end <= phase_start <= previous_phase_end + 5_000
            ):
                failures.append(
                    f"formal phase {phase_name} is not contiguous with its predecessor"
                )
            phase_bounds[phase_name] = (phase_start, phase_end)
            first_phase_start = (
                phase_start if first_phase_start is None else first_phase_start
            )
            last_phase_end = phase_end
            previous_phase_end = phase_end
        if (
            qualified_elapsed_ms is not None
            and first_phase_start is not None
            and last_phase_end is not None
            and abs((last_phase_end - first_phase_start) - qualified_elapsed_ms) > 5_000
        ):
            failures.append(
                "qualified_elapsed_ms does not match the four phase boundaries"
            )
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
                if (
                    warmup_elapsed_ms is None
                    or not phase_offsets
                    or phase_offsets[0] > expected_interval_ms
                    or phase_offsets[-1]
                    < warmup_elapsed_ms - (2 * expected_interval_ms)
                    or any(offset > warmup_elapsed_ms for offset in phase_offsets)
                ):
                    failures.append(
                        "warmup samples do not span the measured warmup window"
                    )
            elif phase_name in phase_bounds:
                phase_start, phase_end = phase_bounds[phase_name]
                if (
                    not phase_offsets
                    or phase_offsets[0] > phase_start + (2 * expected_interval_ms)
                    or phase_offsets[-1] < phase_end - (2 * expected_interval_ms)
                    or any(
                        not (phase_start <= offset <= phase_end)
                        for offset in phase_offsets
                    )
                ):
                    failures.append(
                        f"formal phase {phase_name} samples do not span its boundary"
                    )
        zero_summary_fields = (
            "error_count",
            "unclassified_gap_count",
            "oom_events",
            "container_restarts",
            "oracle_mismatches",
            "resource_limit_violations",
        )
        if any(summary.get(field) != 0 for field in zero_summary_fields):
            failures.append(
                "successful formal soak contains errors, gaps, or limit violations"
            )
        if not renewal_offsets:
            failures.append("formal soak did not prove public-boundary lease renewal")
        else:
            if renewal_offsets[0] > 130_000:
                failures.append("formal soak first lease renewal was too late")
            if any(
                current - previous > 130_000
                for previous, current in zip(renewal_offsets, renewal_offsets[1:])
            ):
                failures.append(
                    "formal soak lease renewal cadence exceeded 130 seconds"
                )
            if (
                finished_epoch_ms is not None
                and previous_requested_expiry is not None
                and previous_requested_expiry < finished_epoch_ms + 100_000
            ):
                failures.append(
                    "final renewed lease does not cover cleanup with a 100-second margin"
                )
        cleanup = (
            evidence.get("cleanup") if isinstance(evidence.get("cleanup"), dict) else {}
        )
        if cleanup != {
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
        }:
            failures.append("successful formal soak cleanup is incomplete")
        if evidence.get("interruption") != "NONE":
            failures.append("successful formal soak was interrupted")
        if metrics.get("formal_schedule_executed") is not True:
            failures.append("formal_schedule_executed is not true")
        if threshold_status == "FROZEN":
            if (
                evidence.get("result") != "PASS"
                or evidence.get("qualification") != "QUALIFIED"
            ):
                failures.append(
                    "frozen thresholds require successful formal evidence to qualify"
                )
        elif (
            evidence.get("result") != "HOLD"
            or evidence.get("qualification") != "NOT_QUALIFIED"
        ):
            failures.append(
                "unfrozen thresholds require formal evidence to remain HOLD/NOT_QUALIFIED"
            )

    if evidence.get("level") == "REHEARSAL":
        if lease_renewals:
            failures.append(
                "short rehearsal unexpectedly contains a formal lease renewal"
            )
        if metrics.get("formal_schedule_executed") is not False:
            failures.append(
                "rehearsal must not claim that the formal schedule executed"
            )
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
                failures.append(
                    "rehearsal unexpectedly contains a formal-duration phase"
                )

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
