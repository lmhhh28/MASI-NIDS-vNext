#!/usr/bin/env python3
"""Unit tests for the root formal module evidence verifier."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_module_gate_output as verifier


class ModuleGateVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="masi-verifier-test-")
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "user.email",
                "test@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Verifier Test"],
            check=True,
        )
        (self.repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("/analysis-py/evidence/\n/schema.json\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "fixture"], check=True
        )
        self.head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        self.run_id = "analysis-test-001"
        self.summary = (
            self.repo
            / f"analysis-py/evidence/module-gates/runs/{self.run_id}/gate-summary.json"
        )
        self.pointer = self.repo / "analysis-py/evidence/module-gates/latest.json"
        self.schema = self.repo / "schema.json"
        self.summary.parent.mkdir(parents=True)
        self.original_source_profile = verifier.MODULE_SOURCE_PROFILES["analysis"]
        verifier.MODULE_SOURCE_PROFILES["analysis"] = {
            "kind": "files", "roots": ("tracked.txt",), "status": "nul-all"
        }
        identity = verifier.current_module_source_identity(self.repo, "analysis")
        self.document = {
            "schema_version": "analysis-plugin-module-gates/v1",
            "run_id": self.run_id,
            "result": "HOLD",
            "qualification": "NOT_QUALIFIED",
            **identity,
            "overall_module_complete": True,
            "completion": {"operational_gates_pass": True},
            "qualification_gates": {
                "module_operational": "PASS",
                "protected_baseline": "HOLD",
            },
            "remaining_holds": ["protected baseline is not formed"],
        }
        self.schema.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(self.document),
                    "properties": {
                        key: (
                            {"type": "boolean"}
                            if isinstance(value, bool)
                            else {"type": "object"}
                            if isinstance(value, dict)
                            else {"type": "array"}
                            if isinstance(value, list)
                            else {"type": "string"}
                        )
                        for key, value in self.document.items()
                    },
                }
            ),
            encoding="utf-8",
        )
        self.original_config = dict(verifier.MODULES["analysis"])
        verifier.MODULES["analysis"] = {
            **self.original_config,
            "schema": "schema.json",
        }
        self.publish()

    def tearDown(self) -> None:
        verifier.MODULES["analysis"] = self.original_config
        verifier.MODULE_SOURCE_PROFILES["analysis"] = self.original_source_profile
        self.temporary.cleanup()

    def publish(self) -> None:
        self.summary.write_text(json.dumps(self.document) + "\n", encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(self.summary.read_bytes()).hexdigest()
        self.pointer.parent.mkdir(parents=True, exist_ok=True)
        self.pointer.write_text(
            json.dumps(
                {
                    "schema_version": "analysis-plugin-module-latest/v1",
                    "run_id": self.run_id,
                    "evidence": f"runs/{self.run_id}/gate-summary.json",
                    "digest": digest,
                    "overall_module_complete": True,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_accepts_digest_bound_qualification_only_hold(self) -> None:
        output = verifier.verify(self.repo, "analysis", self.run_id, False)
        self.assertIn("result=HOLD", output)

    def test_rejects_stale_tree_status_and_dirty_bindings(self) -> None:
        original = dict(self.document)
        for key, value in (
            ("source_tree_digest", "sha256:" + "b" * 64),
            ("working_tree_status_digest", "sha256:" + "c" * 64),
            ("working_tree_dirty", True),
        ):
            self.document = dict(original)
            self.document[key] = value
            self.publish()
            with self.assertRaisesRegex(ValueError, key):
                verifier.verify(self.repo, "analysis", self.run_id, False)
        self.document = original

    def test_accepts_explained_all_hold_and_not_run_qualification_gates(self) -> None:
        self.document["qualification_gates"] = {
            "protected_baseline": {"result": "HOLD", "reason": "baseline absent"},
            "system_integration": {"result": "NOT_RUN", "command": "formal-system"},
        }
        self.document["remaining_holds"] = []
        self.publish()
        self.assertIn("result=HOLD", verifier.verify(self.repo, "analysis", self.run_id, False))

    def test_rejects_failed_gate_hidden_inside_qualification_hold(self) -> None:
        self.document["qualification_gates"] = {
            "protected_baseline": {"result": "HOLD", "reason": "baseline absent"},
            "operational": {"result": "FAIL", "reason": "actual failure"},
        }
        self.publish()
        with self.assertRaisesRegex(ValueError, "failed gate"):
            verifier.verify(self.repo, "analysis", self.run_id, False)

    def test_p4_pass_cannot_hide_a_failed_test(self) -> None:
        document = {
            "result": "PASS",
            "qualification": "QUALIFIED",
            "tests": [{"applicability": "APPLICABLE", "result": "FAIL", "qualification": "NOT_QUALIFIED"}],
            "performance": [{"result": "PASS", "qualification": "QUALIFIED"}],
            "cleanup": {
                "result": "PASS",
                "remaining_resources": {"containers": [], "networks": [], "volumes": []},
            },
        }
        with self.assertRaisesRegex(ValueError, "failed test"):
            verifier.verify_p4_completion(document)

    def test_db_runner_status_mode_preserves_default_untracked_collapsing(self) -> None:
        nested = self.repo / "untracked/nested.txt"
        nested.parent.mkdir()
        nested.write_text("untracked\n", encoding="utf-8")
        _, default_digest = verifier.current_status_identity(self.repo, "nul-default")
        _, all_digest = verifier.current_status_identity(self.repo, "nul-all")
        self.assertNotEqual(default_digest, all_digest)
        self.assertEqual("nul-default", verifier.MODULE_SOURCE_PROFILES["db"]["status"])

    def test_p4_preflight_failure_identity_is_schema_bound(self) -> None:
        source_repo = Path(verifier.__file__).resolve().parents[2]
        failure_schema = json.loads(
            (
                source_repo / "contracts/evidence/module-runner-failure/v1/schema.json"
            ).read_text(encoding="utf-8")
        )
        command_schema = json.loads(
            (source_repo / "contracts/evidence/command/v1/schema.json").read_text(
                encoding="utf-8"
            )
        )
        command = {
            "schema_version": "edge-command-execution/v1",
            "run_id": "p4-preflight-test-001",
            "command_id": "preflight",
            "started_at": "2026-08-28T00:00:00Z",
            "finished_at": "2026-08-28T00:00:00Z",
            "duration_ms": 0,
            "working_directory": "p4",
            "argv": ["module-gates-preflight", "p4"],
            "exit_code": 78,
            "result": "FAIL",
            "qualification": "NOT_QUALIFIED",
            "stable_reason": "COMMAND_EXITED_FAILURE",
            "source_tree_digest": "sha256:" + "a" * 64,
            "working_tree_status_digest": "sha256:" + "b" * 64,
            "log": {
                "path": "preflight.log",
                "sha256": "sha256:" + "c" * 64,
                "bytes": 1,
                "media_type": "text/plain",
            },
        }
        Draft202012Validator(command_schema).validate(command)
        failure = {
            "schema_version": "module-runner-failure/v1",
            "module": "p4",
            "module_id": "MOD-SW-001",
            "run_id": command["run_id"],
            "generated_at": command["finished_at"],
            "failed_command_id": "preflight",
            "exit_code": 78,
            "result": "FAIL",
            "qualification": "NOT_QUALIFIED",
            "stable_reason": "COMMAND_EXITED_FAILURE",
            "source_revision": "d" * 40,
            "source_tree_digest": command["source_tree_digest"],
            "working_tree_status_digest": command["working_tree_status_digest"],
            "command_evidence": {
                "path": "preflight.json",
                "digest": "sha256:" + "e" * 64,
                "bytes": 1,
            },
            "log_evidence": {
                "path": "preflight.log",
                "digest": "sha256:" + "c" * 64,
                "bytes": 1,
            },
            "overall_module_complete": False,
        }
        Draft202012Validator(failure_schema).validate(failure)
        failure["module_id"] = "MOD-INF-001"
        self.assertTrue(list(Draft202012Validator(failure_schema).iter_errors(failure)))

    def test_rejects_pointer_digest_tamper(self) -> None:
        pointer = json.loads(self.pointer.read_text(encoding="utf-8"))
        pointer["digest"] = "sha256:" + "0" * 64
        self.pointer.write_text(json.dumps(pointer), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "latest pointer"):
            verifier.verify(self.repo, "analysis", self.run_id, False)

    def test_rejects_unexplained_hold_and_stale_source(self) -> None:
        self.document["qualification_gates"] = {"protected_baseline": "HOLD"}
        self.document["remaining_holds"] = []
        self.publish()
        with self.assertRaisesRegex(ValueError, "qualification-only"):
            verifier.verify(self.repo, "analysis", self.run_id, False)
        self.document["remaining_holds"] = ["hold"]
        self.document["qualification_gates"] = {
            "module_operational": "PASS",
            "protected_baseline": "HOLD",
        }
        self.document["source_revision"] = "0" * 40
        self.publish()
        with self.assertRaisesRegex(ValueError, "source_revision"):
            verifier.verify(self.repo, "analysis", self.run_id, False)

    def test_rejects_summary_that_does_not_match_public_schema(self) -> None:
        del self.document["completion"]
        self.publish()
        with self.assertRaisesRegex(ValueError, "schema rejected"):
            verifier.verify(self.repo, "analysis", self.run_id, False)

    def test_incomplete_failure_requires_explicit_mode_and_no_pointer(self) -> None:
        self.document["overall_module_complete"] = False
        self.document["result"] = "FAIL"
        self.document["completion"] = {"operational_gates_pass": False}
        self.publish()
        self.pointer.unlink()
        with self.assertRaisesRegex(ValueError, "overall_module_complete"):
            verifier.verify(self.repo, "analysis", self.run_id, False)
        output = verifier.verify(self.repo, "analysis", self.run_id, False, True)
        self.assertIn("result=FAIL", output)

    def test_accepts_schema_and_digest_bound_runner_failure(self) -> None:
        self.summary.unlink()
        self.pointer.unlink()
        source_repo = Path(verifier.__file__).resolve().parents[2]
        for relative in (
            "contracts/evidence/command/v1/schema.json",
            "contracts/evidence/module-runner-failure/v1/schema.json",
        ):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source_repo / relative).read_bytes())
        identity = verifier.current_module_source_identity(self.repo, "analysis")
        log = self.summary.parent / "formal-soak.log"
        log.write_text("formal soak held\n", encoding="utf-8")
        log_digest = "sha256:" + hashlib.sha256(log.read_bytes()).hexdigest()
        sidecar = self.summary.parent / "formal-soak.command.json"
        sidecar.write_text(
            json.dumps(
                {
                    "schema_version": "edge-command-execution/v1",
                    "run_id": self.run_id,
                    "command_id": "formal-soak",
                    "started_at": "2026-08-28T00:00:00Z",
                    "finished_at": "2026-08-28T00:00:01Z",
                    "duration_ms": 1000,
                    "working_directory": "analysis-py",
                    "argv": ["formal-soak"],
                    "exit_code": 2,
                    "result": "HOLD",
                    "qualification": "NOT_QUALIFIED",
                    "source_tree_digest": identity["source_tree_digest"],
                    "working_tree_status_digest": identity["working_tree_status_digest"],
                    "stable_reason": "COMMAND_EXITED_HOLD",
                    "log": {
                        "path": log.name,
                        "sha256": log_digest,
                        "bytes": log.stat().st_size,
                        "media_type": "text/plain",
                    },
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                "python3",
                str(source_repo / "scripts/ci/write_module_failure.py"),
                "--repo",
                str(self.repo),
                "--module",
                "analysis",
                "--run-dir",
                str(self.summary.parent),
                "--command-sidecar",
                sidecar.name,
                "--output",
                str(self.summary.parent / "gate-failure.json"),
            ],
            check=True,
        )
        output = verifier.verify(self.repo, "analysis", self.run_id, False, True)
        self.assertIn("result=HOLD", output)
        failure_path = self.summary.parent / "gate-failure.json"
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        failure["module_id"] = "MOD-INF-001"
        failure_path.write_text(json.dumps(failure), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "failure schema rejected"):
            verifier.verify(self.repo, "analysis", self.run_id, False, True)


if __name__ == "__main__":
    unittest.main()
