#!/usr/bin/env python3
"""Compute a canonical digest over Offline ML current-source inputs."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    roots = (
        repo / "ml-py",
        repo / "contracts/dataset",
        repo / "contracts/model-explanation",
        repo / "contracts/profiles/v1/dataset-p4-window-binary.json",
        repo / "contracts/profiles/v1/model-explanation-evidence.json",
        repo / "contracts/profiles/v1/edge-feature-window.json",
        repo / "contracts/profiles/v1/p4-stateless-firewall-bmv2.json",
        repo / "contracts/profiles/v1/offline-ml-runtime.json",
        repo / "contracts/profiles/v1/offline-ml-performance.json",
        repo / "contracts/profiles/v1/offline-ml-soak.json",
        repo / "contracts/inference/v1/profile.json",
        repo / "contracts/inference/v1/model-runtime-central-cpu.json",
        repo / "contracts/inference/v1/optimization-profile-central-cpu.json",
        repo / "contracts/golden/telemetry/snapshot-v1.json",
        repo / "p4/src/masi_switch.p4",
        repo / "contracts/supply-chain/v1/schema.json",
        repo / "contracts/supply-chain/v1/offline-ml-components.json",
        repo / "contracts/supply-chain/v1/offline-ml-third-party-notices.json",
        *tuple(sorted((repo / "contracts/evidence").glob("offline-ml-*/v1"))),
        repo / "contracts/evidence/command/v1/schema.json",
        repo / "contracts/evidence/module-findings/v1/schema.json",
        repo / "contracts/evidence/traceability/v1/schema.json",
        repo / "deploy/supply-chain/tools.lock.json",
        repo / "deploy/supply-chain/trivy-policy.json",
        repo / "deploy/supply-chain/cosign-offline-signing-config.json",
        repo / "deploy/offline-ml",
        repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md",
        repo / "docs/adr/0019-offline-ml-dataset-training-and-explanation-boundary.md",
        repo / "docs/design/modules/offline-ml-pipeline-design.md",
    )
    excluded_parts = {".venv", ".ruff_cache", "evidence", "build", "dist", "__pycache__"}
    paths: list[Path] = []
    for root in roots:
        if root.is_file():
            paths.append(root)
            continue
        for path in root.rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and not excluded_parts.intersection(path.relative_to(repo).parts)
            ):
                if path.suffix != ".pyc":
                    paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda value: value.relative_to(repo).as_posix().encode("utf-8")):
        relative = path.relative_to(repo).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    print(digest.hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
