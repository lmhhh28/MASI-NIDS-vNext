from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from p4.v1 import p4runtime_pb2

from testkit.p4_switch.lib.p4runtime_client import (
    P4InfoIndex,
    P4RuntimeClient,
    bmv2_device_config,
    reject_equal_priority_conflicts,
    selector_entity,
)
from testkit.p4_switch.lib.qualification_workload import (
    TrafficHarness,
    WorkloadPhase,
    bootstrap_mean_ci,
    delete_rules,
    exact_rule_readback,
    install_infrastructure,
    make_baseline_rules,
    read_policy_bank,
    sha256_bytes,
    table_entry_count,
    validate_packet_oracle_profile,
    write_rules,
)

# The pinned protobuf enum members are generated dynamically and do not expose
# static attribute stubs.  Other type diagnostics remain enabled.
# pyright: reportAttributeAccessIssue=false


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--cert-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-digest", required=True)
    parser.add_argument("--runner-digest", required=True)
    parser.add_argument("--mode", choices=("formal", "rehearsal"), default="formal")
    parser.add_argument("--rehearsal-warmup-seconds", type=float, default=1.0)
    parser.add_argument("--rehearsal-measurement-seconds", type=float, default=1.0)
    parser.add_argument("--rehearsal-repeats", type=int, default=1)
    parser.add_argument("--rehearsal-rule-counts", default="0,128,1024,4096")
    return parser.parse_args()


def normalize_digest(value: str) -> str:
    if value.startswith("sha256:") and len(value) == 71:
        return value
    raise ValueError(f"not an exact sha256 digest: {value}")


def finite_at_most(value: object, upper_bound: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(upper_bound, bool) or not isinstance(upper_bound, (int, float)):
        return False
    numeric = float(value)
    return math.isfinite(numeric) and numeric <= float(upper_bound)


def finite_max(values: list[object]) -> float | None:
    numeric = [
        float(value)
        for value in values
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    ]
    return max(numeric) if numeric else None


def main() -> int:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    profile_path = args.repo / "contracts/profiles/v1/p4-bmv2-functional-reference.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    p4_profile_path = (
        args.repo / "contracts/profiles/v1/p4-stateless-firewall-bmv2.json"
    )
    p4_profile = json.loads(p4_profile_path.read_text(encoding="utf-8"))
    formal = args.mode == "formal"
    statistics = profile["statistics"]
    validate_packet_oracle_profile(profile["packet_oracle_runtime"])
    workload_bounds = profile["workload_resource_bounds"]
    latency_capacity = int(workload_bounds["latency_samples_per_phase"])
    max_packets = int(workload_bounds["maximum_packets_per_run"])
    max_samples = int(workload_bounds["maximum_samples_per_run"])
    if (
        workload_bounds["latency_sampling_method"]
        != "deterministic-reservoir-splitmix64/v1"
        or not workload_bounds["latency_exact_max"]
    ):
        raise RuntimeError("unsupported latency sampling profile")
    if formal:
        warmup_seconds = float(statistics["warmup_seconds"])
        measurement_seconds = float(statistics["measurement_window_seconds"])
        repeats = int(statistics["independent_repeats"])
        rule_counts = [item["rule_count"] for item in profile["rule_thresholds"]]
    else:
        warmup_seconds = args.rehearsal_warmup_seconds
        measurement_seconds = args.rehearsal_measurement_seconds
        repeats = args.rehearsal_repeats
        rule_counts = [int(item) for item in args.rehearsal_rule_counts.split(",")]
    if warmup_seconds <= 0 or measurement_seconds <= 0 or repeats <= 0:
        raise ValueError("all benchmark time/count parameters must be positive")
    if formal and (
        warmup_seconds != 60
        or measurement_seconds != 30
        or repeats != 5
        or rule_counts != [0, 128, 1024, 4096]
    ):
        raise RuntimeError(
            "formal benchmark parameters diverge from the frozen profile"
        )
    if any(count not in {0, 128, 1024, 4096} for count in rule_counts):
        raise ValueError("unsupported rehearsal rule count")

    started_at = utc_now()
    started_ns = time.perf_counter_ns()
    index = P4InfoIndex(args.artifacts / "masi_switch.p4info.txtpb")
    program = (args.artifacts / "masi_switch.json").read_bytes()
    p4info_bytes = (args.artifacts / "masi_switch.p4info.txtpb").read_bytes()
    client = P4RuntimeClient(
        "127.0.0.1:9559",
        device_id=1,
        election_id=100,
        ca_path=args.cert_dir / "ca.crt",
        cert_path=args.cert_dir / "client.crt",
        key_path=args.cert_dir / "client.key",
        rpc_timeout=30,
    )
    warmup: dict[str, Any] = {}
    measurements: list[dict[str, Any]] = []
    unexpected_errors: list[str] = []
    cleanup: dict[str, Any] = {"attempted": False, "completed": False}
    try:
        identity = client.set_pipeline(index.p4info, program)
        client.verify_pipeline_identity(identity)
        install_infrastructure(index, client)
        warmup = TrafficHarness(f"{args.run_id}:benchmark:warmup").run(
            [WorkloadPhase("warmup", warmup_seconds, 1000, 1)],
            latency_sample_capacity_per_phase=latency_capacity,
            max_packets_per_run=max_packets,
            max_samples_per_run=max_samples,
        )
        warmup["supplemental_hints"] = client.drain_supplemental_hints()
        for count in rule_counts:
            for repeat in range(repeats):
                identity = client.set_pipeline(index.p4info, program)
                client.verify_pipeline_identity(identity)
                install_infrastructure(index, client)
                inactive_bank = 1
                preflight_started = time.perf_counter_ns()
                rules = make_baseline_rules(
                    index,
                    inactive_bank,
                    count,
                    generation=count * 10 + repeat,
                )
                reject_equal_priority_conflicts(rules)
                preflight_ms = (time.perf_counter_ns() - preflight_started) / 1e6

                write_started = time.perf_counter_ns()
                write_rules(client, rules)
                write_ms = (time.perf_counter_ns() - write_started) / 1e6

                readback_started = time.perf_counter_ns()
                if rules:
                    readback_exact = exact_rule_readback(index, client, rules)
                else:
                    readback_exact = (
                        table_entry_count(index, client, "baseline_bank_1") == 0
                    )
                readback_ms = (time.perf_counter_ns() - readback_started) / 1e6

                flip_started = time.perf_counter_ns()
                client.write(
                    p4runtime_pb2.Update.MODIFY,
                    [
                        selector_entity(
                            index,
                            "policy_selector",
                            "select_policy_bank_1",
                        )
                    ],
                )
                active_bank = read_policy_bank(index, client)
                selector_flip_ms = (time.perf_counter_ns() - flip_started) / 1e6

                workload = [
                    "baseline-only/uniform",
                    "response-only/hotspot",
                    "mixed/uniform",
                    "mixed/hotspot",
                    "mixed/miss",
                ][repeat % 5]
                traffic: dict[str, Any] = TrafficHarness(
                    f"{args.run_id}:benchmark:{count}:{repeat}"
                ).run(
                    [
                        WorkloadPhase(
                            "measurement",
                            measurement_seconds,
                            1000,
                            2,
                        )
                    ],
                    latency_sample_capacity_per_phase=latency_capacity,
                    max_packets_per_run=max_packets,
                    max_samples_per_run=max_samples,
                )
                traffic_phase = traffic["phases"][0]
                supplemental_hints = client.drain_supplemental_hints()

                rollback_started = time.perf_counter_ns()
                client.write(
                    p4runtime_pb2.Update.MODIFY,
                    [
                        selector_entity(
                            index,
                            "policy_selector",
                            "select_policy_bank_0",
                        )
                    ],
                )
                rollback_bank = read_policy_bank(index, client)
                rollback_ms = (time.perf_counter_ns() - rollback_started) / 1e6

                cleanup_started = time.perf_counter_ns()
                if rules:
                    delete_rules(client, rules)
                residual_entries = table_entry_count(index, client, "baseline_bank_1")
                cleanup_ms = (time.perf_counter_ns() - cleanup_started) / 1e6
                measurements.append(
                    {
                        "rule_count": count,
                        "repeat": repeat + 1,
                        "workload": workload,
                        "preflight_ms": round(preflight_ms, 6),
                        "write_ms": round(write_ms, 6),
                        "readback_ms": round(readback_ms, 6),
                        "selector_flip_ms": round(selector_flip_ms, 6),
                        "rollback_ms": round(rollback_ms, 6),
                        "cleanup_ms": round(cleanup_ms, 6),
                        "readback_exact": readback_exact,
                        "active_bank_after_flip": active_bank,
                        "active_bank_after_rollback": rollback_bank,
                        "cleanup_residual_entries": residual_entries,
                        "traffic": traffic_phase,
                        "traffic_errors": traffic["errors"],
                        "supplemental_hints": supplemental_hints,
                        "queue_depths": client.queue_depths(),
                    }
                )
        cleanup["attempted"] = True
        identity = client.set_pipeline(index.p4info, program)
        client.verify_pipeline_identity(identity)
        residuals = {
            "baseline_bank_0": table_entry_count(index, client, "baseline_bank_0"),
            "baseline_bank_1": table_entry_count(index, client, "baseline_bank_1"),
            "response_overlay": table_entry_count(index, client, "response_overlay"),
        }
        cleanup.update(
            {
                "completed": all(value == 0 for value in residuals.values()),
                "residual_entries": residuals,
            }
        )
    except BaseException as exc:
        unexpected_errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        client.close()

    threshold_by_count = {
        item["rule_count"]: item for item in profile["rule_thresholds"]
    }
    matrix = []
    for count in rule_counts:
        rows = [item for item in measurements if item["rule_count"] == count]
        threshold = threshold_by_count[count]
        traffic_rows = [item["traffic"] for item in rows]
        pps_values = [float(item["achieved_rate_pps"]) for item in traffic_rows]
        bps_values = [float(item["achieved_rate_bps"]) for item in traffic_rows]
        latency_keys = ("p50", "p95", "p99", "max")
        operation_keys = (
            "preflight_ms",
            "write_ms",
            "readback_ms",
            "selector_flip_ms",
            "rollback_ms",
            "cleanup_ms",
        )
        latency_max = {
            key: finite_max(
                [
                    latency[key]
                    for item in traffic_rows
                    if isinstance((latency := item.get("latency_ms")), dict)
                    and key in latency
                ]
            )
            for key in latency_keys
        }
        operation_max = {
            key: finite_max([item.get(key) for item in rows]) for key in operation_keys
        }
        packet_thresholds = profile["packet_thresholds"]
        operation_thresholds = profile["operation_thresholds_ms"]
        checks = {
            "repeat_count": len(rows) == repeats,
            "preflight": bool(rows)
            and finite_at_most(
                operation_max["preflight_ms"], operation_thresholds["preflight"]
            ),
            "write": bool(rows)
            and finite_at_most(operation_max["write_ms"], threshold["write_ms"]),
            "readback": bool(rows)
            and finite_at_most(operation_max["readback_ms"], threshold["readback_ms"]),
            "selector_flip": bool(rows)
            and finite_at_most(
                operation_max["selector_flip_ms"], threshold["selector_flip_ms"]
            ),
            "rollback": bool(rows)
            and finite_at_most(
                operation_max["rollback_ms"], operation_thresholds["rollback"]
            ),
            "cleanup": bool(rows)
            and finite_at_most(
                operation_max["cleanup_ms"], operation_thresholds["cleanup"]
            ),
            "readback_and_cleanup": all(
                bool(item["readback_exact"])
                and item["active_bank_after_flip"] == 1
                and item["active_bank_after_rollback"] == 0
                and item["cleanup_residual_entries"] == 0
                for item in rows
            ),
            "minimum_pps": bool(pps_values)
            and min(pps_values) >= packet_thresholds["minimum_achieved_pps"],
            "minimum_bps": bool(bps_values)
            and min(bps_values) >= packet_thresholds["minimum_achieved_bps"],
            "latency": all(
                bool(traffic_rows)
                and finite_at_most(
                    latency_max[key], packet_thresholds["maximum_latency_ms"][key]
                )
                for key in latency_keys
            ),
            "packet_oracle": all(
                not item["traffic_errors"]
                and item["traffic"]["ingress_capture_gap"] == 0
                and item["traffic"]["egress_capture_or_outcome_gap"] == 0
                for item in rows
            ),
        }
        row_pass = all(checks.values())
        matrix.append(
            {
                "rule_count": count,
                "threshold": threshold,
                "operation_max_ms": operation_max,
                "minimum_achieved_pps": min(pps_values) if pps_values else 0,
                "minimum_achieved_bps": min(bps_values) if bps_values else 0,
                "latency_max_ms": latency_max,
                "pps_bootstrap_ci": (
                    bootstrap_mean_ci(pps_values) if pps_values else None
                ),
                "checks": checks,
                "result": "PASS" if row_pass else "FAIL",
                "repeats": rows,
            }
        )

    warmup_errors = list(warmup.get("errors", []))
    warmup_phases = warmup.get("phases", [])
    warmup_gap = any(
        phase["ingress_capture_gap"] or phase["egress_capture_or_outcome_gap"]
        for phase in warmup_phases
    )
    raw_pass = (
        not unexpected_errors
        and not warmup_errors
        and not warmup_gap
        and bool(cleanup.get("completed"))
        and len(matrix) == len(rule_counts)
        and all(item["result"] == "PASS" for item in matrix)
    )
    if formal:
        result = "PASS" if raw_pass else "FAIL"
        level = "MODULE"
    else:
        result = "HOLD" if raw_pass else "FAIL"
        level = "REHEARSAL"
    qualification = "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED"
    document = {
        "schema_version": "qualification-performance-workload/v1",
        "phase": "performance-workload",
        "run_id": args.run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "monotonic_elapsed_ms": round((time.perf_counter_ns() - started_ns) / 1e6),
        "level": level,
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": qualification,
        "mode": args.mode,
        "requirement_ids": [
            "PERF-001",
            "PERF-002",
            "PERF-P4-FW-001",
            "TEST-P4-FW-001",
        ],
        "profiles": {
            "performance": sha256_bytes(profile_path.read_bytes()),
            "target": sha256_bytes(p4_profile_path.read_bytes()),
        },
        "claim_scope": {
            "runtime_digest": normalize_digest(args.runtime_digest),
            "runner_digest": normalize_digest(args.runner_digest),
            "program_digest": sha256_bytes(program),
            "p4info_artifact_digest": sha256_bytes(p4info_bytes),
            "device_config_digest": sha256_bytes(bmv2_device_config(program)),
            "target_runtime_image": p4_profile["target"]["runtime_image"],
        },
        "parameters": {
            "frame_bytes": profile["frame_bytes"],
            "warmup_seconds": warmup_seconds,
            "measurement_window_seconds": measurement_seconds,
            "independent_repeats": repeats,
            "rule_counts": rule_counts,
            "outlier_policy": statistics["outlier_policy"],
            "confidence_method": statistics["confidence_method"],
            "workload_resource_bounds": workload_bounds,
            "packet_oracle_runtime": profile["packet_oracle_runtime"],
        },
        "warmup": warmup,
        "matrix": matrix,
        "cleanup": cleanup,
        "unexpected_errors": unexpected_errors,
    }
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return 0 if result != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
