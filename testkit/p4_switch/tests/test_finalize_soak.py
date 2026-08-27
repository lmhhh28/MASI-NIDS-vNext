from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "testkit/p4_switch/scripts/finalize-soak.py"


class FinalizeSoakTests(unittest.TestCase):
    def test_rehearsal_emits_complete_shared_soak_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            workload_path = directory / "workload.json"
            resources_path = directory / "resources.jsonl"
            evidence_path = directory / "soak-evidence.json"
            phase_path = directory / "soak.json"
            phase_names = [
                "steady",
                "peak",
                "saturation",
                "recovery-or-activation",
            ]
            workload = {
                "run_id": "p4-soak-test-0001",
                "mode": "rehearsal",
                "result": "HOLD",
                "started_at": "2026-08-23T00:00:00Z",
                "finished_at": "2026-08-23T00:00:06Z",
                "parameters": {"warmup_seconds": 1, "sample_seconds": 1},
                "claim_scope": {
                    "runtime_digest": "sha256:" + "1" * 64,
                    "runner_digest": "sha256:" + "2" * 64,
                },
                "warmup": {
                    "monotonic_start_ns": 100_000_000,
                    "monotonic_end_ns": 1_100_000_000,
                    "supplemental_hints": {
                        "request_queue_before": 0,
                        "response_queue_before": 0,
                        "request_queue_after": 0,
                        "response_queue_after": 0,
                    },
                },
                "traffic": {
                    "monotonic_start_ns": 1_000_000_000,
                    "monotonic_scheduled_end_ns": 5_000_000_000,
                    "phases": [
                        {
                            "name": name,
                            "planned_ms": 1_000,
                            "requested_rate_pps": 1,
                            "achieved_rate_pps": 1,
                            "sender_accepted": 1,
                            "test_ingress_packets": 1,
                            "dut_egress_packets": 1,
                            "ingress_capture_gap": 0,
                            "egress_capture_or_outcome_gap": 0,
                            "latency_ms": {"p99": 1},
                        }
                        for name in phase_names
                    ],
                    "samples": [
                        {
                            "offset_ms": 1_000,
                            "phase": "steady",
                            "module_metrics": {
                                "p4runtime_request_queue": 0,
                                "p4runtime_response_queue": 0,
                                "selector_drift": False,
                            },
                        }
                    ],
                },
                "phase_checks": [
                    {"name": name, "result": "PASS"} for name in phase_names
                ],
                "activation_checks": [True],
                "cleanup": {
                    "attempted": True,
                    "completed": True,
                    "queue_depths": {"request": 0, "response": 0},
                },
                "errors": [],
            }
            resource_sample = {
                "monotonic_ns": 2_000_000_000,
                "switch": {
                    "running": True,
                    "cpu_pct": 1,
                    "rss_bytes": 1024,
                    "fd_count": 4,
                    "thread_count": 2,
                    "oom_events": 0,
                    "restart_count": 0,
                },
                "runner": {
                    "running": True,
                    "cpu_pct": 1,
                    "rss_bytes": 1024,
                    "fd_count": 4,
                    "thread_count": 2,
                    "oom_events": 0,
                    "restart_count": 0,
                },
            }
            workload_path.write_text(json.dumps(workload), encoding="utf-8")
            resources_path.write_text(
                json.dumps(resource_sample) + "\n", encoding="utf-8"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--repo",
                    str(ROOT),
                    "--workload",
                    str(workload_path),
                    "--resources",
                    str(resources_path),
                    "--evidence-output",
                    str(evidence_path),
                    "--phase-output",
                    str(phase_path),
                    "--runner-container-removed",
                    "true",
                    "--module-process-reaped",
                    "true",
                    "--provider-tasks-joined",
                    "true",
                    "--listeners-released",
                    "true",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(2, completed.returncode, completed.stderr)
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual("p4-switch", evidence["claim_scope"]["module"])
            self.assertEqual([], evidence["lease_renewals"])
            self.assertEqual(
                {
                    "module_process_reaped": True,
                    "provider_tasks_joined": True,
                    "listeners_released": True,
                },
                evidence["cleanup"]["checks"],
            )


if __name__ == "__main__":
    unittest.main()
