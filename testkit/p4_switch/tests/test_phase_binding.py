from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "testkit/p4_switch/scripts/bind-phase-evidence.py"


class PhaseBindingTests(unittest.TestCase):
    def test_phase_is_bound_once_to_exact_run_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            phase = root / "unit.json"
            source = root / "source.json"
            phase.write_text(
                json.dumps(
                    {
                        "phase": "unit",
                        "level": "MODULE",
                        "applicability": "APPLICABLE",
                        "result": "PASS",
                        "qualification": "QUALIFIED",
                        "tests": [],
                    }
                ),
                encoding="utf-8",
            )
            identity = {
                "source_revision": "a" * 40,
                "source_tree_digest": "sha256:" + ("b" * 64),
                "working_tree_dirty": True,
                "working_tree_status_digest": "sha256:" + ("c" * 64),
            }
            source.write_text(json.dumps(identity), encoding="utf-8")
            command = [
                "python3",
                str(SCRIPT),
                "--phase",
                str(phase),
                "--phase-name",
                "unit",
                "--run-id",
                "run-one",
                "--source-identity",
                str(source),
            ]
            subprocess.run(command, check=True, timeout=10)
            bound = json.loads(phase.read_text(encoding="utf-8"))
            self.assertEqual("run-one", bound["run_id"])
            for key, value in identity.items():
                self.assertEqual(value, bound[key])
            repeated = subprocess.run(
                command,
                check=False,
                timeout=10,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(0, repeated.returncode)


if __name__ == "__main__":
    unittest.main()
