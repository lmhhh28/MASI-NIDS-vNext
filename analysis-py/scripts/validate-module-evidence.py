#!/usr/bin/env python3
"""Validate Analysis completion evidence and fail-closed tamper negatives."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from evidence_common import (
    ZERO_DIGEST,
    current_snapshot,
    digest,
    load_json,
    reject_zero_digests,
    safe_run_file,
    safe_summary_input,
)

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


def schema_errors(schema: dict[str, Any], document: dict[str, Any]) -> list[str]:
    return [f"{list(error.absolute_path)}: {error.message}" for error in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document)]


def reference_errors(run_dir: Path, document: dict[str, Any]) -> list[str]:
    failures = []
    for name in REFERENCES:
        reference = document[name]
        try:
            target = safe_run_file(run_dir, reference["evidence"])
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(f"unsafe or missing reference {name}: {exc}")
            continue
        if reference.get("digest") == ZERO_DIGEST or digest(target) != reference.get("digest"):
            failures.append(f"digest mismatch {name}")
        else:
            target_document = load_json(target)
            if target_document.get("result") != reference.get("result") or target_document.get("qualification") != reference.get("qualification"):
                failures.append(f"result/qualification mismatch {name}")
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
    document = load_json(safe_summary_input(run_dir, args.summary))
    schema = load_json(repo / "contracts/evidence/analysis-module/v1/schema.json")
    failures = schema_errors(schema, document) + reference_errors(run_dir, document) + reject_zero_digests(document)
    if document.get("run_id") != run_dir.name:
        failures.append("summary run_id differs from immutable run directory")
    if document.get("result") != "HOLD" or document.get("qualification") != "NOT_QUALIFIED":
        failures.append("module result/qualification does not preserve qualification-only HOLD")
    manifest = load_json(repo / "analysis-py/requirements-traceability.json")
    required_commands = {item["command_id"] for item in manifest["execution_bindings"]}
    if not args.provisional:
        required_commands.add("evidence-integrity")
    if set(document.get("executed_gates", {})) != required_commands:
        failures.append("executed gate set differs from the exact manifest")
    revision, status_digest, working_tree_dirty, source_digest = current_snapshot(repo)
    if document.get("source_revision") != revision:
        failures.append("summary source revision differs from the current checkout")
    if document.get("working_tree_status_digest") != status_digest:
        failures.append("summary working-tree status digest differs from the current checkout")
    if document.get("working_tree_dirty") is not working_tree_dirty:
        failures.append("summary working-tree dirty flag differs from the current checkout")
    if document.get("source_tree_digest") != source_digest:
        failures.append("summary source-tree digest differs from the current checkout")
    command_schema = load_json(repo / "contracts/evidence/command/v1/schema.json")
    for command_id in required_commands:
        try:
            sidecar = load_json(safe_run_file(run_dir, f"{command_id}.command.json"))
        except ValueError as exc:
            failures.append(f"unsafe or missing command sidecar {command_id}: {exc}")
            continue
        failures.extend(f"command {command_id}: {error}" for error in schema_errors(command_schema, sidecar))
        if (
            sidecar.get("run_id") != run_dir.name
            or sidecar.get("command_id") != command_id
            or sidecar.get("source_tree_digest") != source_digest
            or sidecar.get("working_tree_status_digest") != status_digest
        ):
            failures.append(f"command snapshot binding mismatch {command_id}")
        try:
            log = safe_run_file(run_dir, sidecar["log"]["path"])
            if digest(log) != sidecar["log"]["sha256"]:
                failures.append(f"command log digest mismatch {command_id}")
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(f"unsafe command log {command_id}: {exc}")
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
        result_tampered = copy.deepcopy(document)
        result_tampered["performance"]["qualification"] = "QUALIFIED"
        if not reference_errors(run_dir, result_tampered):
            raise SystemExit("negative evidence qualification mutation was accepted")
        failed_gate = copy.deepcopy(document)
        failed_gate["executed_gates"]["release-blackbox"] = "FAIL"
        if not schema_errors(schema, failed_gate):
            raise SystemExit("negative gate mutation was accepted")
        escaped = copy.deepcopy(document)
        escaped["release_runtime"]["evidence"] = "../outside.json"
        if not reference_errors(run_dir, escaped):
            raise SystemExit("negative evidence path traversal was accepted")
        soak = load_json(safe_run_file(run_dir, document["formal_soak"]["evidence"]))
        soak_schema = load_json(repo / "contracts/evidence/analysis-soak/v1/schema.json")
        shortened = copy.deepcopy(soak)
        shortened["qualified_elapsed_ms"] = 3_599_999
        if not schema_errors(soak_schema, shortened):
            raise SystemExit("negative shortened formal soak was accepted")
    print("analysis module evidence validation: PASS")


if __name__ == "__main__":
    main()
