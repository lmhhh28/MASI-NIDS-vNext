from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from testkit.p4_switch.lib.source_identity import (
    read_stable_regular_file,
    source_tree_digest,
    validate_source_identity,
)


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
    def prepare_required_source_roots(root: Path) -> None:
        for relative in (
            "p4",
            "testkit/p4_switch",
            "deploy/p4-switch",
            "deploy/supply-chain",
            "contracts",
            "docs",
        ):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "docs/masi-nids-vnext-system-requirements-2026-08-09.md").write_text(
            "requirements\n", encoding="utf-8"
        )

    @staticmethod
    def source_identity() -> dict[str, object]:
        return {
            "source_revision": "a" * 40,
            "source_tree_digest": "sha256:" + ("b" * 64),
            "working_tree_dirty": True,
            "working_tree_status_digest": "sha256:" + ("c" * 64),
        }

    def test_supplied_source_identity_recomputes_tree_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.prepare_required_source_roots(root)
            source = root / "p4/src/masi_switch.p4"
            source.parent.mkdir(parents=True)
            source.write_text("control ingress {}\n", encoding="utf-8")
            document = {
                "source_revision": "a" * 40,
                "source_tree_digest": source_tree_digest(root),
                "working_tree_dirty": True,
                "working_tree_status_digest": "sha256:" + ("b" * 64),
            }
            self.assertEqual(document, validate_source_identity(root, document))
            source.write_text("control ingress { apply {} }\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed during the run"):
                validate_source_identity(root, document)

    def test_source_closure_rejects_a_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            outside = Path(outside_dir)
            (outside / "masi_switch.p4").write_text("control ingress {}\n", encoding="utf-8")
            (root / "p4").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "closure root is a symlink"):
                source_tree_digest(root)

    def test_source_closure_rejects_a_missing_required_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "closure directory is absent"):
                source_tree_digest(Path(temp_dir))

    def test_cli_rejects_a_symlink_repository_root_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            link = Path(temp_dir) / "repo-link"
            link.symlink_to(ROOT, target_is_directory=True)
            completed = subprocess.run(
                ["python3", str(ROOT / "testkit/p4_switch/lib/source_identity.py"), str(link)],
                check=False,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn(b"unsafe P4 source repository", completed.stderr)

    def test_stable_reader_rejects_a_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real = root / "real"
            real.mkdir()
            (real / "source.txt").write_text("source\n", encoding="utf-8")
            (root / "linked").symlink_to(real, target_is_directory=True)
            with self.assertRaises(OSError):
                read_stable_regular_file(root / "linked/source.txt")

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
            (node_modules / "generated-tool").symlink_to(root / "schema.json")
            cache = root / "adapter/__pycache__"
            cache.mkdir(parents=True)
            (cache / "adapter.cpython-313.pyc").write_bytes(b"generated")

            self.assertEqual(expected, module.tree_digest(root))

    def test_compiled_artifact_closure_is_exact_and_stable(self) -> None:
        module = load_aggregate_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in module.EXPECTED_COMPILED_ARTIFACTS:
                (root / name).write_bytes(name.encode())
            payloads = module.stable_exact_regular_files(
                root, module.EXPECTED_COMPILED_ARTIFACTS
            )
            self.assertEqual(set(module.EXPECTED_COMPILED_ARTIFACTS), set(payloads))

            (root / "unbound.bin").write_bytes(b"unbound")
            with self.assertRaisesRegex(ValueError, "differs from the exact profile"):
                module.stable_exact_regular_files(root, module.EXPECTED_COMPILED_ARTIFACTS)
            (root / "unbound.bin").unlink()

            (root / "nested").mkdir()
            with self.assertRaisesRegex(ValueError, "non-regular entry"):
                module.stable_exact_regular_files(root, module.EXPECTED_COMPILED_ARTIFACTS)
            (root / "nested").rmdir()

            (root / "linked").symlink_to(root / "masi_switch.json")
            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                module.stable_exact_regular_files(root, module.EXPECTED_COMPILED_ARTIFACTS)

    def test_checksum_snapshot_detects_post_manifest_file_addition(self) -> None:
        module = load_aggregate_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "phase.json").write_text("{}\n", encoding="utf-8")
            snapshot, lines = module.evidence_checksum_snapshot(root)
            self.assertEqual(1, len(lines))
            (root / "SHA256SUMS").write_text("placeholder\n", encoding="utf-8")
            module.verify_evidence_tree_unchanged(root, snapshot)
            (root / "late.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed after SHA256SUMS"):
                module.verify_evidence_tree_unchanged(root, snapshot)

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
            "registry_digest": "sha256:" + ("1" * 64),
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

    def test_phase_result_qualification_mismatch_becomes_explicit_failure(self) -> None:
        module = load_aggregate_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "unit.json"
            path.write_text(
                json.dumps(
                    {
                        "phase": "unit",
                        "level": "MODULE",
                        "applicability": "APPLICABLE",
                        "result": "PASS",
                        "qualification": "NOT_QUALIFIED",
                        "tests": [
                            module.test_record(
                                "TEST-P4-STATIC-CONTRACT-001", ["TEST-003"], "PASS", {}
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
            phase = module.load_phase(path, "unit")
            self.assertEqual("FAIL", phase["result"])
            self.assertEqual("NOT_QUALIFIED", phase["qualification"])

    def test_duplicate_phase_json_member_becomes_explicit_failure(self) -> None:
        module = load_aggregate_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "unit.json"
            path.write_text('{"phase":"unit","phase":"compiler"}', encoding="utf-8")
            phase = module.load_phase(path, "unit")
            self.assertEqual("FAIL", phase["result"])

    def test_phase_symlink_is_rejected_without_following_target(self) -> None:
        module = load_aggregate_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "outside.json"
            target.write_text(
                json.dumps(
                    {
                        "phase": "unit",
                        "level": "MODULE",
                        "applicability": "APPLICABLE",
                        "result": "PASS",
                        "qualification": "QUALIFIED",
                        "tests": [
                            module.test_record(
                                "TEST-P4-STATIC-CONTRACT-001",
                                ["TEST-003"],
                                "PASS",
                                {"forged": True},
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
            link = root / "unit.json"
            link.symlink_to(target)
            phase = module.load_phase(link, "unit")
            self.assertEqual("FAIL", phase["result"])

    def test_phase_run_binding_mismatch_becomes_explicit_failure(self) -> None:
        module = load_aggregate_module()
        source = self.source_identity()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "unit.json"
            path.write_text(
                json.dumps(
                    {
                        "phase": "unit",
                        "run_id": "old-run",
                        **source,
                        "level": "MODULE",
                        "applicability": "APPLICABLE",
                        "result": "PASS",
                        "qualification": "QUALIFIED",
                        "tests": [
                            module.test_record(
                                "TEST-P4-STATIC-CONTRACT-001",
                                ["TEST-003"],
                                "PASS",
                                {"observation": True},
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
            phase, _ = module.load_phase_record(path, "unit", "current-run", source)
            self.assertEqual("FAIL", phase["result"])
            self.assertIn("run identity mismatch", phase["tests"][0]["evidence"]["error"])

    def test_phase_referenced_file_digest_mismatch_becomes_explicit_failure(self) -> None:
        module = load_aggregate_module()
        source = self.source_identity()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "unit.log").write_text("actual\n", encoding="utf-8")
            path = root / "unit.json"
            path.write_text(
                json.dumps(
                    {
                        "phase": "unit",
                        "run_id": "current-run",
                        **source,
                        "level": "MODULE",
                        "applicability": "APPLICABLE",
                        "result": "PASS",
                        "qualification": "QUALIFIED",
                        "tests": [
                            module.test_record(
                                "TEST-P4-STATIC-CONTRACT-001",
                                ["TEST-003"],
                                "PASS",
                                {"log": "unit.log", "log_sha256": "0" * 64},
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
            phase, _ = module.load_phase_record(path, "unit", "current-run", source)
            self.assertEqual("FAIL", phase["result"])
            self.assertIn("digest mismatch", phase["tests"][0]["evidence"]["error"])


if __name__ == "__main__":
    unittest.main()
