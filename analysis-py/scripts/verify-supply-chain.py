#!/usr/bin/env python3
"""Fail-closed verifier for Analysis Plugin supply-chain evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"evidence is not a regular no-symlink file: {path}")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"JSON evidence exceeds 64 MiB: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def trivy_counts(document: dict[str, Any]) -> tuple[int, int, int]:
    critical = 0
    fixable_high = 0
    secrets = 0
    for result in document.get("Results") or []:
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = vulnerability.get("Severity")
            if severity == "CRITICAL":
                critical += 1
            if severity == "HIGH" and vulnerability.get("FixedVersion"):
                fixable_high += 1
        secrets += len(result.get("Secrets") or [])
    return critical, fixable_high, secrets


def selected_vulnerability_counts(document: dict[str, Any], package_names: set[str]) -> tuple[int, int]:
    critical = 0
    fixable_high = 0
    for result in document.get("Results") or []:
        for vulnerability in result.get("Vulnerabilities") or []:
            if str(vulnerability.get("PkgName")) not in package_names:
                continue
            if vulnerability.get("Severity") == "CRITICAL":
                critical += 1
            if vulnerability.get("Severity") == "HIGH" and vulnerability.get("FixedVersion"):
                fixable_high += 1
    return critical, fixable_high


def misconfiguration_count(document: dict[str, Any]) -> int:
    return sum(
        1
        for result in document.get("Results") or []
        for item in result.get("Misconfigurations") or []
        if item.get("Severity") in {"HIGH", "CRITICAL"} and item.get("Status", "FAIL") != "PASS"
    )


def age_days(value: str) -> float:
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(r"\.(\d{6})\d*(?=[+-]\d\d:\d\d$)", r".\1", normalized)
    parsed = dt.datetime.fromisoformat(normalized)
    return (dt.datetime.now(dt.UTC) - parsed).total_seconds() / 86400


def dependency_closure(pyproject_path: Path, lock_path: Path) -> tuple[bool, int]:
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    requirements = pyproject["project"]["dependencies"]
    if not requirements or any("==" not in requirement for requirement in requirements):
        return False, 0
    packages = lock.get("package", [])
    locked = {str(package.get("name")): str(package.get("version")) for package in packages}
    direct_exact = all(
        locked.get(requirement.split("==", 1)[0].strip().lower().replace("_", "-")) == requirement.split("==", 1)[1].strip() for requirement in requirements
    )
    hashes_closed = True
    for package in packages:
        source = package.get("source") or {}
        if "registry" not in source:
            continue
        artifacts: list[dict[str, Any]] = []
        sdist = package.get("sdist")
        if isinstance(sdist, dict):
            artifacts.append(sdist)
        wheels = package.get("wheels")
        if isinstance(wheels, list):
            artifacts.extend(item for item in wheels if isinstance(item, dict))
        if not artifacts or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("hash", ""))) for item in artifacts):
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
    args = parser.parse_args()

    repo = args.repo.resolve(strict=True)
    supply = args.supply_dir.resolve(strict=True)
    output = args.output
    if output.exists() or output.is_symlink():
        raise SystemExit("refusing to overwrite supply verification")
    for candidate in (repo, supply, output.parent.resolve(strict=True)):
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            if current.is_symlink():
                raise SystemExit(f"symbolic path component rejected: {current}")

    tools = load(repo / "deploy/supply-chain/tools.lock.json")
    policy = load(repo / "deploy/supply-chain/trivy-policy.json")
    image_sbom = load(supply / "image.spdx.json")
    source_sbom = load(supply / "source.spdx.json")
    python_builder_sbom = load(supply / "python-builder-source.spdx.json")
    image_scan = load(supply / "trivy-image.json")
    python_builder_scan = load(supply / "trivy-python-builder-source.json")
    source_secret_scan = load(supply / "trivy-source-secret.json")
    config_scan = load(supply / "trivy-config.json")
    manifest = load(supply / "release-manifest.json")
    signature = load(supply / "signature-checks.json")
    db_meta = load(repo / "out/supply-chain/trivy-cache/db/metadata.json")
    java_meta = load(repo / "out/supply-chain/trivy-cache/java-db/metadata.json")
    policy_meta = load(repo / "out/supply-chain/trivy-cache/policy/metadata.json")
    revocations = load(repo / "contracts/trust/v1/revoked-keys.json")
    component_registry_path = repo / "contracts/supply-chain/v1/analysis-plugin-components.json"
    notices_path = repo / "contracts/supply-chain/v1/analysis-plugin-third-party-notices.json"
    component_registry = load(component_registry_path)
    notices = load(notices_path)
    schema = load(repo / "contracts/supply-chain/v1/schema.json")
    runtime_profile = load(repo / "contracts/profiles/v1/analysis-agent-runtime.json")

    image_critical, image_fixable_high, image_secrets = trivy_counts(image_scan)
    _, _, python_builder_secrets = trivy_counts(python_builder_scan)
    builder_selected_critical, builder_selected_fixable_high = selected_vulnerability_counts(python_builder_scan, {"libssl3"})
    _, _, source_secrets = trivy_counts(source_secret_scan)
    misconfigurations = misconfiguration_count(config_scan)
    lock_closed, lock_package_count = dependency_closure(repo / "analysis-py/pyproject.toml", repo / "analysis-py/uv.lock")
    dockerfile = (repo / "analysis-py/Dockerfile").read_text(encoding="utf-8")
    public_key_digest = digest(repo / "contracts/trust/v1/cosign.pub")
    revoked = public_key_digest in {"sha256:" + str(value).removeprefix("sha256:") for value in revocations.get("revoked_sha256_fingerprints", [])}
    manifest_files = manifest.get("evidence_digests", {})
    evidence_names = [
        "image.spdx.json",
        "source.spdx.json",
        "python-builder-source.spdx.json",
        "trivy-image.json",
        "trivy-python-builder-source.json",
        "trivy-source-secret.json",
        "trivy-config.json",
    ]
    file_digests_exact = all(manifest_files.get(name) == digest(supply / name) for name in evidence_names)
    schema_errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(component_registry))
    registry_names = {item.get("name") for item in component_registry.get("components", [])}
    notice_names = {item.get("component") for item in notices.get("notices", [])}
    registry_by_name = {str(item.get("name")): item for item in component_registry.get("components", [])}
    builder_profile = runtime_profile.get("python_builder_image") or {}
    base_profile = runtime_profile.get("runtime_base_image") or {}
    selected_profile = {str(item.get("component")): item for item in runtime_profile.get("selected_runtime_libraries") or []}
    expected_library_digests = {
        "libcrypto": "sha256:72db1b3de8b7dfbaba4c056135f408da555f9d5e137c82129478e07e769f8070",
        "libssl": "sha256:9aec161fdbc82d3e4280f5084843118939f1f4acc53c98ec963de03cfe812fad",
    }
    expected_runtime_versions = {
        "python": "3.12.13",
        "openssl": "OpenSSL 3.0.20 7 Apr 2026",
        "sqlite": "3.53.4",
        "zlib": "1.3.2",
    }
    manifest_library_digests = manifest.get("runtime_library_digests") or {}
    image_result_types = {str(result.get("Type")) for result in image_scan.get("Results") or []}
    checks = {
        "tool_lock_exact": (
            tools.get("schema_version") == "supply-chain-tools-lock/v1"
            and tools.get("mutable_tag_policy") == "forbidden"
            and all("@sha256:" in entry.get("image", "") for entry in tools.get("tools", {}).values())
        ),
        "trivy_policy_exact": policy.get("schema_version") == "trivy-qualification-policy/v1",
        "image_sbom_spdx": image_sbom.get("spdxVersion", "").startswith("SPDX-"),
        "source_sbom_spdx": source_sbom.get("spdxVersion", "").startswith("SPDX-"),
        "python_builder_sbom_spdx": python_builder_sbom.get("spdxVersion", "").startswith("SPDX-"),
        "image_sbom_nonempty": len(image_sbom.get("packages") or []) > 0,
        "source_sbom_nonempty": len(source_sbom.get("packages") or []) > 0,
        "python_builder_sbom_nonempty": len(python_builder_sbom.get("packages") or []) > 0,
        "no_critical_vulnerability": image_critical == 0,
        "no_fixable_high_vulnerability": image_fixable_high == 0,
        "no_python_builder_selected_library_critical_vulnerability": builder_selected_critical == 0,
        "no_python_builder_selected_library_fixable_high_vulnerability": builder_selected_fixable_high == 0,
        "no_python_builder_secret": python_builder_secrets == 0,
        "no_image_secret": image_secrets == 0,
        "no_source_secret": source_secrets == 0,
        "no_high_critical_misconfiguration": misconfigurations == 0,
        "trivy_db_within_policy_age": age_days(db_meta["UpdatedAt"]) * 86400 <= policy.get("database_max_age_seconds", 0),
        "trivy_java_db_within_seven_days": age_days(java_meta["UpdatedAt"]) <= 7,
        "trivy_policy_digest_exact": (policy_meta.get("Digest") == tools.get("trivy_checks_bundle", {}).get("reference", "").split("@")[-1]),
        "uv_lock_hash_closure": lock_closed,
        "uv_lock_digest_exact": manifest.get("lock_digest") == digest(repo / "analysis-py/uv.lock"),
        "component_registry_schema": not schema_errors,
        "component_registry_closed": (
            component_registry.get("module") == "analysis-plugin"
            and len(component_registry.get("components", [])) >= 14
            and all(
                item.get("decision") in {"ADOPT", "CONDITIONAL", "REJECT"} and item.get("runtime_download") is False
                for item in component_registry.get("components", [])
            )
        ),
        "base_components_registry_exact": (
            registry_by_name.get("Python 3.12.13 slim-bookworm offline builder", {}).get("digest") == builder_profile.get("digest")
            and registry_by_name.get("Chainguard Wolfi Python OCI runtime base", {}).get("digest") == base_profile.get("digest")
            and registry_by_name.get("Debian libcrypto runtime library", {}).get("digest") == expected_library_digests["libcrypto"]
            and registry_by_name.get("Debian libssl runtime library", {}).get("digest") == expected_library_digests["libssl"]
        ),
        "runtime_profile_exact": (
            builder_profile.get("runtime_layer") is False
            and base_profile.get("packaged_python_is_fallback") is False
            and {name: item.get("digest") for name, item in selected_profile.items()} == expected_library_digests
            and runtime_profile.get("effective_runtime_versions") == {key: expected_runtime_versions[key] for key in ("openssl", "sqlite", "zlib")}
        ),
        "third_party_notices_cover_registry": registry_names.issubset(notice_names),
        "docker_bases_digest_pinned": (
            re.findall(r"^ARG PYTHON_BUILDER_IMAGE=([^\s]+@sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE) == [builder_profile.get("reference")]
            and re.findall(r"^ARG RUNTIME_IMAGE=([^\s]+@sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE) == [base_profile.get("reference")]
        ),
        "final_runtime_os_wolfi_only": "wolfi" in image_result_types and "alpine" not in image_result_types,
        "manifest_identity_exact": (
            manifest.get("module_id") == "MOD-AGENT-001"
            and manifest.get("source_revision") == args.source_revision
            and manifest.get("source_tree_digest") == args.source_tree_digest
            and manifest.get("working_tree_status_digest") == args.working_tree_status_digest
            and manifest.get("image_ref") == args.image_ref
            and manifest.get("image_id") == args.image_id
            and manifest.get("runtime_profile") == "analysis-agent-runtime/v1"
            and manifest.get("python_version") == "3.12.13"
            and manifest.get("python_builder_image_digest") == builder_profile.get("digest")
            and manifest.get("runtime_base_image_digest") == base_profile.get("digest")
            and manifest_library_digests == expected_library_digests
            and manifest.get("runtime_versions") == expected_runtime_versions
            and manifest.get("runtime_base_python_fallback") is False
            and manifest.get("runtime_download") is False
            and manifest.get("component_registry_digest") == digest(component_registry_path)
            and manifest.get("third_party_notices_digest") == digest(notices_path)
        ),
        "manifest_evidence_digests_exact": file_digests_exact,
        "cosign_positive": signature.get("positive") is True,
        "cosign_tamper_negative": signature.get("tampered_rejected") is True,
        "cosign_wrong_publisher_negative": signature.get("wrong_publisher_rejected") is True,
        "cosign_key_not_revoked": not revoked,
        "offline_tools_network_none": signature.get("tools_network_none") is True,
        "runtime_download_forbidden": True,
    }
    failures = sorted(name for name, passed in checks.items() if not passed)
    result = "PASS" if not failures else "FAIL"
    evidence = {
        "schema_version": "analysis-supply-chain-evidence/v1",
        "module_id": "MOD-AGENT-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "source_revision": args.source_revision,
        "source_tree_digest": args.source_tree_digest,
        "working_tree_status_digest": args.working_tree_status_digest,
        "working_tree_dirty": bool(manifest.get("working_tree_dirty")),
        "image_ref": args.image_ref,
        "image_id": args.image_id,
        "manifest_digest": digest(supply / "release-manifest.json"),
        "signature_bundle_digest": digest(supply / "release-manifest.sigstore.json"),
        "public_key_digest": public_key_digest,
        "dependency_packages": lock_package_count,
        "scan_counts": {
            "critical_vulnerabilities": image_critical,
            "fixable_high_vulnerabilities": image_fixable_high,
            "python_builder_selected_library_critical_vulnerabilities": builder_selected_critical,
            "python_builder_selected_library_fixable_high_vulnerabilities": builder_selected_fixable_high,
            "python_builder_secrets": python_builder_secrets,
            "image_secrets": image_secrets,
            "source_secrets": source_secrets,
            "high_critical_misconfigurations": misconfigurations,
        },
        "checks": checks,
        "failure_reasons": failures,
        "result": result,
        "qualification": "NOT_QUALIFIED",
        "reason_code": "ANALYSIS_SUPPLY_CHAIN_VERIFIED" if not failures else "ANALYSIS_SUPPLY_CHAIN_REJECTED",
    }
    if not failures:
        evidence_schema = load(repo / "contracts/evidence/analysis-supply/v1/schema.json")
        evidence_errors = list(Draft202012Validator(evidence_schema, format_checker=FormatChecker()).iter_errors(evidence))
        if evidence_errors:
            raise SystemExit("supply evidence schema rejected: " + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in evidence_errors))
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
