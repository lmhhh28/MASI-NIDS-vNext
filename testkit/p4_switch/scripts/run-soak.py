from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from p4.v1 import p4runtime_pb2

from testkit.p4_switch.lib.p4runtime_client import (
    P4InfoIndex,
    P4RuntimeClient,
    bmv2_device_config,
    counter_entity,
    direct_counter_entity,
    selector_entity,
    table_entity,
    table_key_entity,
)
from testkit.p4_switch.lib.qualification_workload import (
    TrafficHarness,
    WorkloadPhase,
    delete_rules,
    exact_rule_readback,
    install_infrastructure,
    make_baseline_rules,
    read_policy_bank,
    sha256_bytes,
    single_packet_outcome,
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
    parser.add_argument("--rehearsal-phase-seconds", type=float, default=2.0)
    parser.add_argument("--rehearsal-sample-seconds", type=float, default=1.0)
    return parser.parse_args()


def exact_digest(value: str) -> str:
    if value.startswith("sha256:") and len(value) == 71:
        return value
    raise ValueError(f"invalid exact digest: {value}")


def main() -> int:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    soak_profile_path = (
        args.repo / "contracts/profiles/v1/qualification-soak-3600s.json"
    )
    performance_profile_path = (
        args.repo / "contracts/profiles/v1/p4-bmv2-functional-reference.json"
    )
    target_profile_path = (
        args.repo / "contracts/profiles/v1/p4-stateless-firewall-bmv2.json"
    )
    soak_profile = json.loads(soak_profile_path.read_text(encoding="utf-8"))
    performance_profile = json.loads(
        performance_profile_path.read_text(encoding="utf-8")
    )
    target_profile = json.loads(target_profile_path.read_text(encoding="utf-8"))
    validate_packet_oracle_profile(performance_profile["packet_oracle_runtime"])
    workload_bounds = performance_profile["workload_resource_bounds"]
    latency_capacity = int(workload_bounds["latency_samples_per_phase"])
    max_packets = int(workload_bounds["maximum_packets_per_run"])
    max_samples = int(workload_bounds["maximum_samples_per_run"])
    if (
        workload_bounds["latency_sampling_method"]
        != "deterministic-reservoir-splitmix64/v1"
        or not workload_bounds["latency_exact_max"]
    ):
        raise RuntimeError("unsupported latency sampling profile")
    formal = args.mode == "formal"
    if formal:
        warmup_seconds = 60.0
        phase_seconds = 900.0
        sample_seconds = 10.0
        activation_interval_seconds = 30.0
        expected_activation_cycles = 30
    else:
        warmup_seconds = args.rehearsal_warmup_seconds
        phase_seconds = args.rehearsal_phase_seconds
        sample_seconds = args.rehearsal_sample_seconds
        activation_interval_seconds = sample_seconds
        expected_activation_cycles = max(1, int(phase_seconds / sample_seconds))
    if min(warmup_seconds, phase_seconds, sample_seconds) <= 0:
        raise ValueError("soak timing values must be positive")
    if formal and (
        soak_profile["warmup_seconds"] != 60
        or soak_profile["qualified_duration_seconds"] != 3600
        or soak_profile["sample_interval_seconds"] != 10
        or [item["duration_seconds"] for item in soak_profile["phases"]]
        != [900, 900, 900, 900]
    ):
        raise RuntimeError("formal soak timing diverges from the frozen profile")

    started_at = utc_now()
    run_started_ns = time.perf_counter_ns()
    index = P4InfoIndex(args.artifacts / "masi_switch.p4info.txtpb")
    program = (args.artifacts / "masi_switch.json").read_bytes()
    p4info_bytes = (args.artifacts / "masi_switch.p4info.txtpb").read_bytes()
    client = P4RuntimeClient(
        "127.0.0.1:9559",
        device_id=1,
        election_id=200,
        ca_path=args.cert_dir / "ca.crt",
        cert_path=args.cert_dir / "client.crt",
        key_path=args.cert_dir / "client.key",
        rpc_timeout=30,
    )
    warmup: dict[str, Any] = {}
    traffic: dict[str, Any] = {}
    activations: list[dict[str, Any]] = []
    callback_errors: list[str] = []
    cleanup: dict[str, Any] = {"attempted": False, "completed": False}
    active_bank = 0
    rules_by_bank: dict[int, list[Any]] = {0: [], 1: []}
    last_activation_cycle = -1
    try:
        identity = client.set_pipeline(index.p4info, program)
        client.verify_pipeline_identity(identity)
        install_infrastructure(index, client)
        initial_rules = make_baseline_rules(index, 0, 1024, generation=1)
        write_rules(client, initial_rules)
        if not exact_rule_readback(index, client, initial_rules):
            raise RuntimeError("initial 1024-rule readback mismatch")
        rules_by_bank[0] = initial_rules
        warmup = TrafficHarness(f"{args.run_id}:soak:warmup").run(
            [WorkloadPhase("warmup", warmup_seconds, 500, 1)],
            latency_sample_capacity_per_phase=latency_capacity,
            max_packets_per_run=max_packets,
            max_samples_per_run=max_samples,
        )
        warmup["supplemental_hints"] = client.drain_supplemental_hints()
        workload_rates = performance_profile["soak_workload"]
        phases = [
            WorkloadPhase("steady", phase_seconds, workload_rates["steady_pps"], 2),
            WorkloadPhase("peak", phase_seconds, workload_rates["peak_pps"], 3),
            WorkloadPhase(
                "saturation",
                phase_seconds,
                workload_rates["saturation_requested_pps"],
                4,
            ),
            WorkloadPhase(
                "recovery-or-activation",
                phase_seconds,
                workload_rates["activation_pps"],
                5,
            ),
        ]
        activation_start_ms = round(3 * phase_seconds * 1000)
        activation_interval_ms = round(activation_interval_seconds * 1000)
        qualified_end_ms = round(4 * phase_seconds * 1000)

        def sample_callback(
            offset_ms: int,
            phase_name: str,
            _traffic_snapshot: dict[str, object],
        ) -> dict[str, object]:
            nonlocal active_bank, last_activation_cycle
            module_metrics: dict[str, object] = {}
            if phase_name == "recovery-or-activation" and offset_ms < qualified_end_ms:
                activation_offset_ms = offset_ms - activation_start_ms
                cycle = max(0, activation_offset_ms // activation_interval_ms)
                if cycle > last_activation_cycle:
                    last_activation_cycle = cycle
                    target_bank = 1 - active_bank
                    desired_count = (0, 128, 1024, 4096)[cycle % 4]
                    prior_rules = rules_by_bank[target_bank]
                    operation_started = time.perf_counter_ns()
                    if prior_rules:
                        delete_rules(client, prior_rules)
                    candidate = make_baseline_rules(
                        index,
                        target_bank,
                        desired_count,
                        generation=100 + cycle,
                    )
                    preflight_finished = time.perf_counter_ns()
                    write_rules(client, candidate)
                    write_finished = time.perf_counter_ns()
                    readback_exact = exact_rule_readback(index, client, candidate)
                    read_finished = time.perf_counter_ns()
                    client.write(
                        p4runtime_pb2.Update.MODIFY,
                        [
                            selector_entity(
                                index,
                                "policy_selector",
                                f"select_policy_bank_{target_bank}",
                            )
                        ],
                    )
                    selected = read_policy_bank(index, client)
                    flip_finished = time.perf_counter_ns()
                    # Exercise rollback/reconcile without changing the final desired bank.
                    client.write(
                        p4runtime_pb2.Update.MODIFY,
                        [
                            selector_entity(
                                index,
                                "policy_selector",
                                f"select_policy_bank_{active_bank}",
                            )
                        ],
                    )
                    rollback_selected = read_policy_bank(index, client)
                    rollback_finished = time.perf_counter_ns()
                    client.write(
                        p4runtime_pb2.Update.MODIFY,
                        [
                            selector_entity(
                                index,
                                "policy_selector",
                                f"select_policy_bank_{target_bank}",
                            )
                        ],
                    )
                    reconciled = read_policy_bank(index, client)

                    overlay = index.overlay_entry(
                        src_ipv4="192.0.2.249",
                        dst_ipv4="198.51.100.10",
                        protocol=17,
                        src_port=55001,
                        dst_port=8080,
                        action="drop",
                    )
                    client.write(p4runtime_pb2.Update.INSERT, [table_entity(overlay)])
                    overlay_readback = client.read([table_key_entity(overlay)])
                    overlay_before = client.read([direct_counter_entity(overlay)])[
                        0
                    ].direct_counter_entry.data.packet_count
                    overlay_drop = single_packet_outcome(
                        f"{args.run_id}:overlay:{cycle}",
                        expect_forward=False,
                        source_ip="192.0.2.249",
                        source_port=55001,
                    )
                    overlay_after = client.read([direct_counter_entity(overlay)])[
                        0
                    ].direct_counter_entry.data.packet_count
                    client.write(
                        p4runtime_pb2.Update.DELETE,
                        [table_key_entity(overlay)],
                    )
                    overlay_deleted = client.read([table_key_entity(overlay)]) == []
                    overlay_forward = single_packet_outcome(
                        f"{args.run_id}:overlay-clean:{cycle}",
                        expect_forward=True,
                        source_ip="192.0.2.249",
                        source_port=55001,
                    )
                    baseline_probe: dict[str, object] | None = None
                    direct_delta: int | None = None
                    if candidate:
                        direct_before = client.read(
                            [direct_counter_entity(candidate[0])]
                        )[0].direct_counter_entry.data.packet_count
                        baseline_probe = single_packet_outcome(
                            f"{args.run_id}:baseline:{cycle}",
                            expect_forward=False,
                            source_ip="192.0.2.250",
                            source_port=55000,
                        )
                        direct_after = client.read(
                            [direct_counter_entity(candidate[0])]
                        )[0].direct_counter_entry.data.packet_count
                        direct_delta = direct_after - direct_before
                    rules_by_bank[target_bank] = candidate
                    active_bank = target_bank
                    activation = {
                        "cycle": cycle + 1,
                        "rule_count": desired_count,
                        "target_bank": target_bank,
                        "old_bank": 1 - target_bank,
                        "preflight_ms": round(
                            (preflight_finished - operation_started) / 1e6, 6
                        ),
                        "write_ms": round(
                            (write_finished - preflight_finished) / 1e6, 6
                        ),
                        "readback_ms": round((read_finished - write_finished) / 1e6, 6),
                        "selector_flip_readback_ms": round(
                            (flip_finished - read_finished) / 1e6, 6
                        ),
                        "rollback_readback_ms": round(
                            (rollback_finished - flip_finished) / 1e6, 6
                        ),
                        "readback_exact": readback_exact,
                        "selected_bank": selected,
                        "rollback_selected_bank": rollback_selected,
                        "reconciled_bank": reconciled,
                        "baseline_drop_probe": baseline_probe,
                        "baseline_direct_counter_delta": direct_delta,
                        "overlay": {
                            "installation_readback_count": len(overlay_readback),
                            "drop_probe": overlay_drop,
                            "direct_counter_delta": overlay_after - overlay_before,
                            "expiry_delete_readback_empty": overlay_deleted,
                            "post_expiry_forward_probe": overlay_forward,
                        },
                    }
                    activations.append(activation)
                    module_metrics["activation_cycle"] = cycle + 1
                    module_metrics["activation_result"] = all(
                        (
                            readback_exact,
                            selected == target_bank,
                            rollback_selected == 1 - target_bank,
                            reconciled == target_bank,
                            overlay_drop["outcome_matches"],
                            overlay_after - overlay_before == 1,
                            overlay_deleted,
                            overlay_forward["outcome_matches"],
                            direct_delta in (None, 1),
                            baseline_probe is None
                            or bool(baseline_probe["outcome_matches"]),
                        )
                    )
            selector = read_policy_bank(index, client)
            baseline_entries = {
                "bank_0": table_entry_count(index, client, "baseline_bank_0"),
                "bank_1": table_entry_count(index, client, "baseline_bank_1"),
                "overlay": table_entry_count(index, client, "response_overlay"),
            }
            eligible_id = index.counter_id("baseline_eligible_counter")
            eligible = sum(
                client.read([counter_entity(eligible_id, bank)])[
                    0
                ].counter_entry.data.packet_count
                for bank in (0, 1)
            )
            telemetry_id = index.counter_id("telemetry_bank_counter")
            telemetry = sum(
                client.read([counter_entity(telemetry_id, bank)])[
                    0
                ].counter_entry.data.packet_count
                for bank in (0, 1)
            )
            supplemental_hints = client.drain_supplemental_hints()
            queues = client.queue_depths()
            module_metrics.update(
                {
                    "active_bank": selector,
                    "expected_active_bank": active_bank,
                    "baseline_bank_0_entries": baseline_entries["bank_0"],
                    "baseline_bank_1_entries": baseline_entries["bank_1"],
                    "overlay_entries": baseline_entries["overlay"],
                    "eligible_packets": eligible,
                    "telemetry_packets": telemetry,
                    "p4runtime_request_queue": queues["request"],
                    "p4runtime_response_queue": queues["response"],
                    "p4runtime_queue_peak_observed": max(
                        supplemental_hints["request_queue_before"],
                        supplemental_hints["response_queue_before"],
                        supplemental_hints["request_queue_after"],
                        supplemental_hints["response_queue_after"],
                    ),
                    "supplemental_digest_hints": supplemental_hints["digest_hints"],
                    "supplemental_packet_in_hints": supplemental_hints[
                        "packet_in_hints"
                    ],
                    "supplemental_digest_acks": supplemental_hints["digest_acks"],
                    "selector_drift": selector != active_bank,
                }
            )
            return module_metrics

        traffic = TrafficHarness(f"{args.run_id}:soak:qualified").run(
            phases,
            sample_interval_seconds=sample_seconds,
            sample_callback=sample_callback,
            latency_sample_capacity_per_phase=latency_capacity,
            max_packets_per_run=max_packets,
            max_samples_per_run=max_samples,
        )
        callback_errors.extend(traffic.get("errors", []))
        cleanup["attempted"] = True
        for bank in (0, 1):
            if rules_by_bank[bank]:
                delete_rules(client, rules_by_bank[bank])
                rules_by_bank[bank] = []
        identity = client.set_pipeline(index.p4info, program)
        client.verify_pipeline_identity(identity)
        residual_entries = {
            "baseline_bank_0": table_entry_count(index, client, "baseline_bank_0"),
            "baseline_bank_1": table_entry_count(index, client, "baseline_bank_1"),
            "response_overlay": table_entry_count(index, client, "response_overlay"),
        }
        cleanup_hints = client.drain_supplemental_hints()
        cleanup.update(
            {
                "completed": all(value == 0 for value in residual_entries.values()),
                "residual_entries": residual_entries,
                "supplemental_hints": cleanup_hints,
                "queue_depths": client.queue_depths(),
            }
        )
        # Keep the runner alive for one resource-sampler interval after traffic
        # and capture cleanup so residual FD/thread growth is observable.
        time.sleep(sample_seconds)
    except BaseException as exc:
        callback_errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        client.close()

    warmup_errors = list(warmup.get("errors", [])) if warmup else ["warmup missing"]
    traffic_phases = traffic.get("phases", []) if traffic else []
    phase_checks = []
    for phase in traffic_phases:
        minimum_rate = float(phase["requested_rate_pps"])
        if phase["name"] == "saturation":
            minimum_rate = float(
                performance_profile["soak_workload"]["saturation_minimum_achieved_pps"]
            )
        checks = {
            "elapsed": int(phase["planned_ms"]) == round(phase_seconds * 1000),
            "sender": phase["sender_attempted"] == phase["sender_accepted"],
            "test_ingress": phase["ingress_capture_gap"] == 0,
            "outcome": phase["egress_capture_or_outcome_gap"] == 0,
            "minimum_rate": float(phase["achieved_rate_pps"]) >= minimum_rate,
        }
        phase_checks.append(
            {
                "name": phase["name"],
                "checks": checks,
                "result": "PASS" if all(checks.values()) else "FAIL",
            }
        )
    activation_checks = []
    for activation in activations:
        overlay = activation["overlay"]
        activation_checks.append(
            bool(activation["readback_exact"])
            and activation["selected_bank"] == activation["target_bank"]
            and activation["rollback_selected_bank"] == activation["old_bank"]
            and activation["reconciled_bank"] == activation["target_bank"]
            and activation["baseline_direct_counter_delta"] in (None, 1)
            and bool(overlay["drop_probe"]["outcome_matches"])
            and overlay["direct_counter_delta"] == 1
            and bool(overlay["expiry_delete_readback_empty"])
            and bool(overlay["post_expiry_forward_probe"]["outcome_matches"])
        )
    raw_pass = (
        not warmup_errors
        and not callback_errors
        and len(traffic_phases) == 4
        and all(item["result"] == "PASS" for item in phase_checks)
        and len(activations) == expected_activation_cycles
        and all(activation_checks)
        and bool(cleanup.get("completed"))
    )
    if formal:
        result = "PASS" if raw_pass else "FAIL"
        level = "MODULE"
    else:
        result = "HOLD" if raw_pass else "FAIL"
        level = "REHEARSAL"
    document = {
        "schema_version": "qualification-soak-workload/v1",
        "phase": "soak-workload",
        "run_id": args.run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "run_monotonic_elapsed_ms": round(
            (time.perf_counter_ns() - run_started_ns) / 1e6
        ),
        "level": level,
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
        "mode": args.mode,
        "requirement_ids": ["DEC-042", "PERF-P4-FW-001", "TEST-P4-FW-001"],
        "profiles": {
            "soak": sha256_bytes(soak_profile_path.read_bytes()),
            "performance": sha256_bytes(performance_profile_path.read_bytes()),
            "target": sha256_bytes(target_profile_path.read_bytes()),
        },
        "claim_scope": {
            "runtime_digest": exact_digest(args.runtime_digest),
            "runner_digest": exact_digest(args.runner_digest),
            "program_digest": sha256_bytes(program),
            "p4info_artifact_digest": sha256_bytes(p4info_bytes),
            "device_config_digest": sha256_bytes(bmv2_device_config(program)),
            "target_runtime_image": target_profile["target"]["runtime_image"],
        },
        "parameters": {
            "warmup_seconds": warmup_seconds,
            "phase_seconds": phase_seconds,
            "sample_seconds": sample_seconds,
            "activation_interval_seconds": activation_interval_seconds,
            "expected_activation_cycles": expected_activation_cycles,
            "workload_resource_bounds": workload_bounds,
            "packet_oracle_runtime": performance_profile["packet_oracle_runtime"],
        },
        "warmup": warmup,
        "traffic": traffic,
        "phase_checks": phase_checks,
        "activations": activations,
        "activation_checks": activation_checks,
        "cleanup": cleanup,
        "errors": callback_errors,
    }
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return 0 if result != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
