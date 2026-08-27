"""Strict structured provider adapter; provider output never bypasses grounding."""

from __future__ import annotations

import json
import time

from pydantic import ValidationError

from .canonical import compute_provider_request_digest, compute_provider_response_digest, go_json_bytes
from .errors import AnalysisError, ProviderUnavailable
from .models import ProviderConfig, ProviderRequest, ProviderResponse
from .security import client_ssl_context, read_bounded_regular_file
from .transport import PinnedHTTPClient


class ProviderAdapter:
    def __init__(self, config: ProviderConfig, *, production: bool, max_connections: int) -> None:
        context = client_ssl_context(config.ca_file, config.cert_file, config.key_file)
        self._client = PinnedHTTPClient(
            config.base_url,
            config.allowed_ips,
            config.server_name,
            context,
            production=production,
            max_connections=max_connections,
        )
        self._config = config
        self._secret = ""
        if config.auth_secret_file:
            raw = read_bounded_regular_file(config.auth_secret_file, max_bytes=4096, secret=True)
            try:
                self._secret = raw.decode().strip()
            except UnicodeDecodeError as exc:
                raise AnalysisError("SECRET_REJECTED", "provider secret is not UTF-8", 503) from exc
            if not self._secret or len(self._secret) > 2048:
                raise AnalysisError("SECRET_REJECTED", "provider secret length rejected", 503)
        self.last_status = "not_called"
        self.last_observed_unix_ms = 0

    @property
    def provider_id(self) -> str:
        return self._config.provider_id

    @property
    def model_id(self) -> str:
        return self._config.model_id

    @property
    def profile_digest(self) -> str:
        return self._config.profile_digest

    @property
    def secret_values(self) -> tuple[str, ...]:
        return (self._secret,) if self._secret else ()

    async def analyze(self, request: ProviderRequest, *, timeout_ms: int, response_bytes: int) -> ProviderResponse:
        if compute_provider_request_digest(request) != request.request_digest:
            raise AnalysisError("PROVIDER_REQUEST_DIGEST_MISMATCH", "provider request digest mismatch", 500)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-MASI-Provider-Profile": "analysis-provider/v1",
            "X-MASI-Request-Digest": request.request_digest,
        }
        if self._secret:
            headers["Authorization"] = "Bearer " + self._secret
        started = time.monotonic()
        try:
            response = await self._client.request(
                "POST",
                "/v1/analyze",
                headers=headers,
                body=go_json_bytes(request.model_dump(mode="json")),
                timeout_ms=timeout_ms,
                max_response_bytes=response_bytes,
            )
            if response.status_code != 200:
                raise ProviderUnavailable(f"provider returned status {response.status_code}")
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ProviderUnavailable("provider content type rejected")
            try:
                decoded = ProviderResponse.model_validate_json(response.body)
            except ValidationError as exc:
                raise ProviderUnavailable("provider response failed strict schema") from exc
            if decoded.request_id != request.request_id or decoded.input_digest != request.input_digest:
                raise ProviderUnavailable("provider response identity mismatch")
            if compute_provider_response_digest(decoded) != decoded.response_digest:
                raise ProviderUnavailable("provider response digest mismatch")
            self.last_status = "ok"
            self.last_observed_unix_ms = time.time_ns() // 1_000_000
            return decoded
        except AnalysisError:
            self.last_status = "unavailable"
            self.last_observed_unix_ms = time.time_ns() // 1_000_000
            raise
        finally:
            _ = started

    async def close(self) -> None:
        await self._client.close()


def provider_response_bytes(response: ProviderResponse) -> bytes:
    return json.dumps(response.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode()
