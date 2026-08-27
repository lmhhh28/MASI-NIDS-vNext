from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from masi_analysis.canonical import canonical_digest, compute_input_digest, file_digest, go_json_bytes
from masi_analysis.constants import INPUT_SCHEMA, PLUGIN_ID, SKILLS
from masi_analysis.models import AnalysisBudgets, FrozenInput

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION = "1" * 40


def json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def budgets(**overrides: int) -> AnalysisBudgets:
    values = {
        "llm_calls": 2,
        "mcp_rounds": 2,
        "tool_calls": 6,
        "tool_parallelism": 3,
        "tool_timeout_ms": 2000,
        "tool_response_bytes": 32768,
        "tool_total_response_bytes": 131072,
        "llm_timeout_ms": 6000,
        "result_budget_ms": 8000,
        "graph_deadline_ms": 30000,
        "artifact_bytes": 65536,
        "outbound_delegations": 2,
        "delegation_depth": 0,
        "polls_per_task": 3,
        "a2a_response_bytes": 131072,
    }
    values.update(overrides)
    return AnalysisBudgets.model_validate(values)


def frozen_input(*, task_id: str = "task-1", quality: str = "valid", content_request: str = "bounded analysis") -> FrozenInput:
    now_ms = time.time_ns() // 1_000_000
    value = FrozenInput.model_validate(
        {
            "schema_version": INPUT_SCHEMA,
            "task_id": task_id,
            "run_id": "run-" + task_id,
            "skill": "analyze_nids_incident",
            "plugin_id": PLUGIN_ID,
            "plugin_revision": REVISION,
            "config_digest": DIGEST_A,
            "binding_generation": 1,
            "scope": "tenant-test",
            "target_set_digest": DIGEST_A,
            "event_refs": [{"id": "event-1", "digest": DIGEST_A}],
            "incident_refs": [{"id": "incident-1", "digest": DIGEST_B}],
            "evidence_refs": [{"id": "evidence-1", "digest": DIGEST_A}, {"id": "explanation-1", "digest": DIGEST_B}],
            "runtime_refs": [{"id": "target-1", "digest": DIGEST_A}],
            "model_result_evidence_refs": ["evidence-1"],
            "model_explanation_evidence_refs": ["explanation-1"],
            "quality": quality,
            "provider_profile_digest": DIGEST_A,
            "prompt_profile_digest": DIGEST_A,
            "tool_policy_digest": DIGEST_A,
            "redaction_profile_digest": DIGEST_A,
            "provenance_digest": DIGEST_A,
            "budgets": budgets().model_dump(mode="json"),
            "input_digest": DIGEST_A,
            "deadline_unix_ms": now_ms + 20_000,
            "expires_at_unix_ms": now_ms + 60_000,
            "locale": "zh-CN",
            "content_request": content_request,
            "idempotency_key": "idem-" + task_id,
            "delegation_path": [],
            "trace_id": "trace-" + task_id,
        }
    )
    return value.model_copy(update={"input_digest": compute_input_digest(value)})


def manifest(*, peer_ids: list[str] | None = None) -> dict[str, Any]:
    capabilities = [
        {"capability_id": "analysis-a2a/v1", "capability_kind": "a2a-agent", "declared": True},
        {"capability_id": "masi.events.get", "capability_kind": "mcp-tool", "declared": True},
        {"capability_id": "masi.incidents.get", "capability_kind": "mcp-tool", "declared": True},
        {"capability_id": "masi.evidence.get", "capability_kind": "mcp-tool", "declared": True},
        {"capability_id": "masi.targets.get", "capability_kind": "mcp-tool", "declared": True},
        {"capability_id": "masi.incidents.open", "capability_kind": "mcp-resource", "declared": True},
        {"capability_id": "masi.evidence.recent", "capability_kind": "mcp-resource", "declared": True},
    ]
    return {
        "schema_version": "masi-plugin-manifest/v1",
        "manifest_id": "masi-analysis-manifest",
        "manifest_revision": 1,
        "manifest_digest": DIGEST_A,
        "plugin_id": PLUGIN_ID,
        "plugin_revision": REVISION,
        "kind": "analysis-agent",
        "publisher": "masi-nids",
        "version": "1.0.0",
        "scope": "tenant-test",
        "artifact_digest": DIGEST_A,
        "artifact_media_type": "application/vnd.oci.image.manifest.v1+json",
        "entrypoint": "masi-analysis",
        "supported_platforms": ["linux/amd64"],
        "host_api_version": "plugin-host-control/v1",
        "input_contract_digest": DIGEST_A,
        "output_contract_digest": DIGEST_B,
        "config_schema_id": "plugin-config/v1",
        "config_schema_version": "masi-plugin-config/v1",
        "config_schema_digest": DIGEST_A,
        "capabilities": capabilities,
        "skill_ids": list(SKILLS),
        "mcp_tool_ids": ["masi.events.get", "masi.incidents.get", "masi.evidence.get", "masi.targets.get"],
        "mcp_resource_ids": ["masi.incidents.open", "masi.evidence.recent"],
        "a2a_peer_ids": peer_ids or [],
        "network_egress_capability_ids": ["masi.mcp.egress", "masi.provider.egress"],
        "filesystem_preopens": [],
        "secret_ref_ids": [],
        "statistics_definitions": [],
        "resource_limits": {
            "cpu_milli": 1000,
            "memory_bytes": 268435456,
            "pid_count": 64,
            "fd_count": 256,
            "disk_bytes": 67108864,
            "linear_memory_bytes": 0,
            "table_elements": 0,
            "instance_count": 1,
            "batch_records": 1,
            "in_flight_bytes": 4194304,
            "concurrency": 16,
            "deadline_ms": 30000,
            "retry_max_attempts": 1,
            "output_bytes": 65536,
            "queue_depth": 64,
        },
        "runtime_profile": "grpc-service/v1",
        "wit_digest": None,
        "service_proto_digest": DIGEST_A,
        "sbom_digest": DIGEST_A,
        "provenance_digest": DIGEST_A,
        "signature_status": "signed",
        "verification_policy_digest": DIGEST_A,
        "verification_bundle_profile": "plugin-verification-bundle/v1",
        "owner_ref": "analysis-owner",
        "support_level": "first-party",
        "compatibility": {
            "host_api_min": "plugin-host-control/v1",
            "host_api_max": "plugin-host-control/v1",
            "runtime_profile": "grpc-service/v1",
            "migration_required": False,
            "rollback_compatible_revisions": [],
        },
        "lifecycle_contract": {
            "startup": "plugin-startup/fail-closed-v1",
            "readiness": "plugin-readiness/exact-binding-v1",
            "liveness": "plugin-liveness/progress-v1",
            "drain": "plugin-drain/bounded-v1",
            "failure": "plugin-failure/stable-reason-v1",
            "fallback": "plugin-fallback/none-v1",
        },
        "observability_contract_digest": DIGEST_A,
        "actor_ref": "analysis-owner",
        "reason_code": "QUALIFIED",
        "trace_id": "trace-manifest",
        "created_at_unix_ms": 1,
    }


def write_runtime(
    root: Path,
    contract_root: Path,
    *,
    service_port: int = 18080,
    mcp_port: int = 18081,
    provider_port: int = 18082,
    peer_port: int | None = None,
    tls_files: dict[str, str] | None = None,
    limits_override: dict[str, int] | None = None,
    binding_ttl_seconds: int = 3600,
) -> Path:
    if binding_ttl_seconds < 1:
        raise ValueError("binding_ttl_seconds must be positive")
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    config_path = root / "config.json"
    binding_path = root / "binding.json"
    peer_ids = ["fixture-peer"] if peer_port is not None else []
    manifest_raw = json_bytes(manifest(peer_ids=peer_ids))
    manifest_path.write_bytes(manifest_raw)
    config: dict[str, Any] = {
        "schema_version": "masi-analysis-config/v1",
        "runtime_profile": "acceptance",
        "plugin_id": PLUGIN_ID,
        "contract_root": str(contract_root),
        "manifest_path": str(manifest_path),
        "binding_path": str(binding_path),
        "store_path": str(root / "analysis.sqlite3"),
        "health_state_path": str(root / "health.json"),
        "listen": {"host": "127.0.0.1", "port": service_port},
        "tls": {
            "enabled": tls_files is not None,
            "cert_file": tls_files["server_cert"] if tls_files else None,
            "key_file": tls_files["server_key"] if tls_files else None,
            "client_ca_file": tls_files["ca"] if tls_files else None,
            "allowed_client_sans": ["masi-auditor", "masi-control"],
        },
        "limits": {
            "request_bytes": 65536,
            "response_bytes": 131072,
            "artifact_bytes": 65536,
            "queue_depth": 8,
            "concurrency": 2,
            "task_retention_seconds": 60,
            "max_tasks": 128,
            "max_trace_events_per_task": 64,
            "shutdown_grace_ms": 2000,
        },
        "mcp": {
            "profile_id": "masi-mcp-readonly/v1",
            "profile_digest": DIGEST_A,
            "base_url": f"{'https://localhost' if tls_files else 'http://127.0.0.1'}:{mcp_port}",
            "origin": "https://analysis.test",
            "allowed_ips": ["127.0.0.1"],
            "server_name": "localhost",
            "ca_file": tls_files["ca"] if tls_files else None,
            "cert_file": tls_files["module_cert"] if tls_files else None,
            "key_file": tls_files["module_key"] if tls_files else None,
            "tool_allowlist": ["masi.events.get", "masi.incidents.get", "masi.evidence.get", "masi.targets.get"],
            "resource_allowlist": ["masi.incidents.open", "masi.evidence.recent"],
        },
        "provider": {
            "profile_id": "analysis-provider/v1",
            "profile_digest": DIGEST_A,
            "provider_id": "fixture-provider",
            "model_id": "fixture-model",
            "base_url": f"{'https://localhost' if tls_files else 'http://127.0.0.1'}:{provider_port}",
            "allowed_ips": ["127.0.0.1"],
            "server_name": "localhost",
            "ca_file": tls_files["ca"] if tls_files else None,
            "cert_file": tls_files["module_cert"] if tls_files else None,
            "key_file": tls_files["module_key"] if tls_files else None,
            "auth_secret_file": None,
        },
        "a2a_peers": (
            [
                {
                    "peer_id": "fixture-peer",
                    "plugin_id": "fixture.peer.agent",
                    "binding_generation": 1,
                    "base_url": f"{'https://localhost' if tls_files else 'http://127.0.0.1'}:{peer_port}",
                    "allowed_ips": ["127.0.0.1"],
                    "server_name": "localhost",
                    "ca_file": tls_files["ca"] if tls_files else None,
                    "cert_file": tls_files["module_cert"] if tls_files else None,
                    "key_file": tls_files["module_key"] if tls_files else None,
                }
            ]
            if peer_port is not None
            else []
        ),
        "public_agent_card_url": f"{'https://localhost' if tls_files else 'http://127.0.0.1'}:{service_port}/.well-known/agent-card.json",
        "config_id": "analysis-config-test",
    }
    if limits_override:
        config["limits"].update(limits_override)
    config_raw = json_bytes(config)
    config_path.write_bytes(config_raw)
    now_ms = time.time_ns() // 1_000_000
    binding = {
        "schema_version": "masi-analysis-binding/v1",
        "plugin_id": PLUGIN_ID,
        "plugin_revision": REVISION,
        "manifest_digest": DIGEST_A,
        "manifest_content_digest": file_digest(manifest_raw),
        "config_digest": file_digest(config_raw),
        "capability_digest": DIGEST_A,
        "resource_profile_digest": DIGEST_A,
        "qualification_digest": DIGEST_A,
        "binding_generation": 1,
        "scope": "tenant-test",
        "activation_state": "active",
        "qualification_status": "qualified",
        "skill_ids": list(SKILLS),
        "tool_allowlist": ["masi.events.get", "masi.incidents.get", "masi.evidence.get", "masi.targets.get"],
        "resource_allowlist": ["masi.incidents.open", "masi.evidence.recent"],
        "a2a_peer_allowlist": peer_ids,
        "provider_profile_digest": DIGEST_A,
        "prompt_profile_digest": DIGEST_A,
        "tool_policy_digest": DIGEST_A,
        "redaction_profile_digest": DIGEST_A,
        "issued_at_unix_ms": now_ms,
        "expires_at_unix_ms": now_ms + binding_ttl_seconds * 1000,
        "manager_identity": "fake-manager",
        "binding_digest": DIGEST_A,
    }
    binding["binding_digest"] = canonical_digest({key: value for key, value in binding.items() if key != "binding_digest"})
    binding_path.write_bytes(go_json_bytes(binding))
    return config_path
