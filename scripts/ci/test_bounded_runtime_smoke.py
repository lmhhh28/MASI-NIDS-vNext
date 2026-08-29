from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/run_bounded_runtime_smoke.py"
SCHEMA = json.loads(
    (ROOT / "contracts/evidence/runtime-smoke-failure/v1/schema.json").read_text(encoding="utf-8")
)


class BoundedRuntimeSmokeTests(unittest.TestCase):
    def test_failure_is_persisted_and_schema_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            command = [
                "python3",
                str(SCRIPT),
                "--repo",
                str(ROOT),
                "--module",
                "web",
                "--timeout-seconds",
                "10",
                "--failure-root",
                temp_dir,
                "--",
                "bash",
                "-c",
                "printf 'bounded failure\\n'; exit 7",
            ]
            completed = subprocess.run(command, check=False, timeout=20)
            self.assertEqual(7, completed.returncode)
            documents = list(Path(temp_dir).glob("*/runtime-smoke-failure.json"))
            self.assertEqual(1, len(documents))
            document = json.loads(documents[0].read_text(encoding="utf-8"))
            Draft202012Validator(SCHEMA, format_checker=FormatChecker()).validate(document)
            self.assertEqual("FAIL", document["result"])
            self.assertFalse(document["timed_out"])
            mismatched = dict(document, module_id="MOD-EDGE-001")
            self.assertTrue(list(Draft202012Validator(SCHEMA).iter_errors(mismatched)))
            self.assertEqual(
                {"runtime-smoke.log", "runtime-smoke-failure.json"},
                {path.name for path in documents[0].parent.iterdir()},
            )
            self.assertEqual([], list(Path(temp_dir).glob(".*")))

    def test_timeout_terminates_process_group_and_records_124(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--repo",
                    str(ROOT),
                    "--module",
                    "analysis",
                    "--timeout-seconds",
                    "1",
                    "--failure-root",
                    temp_dir,
                    "--",
                    "bash",
                    "-c",
                    "sleep 30",
                ],
                check=False,
                timeout=40,
            )
            self.assertEqual(124, completed.returncode)
            document_path = next(Path(temp_dir).glob("*/runtime-smoke-failure.json"))
            document = json.loads(document_path.read_text(encoding="utf-8"))
            self.assertTrue(document["timed_out"])
            self.assertEqual("RUNTIME_SMOKE_TIMED_OUT", document["stable_reason"])

    def test_escaped_descendant_holding_stdout_still_persists_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "descendant.pid"
            environment = dict(os.environ, MASI_TEST_DESCENDANT_PID=str(pid_path))
            completed = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--repo",
                    str(ROOT),
                    "--module",
                    "edge",
                    "--timeout-seconds",
                    "10",
                    "--failure-root",
                    temp_dir,
                    "--",
                    "bash",
                    "-c",
                    "setsid python3 -c 'import os,time,pathlib; pathlib.Path(os.environ[\"MASI_TEST_DESCENDANT_PID\"]).write_text(str(os.getpid())); time.sleep(30)' &",
                ],
                check=False,
                env=environment,
                timeout=20,
            )
            self.assertEqual(1, completed.returncode)
            document_path = next(Path(temp_dir).glob("*/runtime-smoke-failure.json"))
            document = json.loads(document_path.read_text(encoding="utf-8"))
            self.assertEqual("FAIL", document["result"])
            self.assertTrue(document["log"]["truncated"])
            log = document_path.with_name("runtime-smoke.log").read_text(encoding="utf-8")
            self.assertIn("descendant retained stdout", log)
            descendant_pid = int(pid_path.read_text(encoding="utf-8"))
            for _ in range(40):
                if not Path(f"/proc/{descendant_pid}").exists():
                    break
                time.sleep(0.05)
            self.assertFalse(Path(f"/proc/{descendant_pid}").exists())

    def test_continuous_escaped_writer_cannot_bypass_pipe_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [
                    "python3", str(SCRIPT), "--repo", str(ROOT), "--module", "edge",
                    "--timeout-seconds", "10", "--failure-root", temp_dir, "--",
                    "bash", "-c",
                    "setsid python3 -c 'import os; block=b\"x\"*65536; exec(\"while True:\\n os.write(1, block)\")' &",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                timeout=15,
            )
            self.assertEqual(1, completed.returncode)
            document = json.loads(
                next(Path(temp_dir).glob("*/runtime-smoke-failure.json")).read_text(encoding="utf-8")
            )
            self.assertTrue(document["log"]["truncated"])

    def test_closed_parent_stdout_still_persists_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            process = subprocess.Popen(
                [
                    "python3",
                    str(SCRIPT),
                    "--repo",
                    str(ROOT),
                    "--module",
                    "web",
                    "--timeout-seconds",
                    "10",
                    "--failure-root",
                    temp_dir,
                    "--",
                    "bash",
                    "-c",
                    "printf 'closed pipe failure\\n'; exit 7",
                ],
                stdout=subprocess.PIPE,
            )
            assert process.stdout is not None
            process.stdout.close()
            self.assertEqual(7, process.wait(timeout=20))
            document = next(Path(temp_dir).glob("*/runtime-smoke-failure.json"))
            Draft202012Validator(SCHEMA, format_checker=FormatChecker()).validate(
                json.loads(document.read_text(encoding="utf-8"))
            )

    def test_unconsumed_parent_stdout_cannot_bypass_deadline_or_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            process = subprocess.Popen(
                [
                    "python3",
                    str(SCRIPT),
                    "--repo",
                    str(ROOT),
                    "--module",
                    "analysis",
                    "--timeout-seconds",
                    "10",
                    "--failure-root",
                    temp_dir,
                    "--",
                    "python3",
                    "-c",
                    "import os; os.write(1, b'x' * (2 * 1024 * 1024)); raise SystemExit(7)",
                ],
                stdout=subprocess.PIPE,
            )
            self.assertEqual(7, process.wait(timeout=20))
            assert process.stdout is not None
            process.stdout.close()
            document_path = next(Path(temp_dir).glob("*/runtime-smoke-failure.json"))
            document = json.loads(document_path.read_text(encoding="utf-8"))
            self.assertEqual(2 * 1024 * 1024, document["log"]["bytes"])
            self.assertEqual(2 * 1024 * 1024, os.path.getsize(document_path.with_name("runtime-smoke.log")))

    def test_source_identity_failure_prevents_command_and_partial_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as failure_root:
            fake_repo = Path(temp_dir)
            (fake_repo / ".git").mkdir()
            marker = fake_repo / "command-ran"
            completed = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--repo",
                    str(fake_repo),
                    "--module",
                    "web",
                    "--timeout-seconds",
                    "10",
                    "--failure-root",
                    failure_root,
                    "--",
                    "touch",
                    str(marker),
                ],
                check=False,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(marker.exists())
            self.assertEqual([], list(Path(failure_root).iterdir()))
            self.assertIn(b"command was not started", completed.stderr)

    def test_invalid_run_id_is_rejected_before_command_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "command-ran"
            completed = subprocess.run(
                [
                    "python3", str(SCRIPT), "--repo", str(ROOT), "--module", "web",
                    "--timeout-seconds", "10", "--failure-root", temp_dir, "--",
                    "touch", str(marker),
                ],
                check=False,
                env=dict(os.environ, MASI_RUNTIME_SMOKE_RUN_ID="bad id"),
                stderr=subprocess.PIPE,
                timeout=20,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(marker.exists())
            self.assertIn(b"command was not started", completed.stderr)

    def test_launch_failure_is_atomically_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [
                    "python3", str(SCRIPT), "--repo", str(ROOT), "--module", "web",
                    "--timeout-seconds", "10", "--failure-root", temp_dir, "--",
                    "/definitely/absent/masi-runtime-command",
                ],
                check=False,
                timeout=20,
            )
            self.assertEqual(127, completed.returncode)
            document_path = next(Path(temp_dir).glob("*/runtime-smoke-failure.json"))
            document = json.loads(document_path.read_text(encoding="utf-8"))
            Draft202012Validator(SCHEMA, format_checker=FormatChecker()).validate(document)
            self.assertEqual("RUNTIME_SMOKE_LAUNCH_FAILED", document["stable_reason"])


if __name__ == "__main__":
    unittest.main()
