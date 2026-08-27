from __future__ import annotations

import unittest

from masi_analysis.canonical import compute_input_digest
from masi_analysis.errors import UnsafeOutput
from masi_analysis.models import FrozenInput
from masi_analysis.security import redact_text, require_safe_output

from .support import frozen_input


class ContractModelTests(unittest.TestCase):
    def test_input_digest_matches_after_roundtrip(self) -> None:
        value = frozen_input(content_request="中文 bounded <evidence>")
        decoded = FrozenInput.model_validate_json(value.model_dump_json())
        self.assertEqual(value.input_digest, compute_input_digest(decoded))

    def test_model_evidence_refs_must_be_frozen(self) -> None:
        value = frozen_input()
        with self.assertRaises(ValueError):
            FrozenInput.model_validate({**value.model_dump(mode="json"), "model_result_evidence_refs": ["outside"]})

    def test_secret_redaction_and_executable_rejection(self) -> None:
        self.assertNotIn("secret-value", redact_text("Bearer secret-value", ("secret-value",)))
        with self.assertRaises(UnsafeOutput):
            require_safe_output("execute P4Runtime now")


if __name__ == "__main__":
    unittest.main()
