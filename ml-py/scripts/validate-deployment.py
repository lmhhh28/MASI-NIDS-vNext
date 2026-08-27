#!/usr/bin/env python3
"""Validate the production-style Offline ML terminating-job Compose policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    compose = repo / "deploy/offline-ml/compose.acceptance.yaml"
    if arguments.output.exists() or arguments.output.is_symlink():
        raise SystemExit("output already exists")
    with tempfile.TemporaryDirectory() as temporary:
        environment = dict(os.environ)
        environment["MASI_OFFLINE_ML_IMAGE"] = "registry.example/masi-offline-ml@sha256:" + "a" * 64
        environment["MASI_OFFLINE_ML_OUTPUT"] = temporary
        rendered = subprocess.run(
            ["docker", "compose", "-f", str(compose), "config", "--format", "json"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
        )
    config = cast(dict[str, Any], json.loads(rendered.stdout))
    service = cast(dict[str, Any], cast(dict[str, Any], config["services"])["offline-ml"])
    volumes = cast(list[dict[str, Any]], service["volumes"])
    checks = {
        "immutable_image": "@sha256:" in str(service["image"]),
        "non_root": service["user"] == "65532:65532",
        "read_only_rootfs": service["read_only"] is True,
        "network_none": service["network_mode"] == "none",
        "restart_disabled": service["restart"] == "no",
        "init_enabled": service["init"] is True,
        "cap_drop_all": service["cap_drop"] == ["ALL"],
        "no_new_privileges": service["security_opt"] == ["no-new-privileges:true"],
        "pids_bounded": service["pids_limit"] == 128,
        "memory_bounded": int(service["mem_limit"]) == 2147483648,
        "cpu_bounded": int(service["cpus"]) == 2,
        "tmpfs_bounded": len(service["tmpfs"]) == 1 and "/tmp" in service["tmpfs"][0],
        "single_output_mount": len(volumes) == 1 and volumes[0]["target"] == "/output",
        "no_privileged_host_authority": not any(key in service for key in ("privileged", "pid", "ipc", "devices")),
        "terminating_run_command": service["command"]
        == ["run", "--repo", "/opt/masi/repo", "--output", "/output/candidate"],
    }
    evidence = {
        "schema_version": "offline-ml-deployment-evidence/v1",
        "module_id": "MOD-ML-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS" if all(checks.values()) else "FAIL",
        "qualification": "NOT_QUALIFIED",
        "compose_digest": "sha256:" + hashlib.sha256(compose.read_bytes()).hexdigest(),
        "checks": checks,
        "health_endpoints": {
            "applicability": "NOT_APPLICABLE",
            "result": "NOT_RUN",
            "stable_reason": "BOUNDED_OFFLINE_JOB_EXITS_AFTER_ATOMIC_ARTIFACT_PUBLICATION",
        },
    }
    if evidence["result"] == "PASS":
        schema = json.loads(
            (repo / "contracts/evidence/offline-ml-deployment/v1/schema.json").read_text(encoding="utf-8")
        )
        errors = list(Draft202012Validator(schema).iter_errors(evidence))
        if errors:
            raise SystemExit("deployment evidence schema rejected: " + "; ".join(error.message for error in errors))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
