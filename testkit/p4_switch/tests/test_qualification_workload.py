from __future__ import annotations

import multiprocessing
import unittest

from testkit.p4_switch.lib.qualification_workload import (
    MAX_LATENCY_SAMPLES_PER_PHASE,
    TrafficHarness,
    WorkloadPhase,
    _LatencyReservoir,
    _empty_sender_state,
    _publish_sender_progress,
    _sender_progress_snapshot,
    latency_summary,
    validate_packet_oracle_profile,
)


class LatencyReservoirTests(unittest.TestCase):
    def test_reservoir_is_bounded_deterministic_and_keeps_exact_max(self) -> None:
        first = _LatencyReservoir(64, seed=7)
        second = _LatencyReservoir(64, seed=7)
        for value in range(1, 10_001):
            first.observe(value)
            second.observe(value)

        self.assertEqual(64, len(first.samples))
        self.assertEqual(first.samples, second.samples)
        self.assertEqual(10_000, first.population)
        self.assertEqual(10_000, first.maximum_ns)
        self.assertGreater(first.replacements, 0)
        self.assertEqual(
            0.01,
            latency_summary(first.samples, exact_max_ns=first.maximum_ns)["max"],
        )
        self.assertEqual(9_936, first.evidence()["not_retained"])

    def test_capacity_and_latency_bounds_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            _LatencyReservoir(0, seed=1)
        with self.assertRaises(ValueError):
            _LatencyReservoir(MAX_LATENCY_SAMPLES_PER_PHASE + 1, seed=1)
        reservoir = _LatencyReservoir(1, seed=1)
        with self.assertRaises(ValueError):
            reservoir.observe(-1)

    def test_packet_and_sample_workload_bounds_fail_before_socket_setup(self) -> None:
        harness = TrafficHarness("bounded-workload-unit")
        with self.assertRaisesRegex(ValueError, "packet slots"):
            harness.run(
                [WorkloadPhase("packet-overflow", 1, 100, 1)],
                max_packets_per_run=64,
            )
        with self.assertRaisesRegex(ValueError, "requires 401 samples"):
            harness.run(
                [WorkloadPhase("sample-overflow", 401, 1, 1)],
                sample_interval_seconds=1,
                max_samples_per_run=400,
            )

    def test_sender_progress_is_bounded_and_preserves_phase_counts(self) -> None:
        phases = (
            WorkloadPhase("first", 1, 10, 2),
            WorkloadPhase("second", 1, 20, 3),
        )
        state = _empty_sender_state(phases)
        state["attempted"] = 31
        state["accepted"] = 30
        state["accepted_bytes"] = 30 * 256
        state["by_phase"] = {2: 10, 3: 20}
        context = multiprocessing.get_context("spawn")
        progress = context.Array("Q", 5, lock=True)

        _publish_sender_progress(progress, state, (2, 3))

        self.assertEqual(
            {
                "attempted": 31,
                "accepted": 30,
                "accepted_bytes": 30 * 256,
                "by_phase": {2: 10, 3: 20},
            },
            _sender_progress_snapshot(progress, (2, 3)),
        )

    def test_packet_oracle_runtime_profile_fails_closed_on_drift(self) -> None:
        profile: dict[str, object] = {
            "sender_execution": "multiprocessing-spawn/v1",
            "sender_progress_publish_packets": 64,
            "sender_send_buffer_requested_bytes": 4 * 1024 * 1024,
            "capture_execution": "af-packet-thread/v1",
            "capture_receive_buffer_requested_bytes": 16 * 1024 * 1024,
            "capture_packet_statistics_required": True,
            "capture_drain_timeout_ms": 2000,
            "capture_drain_stable_intervals": 5,
        }
        validate_packet_oracle_profile(profile)
        profile["sender_execution"] = "thread"
        with self.assertRaisesRegex(RuntimeError, "diverges"):
            validate_packet_oracle_profile(profile)


if __name__ == "__main__":
    unittest.main()
