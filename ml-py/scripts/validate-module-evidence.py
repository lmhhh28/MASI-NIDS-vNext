#!/usr/bin/env python3
"""Re-derive Offline ML completion and run tamper/short-soak negative self-tests."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not an ordinary file: {path}")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def schema_errors(schema: dict[str, Any], document: dict[str, Any]) -> list[Any]:
    return list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document))


def semantic_validate(repo: Path, run_dir: Path, summary: dict[str, Any]) -> None:
    if summary.get("overall_module_complete") is not True or summary.get("result") != "PASS":
        raise ValueError("completion/result mismatch")
    for name, reference in cast(dict[str, dict[str, str]], summary["evidence_refs"]).items():
        path = run_dir / reference["path"]
        if digest(path) != reference["digest"] or load(path).get("result") != "PASS":
            raise ValueError(f"evidence reference mismatch: {name}")
    manifest = load(repo / "ml-py/requirements-traceability.json")
    expected = {str(item["command_id"]) for item in manifest["execution_bindings"] if item["required"]}
    if set(summary["executed_gates"]) != expected:
        raise ValueError("executed gate set mismatch")
    metadata = load(run_dir / "run-metadata.json")
    for command_id in expected:
        sidecar = load(run_dir / f"{command_id}.command.json")
        log = cast(dict[str, Any], sidecar["log"])
        if (
            sidecar.get("result") != "PASS"
            or sidecar.get("source_tree_digest") != summary["source_tree_digest"]
            or sidecar.get("working_tree_status_digest") != summary["working_tree_status_digest"]
            or digest(run_dir / log["path"]) != log["sha256"]
        ):
            raise ValueError(f"command evidence mismatch: {command_id}")
    if metadata["source_tree_digest"] != summary["source_tree_digest"]:
        raise ValueError("run metadata source mismatch")
    blackbox = load(run_dir / "blackbox.json")
    immutable = cast(dict[str, str], blackbox["immutable_identity"])
    if summary["artifact_digests"]["model"] != immutable["model_digest"]:
        raise ValueError("summary model digest mismatch")
    soak = load(run_dir / "soak.json")
    if (
        soak.get("mode") != "formal"
        or soak.get("configured_qualified_seconds") != 3600
        or soak.get("observed_qualified_elapsed_ms", 0) < 3600000
    ):
        raise ValueError("formal soak incomplete")
    current_source = (
        "sha256:"
        + subprocess.run(
            [
                str(repo / "ml-py/.venv/bin/python"),
                str(repo / "ml-py/scripts/source-tree-digest.py"),
                "--repo",
                str(repo),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
    )
    if current_source != summary["source_tree_digest"]:
        raise ValueError("current source digest drift")


def must_reject(callable_: Any, label: str) -> None:
    try:
        callable_()
    except (ValueError, KeyError):
        return
    raise ValueError(f"negative self-test was accepted: {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    run_dir = arguments.run_dir.resolve(strict=True)
    summary = load(arguments.summary)
    summary_schema = load(repo / "contracts/evidence/offline-ml-module/v1/schema.json")
    errors = schema_errors(summary_schema, summary)
    if errors:
        raise SystemExit("summary schema rejected: " + "; ".join(error.message for error in errors))
    semantic_validate(repo, run_dir, summary)

    completion_negative = copy.deepcopy(summary)
    completion_negative["overall_module_complete"] = False
    if not schema_errors(summary_schema, completion_negative):
        raise SystemExit("completion=false negative was accepted")
    gate_negative = copy.deepcopy(summary)
    gate_negative["executed_gates"][next(iter(gate_negative["executed_gates"]))] = "FAIL"
    if not schema_errors(summary_schema, gate_negative):
        raise SystemExit("gate FAIL negative was accepted")
    ref_negative = copy.deepcopy(summary)
    ref_negative["evidence_refs"]["blackbox"]["digest"] = "sha256:" + "0" * 64
    must_reject(lambda: semantic_validate(repo, run_dir, ref_negative), "evidence digest")
    model_negative = copy.deepcopy(summary)
    model_negative["artifact_digests"]["model"] = "sha256:" + "0" * 64
    must_reject(lambda: semantic_validate(repo, run_dir, model_negative), "model identity")
    soak = load(run_dir / "soak.json")
    soak_schema = load(repo / "contracts/evidence/offline-ml-soak/v1/schema.json")
    short_soak = copy.deepcopy(soak)
    short_soak["configured_qualified_seconds"] = 3599
    short_soak["observed_qualified_elapsed_ms"] = 3599000
    if not schema_errors(soak_schema, short_soak):
        raise SystemExit("shortened soak negative was accepted")
    print(
        json.dumps(
            {
                "schema_version": "offline-ml-module-evidence-validation/v1",
                "result": "PASS",
                "negative_self_tests": 5,
                "overall_module_complete": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
