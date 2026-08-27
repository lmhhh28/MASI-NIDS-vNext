#!/usr/bin/env python3
"""Validate Offline ML public contracts, profiles, traceability, supply registry, and source sentinels."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from masi_offline_ml.contracts import validate_public_contracts


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not an ordinary file: {path}")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    module = repo / "ml-py"
    if arguments.output.exists() or arguments.output.is_symlink():
        raise SystemExit("output already exists")
    checks: dict[str, bool] = {}

    contract_validation = validate_public_contracts(repo)
    checks["dataset_explanation_contracts"] = contract_validation["result"] == "PASS"
    schemas = sorted((repo / "contracts/evidence").glob("offline-ml-*/v1/schema.json"))
    for schema_path in schemas:
        Draft202012Validator.check_schema(load(schema_path))
    checks["evidence_schemas_meta_valid"] = len(schemas) >= 12

    findings_schema = load(repo / "contracts/evidence/module-findings/v1/schema.json")
    findings = load(module / "module-findings.json")
    findings_errors = list(Draft202012Validator(findings_schema, format_checker=FormatChecker()).iter_errors(findings))
    checks["findings_schema"] = not findings_errors
    checks["open_p0_zero"] = not any(
        item.get("status") == "OPEN" and item.get("severity") == "P0" for item in findings["findings"]
    )
    checks["open_findings_zero"] = not any(item.get("status") == "OPEN" for item in findings["findings"])

    traceability = load(module / "requirements-traceability.json")
    binding_ids = [str(item["command_id"]) for item in traceability["execution_bindings"]]
    checks["command_ids_unique"] = len(binding_ids) == len(set(binding_ids)) and len(binding_ids) >= 18
    checks["all_commands_required"] = all(item.get("required") is True for item in traceability["execution_bindings"])
    requirement_ids = [str(item["requirement_id"]) for item in traceability["requirements"]]
    baseline = (repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md").read_text(encoding="utf-8")
    checks["requirement_ids_unique"] = len(requirement_ids) == len(set(requirement_ids)) and len(requirement_ids) >= 20
    checks["requirement_ids_exist"] = all(
        re.search(rf"(?<![A-Z0-9-]){re.escape(value)}(?![A-Z0-9-])", baseline) for value in requirement_ids
    )
    checks["requirement_commands_exist"] = all(
        set(item["command_ids"]).issubset(set(binding_ids)) for item in traceability["requirements"]
    )
    public_contracts = [repo / str(path) for path in traceability["public_contracts"]]
    checks["public_contracts_ordinary"] = len(public_contracts) >= 16 and all(
        path.is_file() and not path.is_symlink() for path in public_contracts
    )
    checks["conditional_applicability_exact"] = len(traceability["conditional_applicability"]) == 4 and all(
        item.get("applicability") == "NOT_APPLICABLE"
        and item.get("result") == "NOT_RUN"
        and re.fullmatch(r"[A-Z][A-Z0-9_]{3,127}", str(item.get("stable_reason", "")))
        for item in traceability["conditional_applicability"]
    )

    registry_schema = load(repo / "contracts/supply-chain/v1/schema.json")
    registry = load(repo / "contracts/supply-chain/v1/offline-ml-components.json")
    notices = load(repo / "contracts/supply-chain/v1/offline-ml-third-party-notices.json")
    checks["component_registry_schema"] = not list(Draft202012Validator(registry_schema).iter_errors(registry))
    component_names = {str(item["name"]) for item in registry["components"]}
    notice_names = {str(item["component"]) for item in notices["notices"]}
    checks["component_notices_closed"] = len(component_names) >= 11 and component_names.issubset(notice_names)
    checks["runtime_download_false"] = all(item.get("runtime_download") is False for item in registry["components"])

    pyproject = tomllib.loads((module / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((module / "uv.lock").read_text(encoding="utf-8"))
    dependencies = cast(list[str], pyproject["project"]["dependencies"])
    locked = {str(item.get("name")): str(item.get("version")) for item in cast(list[dict[str, Any]], lock["package"])}
    checks["direct_dependencies_exact"] = all(
        "==" in dependency
        and locked.get(dependency.split("==", 1)[0].lower().replace("_", "-")) == dependency.split("==", 1)[1]
        for dependency in dependencies
    )
    lock_digest = digest(module / "uv.lock")
    lock_component = next(
        item for item in registry["components"] if item["name"] == "Offline ML uv.lock dependency closure"
    )
    checks["lock_registry_digest"] = lock_component["digest"] == lock_digest
    checks["pytest_absent"] = all(
        "pytest" not in dependency.lower()
        for dependency in dependencies + cast(list[str], pyproject["project"]["optional-dependencies"]["dev"])
    )

    source_payload = "\n".join(path.read_text(encoding="utf-8") for path in (module / "src").rglob("*.py"))
    checks["no_unfinished_markers"] = not re.search(r"\b(?:TODO|FIXME|NotImplementedError)\b", source_payload)
    checks["no_external_authority_imports"] = not re.search(
        r"^(?:from|import)\s+(?:psycopg|asyncpg|requests|httpx|grpc|p4runtime|scapy)",
        source_payload,
        re.MULTILINE,
    )
    checks["no_subprocess_in_runtime"] = not re.search(r"^(?:from|import)\s+subprocess\b", source_payload, re.MULTILINE)
    checks["no_other_module_source_import"] = not re.search(
        r"^(?:from|import)\s+(?:control_go|edge_rs|infer_cpp|analysis_py|plugin_host)",
        source_payload,
        re.MULTILINE,
    )
    dockerfile = (module / "Dockerfile").read_text(encoding="utf-8")
    runtime_profile = load(repo / "contracts/profiles/v1/offline-ml-runtime.json")
    builder_profile = cast(dict[str, Any], runtime_profile["python_builder_image"])
    libraries_profile = cast(dict[str, Any], runtime_profile["runtime_libraries_image"])
    base_profile = cast(dict[str, Any], runtime_profile["runtime_base_image"])
    python_builder_references = re.findall(
        r"^ARG PYTHON_BUILDER_IMAGE=([^\s]+@sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE
    )
    runtime_libraries_references = re.findall(
        r"^ARG RUNTIME_LIBRARIES_IMAGE=([^\s]+@sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE
    )
    runtime_references = re.findall(r"^ARG RUNTIME_IMAGE=([^\s]+@sha256:[0-9a-f]{64})$", dockerfile, re.MULTILINE)
    checks["oci_bases_digest_pinned"] = (
        python_builder_references == [builder_profile["reference"]]
        and runtime_libraries_references == [libraries_profile["reference"]]
        and runtime_references == [base_profile["reference"]]
    )
    runtime_library_files = {
        str(item["component"]): str(item["digest"]) for item in cast(list[dict[str, Any]], libraries_profile["files"])
    }
    checks["oci_runtime_libraries_exact"] = (
        runtime_library_files
        == {
            "zlib": "sha256:cd0d4cbe528e1d83875605c2aa10f0a9ea2d34a055d24db96642e6f78ad7206d",
            "libffi": "sha256:baae077ca48493f45210ce490bb281403b1d68362adef95495f5e0bc28e70eaa",
        }
        and libraries_profile.get("packaged_python_copied") is False
        and all(
            f'io.masi-nids.runtime-library.{name}.digest="{value}"' in dockerfile
            for name, value in runtime_library_files.items()
        )
    )
    checks["oci_runtime_binding_exact"] = (
        runtime_profile["python"]
        == {
            "implementation": "CPython",
            "version": "3.12.13",
            "path": "/usr/local/bin/python3.12",
        }
        and base_profile.get("packaged_python_is_fallback") is False
        and f'io.masi-nids.python-builder.digest="{builder_profile["digest"]}"' in dockerfile
        and f'io.masi-nids.runtime-libraries.image.digest="{libraries_profile["digest"]}"' in dockerfile
        and f'io.masi-nids.runtime-base.digest="{base_profile["digest"]}"' in dockerfile
        and 'io.masi-nids.runtime-base.python.fallback="false"' in dockerfile
    )
    checks["oci_non_root"] = "USER 65532:65532" in dockerfile
    checks["oci_offline_install"] = "RUN --network=none" in dockerfile and "PIP_NO_INDEX=1" in dockerfile

    p4_profile = load(repo / "contracts/profiles/v1/p4runtime-edge-compatibility.json")
    version_decision = cast(dict[str, str], p4_profile["version_decision"])
    checks["p4runtime_1_4_1_frozen"] = (
        p4_profile["accepted_capabilities_api_versions"] == ["1.4.1"]
        and version_decision.get("1.4.1") == "ACCEPT_FOR_PINNED_BMV2_PROFILE"
        and version_decision.get("1.3.0", "").startswith("REJECT_")
        and version_decision.get("1.4.0", "").startswith("REJECT_")
        and version_decision.get("unknown") == "REJECT_FAIL_CLOSED"
    )
    source_digest = (
        "sha256:"
        + subprocess.run(
            [str(module / ".venv/bin/python"), str(module / "scripts/source-tree-digest.py"), "--repo", str(repo)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
    )

    failures = sorted(name for name, passed in checks.items() if not passed)
    evidence = {
        "schema_version": "offline-ml-static-contract-evidence/v1",
        "module_id": "MOD-ML-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS" if not failures else "FAIL",
        "qualification": "NOT_QUALIFIED",
        "contract_validation": contract_validation,
        "schema_count": len(schemas),
        "public_contract_count": len(public_contracts),
        "requirement_count": len(requirement_ids),
        "component_count": len(registry["components"]),
        "lock_digest": lock_digest,
        "source_tree_digest": source_digest,
        "checks": checks,
        "failures": failures,
    }
    schema = load(repo / "contracts/evidence/offline-ml-static/v1/schema.json")
    if not failures:
        errors = list(Draft202012Validator(schema).iter_errors(evidence))
        if errors:
            raise SystemExit("static evidence schema rejected: " + "; ".join(error.message for error in errors))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
