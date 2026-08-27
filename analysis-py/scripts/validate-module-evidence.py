#!/usr/bin/env python3
"""Validate Analysis completion evidence and fail-closed tamper negatives."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

REFERENCES = [
    "release_runtime",
    "blackbox_e2e",
    "compatibility",
    "performance",
    "image_build",
    "oci",
    "deployment",
    "supply_chain",
    "formal_soak",
    "traceability",
]


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not a regular no-symlink JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def schema_errors(schema: dict[str, Any], document: dict[str, Any]) -> list[str]:
    return [f"{list(error.absolute_path)}: {error.message}" for error in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document)]


def reference_errors(run_dir: Path, document: dict[str, Any]) -> list[str]:
    failures = []
    for name in REFERENCES:
        reference = document[name]
        target = run_dir / reference["evidence"]
        if target.is_symlink() or not target.is_file():
            failures.append(f"missing reference {name}")
        elif digest(target) != reference["digest"]:
            failures.append(f"digest mismatch {name}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--negative-self-test", action="store_true")
    parser.add_argument("--provisional", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    run_dir = args.run_dir.resolve(strict=True)
    document = load(args.summary.resolve(strict=True))
    schema = load(repo / "contracts/evidence/analysis-module/v1/schema.json")
    failures = schema_errors(schema, document) + reference_errors(run_dir, document)
    manifest = load(repo / "analysis-py/requirements-traceability.json")
    required_commands = {item["command_id"] for item in manifest["execution_bindings"]}
    if not args.provisional:
        required_commands.add("evidence-integrity")
    if set(document.get("executed_gates", {})) != required_commands:
        failures.append("executed gate set differs from the exact manifest")
    if failures:
        raise SystemExit("Analysis module evidence rejected: " + "; ".join(failures))
    if args.negative_self_test:
        incomplete = copy.deepcopy(document)
        incomplete["overall_module_complete"] = False
        if not schema_errors(schema, incomplete):
            raise SystemExit("negative completion mutation was accepted")
        tampered = copy.deepcopy(document)
        tampered["formal_soak"]["digest"] = "sha256:" + "0" * 64
        if not reference_errors(run_dir, tampered):
            raise SystemExit("negative evidence digest mutation was accepted")
        failed_gate = copy.deepcopy(document)
        failed_gate["executed_gates"]["release-blackbox"] = "FAIL"
        if not schema_errors(schema, failed_gate):
            raise SystemExit("negative gate mutation was accepted")
        soak = load(run_dir / document["formal_soak"]["evidence"])
        soak_schema = load(repo / "contracts/evidence/analysis-soak/v1/schema.json")
        shortened = copy.deepcopy(soak)
        shortened["qualified_elapsed_ms"] = 3_599_999
        if not schema_errors(soak_schema, shortened):
            raise SystemExit("negative shortened formal soak was accepted")
    print("analysis module evidence validation: PASS")


if __name__ == "__main__":
    main()
