from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
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
        # The tracked run predates source-closure fields and remains immutable
        # historical rehearsal evidence. Reuse only its operational payload as
        # a contract fixture; do not imply a source binding it never recorded.
        cls.document.update(
            {
                "source_revision": "1" * 40,
                "source_tree_digest": "sha256:" + "2" * 64,
                "working_tree_dirty": True,
                "working_tree_status_digest": "sha256:" + "3" * 64,
                "cleanup": {
                    "attempted": True,
                    "result": "PASS",
                    "commands": [
                        {"name": name, "exit_code": 0, "output_tail": ""}
                        for name in (
                            "web-containers",
                            "postgresql-compose",
                            "p4-compose",
                        )
                    ],
                    "remaining_resources": [],
                    "processes_reaped_or_not_started": {
                        "edge": True,
                        "central": True,
                        "control": True,
                        "plugin_sideplane": True,
                        "analysis_sideplane": True,
                        "observer": True,
                    },
                },
            }
        )
        cls.schema = json.loads(
            (
                REPO
                / "contracts/evidence/connected-system-web-rehearsal/v1/schema.json"
            ).read_text(encoding="utf-8")
        )
        cls.failure_schema = json.loads(
            (
                REPO / "contracts/evidence/connected-runner-failure/v1/schema.json"
            ).read_text(encoding="utf-8")
        )
        cls.live_schema = json.loads(
            (
                REPO / "contracts/evidence/connected-system-live-runtime/v1/schema.json"
            ).read_text(encoding="utf-8")
        )

    def test_connected_contract_fixture_is_consistent(self) -> None:
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

    def test_runner_failure_evidence_is_explicitly_non_qualified(self) -> None:
        failure = {
            "schema_version": "connected-runner-failure/v1",
            "runner_id": "run-edge-central-pairwise",
            "run_id": "connected-failure-test-001",
            "started_at": "2026-08-28T00:00:00Z",
            "finished_at": "2026-08-28T00:00:01Z",
            "failure_stage": "control-postgresql",
            "error_type": "RuntimeError",
            "participants_started": {
                "central": True,
                "edge": False,
                "control_postgresql": True,
                "p4": False,
                "web": False,
                "plugin_sideplane": False,
                "analysis_sideplane": False,
            },
            "cleanup_attempted": True,
            "level": "REHEARSAL",
            "applicability": "APPLICABLE",
            "result": "FAIL",
            "qualification": "NOT_QUALIFIED",
            "reason_code": "RUNNER_EXCEPTION",
        }
        Draft202012Validator(self.failure_schema).validate(failure)
        failure["result"] = "PASS"
        self.assertTrue(
            list(Draft202012Validator(self.failure_schema).iter_errors(failure))
        )

    def test_live_runtime_manifest_can_be_atomically_replaced(self) -> None:
        runner_path = SYSTEM_ROOT / "run-edge-central-pairwise.py"
        spec = importlib.util.spec_from_file_location(
            "connected_runner_helpers", runner_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "live-runtime.json"
            module.write_json_replace(manifest, {"state": "READY"})
            module.write_json_replace(manifest, {"state": "STOPPING"})
            self.assertEqual("STOPPING", json.loads(manifest.read_text())["state"])

    def test_live_runtime_accepts_bounded_dynamic_postgresql_port(self) -> None:
        document = {
            "schema_version": "connected-system-live-runtime/v1",
            "state": "READY",
            "run_id": "connected-live-test-001",
            "started_at": "2026-08-28T00:00:00Z",
            "ready_at": "2026-08-28T00:00:01Z",
            "expires_at": "2026-08-28T00:01:01Z",
            "keep_alive_seconds": 60,
            "runner_pid": 42,
            "stop_token_path": "/tmp/connected-live-stop",
            "endpoints": {
                "web": "http://127.0.0.1:41001",
                "control_http": "http://127.0.0.1:41002",
                "control_grpc": "https://127.0.0.1:41003",
                "postgresql": "postgresql://127.0.0.1:61234/masi_control_test",
                "p4runtime": "https://127.0.0.1:41004",
                "central_gateway": "https://127.0.0.1:41005",
                "triton": "127.0.0.1:41006",
                "plugin_host": "https://127.0.0.1:41007",
                "analysis": "https://127.0.0.1:41008",
            },
            "processes": {
                "edge_test_runner": 1,
                "control": 2,
                "central_runner": 3,
                "plugin_host_exporter": 4,
                "analysis_exporter": 5,
            },
            "containers": {
                "bmv2": "a" * 64,
                "gateway": "gateway",
                "triton": "triton",
                "web": "web",
                "web_forwarder": "web-forwarder",
            },
            "fixtures": {
                "pipeline_loader": "isolated-one-shot-closed-before-live-state",
                "traffic_sender": "packet-only-no-p4runtime-credentials-closed-before-live-state",
                "analysis_provider": "deterministic-tls13-mtls-fixture",
                "analysis_mcp": "deterministic-tls13-mtls-fixture",
            },
            "level": "REHEARSAL",
            "qualification": "NOT_QUALIFIED",
        }
        validator = Draft202012Validator(self.live_schema)
        validator.validate(document)
        document["endpoints"]["postgresql"] = (
            "postgresql://127.0.0.1:65536/masi_control_test"
        )
        self.assertTrue(list(validator.iter_errors(document)))


if __name__ == "__main__":
    unittest.main()
