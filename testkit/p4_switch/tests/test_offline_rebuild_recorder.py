from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "record-offline-rebuild.py"
REQUIRED_BUNDLE_FILES = {
    "runtime-image.tar",
    "runner-image.tar",
    "runner-deps-image.tar",
    "compiler-image.tar",
    "tool-images.tar",
    "trivy-db.tar",
    "source-archive.tar.gz",
    "source-tree.tar",
    "source.lock.json",
    "qualified-source.patch",
    "Dockerfile.runtime",
    "Dockerfile.runner",
    "Dockerfile.runner-deps",
    "Dockerfile.compiler",
    "tools.lock.json",
    "cosign.pub",
    "SHA256SUMS",
}


class OfflineRebuildRecorderTests(unittest.TestCase):
    def run_recorder(
        self, runtime_observed: str, runtime_log_text: str
    ) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_dir = root / "bundle"
            bundle_dir.mkdir()
            for name in REQUIRED_BUNDLE_FILES:
                (bundle_dir / name).write_bytes(b"")
            runtime_log = root / "runtime.log"
            runner_log = root / "runner.log"
            output = root / "offline-rebuild.json"
            runtime_log.write_text(runtime_log_text, encoding="utf-8")
            runner_log.write_text("runner_image_id=sha256:runner\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--started-at",
                    "2026-08-13T00:00:00Z",
                    "--runtime-expected",
                    "sha256:runtime",
                    "--runtime-observed",
                    runtime_observed,
                    "--runner-expected",
                    "sha256:runner",
                    "--runner-observed",
                    "sha256:runner",
                    "--runtime-log",
                    str(runtime_log),
                    "--runner-log",
                    str(runner_log),
                    "--bundle-dir",
                    str(bundle_dir),
                    "--checksums-verified",
                    "true",
                    "--output",
                    str(output),
                ],
                check=False,
            )
            self.assertTrue(output.is_file())
            return completed.returncode, json.loads(output.read_text(encoding="utf-8"))

    def test_failure_is_written_before_nonzero_exit(self) -> None:
        returncode, document = self.run_recorder("missing", "98% tests passed\n")

        self.assertEqual(1, returncode)
        self.assertFalse(document["runtime_digest_match"])
        self.assertFalse(document["runtime_tests_51_passed"])

    def test_complete_exact_rebuild_exits_zero(self) -> None:
        returncode, document = self.run_recorder(
            "sha256:runtime", "100% tests passed, 0 tests failed out of 51\n"
        )

        self.assertEqual(0, returncode)
        self.assertTrue(document["runtime_digest_match"])
        self.assertTrue(document["runtime_tests_51_passed"])


if __name__ == "__main__":
    unittest.main()
