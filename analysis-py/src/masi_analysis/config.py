"""Fail-closed loading of runtime config, public manifest, and Manager binding."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .canonical import canonical_digest, file_digest, without_key
from .constants import PLUGIN_ID, SKILLS
from .errors import AnalysisError
from .models import AnalysisConfig, BindingObservation
from .security import read_bounded_regular_file, validate_outbound_url, validate_storage_path


def _decode_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError("CONFIG_MALFORMED", f"{label} is not strict JSON", 503) from exc
    if not isinstance(value, dict):
        raise AnalysisError("CONFIG_MALFORMED", f"{label} must be a JSON object", 503)
    return value


def _schema(contract_root: Path, relative: str) -> dict[str, Any]:
    raw = read_bounded_regular_file(str(contract_root / relative), max_bytes=512 * 1024)
    return _decode_json(raw, relative)


def _validate_schema(instance: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        location = "/".join(str(item) for item in errors[0].absolute_path)
        raise AnalysisError("CONFIG_SCHEMA_REJECTED", f"{label} rejected at {location or '/'}", 503)


@dataclass(frozen=True, slots=True)
class RuntimeMaterial:
    config: AnalysisConfig
    config_digest: str
    manifest: dict[str, Any]
    manifest_content_digest: str
    binding: BindingObservation
    contract_root: Path
    store_path: Path


def load_runtime_material(config_path: str) -> RuntimeMaterial:
    config_raw = read_bounded_regular_file(config_path, max_bytes=256 * 1024)
    config_doc = _decode_json(config_raw, "analysis config")
    try:
        config = AnalysisConfig.model_validate(config_doc)
    except ValidationError as exc:
        raise AnalysisError("CONFIG_SCHEMA_REJECTED", "analysis config failed strict validation", 503) from exc
    contract_root = Path(config.contract_root)
    if not contract_root.is_absolute() or contract_root.is_symlink() or not contract_root.is_dir():
        raise AnalysisError("CONTRACT_ROOT_REJECTED", "contract root must be an absolute ordinary directory", 503)
    _validate_schema(config_doc, _schema(contract_root, "analysis/config/v1/schema.json"), "analysis config")
    production = config.runtime_profile == "production"
    if production and not config.tls.enabled:
        raise AnalysisError("TLS_CONFIG_REJECTED", "production A2A requires mTLS", 503)
    if config.tls.enabled and not (config.tls.cert_file and config.tls.key_file and config.tls.client_ca_file):
        raise AnalysisError("TLS_CONFIG_REJECTED", "enabled A2A TLS requires cert, key, and client CA", 503)
    validate_outbound_url(config.mcp.base_url, config.mcp.allowed_ips, production=production)
    validate_outbound_url(config.provider.base_url, config.provider.allowed_ips, production=production)
    for peer in config.a2a_peers:
        validate_outbound_url(peer.base_url, peer.allowed_ips, production=production)
    config_digest = file_digest(config_raw)

    manifest_raw = read_bounded_regular_file(config.manifest_path, max_bytes=512 * 1024)
    manifest = _decode_json(manifest_raw, "plugin manifest")
    _validate_schema(manifest, _schema(contract_root, "plugin/manifest/v1/schema.json"), "plugin manifest")
    manifest_content_digest = file_digest(manifest_raw)

    binding_raw = read_bounded_regular_file(config.binding_path, max_bytes=256 * 1024)
    binding_doc = _decode_json(binding_raw, "analysis binding")
    _validate_schema(binding_doc, _schema(contract_root, "analysis/binding/v1/schema.json"), "analysis binding")
    try:
        binding = BindingObservation.model_validate(binding_doc)
    except ValidationError as exc:
        raise AnalysisError("BINDING_REJECTED", "analysis binding failed strict validation", 503) from exc
    if canonical_digest(without_key(binding_doc, "binding_digest")) != binding.binding_digest:
        raise AnalysisError("BINDING_DIGEST_MISMATCH", "analysis binding digest mismatch", 503)
    _cross_validate(config, config_digest, manifest, manifest_content_digest, binding)
    store_path = validate_storage_path(config.store_path, production)
    validate_storage_path(config.health_state_path, production)
    return RuntimeMaterial(config, config_digest, manifest, manifest_content_digest, binding, contract_root, store_path)


def _cross_validate(
    config: AnalysisConfig,
    config_digest: str,
    manifest: dict[str, Any],
    manifest_content_digest: str,
    binding: BindingObservation,
) -> None:
    if config.plugin_id != PLUGIN_ID or binding.plugin_id != PLUGIN_ID:
        raise AnalysisError("PLUGIN_IDENTITY_MISMATCH", "plugin identity mismatch", 503)
    if binding.config_digest != config_digest or binding.manifest_content_digest != manifest_content_digest:
        raise AnalysisError("BINDING_DIGEST_MISMATCH", "binding does not cover exact config and manifest bytes", 503)
    expected = {
        "plugin_id": PLUGIN_ID,
        "plugin_revision": binding.plugin_revision,
        "manifest_digest": binding.manifest_digest,
        "kind": "analysis-agent",
        "scope": binding.scope,
        "signature_status": "signed",
        "runtime_profile": "grpc-service/v1",
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise AnalysisError("MANIFEST_BINDING_MISMATCH", f"manifest field {field} differs from binding", 503)
    if tuple(sorted(manifest.get("skill_ids", []))) != tuple(sorted(SKILLS)):
        raise AnalysisError("MANIFEST_CAPABILITY_MISMATCH", "manifest does not declare exactly the qualified skills", 503)
    capabilities = {
        (item.get("capability_id"), item.get("capability_kind"))
        for item in manifest.get("capabilities", [])
        if isinstance(item, dict) and item.get("declared") is True
    }
    if ("analysis-a2a/v1", "a2a-agent") not in capabilities:
        raise AnalysisError("MANIFEST_CAPABILITY_MISMATCH", "analysis-a2a/v1 capability missing", 503)
    for tool in binding.tool_allowlist:
        if (tool, "mcp-tool") not in capabilities:
            raise AnalysisError("MANIFEST_CAPABILITY_MISMATCH", "binding tool exceeds manifest capability", 503)
    for resource in binding.resource_allowlist:
        if (resource, "mcp-resource") not in capabilities:
            raise AnalysisError("MANIFEST_CAPABILITY_MISMATCH", "binding resource exceeds manifest capability", 503)
    if not set(binding.tool_allowlist).issubset(config.mcp.tool_allowlist):
        raise AnalysisError("CONFIG_CAPABILITY_MISMATCH", "binding tool exceeds runtime config allowlist", 503)
    if not set(binding.resource_allowlist).issubset(config.mcp.resource_allowlist):
        raise AnalysisError("CONFIG_CAPABILITY_MISMATCH", "binding resource exceeds runtime config allowlist", 503)
    configured_peers = {peer.peer_id for peer in config.a2a_peers}
    if not set(binding.a2a_peer_allowlist).issubset(configured_peers):
        raise AnalysisError("CONFIG_CAPABILITY_MISMATCH", "binding A2A peer exceeds runtime config allowlist", 503)
    if not set(binding.a2a_peer_allowlist).issubset(set(manifest.get("a2a_peer_ids", []))):
        raise AnalysisError("MANIFEST_CAPABILITY_MISMATCH", "binding A2A peer exceeds manifest allowlist", 503)
    if binding.provider_profile_digest != config.provider.profile_digest:
        raise AnalysisError("PROVIDER_PROFILE_MISMATCH", "binding/provider profile mismatch", 503)


class BindingState:
    """Refreshable observed binding; never a replacement for Go canonical lifecycle facts."""

    def __init__(self, material: RuntimeMaterial) -> None:
        self._lock = threading.RLock()
        self._material = material

    def snapshot(self, now_ms: int | None = None) -> BindingObservation:
        current_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock:
            binding = self._material.binding
            if binding.expires_at_unix_ms <= current_ms:
                raise AnalysisError("BINDING_UNAVAILABLE", "binding observation expired", 503)
            if binding.activation_state != "active" or binding.qualification_status != "qualified":
                raise AnalysisError("BINDING_UNAVAILABLE", "exact qualified active binding unavailable", 503)
            return binding

    def material(self) -> RuntimeMaterial:
        with self._lock:
            return self._material

    def reload(self, config_path: str) -> BindingObservation:
        replacement = load_runtime_material(config_path)
        with self._lock:
            previous = self._material.binding
            current = replacement.binding
            if current.plugin_id != previous.plugin_id or current.scope != previous.scope:
                raise AnalysisError("BINDING_FENCED", "binding reload changed immutable plugin scope", 503)
            if current.binding_generation < previous.binding_generation:
                raise AnalysisError("BINDING_FENCED", "binding generation regressed", 503)
            if current.binding_generation == previous.binding_generation and current.binding_digest != previous.binding_digest:
                allowed_state_change = previous.activation_state == "active" and current.activation_state in {
                    "draining",
                    "disabled",
                    "revoked",
                    "expired",
                }
                if not allowed_state_change:
                    raise AnalysisError("BINDING_FENCED", "same generation binding content drifted", 503)
            self._material = replacement
            return current

    def matches_input(self, input_bundle: Any, now_ms: int) -> BindingObservation:
        binding = self.snapshot(now_ms)
        if (
            input_bundle.plugin_id != binding.plugin_id
            or input_bundle.plugin_revision != binding.plugin_revision
            or input_bundle.config_digest != binding.config_digest
            or input_bundle.binding_generation != binding.binding_generation
            or input_bundle.scope != binding.scope
            or input_bundle.skill not in binding.skill_ids
            or input_bundle.provider_profile_digest != binding.provider_profile_digest
            or input_bundle.prompt_profile_digest != binding.prompt_profile_digest
            or input_bundle.tool_policy_digest != binding.tool_policy_digest
            or input_bundle.redaction_profile_digest != binding.redaction_profile_digest
        ):
            raise AnalysisError("BINDING_FENCED", "task does not match exact active binding", 409)
        return binding
