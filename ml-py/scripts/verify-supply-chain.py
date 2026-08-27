#!/usr/bin/env python3
"""Fail-closed Offline ML supply-chain evidence verifier."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"invalid evidence file: {path}")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return "sha256:" + value.hexdigest()


def trivy_counts(document: dict[str, Any]) -> tuple[int, int, int]:
    critical = fixable_high = secrets = 0
    for result in document.get("Results") or []:
        for vulnerability in result.get("Vulnerabilities") or []:
            if vulnerability.get("Severity") == "CRITICAL":
                critical += 1
            if vulnerability.get("Severity") == "HIGH" and vulnerability.get("FixedVersion"):
                fixable_high += 1
        secrets += len(result.get("Secrets") or [])
    return critical, fixable_high, secrets


def misconfiguration_count(document: dict[str, Any]) -> int:
    return sum(
        1
        for result in document.get("Results") or []
        for item in result.get("Misconfigurations") or []
        if item.get("Severity") in {"HIGH", "CRITICAL"} and item.get("Status", "FAIL") != "PASS"
    )


def age_days(value: str) -> float:
    normalized = re.sub(r"\.(\d{6})\d*(?=[+-]\d\d:\d\d$)", r".\1", value.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(normalized)).total_seconds() / 86400


def dependency_closure(pyproject_path: Path, lock_path: Path) -> tuple[bool, int]:
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    requirements = cast(list[str], pyproject["project"]["dependencies"])
    packages = cast(list[dict[str, Any]], lock.get("package", []))
    locked = {str(package.get("name")): str(package.get("version")) for package in packages}
    direct_exact = bool(requirements) and all(
        "==" in requirement
        and locked.get(requirement.split("==", 1)[0].strip().lower().replace("_", "-"))
        == requirement.split("==", 1)[1].strip()
        for requirement in requirements
    )
    hashes_closed = True
    for package in packages:
        source = cast(dict[str, Any], package.get("source") or {})
        if "registry" not in source:
            continue
        artifacts: list[dict[str, Any]] = []
        if isinstance(package.get("sdist"), dict):
            artifacts.append(cast(dict[str, Any], package["sdist"]))
        if isinstance(package.get("wheels"), list):
            artifacts.extend(item for item in package["wheels"] if isinstance(item, dict))
        if not artifacts or any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("hash", ""))) for item in artifacts
        ):
            hashes_closed = False
            break
    return direct_exact and hashes_closed and len(packages) > len(requirements), len(packages)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--supply-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-tree-digest", required=True)
    parser.add_argument("--working-tree-status-digest", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    supply = arguments.supply_dir.resolve(strict=True)
    if arguments.output.exists() or arguments.output.is_symlink():
        raise SystemExit("refusing to overwrite supply verification")

    tools = load(repo / "deploy/supply-chain/tools.lock.json")
    policy = load(repo / "deploy/supply-chain/trivy-policy.json")
    image_sbom = load(supply / "image.spdx.json")
    source_sbom = load(supply / "source.spdx.json")
    runtime_libraries_sbom = load(supply / "runtime-libraries-source.spdx.json")
    image_scan = load(supply / "trivy-image.json")
    runtime_libraries_scan = load(supply / "trivy-runtime-libraries-source.json")
    source_scan = load(supply / "trivy-source-secret.json")
    config_scan = load(supply / "trivy-config.json")
    manifest = load(supply / "release-manifest.json")
    signature = load(supply / "signature-checks.json")
    db_meta = load(repo / "out/supply-chain/trivy-cache/db/metadata.json")
    java_meta = load(repo / "out/supply-chain/trivy-cache/java-db/metadata.json")
    policy_meta = load(repo / "out/supply-chain/trivy-cache/policy/metadata.json")
    revocations = load(repo / "contracts/trust/v1/revoked-keys.json")
    registry_path = repo / "contracts/supply-chain/v1/offline-ml-components.json"
    notices_path = repo / "contracts/supply-chain/v1/offline-ml-third-party-notices.json"
    registry = load(registry_path)
    notices = load(notices_path)
    registry_schema = load(repo / "contracts/supply-chain/v1/schema.json")
    runtime_profile = load(repo / "contracts/profiles/v1/offline-ml-runtime.json")

    image_critical, image_fixable_high, image_secrets = trivy_counts(image_scan)
    libraries_critical, libraries_fixable_high, libraries_secrets = trivy_counts(runtime_libraries_scan)
    _, _, source_secrets = trivy_counts(source_scan)
    misconfigurations = misconfiguration_count(config_scan)
    lock_closed, package_count = dependency_closure(repo / "ml-py/pyproject.toml", repo / "ml-py/uv.lock")
    public_key_digest = digest(repo / "contracts/trust/v1/cosign.pub")
    revoked = public_key_digest in {
        "sha256:" + str(value).removeprefix("sha256:") for value in revocations.get("revoked_sha256_fingerprints", [])
    }
    evidence_names = (
        "image.spdx.json",
        "source.spdx.json",
        "runtime-libraries-source.spdx.json",
        "trivy-image.json",
        "trivy-runtime-libraries-source.json",
        "trivy-source-secret.json",
        "trivy-config.json",
    )
    manifest_files = cast(dict[str, str], manifest.get("evidence_digests", {}))
    registry_names = {item.get("name") for item in registry.get("components", [])}
    notice_names = {item.get("component") for item in notices.get("notices", [])}
    dockerfile = (repo / "ml-py/Dockerfile").read_text(encoding="utf-8")
    python_builder_references = re.findall(
        r"^ARG PYTHON_BUILDER_IMAGE=([^\s]+@sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE
    )
    runtime_libraries_references = re.findall(
        r"^ARG RUNTIME_LIBRARIES_IMAGE=([^\s]+@sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE
    )
    runtime_references = re.findall(r"^ARG RUNTIME_IMAGE=([^\s]+@sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE)
    registry_by_name = {str(item.get("name")): item for item in registry.get("components", [])}
    builder_profile = cast(dict[str, Any], runtime_profile.get("python_builder_image", {}))
    libraries_profile = cast(dict[str, Any], runtime_profile.get("runtime_libraries_image", {}))
    base_profile = cast(dict[str, Any], runtime_profile.get("runtime_base_image", {}))
    library_files = {
        str(item.get("component")): item for item in cast(list[dict[str, Any]], libraries_profile.get("files", []))
    }
    expected_library_digests = {
        "zlib": "sha256:cd0d4cbe528e1d83875605c2aa10f0a9ea2d34a055d24db96642e6f78ad7206d",
        "libffi": "sha256:baae077ca48493f45210ce490bb281403b1d68362adef95495f5e0bc28e70eaa",
    }
    manifest_library_digests = cast(dict[str, str], manifest.get("runtime_library_digests", {}))
    image_results = cast(list[dict[str, Any]], image_scan.get("Results") or [])
    image_packages = cast(list[dict[str, Any]], image_sbom.get("packages") or [])
    image_result_types = {str(result.get("Type")) for result in image_results}
    image_sbom_packages = {(str(item.get("name")), str(item.get("versionInfo"))) for item in image_packages}
    checks = {
        "tool_lock_exact": tools.get("mutable_tag_policy") == "forbidden"
        and all("@sha256:" in item.get("image", "") for item in cast(dict[str, Any], tools["tools"]).values()),
        "trivy_policy_exact": policy.get("schema_version") == "trivy-qualification-policy/v1",
        "image_sbom_spdx": str(image_sbom.get("spdxVersion", "")).startswith("SPDX-"),
        "source_sbom_spdx": str(source_sbom.get("spdxVersion", "")).startswith("SPDX-"),
        "runtime_libraries_sbom_spdx": str(runtime_libraries_sbom.get("spdxVersion", "")).startswith("SPDX-"),
        "image_sbom_nonempty": len(image_sbom.get("packages") or []) > 0,
        "source_sbom_nonempty": len(source_sbom.get("packages") or []) > 0,
        "runtime_libraries_sbom_nonempty": len(runtime_libraries_sbom.get("packages") or []) > 0,
        "final_sbom_contains_selected_libraries": {("zlib", "1.3.2-r4"), ("libffi", "3.8.0-r0")}.issubset(
            image_sbom_packages
        ),
        "no_critical_vulnerability": image_critical == 0,
        "no_fixable_high_vulnerability": image_fixable_high == 0,
        "no_runtime_library_source_critical_vulnerability": libraries_critical == 0,
        "no_runtime_library_source_fixable_high_vulnerability": libraries_fixable_high == 0,
        "no_runtime_library_source_secret": libraries_secrets == 0,
        "no_image_secret": image_secrets == 0,
        "no_source_secret": source_secrets == 0,
        "no_high_critical_misconfiguration": misconfigurations == 0,
        "trivy_db_fresh": age_days(str(db_meta["UpdatedAt"])) * 86400 <= int(policy["database_max_age_seconds"]),
        "trivy_java_db_fresh": age_days(str(java_meta["UpdatedAt"])) <= 7,
        "trivy_policy_digest_exact": policy_meta.get("Digest")
        == cast(dict[str, Any], tools["trivy_checks_bundle"])["reference"].split("@")[-1],
        "uv_lock_hash_closure": lock_closed,
        "uv_lock_digest_exact": manifest.get("lock_digest") == digest(repo / "ml-py/uv.lock"),
        "component_registry_schema": not list(Draft202012Validator(registry_schema).iter_errors(registry)),
        "component_registry_closed": registry.get("module") == "offline-ml"
        and len(registry.get("components", [])) >= 12
        and all(item.get("runtime_download") is False for item in registry.get("components", [])),
        "base_components_registry_exact": registry_by_name.get("Python 3.12.13 slim-bookworm offline builder", {}).get(
            "digest"
        )
        == "sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"
        and registry_by_name.get("Distroless cc Debian 12 OCI runtime base", {}).get("digest")
        == "sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77"
        and registry_by_name.get("Chainguard Wolfi runtime-library source image", {}).get("digest")
        == "sha256:d812438658b47b73cb4c089f4cca09bca1ba50f6cd1843133864ee074d9ec49b"
        and registry_by_name.get("Wolfi zlib runtime library", {}).get("digest") == expected_library_digests["zlib"]
        and registry_by_name.get("Wolfi libffi runtime library", {}).get("digest")
        == expected_library_digests["libffi"],
        "runtime_libraries_profile_exact": libraries_profile.get("packaged_python_copied") is False
        and libraries_profile.get("selected_files_only") is True
        and {name: item.get("digest") for name, item in library_files.items()} == expected_library_digests,
        "third_party_notices_cover_registry": registry_names.issubset(notice_names),
        "docker_bases_digest_pinned": len(python_builder_references) == 1
        and len(runtime_libraries_references) == 1
        and len(runtime_references) == 1
        and python_builder_references[0] == builder_profile.get("reference")
        and runtime_libraries_references[0] == libraries_profile.get("reference")
        and runtime_references[0] == base_profile.get("reference"),
        "final_runtime_os_debian_only": "debian" in image_result_types and "wolfi" not in image_result_types,
        "manifest_identity_exact": manifest.get("module_id") == "MOD-ML-001"
        and manifest.get("source_revision") == arguments.source_revision
        and manifest.get("source_tree_digest") == arguments.source_tree_digest
        and manifest.get("working_tree_status_digest") == arguments.working_tree_status_digest
        and manifest.get("image_ref") == arguments.image_ref
        and manifest.get("image_id") == arguments.image_id
        and manifest.get("runtime_profile") == "offline-ml-runtime/v1"
        and manifest.get("python_builder_image_digest") == builder_profile.get("digest")
        and manifest.get("runtime_libraries_image_digest") == libraries_profile.get("digest")
        and manifest.get("runtime_base_image_digest") == base_profile.get("digest")
        and manifest_library_digests == expected_library_digests
        and manifest.get("runtime_base_python_fallback") is False
        and manifest.get("runtime_download") is False
        and manifest.get("component_registry_digest") == digest(registry_path)
        and manifest.get("third_party_notices_digest") == digest(notices_path),
        "manifest_evidence_digests_exact": all(
            manifest_files.get(name) == digest(supply / name) for name in evidence_names
        ),
        "cosign_positive": signature.get("positive") is True,
        "cosign_tamper_negative": signature.get("tampered_rejected") is True,
        "cosign_wrong_publisher_negative": signature.get("wrong_publisher_rejected") is True,
        "cosign_key_not_revoked": not revoked,
        "offline_tools_network_none": signature.get("tools_network_none") is True,
    }
    failures = sorted(name for name, passed in checks.items() if not passed)
    evidence = {
        "schema_version": "offline-ml-supply-chain-evidence/v1",
        "module_id": "MOD-ML-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "source_revision": arguments.source_revision,
        "source_tree_digest": arguments.source_tree_digest,
        "working_tree_status_digest": arguments.working_tree_status_digest,
        "working_tree_dirty": bool(manifest.get("working_tree_dirty")),
        "image_ref": arguments.image_ref,
        "image_id": arguments.image_id,
        "manifest_digest": digest(supply / "release-manifest.json"),
        "signature_bundle_digest": digest(supply / "release-manifest.sigstore.json"),
        "public_key_digest": public_key_digest,
        "dependency_packages": package_count,
        "scan_counts": {
            "critical_vulnerabilities": image_critical,
            "fixable_high_vulnerabilities": image_fixable_high,
            "runtime_library_source_critical_vulnerabilities": libraries_critical,
            "runtime_library_source_fixable_high_vulnerabilities": libraries_fixable_high,
            "runtime_library_source_secrets": libraries_secrets,
            "image_secrets": image_secrets,
            "source_secrets": source_secrets,
            "high_critical_misconfigurations": misconfigurations,
        },
        "checks": checks,
        "failure_reasons": failures,
        "result": "PASS" if not failures else "FAIL",
        "qualification": "NOT_QUALIFIED",
    }
    arguments.output.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
