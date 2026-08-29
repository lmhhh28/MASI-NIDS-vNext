from __future__ import annotations

import unittest

from masi_analysis.canonical import compute_input_digest
from masi_analysis.errors import UnsafeOutput
from masi_analysis.models import FactRef, FrozenInput, ModelExplanationFact
from masi_analysis.security import redact_text, require_safe_output

from .support import DIGEST_A, DIGEST_B, frozen_input


class ContractModelTests(unittest.TestCase):
    def test_input_digest_matches_after_roundtrip(self) -> None:
        value = frozen_input(content_request="中文 bounded <evidence>")
        decoded = FrozenInput.model_validate_json(value.model_dump_json())
        self.assertEqual(value.input_digest, compute_input_digest(decoded))

    def test_model_evidence_refs_must_be_frozen(self) -> None:
        value = frozen_input()
        with self.assertRaises(ValueError):
            FrozenInput.model_validate({**value.model_dump(mode="json"), "model_result_evidence_refs": ["outside"]})

    def test_digest_and_revision_zero_sentinels_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FactRef(id="fact-1", digest="sha256:" + ("0" * 64))
        value = frozen_input()
        with self.assertRaises(ValueError):
            FrozenInput.model_validate({**value.model_dump(mode="json"), "plugin_revision": "0" * 40})

    def test_secret_redaction_and_executable_rejection(self) -> None:
        self.assertNotIn("secret-value", redact_text("Bearer secret-value", ("secret-value",)))
        with self.assertRaises(UnsafeOutput):
            require_safe_output("execute P4Runtime now")

    def test_explanation_method_inputs_and_limitations_are_exact(self) -> None:
        base = {
            "claim": "Qualified attribution is an association.",
            "evidence_refs": ["explanation-1"],
            "model_digest": DIGEST_A,
            "sample_digest": DIGEST_B,
            "coverage": 1.0,
            "truncated": False,
            "limitations": ["Association is not causality."],
        }
        self.assertEqual(
            ModelExplanationFact.model_validate(
                {
                    **base,
                    "method": "tree-shap",
                    "background_digest": DIGEST_A,
                    "scaler_digest": None,
                }
            ).method,
            "tree-shap",
        )
        for invalid in (
            {**base, "method": "tree-shap", "background_digest": None, "scaler_digest": None},
            {
                **base,
                "method": "logistic-contribution",
                "background_digest": None,
                "scaler_digest": None,
            },
            {
                **base,
                "method": "reconstruction-residual",
                "background_digest": None,
                "scaler_digest": None,
            },
            {
                **base,
                "method": "tree-shap",
                "background_digest": DIGEST_A,
                "scaler_digest": None,
                "coverage": 0.0,
            },
            {
                **base,
                "method": "tree-shap",
                "background_digest": DIGEST_A,
                "scaler_digest": None,
                "limitations": [],
            },
        ):
            with self.subTest(method=invalid["method"], coverage=invalid.get("coverage")):
                with self.assertRaises(ValueError):
                    ModelExplanationFact.model_validate(invalid)


if __name__ == "__main__":
    unittest.main()
