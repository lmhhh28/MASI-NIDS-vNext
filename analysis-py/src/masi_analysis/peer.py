"""Bounded outbound A2A delegation used only as untrusted analysis context."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .canonical import canonical_digest, go_json_bytes
from .concurrency import gather_cancel_on_error
from .constants import A2A_VERSION, PLUGIN_ID
from .errors import AnalysisError, PeerUnavailable
from .models import FrozenInput, PeerConfig, PeerTask, PeerTaskResponse, ProviderDelegation
from .security import client_ssl_context, require_safe_output
from .transport import PinnedHTTPClient


@dataclass(frozen=True, slots=True)
class PeerResult:
    peer_id: str
    remote_task_id: str
    content_digest: str
    summary: str
    status: str
    observed_at_unix_ms: int

    def trajectory_record(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "remote_task_id": self.remote_task_id,
            "content_digest": self.content_digest,
            "status": self.status,
            "observed_at_unix_ms": self.observed_at_unix_ms,
        }


class PeerClient:
    def __init__(self, configs: list[PeerConfig], *, production: bool) -> None:
        self._peers: dict[str, tuple[PeerConfig, PinnedHTTPClient]] = {}
        for config in configs:
            if config.peer_id in self._peers:
                raise AnalysisError("PEER_CONFIG_REJECTED", "duplicate peer identity", 503)
            context = client_ssl_context(config.ca_file, config.cert_file, config.key_file)
            self._peers[config.peer_id] = (
                config,
                PinnedHTTPClient(
                    config.base_url,
                    config.allowed_ips,
                    config.server_name,
                    context,
                    production=production,
                    max_connections=2,
                ),
            )
        self.last_status = "not_called"
        self.last_observed_unix_ms = 0

    async def delegate(
        self,
        requests: list[ProviderDelegation],
        input_bundle: FrozenInput,
        *,
        allowed_peer_ids: set[str],
        secret_values: tuple[str, ...],
    ) -> list[PeerResult]:
        if not requests:
            return []
        if input_bundle.budgets.delegation_depth >= 1:
            raise AnalysisError("LOOP_DETECTED", "delegation depth exhausted", 422)
        if len(requests) > input_bundle.budgets.outbound_delegations:
            raise AnalysisError(
                "A2A_BUDGET_EXHAUSTED",
                "provider delegation set exceeds the remaining outbound budget",
                422,
            )
        seen: set[str] = set()
        for requested in requests:
            if requested.peer_id not in allowed_peer_ids:
                raise AnalysisError("PEER_CAPABILITY_DENIED", "provider requested a peer outside the exact binding", 422)
            if requested.peer_id in seen or requested.peer_id in input_bundle.delegation_path:
                raise AnalysisError("LOOP_DETECTED", "duplicate or cyclic A2A delegation", 422)
            seen.add(requested.peer_id)

        async def one(index: int, request: ProviderDelegation) -> PeerResult:
            return await self._delegate_one(index, request, input_bundle, secret_values)

        try:
            results = await gather_cancel_on_error(one(index, request) for index, request in enumerate(requests, 1))
            self.last_status = "ok"
            self.last_observed_unix_ms = time.time_ns() // 1_000_000
            return list(results)
        except AnalysisError:
            self.last_status = "unavailable"
            self.last_observed_unix_ms = time.time_ns() // 1_000_000
            raise

    async def _delegate_one(
        self,
        index: int,
        requested: ProviderDelegation,
        input_bundle: FrozenInput,
        secret_values: tuple[str, ...],
    ) -> PeerResult:
        peer = self._peers.get(requested.peer_id)
        if peer is None:
            raise PeerUnavailable("provider requested an undeclared A2A peer")
        config, client = peer
        question = require_safe_output(requested.question, secret_values)
        request_id = f"delegate-{input_bundle.run_id}-{index}"
        delegation = {
            "schema_version": "masi-analysis-delegation/v1",
            "request_id": request_id,
            "parent_task_id": input_bundle.task_id,
            "parent_input_digest": input_bundle.input_digest,
            "question": question,
            "evidence_refs": [item.model_dump(mode="json") for item in input_bundle.evidence_refs],
            "delegation_depth": 1,
            "delegation_path": [*input_bundle.delegation_path, PLUGIN_ID],
            "deadline_unix_ms": input_bundle.deadline_unix_ms,
            "trace_id": input_bundle.trace_id,
            "request_digest": "",
        }
        delegation["request_digest"] = canonical_digest({key: value for key, value in delegation.items() if key != "request_digest"})
        message = {
            "message": {
                "messageId": f"msg-{request_id}",
                "role": "ROLE_USER",
                "parts": [{"data": delegation, "mediaType": "application/json"}],
                "metadata": {
                    "schemaVersion": "masi-analysis-delegation/v1",
                    "inputDigest": delegation["request_digest"],
                    "pluginId": config.plugin_id,
                    "bindingGeneration": input_bundle.binding_generation,
                    "deadlineUnixMs": input_bundle.deadline_unix_ms,
                },
            },
            "configuration": {"acceptedOutputModes": ["application/json"]},
            "metadata": {
                "traceId": input_bundle.trace_id,
                "noPush": True,
                "noStreaming": True,
                "delegationDepth": 1,
                "delegationPath": delegation["delegation_path"],
            },
        }
        headers = {
            "A2A-Version": A2A_VERSION,
            "Content-Type": "application/a2a+json",
            "Accept": "application/a2a+json",
        }
        response = await client.request(
            "POST",
            "/message:send",
            headers=headers,
            body=go_json_bytes(message),
            timeout_ms=self._remaining_timeout_ms(input_bundle),
            max_response_bytes=input_bundle.budgets.a2a_response_bytes,
        )
        task = self.decode_task_response(
            response.status_code,
            response.headers,
            response.body,
            plugin_id=config.plugin_id,
            binding_generation=config.binding_generation,
            input_digest=str(delegation["request_digest"]),
            trace_id=input_bundle.trace_id,
        )
        remote_id = task.task_id
        remote_context_id = task.context_id
        for _ in range(input_bundle.budgets.polls_per_task):
            state = task.status.state
            if state not in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                break
            await asyncio.sleep(0.05)
            polled = await client.request(
                "GET",
                "/tasks/" + remote_id,
                headers={"A2A-Version": A2A_VERSION, "Accept": "application/a2a+json"},
                body=None,
                timeout_ms=self._remaining_timeout_ms(input_bundle),
                max_response_bytes=input_bundle.budgets.a2a_response_bytes,
            )
            task = self.decode_task_response(
                polled.status_code,
                polled.headers,
                polled.body,
                plugin_id=config.plugin_id,
                binding_generation=config.binding_generation,
                input_digest=str(delegation["request_digest"]),
                trace_id=input_bundle.trace_id,
            )
            if task.task_id != remote_id or task.context_id != remote_context_id:
                raise PeerUnavailable("A2A peer task identity changed during polling")
        state = task.status.state
        if state != "TASK_STATE_COMPLETED":
            raise PeerUnavailable("A2A peer did not complete within the polling budget")
        artifact = task.artifacts[0]
        data = artifact.parts[0].data
        data_payload = data.model_dump(mode="json")
        content_digest = canonical_digest({key: value for key, value in data_payload.items() if key != "content_digest"})
        if content_digest != data.content_digest or artifact.metadata.content_digest != data.content_digest:
            raise PeerUnavailable("A2A peer Artifact content digest mismatch")
        summary = require_safe_output(data.summary, secret_values)
        return PeerResult(config.peer_id, remote_id, data.content_digest, summary, "succeeded", time.time_ns() // 1_000_000)

    @staticmethod
    def _remaining_timeout_ms(input_bundle: FrozenInput) -> int:
        remaining = input_bundle.deadline_unix_ms - time.time_ns() // 1_000_000
        if remaining <= 0:
            raise PeerUnavailable("A2A parent deadline elapsed")
        return min(remaining, input_bundle.budgets.graph_deadline_ms, 5000)

    @staticmethod
    def decode_task_response(
        status: int,
        headers: Any,
        body: bytes,
        *,
        plugin_id: str,
        binding_generation: int,
        input_digest: str,
        trace_id: str,
    ) -> PeerTask:
        if status != 200 or headers.get("A2A-Version") != A2A_VERSION:
            raise PeerUnavailable("A2A peer status or version rejected")
        if headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/a2a+json":
            raise PeerUnavailable("A2A peer content type rejected")
        try:
            task = PeerTaskResponse.model_validate_json(body).task
        except ValidationError as exc:
            raise PeerUnavailable("A2A peer response failed the strict task contract") from exc
        metadata = task.metadata
        if (
            metadata.plugin_id != plugin_id
            or metadata.binding_generation != binding_generation
            or metadata.input_digest != input_digest
            or metadata.trace_id != trace_id
        ):
            raise PeerUnavailable("A2A peer task binding or input identity mismatch")
        return task

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for _, client in self._peers.values()))


def peer_trajectory_digest(results: list[PeerResult]) -> str:
    return canonical_digest([result.trajectory_record() for result in results])
