#!/usr/bin/env python3
"""Fail-closed structural and coverage validator for MOD-DB-001 traceability."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


EXPECTED_REQUIREMENTS = {
    "ARCH-003",
    "ARCH-REUSE-001",
    "CONTRACT-001",
    "CONTRACT-PROFILE-001",
    "DB-001",
    "DB-002",
    "DB-003",
    "DB-004",
    "DB-005",
    "DB-006",
    "DB-007",
    "DB-FW-001",
    "DB-GOV-001",
    "DB-MODEL-001",
    "DB-PLUGIN-001",
    "DB-PLUGIN-STAT-001",
    "DB-REDIS-001",
    "DB-RULE-001",
    "DB-TARGET-FLEET-001",
    "DEC-003",
    "DEC-039",
    "DEC-042",
    "DEC-044",
    "FUNC-FW-001",
    "FUNC-RULE-001",
    "MIG-001",
    "MIG-003",
    "MIG-005",
    "MIG-INF-001",
    "MIG-P4-FW-001",
    "MIG-PLUGIN-001",
    "MIG-TARGET-FLEET-001",
    "MIG-TEL-INF-001",
    "MOD-DB-001",
    "OBS-001",
    "OBS-002",
    "OBS-003",
    "PERF-001",
    "PERF-002",
    "PERF-P4-FW-001",
    "PERF-PLUGIN-STAT-001",
    "PERF-RULE-001",
    "PERF-TARGET-FLEET-001",
    "REL-001",
    "REL-002",
    "REL-003",
    "SEC-001",
    "SEC-003",
    "SEC-005",
    "SEC-SUPPLY-001",
    "TEST-002",
    "TEST-003",
    "TEST-007",
    "TEST-008",
    "TEST-009",
    "TEST-010",
    "TEST-GATE-001",
    "TEST-INF-001",
    "TEST-P4-FW-001",
    "TEST-PLUGIN-STAT-001",
    "TEST-REAL-E2E-001",
    "TEST-REUSE-001",
    "TEST-RULE-001",
    "TEST-TARGET-FLEET-001",
}
EXPECTED_CONDITIONALS = {
    ("production-ha/v1", "SINGLE_FAILURE_DOMAIN_HOST"),
    ("managed-postgresql/v1", "SELF_HOSTED_SINGLE_DOMAIN_PROFILE"),
    ("cross-region-wal-archive/v1", "CROSS_REGION_NOT_TRIGGERED_ACCEPTANCE_TIER"),
    ("automatic-failover/v1", "NOT_OPERATIONAL_SINGLE_DOMAIN_PROFILE"),
}
ID = re.compile(r"^[A-Z][A-Z0-9-]{2,63}$")
COMMAND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise ValueError(f"unsafe manifest: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    manifest_path = args.manifest.resolve(strict=True)
    if repo not in manifest_path.parents:
        raise ValueError("manifest must be inside repository")
    manifest = read_object(manifest_path)
    if set(manifest) != {
        "schema_version",
        "module_id",
        "owner_role",
        "design_anchor",
        "commands",
        "execution_bindings",
        "public_contracts",
        "requirements",
        "conditional_applicability",
    }:
        raise ValueError("manifest root fields mismatch")
    if manifest["schema_version"] != "postgresql-state-requirement-manifest/v1":
        raise ValueError("manifest schema version mismatch")
    if (
        manifest["module_id"] != "MOD-DB-001"
        or manifest["owner_role"] != "PostgreSQL State module owner"
    ):
        raise ValueError("manifest module/owner mismatch")

    bindings = manifest["execution_bindings"]
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 64:
        raise ValueError("execution binding count outside 1..64")
    command_ids: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {
            "command_id",
            "required",
            "targets",
        }:
            raise ValueError("execution binding fields mismatch")
        command_id = binding["command_id"]
        if (
            not isinstance(command_id, str)
            or not COMMAND.fullmatch(command_id)
            or command_id in command_ids
        ):
            raise ValueError(f"invalid or duplicate command id: {command_id!r}")
        if binding["required"] is not True:
            raise ValueError(f"State binding must be required: {command_id}")
        targets = binding["targets"]
        if (
            not isinstance(targets, list)
            or not 1 <= len(targets) <= 16
            or any(
                not isinstance(target, str) or not 1 <= len(target) <= 128
                for target in targets
            )
        ):
            raise ValueError(f"invalid targets for {command_id}")
        command_ids.add(command_id)

    requirements = manifest["requirements"]
    if not isinstance(requirements, list) or len(requirements) != len(
        EXPECTED_REQUIREMENTS
    ):
        raise ValueError("State requirement count mismatch")
    observed: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict) or set(requirement) != {
            "requirement_id",
            "scenario_ids",
            "command_ids",
            "qualification_limit",
        }:
            raise ValueError("requirement mapping fields mismatch")
        requirement_id = requirement["requirement_id"]
        if (
            not isinstance(requirement_id, str)
            or not ID.fullmatch(requirement_id)
            or requirement_id in observed
        ):
            raise ValueError(f"invalid or duplicate requirement id: {requirement_id!r}")
        scenarios = requirement["scenario_ids"]
        commands = requirement["command_ids"]
        limit = requirement["qualification_limit"]
        if (
            not isinstance(scenarios, list)
            or not 1 <= len(scenarios) <= 16
            or any(
                not isinstance(scenario, str) or not ID.fullmatch(scenario)
                for scenario in scenarios
            )
        ):
            raise ValueError(f"invalid scenarios for {requirement_id}")
        if (
            not isinstance(commands, list)
            or not 1 <= len(commands) <= 32
            or len(commands) != len(set(commands))
        ):
            raise ValueError(f"invalid commands for {requirement_id}")
        missing_commands = set(commands) - command_ids
        if missing_commands:
            raise ValueError(
                f"unknown commands for {requirement_id}: {sorted(missing_commands)}"
            )
        if not isinstance(limit, str) or not 1 <= len(limit) <= 1024:
            raise ValueError(f"invalid qualification limit for {requirement_id}")
        observed.add(requirement_id)
    if observed != EXPECTED_REQUIREMENTS:
        raise ValueError(
            f"requirement coverage mismatch missing={sorted(EXPECTED_REQUIREMENTS - observed)} "
            f"extra={sorted(observed - EXPECTED_REQUIREMENTS)}"
        )

    contracts = manifest["public_contracts"]
    if (
        not isinstance(contracts, list)
        or not 1 <= len(contracts) <= 64
        or len(contracts) != len(set(contracts))
    ):
        raise ValueError("public contract list invalid")
    for relative in contracts:
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            raise ValueError(f"unsafe public contract path: {relative!r}")
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"public contract missing or unsafe: {relative}")

    conditionals = manifest["conditional_applicability"]
    observed_conditionals = {
        (item.get("profile"), item.get("stable_reason"))
        for item in conditionals
        if isinstance(item, dict)
    }
    if (
        len(conditionals) != 4
        or observed_conditionals != EXPECTED_CONDITIONALS
        or any(
            item.get("applicability") != "NOT_APPLICABLE"
            or item.get("result") != "NOT_RUN"
            for item in conditionals
        )
    ):
        raise ValueError("conditional applicability matrix mismatch")

    print(
        "postgresql-state-traceability-manifest-ok "
        f"requirements={len(observed)} commands={len(command_ids)} contracts={len(contracts)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
