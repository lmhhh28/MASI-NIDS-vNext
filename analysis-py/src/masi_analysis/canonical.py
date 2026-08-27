"""Canonical JSON and digest adapters shared with the Go A2A consumer."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .models import AnalysisArtifact, FrozenInput, ProviderRequest, ProviderResponse


def _go_escape(raw: str) -> str:
    return raw.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _normalize_go_numbers(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number rejected")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, dict):
        return {key: _normalize_go_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_go_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_go_numbers(item) for item in value]
    return value


def go_json_bytes(value: Any) -> bytes:
    """Match Go encoding/json compact UTF-8 output for the bounded contract types."""

    raw = json.dumps(_normalize_go_numbers(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return _go_escape(raw).encode()


def sorted_json_bytes(value: Any) -> bytes:
    """Project canonical JSON for self-digested config/provider records."""

    raw = json.dumps(_normalize_go_numbers(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return _go_escape(raw).encode()


def sha256_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_digest(sorted_json_bytes(value))


def file_digest(raw: bytes) -> str:
    return sha256_digest(raw)


def without_key(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {item_key: item_value for item_key, item_value in value.items() if item_key != key}


def stable_sequence_digest(items: Sequence[Mapping[str, Any]]) -> str:
    return canonical_digest(list(items))


def input_payload(input_bundle: FrozenInput) -> dict[str, Any]:
    payload = input_bundle.model_dump(mode="json")
    for key in ("event_refs", "incident_refs", "evidence_refs", "runtime_refs"):
        payload[key] = sorted(payload[key], key=lambda item: (item["id"], item["digest"]))
    return payload


def compute_input_digest(input_bundle: FrozenInput) -> str:
    payload = input_payload(input_bundle)
    payload["input_digest"] = ""
    return sha256_digest(go_json_bytes(payload))


def artifact_payload(artifact: AnalysisArtifact) -> dict[str, Any]:
    return artifact.model_dump(mode="json")


def compute_artifact_digest(artifact: AnalysisArtifact) -> str:
    payload = artifact_payload(artifact)
    payload["artifact_digest"] = ""
    return sha256_digest(go_json_bytes(payload))


def compute_provider_request_digest(request: ProviderRequest) -> str:
    payload = request.model_dump(mode="json")
    return canonical_digest(without_key(payload, "request_digest"))


def compute_provider_response_digest(response: ProviderResponse) -> str:
    payload = response.model_dump(mode="json")
    return canonical_digest(without_key(payload, "response_digest"))
