"""Bounded restricted MCP 2025-11-25 client implementation."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from mcp.types import JSONRPCError, JSONRPCNotification, JSONRPCRequest, JSONRPCResponse
from pydantic import ValidationError

from .canonical import canonical_digest, go_json_bytes
from .constants import MCP_VERSION, RESOURCE_URIS, TOOL_ARGUMENT_FIELDS
from .errors import AnalysisError, MCPUnavailable
from .models import BindingObservation, FrozenInput, MCPConfig, ProviderToolRequest
from .security import client_ssl_context
from .transport import PinnedHTTPClient


@dataclass(frozen=True, slots=True)
class ToolResult:
    request_id: str
    kind: str
    name: str
    target_ref: str
    status: str
    content_digest: str
    observed_at_unix_ms: int
    quality: str
    truncated: bool
    structured: dict[str, Any]

    def trajectory_record(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "name": self.name,
            "target_ref": self.target_ref,
            "status": self.status,
            "content_digest": self.content_digest,
            "observed_at_unix_ms": self.observed_at_unix_ms,
            "quality": self.quality,
            "truncated": self.truncated,
        }


class MCPClient:
    def __init__(self, config: MCPConfig, *, production: bool, max_connections: int) -> None:
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
        self.last_status = "not_called"
        self.last_observed_unix_ms = 0

    def _headers(
        self,
        input_bundle: FrozenInput,
        binding: BindingObservation,
        round_number: int,
        session_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_VERSION,
            "Origin": self._config.origin,
            "X-MASI-Plugin-ID": input_bundle.plugin_id,
            "X-MASI-Binding-Generation": str(binding.binding_generation),
            "X-MASI-MCP-Round": str(round_number),
            "X-MASI-Trace-ID": input_bundle.trace_id,
        }
        if session_id:
            headers["MCP-Session-Id"] = session_id
        if input_bundle.plugin_id == "masi.analysis.langgraph" and self._config.base_url.startswith("http://"):
            headers["X-MASI-Test-Plugin-ID"] = input_bundle.plugin_id
        return headers

    async def _rpc(
        self,
        request: dict[str, Any],
        headers: dict[str, str],
        *,
        timeout_ms: int,
        response_bytes: int,
        allow_empty: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            if "id" in request:
                JSONRPCRequest.model_validate(request)
            else:
                JSONRPCNotification.model_validate(request)
        except ValidationError as exc:
            raise AnalysisError("MCP_REQUEST_REJECTED", "MCP SDK rejected the outbound envelope", 500) from exc
        response = await self._client.request(
            "POST",
            "/mcp",
            headers=headers,
            body=go_json_bytes(request),
            timeout_ms=timeout_ms,
            max_response_bytes=response_bytes,
            allow_empty=allow_empty,
        )
        if response.status_code not in (200, 202):
            raise MCPUnavailable(f"MCP returned status {response.status_code}")
        session_id = response.headers.get("MCP-Session-Id")
        if response.status_code == 202 and not response.body:
            return None, session_id
        media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        body = response.body
        if media == "text/event-stream":
            data_lines = [line[5:].strip() for line in body.decode().splitlines() if line.startswith("data:")]
            if len(data_lines) != 1:
                raise MCPUnavailable("MCP SSE response framing rejected")
            body = data_lines[0].encode()
        elif media != "application/json":
            raise MCPUnavailable("MCP response content type rejected")
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPUnavailable("MCP response JSON malformed") from exc
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or ("result" in value) == ("error" in value):
            raise MCPUnavailable("MCP response envelope malformed")
        if "error" in value:
            try:
                JSONRPCError.model_validate(value)
            except ValidationError as exc:
                raise MCPUnavailable("MCP SDK rejected the error envelope") from exc
            error = value["error"]
            message = str(error.get("message", "MCP error")) if isinstance(error, dict) else "MCP error"
            raise MCPUnavailable(message[:128])
        try:
            JSONRPCResponse.model_validate(value)
        except ValidationError as exc:
            raise MCPUnavailable("MCP SDK rejected the result envelope") from exc
        return value, session_id

    async def execute(
        self,
        requests: list[ProviderToolRequest],
        input_bundle: FrozenInput,
        binding: BindingObservation,
        *,
        round_number: int = 1,
    ) -> list[ToolResult]:
        if not requests:
            return []
        if round_number < 1 or round_number > input_bundle.budgets.mcp_rounds:
            raise AnalysisError("MCP_BUDGET_EXHAUSTED", "MCP round budget exhausted", 422)
        bounded = requests[: input_bundle.budgets.tool_calls]
        initialize = {
            "jsonrpc": "2.0",
            "id": f"init-{input_bundle.run_id}",
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "masi-analysis", "version": "1.0.0"},
            },
        }
        session_id: str | None = None
        try:
            init_response, session_id = await self._rpc(
                initialize,
                self._headers(input_bundle, binding, round_number),
                timeout_ms=input_bundle.budgets.tool_timeout_ms,
                response_bytes=input_bundle.budgets.tool_response_bytes,
            )
            if not session_id or not init_response or init_response.get("result", {}).get("protocolVersion") != MCP_VERSION:
                raise MCPUnavailable("MCP initialization identity mismatch")
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            await self._rpc(
                notification,
                self._headers(input_bundle, binding, round_number, session_id),
                timeout_ms=input_bundle.budgets.tool_timeout_ms,
                response_bytes=1024,
                allow_empty=True,
            )
            semaphore = asyncio.Semaphore(input_bundle.budgets.tool_parallelism)

            async def one(index: int, requested: ProviderToolRequest) -> ToolResult:
                async with semaphore:
                    return await self._execute_one(index, requested, input_bundle, binding, session_id, round_number)

            results = await asyncio.gather(*(one(index, request) for index, request in enumerate(bounded, 1)))
            total = sum(len(go_json_bytes(result.structured)) for result in results)
            if total > input_bundle.budgets.tool_total_response_bytes:
                raise MCPUnavailable("MCP total response byte budget exceeded")
            self.last_status = "ok"
            self.last_observed_unix_ms = time.time_ns() // 1_000_000
            return list(results)
        except AnalysisError:
            self.last_status = "unavailable"
            self.last_observed_unix_ms = time.time_ns() // 1_000_000
            raise
        finally:
            if session_id:
                try:
                    await self._client.request(
                        "DELETE",
                        "/mcp",
                        headers=self._headers(input_bundle, binding, round_number, session_id),
                        body=None,
                        timeout_ms=input_bundle.budgets.tool_timeout_ms,
                        max_response_bytes=1024,
                        allow_empty=True,
                    )
                except AnalysisError:
                    pass

    async def _execute_one(
        self,
        index: int,
        requested: ProviderToolRequest,
        input_bundle: FrozenInput,
        binding: BindingObservation,
        session_id: str,
        round_number: int,
    ) -> ToolResult:
        request_id = f"tool-{input_bundle.run_id}-{index}"
        target_ref = ""
        if requested.kind == "tool":
            if requested.name not in binding.tool_allowlist or requested.name not in TOOL_ARGUMENT_FIELDS:
                raise AnalysisError("MCP_TOOL_DENIED", "provider requested a non-allowlisted tool", 422)
            argument_field, ref_kind = TOOL_ARGUMENT_FIELDS[requested.name]
            if set(requested.arguments) != {argument_field} or not isinstance(requested.arguments[argument_field], str):
                raise AnalysisError("MCP_TOOL_DENIED", "provider tool arguments rejected", 422)
            target_ref = requested.arguments[argument_field]
            refs = {
                "event": input_bundle.event_refs,
                "incident": input_bundle.incident_refs,
                "evidence": input_bundle.evidence_refs,
                "runtime": input_bundle.runtime_refs,
            }[ref_kind]
            if target_ref not in {item.id for item in refs}:
                raise AnalysisError("MCP_TOOL_DENIED", "provider tool target is outside frozen input", 422)
            method = "tools/call"
            params = {"name": requested.name, "arguments": requested.arguments}
        else:
            if requested.name not in binding.resource_allowlist or requested.name not in RESOURCE_URIS:
                raise AnalysisError("MCP_RESOURCE_DENIED", "provider requested a non-allowlisted resource", 422)
            if requested.arguments:
                raise AnalysisError("MCP_RESOURCE_DENIED", "resource request arguments rejected", 422)
            target_ref = requested.name
            method = "resources/read"
            params = {"uri": RESOURCE_URIS[requested.name]}
        rpc_request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        response, _ = await self._rpc(
            rpc_request,
            self._headers(input_bundle, binding, round_number, session_id),
            timeout_ms=input_bundle.budgets.tool_timeout_ms,
            response_bytes=input_bundle.budgets.tool_response_bytes,
        )
        if response is None or response.get("id") != request_id or not isinstance(response.get("result"), dict):
            raise MCPUnavailable("MCP result identity mismatch")
        result = response["result"]
        if result.get("isError") is True:
            status = "failed"
            quality = "invalid"
        else:
            status = "succeeded"
            quality = "valid"
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            structured = result
        content_digest = canonical_digest(structured)
        return ToolResult(
            request_id,
            requested.kind,
            requested.name,
            target_ref,
            status,
            content_digest,
            time.time_ns() // 1_000_000,
            quality,
            False,
            structured,
        )

    async def close(self) -> None:
        await self._client.close()


def trajectory_digest(results: list[ToolResult]) -> str:
    return canonical_digest([result.trajectory_record() for result in results])
