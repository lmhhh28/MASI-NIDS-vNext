from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def parse_time(value: str) -> datetime:
    # Trivy metadata uses RFC3339 nanoseconds while datetime stores microseconds.
    # Truncate only excess fractional precision; timezone and instant stay exact
    # to the resolution needed by the one-second freshness calculation.
    normalized = re.sub(r"(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$)", r"\1", value).replace(
        "Z", "+00:00"
    )
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("supply-chain timestamps must include a timezone")
    return parsed


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=False, capture_output=True, text=True)


def cosign_verify(
    image: str,
    supply_dir: Path,
    trust_dir: Path,
    manifest_name: str,
    bundle_name: str,
    key_name: str,
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
            "--volume",
            f"{supply_dir}:/supply:ro",
            "--volume",
            f"{trust_dir}:/trust:ro",
            image,
            "verify-blob",
            "--private-infrastructure",
            "--key",
            f"/trust/{key_name}",
            "--bundle",
            f"/supply/{bundle_name}",
            f"/supply/{manifest_name}",
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
        document = load(path)
        results = document.get("Results", []) or []
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            target = str(result.get("Target", path.name))
            vulnerabilities = result.get("Vulnerabilities", []) or []
            if isinstance(vulnerabilities, list):
                for item in vulnerabilities:
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
            secrets = result.get("Secrets", []) or []
            if isinstance(secrets, list):
                for item in secrets:
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
            misconfigurations = result.get("Misconfigurations", []) or []
            if isinstance(misconfigurations, list):
                for item in misconfigurations:
                    if not isinstance(item, dict):
                        continue
                    severity = str(item.get("Severity", "UNKNOWN")).upper()
                    if severity in {"HIGH", "CRITICAL"}:
                        findings["misconfig_high_critical"].append(
                            {
                                "source": path.name,
                                "target": target,
                                "id": item.get("ID"),
                                "severity": severity,
                                "title": item.get("Title"),
                            }
                        )
    return findings


def extract_runner_package_records(
    document: dict[str, Any], package_names: set[str]
) -> dict[str, list[dict[str, object]]]:
    records: dict[str, list[dict[str, object]]] = {
        name: [] for name in package_names
    }
    packages = document.get("packages", [])
    if not isinstance(packages, list):
        return records
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = str(package.get("name", ""))
        if name not in records:
            continue
        references = package.get("externalRefs", [])
        if not isinstance(references, list):
            references = []
        purls = [
            str(reference.get("referenceLocator", ""))
            for reference in references
            if isinstance(reference, dict)
            and reference.get("referenceType") == "purl"
        ]
        records[name].append(
            {
                "version": str(package.get("versionInfo", "")),
                "purls": purls,
            }
        )
    return records


def runner_package_checks(
    records: dict[str, list[dict[str, object]]],
    expected_versions: dict[str, str],
    expected_distro: str,
) -> dict[str, bool]:
    versions_exact = all(
        records.get(name)
        and {str(record["version"]) for record in records[name]}
        == {expected_version}
        for name, expected_version in expected_versions.items()
    )
    distro_exact = all(
        records.get(name)
        and all(
            any(
                f"distro={expected_distro}" in str(purl)
                for purl in record.get("purls", [])
            )
            for record in records[name]
        )
        for name in expected_versions
    )
    return {
        "runner_packages_exact": bool(versions_exact),
        "runner_package_distro_exact": bool(distro_exact),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--supply-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--offline-result", type=Path, required=True)
    parser.add_argument("--trivy-cache", type=Path, required=True)
    parser.add_argument("--runtime-digest", required=True)
    parser.add_argument("--runner-digest", required=True)
    parser.add_argument("--compiler-digest", required=True)
    parser.add_argument("--runner-deps-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tools = load(args.repo / "deploy/supply-chain/tools.lock.json")
    policy = load(args.repo / "deploy/supply-chain/trivy-policy.json")
    registry_path = args.repo / "contracts/supply-chain/v1/p4-switch-components.json"
    registry = load(registry_path)
    registry_schema = load(args.repo / "contracts/supply-chain/v1/schema.json")
    registry_errors = list(Draft202012Validator(registry_schema).iter_errors(registry))
    runner_profile = load(
        args.repo / "contracts/profiles/v1/e2e-runner-compose.json"
    )
    traffic_profile = load(
        args.repo / "contracts/profiles/v1/p4-traffic-replay-bmv2-compose.json"
    )
    runner_environment = runner_profile["runner_environment"]
    expected_runner_packages = {
        "iproute2": str(runner_environment["iproute2"]["package_version"]),
        "tcpreplay": str(runner_environment["tcpreplay"]["package_version"]),
    }
    expected_runner_distro = (
        "alpine-" + str(runner_environment["distribution_version"])
    )
    manifest = load(args.manifest)
    offline = load(args.offline_result)
    provenance_path = args.supply_dir / str(manifest["provenance"]["path"])
    provenance = load(provenance_path)
    trust_dir = args.repo / "contracts/trust/v1"
    signing_config_path = (
        args.repo / "deploy/supply-chain/cosign-offline-signing-config.json"
    )
    signing_config = load(signing_config_path)
    cosign_image = str(tools["tools"]["cosign"]["image"])

    positive = cosign_verify(
        cosign_image,
        args.supply_dir,
        trust_dir,
        args.manifest.name,
        args.bundle.name,
        "cosign.pub",
    )
    mutated = args.supply_dir / "release-manifest.mutated-negative.json"
    mutated_document = dict(manifest)
    mutated_document["release_id"] = str(manifest["release_id"]) + "-mutated"
    mutated.write_text(
        json.dumps(mutated_document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    mutated_verify = cosign_verify(
        cosign_image,
        args.supply_dir,
        trust_dir,
        mutated.name,
        args.bundle.name,
        "cosign.pub",
    )
    wrong_key_dir = args.supply_dir / "negative-trust"
    wrong_key_dir.mkdir(exist_ok=True)
    wrong_key = wrong_key_dir / "wrong-publisher.pub"
    shutil.copyfile(
        args.repo / "testkit/fixtures/supply-chain/wrong-publisher-cosign.pub",
        wrong_key,
    )
    wrong_publisher = cosign_verify(
        cosign_image,
        args.supply_dir,
        wrong_key_dir,
        args.manifest.name,
        args.bundle.name,
        wrong_key.name,
    )
    key_policy = load(trust_dir / "cosign-key-policy.json")
    revocations = load(trust_dir / "revoked-keys.json")
    public_key_digest = sha256(trust_dir / "cosign.pub")
    revoked = public_key_digest in {
        "sha256:" + str(value).removeprefix("sha256:")
        for value in revocations["revoked_sha256_fingerprints"]
    }
    signature_checks = {
        "positive_verification": positive.returncode == 0,
        "mutated_subject_rejected": mutated_verify.returncode != 0,
        "wrong_publisher_rejected": wrong_publisher.returncode != 0,
        "public_key_digest_exact": public_key_digest == key_policy["public_key_sha256"],
        "public_key_not_revoked": not revoked,
        "offline_signing_config_exact": signing_config
        == {
            "mediaType": "application/vnd.dev.sigstore.signingconfig.v0.2+json",
            "rekorTlogConfig": {},
            "tsaConfig": {},
        },
        "manifest_signing_config_digest_exact": manifest.get("trust", {}).get(
            "signing_config_digest"
        )
        == sha256(signing_config_path)
        if isinstance(manifest.get("trust"), dict)
        else False,
    }

    sbom_paths = sorted(args.supply_dir.glob("*.spdx.json"))
    sbom_checks = {
        "required_documents": len(sbom_paths) >= 3,
        "all_spdx": True,
        "all_have_packages": True,
        "runner_document_present": any(path.name == "runner.spdx.json" for path in sbom_paths),
    }
    sbom_summary = []
    runner_package_records: dict[str, list[dict[str, object]]] = {
        name: [] for name in expected_runner_packages
    }
    for path in sbom_paths:
        document = load(path)
        is_spdx = str(document.get("spdxVersion", "")).startswith("SPDX-")
        packages = document.get("packages", [])
        package_count = len(packages) if isinstance(packages, list) else 0
        sbom_checks["all_spdx"] = sbom_checks["all_spdx"] and is_spdx
        sbom_checks["all_have_packages"] = (
            sbom_checks["all_have_packages"] and package_count > 0
        )
        sbom_summary.append(
            {"path": path.name, "digest": sha256(path), "packages": package_count}
        )
        if path.name == "runner.spdx.json":
            runner_package_records = extract_runner_package_records(
                document, set(expected_runner_packages)
            )
    sbom_checks.update(
        runner_package_checks(
            runner_package_records,
            expected_runner_packages,
            expected_runner_distro,
        )
    )

    scan_paths = sorted(args.supply_dir.glob("trivy-*.json"))
    findings = trivy_findings(scan_paths)
    db_metadata_path = args.trivy_cache / "db/metadata.json"
    db_metadata = load(db_metadata_path)
    downloaded = parse_time(str(db_metadata["DownloadedAt"]))
    age_seconds = max(
        0,
        int(
            (
                datetime.now(timezone.utc) - downloaded.astimezone(timezone.utc)
            ).total_seconds()
        ),
    )
    policy_age = int(policy["database_max_age_seconds"])
    java_db_metadata_path = args.trivy_cache / "java-db/metadata.json"
    java_db_metadata = load(java_db_metadata_path)
    java_db_downloaded = parse_time(str(java_db_metadata["DownloadedAt"]))
    java_db_age_seconds = max(
        0,
        int(
            (
                datetime.now(timezone.utc) - java_db_downloaded.astimezone(timezone.utc)
            ).total_seconds()
        ),
    )
    scan_checks = {
        "scan_documents_present": len(scan_paths) >= 3,
        "database_present": (args.trivy_cache / "db/trivy.db").is_file(),
        "database_fresh": age_seconds <= policy_age,
        "java_database_present": (args.trivy_cache / "java-db/trivy-java.db").is_file(),
        "java_database_fresh": java_db_age_seconds <= policy_age,
        "no_secrets": not findings["secrets"],
        "no_critical": not findings["critical"],
        "no_fixable_high": not findings["fixable_high"],
        "no_high_critical_misconfiguration": not findings["misconfig_high_critical"],
    }
    unfixed_high_without_exception = bool(findings["unfixed_high"])

    subject_digests = {
        (str(item["name"]), "sha256:" + str(item["digest"]["sha256"]))
        for item in manifest.get("subjects", [])
        if isinstance(item, dict) and isinstance(item.get("digest"), dict)
    }
    provenance_subjects = {
        (str(item["name"]), "sha256:" + str(item["digest"]["sha256"]))
        for item in provenance.get("subject", [])
        if isinstance(item, dict) and isinstance(item.get("digest"), dict)
    }
    provenance_checks = {
        "statement_type": provenance.get("_type") == "https://in-toto.io/Statement/v1",
        "predicate_type": provenance.get("predicateType")
        == "https://slsa.dev/provenance/v1",
        "subjects_exact": subject_digests == provenance_subjects,
        "manifest_digest_exact": manifest["provenance"]["digest"]
        == sha256(provenance_path),
        "runtime_subject": (
            "masi-nids/p4-switch-runtime",
            args.runtime_digest,
        )
        in subject_digests,
        "runner_subject": (
            "masi-nids/p4-switch-e2e-runner",
            args.runner_digest,
        )
        in subject_digests,
        "compiler_subject": (
            "masi-nids/p4-switch-p4c",
            args.compiler_digest,
        )
        in subject_digests,
        "runner_dependencies_subject": (
            "masi-nids/p4-switch-runner-deps",
            args.runner_deps_digest,
        )
        in subject_digests,
    }
    offline_checks = {
        "network_none": offline.get("network") == "none",
        "runtime_digest_match": bool(offline.get("runtime_digest_match")),
        "runner_digest_match": bool(offline.get("runner_digest_match")),
        "runtime_tests_51_passed": bool(offline.get("runtime_tests_51_passed")),
        "bundle_complete": bool(offline.get("bundle_complete")),
        "bundle_checksums_verified": bool(offline.get("bundle_checksums_verified")),
    }
    notice = load(args.repo / "contracts/supply-chain/v1/third-party-notices.json")
    registered_names = {str(item["name"]) for item in registry["components"]}
    notice_names = {str(item["component"]) for item in notice["notices"]}
    registered = {str(item["name"]): item for item in registry["components"]}
    runner_base_digest = str(runner_profile["runner_base_image"]).split("@", 1)[1]
    runner_os = (
        f"{runner_environment['distribution']} "
        f"{runner_environment['distribution_version']}"
    )
    tcpreplay_registration = registered.get("Tcpreplay", {})
    iproute2_registration = registered.get("iproute2", {})
    alpine_registration = registered.get("Alpine Linux runner base", {})
    dependency_registration = registered.get(
        "P4 qualification runner dependency image", {}
    )
    runner_environment_registry_exact = (
        tcpreplay_registration.get("version")
        == runner_environment["tcpreplay"]["tool_version"]
        and tcpreplay_registration.get("package_version")
        == runner_environment["tcpreplay"]["package_version"]
        and tcpreplay_registration.get("runner_os") == runner_os
        and iproute2_registration.get("version")
        == runner_environment["iproute2"]["tool_version"]
        and iproute2_registration.get("package_version")
        == runner_environment["iproute2"]["package_version"]
        and iproute2_registration.get("runner_os") == runner_os
        and alpine_registration.get("version")
        == runner_environment["distribution_version"]
        and alpine_registration.get("digest") == runner_base_digest
        and dependency_registration.get("base_image_digest") == runner_base_digest
        and dependency_registration.get("runner_os") == runner_os
        and dependency_registration.get("iproute2_package_version")
        == runner_environment["iproute2"]["package_version"]
        and dependency_registration.get("tcpreplay_package_version")
        == runner_environment["tcpreplay"]["package_version"]
    )
    traffic_profile_environment_exact = (
        traffic_profile.get("runner_environment") == runner_environment
        and traffic_profile.get("backends", {}).get("tcpreplay/v1", {}).get(
            "tool_version"
        )
        == runner_environment["tcpreplay"]["tool_version"]
        and traffic_profile.get("backends", {}).get("tcpreplay/v1", {}).get(
            "package_version"
        )
        == runner_environment["tcpreplay"]["package_version"]
        and traffic_profile.get("impairment", {}).get("iproute2_tool_version")
        == runner_environment["iproute2"]["tool_version"]
        and traffic_profile.get("impairment", {}).get("iproute2_package_version")
        == runner_environment["iproute2"]["package_version"]
    )
    inventory_checks = {
        "registry_schema": not registry_errors,
        "notices_cover_registry": registered_names == notice_names,
        "notice_file_present": (args.repo / "NOTICE").is_file(),
        "runner_environment_registry_exact": runner_environment_registry_exact,
        "traffic_profile_environment_exact": traffic_profile_environment_exact,
        "no_runtime_download": all(
            item.get("runtime_download") is False for item in registry["components"]
        ),
    }
    hard_fail_checks = {
        **signature_checks,
        **sbom_checks,
        **scan_checks,
        **provenance_checks,
        **offline_checks,
        **inventory_checks,
    }
    failure_reasons = [key for key, value in hard_fail_checks.items() if not value]
    hold_reasons = []
    if unfixed_high_without_exception:
        hold_reasons.append("UNFIXED_HIGH_REQUIRES_OWNER_EXCEPTION")
    if failure_reasons:
        result = "FAIL"
    elif hold_reasons:
        result = "HOLD"
    else:
        result = "PASS"
    qualification = "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED"
    verification = {
        "schema_version": "p4-switch-supply-verification/v1",
        "result": result,
        "qualification": qualification,
        "manifest_digest": sha256(args.manifest),
        "signature_bundle_digest": sha256(args.bundle),
        "registry_digest": sha256(registry_path),
        "checks": {
            "signature": signature_checks,
            "sbom": sbom_checks,
            "scan": scan_checks,
            "provenance": provenance_checks,
            "offline": offline_checks,
            "inventory": inventory_checks,
        },
        "sboms": sbom_summary,
        "runner_environment_binding": {
            "profile": runner_environment,
            "expected_sbom_packages": expected_runner_packages,
            "expected_sbom_distro": expected_runner_distro,
            "observed_sbom_packages": runner_package_records,
        },
        "trivy": {
            "documents": [
                {"path": path.name, "digest": sha256(path)} for path in scan_paths
            ],
            "database_digest": sha256(args.trivy_cache / "db/trivy.db"),
            "database_metadata_digest": sha256(db_metadata_path),
            "database_age_seconds": age_seconds,
            "java_database_digest": sha256(args.trivy_cache / "java-db/trivy-java.db"),
            "java_database_metadata_digest": sha256(java_db_metadata_path),
            "java_database_age_seconds": java_db_age_seconds,
            "findings": findings,
        },
        "negative_verification": {
            "mutated_exit_code": mutated_verify.returncode,
            "wrong_publisher_exit_code": wrong_publisher.returncode,
        },
        "failure_reasons": failure_reasons,
        "hold_reasons": hold_reasons,
    }
    verification_path = args.supply_dir / "verification.json"
    verification_path.write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    phase = {
        "phase": "supply-chain",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": qualification,
        "tests": [
            {
                "id": "TEST-P4-SUPPLY-001",
                "requirement_ids": [
                    "ARCH-REUSE-001",
                    "CONTRACT-SUPPLY-001",
                    "TEST-003",
                ],
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": qualification,
                "evidence": {
                    "manifest": args.manifest.name,
                    "manifest_digest": sha256(args.manifest),
                    "signature_bundle": args.bundle.name,
                    "signature_bundle_digest": sha256(args.bundle),
                    "verification": verification_path.name,
                    "verification_digest": sha256(verification_path),
                    "failure_reasons": failure_reasons,
                    "hold_reasons": hold_reasons,
                    "database_age_seconds": age_seconds,
                    "unfixed_high_count": len(findings["unfixed_high"]),
                },
            }
        ],
    }
    args.output.write_text(
        json.dumps(phase, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if result == "FAIL":
        return 1
    if result == "HOLD":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
