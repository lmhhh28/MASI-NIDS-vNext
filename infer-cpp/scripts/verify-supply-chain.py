#!/usr/bin/env python3
"""Fail-closed verifier for Central Inference CPU supply-chain evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


MAX_JSON_BYTES = 67_108_864
MAX_MANIFEST_BYTES = 8_388_608
MAX_BUNDLE_FILE_BYTES = 4_294_967_296
MAX_BUNDLE_TOTAL_BYTES = 8_589_934_592
MAX_TAR_MEMBERS = 65_536
MAX_PATH_BYTES = 1_024


def absolute_path_chain_has_symlink(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def load(path: Path, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    if (
        absolute_path_chain_has_symlink(path)
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > maximum
    ):
        raise ValueError(f"unsafe, missing, or oversized JSON file: {path}")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON file is not an object: {path}")
    return value


def sha256(path: Path) -> str:
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


def safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_PATH_BYTES:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
        and "//" not in value
    )


def safe_tar_archive(path: Path) -> bool:
    names: set[str] = set()
    total = 0
    try:
        with tarfile.open(path, "r:*") as archive:
            for index, member in enumerate(archive, start=1):
                name = member.name.removeprefix("./")
                if (
                    index > MAX_TAR_MEMBERS
                    or not safe_relative(name)
                    or name in names
                    or not (member.isfile() or member.isdir())
                ):
                    return False
                names.add(name)
                if member.isfile():
                    total += member.size
                    if member.size < 0 or member.size > MAX_BUNDLE_FILE_BYTES or total > MAX_BUNDLE_TOTAL_BYTES:
                        return False
    except (OSError, tarfile.TarError, ValueError):
        return False
    return bool(names)


def parse_time(value: str) -> datetime:
    normalized = re.sub(
        r"(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$)", r"\1", value
    ).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=False, capture_output=True, text=True)


def cosign_verify(
    image: str, supply: Path, trust: Path, manifest_name: str, bundle_name: str, key_name: str
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker", "run", "--rm", "--user", "0:0", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt",
            "no-new-privileges:true", "--pids-limit", "64", "--memory", "256m",
            "--cpus", "1", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--volume", f"{supply}:/supply:ro", "--volume", f"{trust}:/trust:ro",
            image, "verify-blob", "--private-infrastructure", "--key",
            f"/trust/{key_name}", "--bundle", f"/supply/{bundle_name}",
            f"/supply/{manifest_name}",
        ]
    )


def trivy_findings(paths: list[Path]) -> dict[str, list[dict[str, object]]]:
    findings: dict[str, list[dict[str, object]]] = {
        "critical": [], "fixable_high": [], "unfixed_high": [], "secrets": [],
        "misconfig_high_critical": [],
    }
    for path in paths:
        document = load(path)
        if (
            not isinstance(document.get("SchemaVersion"), int)
            or int(document["SchemaVersion"]) < 2
            or not isinstance(document.get("CreatedAt"), str)
            or not isinstance(document.get("ArtifactName"), str)
            or not document["ArtifactName"]
            or not isinstance(document.get("ArtifactType"), str)
            or not document["ArtifactType"]
            or "Results" in document
            and document["Results"] is not None
            and not isinstance(document["Results"], list)
        ):
            raise ValueError(f"Trivy document has an invalid or incomplete envelope: {path}")
        for result in document.get("Results") or []:
            if not isinstance(result, dict) or not isinstance(result.get("Target"), str):
                raise ValueError(f"Trivy result has an invalid target: {path}")
            for collection in ("Vulnerabilities", "Secrets", "Misconfigurations"):
                if collection in result and result[collection] is not None and not isinstance(
                    result[collection], list
                ):
                    raise ValueError(f"Trivy {collection} is not a list: {path}")
            target = str(result.get("Target", path.name))
            for item in result.get("Vulnerabilities", []) or []:
                if not isinstance(item, dict):
                    raise ValueError(f"Trivy vulnerability is not an object: {path}")
                record = {
                    "source": path.name, "target": target,
                    "id": item.get("VulnerabilityID"), "package": item.get("PkgName"),
                    "installed": item.get("InstalledVersion"), "fixed": item.get("FixedVersion"),
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
                if not isinstance(item, dict):
                    raise ValueError(f"Trivy secret is not an object: {path}")
                findings["secrets"].append(
                    {"source": path.name, "target": target, "rule_id": item.get("RuleID")}
                )
            for item in result.get("Misconfigurations", []) or []:
                if not isinstance(item, dict):
                    raise ValueError(f"Trivy misconfiguration is not an object: {path}")
                if str(item.get("Severity", "UNKNOWN")).upper() in {
                    "HIGH", "CRITICAL"
                }:
                    findings["misconfig_high_critical"].append(
                        {"source": path.name, "target": target, "id": item.get("ID")}
                    )
    return findings


def sorted_file_digest(root: Path) -> tuple[str, int]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("checks cache contains a symbolic or special path")
        if path.is_file():
            entries.append(
                {"bytes": path.stat().st_size, "path": path.relative_to(root).as_posix(), "sha256": sha256(path)}
            )
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest(), len(entries)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--supply-dir", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--signature-bundle", type=Path, required=True)
    parser.add_argument("--trivy-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    supply = args.supply_dir.resolve()
    bundle = args.bundle_dir.resolve()
    manifest = load(args.manifest, MAX_MANIFEST_BYTES)
    release_schema = load(repo / "contracts/supply-chain/v1/central-inference-cpu-release.schema.json")
    release_errors = sorted(
        Draft202012Validator(release_schema, format_checker=FormatChecker()).iter_errors(manifest),
        key=lambda error: list(error.absolute_path),
    )
    if release_errors:
        raise ValueError(
            "\n".join(
                f"release manifest schema error at {list(error.absolute_path)}: {error.message}"
                for error in release_errors
            )
        )

    tools = load(repo / "deploy/supply-chain/tools.lock.json")
    policy = load(repo / "deploy/supply-chain/trivy-policy.json")
    trust = repo / "contracts/trust/v1"
    cosign_image = str(tools["tools"]["cosign"]["image"])
    positive = cosign_verify(
        cosign_image, supply, trust, args.manifest.name, args.signature_bundle.name, "cosign.pub"
    )
    mutated = supply / "release-manifest.mutated-negative.json"
    wrong_trust: Path | None = None
    try:
        changed = dict(manifest)
        changed["release_id"] = str(manifest["release_id"]) + "-tampered"
        mutated.write_text(json.dumps(changed, sort_keys=True) + "\n", encoding="utf-8")
        mutated_result = cosign_verify(
            cosign_image, supply, trust, mutated.name, args.signature_bundle.name, "cosign.pub"
        )
        wrong_trust = Path(tempfile.mkdtemp(prefix="masi-inf-wrong-trust-", dir=supply))
        shutil.copyfile(
            repo / "testkit/fixtures/supply-chain/wrong-publisher-cosign.pub",
            wrong_trust / "wrong.pub",
        )
        wrong_result = cosign_verify(
            cosign_image, supply, wrong_trust, args.manifest.name,
            args.signature_bundle.name, "wrong.pub"
        )
    finally:
        mutated.unlink(missing_ok=True)
        if wrong_trust is not None:
            shutil.rmtree(wrong_trust, ignore_errors=True)
    key_policy = load(trust / "cosign-key-policy.json")
    revocations = load(trust / "revoked-keys.json")
    public_key_digest = sha256(trust / "cosign.pub")
    signature_checks = {
        "signature_valid": positive.returncode == 0,
        "mutated_manifest_rejected": mutated_result.returncode != 0,
        "wrong_publisher_rejected": wrong_result.returncode != 0,
        "public_key_exact": public_key_digest == key_policy.get("public_key_sha256"),
        "public_key_not_revoked": public_key_digest
        not in {"sha256:" + str(value).removeprefix("sha256:") for value in revocations["revoked_sha256_fingerprints"]},
        "manifest_trust_exact": manifest["trust"]["public_key_digest"] == public_key_digest,
    }

    sbom_paths = sorted(supply.glob("*.spdx.json"))
    sbom_documents = {path.name: load(path) for path in sbom_paths}
    sbom_checks = {
        "image_and_source_present": set(sbom_documents) >= {"image.spdx.json", "source.spdx.json"},
        "all_spdx": all(str(value.get("spdxVersion", "")).startswith("SPDX-") for value in sbom_documents.values()),
        "all_have_packages": all(isinstance(value.get("packages"), list) and bool(value["packages"]) for value in sbom_documents.values()),
        "image_subject_exact": sbom_documents.get("image.spdx.json", {}).get("name") == "/input/inference-image.tar",
        "source_subject_exact": sbom_documents.get("source.spdx.json", {}).get("name") == "/source",
    }

    scan_paths = sorted(supply.glob("trivy-*.json"))
    findings = trivy_findings(scan_paths)
    db_meta_path = args.trivy_cache / "db/metadata.json"
    java_meta_path = args.trivy_cache / "java-db/metadata.json"
    now = datetime.now(timezone.utc)
    db_age = int((now - parse_time(str(load(db_meta_path)["DownloadedAt"]))).total_seconds())
    java_age = int((now - parse_time(str(load(java_meta_path)["DownloadedAt"]))).total_seconds())
    checks_digest, checks_count = sorted_file_digest(args.trivy_cache / "policy/content")
    checks_lock = tools["trivy_checks_bundle"]
    checks_meta = load(args.trivy_cache / "policy/metadata.json")
    config_log = (supply / "trivy-config.log").read_text(encoding="utf-8")
    scan_checks = {
        "required_documents": {path.name for path in scan_paths} >= {
            "trivy-image.json", "trivy-config.json", "trivy-source-secret.json"
        },
        "database_present": (args.trivy_cache / "db/trivy.db").is_file(),
        "database_fresh": -300 <= db_age <= int(policy["database_max_age_seconds"]),
        "java_database_present": (args.trivy_cache / "java-db/trivy-java.db").is_file(),
        "java_database_fresh": -300 <= java_age <= int(policy["database_max_age_seconds"]),
        "checks_reference_exact": checks_meta.get("Digest") == str(checks_lock["reference"]).rsplit("@", 1)[-1],
        "checks_content_exact": checks_digest == checks_lock.get("content_digest"),
        "checks_count_exact": checks_count == checks_lock.get("file_count"),
        "config_used_offline_cache": "loading from existing cache" in config_log,
        "config_no_embedded_fallback": "Falling back to embedded checks" not in config_log,
        "config_no_download": "Downloading the checks bundle" not in config_log,
        "no_secrets": not findings["secrets"],
        "no_critical": not findings["critical"],
        "no_fixable_high": not findings["fixable_high"],
        "no_high_critical_misconfiguration": not findings["misconfig_high_critical"],
    }

    provenance = load(supply / str(manifest["provenance"]["path"]))
    manifest_subjects = {
        (item["name"], "sha256:" + item["digest"]["sha256"]) for item in manifest["subjects"]
    }
    provenance_subjects = {
        (item["name"], str(item["digest"]).removeprefix("sha256:"))
        if isinstance(item.get("digest"), str)
        else (item["name"], "sha256:" + item["digest"]["sha256"])
        for item in provenance["subject"]
    }
    provenance_checks = {
        "statement_exact": provenance.get("_type") == "https://in-toto.io/Statement/v1",
        "predicate_exact": provenance.get("predicateType") == "https://slsa.dev/provenance/v1",
        "subjects_exact": provenance_subjects == manifest_subjects,
        "digest_exact": manifest["provenance"]["digest"] == sha256(supply / manifest["provenance"]["path"]),
        "source_revision_exact": provenance["predicate"]["buildDefinition"]["externalParameters"]["source_revision"] == manifest["source_revision"],
        "source_tree_exact": provenance["predicate"]["buildDefinition"]["externalParameters"]["source_tree_digest"] == manifest["source_tree_digest"],
    }

    offline = load(supply / str(manifest["offline_rebuild"]["path"]))
    offline_checks = {
        "network_none": offline.get("network") == "none",
        "clean_snapshot": offline.get("clean_snapshot") is True,
        "binary_digest_match": offline.get("binary_digest_match") is True,
        "source_tree_exact": offline.get("source_tree_digest") == manifest["source_tree_digest"],
        "image_binary_exact": offline.get("image_binary_digest") == sha256(supply / "masi_inference_gateway.image.bin"),
        "rebuilt_binary_exact": offline.get("rebuilt_binary_digest") == sha256(supply / "masi_inference_gateway.offline-rebuild.bin"),
        "result_digest_exact": manifest["offline_rebuild"]["digest"] == sha256(supply / manifest["offline_rebuild"]["path"]),
    }

    registry_path = repo / "contracts/supply-chain/v1/central-inference-cpu-components.json"
    notices_path = repo / "contracts/supply-chain/v1/central-inference-cpu-third-party-notices.json"
    registry = load(registry_path)
    notices = load(notices_path)
    registry_schema = load(repo / "contracts/supply-chain/v1/schema.json")
    inventory_checks = {
        "registry_schema": not list(Draft202012Validator(registry_schema).iter_errors(registry)),
        "module_exact": registry.get("module") == "central-inference" and notices.get("module") == "central-inference",
        "notices_cover_registry": {item["name"] for item in registry["components"]}
        == {item["component"] for item in notices["notices"]},
        "component_names_unique": len({item["name"] for item in registry["components"]}) == len(registry["components"]),
        "no_runtime_download": all(item.get("runtime_download") is False for item in registry["components"]),
        "component_digest_exact": manifest["inventory"]["component_registry"]["digest"] == sha256(registry_path),
        "notices_digest_exact": manifest["inventory"]["third_party_notices"]["digest"] == sha256(notices_path),
    }
    for group in ("sboms", "scans", "evidence_inputs"):
        for item in manifest["inventory"][group]:
            path = supply / item["path"]
            inventory_checks[f"{group}:{item['path']}"] = (
                safe_relative(item["path"])
                and path.is_file()
                and not path.is_symlink()
                and path.stat().st_size == item["bytes"]
                and sha256(path) == item["digest"]
            )

    declared = {item["path"]: item for item in manifest["offline_bundle"]["files"]}
    actual = {
        path.relative_to(bundle).as_posix(): path
        for path in bundle.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    bundle_checks = {
        "file_set_exact": set(declared) == set(actual),
        "all_files_exact": all(
            safe_relative(relative)
            and path.stat().st_size == declared[relative]["bytes"]
            and sha256(path) == declared[relative]["digest"]
            for relative, path in actual.items()
            if relative in declared
        ),
        "checksums_digest_exact": manifest["offline_bundle"]["checksums_digest"] == sha256(bundle / "SHA256SUMS"),
        "tar_archives_safe": all(safe_tar_archive(path) for relative, path in actual.items() if relative.endswith(".tar")),
        "source_tree_exact": sha256(bundle / "source-tree.tar") == manifest["source_tree_digest"],
    }

    image_inspect = json.loads((supply / "image-config.json").read_text(encoding="utf-8"))
    image_doc = image_inspect[0] if isinstance(image_inspect, list) and len(image_inspect) == 1 else {}
    labels = image_doc.get("Config", {}).get("Labels", {}) if isinstance(image_doc, dict) else {}
    tool_checks = {
        "tools_lock_exact": manifest.get("tools") == tools.get("tools"),
        "release_image_config_exact": sha256(supply / "image-archive-config.json")
        == manifest["image_config_digest"],
        # Docker's containerd image store exposes the imported OCI manifest as
        # `.Id`; the archive config bytes are independently checked above.
        "release_image_manifest_id_exact": image_doc.get("Id")
        == manifest["image_manifest_digest"],
        "release_image_repo_digest_exact": any(
            str(value).endswith("@" + manifest["image_manifest_digest"])
            for value in image_doc.get("RepoDigests", [])
        ),
        "source_revision_label_exact": labels.get("org.opencontainers.image.revision") == manifest["source_revision"],
        "source_tree_label_exact": labels.get("io.masi-nids.source-tree.digest") == manifest["source_tree_digest"],
        "builder_base_label_exact": labels.get("io.masi-nids.builder-base.digest") == manifest["base_images"]["builder"].rsplit("@", 1)[-1],
        "runtime_base_label_exact": labels.get("io.masi-nids.runtime-base.digest") == manifest["base_images"]["runtime"].rsplit("@", 1)[-1],
    }
    for name, value in tools["tools"].items():
        inspected = run(["docker", "image", "inspect", str(value["image"]), "--format", "{{json .RepoDigests}}"])
        try:
            repo_digests = json.loads(inspected.stdout) if inspected.returncode == 0 else []
        except json.JSONDecodeError:
            repo_digests = []
        tool_checks[f"{name}_image_exact"] = isinstance(repo_digests, list) and str(
            value["image"]
        ) in repo_digests

    safety = load(supply / "input-safety.json")
    safety_checks = {
        "input_safety_passed": safety.get("result") == "PASS",
        "all_negative_cases": set(safety.get("rejected_cases", []))
        >= {"symlink", "path-traversal", "tar-traversal", "duplicate", "special-file"},
    }
    hard_checks = {
        **signature_checks, **sbom_checks, **scan_checks, **provenance_checks,
        **offline_checks, **inventory_checks, **bundle_checks, **tool_checks, **safety_checks,
    }
    failures = sorted(name.upper().replace(":", "_").replace("-", "_") for name, passed in hard_checks.items() if not passed)
    holds: list[str] = []
    if findings["unfixed_high"]:
        holds.append("UNFIXED_HIGH_REQUIRES_OWNER_EXCEPTION")
    if manifest.get("working_tree_dirty") is not False:
        holds.append("DIRTY_WORKTREE_NOT_RELEASE_BASELINE")
    result = "FAIL" if failures else "HOLD" if holds else "PASS"
    verification = {
        "schema_version": "central-inference-supply-verification/v1",
        "test_id": "SEC-SUPPLY-001",
        "run_id": str(manifest["release_id"]),
        "module": "MOD-INF-001",
        "requirement_ids": ["MOD-INF-001", "ARCH-REUSE-001", "SEC-SUPPLY-001", "TEST-010"],
        "level": "MODULE", "applicability": "APPLICABLE", "result": result,
        "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
        "source_revision": manifest["source_revision"],
        "source_tree_digest": manifest["source_tree_digest"],
        "working_tree_dirty": manifest["working_tree_dirty"],
        "working_tree_status_digest": manifest["working_tree_status_digest"],
        "manifest_digest": sha256(args.manifest),
        "offline_rebuild": {
            "attempted": True, "binary_digest_equal": bool(offline["binary_digest_match"]),
            "network_used": offline.get("network") != "none",
            "source_bundle_digest": manifest["source_tree_digest"],
            "rebuilt_binary_digest": offline["rebuilt_binary_digest"],
        },
        "sbom": {
            "generated": all(sbom_checks.values()), "format": "spdx",
            "path": "image.spdx.json", "digest": sha256(supply / "image.spdx.json"),
        },
        "vulnerability_scan": {
            "executed": (supply / "trivy-image.json").is_file(),
            "db_offline": scan_checks["database_present"] and scan_checks["database_fresh"],
            "policy_loaded": scan_checks["checks_content_exact"] and scan_checks["config_used_offline_cache"],
            "findings_path": "trivy-image.json", "findings_digest": sha256(supply / "trivy-image.json"),
        },
        "secret_scan": {"executed": scan_checks["required_documents"], "secrets_found": len(findings["secrets"])},
        "config_scan": {
            "executed": (supply / "trivy-config.json").is_file(),
            "policy_cache_offline": scan_checks["config_used_offline_cache"] and scan_checks["config_no_download"],
            "findings_path": "trivy-config.json",
        },
        "signature": {
            "valid": signature_checks["signature_valid"], "tamper_detected": False,
            "mutated_manifest_rejected": signature_checks["mutated_manifest_rejected"],
            "wrong_publisher_rejected": signature_checks["wrong_publisher_rejected"],
            "publisher": "local-bootstrap",
        },
        "release_manifest": {
            "digest": sha256(args.manifest), "closure_equal": all(provenance_checks.values()) and all(inventory_checks.values()),
            "max_bytes": MAX_MANIFEST_BYTES, "sha256sums_match": all(bundle_checks.values()),
        },
        "input_safety": {
            "symlink_rejected": safety_checks["all_negative_cases"],
            "path_traversal_rejected": safety_checks["all_negative_cases"],
            "tar_traversal_rejected": safety_checks["all_negative_cases"],
            "duplicate_rejected": safety_checks["all_negative_cases"],
            "special_file_rejected": safety_checks["all_negative_cases"],
        },
        "failure_reasons": failures, "hold_reasons": holds, "overall_module_complete": False,
    }
    evidence_schema = load(repo / "contracts/evidence/central-inference-supply/v1/schema.json")
    evidence_errors = sorted(
        Draft202012Validator(evidence_schema).iter_errors(verification),
        key=lambda error: list(error.absolute_path),
    )
    if evidence_errors:
        raise ValueError(
            "\n".join(
                f"supply evidence schema error at {list(error.absolute_path)}: {error.message}"
                for error in evidence_errors
            )
        )
    args.output.write_text(json.dumps(verification, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result == "PASS" else 2 if result == "HOLD" else 1


if __name__ == "__main__":
    raise SystemExit(main())
