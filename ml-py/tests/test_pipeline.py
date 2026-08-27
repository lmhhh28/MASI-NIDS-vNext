from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from masi_offline_ml.canonical import ValidationError, write_fresh_json
from masi_offline_ml.pipeline import run_pipeline, verify_pipeline_output

REPO = Path(__file__).resolve().parents[2]


class PipelineTests(unittest.TestCase):
    def test_two_real_runs_have_identical_immutable_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / ".first.staging-dead"
            stale.mkdir()
            write_fresh_json(
                stale / ".offline-ml-staging-owner.json",
                {
                    "schema_version": "offline-ml-staging-owner/v1",
                    "pid": 2147483647,
                    "process_start_ticks": "1",
                    "output": str(root / "first"),
                },
            )
            first = run_pipeline(REPO, root / "first")
            second = run_pipeline(REPO, root / "second")
            first_verification = verify_pipeline_output(REPO, root / "first")
            second_verification = verify_pipeline_output(REPO, root / "second")

            self.assertEqual(first["candidate_executions"], 9)
            self.assertEqual(first["runtime"]["recovered_staging_directories"], 1)
            self.assertEqual(first["dataset_revision"], second["dataset_revision"])
            self.assertEqual(first["bundle"]["model_digest"], second["bundle"]["model_digest"])
            self.assertEqual(
                first["bundle"]["repository_closure_digest"], second["bundle"]["repository_closure_digest"]
            )
            self.assertEqual(first["archive"]["sha256"], second["archive"]["sha256"])
            self.assertEqual(first_verification["result"], "PASS")
            self.assertEqual(second_verification["result"], "PASS")

    def test_tampered_candidate_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            run_pipeline(REPO, output)
            metrics = next((output / "candidates").glob("*/seed-17/metrics.json"))
            metrics.write_bytes(metrics.read_bytes() + b" ")
            with self.assertRaises(ValidationError) as context:
                verify_pipeline_output(REPO, output)
            self.assertEqual(context.exception.code, "candidate_file_digest")

    def test_failed_training_removes_private_staging_and_does_not_publish(self) -> None:
        def fail_training(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected-training-crash")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "run"
            with patch("masi_offline_ml.pipeline.TRAINERS", (fail_training,)):
                with self.assertRaisesRegex(RuntimeError, "injected-training-crash"):
                    run_pipeline(REPO, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".run.staging-*")), [])

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            output.mkdir()
            marker = output / "owned-by-user"
            marker.write_text("preserve", encoding="utf-8")
            with self.assertRaises(ValidationError) as context:
                run_pipeline(REPO, output)
            self.assertEqual(context.exception.code, "output_directory_exists")
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
