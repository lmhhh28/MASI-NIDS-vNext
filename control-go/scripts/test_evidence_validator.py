from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from typing import Any


class EvidenceValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        library: dict[str, Any] = runpy.run_path(
            str(Path(__file__).with_name("validate-evidence.py")),
            run_name="control_evidence_validator_library",
        )
        cls.load_object = staticmethod(library["load_object"])
        cls.valid_digest = staticmethod(library["valid_digest"])

    def test_duplicate_json_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="masi-control-evidence-negative."
        ) as temporary:
            document = Path(temporary) / "duplicate.json"
            document.write_text('{"result":"PASS","result":"FAIL"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON member"):
                self.load_object(document)

    def test_all_zero_digest_is_rejected(self) -> None:
        self.assertFalse(self.valid_digest("sha256:" + "0" * 64))
        self.assertTrue(self.valid_digest("sha256:" + "1" * 64))


if __name__ == "__main__":
    unittest.main()
