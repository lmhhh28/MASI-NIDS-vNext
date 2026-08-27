from __future__ import annotations

import runpy
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx

SOAK_SUPPORT = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/run-soak.py"), run_name="analysis_soak_support")
account_state = cast(Callable[..., None], SOAK_SUPPORT["account_state"])
wait_http_ready = cast(Callable[..., None], SOAK_SUPPORT["wait_http_ready"])


class AccountStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.counters = {"completed": 0, "typed_rejected": 0, "failed": 0, "http_error": 0, "unclassified": 0}
        self.state_counts: dict[str, int] = {}
        self.unclassified_samples: list[str] = []

    def account(self, state: str, phase: str, *, crash_expected: bool = False) -> None:
        account_state(
            self.counters,
            self.state_counts,
            self.unclassified_samples,
            state=state,
            phase=phase,
            crash_expected=crash_expected,
        )

    def test_expected_crash_transport_error_is_not_unclassified(self) -> None:
        self.account("http_error", "recovery-crash", crash_expected=True)
        self.assertEqual(self.counters["http_error"], 1)
        self.assertEqual(self.counters["unclassified"], 0)
        self.assertEqual(self.state_counts, {"recovery-crash:http_error": 1})
        self.assertEqual(self.unclassified_samples, [])

    def test_expected_crash_submit_completion_is_not_unclassified(self) -> None:
        self.account("submitted", "recovery-crash", crash_expected=True)
        self.assertEqual(self.counters["http_error"], 0)
        self.assertEqual(self.counters["unclassified"], 0)
        self.assertEqual(self.state_counts, {"recovery-crash:submitted": 1})

    def test_non_crash_http_error_remains_a_gate_failure(self) -> None:
        self.account("http_error", "saturation")
        self.assertEqual(self.counters["http_error"], 1)
        self.assertEqual(self.counters["unclassified"], 1)
        self.assertEqual(self.unclassified_samples, ["saturation:http_error"])

    def test_terminal_states_are_classified_by_phase(self) -> None:
        self.account("TASK_STATE_COMPLETED", "steady")
        self.account("TASK_STATE_REJECTED", "saturation")
        self.account("TASK_STATE_FAILED", "recovery")
        self.assertEqual(self.counters, {"completed": 1, "typed_rejected": 1, "failed": 1, "http_error": 0, "unclassified": 0})
        self.assertEqual(
            self.state_counts,
            {
                "steady:TASK_STATE_COMPLETED": 1,
                "saturation:TASK_STATE_REJECTED": 1,
                "recovery:TASK_STATE_FAILED": 1,
            },
        )


class HttpReadinessTests(unittest.TestCase):
    def test_waits_for_exact_https_readiness(self) -> None:
        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            if requests == 1:
                return httpx.Response(503, json={"ready": False})
            return httpx.Response(200, json={"ready": True})

        with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://analysis.test") as client:
            wait_http_ready(client, timeout=0.2, poll_interval=0.001)
        self.assertEqual(requests, 2)

    def test_reports_last_readiness_status_on_timeout(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"ready": False})

        with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://analysis.test") as client:
            with self.assertRaisesRegex(RuntimeError, "status=503"):
                wait_http_ready(client, timeout=0.005, poll_interval=0.001)


if __name__ == "__main__":
    unittest.main()
