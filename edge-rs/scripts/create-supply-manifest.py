#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


MAX_JSON_BYTES = 67_108_864
MAX_MANIFEST_BYTES = 8_388_608
MAX_BUNDLE_FILES = 32_768
MAX_BUNDLE_TOTAL_BYTES = 8_589_934_592
MAX_BUNDLE_FILE_BYTES = 4_294_967_296
MAX_PATH_BYTES = 1_024


def absolute_path_chain_has_symlink(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def load(path: Path, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    if (
        absolute_path_chain_has_symlink(path)
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > max_bytes
    ):
        raise ValueError(f"{path} is missing, symbolic, or oversized")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def sha256(path: Path) -> str:
    if absolute_path_chain_has_symlink(path):
        raise ValueError(f"{path} has a symbolic path component")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError("the qualified Linux profile requires O_NOFOLLOW")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | nofollow)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1:
            raise ValueError(f"{path} is not a nonempty regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(descriptor)
        lexical_after = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(lexical_after.st_mode)
            or stat_identity(before) != stat_identity(after)
            or stat_identity(before) != stat_identity(lexical_after)
            or absolute_path_chain_has_symlink(path)
        ):
            raise ValueError(f"{path} changed while it was being hashed")
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def subject(name: str, digest: str) -> dict[str, object]:
    return {"name": name, "digest": {"sha256": digest.removeprefix("sha256:")}}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--supply-dir", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-tree-digest", required=True)
    parser.add_argument("--image-manifest-digest", required=True)
    parser.add_argument("--image-archive-index-digest", required=True)
    parser.add_argument("--image-config-digest", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--builder-image", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument(
        "--working-tree-dirty", choices=("true", "false"), required=True
    )
    parser.add_argument("--working-tree-status-digest", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--offline-rebuild", type=Path, required=True)
    parser.add_argument("--trivy-cache", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    args = parser.parse_args()

    for label, path in (
        ("repository", args.repo),
        ("supply directory", args.supply_dir),
        ("bundle directory", args.bundle_dir),
        ("offline rebuild", args.offline_rebuild),
        ("Trivy cache", args.trivy_cache),
        ("provenance output", args.provenance_output),
        ("manifest output", args.manifest_output),
    ):
        if absolute_path_chain_has_symlink(path):
            raise ValueError(f"{label} contains a symbolic path component")

    repo = args.repo.resolve()
    supply = args.supply_dir.resolve()
    bundle = args.bundle_dir.resolve()
    registry = repo / "contracts/supply-chain/v1/rust-edge-agent-components.json"
    notices = (
        repo / "contracts/supply-chain/v1/rust-edge-agent-third-party-notices.json"
    )
    cargo_lock = repo / "edge-rs/Cargo.lock"
    dockerfile = repo / "edge-rs/Dockerfile"
    image_binary = supply / "masi-edge.image.bin"
    rebuilt_binary = supply / "masi-edge.offline-rebuild.bin"
    working_tree_status = supply / "working-tree-status.txt"
    tools_lock = repo / "deploy/supply-chain/tools.lock.json"
    tools = load(tools_lock)
    offline = load(args.offline_rebuild)
    checksum_file = bundle / "SHA256SUMS"
    if (
        checksum_file.is_symlink()
        or not checksum_file.is_file()
        or checksum_file.stat().st_size > MAX_MANIFEST_BYTES
    ):
        raise ValueError("offline bundle checksum inventory is missing or oversized")
    bundle_files = []
    bundle_paths: set[str] = set()
    bundle_total_bytes = 0
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            raise ValueError("offline bundle checksum inventory has a malformed line")
        digest, relative_path = fields
        normalized_path = relative_path.removeprefix("*").removeprefix("./")
        relative = Path(normalized_path)
        if (
            not normalized_path
            or len(normalized_path.encode()) > MAX_PATH_BYTES
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != normalized_path
            or normalized_path in bundle_paths
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError(
                f"unsafe or duplicate offline bundle entry {normalized_path!r}"
            )
        bundle_path = bundle / relative
        if bundle_path.is_symlink() or not bundle_path.is_file():
            raise ValueError(
                f"offline bundle entry is not a regular file: {normalized_path}"
            )
        size = bundle_path.stat().st_size
        if size < 1 or size > MAX_BUNDLE_FILE_BYTES:
            raise ValueError(
                f"offline bundle entry has invalid size: {normalized_path}"
            )
        bundle_paths.add(normalized_path)
        bundle_total_bytes += size
        bundle_files.append(
            {"path": normalized_path, "digest": f"sha256:{digest}", "bytes": size}
        )
    if (
        not bundle_files
        or len(bundle_files) > MAX_BUNDLE_FILES
        or bundle_total_bytes > MAX_BUNDLE_TOTAL_BYTES
    ):
        raise ValueError("offline bundle exceeds its frozen file or byte ceiling")
    actual_bundle_paths: set[str] = set()
    for path in bundle.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError(
                f"offline bundle contains a symbolic or special path: {path}"
            )
        if path.is_file() and path != checksum_file:
            actual_bundle_paths.add(path.relative_to(bundle).as_posix())
    if actual_bundle_paths != bundle_paths:
        raise ValueError("offline bundle inventory is not an exact filesystem closure")

    subjects = [
        subject("source-tree.tar", args.source_tree_digest),
        subject("masi-nids/rust-edge-agent-oci-manifest", args.image_manifest_digest),
        subject(
            "masi-nids/rust-edge-agent-docker-archive-index",
            args.image_archive_index_digest,
        ),
        subject("masi-nids/rust-edge-agent-oci-config", args.image_config_digest),
        subject("usr/local/bin/masi-edge", sha256(image_binary)),
        subject("offline-rebuild/masi-edge", sha256(rebuilt_binary)),
        subject("edge-rs/Cargo.lock", sha256(cargo_lock)),
        subject("edge-rs/Dockerfile", sha256(dockerfile)),
        subject("working-tree-status.txt", args.working_tree_status_digest),
    ]
    sboms = sorted(supply.glob("*.spdx.json"))
    scans = sorted(supply.glob("trivy-*.json"))
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://masi-nids.example/build/rust-edge-agent-module/v1",
                "externalParameters": {
                    "source_revision": args.source_revision,
                    "source_tree_digest": args.source_tree_digest,
                    "working_tree_dirty": args.working_tree_dirty == "true",
                    "working_tree_status_digest": args.working_tree_status_digest,
                    "platform": "linux/amd64",
                    "network": "none",
                    "cargo_locked": True,
                    "runtime_dockerfile": "edge-rs/Dockerfile",
                },
                "resolvedDependencies": [
                    {
                        "uri": "git+MASI-NIDS-vNext",
                        "digest": {"gitCommit": args.source_revision},
                    },
                    {
                        "uri": "edge-rs/Cargo.lock",
                        "digest": {
                            "sha256": sha256(cargo_lock).removeprefix("sha256:")
                        },
                    },
                    {
                        "uri": f"oci://{args.builder_image.split('@', 1)[0]}",
                        "digest": {
                            "sha256": args.builder_image.rsplit("@sha256:", 1)[1]
                        },
                    },
                    {
                        "uri": f"oci://{args.runtime_image.split('@', 1)[0]}",
                        "digest": {
                            "sha256": args.runtime_image.rsplit("@sha256:", 1)[1]
                        },
                    },
                ],
            },
            "runDetails": {
                "builder": {"id": "docker-local-owner-authorized-module-gate"},
                "metadata": {
                    "invocationId": args.run_id,
                    "startedOn": offline["started_at"],
                    "finishedOn": offline["finished_at"],
                    "reproducible": bool(offline["binary_digest_match"]),
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

    db_metadata = args.trivy_cache / "db/metadata.json"
    db = args.trivy_cache / "db/trivy.db"
    java_metadata = args.trivy_cache / "java-db/metadata.json"
    java_db = args.trivy_cache / "java-db/trivy-java.db"
    checks_metadata = args.trivy_cache / "policy/metadata.json"
    signing_config = repo / "deploy/supply-chain/cosign-offline-signing-config.json"
    public_key = repo / "contracts/trust/v1/cosign.pub"
    manifest = {
        "schema_version": "rust-edge-agent-supply-release/v1",
        "release_id": f"rust-edge-agent-supply-{args.run_id}",
        "created_at": utc_now(),
        "source_revision": args.source_revision,
        "source_tree_digest": args.source_tree_digest,
        "working_tree_dirty": args.working_tree_dirty == "true",
        "working_tree_status_digest": args.working_tree_status_digest,
        "image_ref": args.image_ref,
        "image_manifest_digest": args.image_manifest_digest,
        "image_archive_index_digest": args.image_archive_index_digest,
        "image_config_digest": args.image_config_digest,
        "base_images": {
            "builder": args.builder_image,
            "runtime": args.runtime_image,
        },
        "subjects": subjects,
        "inventory": {
            "component_registry": {
                "path": registry.relative_to(repo).as_posix(),
                "digest": sha256(registry),
            },
            "third_party_notices": {
                "path": notices.relative_to(repo).as_posix(),
                "digest": sha256(notices),
            },
            "cargo_lock": {
                "path": cargo_lock.relative_to(repo).as_posix(),
                "digest": sha256(cargo_lock),
            },
            "working_tree_status": {
                "path": working_tree_status.name,
                "digest": sha256(working_tree_status),
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
            "evidence_inputs": [
                {
                    "path": path.name,
                    "digest": sha256(path),
                    "bytes": path.stat().st_size,
                }
                for path in (
                    supply / "image-archive-manifest.json",
                    supply / "image-archive-config.json",
                    supply / "image-config.json",
                    supply / "oci-archive-inspection.json",
                    supply / "trivy-config.log",
                    working_tree_status,
                )
            ],
        },
        "offline_bundle": {
            "directory": bundle.name,
            "checksums_path": checksum_file.name,
            "checksums_digest": sha256(checksum_file),
            "files": bundle_files,
        },
        "provenance": {
            "path": args.provenance_output.name,
            "digest": sha256(args.provenance_output),
        },
        "offline_rebuild": {
            "path": args.offline_rebuild.name,
            "digest": sha256(args.offline_rebuild),
            "network": "none",
        },
        "trust": {
            "public_key": public_key.relative_to(repo).as_posix(),
            "public_key_digest": sha256(public_key),
            "signing_config": signing_config.relative_to(repo).as_posix(),
            "signing_config_digest": sha256(signing_config),
            "transparency_log": "disabled for Owner-authorized local bootstrap",
            "revocation_file": "contracts/trust/v1/revoked-keys.json",
        },
        "tools": {name: value for name, value in tools["tools"].items()},
        "trivy_checks_bundle": {
            **tools["trivy_checks_bundle"],
            "metadata": load(checks_metadata),
            "metadata_digest": sha256(checks_metadata),
        },
        "vulnerability_database": {
            "metadata": load(db_metadata),
            "metadata_digest": sha256(db_metadata),
            "database_digest": sha256(db),
            "database_bytes": db.stat().st_size,
        },
        "java_vulnerability_database": {
            "metadata": load(java_metadata),
            "metadata_digest": sha256(java_metadata),
            "database_digest": sha256(java_db),
            "database_bytes": java_db.stat().st_size,
        },
        "environment": {
            "docker": json.loads(
                subprocess.run(
                    ["docker", "version", "--format", "{{json .}}"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            ),
            "kernel": platform.release(),
            "machine": platform.machine(),
            "uid": os.getuid(),
        },
        "resource_limits": {
            "manifest_bytes": MAX_MANIFEST_BYTES,
            "json_input_bytes": MAX_JSON_BYTES,
            "offline_bundle_files": MAX_BUNDLE_FILES,
            "offline_bundle_total_bytes": MAX_BUNDLE_TOTAL_BYTES,
            "offline_bundle_file_bytes": MAX_BUNDLE_FILE_BYTES,
            "tar_members": 65_536,
            "tar_expanded_bytes": MAX_BUNDLE_TOTAL_BYTES,
            "checks_bundle_files": 4_096,
            "checks_bundle_total_bytes": MAX_JSON_BYTES,
            "path_bytes": MAX_PATH_BYTES,
        },
    }
    schema = load(
        repo / "contracts/supply-chain/v1/rust-edge-agent-release.schema.json"
    )
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            manifest
        ),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(
            "\n".join(
                f"release manifest schema error at {list(error.path)}: {error.message}"
                for error in errors
            )
        )
    encoded_manifest = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if len(encoded_manifest.encode()) > MAX_MANIFEST_BYTES:
        raise ValueError("release manifest exceeds its frozen byte ceiling")
    args.manifest_output.write_text(encoded_manifest, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
