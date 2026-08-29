from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]


class QuickCISchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (ROOT / "contracts/evidence/quick-ci/v1/schema.json").read_text(
                encoding="utf-8"
            )
        )
        cls.validator = Draft202012Validator(cls.schema)
        cls.document = {
            "schema_version": "quick-ci-rehearsal-summary/v1",
            "run_id": "gh-123456-1-quick-ci",
            "source_revision": "a" * 40,
            "jobs": {
                "repository-hygiene": "success",
                "p4-contracts": "success",
                "edge-rust": "success",
                "plugin-host-rust": "success",
                "control-go": "success",
                "postgresql-state": "success",
                "analysis-python": "success",
                "offline-ml-python": "success",
                "web": "success",
                "inference-contracts": "success",
            },
            "level": "REHEARSAL",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "qualification_scope": "HOSTED_UNPRIVILEGED_FAST_FEEDBACK_ONLY; NO_MODULE_PAIRWISE_SYSTEM_OR_RELEASE_QUALIFICATION",
        }

    def test_exact_success_set_is_valid(self) -> None:
        self.validator.validate(self.document)

    def test_unknown_or_missing_job_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.document)
        del candidate["jobs"]["web"]
        candidate["jobs"]["invented-job"] = "success"
        self.assertTrue(list(self.validator.iter_errors(candidate)))

    def test_pass_rejects_non_success_job(self) -> None:
        candidate = copy.deepcopy(self.document)
        candidate["jobs"]["web"] = "failure"
        self.assertTrue(list(self.validator.iter_errors(candidate)))


if __name__ == "__main__":
    unittest.main()
