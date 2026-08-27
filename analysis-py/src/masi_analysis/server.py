"""Real A2A HTTP+JSON server, mTLS identity gate, health, trace, and metrics."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import ssl
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from .canonical import go_json_bytes
from .config import RuntimeMaterial
from .constants import A2A_VERSION, SKILLS, VERSION
from .errors import AnalysisError
from .security import atomic_write_private, peer_sans, server_ssl_context
from .service import AnalysisService
from .store import TaskRecord

LOGGER = logging.getLogger("masi_analysis")


class AnalysisHTTPServer:
    def __init__(self, material: RuntimeMaterial, config_path: str) -> None:
        self._material = material
        self._config_path = config_path
        self.service = AnalysisService(material)
        self._stop = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._app = web.Application(client_max_size=material.config.limits.request_bytes)
        self._app.add_routes(
            [
                web.get("/.well-known/agent-card.json", self._agent_card),
                web.post("/message:send", self._submit),
                web.get(r"/tasks/{task_id:[A-Za-z0-9._:-]{1,128}}", self._poll),
                web.get(r"/traces/{task_id:[A-Za-z0-9._:-]{1,128}}", self._traces),
                web.get("/health/startup", self._health_startup),
                web.get("/health/ready", self._health_ready),
                web.get("/health/live", self._health_live),
                web.get("/metrics", self._metrics),
            ]
        )
        self._app.middlewares.append(self._identity_middleware)

    @web.middleware
    async def _identity_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path.startswith("/health/") or request.path == "/metrics":
            peer = request.remote or ""
            if peer not in {"127.0.0.1", "::1"}:
                return self._error(AnalysisError("IDENTITY_MISMATCH", "health boundary is loopback-only", 403), "health")
            return await handler(request)
        config = self._material.config
        if config.tls.enabled:
            ssl_object = request.transport.get_extra_info("ssl_object") if request.transport else None
            matched = peer_sans(ssl_object) & set(config.tls.allowed_client_sans)
            if len(matched) != 1:
                return self._error(AnalysisError("IDENTITY_MISMATCH", "A2A client SAN rejected", 401), "identity")
            request["masi.requester_identity"] = next(iter(matched))
        elif config.runtime_profile != "acceptance" or request.remote not in {"127.0.0.1", "::1"}:
            return self._error(AnalysisError("IDENTITY_MISMATCH", "plaintext A2A is acceptance-loopback only", 401), "identity")
        else:
            request["masi.requester_identity"] = "acceptance-loopback"
        return await handler(request)

    @staticmethod
    def _requester_identity(request: web.Request) -> str:
        identity = request.get("masi.requester_identity")
        if not isinstance(identity, str) or not identity:
            raise AnalysisError("IDENTITY_MISMATCH", "authenticated A2A identity unavailable", 401)
        return identity

    def _validate_version(self, request: web.Request) -> None:
        header = request.headers.get("A2A-Version")
        query = request.query.get("A2A-Version")
        if header and query and header != query:
            raise AnalysisError("VERSION_NOT_SUPPORTED", "A2A header/query versions disagree", 400)
        selected = header or query
        if selected != A2A_VERSION:
            raise AnalysisError("VERSION_NOT_SUPPORTED", "A2A 1.0 is required", 400)

    async def _bounded_body(self, request: web.Request) -> bytes:
        limit = self._material.config.limits.request_bytes
        content_length = request.content_length
        if content_length is not None and (content_length < 1 or content_length > limit):
            raise AnalysisError("BODY_TOO_LARGE", "A2A body length rejected", 413)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.content.iter_chunked(8192):
            size += len(chunk)
            if size > limit:
                raise AnalysisError("BODY_TOO_LARGE", "A2A body exceeded bound before parsing", 413)
            chunks.append(chunk)
        if size == 0:
            raise AnalysisError("MALFORMED_REQUEST", "A2A body required", 400)
        return b"".join(chunks)

    async def _submit(self, request: web.Request) -> web.Response:
        trace_id = "a2a-submit"
        try:
            self._validate_version(request)
            if request.content_type.lower() != "application/a2a+json":
                raise AnalysisError("CONTENT_TYPE_UNSUPPORTED", "Content-Type must be application/a2a+json", 415)
            raw = await self._bounded_body(request)
            record = await self.service.submit(raw, requester_identity=self._requester_identity(request))
            trace_id = record.input_bundle.trace_id
            return self._json(self._task_response(record))
        except AnalysisError as exc:
            return self._error(exc, trace_id)

    async def _poll(self, request: web.Request) -> web.Response:
        trace_id = "a2a-poll"
        try:
            self._validate_version(request)
            record = self.service.get(request.match_info["task_id"], self._requester_identity(request))
            trace_id = record.input_bundle.trace_id
            return self._json(self._task_response(record))
        except AnalysisError as exc:
            return self._error(exc, trace_id)

    async def _traces(self, request: web.Request) -> web.Response:
        trace_id = "a2a-trace"
        try:
            self._validate_version(request)
            requester_identity = self._requester_identity(request)
            record = self.service.get(request.match_info["task_id"], requester_identity)
            trace_id = record.input_bundle.trace_id
            traces = self.service.store.traces_for_requester(record.task_id, requester_identity)
            return self._json(
                {
                    "schema_version": "masi-analysis-trace/v1",
                    "task_id": record.task_id,
                    "trace_id": trace_id,
                    "events": [
                        {
                            "sequence": item.sequence,
                            "code": item.code,
                            "occurred_at_unix_ms": item.occurred_at_unix_ms,
                            "detail_digest": item.detail_digest,
                        }
                        for item in traces
                    ],
                },
                content_type="application/json",
            )
        except AnalysisError as exc:
            return self._error(exc, trace_id)

    async def _agent_card(self, request: web.Request) -> web.Response:
        try:
            self._validate_version(request)
        except AnalysisError as exc:
            return self._error(exc, "agent-card")
        descriptions = {
            "analyze_nids_incident": "Grounded incident evidence analysis.",
            "compare_event_windows": "Bounded comparison of frozen event-window references.",
            "draft_mitigation_advice": "Non-executable mitigation advice for human review.",
            "generate_incident_content": "Bounded report, notification, ticket, checklist, or retrospective draft.",
        }
        card = {
            "name": "MASI-NIDS Analysis Agent",
            "description": "Grounded and non-executable security evidence analysis.",
            "url": self._material.config.public_agent_card_url.rsplit("/.well-known", 1)[0],
            "protocolVersion": A2A_VERSION,
            "preferredTransport": "HTTP+JSON",
            "version": VERSION,
            "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": skill,
                    "name": skill.replace("_", " ").title(),
                    "description": descriptions[skill],
                    "inputModes": ["application/json"],
                    "outputModes": ["application/json"],
                }
                for skill in SKILLS
            ],
            "security": {"scheme": "mutualTLS", "mtlsRequired": True, "dynamicDiscovery": False},
        }
        return self._json(card)

    async def _health_startup(self, _request: web.Request) -> web.Response:
        health = self.service.health()
        return web.json_response(health, status=200 if health["startup"] else 503)

    async def _health_ready(self, _request: web.Request) -> web.Response:
        health = self.service.health()
        return web.json_response(health, status=200 if health["ready"] else 503)

    async def _health_live(self, _request: web.Request) -> web.Response:
        health = self.service.health()
        return web.json_response(health, status=200 if health["live"] else 503)

    async def _metrics(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=self.service.metrics.render(queue_depth=self.service.queue.qsize(), in_flight=self.service.in_flight),
            content_type="text/plain",
        )

    @staticmethod
    def _task_response(record: TaskRecord) -> dict[str, object]:
        artifacts: list[dict[str, object]] = []
        if record.artifact is not None:
            artifacts.append(
                {
                    "artifactId": record.artifact.artifact_id,
                    "name": "MASI-NIDS bounded analysis",
                    "description": "Grounded, non-executable AnalysisArtifact.",
                    "parts": [{"data": record.artifact.model_dump(mode="json"), "mediaType": "application/json"}],
                    "metadata": {"artifactDigest": record.artifact.artifact_digest, "nonExecutable": True},
                }
            )
        timestamp = datetime.fromtimestamp(record.updated_at_unix_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
        return {
            "task": {
                "id": record.task_id,
                "contextId": record.context_id,
                "status": {"state": record.state, "timestamp": timestamp},
                "artifacts": artifacts,
                "history": [],
                "metadata": {
                    "schemaVersion": "masi-analysis-task/v1",
                    "pluginId": record.plugin_id,
                    "bindingGeneration": record.binding_generation,
                    "inputDigest": record.input_digest,
                    "traceId": record.input_bundle.trace_id,
                    "reasonCode": record.reason_code,
                },
            }
        }

    def _json(self, payload: Mapping[str, object], *, content_type: str = "application/a2a+json", status: int = 200) -> web.Response:
        raw = go_json_bytes(payload)
        if len(raw) > self._material.config.limits.response_bytes:
            return self._error(AnalysisError("INTERNAL_UNAVAILABLE", "response bound exceeded", 500), "response")
        response = web.Response(body=raw, status=status, content_type=content_type)
        response.headers["A2A-Version"] = A2A_VERSION
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _error(self, error: AnalysisError, trace_id: str) -> web.Response:
        payload = {
            "schema_version": "masi-a2a-error/v1",
            "error_code": error.code,
            "message": error.message[:256],
            "retryable": error.retryable,
            "trace_id": trace_id if trace_id and len(trace_id) <= 128 else "a2a-error",
        }
        return self._json(payload, status=error.http_status)

    async def run(self) -> None:
        await self.service.start()
        runner = web.AppRunner(self._app, access_log=None, shutdown_timeout=self._material.config.limits.shutdown_grace_ms / 1000)
        await runner.setup()
        self._runner = runner
        ssl_context: ssl.SSLContext | None = None
        tls = self._material.config.tls
        if tls.enabled:
            assert tls.cert_file and tls.key_file and tls.client_ca_file
            ssl_context = server_ssl_context(tls.cert_file, tls.key_file, tls.client_ca_file)
        site = web.TCPSite(
            runner,
            self._material.config.listen.host,
            self._material.config.listen.port,
            ssl_context=ssl_context,
            shutdown_timeout=self._material.config.limits.shutdown_grace_ms / 1000,
        )
        self._site = site
        await site.start()
        health_task = asyncio.create_task(self._health_writer(), name="analysis-health-writer")
        self._health_task = health_task
        self._install_signal_handlers()
        LOGGER.info("analysis_started", extra={"message_code": "STARTED"})
        await self._stop.wait()
        await site.stop()
        await self.service.drain_and_close()
        health_task.cancel()
        await asyncio.gather(health_task, return_exceptions=True)
        await runner.cleanup()
        self._write_health(final=True)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_stop)
        loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.create_task(self._reload()))

    def _request_stop(self) -> None:
        self.service.draining = True
        self._stop.set()

    async def _reload(self) -> None:
        try:
            await self.service.reload_binding(self._config_path)
            LOGGER.info("analysis_binding_reloaded", extra={"message_code": "BINDING_RELOADED"})
        except AnalysisError as exc:
            self.service.draining = True
            LOGGER.error("analysis_binding_reload_failed", extra={"message_code": exc.code})

    async def _health_writer(self) -> None:
        while True:
            self._write_health(final=False)
            await asyncio.sleep(1)

    def _write_health(self, *, final: bool) -> None:
        health = self.service.health()
        health["pid"] = os.getpid()
        if final:
            health["startup"] = False
            health["ready"] = False
            health["live"] = False
            health["updated_at_unix_ms"] = time.time_ns() // 1_000_000
        atomic_write_private(Path(self._material.config.health_state_path), go_json_bytes(health))
