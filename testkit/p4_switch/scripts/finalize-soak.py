from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    result = []
    if not path.exists():
        return result
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not an object")
        result.append(value)
    return result


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def artifact(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "name": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def running_container(sample: dict[str, object], name: str) -> dict[str, object] | None:
    value = sample.get(name)
    if isinstance(value, dict) and value.get("running"):
        return value
    return None


def nearest_resource(
    resources: list[dict[str, object]], target_ns: int
) -> tuple[dict[str, object] | None, int | None]:
    candidates = [
        sample
        for sample in resources
        if isinstance(sample.get("monotonic_ns"), int)
        and running_container(sample, "switch") is not None
        and running_container(sample, "runner") is not None
    ]
    if not candidates:
        return None, None

    def distance(item: dict[str, object]) -> int:
        monotonic_ns = item.get("monotonic_ns")
        assert isinstance(monotonic_ns, int)
        return abs(monotonic_ns - target_ns)

    selected = min(candidates, key=distance)
    selected_monotonic_ns = selected.get("monotonic_ns")
    assert isinstance(selected_monotonic_ns, int)
    distance_ms = abs(selected_monotonic_ns - target_ns) // 1_000_000
    return selected, distance_ms


def integer(container: dict[str, object], key: str) -> int:
    value = container.get(key, 0)
    return int(value) if isinstance(value, (int, float)) else 0


def number(container: dict[str, object], key: str) -> float:
    value = container.get(key, 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--phase-output", type=Path, required=True)
    parser.add_argument(
        "--runner-container-removed", choices=("true", "false"), required=True
    )
    args = parser.parse_args()
    workload = load_json(args.workload)
    resources = load_jsonl(args.resources)
    performance_profile = load_json(
        args.repo / "contracts/profiles/v1/p4-bmv2-functional-reference.json"
    )
    soak_profile_path = (
        args.repo / "contracts/profiles/v1/qualification-soak-3600s.json"
    )
    _soak_profile = load_json(soak_profile_path)
    traffic = workload.get("traffic", {})
    if not isinstance(traffic, dict):
        traffic = {}
    raw_samples = traffic.get("samples", [])
    if not isinstance(raw_samples, list):
        raw_samples = []
    traffic_start_ns = int(traffic.get("monotonic_start_ns", 0))
    thresholds = performance_profile["resource_thresholds"]
    assert isinstance(thresholds, dict)
    switch_threshold = thresholds["switch"]
    runner_threshold = thresholds["runner"]
    assert isinstance(switch_threshold, dict)
    assert isinstance(runner_threshold, dict)
    samples = []
    warmup = workload.get("warmup", {})
    warmup_hints = (
        warmup.get("supplemental_hints", {}) if isinstance(warmup, dict) else {}
    )
    warmup_queue_peak = (
        max(
            int(warmup_hints.get("request_queue_before", 0)),
            int(warmup_hints.get("response_queue_before", 0)),
            int(warmup_hints.get("request_queue_after", 0)),
            int(warmup_hints.get("response_queue_after", 0)),
        )
        if isinstance(warmup_hints, dict)
        else 0
    )
    resource_violation_count = int(warmup_queue_peak > thresholds["p4rpc_entity_queue"])
    resource_gap_count = 0
    switch_rss = []
    runner_rss = []
    switch_fd = []
    runner_fd = []
    switch_threads = []
    runner_threads = []
    max_oom = 0
    max_restart = 0
    for raw in raw_samples[:400]:
        if not isinstance(raw, dict):
            continue
        offset_ms = int(raw.get("offset_ms", 0))
        selected, distance_ms = nearest_resource(
            resources, traffic_start_ns + offset_ms * 1_000_000
        )
        metrics = raw.get("module_metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        if selected is None or distance_ms is None or distance_ms > 15_000:
            resource_gap_count += 1
            sample = {
                "offset_ms": offset_ms,
                "phase": raw.get("phase", "steady"),
                "quality": "gap",
                "cpu_pct": 0,
                "rss_bytes": 0,
                "fd_count": 0,
                "thread_count": 0,
                "queue_depth": max(
                    int(metrics.get("p4runtime_request_queue", 0)),
                    int(metrics.get("p4runtime_response_queue", 0)),
                    int(metrics.get("p4runtime_queue_peak_observed", 0)),
                ),
                "oom_events": 0,
                "container_restarts": 0,
                "oracle_errors": 0,
                "gap_count": 1,
                "module_metrics": {
                    **metrics,
                    "resource_sample_distance_ms": distance_ms,
                },
            }
            samples.append(sample)
            continue
        switch = running_container(selected, "switch")
        runner = running_container(selected, "runner")
        assert switch is not None and runner is not None
        switch_values = {
            "cpu_pct": number(switch, "cpu_pct"),
            "rss_bytes": integer(switch, "rss_bytes"),
            "fd_count": integer(switch, "fd_count"),
            "thread_count": integer(switch, "thread_count"),
        }
        runner_values = {
            "cpu_pct": number(runner, "cpu_pct"),
            "rss_bytes": integer(runner, "rss_bytes"),
            "fd_count": integer(runner, "fd_count"),
            "thread_count": integer(runner, "thread_count"),
        }
        switch_rss.append(switch_values["rss_bytes"])
        runner_rss.append(runner_values["rss_bytes"])
        switch_fd.append(switch_values["fd_count"])
        runner_fd.append(runner_values["fd_count"])
        switch_threads.append(switch_values["thread_count"])
        runner_threads.append(runner_values["thread_count"])
        oom = max(integer(switch, "oom_events"), integer(runner, "oom_events"))
        restart = max(
            integer(switch, "restart_count"), integer(runner, "restart_count")
        )
        max_oom = max(max_oom, oom)
        max_restart = max(max_restart, restart)
        violations = {
            "switch_cpu": switch_values["cpu_pct"] > switch_threshold["cpu_pct"],
            "switch_rss": switch_values["rss_bytes"] > switch_threshold["rss_bytes"],
            "switch_fd": switch_values["fd_count"] > switch_threshold["fd_count"],
            "switch_threads": switch_values["thread_count"]
            > switch_threshold["thread_count"],
            "runner_cpu": runner_values["cpu_pct"] > runner_threshold["cpu_pct"],
            "runner_rss": runner_values["rss_bytes"] > runner_threshold["rss_bytes"],
            "runner_fd": runner_values["fd_count"] > runner_threshold["fd_count"],
            "runner_threads": runner_values["thread_count"]
            > runner_threshold["thread_count"],
            "queue": max(
                int(metrics.get("p4runtime_request_queue", 0)),
                int(metrics.get("p4runtime_response_queue", 0)),
                int(metrics.get("p4runtime_queue_peak_observed", 0)),
            )
            > thresholds["p4rpc_entity_queue"],
            "oom": oom > thresholds["oom_events"],
            "restart": restart > 0,
            "selector_drift": bool(metrics.get("selector_drift", False)),
        }
        resource_violation_count += int(any(violations.values()))
        traffic_snapshot = raw.get("traffic", {})
        if not isinstance(traffic_snapshot, dict):
            traffic_snapshot = {}
        sample = {
            "offset_ms": offset_ms,
            "phase": raw.get("phase", "steady"),
            "quality": "valid",
            "cpu_pct": switch_values["cpu_pct"],
            "rss_bytes": switch_values["rss_bytes"],
            "fd_count": switch_values["fd_count"],
            "thread_count": switch_values["thread_count"],
            "queue_depth": max(
                int(metrics.get("p4runtime_request_queue", 0)),
                int(metrics.get("p4runtime_response_queue", 0)),
                int(metrics.get("p4runtime_queue_peak_observed", 0)),
            ),
            "oom_events": oom,
            "container_restarts": restart,
            # Accepted minus capture at a sampling instant is in-flight, not a
            # classified gap. Final oracle gaps are counted in the summary.
            "oracle_errors": 0,
            "gap_count": 0,
            "module_metrics": {
                **metrics,
                "resource_sample_distance_ms": distance_ms,
                "runner_cpu_pct": runner_values["cpu_pct"],
                "runner_rss_bytes": runner_values["rss_bytes"],
                "runner_fd_count": runner_values["fd_count"],
                "runner_thread_count": runner_values["thread_count"],
                "resource_violations": ",".join(
                    key for key, value in violations.items() if value
                ),
                "traffic_snapshot_present": bool(traffic_snapshot),
            },
        }
        samples.append(sample)

    traffic_phases = traffic.get("phases", [])
    if not isinstance(traffic_phases, list):
        traffic_phases = []
    phase_checks = workload.get("phase_checks", [])
    if not isinstance(phase_checks, list):
        phase_checks = []
    result_by_phase = {
        item.get("name"): item.get("result")
        for item in phase_checks
        if isinstance(item, dict)
    }
    phases = []
    oracle_gaps = 0
    for phase in traffic_phases:
        if not isinstance(phase, dict):
            continue
        oracle_gaps += int(phase.get("ingress_capture_gap", 0))
        oracle_gaps += int(phase.get("egress_capture_or_outcome_gap", 0))
        phases.append(
            {
                "name": phase["name"],
                "planned_ms": 900_000,
                "elapsed_ms": int(phase["planned_ms"]),
                "result": result_by_phase.get(phase["name"], "FAIL"),
                "requested_rate_pps": phase["requested_rate_pps"],
                "achieved_rate_pps": phase["achieved_rate_pps"],
                "errors": int(
                    phase.get("ingress_capture_gap", 0)
                    + phase.get("egress_capture_or_outcome_gap", 0)
                ),
                "module_metrics": {
                    "sender_accepted": phase.get("sender_accepted"),
                    "test_ingress_packets": phase.get("test_ingress_packets"),
                    "dut_egress_packets": phase.get("dut_egress_packets"),
                    "latency_p99_ms": phase.get("latency_ms", {}).get("p99")
                    if isinstance(phase.get("latency_ms"), dict)
                    else None,
                },
            }
        )
    rss_growth_limit = performance_profile["soak_workload"]["rss_growth_max_bytes"]
    switch_rss_growth = max(switch_rss, default=0) - (
        switch_rss[0] if switch_rss else 0
    )
    runner_rss_growth = max(runner_rss, default=0) - (
        runner_rss[0] if runner_rss else 0
    )
    residuals = {
        "switch_fd": (switch_fd[-1] - switch_fd[0]) if switch_fd else 0,
        "runner_fd": (runner_fd[-1] - runner_fd[0]) if runner_fd else 0,
        "switch_threads": (
            switch_threads[-1] - switch_threads[0] if switch_threads else 0
        ),
        "runner_threads": (
            runner_threads[-1] - runner_threads[0] if runner_threads else 0
        ),
        "queue": max(
            int(workload.get("cleanup", {}).get("queue_depths", {}).get("request", 0)),
            int(workload.get("cleanup", {}).get("queue_depths", {}).get("response", 0)),
        )
        if isinstance(workload.get("cleanup"), dict)
        and isinstance(workload.get("cleanup", {}).get("queue_depths"), dict)
        else 0,
    }
    residual_violation = any(value > 0 for value in residuals.values())
    growth_violation = (
        switch_rss_growth > rss_growth_limit or runner_rss_growth > rss_growth_limit
    )
    if growth_violation:
        resource_violation_count += 1
    if residual_violation:
        resource_violation_count += 1
    formal = workload.get("mode") == "formal"
    elapsed_ms = int(
        (int(traffic.get("monotonic_scheduled_end_ns", 0)) - traffic_start_ns)
        / 1_000_000
    )
    cleanup_source = workload.get("cleanup", {})
    if not isinstance(cleanup_source, dict):
        cleanup_source = {}
    runner_removed = args.runner_container_removed == "true"
    cleanup_completed = bool(cleanup_source.get("completed")) and runner_removed
    workload_errors = workload.get("errors", [])
    if not isinstance(workload_errors, list):
        workload_errors = ["invalid workload errors field"]
    error_count = len(workload_errors)
    activation_checks = workload.get("activation_checks", [])
    if not isinstance(activation_checks, list):
        activation_checks = []
    complete = (
        formal
        and workload.get("result") == "PASS"
        and elapsed_ms >= 3_600_000
        and len(phases) == 4
        and all(phase["result"] == "PASS" for phase in phases)
        and len(activation_checks) == 30
        and all(bool(value) for value in activation_checks)
        and error_count == 0
        and oracle_gaps == 0
        and resource_gap_count == 0
        and resource_violation_count == 0
        and max_oom == 0
        and max_restart == 0
        and cleanup_completed
    )
    failed = workload.get("result") == "FAIL" or error_count > 0 or oracle_gaps > 0
    if complete:
        result = "PASS"
    elif failed:
        result = "FAIL"
    else:
        result = "HOLD"
    claim_scope = workload.get("claim_scope", {})
    if not isinstance(claim_scope, dict):
        claim_scope = {}
    claim_scope_digest = digest_bytes(
        json.dumps(claim_scope, sort_keys=True, separators=(",", ":")).encode()
    )
    evidence = {
        "schema_version": "qualification-soak/v1",
        "run_id": str(workload.get("run_id", "unknown-run")),
        "module": "p4-switch",
        "requirement_ids": ["DEC-042", "PERF-P4-FW-001", "TEST-P4-FW-001"],
        "level": "MODULE" if formal else "REHEARSAL",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
        "profile_digest": digest_bytes(soak_profile_path.read_bytes()),
        "claim_scope_digest": claim_scope_digest,
        "started_at": str(workload.get("started_at", utc_now())),
        "finished_at": str(workload.get("finished_at", utc_now())),
        "monotonic_start_ns": traffic_start_ns,
        "monotonic_end_ns": int(traffic.get("monotonic_scheduled_end_ns", 1)),
        "warmup_elapsed_ms": int(
            float(workload.get("parameters", {}).get("warmup_seconds", 0)) * 1000
            if isinstance(workload.get("parameters"), dict)
            else 0
        ),
        "duration_target_ms": 3_600_000,
        "qualified_elapsed_ms": elapsed_ms,
        "sample_interval_ms": int(
            float(workload.get("parameters", {}).get("sample_seconds", 10)) * 1000
            if isinstance(workload.get("parameters"), dict)
            else 10_000
        ),
        "phases": phases,
        "samples": samples
        or [
            {
                "offset_ms": 0,
                "phase": "warmup",
                "quality": "gap",
                "cpu_pct": 0,
                "rss_bytes": 0,
                "fd_count": 0,
                "thread_count": 0,
                "queue_depth": 0,
                "oom_events": 0,
                "container_restarts": 0,
                "oracle_errors": 1,
                "gap_count": 1,
            }
        ],
        "summary": {
            "error_count": error_count,
            "unclassified_gap_count": oracle_gaps + resource_gap_count,
            "oom_events": max_oom,
            "container_restarts": max_restart,
            "oracle_mismatches": oracle_gaps,
            "resource_limit_violations": resource_violation_count,
            "module_metrics": {
                "activation_cycles": len(activation_checks),
                "switch_rss_growth_bytes": switch_rss_growth,
                "runner_rss_growth_bytes": runner_rss_growth,
                "rss_growth_limit_bytes": rss_growth_limit,
                "warmup_queue_peak": warmup_queue_peak,
                "post_cleanup_residuals": json.dumps(residuals, sort_keys=True),
            },
        },
        "interruption": "NONE"
        if workload.get("result") != "FAIL"
        else "EVIDENCE_FAILURE",
        "cleanup": {
            "attempted": bool(cleanup_source.get("attempted")),
            "completed": cleanup_completed,
            "exit_code": 0 if cleanup_completed else 1,
            "remaining_resources": []
            if cleanup_completed
            else ["runner-or-p4-workload-state"],
        },
        "artifacts": [artifact(args.workload), artifact(args.resources)],
    }
    schema = load_json(args.repo / "contracts/evidence/soak/v1/schema.json")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(evidence), key=lambda item: list(item.path))
    if errors:
        raise SystemExit("\n".join(error.message for error in errors))
    args.evidence_output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    phase = {
        "phase": "soak",
        "level": evidence["level"],
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": evidence["qualification"],
        "tests": [
            {
                "id": "TEST-P4-SOAK-3600S-001",
                "requirement_ids": [
                    "DEC-042",
                    "PERF-P4-FW-001",
                    "TEST-P4-FW-001",
                ],
                "level": evidence["level"],
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": evidence["qualification"],
                "evidence": {
                    "soak_evidence": args.evidence_output.name,
                    "soak_evidence_sha256": digest_bytes(
                        args.evidence_output.read_bytes()
                    ),
                    "qualified_elapsed_ms": elapsed_ms,
                    "activation_cycles": len(activation_checks),
                    "summary": evidence["summary"],
                    "cleanup": evidence["cleanup"],
                },
            }
        ],
    }
    args.phase_output.write_text(
        json.dumps(phase, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if result == "FAIL":
        return 1
    if result == "HOLD":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
