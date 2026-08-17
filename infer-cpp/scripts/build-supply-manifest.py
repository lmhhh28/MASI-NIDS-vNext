#!/usr/bin/env python3
"""Build the bounded Central Inference CPU release/provenance manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


MAX_JSON_BYTES = 67_108_864
MAX_MANIFEST_BYTES = 8_388_608
MAX_BUNDLE_FILES = 32_768
MAX_BUNDLE_TOTAL_BYTES = 8_589_934_592
MAX_BUNDLE_FILE_BYTES = 4_294_967_296
MAX_PATH_BYTES = 1_024
MAX_TAR_MEMBERS = 65_536


def load(path: Path, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError(f"unsafe, missing, or oversized JSON file: {path}")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON file is not an object: {path}")
    return value


def sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"digest input is not a regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"digest input is not regular: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            for chunk in iter(lambda: source.read(1_048_576), b""):
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"digest input changed while being read: {path}")
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(
        value
        and len(value.encode()) <= MAX_PATH_BYTES
        and not path.is_absolute()
        and ".." not in path.parts
        and "." not in path.parts
        and path.as_posix() == value
        and "//" not in value
    )


def sized(path: Path) -> dict[str, object]:
    size = path.stat().st_size
    if size < 1 or size > MAX_JSON_BYTES:
        raise ValueError(f"evidence input is empty or oversized: {path}")
    return {"path": path.name, "digest": sha256(path), "bytes": size}


def subject(name: str, digest: str) -> dict[str, object]:
    return {"name": name, "digest": {"sha256": digest.removeprefix("sha256:")}}


def inspect_archive(archive_path: Path, supply: Path) -> tuple[str, str]:
    seen: set[str] = set()
    total = 0
    manifest_bytes = b""
    config_members: dict[str, bytes] = {}
    with tarfile.open(archive_path, "r") as archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_TAR_MEMBERS:
            raise ValueError("image archive member count is outside the frozen bound")
        for member in members:
            name = PurePosixPath(member.name).as_posix().removeprefix("./")
            if (
                not safe_relative(name)
                or name in seen
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"unsafe image archive member: {member.name!r}")
            seen.add(name)
            total += member.size
            if total > MAX_BUNDLE_TOTAL_BYTES or member.size > MAX_BUNDLE_FILE_BYTES:
                raise ValueError("image archive exceeds the frozen expansion bound")
            if not member.isfile():
                continue
            if name == "manifest.json" or re.fullmatch(
                r"(?:blobs/sha256/)?[0-9a-f]{64}(?:\.json)?", name
            ):
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"cannot read image archive member: {name}")
                payload = extracted.read(MAX_JSON_BYTES + 1)
                if len(payload) > MAX_JSON_BYTES:
                    raise ValueError(f"image archive JSON member is oversized: {name}")
                if name == "manifest.json":
                    manifest_bytes = payload
                else:
                    config_members[name] = payload
    manifest_value = json.loads(manifest_bytes)
    if not isinstance(manifest_value, list) or len(manifest_value) != 1:
        raise ValueError("image archive must contain exactly one manifest entry")
    entry = manifest_value[0]
    if not isinstance(entry, dict) or not isinstance(entry.get("Config"), str):
        raise ValueError("image archive manifest has no config member")
    config_name = PurePosixPath(entry["Config"]).as_posix()
    if not safe_relative(config_name) or config_name not in config_members:
        raise ValueError("image archive config path is unsafe or absent")
    config_bytes = config_members[config_name]
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    filename_digest = PurePosixPath(config_name).name.removesuffix(".json")
    if config_digest != "sha256:" + filename_digest:
        raise ValueError("image archive config filename does not match its bytes")
    (supply / "image-archive-manifest.json").write_bytes(manifest_bytes)
    (supply / "image-archive-config.json").write_bytes(config_bytes)
    return "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(), config_digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--supply-dir", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-tree-digest", required=True)
    parser.add_argument("--working-tree-dirty", choices=("true", "false"), required=True)
    parser.add_argument("--working-tree-status-digest", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--image-manifest-digest", required=True)
    parser.add_argument("--image-config-digest", required=True)
    parser.add_argument("--builder-image", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--trivy-cache", type=Path, required=True)
    parser.add_argument("--offline-rebuild", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    supply = args.supply_dir.resolve()
    bundle = args.bundle_dir.resolve()
    image_archive = bundle / "inference-image.tar"
    archive_index_digest, observed_config_digest = inspect_archive(image_archive, supply)
    if observed_config_digest != args.image_config_digest:
        raise ValueError("image archive config digest disagrees with docker image identity")

    checksum_path = bundle / "SHA256SUMS"
    bundle_entries: list[dict[str, object]] = []
    bundle_total = 0
    inventory: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if match is None:
            raise ValueError("offline bundle SHA256SUMS contains a malformed line")
        relative = match.group(2).removeprefix("./")
        if not safe_relative(relative) or relative in inventory:
            raise ValueError(f"unsafe or duplicate offline bundle path: {relative!r}")
        path = bundle / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"offline bundle path is not regular: {relative}")
        size = path.stat().st_size
        if size < 1 or size > MAX_BUNDLE_FILE_BYTES:
            raise ValueError(f"offline bundle member is empty or oversized: {relative}")
        digest = "sha256:" + match.group(1)
        if sha256(path) != digest:
            raise ValueError(f"offline bundle digest mismatch: {relative}")
        inventory[relative] = digest
        bundle_total += size
        bundle_entries.append({"path": relative, "digest": digest, "bytes": size})
    actual = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path != checksum_path
    }
    if (
        not bundle_entries
        or len(bundle_entries) > MAX_BUNDLE_FILES
        or bundle_total > MAX_BUNDLE_TOTAL_BYTES
        or actual != set(inventory)
    ):
        raise ValueError("offline bundle is not an exact bounded filesystem closure")

    registry = repo / "contracts/supply-chain/v1/central-inference-cpu-components.json"
    notices = repo / "contracts/supply-chain/v1/central-inference-cpu-third-party-notices.json"
    vendored = repo / "contracts/supply-chain/v1/central-inference-vendored-sources.json"
    cmake_presets = repo / "infer-cpp/CMakePresets.json"
    dockerfile = repo / "infer-cpp/Dockerfile"
    cmake_lists = repo / "infer-cpp/CMakeLists.txt"
    working_status = supply / "working-tree-status.txt"
    image_binary = supply / "masi_inference_gateway.image.bin"
    rebuilt_binary = supply / "masi_inference_gateway.offline-rebuild.bin"
    tools_lock = repo / "deploy/supply-chain/tools.lock.json"
    tools = load(tools_lock)
    offline = load(args.offline_rebuild)
    sboms = sorted(supply.glob("*.spdx.json"))
    scans = sorted(supply.glob("trivy-*.json"))
    evidence_inputs = [
        supply / "image-archive-manifest.json",
        supply / "image-archive-config.json",
        supply / "image-config.json",
        supply / "offline-rebuild.json",
        supply / "input-safety.json",
        supply / "toolchain-binding.json",
        supply / "trivy-config.log",
        working_status,
    ]
    if len(sboms) < 2 or len(scans) < 3 or any(not path.is_file() for path in evidence_inputs):
        raise ValueError("required SBOM, scan, or evidence inputs are missing")

    subjects = [
        subject("source-tree.tar", args.source_tree_digest),
        subject("masi-nids/central-inference-cpu-oci-manifest", args.image_manifest_digest),
        subject("masi-nids/central-inference-cpu-docker-archive-index", archive_index_digest),
        subject("masi-nids/central-inference-cpu-oci-config", args.image_config_digest),
        subject("usr/local/bin/masi_inference_gateway", sha256(image_binary)),
        subject("offline-rebuild/masi_inference_gateway", sha256(rebuilt_binary)),
        subject("infer-cpp/CMakeLists.txt", sha256(cmake_lists)),
        subject("infer-cpp/Dockerfile", sha256(dockerfile)),
        subject("working-tree-status.txt", args.working_tree_status_digest),
    ]
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://masi-nids.example/build/central-inference-cpu-module/v1",
                "externalParameters": {
                    "source_revision": args.source_revision,
                    "source_tree_digest": args.source_tree_digest,
                    "working_tree_dirty": args.working_tree_dirty == "true",
                    "working_tree_status_digest": args.working_tree_status_digest,
                    "network": "none",
                    "runtime_profile": "model-runtime-central-cpu/v1",
                },
                "resolvedDependencies": [
                    {"uri": "git+MASI-NIDS-vNext", "digest": {"gitCommit": args.source_revision}},
                    {"uri": f"oci://{args.builder_image.split('@', 1)[0]}", "digest": {"sha256": args.builder_image.rsplit("@sha256:", 1)[1]}},
                    {"uri": f"oci://{args.runtime_image.split('@', 1)[0]}", "digest": {"sha256": args.runtime_image.rsplit("@sha256:", 1)[1]}},
                ],
            },
            "runDetails": {
                "builder": {"id": "docker-network-none-central-inference-module-gate"},
                "metadata": {
                    "invocationId": args.run_id,
                    "startedOn": offline["started_at"],
                    "finishedOn": offline["finished_at"],
                    "reproducible": bool(offline["binary_digest_match"]),
                },
                "byproducts": [
                    {"name": path.name, "digest": sha256(path)} for path in sboms + scans
                ],
            },
        },
    }
    args.provenance_output.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    trivy_cache = args.trivy_cache.resolve()
    db_meta = trivy_cache / "db/metadata.json"
    db = trivy_cache / "db/trivy.db"
    java_meta = trivy_cache / "java-db/metadata.json"
    java_db = trivy_cache / "java-db/trivy-java.db"
    checks_meta = trivy_cache / "policy/metadata.json"
    signing_config = repo / "deploy/supply-chain/cosign-offline-signing-config.json"
    public_key = repo / "contracts/trust/v1/cosign.pub"
    manifest = {
        "schema_version": "central-inference-cpu-supply-release/v1",
        "release_id": f"central-inference-cpu-supply-{args.run_id}",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_revision": args.source_revision,
        "source_tree_digest": args.source_tree_digest,
        "working_tree_dirty": args.working_tree_dirty == "true",
        "working_tree_status_digest": args.working_tree_status_digest,
        "image_ref": args.image_ref,
        "image_manifest_digest": args.image_manifest_digest,
        "image_archive_index_digest": archive_index_digest,
        "image_config_digest": args.image_config_digest,
        "base_images": {"builder": args.builder_image, "runtime": args.runtime_image},
        "subjects": subjects,
        "inventory": {
            "component_registry": {"path": registry.relative_to(repo).as_posix(), "digest": sha256(registry)},
            "third_party_notices": {"path": notices.relative_to(repo).as_posix(), "digest": sha256(notices)},
            "vendored_sources": {"path": vendored.relative_to(repo).as_posix(), "digest": sha256(vendored)},
            "cmake_presets": {"path": cmake_presets.relative_to(repo).as_posix(), "digest": sha256(cmake_presets)},
            "working_tree_status": {"path": working_status.name, "digest": sha256(working_status)},
            "sboms": [sized(path) for path in sboms],
            "scans": [sized(path) for path in scans],
            "evidence_inputs": [sized(path) for path in evidence_inputs],
        },
        "offline_bundle": {
            "directory": bundle.name,
            "checksums_path": "SHA256SUMS",
            "checksums_digest": sha256(checksum_path),
            "files": bundle_entries,
        },
        "provenance": {"path": args.provenance_output.name, "digest": sha256(args.provenance_output)},
        "offline_rebuild": {"path": args.offline_rebuild.name, "digest": sha256(args.offline_rebuild), "network": "none"},
        "trust": {
            "public_key": public_key.relative_to(repo).as_posix(),
            "public_key_digest": sha256(public_key),
            "signing_config": signing_config.relative_to(repo).as_posix(),
            "signing_config_digest": sha256(signing_config),
            "transparency_log": "disabled for Owner-authorized local bootstrap",
            "revocation_file": "contracts/trust/v1/revoked-keys.json",
        },
        "tools": tools["tools"],
        "trivy_checks_bundle": {
            **tools["trivy_checks_bundle"],
            "metadata": load(checks_meta),
            "metadata_digest": sha256(checks_meta),
        },
        "vulnerability_database": {
            "metadata": load(db_meta),
            "metadata_digest": sha256(db_meta),
            "database_digest": sha256(db),
            "database_bytes": db.stat().st_size,
        },
        "java_vulnerability_database": {
            "metadata": load(java_meta),
            "metadata_digest": sha256(java_meta),
            "database_digest": sha256(java_db),
            "database_bytes": java_db.stat().st_size,
        },
        "environment": {
            "docker": json.loads(subprocess.run(["docker", "version", "--format", "{{json .}}"], check=True, capture_output=True, text=True).stdout),
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
            "tar_members": MAX_TAR_MEMBERS,
            "tar_expanded_bytes": MAX_BUNDLE_TOTAL_BYTES,
            "checks_bundle_files": 4_096,
            "checks_bundle_total_bytes": MAX_JSON_BYTES,
            "path_bytes": MAX_PATH_BYTES,
        },
    }
    schema = load(repo / "contracts/supply-chain/v1/central-inference-cpu-release.schema.json")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ValueError(
            "\n".join(
                f"release manifest schema error at {list(error.absolute_path)}: {error.message}"
                for error in errors
            )
        )
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode()) > MAX_MANIFEST_BYTES:
        raise ValueError("release manifest exceeds the frozen byte ceiling")
    args.manifest_output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
