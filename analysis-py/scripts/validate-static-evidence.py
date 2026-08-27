#!/usr/bin/env python3
"""Validate Analysis findings and static requirement manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular no-symlink JSON: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(schema: dict[str, Any], document: dict[str, Any], name: str) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ValueError(name + ": " + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--scope", choices=["findings", "traceability", "all"], default="all")
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    module = repo / "analysis-py"
    if args.scope in {"findings", "all"}:
        findings = load(module / "module-findings.json")
        validate(load(repo / "contracts/evidence/module-findings/v1/schema.json"), findings, "findings")
        open_p0 = [item["finding_id"] for item in findings["findings"] if item["status"] == "OPEN" and item["severity"] == "P0"]
        if open_p0:
            raise SystemExit("open P0 findings: " + ", ".join(open_p0))
    if args.scope in {"traceability", "all"}:
        manifest = load(module / "requirements-traceability.json")
        if manifest.get("schema_version") != "analysis-plugin-requirement-manifest/v1" or manifest.get("module_id") != "MOD-AGENT-001":
            raise SystemExit("Analysis traceability manifest identity mismatch")
        bindings = manifest.get("execution_bindings", [])
        command_ids = [item.get("command_id") for item in bindings]
        if len(command_ids) != len(set(command_ids)) or any(not item.get("required") for item in bindings):
            raise SystemExit("traceability execution bindings are duplicate or optional")
        requirements = manifest.get("requirements", [])
        requirement_ids = [item.get("requirement_id") for item in requirements]
        if not 48 <= len(requirement_ids) <= 64 or len(requirement_ids) != len(set(requirement_ids)):
            raise SystemExit("Analysis requirement set is not unique or bounded")
        baseline = (repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md").read_text(encoding="utf-8")
        missing = [item for item in requirement_ids if not isinstance(item, str) or item not in baseline]
        if missing:
            raise SystemExit("requirement absent from baseline: " + ", ".join(str(item) for item in missing))
        known_commands = set(command_ids)
        for item in requirements:
            if not item.get("scenario_ids") or not item.get("qualification_limit"):
                raise SystemExit(f"incomplete requirement mapping: {item.get('requirement_id')}")
            if not set(item.get("command_ids", [])).issubset(known_commands):
                raise SystemExit(f"unknown command mapping: {item.get('requirement_id')}")
        if len(manifest.get("conditional_applicability", [])) != 5:
            raise SystemExit("Analysis conditional applicability must have exact five entries")
        for path in manifest.get("public_contracts", []):
            candidate = repo / path
            if candidate.is_symlink() or not candidate.is_file():
                raise SystemExit(f"public contract missing: {path}")
    print(f"analysis static evidence {args.scope}: PASS")


if __name__ == "__main__":
    main()
