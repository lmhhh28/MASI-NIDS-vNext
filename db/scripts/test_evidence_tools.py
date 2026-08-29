#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


def load_summary_module(script_dir: Path) -> ModuleType:
    path = script_dir / "build-module-summary.py"
    spec = importlib.util.spec_from_file_location("db_module_summary", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load module summary builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvidenceToolsTest(unittest.TestCase):
    def test_current_migration_chain_is_exact(self) -> None:
        script_dir = Path(__file__).resolve().parent
        summary = load_summary_module(script_dir)
        items, chain = summary.source_migration_chain(script_dir.parent / "migrations")
        self.assertEqual(31, len(items))
        self.assertEqual(
            "sha256:e6b697e7dba9a0e080b32c5f67af9250b4973587727b2d536a2675ea67278b48",
            chain,
        )
        self.assertEqual("0031_reject_zero_digest_defaults.sql", items[-1]["name"])
        oci_schema = json.loads(
            (
                script_dir.parent.parent
                / "contracts/evidence/postgresql-state-oci/v1/schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(31, oci_schema["properties"]["migration_count"]["const"])

    def test_observed_capacity_remains_not_qualified(self) -> None:
        script_dir = Path(__file__).resolve().parent
        repo = script_dir.parent.parent
        measurement = {
            "revision_json_rules": 0,
            "epoch_rows": 0,
            "observation_rows": 0,
            "rollup_5m_rows": 0,
            "rollup_1h_rows": 0,
            "build_microseconds": 1,
            "current_query_microseconds": 1,
            "page_query_microseconds": 1,
            "trend_query_microseconds": 1,
            "rolled_back": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            output = root / "capacity.json"
            raw.write_text(
                json.dumps(
                    {
                        "mode": "capacity",
                        "result": "PASS",
                        "identity": {
                            "database": "masi_state_module_test",
                            "user": "masi_app_login",
                            "server_version_num": 180006,
                            "tls": True,
                        },
                        "capacity": [
                            {"rule_count": count, **measurement}
                            for count in (0, 128, 1024, 4096)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "python3",
                    str(script_dir / "build-capacity-evidence.py"),
                    "--raw",
                    str(raw),
                    "--run-id",
                    "db-capacity-test-001",
                    "--output",
                    str(output),
                ],
                check=True,
            )
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("PASS", document["result"])
            self.assertEqual("NOT_QUALIFIED", document["qualification"])
            subprocess.run(
                [
                    "python3",
                    str(script_dir / "validate-json.py"),
                    "--schema",
                    str(
                        repo
                        / "contracts/evidence/postgresql-state-capacity/v1/schema.json"
                    ),
                    "--document",
                    str(output),
                ],
                check=True,
            )

    def test_command_sidecar_rejects_changed_log(self) -> None:
        script_dir = Path(__file__).resolve().parent
        repo = script_dir.parent.parent
        summary = load_summary_module(script_dir)
        source_digest = "sha256:" + "1" * 64
        status_digest = "sha256:" + "2" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "unit.log"
            sidecar = root / "unit.command.json"
            log.write_text("verified\n", encoding="utf-8")
            sidecar.write_text(
                json.dumps(
                    {
                        "schema_version": "edge-command-execution/v1",
                        "run_id": "db-test-command-001",
                        "command_id": "unit",
                        "started_at": "2026-08-20T00:00:00Z",
                        "finished_at": "2026-08-20T00:00:01Z",
                        "duration_ms": 1000,
                        "working_directory": "db",
                        "argv": ["go", "test", "./..."],
                        "exit_code": 0,
                        "result": "PASS",
                        "qualification": "QUALIFIED",
                        "source_tree_digest": source_digest,
                        "working_tree_status_digest": status_digest,
                        "stable_reason": None,
                        "log": {
                            "path": "unit.log",
                            "sha256": summary.digest(log),
                            "bytes": log.stat().st_size,
                            "media_type": "text/plain",
                        },
                    }
                ),
                encoding="utf-8",
            )
            schema = repo / "contracts/evidence/command/v1/schema.json"
            summary.verify_command_sidecar(
                "unit",
                sidecar,
                root,
                schema,
                "db-test-command-001",
                source_digest,
                status_digest,
            )
            log.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "log digest mismatch"):
                summary.verify_command_sidecar(
                    "unit",
                    sidecar,
                    root,
                    schema,
                    "db-test-command-001",
                    source_digest,
                    status_digest,
                )

    def test_supply_raw_closure_rejects_changed_scan(self) -> None:
        script_dir = Path(__file__).resolve().parent
        summary = load_summary_module(script_dir)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = {
                "db_sbom": "db.sbom.cdx.json",
                "pgbouncer_sbom": "pgbouncer.sbom.cdx.json",
                "postgres_sbom": "postgres.sbom.cdx.json",
                "db_scan": "db.trivy.json",
                "pgbouncer_scan": "pgbouncer.trivy.json",
                "postgres_scan": "postgres.trivy.json",
                "source_scan": "source.trivy-secret.json",
                "trivy_db_metadata": "trivy-db-metadata.json",
            }
            for key, name in names.items():
                value = (
                    {"components": [{"name": "component"}]}
                    if key.endswith("sbom")
                    else {"Results": []}
                )
                if key == "trivy_db_metadata":
                    value = {"UpdatedAt": "2026-08-20T00:00:00Z"}
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            supply = {
                "digests": {
                    key: summary.digest(root / name) for key, name in names.items()
                },
                "policy": {
                    "critical_vulnerabilities": 0,
                    "fixable_high_vulnerabilities": 0,
                    "image_secrets": 0,
                    "source_secrets": 0,
                },
                "observed_at": "2026-08-20T00:00:00Z",
                "trivy_database_age_seconds": 0,
            }
            self.assertEqual(supply["digests"], summary.verify_supply_raw(supply, root))
            (root / "db.trivy.json").write_text(
                json.dumps(
                    {"Results": [{"Vulnerabilities": [{"Severity": "CRITICAL"}]}]}
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "digest closure mismatch"):
                summary.verify_supply_raw(supply, root)

    def test_formal_soak_conversion_matches_shared_schema(self) -> None:
        script_dir = Path(__file__).resolve().parent
        repo = script_dir.parent.parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            resources = root / "resources.json"
            output = root / "soak.json"
            phases = [
                ("warmup", "2026-08-20T00:01:00.000000001Z", 100),
                ("steady", "2026-08-20T00:16:00.000000002Z", 18_000),
                ("peak", "2026-08-20T00:31:00Z", 72_000),
                ("saturation", "2026-08-20T00:46:00Z", 144_000),
                ("recovery-or-activation", "2026-08-20T01:01:00Z", 36_000),
            ]
            raw.write_text(
                json.dumps(
                    {
                        "schema_version": "postgresql-state-workload/v1",
                        "mode": "soak",
                        "result": "PASS",
                        "phases": [
                            {"name": "steady", "target_qps": 20, "concurrency": 4},
                            {"name": "peak", "target_qps": 80, "concurrency": 12},
                            {
                                "name": "saturation",
                                "target_qps": 160,
                                "concurrency": 32,
                            },
                            {
                                "name": "recovery-or-activation",
                                "target_qps": 40,
                                "concurrency": 8,
                            },
                        ],
                        "soak": {
                            "started_at": "2026-08-20T00:00:00.123456789Z",
                            "finished_at": "2026-08-20T01:01:00.123456789Z",
                            "monotonic_seconds": 3660,
                            "warmup_seconds": 60,
                            "qualified_seconds": 3600,
                            "formal": True,
                            "phase_duration_seconds": 900,
                            "samples": [
                                {
                                    "timestamp": timestamp,
                                    "phase": name,
                                    "operations": operations,
                                    "errors": 0,
                                    "average_microseconds": 3000,
                                    "max_microseconds": 9000,
                                    "p99_upper_bound_ms": 10,
                                    "pool_total": 4,
                                    "pool_acquired": 0,
                                    "pool_idle": 4,
                                }
                                for name, timestamp, operations in phases
                            ],
                            "operations": 270_100,
                            "errors": 0,
                            "cleanup_rows": 1024,
                            "first_errors": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            resources.write_text(
                json.dumps(
                    {
                        "schema_version": "postgresql-state-resource-samples/v1",
                        "started_at": "2026-08-20T00:00:00.123456789Z",
                        "finished_at": "2026-08-20T01:01:00.123456789Z",
                        "interval_seconds": 10,
                        "containers": ["postgres", "pgbouncer"],
                        "failures": [],
                        "samples": [
                            {
                                "timestamp": timestamp,
                                "offset_ms": index * 900_000,
                                "phase": name,
                                "quality": "valid",
                                "cpu_pct": 10.0,
                                "rss_bytes": 134_217_728,
                                "fd_count": 32,
                                "thread_count": 16,
                                "queue_depth": 0,
                                "oom_events": 0,
                                "container_restarts": 0,
                                "oracle_errors": 0,
                                "gap_count": 0,
                            }
                            for index, (name, timestamp, _) in enumerate(phases)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            profile = repo / "contracts/profiles/v1/qualification-soak-3600s.json"
            subprocess.run(
                [
                    "python3",
                    str(script_dir / "build-soak-evidence.py"),
                    "--raw",
                    str(raw),
                    "--resources",
                    str(resources),
                    "--profile",
                    str(profile),
                    "--run-id",
                    "db-test-soak-001",
                    "--output",
                    str(output),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "python3",
                    str(script_dir / "validate-json.py"),
                    "--schema",
                    str(repo / "contracts/evidence/soak/v1/schema.json"),
                    "--document",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
