#!/usr/bin/env python3
"""Validate findings and the static requirement manifest before gate aggregation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular no-symlink JSON: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(schema: dict, document: dict, name: str) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ValueError(
            name + ": " + "; ".join(
                f"{list(error.absolute_path)}: {error.message}" for error in errors
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--scope", choices=["findings", "traceability", "all"], default="all")
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    host = repo / "plugin-host-rs"
    if args.scope in {"findings", "all"}:
        findings = load(host / "module-findings.json")
        validate(load(repo / "contracts/evidence/module-findings/v1/schema.json"), findings, "findings")
        open_p0 = [
            item["finding_id"]
            for item in findings["findings"]
            if item["status"] == "OPEN" and item["severity"] == "P0"
        ]
        if open_p0:
            raise SystemExit("open P0 findings: " + ", ".join(open_p0))
    if args.scope in {"traceability", "all"}:
        manifest = load(host / "requirements-traceability.json")
        if manifest.get("schema_version") != "plugin-runtime-host-requirement-manifest/v1":
            raise SystemExit("traceability manifest schema mismatch")
        bindings = manifest.get("execution_bindings", [])
        command_ids = [item.get("command_id") for item in bindings]
        if len(command_ids) != len(set(command_ids)) or any(not item.get("required") for item in bindings):
            raise SystemExit("traceability execution bindings are duplicate or optional")
        requirements = manifest.get("requirements", [])
        requirement_ids = [item.get("requirement_id") for item in requirements]
        if not 32 <= len(requirement_ids) <= 64 or len(requirement_ids) != len(set(requirement_ids)):
            raise SystemExit("traceability requirement set is not unique/bounded")
        known_commands = set(command_ids)
        for item in requirements:
            if not item.get("scenario_ids") or not item.get("qualification_limit"):
                raise SystemExit(f"incomplete requirement mapping: {item.get('requirement_id')}")
            if not set(item.get("command_ids", [])).issubset(known_commands):
                raise SystemExit(f"unknown command mapping: {item.get('requirement_id')}")
        if len(manifest.get("conditional_applicability", [])) != 6:
            raise SystemExit("Plugin Host conditional applicability must have exact six entries")
        for path in manifest.get("public_contracts", []):
            candidate = repo / path
            if candidate.is_symlink() or not candidate.is_file():
                raise SystemExit(f"public contract missing: {path}")
    print(f"plugin-host static evidence {args.scope}: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
