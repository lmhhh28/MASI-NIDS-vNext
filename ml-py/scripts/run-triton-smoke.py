#!/usr/bin/env python3
"""Load an Offline ML repository in the pinned real Triton CPU execution plane."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, cast

TRITON_IMAGE = "nvcr.io/nvidia/tritonserver@sha256:75bcfa5b0043898ece3e603c17a5bbbb1c9bddc390563db24312ef59d83735e5"


def command(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline-output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--triton-image", default=TRITON_IMAGE)
    arguments = parser.parse_args()
    pipeline_output = arguments.pipeline_output.resolve(strict=True)
    repository = (pipeline_output / "bundle/repository").resolve(strict=True)
    if arguments.evidence.exists() or arguments.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    pipeline_result = cast(
        dict[str, Any], json.loads((pipeline_output / "pipeline-result.json").read_text(encoding="utf-8"))
    )
    name = f"masi-offline-ml-triton-{uuid.uuid4().hex[:12]}"
    started = time.monotonic_ns()
    try:
        container_id = command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=256m",
                "--shm-size",
                "256m",
                "--mount",
                f"type=bind,src={repository},dst=/models,readonly",
                arguments.triton_image,
                "tritonserver",
                "--model-repository=/models",
                "--model-control-mode=none",
                "--disable-auto-complete-config",
                "--strict-readiness=true",
            ]
        ).stdout.strip()
        ready = False
        for _ in range(300):
            probe = command(
                ["docker", "exec", name, "curl", "--fail", "--silent", "http://127.0.0.1:8000/v2/health/ready"],
                check=False,
            )
            if probe.returncode == 0:
                ready = True
                break
            status = command(["docker", "inspect", "--format", "{{.State.Running}}", name], check=False)
            if status.stdout.strip() != "true":
                break
            time.sleep(0.1)
        if not ready:
            raise RuntimeError(command(["docker", "logs", name], check=False).stdout)

        metadata = json.loads(
            command(
                [
                    "docker",
                    "exec",
                    name,
                    "curl",
                    "--fail",
                    "--silent",
                    "http://127.0.0.1:8000/v2/models/masi-ids-window-v1",
                ]
            ).stdout
        )
        config = json.loads(
            command(
                [
                    "docker",
                    "exec",
                    name,
                    "curl",
                    "--fail",
                    "--silent",
                    "http://127.0.0.1:8000/v2/models/masi-ids-window-v1/config",
                ]
            ).stdout
        )
        vectors = [
            [50, 4000, 10, 3, 250, 10],
            [10000, 1000000, 50, 2000, 200000, 10],
            [2, 108, 1, 2, 108, 1],
        ]
        payload = {
            "inputs": [{"name": "features", "shape": [3, 6], "datatype": "UINT64", "data": vectors}],
            "outputs": [{"name": "scores"}],
        }
        response = json.loads(
            command(
                [
                    "docker",
                    "exec",
                    name,
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    json.dumps(payload, separators=(",", ":")),
                    "http://127.0.0.1:8000/v2/models/masi-ids-window-v1/infer",
                ]
            ).stdout
        )
        output = cast(dict[str, Any], response["outputs"][0])
        scores = [float(value) for value in cast(list[float], output["data"])]
        if output["datatype"] != "FP32" or output["shape"] != [3, 2] or len(scores) != 6:
            raise RuntimeError(f"Triton output contract mismatch: {output}")
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in scores):
            raise RuntimeError("Triton returned invalid probability")
        if any(abs(scores[index] + scores[index + 1] - 1.0) > 2e-6 for index in range(0, 6, 2)):
            raise RuntimeError("Triton class probabilities do not sum to one")
        logs = command(["docker", "logs", name]).stdout
        if "successfully loaded 'masi-ids-window-v1'" not in logs or "| masi-ids-window-v1 | 1" not in logs:
            raise RuntimeError("Triton readiness log binding missing")
        image_metadata = cast(
            list[dict[str, Any]], json.loads(command(["docker", "image", "inspect", arguments.triton_image]).stdout)
        )[0]
        elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
        evidence = {
            "schema_version": "offline-ml-triton-smoke-evidence/v1",
            "module_id": "MOD-ML-001",
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "triton_image": arguments.triton_image,
            "triton_image_id": image_metadata["Id"],
            "triton_server_version": "2.59.0",
            "model_control_mode": "none",
            "strict_readiness": True,
            "repository_read_only": True,
            "network_external": False,
            "container_id": container_id,
            "startup_elapsed_ms": elapsed_ms,
            "model_metadata": metadata,
            "model_config": config,
            "inference": response,
            "numeric": {"records": 3, "finite": True, "probability_sum_tolerance": 0.000002},
            "model_digest": cast(dict[str, Any], pipeline_result["bundle"])["model_digest"],
            "repository_closure_digest": cast(dict[str, Any], pipeline_result["bundle"])["repository_closure_digest"],
            "logs_digest": "sha256:" + hashlib.sha256(logs.encode()).hexdigest(),
        }
        arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
        arguments.evidence.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, sort_keys=True))
    finally:
        command(["docker", "rm", "-f", name], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
