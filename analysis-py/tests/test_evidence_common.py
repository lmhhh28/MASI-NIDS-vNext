from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from evidence_common import load_json, safe_run_file  # pyright: ignore[reportMissingImports]  # noqa: E402


def load_summary_builder():
    path = SCRIPTS / "build-module-summary.py"
    spec = importlib.util.spec_from_file_location("analysis_summary_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Analysis summary builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvidenceCommonTests(unittest.TestCase):
    def test_run_reference_cannot_escape_or_traverse_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            run.mkdir()
            (run / "valid.json").write_text('{"result":"PASS"}', encoding="utf-8")
            self.assertEqual(run / "valid.json", safe_run_file(run, "valid.json"))
            with self.assertRaises(ValueError):
                safe_run_file(run, "../outside.json")
            with self.assertRaises(ValueError):
                safe_run_file(run, str((root / "outside.json").absolute()))
            (root / "outside.json").write_text("{}", encoding="utf-8")
            (run / "linked.json").symlink_to(root / "outside.json")
            with self.assertRaises(ValueError):
                safe_run_file(run, "linked.json")

    def test_duplicate_json_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "duplicate.json"
            document.write_text('{"result":"PASS","result":"FAIL"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON member"):
                load_json(document)

    def test_evidence_reference_binds_run_source_and_producer(self) -> None:
        builder = load_summary_builder()
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "analysis-reference-run"
            run.mkdir()
            source_digest = "sha256:" + "a" * 64
            status_digest = "sha256:" + "b" * 64
            revision = "c" * 40
            evidence = run / "evidence.json"
            evidence.write_text(
                json.dumps(
                    {
                        "run_id": run.name,
                        "source_revision": revision,
                        "source_tree_digest": source_digest,
                        "working_tree_status_digest": status_digest,
                        "result": "PASS",
                        "qualification": "NOT_QUALIFIED",
                    }
                ),
                encoding="utf-8",
            )
            command = {
                "run_id": run.name,
                "source_tree_digest": source_digest,
                "working_tree_status_digest": status_digest,
            }
            (run / "producer.command.json").write_text(json.dumps(command), encoding="utf-8")
            reference = builder.evidence_ref(
                run,
                evidence.name,
                "producer",
                {"producer": command},
                revision,
                source_digest,
                status_digest,
            )
            self.assertEqual(run.name, reference["run_id"])
            self.assertEqual("producer", reference["producer_command_id"])
            document = json.loads(evidence.read_text(encoding="utf-8"))
            document["run_id"] = "another-run"
            evidence.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "run_id mismatch"):
                builder.evidence_ref(
                    run,
                    evidence.name,
                    "producer",
                    {"producer": command},
                    revision,
                    source_digest,
                    status_digest,
                )


if __name__ == "__main__":
    unittest.main()
