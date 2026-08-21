#!/usr/bin/env python3
"""Validate a Plugin Host summary and prove tamper negatives fail closed."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not a regular no-symlink JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def schema_errors(schema: dict, document: dict) -> list[str]:
    return [
        error.message
        for error in Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).iter_errors(document)
    ]


def validate_references(run_dir: Path, document: dict) -> list[str]:
    failures = []
    for name in [
        "blackbox_e2e",
        "fault_recovery",
        "wasm_faults",
        "deep_checks",
        "performance",
        "oci",
        "supply_chain",
        "formal_soak",
        "traceability",
    ]:
        reference = document[name]
        target = run_dir / reference["evidence"]
        if target.is_symlink() or not target.is_file():
            failures.append(f"missing reference {name}")
        elif digest(target) != reference["digest"]:
            failures.append(f"digest mismatch {name}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--negative-self-test", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    run_dir = args.run_dir.resolve(strict=True)
    document = load(args.summary.resolve(strict=True))
    schema = load(repo / "contracts/evidence/plugin-runtime-host-module/v1/schema.json")
    failures = schema_errors(schema, document) + validate_references(run_dir, document)
    if failures:
        raise SystemExit("module evidence rejected: " + "; ".join(failures))
    if args.negative_self_test:
        incomplete = copy.deepcopy(document)
        incomplete["overall_module_complete"] = False
        if not schema_errors(schema, incomplete):
            raise SystemExit("negative completion mutation was accepted")
        tampered = copy.deepcopy(document)
        tampered["formal_soak"]["digest"] = "sha256:" + "0" * 64
        if not validate_references(run_dir, tampered):
            raise SystemExit("negative evidence-digest mutation was accepted")
        failed_gate = copy.deepcopy(document)
        failed_gate["executed_gates"]["release-blackbox"] = "FAIL"
        if not schema_errors(schema, failed_gate):
            raise SystemExit("negative executed-gate mutation was accepted")
    print("plugin-host module evidence validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
