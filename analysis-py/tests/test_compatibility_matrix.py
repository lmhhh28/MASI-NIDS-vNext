from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class CompatibilityMatrixTests(unittest.TestCase):
    def test_complete_matrix_validator(self) -> None:
        module_root = Path(__file__).resolve().parents[1]
        repository = module_root.parent
        completed = subprocess.run(
            [
                sys.executable,
                str(module_root / "scripts/validate-compatibility.py"),
                "--repo",
                str(repository),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('"row_count":17', completed.stdout)


if __name__ == "__main__":
    unittest.main()
