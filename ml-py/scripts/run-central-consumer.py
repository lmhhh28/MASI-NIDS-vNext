#!/usr/bin/env python3
"""Validate producer output with the exact Central Gateway C++ closure implementation."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, cast


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validator", type=Path, required=True)
    parser.add_argument("--pipeline-output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    validator = arguments.validator.resolve(strict=True)
    output = arguments.pipeline_output.resolve(strict=True)
    repository = (output / "bundle/repository").resolve(strict=True)
    if arguments.evidence.exists() or arguments.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    bundle_result = cast(dict[str, Any], json.loads((output / "bundle/bundle-result.json").read_text(encoding="utf-8")))
    identity = str(bundle_result["repository_identity"])
    closure_digest = str(bundle_result["repository_closure_digest"])
    command = [str(validator), str(repository), identity, closure_digest]
    positive = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if positive.returncode != 0:
        raise RuntimeError(positive.stdout)
    result = cast(dict[str, Any], json.loads(positive.stdout.strip().splitlines()[-1]))
    if result.get("result") != "PASS" or result.get("model_digest") != bundle_result["model_digest"]:
        raise RuntimeError("Central consumer result mismatch")

    wrong_identity = subprocess.run(
        [str(validator), str(repository), identity + "-wrong", closure_digest],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    wrong_digest = subprocess.run(
        [str(validator), str(repository), identity, "sha256:" + "0" * 64],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if wrong_identity.returncode != 2 or wrong_digest.returncode != 2:
        raise RuntimeError("Central consumer negative fence was not enforced")
    evidence = {
        "schema_version": "offline-ml-central-consumer-evidence/v1",
        "module_id": "MOD-ML-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
        "validator": str(validator),
        "validator_digest": sha256(validator),
        "consumer_result": result,
        "identity_negative": {"exit_code": wrong_identity.returncode, "result": "PASS"},
        "closure_digest_negative": {"exit_code": wrong_digest.returncode, "result": "PASS"},
        "producer_consumer_model_digest_match": True,
        "producer_consumer_closure_digest_match": True,
    }
    arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
    arguments.evidence.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
