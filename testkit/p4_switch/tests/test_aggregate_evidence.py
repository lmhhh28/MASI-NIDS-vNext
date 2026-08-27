from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "testkit/p4_switch/scripts/aggregate-evidence.py"


def load_aggregate_module():
    spec = importlib.util.spec_from_file_location("p4_aggregate_evidence", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load aggregate-evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AggregateEvidenceTests(unittest.TestCase):
    @staticmethod
    def passing_operational_tests(module):
        tests = []
        for test_id in sorted(module.MANDATORY_OPERATIONAL_TEST_IDS):
            evidence = {}
            if test_id == "TEST-P4-SOAK-3600S-001":
                evidence = {
                    "qualified_elapsed_ms": 3_600_000,
                    "cleanup": {"completed": True, "remaining_resources": []},
                    "summary": {
                        "container_restarts": 0,
                        "error_count": 0,
                        "oom_events": 0,
                        "oracle_mismatches": 0,
                        "resource_limit_violations": 0,
                        "unclassified_gap_count": 0,
                        "module_metrics": {"formal_schedule_executed": True},
                    },
                }
            tests.append(module.test_record(test_id, ["TEST-003"], "PASS", evidence))
        return tests

    def test_tree_digest_excludes_generated_dependency_and_bytecode_caches(
        self,
    ) -> None:
        module = load_aggregate_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "schema.json").write_text('{"schema":"v1"}\n', encoding="utf-8")
            expected = module.tree_digest(root)

            node_modules = root / "client/node_modules"
            node_modules.mkdir(parents=True)
            (node_modules / ".package-lock.json").write_text(
                '{"generated":true}\n', encoding="utf-8"
            )
            cache = root / "adapter/__pycache__"
            cache.mkdir(parents=True)
            (cache / "adapter.cpython-313.pyc").write_bytes(b"generated")

            self.assertEqual(expected, module.tree_digest(root))

    def test_p4_findings_registry_is_valid_and_has_no_open_p0(self) -> None:
        module = load_aggregate_module()
        findings = module.module_findings_summary(ROOT)
        self.assertEqual(0, findings["open_total"])
        self.assertEqual(0, findings["open_p0"])
        self.assertRegex(findings["registry_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_operational_completion_requires_every_gate_and_zero_open_p0(self) -> None:
        module = load_aggregate_module()
        tests = self.passing_operational_tests(module)
        findings = {
            "open_total": 0,
            "open_p0": 0,
            "registry_digest": "sha256:" + ("0" * 64),
        }

        completion, overall = module.derive_operational_completion(tests, findings)
        self.assertTrue(overall)
        self.assertTrue(all(completion.values()))

        open_p0 = dict(findings, open_total=1, open_p0=1)
        completion, overall = module.derive_operational_completion(tests, open_p0)
        self.assertFalse(overall)
        self.assertFalse(completion["open_p0_zero"])

        missing_gate = tests[:-1]
        completion, overall = module.derive_operational_completion(
            missing_gate, findings
        )
        self.assertFalse(overall)
        self.assertFalse(completion["mandatory_tests_executed"])

        short_soak = self.passing_operational_tests(module)
        soak = next(
            item for item in short_soak if item["id"] == "TEST-P4-SOAK-3600S-001"
        )
        soak["evidence"]["qualified_elapsed_ms"] = 3_599_999
        completion, overall = module.derive_operational_completion(short_soak, findings)
        self.assertFalse(overall)
        self.assertFalse(completion["formal_soak_executed"])


if __name__ == "__main__":
    unittest.main()
