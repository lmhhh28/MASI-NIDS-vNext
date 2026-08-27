#!/usr/bin/env python3
"""Validate the complete Analysis public contract, profile and golden closure."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular no-symlink JSON: {path}")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> None:
    repository = Path(__file__).resolve().parents[2]
    contracts = repository / "contracts"
    resources: list[tuple[str, Resource[Any]]] = []
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(contracts.rglob("*.json")):
        try:
            value = load(path)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if value.get("$schema") == "https://json-schema.org/draft/2020-12/schema" and isinstance(value.get("$id"), str):
            Draft202012Validator.check_schema(value)
            relative = path.relative_to(contracts).as_posix()
            schemas[relative] = value
            resources.append((value["$id"], Resource.from_contents(value)))
    registry = Registry().with_resources(resources)

    def validate(schema_name: str, document: dict[str, Any]) -> None:
        errors = sorted(
            Draft202012Validator(schemas[schema_name], registry=registry, format_checker=FormatChecker()).iter_errors(document),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            raise ValueError(schema_name + ": " + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors))

    golden = contracts / "analysis/v1/golden"
    mappings = {
        "input-v1.json": "analysis/input/v1/schema.json",
        "artifact-v1.json": "analysis/artifact/v1/schema.json",
        "a2a-send-request-v1.json": "analysis/a2a/v1/send-request.schema.json",
        "a2a-task-completed-v1.json": "analysis/a2a/v1/task-response.schema.json",
        "a2a-peer-task-completed-v1.json": "analysis/a2a/v1/peer-task-response.schema.json",
        "a2a-agent-card-v1.json": "analysis/a2a/v1/agent-card.schema.json",
        "a2a-error-v1.json": "analysis/a2a/v1/error.schema.json",
        "provider-request-v1.json": "analysis/provider/v1/schema.json",
        "provider-response-v1.json": "analysis/provider/v1/schema.json",
    }
    for filename, schema_name in mappings.items():
        validate(schema_name, load(golden / filename))
    catalog = load(golden / "catalog.json")
    for key, item in catalog.items():
        if key == "schema_version":
            continue
        target = golden / str(item["path"])
        raw = target.read_bytes().rstrip(b"\n")
        observed = "sha256:" + hashlib.sha256(raw).hexdigest()
        expected = item.get("file_digest", item.get("content_digest"))
        if observed != expected:
            raise ValueError(f"golden catalog digest mismatch: {key}")

    executable = copy.deepcopy(load(golden / "artifact-v1.json"))
    executable["non_executable"] = False
    if not list(Draft202012Validator(schemas["analysis/artifact/v1/schema.json"], registry=registry).iter_errors(executable)):
        raise ValueError("executable Artifact negative was accepted")
    unknown_major = copy.deepcopy(load(golden / "input-v1.json"))
    unknown_major["schema_version"] = "masi-analysis-input/v2"
    if not list(Draft202012Validator(schemas["analysis/input/v1/schema.json"]).iter_errors(unknown_major)):
        raise ValueError("unknown Analysis input major was accepted")

    validate("supply-chain/v1/schema.json", load(contracts / "supply-chain/v1/analysis-plugin-components.json"))
    expected_profiles = {
        "analysis-agent-runtime/v1",
        "analysis-provider/v1",
        "analysis-performance/v1",
        "analysis-soak/v1",
        "a2a-agent/v1",
        "masi-mcp-readonly/v1",
    }
    observed_profiles = {
        load(path).get("profile_id")
        for path in [
            contracts / "profiles/v1/analysis-agent-runtime.json",
            contracts / "profiles/v1/analysis-provider.json",
            contracts / "profiles/v1/analysis-performance.json",
            contracts / "profiles/v1/analysis-soak.json",
            contracts / "profiles/v1/a2a-agent.json",
            contracts / "profiles/v1/masi-mcp-readonly.json",
        ]
    }
    if observed_profiles != expected_profiles:
        raise ValueError("Analysis profile identity closure drifted")
    print(f"analysis contract validation: PASS ({len(schemas)} schemas, {len(mappings)} goldens)")


if __name__ == "__main__":
    main()
