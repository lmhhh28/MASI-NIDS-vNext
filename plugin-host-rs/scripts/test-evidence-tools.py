#!/usr/bin/env python3
"""Exercise traceability/summary/tamper tooling against isolated synthetic evidence."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    host = Path(__file__).resolve().parent.parent
    repo = host.parent
    manifest = json.loads((host / "requirements-traceability.json").read_text(encoding="utf-8"))
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    source_digest = "sha256:" + "a" * 64
    status_digest = sha_bytes(b"")
    evidence_parent = host / "evidence"
    evidence_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tool-test-", dir=evidence_parent) as temporary:
        run = Path(temporary)
        (run / "working-tree-status.txt").write_bytes(b"")
        for binding in manifest["execution_bindings"]:
            command_id = binding["command_id"]
            log = run / f"{command_id}.log"
            log.write_text(f"synthetic command evidence for {command_id}\n", encoding="utf-8")
            write(
                run / f"{command_id}.command.json",
                {
                    "schema_version": "edge-command-execution/v1",
                    "run_id": "synthetic-evidence-tools",
                    "command_id": command_id,
                    "started_at": "2026-08-21T00:00:00Z",
                    "finished_at": "2026-08-21T00:00:01Z",
                    "duration_ms": 1000,
                    "working_directory": "plugin-host-rs",
                    "argv": ["synthetic", command_id],
                    "exit_code": 0,
                    "result": "PASS",
                    "qualification": "QUALIFIED",
                    "stable_reason": None,
                    "source_tree_digest": source_digest,
                    "working_tree_status_digest": status_digest,
                    "log": {
                        "path": log.name,
                        "sha256": sha_bytes(log.read_bytes()),
                        "bytes": log.stat().st_size,
                        "media_type": "text/plain",
                    },
                },
            )
        scenarios = []
        for runtime in ["wasm-component/v1", "grpc-service/v1"]:
            for concurrency in [1, 2, 4, 8, 32]:
                for batch_class, input_bytes in [
                    ("minimum", 128),
                    ("typical", 65536),
                    ("maximum", 2097152),
                ]:
                    scenarios.append(
                        {
                            "runtime_profile": runtime,
                            "concurrency": concurrency,
                            "batch_class": batch_class,
                            "input_bytes": input_bytes,
                            "calls": 1,
                            "trials": 3,
                            "warmup_calls": 8,
                            "admission_queue_execute_e2e_micros": {
                                "p50": 1,
                                "p95": 1,
                                "p99": 1,
                                "max": 1,
                            },
                            "throughput_per_second": 1.0,
                            "rejected": 0,
                            "timeouts": 0,
                            "retries": 0,
                            "elapsed_millis": 1,
                        }
                    )
        write(
            run / "performance/performance-evidence.json",
            {
                "schema_version": "plugin-host-performance-evidence/v1",
                "module_id": "MOD-PLUGIN-001",
                "runtime_profiles": ["wasm-component/v1", "grpc-service/v1"],
                "wasmtime_version": "47.0.3",
                "wasm_matrix": scenarios[:15],
                "service_matrix": scenarios[15:],
                "wasm_cold_warm": {
                    "component_compile_and_admission_micros": 1,
                    "cold_first_instantiate_execute_micros": 1,
                    "warm_calls": 64,
                    "warm_execute_micros": {"p50": 1, "p95": 1, "p99": 1, "max": 1},
                    "component_cache_state": "fresh-host-state-no-precompiled-entry",
                },
                "saturation": {
                    "submitted": 128, "succeeded": 1, "classified_rejections": 127,
                    "unclassified_failures": 0,
                    "stable_rejection_reasons": ["RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED"],
                    "peak_queued_observed": 32, "peak_in_flight_observed": 2,
                    "per_binding_queue_limit": 32, "global_queue_limit": 64,
                    "per_binding_in_flight_limit": 2, "final_queued": 0, "final_in_flight": 0,
                },
                "environment_profile_digest": "sha256:" + "3" * 64,
                "result_set_digest": "sha256:" + "4" * 64,
                "resource_peak_observation": {
                    "rss_bytes": 1,
                    "fd_count": 1,
                    "threads": 1,
                    "tasks": "bounded",
                    "allocations": "bounded",
                    "copies": "bounded",
                    "instances_in_flight_limit": 2,
                    "queue_depth_limit": 32,
                },
                "steady_rejected_total": 0,
                "maximum_p99_micros": 1,
                "absolute_deadline_micros": 10000000,
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "reason_code": "HOST_BOUNDARY_PERFORMANCE_WITHIN_PROFILE",
                "core_peak_relative_degradation": {
                    "applicability": "NOT_APPLICABLE",
                    "reason_code": "REQUIRES_FORMAL_CORE_PLUGIN_PAIRWISE_NOT_STARTED",
                },
            },
        )
        write(
            run / "deep/deep-checks-evidence.json",
            {
                "schema_version": "plugin-host-deep-checks-evidence/v1",
                "module_id": "MOD-PLUGIN-001",
                "cargo_audit": "PASS",
                "cargo_deny_advisories_licenses_sources": "PASS",
                "line_coverage_percent": 50.0,
                "line_coverage_minimum_percent": 50,
                "valgrind_definite_leaks_and_errors": "PASS",
                "sanitizer_profile": "synthetic tooling test",
                "miri": {
                    "applicability": "NOT_APPLICABLE",
                    "result": "NOT_RUN",
                    "qualification": "NOT_QUALIFIED",
                    "stable_reason": "EXACT_STABLE_TOOLCHAIN_HAS_NO_MIRI_COMPONENT",
                },
                "stable_rust_sanitizer": {
                    "applicability": "NOT_APPLICABLE",
                    "result": "NOT_RUN",
                    "qualification": "NOT_QUALIFIED",
                    "stable_reason": "RUST_Z_SANITIZER_REQUIRES_NON_PROFILE_NIGHTLY_TOOLCHAIN",
                },
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "reason_code": "RUST_DEEP_CHECKS_PASS",
            },
        )
        write(
            run / "fault/fault-evidence.json",
            {
                "schema_version": "plugin-host-fault-evidence/v1",
                "module_id": "MOD-PLUGIN-001",
                "runtime_profiles": ["grpc-service/v1"],
                "case_ids": [
                    "service-hang", "service-trap", "service-wrong-digest", "service-oversize",
                    "service-caller-cancellation", "service-crash-isolation", "service-disable-failure",
                    "service-disable-hang", "service-drain-hang", "trust-freshness-reconcile",
                    "service-failure-quarantine",
                ],
                "cases": [
                    {"case_id": case_id, "fault": "synthetic-fault",
                     "stable_reason": "UNAVAILABLE", "bounded_millis": 1,
                     "recovered": True, "result": "PASS"}
                    for case_id in [
                        "service-hang", "service-trap", "service-wrong-digest", "service-oversize",
                        "service-caller-cancellation", "service-crash-isolation", "service-disable-failure",
                        "service-disable-hang", "service-drain-hang", "trust-freshness-reconcile",
                        "service-failure-quarantine",
                    ]
                ],
                "host_remained_available": True,
                "unclassified_failures": 0,
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "reason_code": "PLUGIN_HOST_FAULT_MATRIX_PASS",
            },
        )
        write(
            run / "oci/oci-smoke-evidence.json",
            {
                "schema_version": "plugin-host-oci-smoke-evidence/v1",
                "module_id": "MOD-PLUGIN-001",
                "image_ref": "synthetic:test",
                "image_id": "sha256:" + "b" * 64,
                "source_revision": revision,
                "source_tree_digest": source_digest,
                "image_binary_digest": "sha256:" + "c" * 64,
                "local_release_binary_digest": "sha256:" + "d" * 64,
                "release_binaries_share_exact_source_tree": True,
                "manager_leaf_digest": "sha256:" + "e" * 64,
                "runtime_profile": "plugin-runtime-host/v1",
                "wasmtime_version": "47.0.3",
                "non_root_user": "65532:65532",
                "read_only_rootfs": True,
                "capability_drop_all": True,
                "no_new_privileges": True,
                "tls13_only": True,
                "plaintext_rejected": True,
                "unauthorized_leaf_rejected": True,
                "startup_ready_live_metrics": True,
                "docker_semantic_healthcheck": True,
                "graceful_stop_seconds": 1,
                "actual_oci_started": True,
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "reason_code": "MODULE_OCI_BOUNDARY_VERIFIED",
            },
        )
        supply_checks = {f"check_{index}": True for index in range(20)}
        write(
            run / "supply/supply-chain-evidence.json",
            {
                "schema_version": "plugin-host-supply-chain-evidence/v1",
                "module_id": "MOD-PLUGIN-001",
                "source_revision": revision,
                "source_tree_digest": source_digest,
                "image_ref": "synthetic:test",
                "image_id": "sha256:" + "b" * 64,
                "manifest_digest": "sha256:" + "f" * 64,
                "signature_bundle_digest": "sha256:" + "1" * 64,
                "public_key_digest": "sha256:" + "2" * 64,
                "scan_counts": {
                    "critical_vulnerabilities": 0,
                    "fixable_high_vulnerabilities": 0,
                    "image_secrets": 0,
                    "source_secrets": 0,
                    "high_critical_misconfigurations": 0,
                },
                "checks": supply_checks,
                "failure_reasons": [],
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "reason_code": "PLUGIN_HOST_SUPPLY_CHAIN_VERIFIED",
            },
        )
        stages = [
            {
                "name": name,
                "requested_seconds": seconds,
                "actual_millis": seconds * 1000,
                "successes": 1,
                "classified_rejections": 0,
                "unclassified_failures": 0,
                "completed": True,
            }
            for name, seconds in [
                ("warmup", 60),
                ("steady", 900),
                ("peak", 900),
                ("saturation", 900),
                ("recovery_activation", 900),
            ]
        ]
        stages[3]["classified_rejections"] = 1
        stages[4]["service_crash_classified"] = True
        stages[4]["service_restart_recovered"] = True
        samples = [
            {"elapsed_unix_ms": index + 1, "rss_bytes": 1, "threads": 1, "fds": 1,
             "queued": 1 if index == 150 else 0, "in_flight": 1 if index == 150 else 0,
             "manager_connections": 1, "manager_connections_peak": 1}
            for index in range(300)
        ]
        write(
            run / "formal-soak/soak-evidence.json",
            {
                "schema_version": "plugin-host-soak-evidence/v1",
                "module_id": "MOD-PLUGIN-001",
                "runtime_profiles": ["wasm-component/v1", "grpc-service/v1"],
                "wasmtime_version": "47.0.3",
                "warmup_seconds": 60,
                "formal_seconds": 3600,
                "stages": stages,
                "resource_samples": samples,
                "max_rss_bytes": 1,
                "max_threads": 1,
                "max_fds": 1,
                "max_queued": 1,
                "max_in_flight": 1,
                "max_manager_connections": 1,
                "resource_growth": {
                    "rss_bytes_delta": 0, "threads_delta": 0, "fds_delta": 0,
                    "queued_final": 0, "in_flight_final": 0, "sample_count": 300,
                    "leak_thresholds_passed": True,
                },
                "revocation_refresh_failures": 0,
                "unclassified_failures": 0,
                "candidate_processes_remained_live": True,
                "cleanup_succeeded": True,
                "formal_soak_executed": True,
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "reason_code": "FORMAL_PLUGIN_HOST_SOAK_COMPLETE",
            },
        )
        subprocess.run(
            [
                "python3",
                str(host / "scripts/build-traceability.py"),
                "--repo",
                str(repo),
                "--run-dir",
                str(run),
                "--manifest",
                str(host / "requirements-traceability.json"),
                "--output",
                str(run / "traceability.json"),
            ],
            check=True,
        )
        summary = run / "gate-summary.json"
        subprocess.run(
            [
                "python3",
                str(host / "scripts/build-module-summary.py"),
                "--repo",
                str(repo),
                "--run-dir",
                str(run),
                "--output",
                str(summary),
            ],
            check=True,
        )
        subprocess.run(
            [
                "python3",
                str(host / "scripts/validate-module-evidence.py"),
                "--repo",
                str(repo),
                "--run-dir",
                str(run),
                "--summary",
                str(summary),
                "--negative-self-test",
            ],
            check=True,
        )
        if not json.loads(summary.read_text(encoding="utf-8"))["overall_module_complete"]:
            raise SystemExit("synthetic evidence tool summary was not complete")
    print("plugin-host evidence tooling synthetic/negative tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
