#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Iterator

from jsonschema import Draft202012Validator, FormatChecker


MAX_JSON_BYTES = 67_108_864
MAX_MANIFEST_BYTES = 8_388_608
MAX_BUNDLE_FILES = 32_768
MAX_BUNDLE_TOTAL_BYTES = 8_589_934_592
MAX_BUNDLE_FILE_BYTES = 4_294_967_296
MAX_TAR_MEMBERS = 65_536
MAX_TAR_EXPANDED_BYTES = 8_589_934_592
MAX_CHECKS_FILES = 4_096
MAX_CHECKS_TOTAL_BYTES = 67_108_864
MAX_PATH_BYTES = 1_024
MAX_CONFIG_BLOB_BYTES = 8_388_608


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


@contextmanager
def stable_regular_file(
    path: Path, maximum: int
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    if absolute_path_chain_has_symlink(path):
        raise ValueError(f"{path} has a symbolic path component")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError("the qualified Linux profile requires O_NOFOLLOW")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | nofollow)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 1
            or before.st_size > maximum
        ):
            raise ValueError(f"{path} is empty, non-regular, or oversized")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source, before
        after = os.fstat(descriptor)
        lexical_after = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(lexical_after.st_mode)
            or stat_identity(before) != stat_identity(after)
            or stat_identity(before) != stat_identity(lexical_after)
            or absolute_path_chain_has_symlink(path)
        ):
            raise ValueError(f"{path} changed while it was being verified")
    finally:
        os.close(descriptor)


def read_bytes_stable(path: Path, maximum: int) -> bytes:
    with stable_regular_file(path, maximum) as (source, _):
        payload = source.read(maximum + 1)
    if len(payload) > maximum:
        raise ValueError(f"{path} exceeds its byte ceiling")
    return payload


def read_text_stable(path: Path, maximum: int = MAX_JSON_BYTES) -> str:
    return read_bytes_stable(path, maximum).decode("utf-8")


def load_value(path: Path, max_bytes: int = MAX_JSON_BYTES) -> object:
    return json.loads(
        read_text_stable(path, max_bytes),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def load(path: Path, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    value = load_value(path, max_bytes)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def stable_digest_snapshot(
    path: Path, maximum: int = MAX_BUNDLE_FILE_BYTES
) -> tuple[str, int, int, int, int, int]:
    digest = hashlib.sha256()
    with stable_regular_file(path, maximum) as (source, before):
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return ("sha256:" + digest.hexdigest(), before.st_size, *stat_identity(before)[:2], before.st_mtime_ns, before.st_ctime_ns)


def sha256(path: Path) -> str:
    return stable_digest_snapshot(path)[0]


def sorted_file_digest(root: Path) -> tuple[str | None, int, int]:
    if (
        absolute_path_chain_has_symlink(root)
        or root.is_symlink()
        or not root.is_dir()
    ):
        return None, 0, 0
    entries: list[dict[str, object]] = []
    total_bytes = 0
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            return None, 0, 0
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            if (
                len(relative.encode()) > MAX_PATH_BYTES
                or size < 1
                or size > MAX_JSON_BYTES
                or len(entries) >= MAX_CHECKS_FILES
                or total_bytes + size > MAX_CHECKS_TOTAL_BYTES
            ):
                return None, 0, 0
            total_bytes += size
            entries.append(
                {
                    "bytes": size,
                    "path": relative,
                    "sha256": sha256(path),
                }
            )
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return (
        "sha256:" + hashlib.sha256(payload).hexdigest(),
        len(entries),
        total_bytes,
    )


def safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_PATH_BYTES:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute() and ".." not in path.parts and path.as_posix() == value
    )


def path_chain_has_symlink(root: Path, lexical_path: Path) -> bool:
    try:
        relative = lexical_path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def regular_file_within(root: Path, value: object, *, max_bytes: int) -> Path | None:
    if not safe_relative(value):
        return None
    lexical = root / str(value)
    if path_chain_has_symlink(root, lexical) or not lexical.is_file():
        return None
    resolved = lexical.resolve()
    if not resolved.is_relative_to(root):
        return None
    size = resolved.stat().st_size
    return resolved if 0 < size <= max_bytes else None


def safe_tar_archive(path: Path) -> bool:
    member_count = 0
    expanded_bytes = 0
    names: set[str] = set()
    try:
        with stable_regular_file(path, MAX_BUNDLE_FILE_BYTES) as (source, _):
            with tarfile.open(fileobj=source, mode="r:*") as archive:
                for member in archive:
                    member_count += 1
                    name = member.name.removeprefix("./")
                    if (
                        member_count > MAX_TAR_MEMBERS
                        or not safe_relative(name)
                        or name in names
                        or not (member.isfile() or member.isdir())
                    ):
                        return False
                    names.add(name)
                    if member.isfile():
                        expanded_bytes += member.size
                        if (
                            member.size < 0
                            or member.size > MAX_BUNDLE_FILE_BYTES
                            or expanded_bytes > MAX_TAR_EXPANDED_BYTES
                        ):
                            return False
    except (OSError, UnicodeDecodeError, ValueError, tarfile.TarError):
        return False
    return member_count > 0


def parse_time(value: str) -> datetime:
    normalized = re.sub(r"(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$)", r"\1", value).replace(
        "Z", "+00:00"
    )
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("supply-chain timestamps require timezone")
    return parsed


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=False, capture_output=True, text=True)


def normalized_repository(image_ref: str) -> str:
    repository = image_ref.split("@", 1)[0]
    tail = repository.rsplit("/", 1)[-1]
    if ":" in tail:
        repository = repository.rsplit(":", 1)[0]
    if repository.startswith("docker.io/library/"):
        return repository.removeprefix("docker.io/library/")
    if repository.startswith("docker.io/"):
        return repository.removeprefix("docker.io/")
    return repository


def repo_digest_exact(image_ref: str, expected_digest: str | None = None) -> bool:
    inspected = run(["docker", "image", "inspect", image_ref])
    if inspected.returncode != 0:
        return False
    try:
        documents = json.loads(inspected.stdout)
        repo_digests = documents[0].get("RepoDigests", [])
    except (IndexError, TypeError, json.JSONDecodeError):
        return False
    digest = expected_digest
    if digest is None:
        if "@sha256:" not in image_ref:
            return False
        digest = "sha256:" + image_ref.rsplit("@sha256:", 1)[1]
    expected = f"{normalized_repository(image_ref)}@{digest}"
    return expected in repo_digests


def cargo_registry_packages(cargo_lock: Path) -> set[tuple[str, str]]:
    packages: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    field = re.compile(r'^(name|version|source) = "(.*)"$')
    for line in read_text_stable(cargo_lock).splitlines() + ["[[package]]"]:
        if line == "[[package]]":
            if current is not None:
                packages.append(current)
            current = {}
            continue
        if current is None:
            continue
        match = field.match(line)
        if match:
            current[match.group(1)] = match.group(2)
    return {
        (item["name"], item["version"])
        for item in packages
        if item.get("source", "").startswith("registry+")
    }


def spdx_cargo_packages(document: dict[str, Any]) -> set[tuple[str, str]]:
    packages: set[tuple[str, str]] = set()
    for item in document.get("packages", []) or []:
        if not isinstance(item, dict):
            continue
        for reference in item.get("externalRefs", []) or []:
            if not isinstance(reference, dict):
                continue
            locator = str(reference.get("referenceLocator", ""))
            if not locator.startswith("pkg:cargo/") or "@" not in locator:
                continue
            name_version = urllib.parse.unquote(
                locator.removeprefix("pkg:cargo/").split("?", 1)[0]
            )
            name, version = name_version.rsplit("@", 1)
            packages.add((name, version))
    return packages


def cosign_verify(
    image: str, supply: Path, trust: Path, manifest: str, bundle: str, key: str
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--volume",
            f"{supply}:/supply:ro",
            "--volume",
            f"{trust}:/trust:ro",
            image,
            "verify-blob",
            "--private-infrastructure",
            "--key",
            f"/trust/{key}",
            "--bundle",
            f"/supply/{bundle}",
            f"/supply/{manifest}",
        ]
    )


def trivy_findings(paths: list[Path]) -> dict[str, list[dict[str, object]]]:
    findings: dict[str, list[dict[str, object]]] = {
        "critical": [],
        "fixable_high": [],
        "unfixed_high": [],
        "secrets": [],
        "misconfig_high_critical": [],
    }
    for path in paths:
        for result in load(path).get("Results", []) or []:
            if not isinstance(result, dict):
                continue
            target = str(result.get("Target", path.name))
            for item in result.get("Vulnerabilities", []) or []:
                if not isinstance(item, dict):
                    continue
                record = {
                    "source": path.name,
                    "target": target,
                    "id": item.get("VulnerabilityID"),
                    "package": item.get("PkgName"),
                    "installed": item.get("InstalledVersion"),
                    "fixed": item.get("FixedVersion"),
                    "severity": item.get("Severity"),
                }
                severity = str(item.get("Severity", "UNKNOWN")).upper()
                if severity == "CRITICAL":
                    findings["critical"].append(record)
                elif severity == "HIGH" and item.get("FixedVersion"):
                    findings["fixable_high"].append(record)
                elif severity == "HIGH":
                    findings["unfixed_high"].append(record)
            for item in result.get("Secrets", []) or []:
                if isinstance(item, dict):
                    findings["secrets"].append(
                        {
                            "source": path.name,
                            "target": target,
                            "rule_id": item.get("RuleID"),
                            "category": item.get("Category"),
                            "severity": item.get("Severity"),
                        }
                    )
            for item in result.get("Misconfigurations", []) or []:
                if isinstance(item, dict) and str(
                    item.get("Severity", "UNKNOWN")
                ).upper() in {"HIGH", "CRITICAL"}:
                    findings["misconfig_high_critical"].append(
                        {
                            "source": path.name,
                            "target": target,
                            "id": item.get("ID"),
                            "severity": item.get("Severity"),
                            "title": item.get("Title"),
                        }
                    )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--supply-dir", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--trivy-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    for label, path in (
        ("repository", args.repo),
        ("supply directory", args.supply_dir),
        ("bundle directory", args.bundle_dir),
        ("manifest", args.manifest),
        ("signature bundle", args.bundle),
        ("Trivy cache", args.trivy_cache),
        ("output", args.output),
    ):
        if absolute_path_chain_has_symlink(path):
            raise SystemExit(f"{label} must not contain a symbolic path component")
    repo = args.repo.resolve()
    supply = args.supply_dir.resolve()
    bundle_dir = args.bundle_dir.resolve()
    if not supply.is_dir() or not bundle_dir.is_dir() or not args.trivy_cache.is_dir():
        raise SystemExit("supply, bundle, and Trivy cache roots must be directories")
    manifest = load(args.manifest, MAX_MANIFEST_BYTES)
    release_schema = load(
        repo / "contracts/supply-chain/v1/rust-edge-agent-release.schema.json"
    )
    release_errors = sorted(
        Draft202012Validator(
            release_schema, format_checker=FormatChecker()
        ).iter_errors(manifest),
        key=lambda error: list(error.path),
    )
    if release_errors:
        raise SystemExit(
            "\n".join(
                f"release manifest schema error at {list(error.path)}: {error.message}"
                for error in release_errors
            )
        )
    provenance_path = regular_file_within(
        supply,
        manifest["provenance"]["path"],
        max_bytes=MAX_JSON_BYTES,
    )
    if provenance_path is None:
        raise SystemExit("release provenance path is missing, symbolic, or escaping")
    provenance = load(provenance_path)
    tools = load(repo / "deploy/supply-chain/tools.lock.json")
    policy = load(repo / "deploy/supply-chain/trivy-policy.json")
    registry_path = repo / "contracts/supply-chain/v1/rust-edge-agent-components.json"
    notices_path = (
        repo / "contracts/supply-chain/v1/rust-edge-agent-third-party-notices.json"
    )
    registry = load(registry_path)
    notices = load(notices_path)
    schema = load(repo / "contracts/supply-chain/v1/schema.json")
    registry_errors = list(Draft202012Validator(schema).iter_errors(registry))

    trust = repo / "contracts/trust/v1"
    cosign_image = str(tools["tools"]["cosign"]["image"])
    positive = cosign_verify(
        cosign_image, supply, trust, args.manifest.name, args.bundle.name, "cosign.pub"
    )
    mutated = supply / "release-manifest.mutated-negative.json"
    wrong_trust = supply / "negative-trust"
    try:
        mutated_document = dict(manifest)
        mutated_document["release_id"] = str(manifest["release_id"]) + "-tampered"
        mutated.write_text(
            json.dumps(mutated_document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        mutated_result = cosign_verify(
            cosign_image,
            supply,
            trust,
            mutated.name,
            args.bundle.name,
            "cosign.pub",
        )
        wrong_trust.mkdir(exist_ok=True)
        shutil.copyfile(
            repo / "testkit/fixtures/supply-chain/wrong-publisher-cosign.pub",
            wrong_trust / "wrong.pub",
        )
        wrong_result = cosign_verify(
            cosign_image,
            supply,
            wrong_trust,
            args.manifest.name,
            args.bundle.name,
            "wrong.pub",
        )
    finally:
        mutated.unlink(missing_ok=True)
        if wrong_trust.exists():
            shutil.rmtree(wrong_trust)
    key_policy = load(trust / "cosign-key-policy.json")
    revocations = load(trust / "revoked-keys.json")
    public_key_digest = sha256(trust / "cosign.pub")
    revoked = public_key_digest in {
        "sha256:" + str(value).removeprefix("sha256:")
        for value in revocations["revoked_sha256_fingerprints"]
    }
    signature_checks = {
        "positive_verification": positive.returncode == 0,
        "mutated_manifest_rejected": mutated_result.returncode != 0,
        "wrong_publisher_rejected": wrong_result.returncode != 0,
        "public_key_digest_exact": public_key_digest == key_policy["public_key_sha256"],
        "public_key_not_revoked": not revoked,
        "manifest_public_key_digest_exact": manifest["trust"]["public_key_digest"]
        == public_key_digest,
        "allowed_algorithm_exact": key_policy.get("allowed_algorithm")
        == "ECDSA-P256-SHA256",
        "owner_authorization_present": bool(
            key_policy.get("passwordless_local_key_owner_authorization")
        ),
        "scope_explicit": key_policy.get("scope")
        == "one-time v1.18 bootstrap qualification artifacts",
    }

    sbom_paths = sorted(supply.glob("*.spdx.json"))
    sbom_summary: list[dict[str, object]] = []
    sbom_checks = {
        "image_and_source_present": {path.name for path in sbom_paths}
        >= {"image.spdx.json", "source.spdx.json"},
        "all_spdx": True,
        "all_have_packages": True,
    }
    for path in sbom_paths:
        document = load(path)
        packages = document.get("packages", [])
        package_count = len(packages) if isinstance(packages, list) else 0
        sbom_checks["all_spdx"] = bool(
            sbom_checks["all_spdx"]
            and str(document.get("spdxVersion", "")).startswith("SPDX-")
        )
        sbom_checks["all_have_packages"] = bool(
            sbom_checks["all_have_packages"] and package_count > 0
        )
        sbom_summary.append(
            {"path": path.name, "digest": sha256(path), "packages": package_count}
        )
    image_spdx = (
        load(supply / "image.spdx.json")
        if (supply / "image.spdx.json").is_file()
        else {}
    )
    source_spdx = (
        load(supply / "source.spdx.json")
        if (supply / "source.spdx.json").is_file()
        else {}
    )
    cargo_packages = cargo_registry_packages(repo / "edge-rs/Cargo.lock")
    spdx_packages = spdx_cargo_packages(source_spdx)
    sbom_checks.update(
        {
            "image_subject_exact": image_spdx.get("name") == "/input/edge-image.tar",
            "source_subject_exact": source_spdx.get("name") == "/source",
            "cargo_lock_registry_closure_exact": cargo_packages == spdx_packages,
        }
    )

    scan_paths = sorted(supply.glob("trivy-*.json"))
    findings = trivy_findings(scan_paths)
    db_metadata_path = args.trivy_cache / "db/metadata.json"
    java_metadata_path = args.trivy_cache / "java-db/metadata.json"
    db_age = max(
        0,
        int(
            (
                datetime.now(timezone.utc)
                - parse_time(str(load(db_metadata_path)["DownloadedAt"]))
            ).total_seconds()
        ),
    )
    java_age = max(
        0,
        int(
            (
                datetime.now(timezone.utc)
                - parse_time(str(load(java_metadata_path)["DownloadedAt"]))
            ).total_seconds()
        ),
    )
    max_age = int(policy["database_max_age_seconds"])
    trivy_image_document = (
        load(supply / "trivy-image.json")
        if (supply / "trivy-image.json").is_file()
        else {}
    )
    trivy_config_document = (
        load(supply / "trivy-config.json")
        if (supply / "trivy-config.json").is_file()
        else {}
    )
    trivy_secret_document = (
        load(supply / "trivy-source-secret.json")
        if (supply / "trivy-source-secret.json").is_file()
        else {}
    )
    checks_lock = tools["trivy_checks_bundle"]
    checks_metadata_path = args.trivy_cache / "policy/metadata.json"
    checks_metadata = load(checks_metadata_path)
    checks_content_digest, checks_file_count, checks_total_bytes = sorted_file_digest(
        args.trivy_cache / "policy/content"
    )
    trivy_config_log_path = supply / "trivy-config.log"
    trivy_config_log = (
        read_text_stable(trivy_config_log_path)
        if trivy_config_log_path.is_file() and not trivy_config_log_path.is_symlink()
        else ""
    )
    trivy_metadata = trivy_image_document.get("Metadata", {})
    scan_checks = {
        "required_documents": {path.name for path in scan_paths}
        >= {"trivy-image.json", "trivy-config.json", "trivy-source-secret.json"},
        "database_present": (args.trivy_cache / "db/trivy.db").is_file(),
        "database_fresh": db_age <= max_age,
        "java_database_present": (args.trivy_cache / "java-db/trivy-java.db").is_file(),
        "java_database_fresh": java_age <= max_age,
        "checks_bundle_reference_digest_pinned": re.fullmatch(
            r"[^\s@]+@sha256:[0-9a-f]{64}", str(checks_lock.get("reference", ""))
        )
        is not None,
        "checks_bundle_metadata_digest_exact": checks_metadata.get("Digest")
        == str(checks_lock["reference"]).rsplit("@", 1)[-1],
        "checks_bundle_content_digest_exact": checks_content_digest
        == checks_lock.get("content_digest"),
        "checks_bundle_file_count_exact": checks_file_count
        == checks_lock.get("file_count"),
        "checks_bundle_resource_bounds": 0 < checks_file_count <= MAX_CHECKS_FILES
        and 0 < checks_total_bytes <= MAX_CHECKS_TOTAL_BYTES,
        "checks_bundle_lock_semantics_exact": set(checks_lock)
        == {
            "version",
            "reference",
            "content_digest_algorithm",
            "content_digest",
            "file_count",
            "license",
        }
        and checks_lock.get("version") == "2"
        and checks_lock.get("content_digest_algorithm") == "sorted-file-sha256-json/v1"
        and checks_lock.get("license") == "Apache-2.0",
        "checks_bundle_manifest_exact": manifest.get("trivy_checks_bundle", {}).get(
            "reference"
        )
        == checks_lock.get("reference")
        and manifest.get("trivy_checks_bundle", {}).get("content_digest")
        == checks_lock.get("content_digest")
        and manifest.get("trivy_checks_bundle", {}).get("content_digest_algorithm")
        == checks_lock.get("content_digest_algorithm")
        and manifest.get("trivy_checks_bundle", {}).get("file_count")
        == checks_lock.get("file_count")
        and manifest.get("trivy_checks_bundle", {}).get("version")
        == checks_lock.get("version")
        and manifest.get("trivy_checks_bundle", {}).get("license")
        == checks_lock.get("license")
        and manifest.get("trivy_checks_bundle", {}).get("metadata_digest")
        == sha256(checks_metadata_path),
        "config_scan_loaded_pinned_cache": "loading from existing cache"
        in trivy_config_log,
        "config_scan_no_embedded_fallback": "Falling back to embedded checks"
        not in trivy_config_log,
        "config_scan_no_bundle_download": "Downloading the checks bundle"
        not in trivy_config_log,
        "no_secrets": not findings["secrets"],
        "no_critical": not findings["critical"],
        "no_fixable_high": not findings["fixable_high"],
        "no_high_critical_misconfiguration": not findings["misconfig_high_critical"],
        "image_config_subject_exact": isinstance(trivy_metadata, dict)
        and trivy_metadata.get("ImageID") == manifest.get("image_config_digest"),
        "image_tag_subject_exact": isinstance(trivy_metadata, dict)
        and manifest.get("image_ref") in (trivy_metadata.get("RepoTags", []) or []),
        "image_archive_subject_exact": trivy_image_document.get("ArtifactName")
        == "/input/edge-image.tar",
        "config_source_subject_exact": trivy_config_document.get("ArtifactName")
        == "/source",
        "secret_source_subject_exact": trivy_secret_document.get("ArtifactName")
        == "/source",
    }

    declared_inventory_files = [
        item
        for group in (
            manifest["inventory"]["sboms"],
            manifest["inventory"]["scans"],
            manifest["inventory"]["evidence_inputs"],
        )
        for item in group
    ]
    declared_inventory_paths = [str(item["path"]) for item in declared_inventory_files]
    manifest_file_checks = len(declared_inventory_paths) == len(
        set(declared_inventory_paths)
    )
    for item in declared_inventory_files:
        artifact = regular_file_within(supply, item["path"], max_bytes=MAX_JSON_BYTES)
        manifest_file_checks = bool(
            manifest_file_checks
            and artifact is not None
            and artifact.stat().st_size == item["bytes"]
            and sha256(artifact) == item["digest"]
        )
    manifest_subjects = {
        (item["name"], "sha256:" + item["digest"]["sha256"])
        for item in manifest["subjects"]
    }
    provenance_subjects = {
        (item["name"], "sha256:" + item["digest"]["sha256"])
        for item in provenance["subject"]
    }
    offline_path = regular_file_within(
        supply,
        manifest["offline_rebuild"]["path"],
        max_bytes=MAX_JSON_BYTES,
    )
    if offline_path is None:
        raise SystemExit("offline rebuild path is missing, symbolic, or escaping")
    offline = load(offline_path)
    archive_index_path = supply / "image-archive-manifest.json"
    if (
        archive_index_path.is_symlink()
        or not archive_index_path.is_file()
        or archive_index_path.stat().st_size > MAX_JSON_BYTES
    ):
        archive_index: object = None
    else:
        archive_index = load_value(archive_index_path)
    archive_entry = (
        archive_index[0]
        if isinstance(archive_index, list) and len(archive_index) == 1
        else {}
    )
    archive_config_path = (
        str(archive_entry.get("Config", "")) if isinstance(archive_entry, dict) else ""
    )
    archive_config_path_valid = (
        re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", archive_config_path) is not None
    )
    archive_config_digest = (
        "sha256:" + PurePosixPath(archive_config_path).name
        if archive_config_path_valid
        else ""
    )
    archive_config_blob_digest = ""
    try:
        with tarfile.open(bundle_dir / "edge-image.tar", "r") as archive:
            member = archive.getmember(archive_config_path)
            if member.isfile() and 0 < member.size <= MAX_CONFIG_BLOB_BYTES:
                extracted = archive.extractfile(member)
                if extracted is not None:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                        digest.update(chunk)
                    archive_config_blob_digest = "sha256:" + digest.hexdigest()
    except (KeyError, OSError, tarfile.TarError, ValueError):
        archive_config_blob_digest = ""
    supplied_oci_inspection_path = supply / "oci-archive-inspection.json"
    supplied_archive_config_path = supply / "image-archive-config.json"
    supplied_oci_inspection = (
        load(supplied_oci_inspection_path)
        if supplied_oci_inspection_path.is_file()
        and not supplied_oci_inspection_path.is_symlink()
        else {}
    )
    recomputed_oci_inspection: dict[str, Any] = {}
    recomputed_oci_index_digest = ""
    recomputed_oci_config_digest = ""
    oci_inspector_status = 1
    with tempfile.TemporaryDirectory(
        prefix="masi-edge-supply-oci-inspect-"
    ) as directory:
        inspection_root = Path(directory)
        inspection_result = run(
            [
                sys.executable,
                str(repo / "edge-rs/scripts/inspect-oci-archive.py"),
                "--archive",
                str(bundle_dir / "edge-image.tar"),
                "--image-ref",
                str(manifest["image_ref"]),
                "--index-output",
                str(inspection_root / "image-archive-manifest.json"),
                "--config-output",
                str(inspection_root / "image-archive-config.json"),
                "--summary-output",
                str(inspection_root / "oci-archive-inspection.json"),
            ]
        )
        oci_inspector_status = inspection_result.returncode
        if oci_inspector_status == 0:
            recomputed_oci_inspection = load(
                inspection_root / "oci-archive-inspection.json"
            )
            recomputed_oci_index_digest = sha256(
                inspection_root / "image-archive-manifest.json"
            )
            recomputed_oci_config_digest = sha256(
                inspection_root / "image-archive-config.json"
            )
    recorded_image_path = supply / "image-config.json"
    recorded_image_inspect = (
        load_value(recorded_image_path)
        if recorded_image_path.is_file()
        and not recorded_image_path.is_symlink()
        and recorded_image_path.stat().st_size <= MAX_JSON_BYTES
        else None
    )
    recorded_repo_digests = (
        recorded_image_inspect[0].get("RepoDigests", [])
        if isinstance(recorded_image_inspect, list)
        and len(recorded_image_inspect) == 1
        and isinstance(recorded_image_inspect[0], dict)
        else []
    )
    recorded_image_config = (
        recorded_image_inspect[0].get("Config", {})
        if isinstance(recorded_image_inspect, list)
        and len(recorded_image_inspect) == 1
        and isinstance(recorded_image_inspect[0], dict)
        else {}
    )
    recorded_image_labels = (
        recorded_image_config.get("Labels", {})
        if isinstance(recorded_image_config, dict)
        and isinstance(recorded_image_config.get("Labels", {}), dict)
        else {}
    )
    recorded_expected_repo_digest = f"{normalized_repository(str(manifest['image_ref']))}@{manifest['image_manifest_digest']}"
    provenance_checks = {
        "statement_type": provenance.get("_type") == "https://in-toto.io/Statement/v1",
        "predicate_type": provenance.get("predicateType")
        == "https://slsa.dev/provenance/v1",
        "subjects_exact": manifest_subjects == provenance_subjects,
        "manifest_digest_exact": manifest["provenance"]["digest"]
        == sha256(provenance_path),
        "inventory_file_digests_exact": manifest_file_checks,
        "image_binary_subject_exact": (
            "usr/local/bin/masi-edge",
            sha256(supply / "masi-edge.image.bin"),
        )
        in manifest_subjects,
        "offline_binary_subject_exact": (
            "offline-rebuild/masi-edge",
            sha256(supply / "masi-edge.offline-rebuild.bin"),
        )
        in manifest_subjects,
        "source_tree_subject_exact": (
            "source-tree.tar",
            sha256(bundle_dir / "source-tree.tar"),
        )
        in manifest_subjects,
        "image_manifest_subject_exact": (
            "masi-nids/rust-edge-agent-oci-manifest",
            str(manifest["image_manifest_digest"]),
        )
        in manifest_subjects,
        "image_archive_index_subject_exact": (
            "masi-nids/rust-edge-agent-docker-archive-index",
            sha256(archive_index_path),
        )
        in manifest_subjects,
        "image_config_subject_exact": (
            "masi-nids/rust-edge-agent-oci-config",
            str(manifest["image_config_digest"]),
        )
        in manifest_subjects,
        "image_archive_index_digest_exact": manifest["image_archive_index_digest"]
        == sha256(archive_index_path),
        "archive_config_path_digest_exact": archive_config_digest
        == manifest["image_config_digest"],
        "archive_config_blob_digest_exact": archive_config_blob_digest
        == manifest["image_config_digest"],
        "archive_config_path_safe": archive_config_path_valid,
        "image_archive_tar_safe": safe_tar_archive(bundle_dir / "edge-image.tar"),
        "oci_archive_inspector_passed": oci_inspector_status == 0,
        "oci_archive_inspection_exact": supplied_oci_inspection
        == recomputed_oci_inspection,
        "oci_archive_manifest_exact": recomputed_oci_inspection.get("manifest_digest")
        == manifest["image_manifest_digest"],
        "oci_archive_index_evidence_exact": recomputed_oci_index_digest
        == sha256(archive_index_path),
        "oci_archive_config_evidence_exact": supplied_archive_config_path.is_file()
        and not supplied_archive_config_path.is_symlink()
        and recomputed_oci_config_digest == sha256(supplied_archive_config_path)
        and recomputed_oci_inspection.get("config_digest")
        == manifest["image_config_digest"],
        "oci_archive_binary_exact": recomputed_oci_inspection.get("binary_digest")
        == sha256(supply / "masi-edge.image.bin"),
        "oci_archive_has_no_embedded_attestation": recomputed_oci_inspection.get(
            "attestation_count"
        )
        == 0,
        "archive_tag_exact": isinstance(archive_entry, dict)
        and manifest["image_ref"] in (archive_entry.get("RepoTags", []) or []),
        "working_tree_status_subject_exact": (
            "working-tree-status.txt",
            sha256(supply / "working-tree-status.txt"),
        )
        in manifest_subjects,
        "working_tree_status_digest_exact": manifest["working_tree_status_digest"]
        == sha256(supply / "working-tree-status.txt"),
        "recorded_image_repo_digest_exact": recorded_expected_repo_digest
        in recorded_repo_digests,
        "image_source_revision_label_exact": recorded_image_labels.get(
            "org.opencontainers.image.revision"
        )
        == manifest["source_revision"],
        "image_source_tree_digest_label_exact": recorded_image_labels.get(
            "io.masi-nids.source-tree.digest"
        )
        == manifest["source_tree_digest"],
        "source_revision_exact": provenance["predicate"]["buildDefinition"][
            "externalParameters"
        ]["source_revision"]
        == manifest["source_revision"],
        "source_tree_digest_exact": provenance["predicate"]["buildDefinition"][
            "externalParameters"
        ]["source_tree_digest"]
        == manifest["source_tree_digest"],
        "working_tree_state_exact": provenance["predicate"]["buildDefinition"][
            "externalParameters"
        ]["working_tree_dirty"]
        == manifest["working_tree_dirty"]
        and provenance["predicate"]["buildDefinition"]["externalParameters"][
            "working_tree_status_digest"
        ]
        == manifest["working_tree_status_digest"],
        "offline_result_digest_exact": manifest["offline_rebuild"]["digest"]
        == sha256(supply / str(manifest["offline_rebuild"]["path"])),
    }
    offline_checks = {
        "network_none": offline.get("network") == "none",
        "cargo_locked_offline": bool(offline.get("cargo_locked_offline")),
        "binary_digest_match": bool(offline.get("binary_digest_match")),
        "source_tree_digest_exact": offline.get("source_tree_digest")
        == manifest.get("source_tree_digest"),
    }
    registry_names = {str(item["name"]) for item in registry["components"]}
    notice_names = {str(item["component"]) for item in notices["notices"]}
    inventory_checks = {
        "registry_schema": not registry_errors,
        "module_exact": registry.get("module") == "rust-edge-agent"
        and notices.get("module") == "rust-edge-agent",
        "notices_cover_registry": registry_names == notice_names,
        "component_names_unique": len(registry_names) == len(registry["components"]),
        "no_runtime_download": all(
            item.get("runtime_download") is False for item in registry["components"]
        ),
        "cargo_lock_digest_exact": manifest["inventory"]["cargo_lock"]["digest"]
        == sha256(repo / "edge-rs/Cargo.lock"),
        "working_tree_status_digest_exact": manifest["inventory"][
            "working_tree_status"
        ]["digest"]
        == sha256(supply / str(manifest["inventory"]["working_tree_status"]["path"])),
        "component_registry_digest_exact": manifest["inventory"]["component_registry"][
            "digest"
        ]
        == sha256(registry_path),
        "third_party_notices_digest_exact": manifest["inventory"][
            "third_party_notices"
        ]["digest"]
        == sha256(notices_path),
    }

    bundle_entries = manifest["offline_bundle"]["files"]
    bundle_paths = [str(item["path"]) for item in bundle_entries]
    bundle_path_safe = all(safe_relative(path) for path in bundle_paths)
    bundle_paths_unique = len(bundle_paths) == len(set(bundle_paths))
    actual_bundle_files: dict[str, Path] = {}
    bundle_structure_safe = not bundle_dir.is_symlink()
    for path in bundle_dir.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            bundle_structure_safe = False
            continue
        if path.is_file() and path != bundle_dir / "SHA256SUMS":
            relative = path.relative_to(bundle_dir).as_posix()
            if not safe_relative(relative) or relative in actual_bundle_files:
                bundle_structure_safe = False
                continue
            actual_bundle_files[relative] = path
    bundle_file_snapshots: dict[str, tuple[str, int, int, int, int, int]] = {}
    for relative, path in actual_bundle_files.items():
        try:
            bundle_file_snapshots[relative] = stable_digest_snapshot(
                path, MAX_BUNDLE_FILE_BYTES
            )
        except (OSError, ValueError):
            bundle_structure_safe = False
    actual_total_bytes = sum(value[1] for value in bundle_file_snapshots.values())
    bundle_resources_bounded = (
        0 < len(actual_bundle_files) <= MAX_BUNDLE_FILES
        and set(bundle_file_snapshots) == set(actual_bundle_files)
        and 0 < actual_total_bytes <= MAX_BUNDLE_TOTAL_BYTES
        and all(
            0 < snapshot[1] <= MAX_BUNDLE_FILE_BYTES
            for snapshot in bundle_file_snapshots.values()
        )
    )
    bundle_file_set_exact = set(bundle_paths) == set(actual_bundle_files)
    bundle_files_exact = (
        bundle_structure_safe
        and bundle_path_safe
        and bundle_paths_unique
        and bundle_resources_bounded
        and bundle_file_set_exact
        and all(
            str(item["path"]) in bundle_file_snapshots
            and bundle_file_snapshots[str(item["path"])][1] == item["bytes"]
            and bundle_file_snapshots[str(item["path"])][0] == item["digest"]
            for item in bundle_entries
        )
    )
    checksums_path = regular_file_within(
        bundle_dir,
        manifest["offline_bundle"]["checksums_path"],
        max_bytes=MAX_MANIFEST_BYTES,
    )
    checksum_inventory: dict[str, str] = {}
    checksum_inventory_valid = checksums_path is not None
    if checksums_path is not None:
        for line in read_text_stable(
            checksums_path, MAX_MANIFEST_BYTES
        ).splitlines():
            match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
            if match is None:
                checksum_inventory_valid = False
                continue
            relative = match.group(2).removeprefix("./")
            if (
                not safe_relative(relative)
                or relative in checksum_inventory
                or relative == "SHA256SUMS"
            ):
                checksum_inventory_valid = False
                continue
            checksum_inventory[relative] = "sha256:" + match.group(1)
    expected_bundle_digests = {
        str(item["path"]): str(item["digest"]) for item in bundle_entries
    }
    checksum_inventory_exact = (
        checksum_inventory_valid and checksum_inventory == expected_bundle_digests
    )
    expected_tar_paths = {
        "base-images.tar",
        "cargo-vendor.tar",
        "edge-image.tar",
        "source-tree.tar",
        "tool-images.tar",
        "trivy-db.tar",
    }
    observed_tar_paths = {path for path in actual_bundle_files if path.endswith(".tar")}
    tar_archives_safe = observed_tar_paths == expected_tar_paths and all(
        safe_tar_archive(actual_bundle_files[path]) for path in observed_tar_paths
    )
    expected_resource_limits = {
        "manifest_bytes": MAX_MANIFEST_BYTES,
        "json_input_bytes": MAX_JSON_BYTES,
        "offline_bundle_files": MAX_BUNDLE_FILES,
        "offline_bundle_total_bytes": MAX_BUNDLE_TOTAL_BYTES,
        "offline_bundle_file_bytes": MAX_BUNDLE_FILE_BYTES,
        "tar_members": MAX_TAR_MEMBERS,
        "tar_expanded_bytes": MAX_TAR_EXPANDED_BYTES,
        "checks_bundle_files": MAX_CHECKS_FILES,
        "checks_bundle_total_bytes": MAX_CHECKS_TOTAL_BYTES,
        "path_bytes": MAX_PATH_BYTES,
    }
    bundle_checks = {
        "paths_safe": bundle_path_safe,
        "paths_unique": bundle_paths_unique,
        "filesystem_structure_safe": bundle_structure_safe,
        "file_set_exact": bundle_file_set_exact,
        "resource_bounds": bundle_resources_bounded,
        "all_files_exact": bundle_files_exact,
        "checksums_digest_exact": checksums_path is not None
        and sha256(checksums_path) == manifest["offline_bundle"]["checksums_digest"],
        "checksums_inventory_exact": checksum_inventory_exact,
        "tar_archives_safe": tar_archives_safe,
        "resource_contract_exact": manifest.get("resource_limits")
        == expected_resource_limits,
        "unchanged_during_verification": False,
        "source_tree_digest_exact": "source-tree.tar" in actual_bundle_files
        and sha256(actual_bundle_files["source-tree.tar"])
        == manifest["source_tree_digest"],
    }

    tool_checks: dict[str, bool] = {
        "tools_lock_shape_exact": set(tools)
        == {
            "schema_version",
            "platform",
            "tools",
            "trivy_checks_bundle",
            "network_policy",
            "mutable_tag_policy",
        }
        and tools.get("schema_version") == "supply-chain-tools-lock/v1"
        and tools.get("platform") == "linux/amd64"
        and set(tools.get("tools", {})) == {"syft", "trivy", "cosign"}
        and tools.get("network_policy")
        == "formal verification runs all tools with --network none"
        and tools.get("mutable_tag_policy") == "forbidden",
        "manifest_tools_exact": manifest.get("tools") == tools.get("tools"),
    }
    for name, value in tools["tools"].items():
        image_ref = str(value["image"])
        tool_checks[f"{name}_image_repo_digest_exact"] = repo_digest_exact(image_ref)
    for name, image_ref in manifest["base_images"].items():
        tool_checks[f"{name}_base_image_repo_digest_exact"] = repo_digest_exact(
            str(image_ref)
        )
    tool_checks["release_image_manifest_repo_digest_exact"] = repo_digest_exact(
        str(manifest["image_ref"]), str(manifest["image_manifest_digest"])
    )
    try:
        bundle_checks["unchanged_during_verification"] = (
            set(bundle_file_snapshots) == set(actual_bundle_files)
            and all(
                stable_digest_snapshot(path, MAX_BUNDLE_FILE_BYTES)
                == bundle_file_snapshots[relative]
                for relative, path in actual_bundle_files.items()
            )
        )
    except (OSError, ValueError):
        bundle_checks["unchanged_during_verification"] = False

    hard_checks = {
        **signature_checks,
        **sbom_checks,
        **scan_checks,
        **provenance_checks,
        **offline_checks,
        **inventory_checks,
        **bundle_checks,
        **tool_checks,
    }
    failures = [name for name, passed in hard_checks.items() if not passed]
    holds = (
        ["UNFIXED_HIGH_REQUIRES_OWNER_EXCEPTION"] if findings["unfixed_high"] else []
    )
    if manifest.get("working_tree_dirty") is not False:
        holds.append("DIRTY_WORKTREE_NOT_RELEASE_BASELINE")
    result = "FAIL" if failures else "HOLD" if holds else "PASS"
    verification = {
        "schema_version": "rust-edge-agent-supply-verification/v1",
        "test_id": "SEC-SUPPLY-001",
        "requirement_ids": [
            "MOD-EDGE-001",
            "ARCH-REUSE-001",
            "SEC-SUPPLY-001",
            "TEST-010",
        ],
        "run_id": manifest["release_id"],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
        "manifest_digest": sha256(args.manifest),
        "source_revision": manifest["source_revision"],
        "source_tree_digest": manifest["source_tree_digest"],
        "working_tree_dirty": manifest["working_tree_dirty"],
        "working_tree_status_digest": manifest["working_tree_status_digest"],
        "signature_bundle_digest": sha256(args.bundle),
        "registry_digest": sha256(registry_path),
        "checks": {
            "signature": signature_checks,
            "sbom": sbom_checks,
            "scan": scan_checks,
            "provenance": provenance_checks,
            "offline_rebuild": offline_checks,
            "inventory": inventory_checks,
            "offline_bundle": bundle_checks,
            "tool_images": tool_checks,
        },
        "sboms": sbom_summary,
        "trivy": {
            "database_age_seconds": db_age,
            "java_database_age_seconds": java_age,
            "findings": findings,
        },
        "negative_verification": {
            "mutated_exit_code": mutated_result.returncode,
            "wrong_publisher_exit_code": wrong_result.returncode,
        },
        "failure_reasons": failures,
        "hold_reasons": holds,
        "overall_module_complete": False,
    }
    evidence_schema = load(repo / "contracts/evidence/v1/edge-module-schema.json")
    evidence_errors = list(
        Draft202012Validator(evidence_schema).iter_errors(verification)
    )
    if evidence_errors:
        raise SystemExit(
            "\n".join(
                f"edge supply evidence {list(error.path)}: {error.message}"
                for error in evidence_errors
            )
        )
    args.output.write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if result == "PASS" else 2 if result == "HOLD" else 1


if __name__ == "__main__":
    raise SystemExit(main())
