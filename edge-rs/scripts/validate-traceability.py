#!/usr/bin/env python3
"""Validate and aggregate run-bound Rust Edge requirement evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


EXPECTED_REQUIREMENTS = {
    "MOD-EDGE-001",
    "ARCH-003",
    "ARCH-004",
    "ARCH-TELEMETRY-001",
    "ARCH-TARGET-FLEET-001",
    "REL-TARGET-FLEET-001",
    "CONTRACT-P4-001",
    "CONTRACT-P4-FW-001",
    "CONTRACT-RULE-001",
    "CONTRACT-TARGET-001",
    "CONTRACT-TELEMETRY-001",
    "CONTRACT-INFERENCE-001",
    "CONTRACT-MODEL-001",
    "FUNC-EFFECT-001",
    "FUNC-TEL-001",
    "TEST-003",
    "TEST-007",
    "TEST-008",
    "TEST-009",
    "TEST-010",
    "TEST-RULE-001",
    "TEST-P4-FW-001",
    "TEST-TARGET-FLEET-001",
    "TEST-TEL-INF-001",
    "TEST-REAL-E2E-001",
    "DEC-044",
}

EDGE_SUMMARY_VERSIONS = {
    "edge-module-gate-summary/v1",
    "edge-oci-startup-evidence/v1",
    "edge-deep-check-evidence/v1",
    "rust-edge-agent-supply-verification/v1",
}
BLACKBOX_VERSIONS = {
    "edge-module-e2e-evidence/v1",
    "edge-module-fault-evidence/v1",
    "edge-fault-evidence/v1",
    "edge-security-evidence/v1",
    "edge-telemetry-evidence/v1",
    "edge-resource-bound-evidence/v1",
    "edge-rule-observation-evidence/v1",
    "edge-os-fault-evidence/v1",
    "edge-target-capacity-rehearsal/v1",
    "edge-performance-rehearsal/v1",
}
KNOWN_JSON_EVIDENCE_VERSIONS = (
    EDGE_SUMMARY_VERSIONS | BLACKBOX_VERSIONS | {"qualification-soak/v1"}
)
RESULTS = {"PASS", "FAIL", "HOLD", "NOT_RUN"}
QUALIFICATIONS = {"QUALIFIED", "NOT_QUALIFIED"}
MAX_ARTIFACT_BYTES = 67_108_864
FROZEN_CONDITIONAL_APPLICABILITY = [
    {
        "profile": "target-gnmi-readonly/v1",
        "applicability": "NOT_APPLICABLE",
        "result": "NOT_RUN",
        "stable_reason": "NOT_TRIGGERED_BY_FIRST_RELEASE_BMV2_ASSIGNMENT",
    },
    {
        "profile": "mirror-packet-mmap/v1",
        "applicability": "NOT_APPLICABLE",
        "result": "NOT_RUN",
        "stable_reason": "P4_FEATURES_SUFFICIENT_FOR_SELECTED_MODEL",
    },
    {
        "profile": "mirror-af-xdp/v1",
        "applicability": "NOT_APPLICABLE",
        "result": "NOT_RUN",
        "stable_reason": "PACKET_MMAP_PROFILE_NOT_TRIGGERED",
    },
    {
        "profile": "mirror-dpdk/v1",
        "applicability": "NOT_APPLICABLE",
        "result": "NOT_RUN",
        "stable_reason": "AF_XDP_PROFILE_NOT_TRIGGERED",
    },
]


def load(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def worst_result(results: list[str]) -> str:
    rank = {"PASS": 0, "NOT_RUN": 1, "HOLD": 2, "FAIL": 3}
    return max(results, key=lambda item: rank[item]) if results else "NOT_RUN"


def normalized_result(value: object) -> str:
    return value if isinstance(value, str) and value in RESULTS else "FAIL"


def normalized_qualification(value: object) -> str:
    return (
        value if isinstance(value, str) and value in QUALIFICATIONS else "NOT_QUALIFIED"
    )


def safe_relative(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    path = Path(text)
    return (
        bool(text)
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == text
    )


def path_chain_has_symlink(root: Path, lexical_path: Path) -> bool:
    """Reject a symlink in any path component below an already-resolved root."""
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


def schema_failures(
    schema: dict[str, Any], document: dict[str, Any], prefix: str
) -> list[str]:
    return [
        f"{prefix} schema error at {list(error.path)}: {error.message}"
        for error in sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
                document
            ),
            key=lambda item: list(item.path),
        )
    ]


def supply_release_schema_failures(
    schema: dict[str, Any], document: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    offline_bundle = document.get("offline_bundle")
    files = offline_bundle.get("files") if isinstance(offline_bundle, dict) else None
    if isinstance(files, list):
        fingerprints = [
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for value in files
        ]
        paths = [
            value.get("path")
            for value in files
            if isinstance(value, dict) and isinstance(value.get("path"), str)
        ]
        if len(fingerprints) != len(set(fingerprints)):
            failures.append("supply release manifest repeats an offline bundle entry")
        if len(paths) != len(set(paths)):
            failures.append("supply release manifest repeats an offline bundle path")
    optimized_schema = copy.deepcopy(schema)
    optimized_schema["properties"]["offline_bundle"]["properties"]["files"].pop(
        "uniqueItems", None
    )
    failures.extend(
        schema_failures(optimized_schema, document, "supply release manifest")
    )
    return failures


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def command_expected_result(exit_code: object) -> tuple[str, str, str | None] | None:
    if exit_code is None:
        return "NOT_RUN", "NOT_QUALIFIED", None
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None
    if exit_code == 0:
        return "PASS", "QUALIFIED", None
    if exit_code == 2:
        return "HOLD", "NOT_QUALIFIED", "COMMAND_EXITED_HOLD"
    return "FAIL", "NOT_QUALIFIED", "COMMAND_EXITED_FAILURE"


def exact_passing_rust_tests(log_text: str) -> set[str]:
    tests: set[str] = set()
    pattern = re.compile(r"^test ([A-Za-z0-9_:]+) \.\.\. ok$")
    for line in log_text.splitlines():
        match = pattern.fullmatch(line)
        if match is not None:
            tests.add(match.group(1).rsplit("::", 1)[-1])
    return tests


def rust_test_writes_evidence(source: str, target: str, filename: str) -> bool:
    function = re.search(
        rf"(?m)^async\s+fn\s+{re.escape(target)}\s*\(|^fn\s+{re.escape(target)}\s*\(",
        source,
    )
    if function is None:
        return False
    next_test = re.search(r"(?m)^#\[(?:tokio::test|test)", source[function.end() :])
    end = len(source) if next_test is None else function.end() + next_test.start()
    body = source[function.start() : end]
    return f'"{filename}"' in body


def add_artifact(
    index: dict[str, dict[str, Any]], relative: str, path: Path, media_type: str
) -> None:
    if relative not in index:
        index[relative] = {
            "path": relative,
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
            "media_type": media_type,
        }


def bind_declared_supply_artifact(
    *,
    evidence_root: Path,
    supply_dir: Path,
    declaration: object,
    label: str,
    require_bytes: bool,
    seen_paths: set[str],
    failures: list[str],
    artifact_index: dict[str, dict[str, Any]],
    bound_claims: set[str],
) -> Path | None:
    if not isinstance(declaration, dict):
        failures.append(f"supply {label} declaration is not an object")
        return None

    path_value = declaration.get("path")
    digest = declaration.get("digest")
    if not safe_relative(path_value):
        failures.append(f"supply {label} has an unsafe path")
        return None
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
    ):
        failures.append(f"supply {label} has an invalid digest")
        return None

    path_text = str(path_value)
    if path_text in seen_paths:
        failures.append(f"supply {label} repeats path {path_text}")
        return None
    seen_paths.add(path_text)

    lexical_path = supply_dir / path_text
    if path_chain_has_symlink(supply_dir, lexical_path) or not lexical_path.is_file():
        failures.append(f"supply {label} is missing or is a symbolic link: {path_text}")
        return None
    artifact_path = lexical_path.resolve()
    supply_root = supply_dir.resolve()
    if not artifact_path.is_relative_to(
        supply_root
    ) or not artifact_path.is_relative_to(evidence_root):
        failures.append(f"supply {label} escapes its evidence directory: {path_text}")
        return None

    size = artifact_path.stat().st_size
    if size < 1 or size > MAX_ARTIFACT_BYTES:
        failures.append(f"supply {label} has an invalid size: {path_text}")
        return None

    relative = artifact_path.relative_to(evidence_root).as_posix()
    media_type = "application/json" if artifact_path.suffix == ".json" else "text/plain"
    bound_claims.add(relative)
    add_artifact(artifact_index, relative, artifact_path, media_type)

    if sha256(artifact_path) != digest:
        failures.append(f"supply {label} digest mismatch: {path_text}")
    expected_bytes = declaration.get("bytes")
    if require_bytes and (
        not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes != size
    ):
        failures.append(f"supply {label} byte count mismatch: {path_text}")
    if artifact_path.suffix == ".json":
        try:
            json.loads(
                artifact_path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"invalid supply {label} JSON {path_text}: {error}")
    return artifact_path


def bind_supply_subordinates(
    *,
    repo: Path,
    evidence_root: Path,
    supply_source_lexical: Path,
    supply_parent: dict[str, Any],
    oci_parent: dict[str, Any] | None,
    failures: list[str],
    artifact_index: dict[str, dict[str, Any]],
    bound_claims: set[str],
) -> None:
    supply_dir_lexical = supply_source_lexical.parent / "supply"
    if (
        path_chain_has_symlink(evidence_root, supply_dir_lexical)
        or not supply_dir_lexical.is_dir()
    ):
        failures.append(
            "supply subordinate evidence directory is missing or is a symbolic link"
        )
        return
    supply_dir = supply_dir_lexical.resolve()
    if not supply_dir.is_relative_to(evidence_root):
        failures.append("supply subordinate evidence directory escapes the run root")
        return

    manifest_digest = supply_parent.get("manifest_digest")
    if not isinstance(manifest_digest, str):
        failures.append("supply parent does not bind a release manifest digest")
        return

    seen_paths: set[str] = set()
    manifest_path = bind_declared_supply_artifact(
        evidence_root=evidence_root,
        supply_dir=supply_dir,
        declaration={"path": "release-manifest.json", "digest": manifest_digest},
        label="release manifest",
        require_bytes=False,
        seen_paths=seen_paths,
        failures=failures,
        artifact_index=artifact_index,
        bound_claims=bound_claims,
    )
    if manifest_path is None:
        return
    try:
        release_manifest = load(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if release_manifest.get("schema_version") != "rust-edge-agent-supply-release/v1":
        failures.append("supply release manifest has an unknown schema")
    else:
        failures.extend(
            supply_release_schema_failures(
                load(
                    repo
                    / "contracts/supply-chain/v1/rust-edge-agent-release.schema.json"
                ),
                release_manifest,
            )
        )
    if isinstance(oci_parent, dict):
        for field in (
            "source_revision",
            "source_tree_digest",
            "working_tree_dirty",
            "working_tree_status_digest",
            "image_ref",
            "image_manifest_digest",
            "image_config_digest",
        ):
            if release_manifest.get(field) != oci_parent.get(field):
                failures.append(
                    f"supply release manifest disagrees with OCI evidence: {field}"
                )
        release_subjects = release_manifest.get("subjects")
        binary_subjects = (
            [
                item
                for item in release_subjects
                if isinstance(item, dict)
                and item.get("name") == "usr/local/bin/masi-edge"
            ]
            if isinstance(release_subjects, list)
            else []
        )
        expected_binary_digest = str(oci_parent.get("image_binary_digest", ""))
        if (
            len(binary_subjects) != 1
            or not isinstance(binary_subjects[0].get("digest"), dict)
            or "sha256:" + str(binary_subjects[0]["digest"].get("sha256", ""))
            != expected_binary_digest
        ):
            failures.append(
                "supply release manifest disagrees with OCI evidence: binary"
            )
        oci_archive = oci_parent.get("archive")
        offline_bundle = release_manifest.get("offline_bundle")
        bundle_entries = (
            offline_bundle.get("files") if isinstance(offline_bundle, dict) else None
        )
        image_archive_entries = (
            [
                item
                for item in bundle_entries
                if isinstance(item, dict) and item.get("path") == "edge-image.tar"
            ]
            if isinstance(bundle_entries, list)
            else []
        )
        if (
            not isinstance(oci_archive, dict)
            or release_manifest.get("image_archive_index_digest")
            != oci_archive.get("index_digest")
            or len(image_archive_entries) != 1
            or image_archive_entries[0].get("digest")
            != oci_archive.get("archive_digest")
            or image_archive_entries[0].get("bytes") != oci_archive.get("archive_bytes")
        ):
            failures.append(
                "supply release manifest disagrees with OCI archive inspection"
            )

    inventory = release_manifest.get("inventory")
    if not isinstance(inventory, dict):
        failures.append("supply release manifest inventory is missing")
        return
    for group in ("sboms", "scans", "evidence_inputs"):
        declarations = inventory.get(group)
        if not isinstance(declarations, list):
            failures.append(f"supply release manifest inventory {group} is not a list")
            continue
        for index, declaration in enumerate(declarations):
            bind_declared_supply_artifact(
                evidence_root=evidence_root,
                supply_dir=supply_dir,
                declaration=declaration,
                label=f"inventory {group}[{index}]",
                require_bytes=True,
                seen_paths=seen_paths,
                failures=failures,
                artifact_index=artifact_index,
                bound_claims=bound_claims,
            )

    for field in ("provenance", "offline_rebuild"):
        bind_declared_supply_artifact(
            evidence_root=evidence_root,
            supply_dir=supply_dir,
            declaration=release_manifest.get(field),
            label=field.replace("_", " "),
            require_bytes=False,
            seen_paths=seen_paths,
            failures=failures,
            artifact_index=artifact_index,
            bound_claims=bound_claims,
        )

    signature_digest = supply_parent.get("signature_bundle_digest")
    bind_declared_supply_artifact(
        evidence_root=evidence_root,
        supply_dir=supply_dir,
        declaration={
            "path": "release-manifest.sigstore.json",
            "digest": signature_digest,
        },
        label="signature bundle",
        require_bytes=False,
        seen_paths=seen_paths,
        failures=failures,
        artifact_index=artifact_index,
        bound_claims=bound_claims,
    )


def bind_oci_archive_subordinates(
    *,
    evidence_root: Path,
    oci_parent: dict[str, Any],
    failures: list[str],
    artifact_index: dict[str, dict[str, Any]],
    bound_claims: set[str],
) -> None:
    archive = oci_parent.get("archive")
    if not isinstance(archive, dict):
        failures.append("OCI parent does not bind archive inspection evidence")
        return
    declarations = {
        "oci-smoke/edge-image.tar": (
            archive.get("archive_digest"),
            archive.get("archive_bytes"),
        ),
        "oci-smoke/oci-archive-inspection.json": (None, None),
        "oci-smoke/image-archive-manifest.json": (
            archive.get("index_digest"),
            archive.get("index_bytes"),
        ),
        "oci-smoke/image-config.json": (
            archive.get("config_digest"),
            archive.get("config_bytes"),
        ),
    }
    paths: dict[str, Path] = {}
    for relative, (expected_digest, expected_bytes) in declarations.items():
        lexical = evidence_root / relative
        if path_chain_has_symlink(evidence_root, lexical) or not lexical.is_file():
            failures.append(
                f"OCI archive subordinate is missing or symbolic: {relative}"
            )
            continue
        path = lexical.resolve()
        size = path.stat().st_size
        if not path.is_relative_to(evidence_root) or not 0 < size <= MAX_ARTIFACT_BYTES:
            failures.append(
                f"OCI archive subordinate is unsafe or oversized: {relative}"
            )
            continue
        if expected_digest is not None and sha256(path) != expected_digest:
            failures.append(f"OCI archive subordinate digest mismatch: {relative}")
        if expected_bytes is not None and size != expected_bytes:
            failures.append(f"OCI archive subordinate byte count mismatch: {relative}")
        paths[relative] = path
        bound_claims.add(relative)
        add_artifact(
            artifact_index,
            relative,
            path,
            "application/x-tar" if relative.endswith(".tar") else "application/json",
        )

    summary_path = paths.get("oci-smoke/oci-archive-inspection.json")
    index_path = paths.get("oci-smoke/image-archive-manifest.json")
    config_path = paths.get("oci-smoke/image-config.json")
    try:
        summary = load(summary_path) if summary_path is not None else None
        index = (
            json.loads(
                index_path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            if index_path is not None
            else None
        )
        config = load(config_path) if config_path is not None else None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        failures.append(f"invalid OCI archive subordinate JSON: {error}")
        return
    if summary != archive:
        failures.append("OCI archive inspection is not exactly embedded in its parent")
    image = index[0] if isinstance(index, list) and len(index) == 1 else None
    runtime_config = config.get("config") if isinstance(config, dict) else None
    rootfs = config.get("rootfs") if isinstance(config, dict) else None
    layers = image.get("Layers") if isinstance(image, dict) else None
    if (
        not isinstance(image, dict)
        or set(image) != {"Config", "Layers", "RepoTags"}
        or image.get("Config") != archive.get("config_blob_path")
        or image.get("RepoTags") != [oci_parent.get("image_ref")]
        or not isinstance(layers, list)
        or len(layers) != archive.get("layer_count")
        or not isinstance(config, dict)
        or config.get("architecture") != "amd64"
        or config.get("os") != "linux"
        or not isinstance(runtime_config, dict)
        or runtime_config.get("User") != "65532:65532"
        or runtime_config.get("Entrypoint") != ["/usr/local/bin/masi-edge"]
        or not isinstance(rootfs, dict)
        or rootfs.get("type") != "layers"
        or not isinstance(rootfs.get("diff_ids"), list)
        or len(rootfs["diff_ids"]) != len(layers)
        or archive.get("config_digest") != oci_parent.get("image_config_digest")
        or archive.get("binary_digest") != oci_parent.get("image_binary_digest")
        or archive.get("manifest_digest") != oci_parent.get("image_manifest_digest")
        or archive.get("attestation_count") != 0
        or archive.get("manifest_media_type")
        != "application/vnd.docker.distribution.manifest.v2+json"
        or archive.get("image_ref") != oci_parent.get("image_ref")
    ):
        failures.append("OCI archive subordinate semantics drifted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--summary-out", type=Path)
    args = parser.parse_args()

    repo = args.repo.resolve()
    manifest_path = args.manifest.resolve()
    manifest = load(manifest_path)
    failures: list[str] = []
    if manifest.get("schema_version") != "rust-edge-agent-requirement-traceability/v1":
        failures.append("unknown traceability schema version")
    if manifest.get("module_id") != "MOD-EDGE-001":
        failures.append("module_id is not MOD-EDGE-001")

    requirements = manifest.get("requirements", [])
    if not isinstance(requirements, list):
        failures.append("requirements is not an array")
        requirements = []
    ids = [
        item.get("requirement_id") for item in requirements if isinstance(item, dict)
    ]
    if len(ids) != len(set(ids)):
        failures.append("requirement IDs are not unique")
    if set(ids) != EXPECTED_REQUIREMENTS:
        failures.append(
            "requirement coverage mismatch: missing="
            f"{sorted(EXPECTED_REQUIREMENTS - set(ids))}, extra={sorted(set(ids) - EXPECTED_REQUIREMENTS)}"
        )

    requirements_text = (
        repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md"
    ).read_text(encoding="utf-8")
    test_source_paths = [
        repo / "edge-rs/tests/contract_golden.rs",
        repo / "edge-rs/tests/module_blackbox.rs",
        repo / "edge-rs/tests/property_invariants.rs",
        *sorted((repo / "edge-rs/src").rglob("*.rs")),
    ]
    test_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in test_source_paths
    )
    module_blackbox_source = (repo / "edge-rs/tests/module_blackbox.rs").read_text(
        encoding="utf-8"
    )
    all_test_targets: list[str] = []
    all_json_evidence_paths: set[str] = set()
    for item in requirements:
        if not isinstance(item, dict):
            failures.append("requirement entry is not an object")
            continue
        requirement_id = str(item.get("requirement_id", ""))
        if (
            re.search(
                rf"(?<![A-Z0-9-]){re.escape(requirement_id)}(?![A-Z0-9-])",
                requirements_text,
            )
            is None
        ):
            failures.append(
                f"{requirement_id} is absent from the requirements baseline"
            )
        for field in ("scenario_ids", "test_targets", "evidence_files"):
            value = item.get(field)
            if (
                not isinstance(value, list)
                or not value
                or len(value) != len(set(value))
            ):
                failures.append(
                    f"{requirement_id}.{field} must be a nonempty unique array"
                )
        if not item.get("qualification_limit"):
            failures.append(f"{requirement_id} has no qualification_limit")
        for target in item.get("test_targets", []):
            target_text = str(target)
            all_test_targets.append(target_text)
            if target_text.startswith("scripts/"):
                if not (repo / "edge-rs" / target_text).is_file():
                    failures.append(
                        f"{requirement_id} references missing {target_text}"
                    )
            elif re.search(rf"\bfn\s+{re.escape(target_text)}\b", test_sources) is None:
                failures.append(
                    f"{requirement_id} references unknown test {target_text}"
                )
        for evidence in item.get("evidence_files", []):
            if not safe_relative(evidence):
                failures.append(f"{requirement_id} has unsafe evidence path {evidence}")
            elif str(evidence).endswith(".json"):
                all_json_evidence_paths.add(str(evidence))

    for contract in manifest.get("public_contracts", []):
        contract_path = repo / str(contract)
        if not contract_path.is_file():
            failures.append(f"public contract is missing: {contract}")

    conditional = manifest.get("conditional_applicability", [])
    if conditional != FROZEN_CONDITIONAL_APPLICABILITY:
        failures.append(
            "conditional applicability must exactly match the four frozen profile records"
        )

    execution_bindings = manifest.get("execution_bindings", [])
    if not isinstance(execution_bindings, list) or not execution_bindings:
        failures.append("execution_bindings must be a nonempty array")
        execution_bindings = []
    binding_by_command: dict[str, dict[str, Any]] = {}
    target_to_command: dict[str, str] = {}
    for binding in execution_bindings:
        if not isinstance(binding, dict):
            failures.append("execution binding is not an object")
            continue
        command_id = str(binding.get("command_id", ""))
        log = str(binding.get("log", ""))
        mode = binding.get("mode")
        targets = binding.get("targets")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", command_id):
            failures.append(f"invalid execution command_id {command_id}")
        if command_id in binding_by_command:
            failures.append(f"duplicate execution command_id {command_id}")
        binding_by_command[command_id] = binding
        if log != f"{command_id}.log" or not safe_relative(log):
            failures.append(f"execution binding {command_id} has a noncanonical log")
        if mode not in {"rust-libtest", "command"}:
            failures.append(f"execution binding {command_id} has invalid mode")
        if not isinstance(binding.get("required"), bool):
            failures.append(
                f"execution binding {command_id} has no boolean required field"
            )
        if not isinstance(targets, list) or len(targets) != len(set(targets)):
            failures.append(
                f"execution binding {command_id} targets are not a unique array"
            )
            targets = []
        for target in targets:
            target_text = str(target)
            if target_text in target_to_command:
                failures.append(
                    f"test target {target_text} has multiple execution bindings"
                )
            target_to_command[target_text] = command_id
    if not set(all_test_targets).issubset(target_to_command):
        failures.append(
            "execution target coverage mismatch: missing="
            f"{sorted(set(all_test_targets) - set(target_to_command))}"
        )

    evidence_catalog = manifest.get("evidence_catalog", [])
    if not isinstance(evidence_catalog, list) or not evidence_catalog:
        failures.append("evidence_catalog must be a nonempty array")
        evidence_catalog = []
    catalog_by_path: dict[str, dict[str, Any]] = {}
    for entry in evidence_catalog:
        if not isinstance(entry, dict):
            failures.append("evidence catalog entry is not an object")
            continue
        path = str(entry.get("path", ""))
        if not safe_relative(path) or not path.endswith(".json"):
            failures.append(f"unsafe evidence catalog path {path}")
        if path in catalog_by_path:
            failures.append(f"duplicate evidence catalog path {path}")
        catalog_by_path[path] = entry
        schema_version = entry.get("schema_version")
        if (
            not isinstance(schema_version, str)
            or schema_version not in KNOWN_JSON_EVIDENCE_VERSIONS
        ):
            failures.append(f"{path} catalogs unknown schema {schema_version}")
        test_id = entry.get("test_id")
        if test_id is not None and not isinstance(test_id, str):
            failures.append(f"{path} catalogs an invalid test_id")
        requirement_ids = entry.get("requirement_ids")
        if (
            not isinstance(requirement_ids, list)
            or not requirement_ids
            or len(requirement_ids) != len(set(requirement_ids))
            or any(not isinstance(value, str) for value in requirement_ids)
        ):
            failures.append(f"{path} catalogs invalid requirement_ids")
            requirement_ids = []
        for requirement_id in requirement_ids:
            if (
                re.search(
                    rf"(?<![A-Z0-9-]){re.escape(requirement_id)}(?![A-Z0-9-])",
                    requirements_text,
                )
                is None
            ):
                failures.append(
                    f"{path} catalogs unknown baseline requirement {requirement_id}"
                )
        producers = entry.get("producer_command_ids")
        if (
            not isinstance(producers, list)
            or not producers
            or len(producers) != len(set(producers))
            or any(value not in binding_by_command for value in producers)
        ):
            failures.append(f"{path} catalogs invalid producer commands")
            producers = []
        producer_targets = entry.get("producer_test_targets")
        if not isinstance(entry.get("required"), bool):
            failures.append(f"{path} has no boolean required field")
        if (
            not isinstance(producer_targets, list)
            or len(producer_targets) != len(set(producer_targets))
            or any(not isinstance(value, str) for value in producer_targets)
        ):
            failures.append(f"{path} catalogs invalid producer test targets")
            producer_targets = []
        rust_producers = [
            value
            for value in producers
            if value in binding_by_command
            and binding_by_command[value].get("mode") == "rust-libtest"
        ]
        if rust_producers and not producer_targets:
            failures.append(f"{path} has no exact Rust producer test target")
        if not rust_producers and producer_targets:
            failures.append(f"{path} assigns Rust targets to a command-only producer")
        for target in producer_targets:
            if re.search(rf"\bfn\s+{re.escape(target)}\b", test_sources) is None:
                failures.append(f"{path} catalogs unknown producer test {target}")
            elif not rust_test_writes_evidence(
                module_blackbox_source, target, Path(path).name
            ):
                failures.append(
                    f"{path} is not written by its cataloged Rust producer test {target}"
                )
    if not all_json_evidence_paths.issubset(catalog_by_path):
        failures.append(
            "evidence catalog coverage mismatch: missing="
            f"{sorted(all_json_evidence_paths - set(catalog_by_path))}"
        )

    requirement_evidence: list[dict[str, Any]] = []
    artifact_index: dict[str, dict[str, Any]] = {}
    if args.evidence_root is not None:
        if args.evidence_root.is_symlink():
            failures.append("evidence root must not be a symbolic link")
        evidence_root = args.evidence_root.resolve()
        if not evidence_root.is_dir():
            failures.append(f"evidence root is not a directory: {evidence_root}")
        else:
            symbolic_paths = sorted(
                path.relative_to(evidence_root).as_posix()
                for path in evidence_root.rglob("*")
                if path.is_symlink()
            )
            if symbolic_paths:
                failures.append(
                    f"evidence tree contains symbolic links: {symbolic_paths}"
                )
        expected_run_id = evidence_root.name
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", expected_run_id) is None:
            failures.append("evidence root basename is not a valid run_id")

        command_schema = load(repo / "contracts/evidence/command/v1/schema.json")
        edge_summary_schema = load(
            repo / "contracts/evidence/v1/edge-module-schema.json"
        )
        blackbox_schema = load(repo / "contracts/evidence/edge-blackbox/v1/schema.json")
        soak_schema = load(repo / "contracts/evidence/soak/v1/schema.json")
        command_runs: dict[str, dict[str, Any]] = {}
        run_ids: set[str] = set()
        source_tree_digests: set[str] = set()
        status_digests: set[str] = set()

        if evidence_root.is_dir():
            expected_sidecars = {
                f"{command_id}.command.json" for command_id in binding_by_command
            }
            observed_sidecars = {
                path.relative_to(evidence_root).as_posix()
                for path in evidence_root.rglob("*.command.json")
            }
            unknown_sidecars = observed_sidecars - expected_sidecars
            if unknown_sidecars:
                failures.append(f"unbound command sidecars: {sorted(unknown_sidecars)}")
            for command_id, binding in binding_by_command.items():
                log_relative = str(binding["log"])
                command_relative = f"{command_id}.command.json"
                log_lexical = evidence_root / log_relative
                command_lexical = evidence_root / command_relative
                present = log_lexical.is_file() or command_lexical.is_file()
                if not present and not binding.get("required"):
                    continue
                if path_chain_has_symlink(
                    evidence_root, log_lexical
                ) or path_chain_has_symlink(evidence_root, command_lexical):
                    failures.append(
                        f"execution binding {command_id} contains a symbolic link"
                    )
                    continue
                log_path = log_lexical.resolve()
                command_path = command_lexical.resolve()
                if not log_path.is_relative_to(
                    evidence_root
                ) or not command_path.is_relative_to(evidence_root):
                    failures.append(
                        f"execution binding {command_id} escapes the run root"
                    )
                    continue
                if (
                    not log_path.is_file()
                    or log_path.is_symlink()
                    or not command_path.is_file()
                    or command_path.is_symlink()
                ):
                    failures.append(f"execution binding {command_id} is incomplete")
                    continue
                if (
                    log_path.stat().st_size > MAX_ARTIFACT_BYTES
                    or command_path.stat().st_size > MAX_ARTIFACT_BYTES
                ):
                    failures.append(f"execution binding {command_id} is oversized")
                    continue
                add_artifact(artifact_index, log_relative, log_path, "text/plain")
                add_artifact(
                    artifact_index, command_relative, command_path, "application/json"
                )
                try:
                    document = load(command_path)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    failures.append(
                        f"invalid command evidence {command_relative}: {error}"
                    )
                    continue
                failures.extend(
                    schema_failures(command_schema, document, command_relative)
                )
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                if document.get("command_id") != command_id:
                    failures.append(f"{command_relative} command_id drifted")
                if document.get("run_id") != expected_run_id:
                    failures.append(
                        f"{command_relative} run_id does not match the evidence directory"
                    )
                log_document = (
                    document.get("log") if isinstance(document.get("log"), dict) else {}
                )
                if log_document.get("path") != log_relative:
                    failures.append(
                        f"{command_relative} does not bind its exact log path"
                    )
                if log_document.get("sha256") != sha256(log_path):
                    failures.append(f"{command_relative} log digest mismatch")
                if log_document.get("bytes") != log_path.stat().st_size:
                    failures.append(f"{command_relative} log byte count mismatch")
                expected = command_expected_result(document.get("exit_code"))
                if expected is None:
                    failures.append(f"{command_relative} has invalid exit_code")
                else:
                    expected_result, expected_qualification, expected_reason = expected
                    if document.get("result") != expected_result:
                        failures.append(
                            f"{command_relative} result does not match exit_code"
                        )
                    if document.get("qualification") != expected_qualification:
                        failures.append(
                            f"{command_relative} qualification does not match exit_code"
                        )
                    if (
                        expected_reason is not None
                        and document.get("stable_reason") != expected_reason
                    ):
                        if not (
                            expected_result == "FAIL"
                            and document.get("stable_reason")
                            == "COMMAND_EVIDENCE_WRITE_FAILURE"
                        ):
                            failures.append(
                                f"{command_relative} stable_reason does not match exit_code"
                            )
                started = parse_timestamp(document.get("started_at"))
                finished = parse_timestamp(document.get("finished_at"))
                if started is None or finished is None or finished < started:
                    failures.append(
                        f"{command_relative} has invalid execution timestamps"
                    )
                if document.get("working_directory") != "edge-rs":
                    failures.append(f"{command_relative} working directory drifted")
                run_ids.add(str(document.get("run_id")))
                source_tree_digests.add(str(document.get("source_tree_digest")))
                status_digests.add(str(document.get("working_tree_status_digest")))
                command_runs[command_id] = {
                    "document": document,
                    "log_path": log_path,
                    "command_path": command_path,
                    "passed_tests": exact_passing_rust_tests(log_text),
                    "binding": binding,
                }
                if binding.get("required") and document.get("result") == "FAIL":
                    failures.append(f"required execution binding {command_id} failed")
        if len(run_ids) > 1:
            failures.append("command evidence run_id values are inconsistent")
        if len(source_tree_digests) > 1:
            failures.append(
                "command evidence source_tree_digest values are inconsistent"
            )
        if len(status_digests) > 1:
            failures.append("command evidence working-tree digests are inconsistent")

        evidence_cache: dict[str, dict[str, Any] | None] = {}
        for relative_text, catalog in catalog_by_path.items():
            lexical_path = evidence_root / relative_text
            if path_chain_has_symlink(evidence_root, lexical_path):
                failures.append(
                    f"catalog evidence contains a symbolic link: {relative_text}"
                )
                evidence_cache[relative_text] = None
                continue
            path = lexical_path.resolve()
            if not path.is_relative_to(evidence_root):
                failures.append(f"catalog evidence escapes run root: {relative_text}")
                evidence_cache[relative_text] = None
                continue
            if not path.is_file() or path.is_symlink():
                if catalog.get("required"):
                    failures.append(f"catalog evidence is missing: {relative_text}")
                evidence_cache[relative_text] = None
                continue
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                failures.append(f"catalog evidence is oversized: {relative_text}")
                evidence_cache[relative_text] = None
                continue
            add_artifact(artifact_index, relative_text, path, "application/json")
            try:
                document = load(path)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"invalid JSON evidence {relative_text}: {error}")
                evidence_cache[relative_text] = None
                continue
            evidence_cache[relative_text] = document
            schema_version = document.get("schema_version")
            if schema_version != catalog.get("schema_version"):
                failures.append(
                    f"{relative_text} schema_version does not match its catalog"
                )
            if (
                not isinstance(schema_version, str)
                or schema_version not in KNOWN_JSON_EVIDENCE_VERSIONS
            ):
                failures.append(
                    f"{relative_text} has unknown schema_version {schema_version}"
                )
            elif schema_version in EDGE_SUMMARY_VERSIONS:
                failures.extend(
                    schema_failures(edge_summary_schema, document, relative_text)
                )
            elif schema_version in BLACKBOX_VERSIONS:
                failures.extend(
                    schema_failures(blackbox_schema, document, relative_text)
                )
            elif schema_version == "qualification-soak/v1":
                failures.extend(schema_failures(soak_schema, document, relative_text))
            if document.get("test_id") != catalog.get("test_id"):
                failures.append(f"{relative_text} test_id does not match its catalog")
            if document.get("requirement_ids") != catalog.get("requirement_ids"):
                failures.append(
                    f"{relative_text} requirement_ids do not exactly match its catalog"
                )
            result_value = document.get("result")
            qualification_value = document.get("qualification")
            result = normalized_result(result_value)
            qualification = normalized_qualification(qualification_value)
            if result_value != result:
                failures.append(f"{relative_text} has invalid result")
            if qualification_value != qualification:
                failures.append(f"{relative_text} has invalid qualification")
            if result == "PASS" and qualification != "QUALIFIED":
                failures.append(f"{relative_text} PASS is not QUALIFIED")
            if result != "PASS" and qualification != "NOT_QUALIFIED":
                failures.append(f"{relative_text} non-PASS is incorrectly QUALIFIED")
            if result == "FAIL":
                failures.append(f"{relative_text} records a failed gate")

            producers = list(catalog.get("producer_command_ids", []))
            if schema_version == "qualification-soak/v1":
                expected_producer = (
                    "formal-soak" if document.get("level") == "MODULE" else "tests"
                )
                producers = [expected_producer]
            available_producers = [
                command_runs[value] for value in producers if value in command_runs
            ]
            if not available_producers:
                failures.append(f"{relative_text} has no executed cataloged producer")
            for producer in available_producers:
                producer_document = producer["document"]
                if producer["binding"].get("mode") == "rust-libtest":
                    if producer_document.get("result") != "PASS":
                        failures.append(
                            f"{relative_text} Rust producer command did not pass"
                        )
                    for target in catalog.get("producer_test_targets", []):
                        if target not in producer["passed_tests"]:
                            failures.append(
                                f"{relative_text} lacks exact producer libtest PASS {target}"
                            )
                elif producer_document.get("result") == "FAIL":
                    failures.append(f"{relative_text} producer command failed")
                if producer["binding"].get("mode") == "command" and (
                    result != producer_document.get("result")
                    or qualification != producer_document.get("qualification")
                ):
                    failures.append(
                        f"{relative_text} result does not match its command-mode producer"
                    )
                if schema_version in EDGE_SUMMARY_VERSIONS and document.get(
                    "source_tree_digest"
                ) != producer_document.get("source_tree_digest"):
                    failures.append(
                        f"{relative_text} source tree does not match its producer"
                    )

        bound_supplemental_claims: set[str] = set()
        oci_probe_relative = "oci-smoke/oci-probe.json"
        oci_probe_path = evidence_root / oci_probe_relative
        oci_parent = evidence_cache.get("oci-smoke/oci-smoke-evidence.json")
        oci_probe_expected = isinstance(oci_parent, dict) and isinstance(
            oci_parent.get("probe"), dict
        )
        if oci_probe_expected or oci_probe_path.exists() or oci_probe_path.is_symlink():
            if (
                path_chain_has_symlink(evidence_root, oci_probe_path)
                or not oci_probe_path.is_file()
            ):
                failures.append("OCI probe evidence is missing or is a symbolic link")
            elif oci_probe_path.stat().st_size > MAX_ARTIFACT_BYTES:
                failures.append("OCI probe evidence is oversized")
            else:
                try:
                    oci_probe = load(oci_probe_path)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    failures.append(f"invalid OCI probe evidence: {error}")
                else:
                    if (
                        not isinstance(oci_parent, dict)
                        or oci_parent.get("probe") != oci_probe
                    ):
                        failures.append(
                            "OCI probe is not exactly embedded in its cataloged parent"
                        )
                    else:
                        bound_supplemental_claims.add(oci_probe_relative)
                        add_artifact(
                            artifact_index,
                            oci_probe_relative,
                            oci_probe_path,
                            "application/json",
                        )

        if isinstance(oci_parent, dict):
            bind_oci_archive_subordinates(
                evidence_root=evidence_root,
                oci_parent=oci_parent,
                failures=failures,
                artifact_index=artifact_index,
                bound_claims=bound_supplemental_claims,
            )

        supply_latest_relative = "supply-chain/latest.json"
        supply_latest_path = evidence_root / supply_latest_relative
        if (
            path_chain_has_symlink(evidence_root, supply_latest_path)
            or not supply_latest_path.is_file()
        ):
            failures.append(
                "supply-chain latest pointer is missing or is a symbolic link"
            )
        else:
            try:
                supply_latest = load(supply_latest_path)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"invalid supply-chain latest pointer: {error}")
            else:
                source_pointer = supply_latest.get("evidence")
                if not safe_relative(source_pointer):
                    failures.append(
                        "supply-chain latest pointer has an unsafe evidence path"
                    )
                else:
                    latest_run_id = supply_latest.get("run_id")
                    source_parts = Path(str(source_pointer)).parts
                    supply_source_lexical = supply_latest_path.parent / str(
                        source_pointer
                    )
                    supply_source_path = supply_source_lexical.resolve()
                    if (
                        not isinstance(latest_run_id, str)
                        or re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", latest_run_id
                        )
                        is None
                        or len(source_parts) != 3
                        or source_parts[0] != "runs"
                        or source_parts[1] != latest_run_id
                        or source_parts[2] != "supply-chain.json"
                        or path_chain_has_symlink(evidence_root, supply_source_lexical)
                        or not supply_source_path.is_relative_to(
                            supply_latest_path.parent
                        )
                        or not supply_source_path.is_file()
                    ):
                        failures.append(
                            "supply-chain latest pointer escapes or is missing"
                        )
                    else:
                        source_relative = supply_source_path.relative_to(
                            evidence_root
                        ).as_posix()
                        try:
                            supply_source = load(supply_source_path)
                        except (OSError, ValueError, json.JSONDecodeError) as error:
                            failures.append(f"invalid pointed supply evidence: {error}")
                        else:
                            supply_parent = evidence_cache.get(
                                "supply-chain/supply-chain.json"
                            )
                            if (
                                not isinstance(supply_parent, dict)
                                or supply_parent != supply_source
                                or supply_latest.get("digest")
                                != sha256(supply_source_path)
                                or supply_source.get("run_id")
                                not in {
                                    latest_run_id,
                                    f"rust-edge-agent-supply-{latest_run_id}",
                                }
                            ):
                                failures.append(
                                    "pointed supply evidence is not exactly bound to its cataloged copy"
                                )
                            else:
                                bound_supplemental_claims.add(source_relative)
                                add_artifact(
                                    artifact_index,
                                    source_relative,
                                    supply_source_path,
                                    "application/json",
                                )
                                bind_supply_subordinates(
                                    repo=repo,
                                    evidence_root=evidence_root,
                                    supply_source_lexical=supply_source_lexical,
                                    supply_parent=supply_parent,
                                    oci_parent=oci_parent
                                    if isinstance(oci_parent, dict)
                                    else None,
                                    failures=failures,
                                    artifact_index=artifact_index,
                                    bound_claims=bound_supplemental_claims,
                                )
                bound_supplemental_claims.add(supply_latest_relative)
                add_artifact(
                    artifact_index,
                    supply_latest_relative,
                    supply_latest_path,
                    "application/json",
                )

        aggregator_outputs = {
            "gate-summary.json": "edge-module-gate-summary/v1",
            "traceability-summary.json": "rust-edge-agent-traceability-evidence/v1",
        }
        for relative, expected_schema in aggregator_outputs.items():
            output_path = evidence_root / relative
            if output_path.is_file() and not path_chain_has_symlink(
                evidence_root, output_path
            ):
                try:
                    output_document = load(output_path)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    failures.append(f"invalid aggregator output {relative}: {error}")
                else:
                    if output_document.get("schema_version") != expected_schema:
                        failures.append(
                            f"aggregator output {relative} has unknown schema"
                        )
                    bound_supplemental_claims.add(relative)

        known_json_paths = (
            set(catalog_by_path)
            | {f"{command_id}.command.json" for command_id in binding_by_command}
            | bound_supplemental_claims
        )
        for candidate in evidence_root.rglob("*.json"):
            relative = candidate.relative_to(evidence_root).as_posix()
            if relative in known_json_paths:
                continue
            if path_chain_has_symlink(evidence_root, candidate):
                failures.append(f"unbound JSON artifact is a symbolic link: {relative}")
                continue
            if candidate.stat().st_size > MAX_ARTIFACT_BYTES:
                failures.append(f"unbound JSON artifact is oversized: {relative}")
                continue
            try:
                json.loads(
                    candidate.read_text(encoding="utf-8"),
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(value)
                    ),
                )
            except (OSError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"invalid unbound JSON artifact {relative}: {error}")
                continue
            failures.append(f"unbound JSON artifact: {relative}")

        for item in requirements:
            if not isinstance(item, dict):
                continue
            requirement_id = str(item.get("requirement_id", ""))
            resolved_entries: list[dict[str, Any]] = []
            for relative in item.get("evidence_files", []):
                relative_text = str(relative)
                if relative_text.endswith(".log"):
                    matching = [
                        (command_id, run)
                        for command_id, run in command_runs.items()
                        if run["binding"].get("log") == relative_text
                    ]
                    if len(matching) != 1:
                        failures.append(
                            f"{requirement_id} log has no unique command binding: {relative_text}"
                        )
                        continue
                    command_id, run = matching[0]
                    command_document = run["document"]
                    resolved_entries.append(
                        {
                            "path": relative_text,
                            "kind": "execution-log",
                            "schema_version": "edge-command-execution/v1",
                            "test_id": None,
                            "requirement_ids": [],
                            "result": normalized_result(command_document.get("result")),
                            "qualification": normalized_qualification(
                                command_document.get("qualification")
                            ),
                            "command_id": command_id,
                            "command_evidence_path": f"{command_id}.command.json",
                            "command_evidence_digest": sha256(run["command_path"]),
                            "log_digest": sha256(run["log_path"]),
                            "exit_code": command_document.get("exit_code"),
                        }
                    )
                    continue
                document = evidence_cache.get(relative_text)
                if document is None:
                    failures.append(
                        f"{requirement_id} cannot resolve JSON evidence {relative_text}"
                    )
                    continue
                document_requirements = document.get("requirement_ids", [])
                if requirement_id not in document_requirements:
                    failures.append(
                        f"{requirement_id} is absent from its exact JSON evidence {relative_text}"
                    )
                test_id = document.get("test_id")
                if test_id is not None and test_id not in set(
                    item.get("scenario_ids", [])
                ):
                    failures.append(
                        f"{requirement_id} evidence {relative_text} has unrelated test_id {test_id}"
                    )
                resolved_entries.append(
                    {
                        "path": relative_text,
                        "kind": "json-evidence",
                        "schema_version": document.get("schema_version"),
                        "test_id": test_id,
                        "requirement_ids": document_requirements,
                        "result": normalized_result(document.get("result")),
                        "qualification": normalized_qualification(
                            document.get("qualification")
                        ),
                    }
                )

            target_results: list[dict[str, Any]] = []
            for target in item.get("test_targets", []):
                target_text = str(target)
                command_id = target_to_command.get(target_text, "")
                run = command_runs.get(command_id)
                target_result = "FAIL"
                target_qualification = "NOT_QUALIFIED"
                if run is None:
                    failures.append(
                        f"{requirement_id} target has no command evidence: {target_text}"
                    )
                else:
                    command_document = run["document"]
                    command_result = normalized_result(command_document.get("result"))
                    command_qualification = normalized_qualification(
                        command_document.get("qualification")
                    )
                    if run["binding"].get("mode") == "rust-libtest":
                        if command_result == "PASS":
                            if target_text in run["passed_tests"]:
                                target_result = "PASS"
                                target_qualification = "QUALIFIED"
                            else:
                                failures.append(
                                    f"{requirement_id} lacks an exact passing libtest record: {target_text}"
                                )
                        else:
                            target_result = command_result
                            target_qualification = command_qualification
                    else:
                        target_result = command_result
                        target_qualification = command_qualification
                    if target_result == "FAIL":
                        failures.append(
                            f"{requirement_id} test target failed: {target_text}"
                        )
                target_results.append(
                    {
                        "target": target_text,
                        "command_id": command_id,
                        "result": target_result,
                        "qualification": target_qualification,
                    }
                )

            evidence_results = [
                str(entry.get("result", "FAIL")) for entry in resolved_entries
            ] + [str(entry["result"]) for entry in target_results]
            evidence_qualifications = [
                str(entry.get("qualification", "NOT_QUALIFIED"))
                for entry in resolved_entries
            ] + [str(entry["qualification"]) for entry in target_results]
            requirement_evidence.append(
                {
                    "requirement_id": requirement_id,
                    "scenario_ids": item.get("scenario_ids", []),
                    "test_targets": target_results,
                    "evidence": resolved_entries,
                    "evidence_result": worst_result(evidence_results),
                    "qualification": "QUALIFIED"
                    if evidence_qualifications
                    and all(value == "QUALIFIED" for value in evidence_qualifications)
                    else "NOT_QUALIFIED",
                    "qualification_limit": item.get("qualification_limit"),
                }
            )

    failures = list(dict.fromkeys(failures))
    manifest_digest = sha256(manifest_path)
    summary = {
        "schema_version": "rust-edge-agent-traceability-evidence/v1",
        "module_id": manifest.get("module_id"),
        "manifest": str(manifest_path),
        "manifest_digest": manifest_digest,
        "evidence_root": str(args.evidence_root.resolve())
        if args.evidence_root
        else None,
        "result": "FAIL" if failures else "PASS",
        "qualification": "NOT_QUALIFIED"
        if args.evidence_root is None
        or failures
        or any(item["qualification"] != "QUALIFIED" for item in requirement_evidence)
        else "QUALIFIED",
        "requirements": requirement_evidence,
        "requirement_ids": sorted(ids),
        "conditional_applicability": conditional,
        "artifacts": [artifact_index[path] for path in sorted(artifact_index)],
        "failures": failures,
        "valid": not failures,
    }
    traceability_schema = load(repo / "contracts/evidence/traceability/v1/schema.json")
    summary_schema_errors = schema_failures(
        traceability_schema, summary, "traceability summary"
    )
    if summary_schema_errors:
        failures.extend(summary_schema_errors)
        summary["result"] = "FAIL"
        summary["qualification"] = "NOT_QUALIFIED"
        summary["failures"] = failures
        summary["valid"] = False
    if args.summary_out is not None:
        summary_path = args.summary_out.resolve()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if failures:
        raise SystemExit("\n".join(failures))
    print(
        json.dumps(
            {
                "manifest_digest": manifest_digest,
                "result": summary["result"],
                "qualification": summary["qualification"],
                "requirements": len(requirements),
                "artifacts": len(artifact_index),
                "evidence_aggregated": args.evidence_root is not None,
                "valid": summary["valid"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
