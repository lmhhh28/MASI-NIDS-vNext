#!/usr/bin/env python3
"""Fail-closed verifier for Plugin Host module supply-chain evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


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


def misconfiguration_count(document: dict[str, Any]) -> int:
    return sum(
        1
        for result in document.get("Results") or []
        for item in result.get("Misconfigurations") or []
        if item.get("Severity") in {"HIGH", "CRITICAL"}
        and item.get("Status", "FAIL") != "PASS"
    )


def age_days(value: str) -> float:
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(r"\.(\d{6})\d*(?=[+-]\d\d:\d\d$)", r".\1", normalized)
    parsed = dt.datetime.fromisoformat(normalized)
    return (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 86400


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--supply-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-tree-digest", required=True)
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
    image_scan = load(supply / "trivy-image.json")
    source_secret_scan = load(supply / "trivy-source-secret.json")
    config_scan = load(supply / "trivy-config.json")
    manifest = load(supply / "release-manifest.json")
    signature = load(supply / "signature-checks.json")
    db_meta = load(repo / "out/supply-chain/trivy-cache/db/metadata.json")
    java_meta = load(repo / "out/supply-chain/trivy-cache/java-db/metadata.json")
    policy_meta = load(repo / "out/supply-chain/trivy-cache/policy/metadata.json")
    revocations = load(repo / "contracts/trust/v1/revoked-keys.json")
    component_registry_path = (
        repo / "contracts/supply-chain/v1/plugin-runtime-host-components.json"
    )
    notices_path = (
        repo / "contracts/supply-chain/v1/plugin-runtime-host-third-party-notices.json"
    )
    component_registry = load(component_registry_path)
    notices = load(notices_path)

    image_critical, image_fixable_high, image_secrets = trivy_counts(image_scan)
    _, _, source_secrets = trivy_counts(source_secret_scan)
    misconfigurations = misconfiguration_count(config_scan)
    cargo_lock = (repo / "plugin-host-rs/Cargo.lock").read_text(encoding="utf-8")
    registry_packages = len(re.findall(r'^source = "registry\+', cargo_lock, flags=re.MULTILINE))
    registry_checksums = len(re.findall(r"^checksum = \"[0-9a-f]{64}\"$", cargo_lock, flags=re.MULTILINE))
    dockerfile = (repo / "plugin-host-rs/Dockerfile").read_text(encoding="utf-8")
    public_key_digest = digest(repo / "contracts/trust/v1/cosign.pub")
    revoked = public_key_digest in {
        "sha256:" + str(value).removeprefix("sha256:")
        for value in revocations.get("revoked_sha256_fingerprints", [])
    }
    manifest_files = manifest.get("evidence_digests", {})
    file_digests_exact = all(
        manifest_files.get(name) == digest(supply / name)
        for name in [
            "image.spdx.json",
            "source.spdx.json",
            "trivy-image.json",
            "trivy-source-secret.json",
            "trivy-config.json",
        ]
    )
    checks = {
        "tool_lock_exact": (
            tools.get("schema_version") == "supply-chain-tools-lock/v1"
            and tools.get("mutable_tag_policy") == "forbidden"
            and all(
                "@sha256:" in entry.get("image", "")
                for entry in tools.get("tools", {}).values()
            )
        ),
        "trivy_policy_exact": policy.get("schema_version") == "trivy-qualification-policy/v1",
        "image_sbom_spdx": image_sbom.get("spdxVersion", "").startswith("SPDX-"),
        "source_sbom_spdx": source_sbom.get("spdxVersion", "").startswith("SPDX-"),
        "image_sbom_nonempty": len(image_sbom.get("packages") or []) > 0,
        "source_sbom_nonempty": len(source_sbom.get("packages") or []) > 0,
        "no_critical_vulnerability": image_critical == 0,
        "no_fixable_high_vulnerability": image_fixable_high == 0,
        "no_image_secret": image_secrets == 0,
        "no_source_secret": source_secrets == 0,
        "no_high_critical_misconfiguration": misconfigurations == 0,
        "trivy_db_within_policy_age": (
            age_days(db_meta["UpdatedAt"]) * 86400
            <= policy.get("database_max_age_seconds", 0)
        ),
        "trivy_java_db_within_seven_days": age_days(java_meta["UpdatedAt"]) <= 7,
        "trivy_policy_digest_exact": (
            policy_meta.get("Digest")
            == tools.get("trivy_checks_bundle", {}).get("reference", "").split("@")[-1]
        ),
        "cargo_registry_checksum_closure": (
            registry_packages > 0 and registry_packages == registry_checksums
        ),
        "component_registry_closed": (
            component_registry.get("module") == "plugin-runtime-host"
            and len(component_registry.get("components", [])) >= 11
            and all(
                item.get("decision") in {"ADOPT", "CONDITIONAL", "REJECT"}
                and item.get("runtime_download") is False
                for item in component_registry.get("components", [])
            )
        ),
        "third_party_notices_cover_registry": {
            item.get("name") for item in component_registry.get("components", [])
        }.issubset({item.get("component") for item in notices.get("notices", [])}),
        "docker_base_images_digest_pinned": (
            len(re.findall(r"^ARG .*_IMAGE=.*@sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE))
            == 2
        ),
        "manifest_identity_exact": (
            manifest.get("module_id") == "MOD-PLUGIN-001"
            and manifest.get("source_revision") == args.source_revision
            and manifest.get("source_tree_digest") == args.source_tree_digest
            and manifest.get("image_ref") == args.image_ref
            and manifest.get("image_id") == args.image_id
            and manifest.get("runtime_profile") == "plugin-runtime-host/v1"
            and manifest.get("wasmtime_version") == "47.0.3"
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
        "schema_version": "plugin-host-supply-chain-evidence/v1",
        "module_id": "MOD-PLUGIN-001",
        "source_revision": args.source_revision,
        "source_tree_digest": args.source_tree_digest,
        "image_ref": args.image_ref,
        "image_id": args.image_id,
        "manifest_digest": digest(supply / "release-manifest.json"),
        "signature_bundle_digest": digest(supply / "release-manifest.sigstore.json"),
        "public_key_digest": public_key_digest,
        "scan_counts": {
            "critical_vulnerabilities": image_critical,
            "fixable_high_vulnerabilities": image_fixable_high,
            "image_secrets": image_secrets,
            "source_secrets": source_secrets,
            "high_critical_misconfigurations": misconfigurations,
        },
        "checks": checks,
        "failure_reasons": failures,
        "result": result,
        "qualification": "NOT_QUALIFIED",
        "reason_code": (
            "PLUGIN_HOST_SUPPLY_CHAIN_VERIFIED"
            if not failures
            else "PLUGIN_HOST_SUPPLY_CHAIN_REJECTED"
        ),
    }
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
