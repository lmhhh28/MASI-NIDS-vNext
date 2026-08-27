#!/usr/bin/env python3
"""Validate the bounded Analysis compose asset and immutable production image form."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from masi_analysis.canonical import file_digest, go_json_bytes


def main() -> None:
    parser = argparse.ArgumentParser()
    repository = Path(__file__).resolve().parents[2]
    parser.add_argument("--repo", type=Path, default=repository)
    parser.add_argument("--evidence")
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    compose_path = repo / "deploy/analysis/compose.acceptance.yaml"
    if compose_path.is_symlink() or not compose_path.is_file():
        raise SystemExit("Analysis compose asset must be a regular no-symlink file")
    text = compose_path.read_text(encoding="utf-8")
    exact_image = 'image: "${MASI_ANALYSIS_IMAGE_REPOSITORY:?set immutable Analysis repository}@${MASI_ANALYSIS_IMAGE_DIGEST:?set sha256 digest}"'
    checks = {
        "immutable_repository_digest_template": exact_image in text,
        "non_root": 'user: "65532:65532"' in text,
        "read_only_rootfs": "read_only: true" in text,
        "cap_drop_all": 'cap_drop: ["ALL"]' in text,
        "no_new_privileges": "no-new-privileges:true" in text,
        "bounded_pids": bool(re.search(r"(?m)^\s+pids_limit:\s+64$", text)),
        "bounded_memory": bool(re.search(r"(?m)^\s+mem_limit:\s+384m$", text)),
        "bounded_cpu": bool(re.search(r"(?m)^\s+cpus:\s+1\.0$", text)),
        "private_state_tmpfs": "/var/lib/masi-analysis:rw,noexec,nosuid,size=96m,uid=65532,gid=65532,mode=0700" in text,
        "private_health_tmpfs": "/var/run/masi-analysis:rw,noexec,nosuid,size=4m,uid=65532,gid=65532,mode=0700" in text,
        "config_and_secrets_read_only": text.count("read_only: true") >= 3,
        "restricted_external_network": "external: true" in text and "MASI_ANALYSIS_NETWORK" in text,
        "bounded_stop": "stop_grace_period: 10s" in text,
        "no_privileged_authority": not any(
            value in text.lower() for value in ("privileged: true", "network_mode: host", "pid: host", "ipc: host", "docker.sock", "hostpath", "/dev/")
        ),
    }
    failures = sorted(key for key, value in checks.items() if not value)
    if failures:
        raise SystemExit("Analysis deployment policy failed: " + ", ".join(failures))
    evidence = {
        "schema_version": "analysis-deployment-evidence/v1",
        "module_id": "MOD-AGENT-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
        "compose_digest": file_digest(compose_path.read_bytes()),
        "production_image_reference_pattern": "repository@sha256:<64-lower-hex>",
        "checks": checks,
    }
    raw = go_json_bytes(evidence) + b"\n"
    if args.evidence:
        output = Path(args.evidence)
        if output.exists() or output.is_symlink():
            raise SystemExit("refusing to overwrite deployment evidence")
        output.write_bytes(raw)
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
