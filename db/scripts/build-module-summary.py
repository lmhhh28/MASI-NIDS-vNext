#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 67_108_864:
        raise ValueError(f"unsafe summary input: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not an object: {path}")
    return value


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 67_108_864:
        raise ValueError(f"unsafe digest input: {path}")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate_document(document: Path, schema_path: Path) -> None:
    schema = read(schema_path)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            read(document)
        ),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "$"
        raise ValueError(
            f"schema validation failed {document} at {location}: {first.message}"
        )


def verify_supply_raw(supply: dict[str, Any], supply_directory: Path) -> dict[str, str]:
    files = {
        "db_sbom": supply_directory / "db.sbom.cdx.json",
        "pgbouncer_sbom": supply_directory / "pgbouncer.sbom.cdx.json",
        "postgres_sbom": supply_directory / "postgres.sbom.cdx.json",
        "db_scan": supply_directory / "db.trivy.json",
        "pgbouncer_scan": supply_directory / "pgbouncer.trivy.json",
        "postgres_scan": supply_directory / "postgres.trivy.json",
        "source_scan": supply_directory / "source.trivy-secret.json",
        "trivy_db_metadata": supply_directory / "trivy-db-metadata.json",
    }
    observed = {name: digest(path) for name, path in files.items()}
    if observed != supply["digests"]:
        raise ValueError("supply raw artifact digest closure mismatch")
    for name in ("db_sbom", "pgbouncer_sbom", "postgres_sbom"):
        components = read(files[name]).get("components")
        if not isinstance(components, list) or not components:
            raise ValueError(f"empty or invalid SBOM: {name}")

    critical = fixable_high = image_secrets = 0
    for name in ("db_scan", "pgbouncer_scan", "postgres_scan"):
        results = read(files[name]).get("Results") or []
        if not isinstance(results, list):
            raise ValueError(f"invalid Trivy results: {name}")
        for result in results:
            vulnerabilities = result.get("Vulnerabilities") or []
            secrets = result.get("Secrets") or []
            if not isinstance(vulnerabilities, list) or not isinstance(secrets, list):
                raise ValueError(f"invalid Trivy finding arrays: {name}")
            image_secrets += len(secrets)
            for vulnerability in vulnerabilities:
                severity = vulnerability.get("Severity")
                fixed = vulnerability.get("FixedVersion") or ""
                critical += int(severity == "CRITICAL")
                fixable_high += int(severity == "HIGH" and fixed != "")
    source_results = read(files["source_scan"]).get("Results") or []
    if not isinstance(source_results, list):
        raise ValueError("invalid source secret scan")
    source_secrets = sum(len(result.get("Secrets") or []) for result in source_results)
    policy = {
        "critical_vulnerabilities": critical,
        "fixable_high_vulnerabilities": fixable_high,
        "image_secrets": image_secrets,
        "source_secrets": source_secrets,
    }
    if policy != supply["policy"] or any(policy.values()):
        raise ValueError(f"supply policy mismatch or findings remain: {policy}")

    metadata = read(files["trivy_db_metadata"])
    timestamp = re.sub(r"(\.\d{6})\d+", r"\1", metadata["UpdatedAt"])
    updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    observed_at = datetime.fromisoformat(supply["observed_at"].replace("Z", "+00:00"))
    age = (observed_at - updated).total_seconds()
    if age < 0 or age > 86400 or abs(age - supply["trivy_database_age_seconds"]) > 1:
        raise ValueError("Trivy database age evidence mismatch")
    return observed


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def parse_time(value: str) -> datetime:
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", value)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, f"timestamp lacks timezone: {value}")
    return parsed


def source_migration_chain(directory: Path) -> tuple[list[dict[str, Any]], str]:
    require(
        not directory.is_symlink() and directory.is_dir(), "unsafe migration directory"
    )
    paths = sorted(directory.glob("*.sql"), key=lambda path: path.name)
    require(len(paths) == 31, f"expected 31 migrations, got {len(paths)}")
    items: list[dict[str, Any]] = []
    chain = hashlib.sha256()
    for version, path in enumerate(paths, start=1):
        require(
            re.fullmatch(rf"{version:04d}_[A-Za-z0-9_-]+\.sql", path.name) is not None,
            f"invalid migration sequence: {path.name}",
        )
        require(
            not path.is_symlink()
            and path.is_file()
            and 0 < path.stat().st_size <= 4_194_304,
            f"unsafe migration file: {path}",
        )
        checksum = digest(path)
        chain.update(f"{path.name}\t{checksum}\n".encode())
        items.append({"version": version, "name": path.name, "checksum": checksum})
    return items, "sha256:" + chain.hexdigest()


def verify_migration_manifest(
    document: dict[str, Any], expected_items: list[dict[str, Any]], expected_digest: str
) -> None:
    require(
        set(document)
        == {"schema_version", "chain_digest", "migration_count", "migrations"},
        "migration-chain document shape mismatch",
    )
    require(
        document["schema_version"] == "migration-chain-manifest/v1",
        "migration-chain schema mismatch",
    )
    require(document["migration_count"] == 31, "migration-chain count mismatch")
    require(
        document["chain_digest"] == expected_digest, "migration-chain digest mismatch"
    )
    require(
        document["migrations"] == expected_items,
        "migration-chain source closure mismatch",
    )


def verify_migration_run(
    document: dict[str, Any],
    expected_names: list[str],
    expected_digest: str,
    fresh: bool,
) -> None:
    require(
        set(document)
        == {
            "database",
            "server_version_num",
            "tls",
            "schema_version",
            "chain_digest",
            "history_table",
            "applied",
            "already_applied",
            "started_at",
            "completed_at",
            "elapsed_ms",
        },
        "migration result shape mismatch",
    )
    require(
        document["database"] == "masi_state_module_test", "migration database mismatch"
    )
    require(
        180000 <= document["server_version_num"] < 190000,
        "migration did not use PostgreSQL 18",
    )
    require(document["tls"] is True, "migration TLS readback missing")
    require(document["schema_version"] == "22", "migration schema version mismatch")
    require(
        document["chain_digest"] == expected_digest, "migration result chain mismatch"
    )
    require(
        document["history_table"] == "masi_migration_history",
        "migration history table mismatch",
    )
    expected_applied = expected_names if fresh else []
    expected_existing = [] if fresh else expected_names
    require(document["applied"] == expected_applied, "migration applied set mismatch")
    require(
        document["already_applied"] == expected_existing,
        "migration existing set mismatch",
    )
    started, completed = (
        parse_time(document["started_at"]),
        parse_time(document["completed_at"]),
    )
    require(completed >= started, "migration timestamps are reversed")
    require(
        type(document["elapsed_ms"]) is int and 0 <= document["elapsed_ms"] <= 600_000,
        "migration elapsed time outside bound",
    )


def verify_catalog(document: dict[str, Any], expected_digest: str) -> None:
    expected_tables = {
        "database_recovery_events",
        "effect_attempts",
        "effect_decisions",
        "effect_intents",
        "effect_proposals",
        "event_identities",
        "events",
        "firewall_bindings",
        "firewall_revisions",
        "fleet_child_intents",
        "fleet_operations",
        "incidents",
        "masi_schema_meta",
        "model_control_incarnations",
        "model_control_state",
        "model_revisions",
        "plugin_bindings",
        "plugin_manifests",
        "plugin_qualifications",
        "plugin_statistic_artifacts",
        "plugin_statistic_runs",
        "plugin_statistic_schedules",
        "plugin_statistics_current",
        "plugin_statistics_definitions",
        "plugin_statistics_history",
        "pool_generations",
        "retention_holds",
        "rule_observation_epochs",
        "rule_observations",
        "rule_rollups_1h",
        "rule_rollups_5m",
        "shard_bindings",
        "target_assignments",
        "target_control_incarnations",
        "target_control_state",
        "targets",
    }
    expected_roles = {
        "masi_analysis_private",
        "masi_backup",
        "masi_control_app",
        "masi_control_maintenance",
        "masi_control_readonly",
        "masi_monitoring",
    }
    require(
        set(document)
        == {
            "database",
            "server_version_num",
            "tls",
            "in_recovery",
            "timeline_id",
            "schema_version",
            "schema_chain_digest",
            "migration_rows",
            "required_tables",
            "required_roles",
            "settings",
            "partitioned_parents",
            "invalid_constraints",
            "writer_gates",
        },
        "catalog readback shape mismatch",
    )
    require(
        document["database"] == "masi_state_module_test", "catalog database mismatch"
    )
    require(
        180000 <= document["server_version_num"] < 190000,
        "catalog did not use PostgreSQL 18",
    )
    require(
        document["tls"] is True and document["in_recovery"] is False,
        "catalog identity mismatch",
    )
    require(
        type(document["timeline_id"]) is int and document["timeline_id"] >= 1,
        "catalog timeline mismatch",
    )
    require(document["schema_version"] == "22", "catalog schema version mismatch")
    require(
        document["schema_chain_digest"] == expected_digest, "catalog chain mismatch"
    )
    require(document["migration_rows"] == 31, "catalog migration row count mismatch")
    require(
        document["required_tables"] == {name: True for name in expected_tables},
        "catalog required-table closure mismatch",
    )
    require(
        document["required_roles"] == {name: True for name in expected_roles},
        "catalog required-role closure mismatch",
    )
    require(
        document["settings"]
        == {
            "archive_mode": "on",
            "idle_in_transaction_session_timeout": "30s",
            "lock_timeout": "5s",
            "max_connections": "96",
            "max_wal_size": "2GB",
            "shared_buffers": "256MB",
            "statement_timeout": "30s",
            "wal_level": "replica",
        },
        "catalog server settings mismatch",
    )
    partitions = document["partitioned_parents"]
    require(
        set(partitions)
        == {"events", "rule_rollups_5m", "rule_rollups_1h", "plugin_statistics_history"}
        and all(type(count) is int and count >= 2 for count in partitions.values()),
        "catalog partition coverage mismatch",
    )
    require(
        document["invalid_constraints"] == [],
        "catalog contains unvalidated constraints",
    )
    require(
        document["writer_gates"] == {"model": False, "target": False},
        "catalog writer gates are open",
    )


def verify_roles(document: dict[str, Any]) -> None:
    no_login = {
        "login": False,
        "superuser": False,
        "create_db": False,
        "create_role": False,
        "replication": False,
        "inherit": True,
        "connection_limit": -1,
    }
    expected = {
        "masi_analysis_private": no_login,
        "masi_app_login": {**no_login, "login": True, "connection_limit": 64},
        "masi_backup": {**no_login, "replication": True},
        "masi_bootstrap": {
            "login": False,
            "superuser": True,
            "create_db": True,
            "create_role": True,
            "replication": True,
            "inherit": True,
            "connection_limit": -1,
        },
        "masi_control_app": no_login,
        "masi_control_maintenance": no_login,
        "masi_control_readonly": no_login,
        "masi_migration_login": {
            **no_login,
            "login": True,
            "inherit": False,
            "connection_limit": 4,
        },
        "masi_monitoring": no_login,
        "masi_monitoring_login": {**no_login, "login": True, "connection_limit": 4},
        "masi_replication_login": {
            **no_login,
            "login": True,
            "replication": True,
            "inherit": False,
            "connection_limit": 4,
        },
    }
    require(
        set(document)
        == {
            "database",
            "tls",
            "roles",
            "app_control_member",
            "app_backup_member",
            "monitoring_member",
            "migration_private_member",
            "migration_monitor_member",
            "public_connect",
            "public_temporary",
        },
        "runtime-role readback shape mismatch",
    )
    require(
        document["database"] == "masi_state_module_test" and document["tls"] is True,
        "runtime-role identity mismatch",
    )
    require(document["roles"] == expected, "runtime-role exact matrix mismatch")
    require(
        document["app_control_member"] is True, "application control membership missing"
    )
    for key in (
        "app_backup_member",
        "migration_private_member",
        "migration_monitor_member",
        "public_connect",
        "public_temporary",
    ):
        require(document[key] is False, f"forbidden runtime role fact: {key}")
    require(document["monitoring_member"] is True, "monitoring membership missing")


def verify_pool(document: dict[str, Any]) -> None:
    require(
        set(document)
        == {
            "tls",
            "version",
            "settings",
            "configured_database",
            "database_host",
            "database_port",
            "pool_rows",
        },
        "pool readback shape mismatch",
    )
    require(
        document["tls"] is True and document["version"] == "PgBouncer 1.25.2",
        "pool identity mismatch",
    )
    require(
        document["settings"]
        == {
            "application_name_add_host": "1",
            "auth_type": "scram-sha-256",
            "client_tls_sslmode": "verify-full",
            "default_pool_size": "24",
            "max_client_conn": "256",
            "max_db_connections": "32",
            "max_packet_size": "16777216",
            "max_user_connections": "32",
            "pool_mode": "transaction",
            "query_timeout": "35",
            "query_wait_timeout": "10",
            "server_check_query": "SELECT 1",
            "server_reset_query": "DISCARD ALL",
            "server_tls_sslmode": "verify-full",
        },
        "pool settings mismatch",
    )
    require(
        document["configured_database"] is True
        and document["database_host"] == "postgres"
        and document["database_port"] == 5432,
        "pool database route mismatch",
    )
    require(
        type(document["pool_rows"]) is int and 1 <= document["pool_rows"] <= 64,
        "pool row count outside bound",
    )


def verify_application(document: dict[str, Any]) -> None:
    require(
        set(document)
        == {
            "database",
            "user",
            "server_version_num",
            "tls",
            "backend_pid",
            "can_insert_retention_hold",
            "can_delete_retention_hold",
            "can_use_analysis_private",
            "is_backup_member",
            "rolled_back_probe_write",
            "plaintext_fallback_rejected",
        },
        "application readback shape mismatch",
    )
    require(
        document["database"] == "masi_state_module_test"
        and document["user"] == "masi_app_login",
        "application identity mismatch",
    )
    require(
        180000 <= document["server_version_num"] < 190000 and document["tls"] is True,
        "application PostgreSQL/TLS mismatch",
    )
    require(
        type(document["backend_pid"]) is int and document["backend_pid"] > 0,
        "application backend PID invalid",
    )
    require(
        document["can_insert_retention_hold"] is True,
        "application insert privilege missing",
    )
    for key in (
        "can_delete_retention_hold",
        "can_use_analysis_private",
        "is_backup_member",
    ):
        require(document[key] is False, f"application has forbidden capability: {key}")
    require(
        document["rolled_back_probe_write"] is True,
        "application probe was not rolled back",
    )
    require(
        document["plaintext_fallback_rejected"] is True,
        "plaintext fallback was not rejected",
    )


def verify_command_sidecar(
    command_id: str,
    sidecar_path: Path,
    run_directory: Path,
    command_schema: Path,
    run_id: str,
    source_tree_digest: str,
    status_digest: str,
) -> dict[str, Any]:
    validate_document(sidecar_path, command_schema)
    sidecar = read(sidecar_path)
    require(
        sidecar["run_id"] == run_id and sidecar["command_id"] == command_id,
        f"command identity mismatch: {command_id}",
    )
    require(
        sidecar["working_directory"] == "db",
        f"command working directory mismatch: {command_id}",
    )
    require(
        sidecar["source_tree_digest"] == source_tree_digest,
        f"command source digest mismatch: {command_id}",
    )
    require(
        sidecar["working_tree_status_digest"] == status_digest,
        f"command status digest mismatch: {command_id}",
    )
    require(
        sidecar["result"] == "PASS"
        and sidecar["qualification"] == "QUALIFIED"
        and sidecar["exit_code"] == 0
        and sidecar.get("stable_reason") is None,
        f"command did not pass: {command_id}",
    )
    started, finished = (
        parse_time(sidecar["started_at"]),
        parse_time(sidecar["finished_at"]),
    )
    require(finished >= started, f"command timestamps are reversed: {command_id}")
    expected_log = f"{command_id}.log"
    require(
        sidecar["log"]["path"] == expected_log,
        f"command log path mismatch: {command_id}",
    )
    log = run_directory / expected_log
    require(
        sidecar["log"]["sha256"] == digest(log),
        f"command log digest mismatch: {command_id}",
    )
    require(
        sidecar["log"]["bytes"] == log.stat().st_size,
        f"command log size mismatch: {command_id}",
    )
    return sidecar


def verify_traceability(
    document: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    run_directory: Path,
    sidecars: dict[str, dict[str, Any]],
) -> None:
    requirement_ids = [item["requirement_id"] for item in manifest["requirements"]]
    require(
        document["schema_version"] == "postgresql-state-requirement-traceability/v1",
        "traceability schema identity mismatch",
    )
    require(document["module_id"] == "MOD-DB-001", "traceability module mismatch")
    require(
        document["manifest"] == "db/requirements-traceability.json",
        "traceability manifest path mismatch",
    )
    require(
        document["manifest_digest"] == digest(manifest_path),
        "traceability manifest digest mismatch",
    )
    require(
        document["evidence_root"] == str(run_directory),
        "traceability evidence root mismatch",
    )
    require(
        document["result"] == "PASS"
        and document["qualification"] == "QUALIFIED"
        and document["valid"] is True
        and document["failures"] == [],
        "traceability is not a qualified PASS",
    )
    require(
        document["requirement_ids"] == requirement_ids,
        "traceability requirement set mismatch",
    )
    require(
        document["conditional_applicability"] == manifest["conditional_applicability"],
        "traceability conditional applicability mismatch",
    )
    requirements = document["requirements"]
    require(
        len(requirements) == len(manifest["requirements"]),
        "traceability requirement count mismatch",
    )
    bindings = {item["command_id"]: item for item in manifest["execution_bindings"]}
    for actual, expected in zip(requirements, manifest["requirements"], strict=True):
        require(
            actual["requirement_id"] == expected["requirement_id"],
            "traceability requirement order mismatch",
        )
        require(
            actual["scenario_ids"] == expected["scenario_ids"],
            "traceability scenario mismatch",
        )
        require(
            actual["qualification_limit"] == expected["qualification_limit"],
            "traceability qualification limit mismatch",
        )
        require(
            actual["evidence_result"] == "PASS"
            and actual["qualification"] == "QUALIFIED",
            "traceability requirement did not pass",
        )
        require(
            [item["command_id"] for item in actual["test_targets"]]
            == expected["command_ids"],
            "traceability target command mismatch",
        )
        require(
            [item["target"] for item in actual["test_targets"]]
            == [
                ",".join(bindings[command_id]["targets"])
                for command_id in expected["command_ids"]
            ],
            "traceability target name mismatch",
        )
        require(
            [item["path"] for item in actual["evidence"]]
            == [f"{command_id}.command.json" for command_id in expected["command_ids"]],
            "traceability evidence path mismatch",
        )
        require(
            all(
                item["result"] == "PASS" and item["qualification"] == "QUALIFIED"
                for item in actual["test_targets"] + actual["evidence"]
            ),
            "traceability contains a non-PASS binding",
        )
    expected_artifacts = {
        "db/requirements-traceability.json": (
            digest(manifest_path),
            manifest_path.stat().st_size,
        ),
        **{
            f"{command_id}.command.json": (
                digest(run_directory / f"{command_id}.command.json"),
                (run_directory / f"{command_id}.command.json").stat().st_size,
            )
            for command_id in sidecars
        },
    }
    observed_artifacts = {
        item["path"]: (item["sha256"], item["bytes"]) for item in document["artifacts"]
    }
    require(
        observed_artifacts == expected_artifacts,
        "traceability artifact closure mismatch",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-directory", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-tree-digest", required=True)
    parser.add_argument("--working-tree-status-digest", required=True)
    parser.add_argument(
        "--working-tree-dirty", required=True, choices=("true", "false")
    )
    parser.add_argument("--db-image-id", required=True)
    parser.add_argument("--pgbouncer-image-id", required=True)
    parser.add_argument("--postgres-image-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repo_root = args.manifest.parent.parent
    require(
        args.run_directory.is_absolute()
        and not args.run_directory.is_symlink()
        and args.run_directory.is_dir(),
        "unsafe run directory",
    )
    manifest = read(args.manifest)
    require(
        manifest.get("module_id") == "MOD-DB-001",
        "requirement manifest module mismatch",
    )
    require(
        len(manifest.get("requirements", [])) == 64,
        "requirement manifest must contain 64 items",
    )
    require(
        all(
            binding.get("required") is True
            for binding in manifest.get("execution_bindings", [])
        ),
        "all manifest command bindings must be required",
    )

    schemas = {
        "command": repo_root / "contracts/evidence/command/v1/schema.json",
        "soak": repo_root / "contracts/evidence/soak/v1/schema.json",
        "capacity": repo_root
        / "contracts/evidence/postgresql-state-capacity/v1/schema.json",
        "recovery": repo_root
        / "contracts/evidence/postgresql-state-recovery/v1/schema.json",
        "traceability": repo_root / "contracts/evidence/traceability/v1/schema.json",
        "findings": repo_root / "contracts/evidence/module-findings/v1/schema.json",
        "oci": repo_root / "contracts/evidence/postgresql-state-oci/v1/schema.json",
        "supply": repo_root
        / "contracts/evidence/postgresql-state-supply/v1/schema.json",
        "module": repo_root
        / "contracts/evidence/v1/postgresql-state-module-schema.json",
    }
    executed: dict[str, str] = {}
    sidecars: dict[str, dict[str, Any]] = {}
    for binding in manifest["execution_bindings"]:
        command_id = binding["command_id"]
        require(command_id not in sidecars, f"duplicate command binding: {command_id}")
        sidecar = verify_command_sidecar(
            command_id,
            args.run_directory / f"{command_id}.command.json",
            args.run_directory,
            schemas["command"],
            args.run_id,
            args.source_tree_digest,
            args.working_tree_status_digest,
        )
        sidecars[command_id] = sidecar
        executed[command_id] = sidecar["result"]

    findings_path = args.manifest.parent / "module-findings.json"
    validate_document(findings_path, schemas["findings"])
    findings = read(findings_path)
    require(
        findings["module_id"] == "MOD-DB-001" and findings["decision_id"] == "DEC-044",
        "findings registry identity mismatch",
    )
    open_findings = [item for item in findings["findings"] if item["status"] == "OPEN"]

    formal = args.run_directory / "formal-soak" / "formal-soak-evidence.json"
    formal_raw = args.run_directory / "formal-soak" / "raw-workload.json"
    formal_resources = args.run_directory / "formal-soak" / "resource-samples.json"
    capacity = args.run_directory / "capacity" / "capacity-evidence.json"
    capacity_raw = args.run_directory / "capacity" / "capacity-raw.json"
    recovery = args.run_directory / "recovery" / "recovery-evidence.json"
    backup_manifest = args.run_directory / "recovery" / "backup-manifest.sha256"
    pitr_before = args.run_directory / "recovery" / "pitr-before.json"
    pitr_rotation = args.run_directory / "recovery" / "pitr-rotation.json"
    pitr_after = args.run_directory / "recovery" / "pitr-after.json"
    replica_before = args.run_directory / "recovery" / "replica-before.json"
    replica_after = args.run_directory / "recovery" / "replica-after.json"
    oci = args.run_directory / "oci" / "oci-evidence.json"
    supply = args.run_directory / "supply" / "supply-evidence.json"
    traceability = args.run_directory / "traceability.json"
    migration_chain = args.run_directory / "migration-chain.json"
    fresh = args.run_directory / "fresh-migration.json"
    repeat = args.run_directory / "repeat-migration.json"
    disk_fault = args.run_directory / "disk-fault-migration.json"
    catalog = args.run_directory / "catalog-readback.json"
    roles = args.run_directory / "runtime-roles.json"
    pool = args.run_directory / "pool-readback.json"
    application = args.run_directory / "application-boundary.json"
    required_artifacts = [
        formal,
        formal_raw,
        formal_resources,
        capacity,
        capacity_raw,
        recovery,
        backup_manifest,
        pitr_before,
        pitr_rotation,
        pitr_after,
        replica_before,
        replica_after,
        oci,
        supply,
        traceability,
        migration_chain,
        fresh,
        repeat,
        disk_fault,
        catalog,
        roles,
        pool,
        application,
    ]
    if any(not path.is_file() for path in required_artifacts):
        raise ValueError("one or more required summary artifacts are missing")

    expected_migrations, expected_chain = source_migration_chain(
        args.manifest.parent / "migrations"
    )
    migration_chain_document = read(migration_chain)
    verify_migration_manifest(
        migration_chain_document, expected_migrations, expected_chain
    )
    expected_names = [item["name"] for item in expected_migrations]
    fresh_document = read(fresh)
    repeat_document = read(repeat)
    disk_fault_document = read(disk_fault)
    verify_migration_run(fresh_document, expected_names, expected_chain, fresh=True)
    verify_migration_run(repeat_document, expected_names, expected_chain, fresh=False)
    verify_migration_run(
        disk_fault_document, expected_names, expected_chain, fresh=True
    )
    verify_catalog(read(catalog), expected_chain)
    verify_roles(read(roles))
    verify_pool(read(pool))
    verify_application(read(application))

    validate_document(formal, schemas["soak"])
    formal_document = read(formal)
    require(
        formal_document["run_id"] == args.run_id
        and formal_document["module"] == "postgresql-state",
        "formal soak identity mismatch",
    )
    require(
        formal_document["level"] == "MODULE"
        and formal_document["applicability"] == "APPLICABLE"
        and formal_document["result"] == "PASS"
        and formal_document["qualification"] == "QUALIFIED",
        "formal soak is not a qualified module PASS",
    )
    require(
        formal_document["profile_digest"]
        == digest(repo_root / "contracts/profiles/v1/qualification-soak-3600s.json"),
        "formal soak profile digest mismatch",
    )
    formal_artifacts = {
        item["name"]: ("sha256:" + item["sha256"], item["bytes"])
        for item in formal_document["artifacts"]
    }
    require(
        formal_artifacts
        == {
            "raw-workload.json": (digest(formal_raw), formal_raw.stat().st_size),
            "resource-samples.json": (
                digest(formal_resources),
                formal_resources.stat().st_size,
            ),
        },
        "formal soak raw artifact closure mismatch",
    )

    validate_document(capacity, schemas["capacity"])
    capacity_document = read(capacity)
    require(
        capacity_document["run_id"] == args.run_id
        and capacity_document["result"] == "PASS"
        and capacity_document["qualification"] == "NOT_QUALIFIED",
        "capacity evidence mismatch",
    )
    capacity_raw_document = read(capacity_raw)
    require(
        capacity_raw_document.get("schema_version") == "postgresql-state-workload/v1"
        and capacity_raw_document.get("mode") == "capacity"
        and capacity_raw_document.get("result") == "PASS",
        "capacity raw identity mismatch",
    )
    require(
        capacity_raw_document.get("identity") == capacity_document["identity"]
        and capacity_raw_document.get("capacity") == capacity_document["matrix"],
        "capacity raw/evidence closure mismatch",
    )
    require(
        capacity_raw_document.get("phases")
        == [
            {"name": "steady", "target_qps": 20, "concurrency": 4},
            {"name": "peak", "target_qps": 80, "concurrency": 12},
            {"name": "saturation", "target_qps": 160, "concurrency": 32},
            {"name": "recovery-or-activation", "target_qps": 40, "concurrency": 8},
        ],
        "capacity phase profile mismatch",
    )

    validate_document(recovery, schemas["recovery"])
    recovery_document = read(recovery)
    require(
        recovery_document["run_id"] == args.run_id
        and recovery_document["result"] == "PASS"
        and recovery_document["qualification"] == "NOT_QUALIFIED",
        "recovery evidence mismatch",
    )
    require(
        recovery_document["pitr"]["evidence_digests"]
        == [digest(pitr_before), digest(pitr_rotation), digest(pitr_after)],
        "PITR raw artifact closure mismatch",
    )
    require(
        recovery_document["streaming_failover"]["evidence_digests"]
        == [digest(replica_before), digest(replica_after)],
        "streaming failover raw artifact closure mismatch",
    )
    backup_text = backup_manifest.read_text(encoding="utf-8")
    backup_match = re.fullmatch(
        r"([0-9a-f]{64})  /base/18/docker/backup_manifest\n", backup_text
    )
    require(
        backup_match is not None, "backup manifest checksum evidence shape mismatch"
    )
    require(
        "sha256:" + backup_match.group(1)
        == recovery_document["pitr"]["backup_manifest_digest"],
        "backup manifest digest mismatch",
    )

    validate_document(oci, schemas["oci"])
    oci_document = read(oci)
    expected_image_ids = {
        "db": args.db_image_id,
        "pgbouncer": args.pgbouncer_image_id,
        "postgres": args.postgres_image_id,
    }
    require(oci_document["run_id"] == args.run_id, "OCI run identity mismatch")
    require(
        oci_document["image_ids"] == expected_image_ids, "OCI image identity mismatch"
    )
    require(
        oci_document["migration_count"] == 31
        and oci_document["migration_chain_digest"] == expected_chain,
        "OCI migration closure mismatch",
    )

    validate_document(supply, schemas["supply"])
    supply_document = read(supply)
    require(supply_document["run_id"] == args.run_id, "supply run identity mismatch")
    require(
        supply_document["image_ids"] == expected_image_ids,
        "supply image identity mismatch",
    )
    require(
        supply_document["images"] == oci_document["images"],
        "OCI/supply image tags differ",
    )
    supply_raw_digests = verify_supply_raw(supply_document, supply.parent)

    validate_document(traceability, schemas["traceability"])
    traceability_document = read(traceability)
    verify_traceability(
        traceability_document, manifest, args.manifest, args.run_directory, sidecars
    )

    all_pass = all(value == "PASS" for value in executed.values())
    formal_pass = formal_document["result"] == "PASS"
    capacity_pass = capacity_document["result"] == "PASS"
    recovery_pass = recovery_document["result"] == "PASS"
    complete = (
        all_pass
        and formal_pass
        and capacity_pass
        and recovery_pass
        and not any(item["severity"] == "P0" for item in open_findings)
    )
    artifacts = {
        "migration_chain": expected_chain,
        "migration_0029": digest(
            args.manifest.parent / "migrations/0029_postgresql_state_hardening.sql"
        ),
        "migration_0030": digest(
            args.manifest.parent / "migrations/0030_partition_default_drain.sql"
        ),
        "migration_0031": digest(
            args.manifest.parent / "migrations/0031_reject_zero_digest_defaults.sql"
        ),
        "requirements_baseline": digest(
            repo_root / "docs/masi-nids-vnext-system-requirements-2026-08-09.md"
        ),
        "traceability_manifest": digest(args.manifest),
        "traceability_evidence": digest(traceability),
        "module_findings": digest(findings_path),
        "module_findings_schema": digest(schemas["findings"]),
        "formal_soak_evidence": digest(formal),
        "capacity_evidence": digest(capacity),
        "recovery_evidence": digest(recovery),
        "oci_evidence": digest(oci),
        "supply_evidence": digest(supply),
        "db_image_id": args.db_image_id,
        "pgbouncer_image_id": args.pgbouncer_image_id,
        "postgres_image_id": args.postgres_image_id,
        "performance_profile": digest(
            repo_root
            / "contracts/profiles/v1/performance-environment-postgresql-state.json"
        ),
        "reliability_profile": digest(
            repo_root
            / "contracts/profiles/v1/reliability-environment-postgresql-state-single-domain.json"
        ),
        "migration_chain_evidence": digest(migration_chain),
        "fresh_migration_evidence": digest(fresh),
        "repeat_migration_evidence": digest(repeat),
        "disk_fault_migration_evidence": digest(disk_fault),
        "catalog_readback_evidence": digest(catalog),
        "runtime_roles_evidence": digest(roles),
        "pool_readback_evidence": digest(pool),
        "application_boundary_evidence": digest(application),
        "formal_soak_raw": digest(formal_raw),
        "formal_soak_resources": digest(formal_resources),
        "capacity_raw": digest(capacity_raw),
        "backup_manifest_checksum": digest(backup_manifest),
        "pitr_before": digest(pitr_before),
        "pitr_rotation": digest(pitr_rotation),
        "pitr_after": digest(pitr_after),
        "replica_before": digest(replica_before),
        "replica_after": digest(replica_after),
        "command_evidence_schema": digest(schemas["command"]),
        "soak_evidence_schema": digest(schemas["soak"]),
        "capacity_evidence_schema": digest(schemas["capacity"]),
        "recovery_evidence_schema": digest(schemas["recovery"]),
        "traceability_evidence_schema": digest(schemas["traceability"]),
        "oci_evidence_schema": digest(schemas["oci"]),
        "supply_evidence_schema": digest(schemas["supply"]),
        "module_summary_schema": digest(schemas["module"]),
        **{f"supply_raw_{name}": value for name, value in supply_raw_digests.items()},
    }
    document = {
        "schema_version": "postgresql-state-module-gate-summary/v1",
        "test_id": "TEST-DB-MODULE-GATES-001",
        "requirement_ids": [
            item["requirement_id"] for item in manifest["requirements"]
        ],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "HOLD" if complete else "FAIL",
        "qualification": "NOT_QUALIFIED",
        "module_id": "MOD-DB-001",
        "run_id": args.run_id,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_revision": args.source_revision,
        "source_tree_digest": args.source_tree_digest,
        "working_tree_dirty": args.working_tree_dirty == "true",
        "working_tree_status_digest": args.working_tree_status_digest,
        "overall_module_complete": complete,
        "artifact_digests": artifacts,
        "executed_gates": executed,
        "qualification_gates": {
            "formal_soak_3600_seconds": "PASS" if formal_pass else "FAIL",
            "single_domain_physical_recovery": "PASS" if recovery_pass else "FAIL",
            "protected_release_baseline": "HOLD",
            "owner_frozen_absolute_slo": "HOLD",
            "production_ha_multi_failure_domain": "HOLD",
        },
        "conditional_applicability": manifest["conditional_applicability"],
        "completion": {
            "operational_gates_pass": all_pass
            and formal_pass
            and capacity_pass
            and recovery_pass,
            "open_p0_zero": not any(item["severity"] == "P0" for item in open_findings),
            "real_runtime_started": executed.get("fresh-migration") == "PASS"
            and executed.get("pool-readback") == "PASS",
            "formal_soak_executed": formal_pass,
            "no_required_not_run": all(
                value != "NOT_RUN" for value in executed.values()
            ),
        },
        "blackbox_e2e": {
            "result": executed.get("blackbox", "NOT_RUN"),
            "evidence": "blackbox.log",
            "digest": digest(args.run_directory / "blackbox.log"),
        },
        "recovery": {
            "result": "PASS" if recovery_pass else "FAIL",
            "evidence": "recovery/recovery-evidence.json",
            "digest": digest(recovery),
        },
        "performance": {
            "result": "PASS" if capacity_pass else "FAIL",
            "evidence": "capacity/capacity-evidence.json",
            "digest": digest(capacity),
        },
        "formal_soak": {
            "result": "PASS" if formal_pass else "FAIL",
            "evidence": "formal-soak/formal-soak-evidence.json",
            "digest": digest(formal),
        },
        "findings": {
            "open_total": len(open_findings),
            "open_p0": sum(item["severity"] == "P0" for item in open_findings),
        },
        "remaining_holds": [
            "DEC-001 absolute production performance/RPO/RTO thresholds are not Owner-frozen.",
            "The acceptance host is one physical failure domain with manual promotion, not production-ha automatic failover.",
            "A protected release commit/tag and signed provenance have not been established for this dirty-tree implementation run.",
        ],
    }
    if (
        args.output.exists()
        or args.output.is_symlink()
        or not args.output.is_absolute()
    ):
        raise ValueError("output must be a fresh absolute path")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
