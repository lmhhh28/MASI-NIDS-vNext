#!/usr/bin/env python3
"""Validate Plugin Host public schema/profile closure and negative semantics."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"contract is not a regular no-symlink file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    host_root = Path(__file__).resolve().parent.parent
    repo = host_root.parent
    schemas = [
        repo / "contracts/plugin/manifest/v1/schema.json",
        repo / "contracts/plugin/config/v1/schema.json",
        repo / "contracts/plugin/host/v1/config.schema.json",
        repo / "contracts/plugin/host/v1/verification-bundle.schema.json",
        repo / "contracts/plugin/statistics/v1/schema.json",
        repo / "contracts/evidence/plugin-runtime-host-oci/v1/schema.json",
        repo / "contracts/evidence/plugin-runtime-host-supply/v1/schema.json",
        repo / "contracts/evidence/plugin-runtime-host-performance/v1/schema.json",
        repo / "contracts/evidence/plugin-runtime-host-soak/v1/schema.json",
        repo / "contracts/evidence/plugin-runtime-host-deep/v1/schema.json",
        repo / "contracts/evidence/plugin-runtime-host-fault/v1/schema.json",
        repo / "contracts/evidence/plugin-runtime-host-module/v1/schema.json",
        repo / "contracts/supply-chain/v1/schema.json",
    ]
    for path in schemas:
        Draft202012Validator.check_schema(load(path))

    host_profile = load(repo / "contracts/profiles/v1/plugin-runtime-host.json")
    service_profile = load(repo / "contracts/profiles/v1/grpc-service.json")
    wasm_profile = load(repo / "contracts/profiles/v1/wasm-component.json")
    statistics_profile = load(repo / "contracts/profiles/v1/plugin-statistics.json")
    display_profile = load(repo / "contracts/profiles/v1/plugin-statistics-display.json")
    manifest_schema_document = load(repo / "contracts/plugin/manifest/v1/schema.json")
    statistics_schema_document = load(repo / "contracts/plugin/statistics/v1/schema.json")
    component_registry = load(
        repo / "contracts/supply-chain/v1/plugin-runtime-host-components.json"
    )
    Draft202012Validator(load(repo / "contracts/supply-chain/v1/schema.json")).validate(
        component_registry
    )
    failures: list[str] = []
    checks = {
        "host_proto_digest": host_profile.get("host_proto_digest")
        == digest(repo / "contracts/plugin/host/v1/host.proto"),
        "service_proto_digest": service_profile.get("service_proto_digest")
        == digest(repo / "contracts/plugin/service/v1/service.proto"),
        "wit_digest": wasm_profile.get("wit_digest")
        == digest(repo / "contracts/plugin/wit/v1/pure-transform.wit"),
        "toolchain_exact": host_profile.get("rust_toolchain") == "1.97.1"
        and host_profile.get("wasmtime") == "47.0.3",
        "closed_runtimes": host_profile.get("runtime_profiles")
        == ["wasm-component/v1", "grpc-service/v1"],
        "closed_managed_kinds": host_profile.get("managed_kinds")
        == ["pure-transform", "read-only-tool"],
        "analysis_direct_only": host_profile.get("direct_not_proxied_kinds")
        == ["analysis-agent"],
        "wasi_02_exact": wasm_profile.get("wasi_profile")
        == "WASI 0.2 Preview 2 Component Model",
        "wasi_03_rejected": wasm_profile.get("wasi_0_3_admission") == "reject",
        "no_wasm_imports": wasm_profile.get("imports") == [],
        "service_no_retry": service_profile.get("limits", {}).get("max_attempts") == 1
        and service_profile.get("retry", {}).get("transparent_retry") is False
        and service_profile.get("retry", {}).get("retryable_statuses") == []
        and service_profile.get("retry", {}).get("max_attempts") == 1
        and service_profile.get("retry", {}).get("throttling", {}).get("enabled") is False
        and all(
            method.get("retry") is False
            and method.get("total_deadline_ms", 0) >= method.get("execution_deadline_ms", 0)
            for method in service_profile.get("methods", {}).values()
        ),
        "manifest_required_surface": {
            "artifact_digest", "entrypoint", "supported_platforms", "host_api_version",
            "input_contract_digest", "output_contract_digest", "config_schema_id",
            "config_schema_version", "config_schema_digest", "network_egress_capability_ids",
            "filesystem_preopens", "secret_ref_ids", "statistics_definitions", "compatibility",
            "lifecycle_contract", "observability_contract_digest"
        }.issubset(set(manifest_schema_document.get("required", []))),
        "host_resource_profile_exact": host_profile.get("limits")
        == {
            "bindings": 32,
            "global_queue": 64,
            "per_binding_queue": 32,
            "per_binding_in_flight": 2,
            "control_message_bytes": 4194304,
            "input_bytes": 2097152,
            "output_bytes": 1048576,
            "deadline_ms": 10000,
        },
        "statistics_resource_profile_exact": statistics_profile.get("limits", {}).get("numeric_points")
        == 10000
        and statistics_profile.get("limits", {}).get("total_rows") == 2000
        and statistics_profile.get("limits", {}).get("artifact_bytes") == 1048576,
        "display_is_declarative": display_profile.get("raw_json_fallback") is False
        and "JavaScript" in display_profile.get("forbidden", []),
        "runtime_download_forbidden": all(
            profile.get("runtime_download") is False
            for profile in [host_profile, service_profile, wasm_profile, statistics_profile, display_profile]
        ),
        "component_registry_closed": component_registry.get("module")
        == "plugin-runtime-host"
        and len(component_registry.get("components", [])) >= 11
        and all(
            component.get("runtime_download") is False
            for component in component_registry.get("components", [])
        ),
    }
    for name, passed in checks.items():
        if not passed:
            failures.append(name)

    statistics_golden = load(
        repo / "contracts/plugin/statistics/v1/golden/input-bundle-v1.json"
    )
    if list(Draft202012Validator(statistics_schema_document).iter_errors(statistics_golden)):
        failures.append("statistics_input_golden_rejected")

    host_schema = Draft202012Validator(load(schemas[2]))
    valid_host = {
        "schema_version": "plugin-host-config/v1",
        "host_id": "host.test",
        "profile_id": "plugin-runtime-host/v1",
        "listen_address": "127.0.0.1:7445",
        "health_address": "127.0.0.1:8080",
        "artifact_cache_root": "/var/lib/masi-plugin-host/artifacts",
        "server_tls": {
            "certificate_path": "/run/tls/server.pem",
            "private_key_path": "/run/tls/server.key",
            "client_ca_path": "/run/tls/ca.pem",
            "allowed_client_certificate_sha256": ["sha256:" + "a" * 64],
        },
        "trust": {
            "trust_policy_digest": "sha256:" + "b" * 64,
            "revocation_max_age_ms": 300000,
            "publishers": [
                {"publisher_identity": "publisher.test", "ed25519_public_key_hex": "01" * 32}
            ],
        },
        "limits": {
            "max_bindings": 32,
            "global_queue_depth": 64,
            "per_binding_queue_depth": 32,
            "per_binding_in_flight": 2,
            "max_control_message_bytes": 4194304,
            "max_input_bytes": 2097152,
            "max_output_bytes": 1048576,
            "max_deadline_ms": 10000,
            "queue_wait_ms": 1000,
            "wasm_fuel": 50000000,
            "wasm_linear_memory_bytes": 67108864,
            "wasm_table_elements": 10000,
            "wasm_instances": 32,
            "wasm_memories": 2,
            "wasm_tables": 4,
            "wasm_stack_bytes": 2097152,
            "epoch_tick_ms": 10,
            "failure_threshold": 3,
            "circuit_open_ms": 1000,
            "restart_window_ms": 600000,
            "max_restarts_in_window": 5,
            "quarantine_ms": 900000,
            "drain_deadline_ms": 10000,
            "shutdown_deadline_ms": 10000,
        },
        "service_endpoints": [],
    }
    if list(host_schema.iter_errors(valid_host)):
        failures.append("valid_host_config_rejected")
    for mutation in [
        {**valid_host, "schema_version": "plugin-host-config/v2"},
        {**valid_host, "unexpected": True},
        {**valid_host, "limits": {**valid_host["limits"], "global_queue_depth": 65}},
    ]:
        if not list(host_schema.iter_errors(mutation)):
            failures.append("negative_host_config_accepted")

    wit = repo / "contracts/plugin/wit/v1/pure-transform.wit"
    subprocess.run(
        ["wasm-tools", "component", "embed", str(wit), "--dummy", "-o", "/dev/null"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    source = wit.read_text(encoding="utf-8")
    if not re.search(r"world\s+pure-transform\s*\{", source) or "import " in source:
        failures.append("wit_world_or_import_drift")
    if failures:
        print(json.dumps({"valid": False, "failures": failures}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"valid": True, "checks": sorted(checks), "schemas": [str(path.relative_to(repo)) for path in schemas]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
