#!/usr/bin/env python3
"""Run the installed release pipeline twice and retain one verified immutable candidate output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return "sha256:" + value.hexdigest()


def remove_tree(path: Path) -> None:
    for root, directories, files in os.walk(path, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            target = root_path / name
            if not target.is_symlink():
                os.chmod(target, 0o600)
            target.unlink()
        for name in directories:
            target = root_path / name
            if target.is_symlink():
                target.unlink()
            else:
                os.chmod(target, 0o700)
                target.rmdir()
    os.chmod(path, 0o700)
    path.rmdir()


def execute(entrypoint: Path, repo: Path, output: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run = subprocess.run(
        [str(entrypoint), "run", "--repo", str(repo), "--output", str(output)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    verify = subprocess.run(
        [str(entrypoint), "verify", "--repo", str(repo), "--output", str(output)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return (
        cast(dict[str, Any], json.loads(run.stdout.strip().splitlines()[-1])),
        cast(dict[str, Any], json.loads(verify.stdout.strip().splitlines()[-1])),
    )


def immutable_identity(result: dict[str, Any]) -> dict[str, str]:
    return {
        "dataset_revision": str(result["dataset_revision"]),
        "model_digest": str(cast(dict[str, Any], result["bundle"])["model_digest"]),
        "model_revision_digest": str(cast(dict[str, Any], result["bundle"])["model_revision_digest"]),
        "repository_closure_digest": str(cast(dict[str, Any], result["bundle"])["repository_closure_digest"]),
        "archive_digest": str(cast(dict[str, Any], result["archive"])["sha256"]),
    }


def candidate_evidence(output: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for manifest_path in sorted(output.glob("candidates/*/seed-*/candidate-manifest.json")):
        manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
        explanation = cast(
            dict[str, Any], json.loads((manifest_path.parent / "explanation.json").read_text(encoding="utf-8"))
        )
        safety = cast(dict[str, bool], explanation["safety"])
        onnx = cast(
            dict[str, Any], json.loads((manifest_path.parent / "onnx-evidence.json").read_text(encoding="utf-8"))
        )
        rows.append(
            {
                "candidate_id": manifest["candidate_id"],
                "seed": manifest["seed"],
                "eligible": manifest["eligible"],
                "model_digest": onnx["model_digest"],
                "numeric": onnx["numeric"],
                "providers": onnx["providers"],
                "zipmap": onnx["zipmap"],
                "custom_operators": onnx["custom_operators"],
                "explanation_stability": cast(dict[str, Any], explanation["stability"])["status"],
                "explanation_safe": all(value is False for value in safety.values()),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--entrypoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    entrypoint = arguments.entrypoint.resolve(strict=True)
    for path in (arguments.output, arguments.evidence):
        if path.exists() or path.is_symlink():
            raise SystemExit(f"output already exists: {path}")
    temporary = Path(tempfile.mkdtemp(prefix="masi-offline-ml-blackbox."))
    try:
        first_result, first_verification = execute(entrypoint, repo, arguments.output)
        second_output = temporary / "second"
        second_result, second_verification = execute(entrypoint, repo, second_output)
        first_identity = immutable_identity(first_result)
        second_identity = immutable_identity(second_result)
        if first_identity != second_identity:
            raise RuntimeError("two installed-release runs produced different immutable identities")
        rows = candidate_evidence(arguments.output)
        second_rows = candidate_evidence(second_output)
        if len(rows) != 9 or len(second_rows) != 9:
            raise RuntimeError("mandatory candidate execution count mismatch")
        first_models = {(row["candidate_id"], row["seed"]): row["model_digest"] for row in rows}
        second_models = {(row["candidate_id"], row["seed"]): row["model_digest"] for row in second_rows}
        if first_models != second_models:
            raise RuntimeError("candidate model digest drift")
        if not all(
            row["eligible"]
            and row["providers"] == ["CPUExecutionProvider"]
            and row["zipmap"] is False
            and row["custom_operators"] is False
            and row["explanation_safe"]
            for row in rows
        ):
            raise RuntimeError("candidate qualification invariant failed")
        evidence = {
            "schema_version": "offline-ml-blackbox-evidence/v1",
            "module_id": "MOD-ML-001",
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "entrypoint": str(entrypoint),
            "entrypoint_digest": digest(entrypoint),
            "real_release_process": True,
            "real_training_export_packaging": True,
            "pipeline_runs": 2,
            "candidate_executions_per_run": 9,
            "candidate_recipes": ["lr-window-binary/v1", "xgb-window-binary/v1", "ae-window-benign/v1"],
            "seeds": [17, 29, 43],
            "immutable_identity": first_identity,
            "candidate_evidence": rows,
            "first_verification": first_verification,
            "second_verification": second_verification,
            "winner": first_result["winner"],
            "winner_seed": first_result["winner_seed"],
            "ensemble": False,
            "fallback": False,
            "cpu": {"applicability": "APPLICABLE", "result": "PASS"},
            "cuda": {
                "applicability": "NOT_APPLICABLE",
                "result": "NOT_RUN",
                "stable_reason": "EXACT_BUNDLE_DOES_NOT_DECLARE_CUDA_RUNTIME",
            },
            "official_corpus": {
                "applicability": "NOT_APPLICABLE",
                "result": "NOT_RUN",
                "stable_reason": "MODULE_OPERATIONAL_FIXTURE_DOES_NOT_CLAIM_PRODUCTION_DATA_QUALITY",
            },
        }
        arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
        arguments.evidence.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, sort_keys=True))
        remove_tree(second_output)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
