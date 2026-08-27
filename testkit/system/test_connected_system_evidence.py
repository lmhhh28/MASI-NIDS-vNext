from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

SYSTEM_ROOT = Path(__file__).resolve().parent
REPO = SYSTEM_ROOT.parents[1]
sys.path.insert(0, str(SYSTEM_ROOT))

from validate_connected_system_evidence import (  # noqa: E402
    EvidenceSemanticError,
    validate_semantics,
)


class ConnectedSystemEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(
            (
                REPO
                / "evidence/system-connected-full/20260827T023500Z-rehearsal-005/summary.json"
            ).read_text(encoding="utf-8")
        )
        cls.schema = json.loads(
            (
                REPO
                / "contracts/evidence/connected-system-web-rehearsal/v1/schema.json"
            ).read_text(encoding="utf-8")
        )

    def test_frozen_connected_evidence_is_consistent(self) -> None:
        Draft202012Validator(self.schema).validate(self.document)
        validate_semantics(self.document)

    def test_duplicate_browser_set_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.document)
        candidate["web_observations"][2]["browser"] = "chromium"
        with self.assertRaises(EvidenceSemanticError):
            validate_semantics(candidate)

    def test_cross_boundary_artifact_drift_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.document)
        candidate["web_observations"][0]["analysis_artifact_id"] = "artifact-drift"
        with self.assertRaises(EvidenceSemanticError):
            validate_semantics(candidate)

    def test_inference_arithmetic_drift_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.document)
        candidate["triton_statistics"]["inference_count_delta"] = 2
        with self.assertRaises(EvidenceSemanticError):
            validate_semantics(candidate)

    def test_unknown_evidence_property_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.document)
        candidate["event_evidence"]["unreviewed"] = True
        errors = list(Draft202012Validator(self.schema).iter_errors(candidate))
        self.assertTrue(errors)

    def test_secret_path_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.document)
        candidate["qualification_scope"] = "/tmp/private.key"
        with self.assertRaises(EvidenceSemanticError):
            validate_semantics(candidate)


if __name__ == "__main__":
    unittest.main()
