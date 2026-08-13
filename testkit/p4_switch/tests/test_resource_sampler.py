from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "sample-container-resources.py"
)
SPEC = importlib.util.spec_from_file_location("sample_container_resources", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load {SCRIPT}")
SAMPLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SAMPLER)


def metadata(*, running: bool) -> dict[str, object]:
    return {
        "State": {"Running": running, "OOMKilled": False},
        "HostConfig": {
            "NanoCpus": 4_000_000_000,
            "CpusetCpus": "4-7",
            "Memory": 4_294_967_296,
            "PidsLimit": 512,
        },
        "RestartCount": 0,
    }


class ResourceSamplerTests(unittest.TestCase):
    def test_normal_exit_during_collection_is_a_terminal_sample(self) -> None:
        with (
            mock.patch.object(
                SAMPLER,
                "inspect",
                side_effect=[metadata(running=True), metadata(running=False)],
            ),
            mock.patch.object(
                SAMPLER,
                "stats",
                side_effect=subprocess.CalledProcessError(1, ["docker", "stats"]),
            ),
        ):
            observed = SAMPLER.collect("runner")

        self.assertFalse(observed["running"])
        self.assertEqual("stopped_during_sample", observed["sampling_transition"])
        self.assertNotIn("error", observed)

    def test_metric_failure_while_still_running_remains_an_error(self) -> None:
        with (
            mock.patch.object(
                SAMPLER,
                "inspect",
                side_effect=[metadata(running=True), metadata(running=True)],
            ),
            mock.patch.object(
                SAMPLER,
                "stats",
                side_effect=subprocess.CalledProcessError(1, ["docker", "stats"]),
            ),
        ):
            observed = SAMPLER.collect("runner")

        self.assertFalse(observed["running"])
        self.assertIn("CalledProcessError", observed["error"])

    def test_initial_terminal_state_skips_runtime_metric_calls(self) -> None:
        with (
            mock.patch.object(SAMPLER, "inspect", return_value=metadata(running=False)),
            mock.patch.object(SAMPLER, "stats") as stats,
            mock.patch.object(SAMPLER, "process_metrics") as process_metrics,
        ):
            observed = SAMPLER.collect("runner")

        self.assertFalse(observed["running"])
        stats.assert_not_called()
        process_metrics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
