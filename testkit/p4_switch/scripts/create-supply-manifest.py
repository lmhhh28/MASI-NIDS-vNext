from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path


EXCLUDED_TOP_LEVEL = {".git", ".cache", ".masi-secrets", "evidence", "out"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    paths = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        paths.append(path)
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def docker_version() -> dict[str, str]:
    payload = json.loads(
        subprocess.run(
            ["docker", "version", "--format", "{{json .}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return {
        "engine": payload["Server"]["Version"],
        "api": payload["Server"]["ApiVersion"],
        "client": payload["Client"]["Version"],
    }


def file_subject(name: str, path: Path) -> dict[str, object]:
    return {"name": name, "digest": {"sha256": sha256(path).removeprefix("sha256:")}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--supply-dir", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--pi-source-archive", type=Path, required=True)
    parser.add_argument("--runtime-digest", required=True)
    parser.add_argument("--runner-digest", required=True)
    parser.add_argument("--compiler-digest", required=True)
    parser.add_argument("--runner-deps-digest", required=True)
    parser.add_argument("--offline-result", type=Path, required=True)
    parser.add_argument("--trivy-cache", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    args = parser.parse_args()
    source_lock = json.loads(
        (args.repo / "deploy/p4-switch/bmv2/source.lock.json").read_text(
            encoding="utf-8"
        )
    )
    pi_source_lock = json.loads(
        (args.repo / "deploy/p4-switch/pi/source.lock.json").read_text(
            encoding="utf-8"
        )
    )
    offline = json.loads(args.offline_result.read_text(encoding="utf-8"))
    db_metadata_path = args.trivy_cache / "db/metadata.json"
    db_path = args.trivy_cache / "db/trivy.db"
    java_db_metadata_path = args.trivy_cache / "java-db/metadata.json"
    java_db_path = args.trivy_cache / "java-db/trivy-java.db"
    subjects = [
        {
            "name": "masi-nids/p4-switch-runtime",
            "digest": {"sha256": args.runtime_digest.removeprefix("sha256:")},
        },
        {
            "name": "masi-nids/p4-switch-e2e-runner",
            "digest": {"sha256": args.runner_digest.removeprefix("sha256:")},
        },
        {
            "name": "masi-nids/p4-switch-p4c",
            "digest": {"sha256": args.compiler_digest.removeprefix("sha256:")},
        },
        {
            "name": "masi-nids/p4-switch-runner-deps",
            "digest": {"sha256": args.runner_deps_digest.removeprefix("sha256:")},
        },
        file_subject("p4/src/masi_switch.p4", args.repo / "p4/src/masi_switch.p4"),
        file_subject("masi_switch.json", args.artifacts / "masi_switch.json"),
        file_subject(
            "masi_switch.p4info.txtpb",
            args.artifacts / "masi_switch.p4info.txtpb",
        ),
        file_subject(args.source_archive.name, args.source_archive),
        file_subject(args.pi_source_archive.name, args.pi_source_archive),
    ]
    sboms = sorted(args.supply_dir.glob("*.spdx.json"))
    scans = sorted(args.supply_dir.glob("trivy-*.json"))
    resolved_dependencies = [
        {
            "uri": source_lock["upstream"],
            "digest": {"gitCommit": source_lock["source_commit"]},
        },
        {
            "uri": source_lock["source_archive"]["filename"],
            "digest": {"sha256": source_lock["source_archive"]["sha256"]},
        },
        {
            "uri": source_lock["patch"]["path"],
            "digest": {"sha256": source_lock["patch"]["sha256"]},
        },
        {
            "uri": pi_source_lock["upstream"],
            "digest": {"gitCommit": pi_source_lock["source_commit"]},
        },
        {
            "uri": pi_source_lock["source_archive"]["filename"],
            "digest": {"sha256": pi_source_lock["source_archive"]["sha256"]},
        },
        {
            "uri": pi_source_lock["patch"]["path"],
            "digest": {"sha256": pi_source_lock["patch"]["sha256"]},
        },
        *[
            {
                "uri": f'{component["upstream"]}#submodule={submodule_path}',
                "digest": {"gitCommit": component["commit"]},
            }
            for submodule_path, component in sorted(
                pi_source_lock["submodules"].items()
            )
        ],
        {
            "uri": "oci://" + source_lock["build_base"].split("@", 1)[0],
            "digest": {
                "sha256": source_lock["build_base"].split("sha256:", 1)[1]
            },
        },
    ]
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://masi-nids.example/build/p4-switch-module/v1",
                "externalParameters": {
                    "source_date_epoch": source_lock["source_date_epoch"],
                    "platform": source_lock["platform"],
                    "network": "none",
                    "runtime_dockerfile": "deploy/p4-switch/bmv2/Dockerfile.runtime",
                    "runner_dockerfile": "deploy/p4-switch/Dockerfile.runner",
                    "pi_generated_build_system": pi_source_lock[
                        "generated_build_system"
                    ],
                },
                "resolvedDependencies": resolved_dependencies,
            },
            "runDetails": {
                "builder": {"id": "docker-buildx/local-owner-authorized-bootstrap"},
                "metadata": {
                    "invocationId": args.run_id,
                    "startedOn": offline["started_at"],
                    "finishedOn": offline["finished_at"],
                    "reproducible": bool(
                        offline.get("runtime_digest_match")
                        and offline.get("runner_digest_match")
                    ),
                },
                "byproducts": [
                    {"name": path.name, "digest": sha256(path)}
                    for path in sboms + scans
                ],
            },
        },
    }
    args.provenance_output.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    public_key = args.repo / "contracts/trust/v1/cosign.pub"
    signing_config = (
        args.repo / "deploy/supply-chain/cosign-offline-signing-config.json"
    )
    registry = args.repo / "contracts/supply-chain/v1/p4-switch-components.json"
    notices = args.repo / "contracts/supply-chain/v1/third-party-notices.json"
    manifest = {
        "schema_version": "p4-switch-supply-release/v1",
        "release_id": f"p4-switch-supply-{args.run_id}",
        "created_at": utc_now(),
        "subjects": subjects,
        "source_tree_digest": tree_digest(args.repo),
        "inventory": {
            "component_registry": {
                "path": registry.relative_to(args.repo).as_posix(),
                "digest": sha256(registry),
            },
            "third_party_notices": {
                "path": notices.relative_to(args.repo).as_posix(),
                "digest": sha256(notices),
            },
            "sboms": [
                {
                    "path": path.name,
                    "digest": sha256(path),
                    "bytes": path.stat().st_size,
                }
                for path in sboms
            ],
            "scans": [
                {
                    "path": path.name,
                    "digest": sha256(path),
                    "bytes": path.stat().st_size,
                }
                for path in scans
            ],
        },
        "provenance": {
            "path": args.provenance_output.name,
            "digest": sha256(args.provenance_output),
        },
        "trust": {
            "public_key": public_key.relative_to(args.repo).as_posix(),
            "public_key_digest": sha256(public_key),
            "signing_config": signing_config.relative_to(args.repo).as_posix(),
            "signing_config_digest": sha256(signing_config),
            "transparency_log": "disabled for Owner-authorized local bootstrap",
            "revocation_file": "contracts/trust/v1/revoked-keys.json",
        },
        "vulnerability_database": {
            "metadata": json.loads(db_metadata_path.read_text(encoding="utf-8")),
            "metadata_digest": sha256(db_metadata_path),
            "database_digest": sha256(db_path),
            "database_bytes": db_path.stat().st_size,
        },
        "java_vulnerability_database": {
            "metadata": json.loads(java_db_metadata_path.read_text(encoding="utf-8")),
            "metadata_digest": sha256(java_db_metadata_path),
            "database_digest": sha256(java_db_path),
            "database_bytes": java_db_path.stat().st_size,
        },
        "offline_rebuild": {
            "path": args.offline_result.name,
            "digest": sha256(args.offline_result),
            "network": "none",
        },
        "environment": {
            "docker": docker_version(),
            "kernel": platform.release(),
            "machine": platform.machine(),
            "uid": os.getuid(),
        },
    }
    args.manifest_output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
