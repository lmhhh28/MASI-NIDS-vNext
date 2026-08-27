"""Analysis application lifecycle and bounded task workers."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from pydantic import ValidationError

from .canonical import compute_input_digest, file_digest
from .config import BindingState, RuntimeMaterial
from .constants import TASK_FAILED, TASK_REJECTED, TASK_WORKING
from .errors import AnalysisError
from .graph import AnalysisGraph
from .mcp import MCPClient
from .metrics import Metrics
from .models import A2ASendRequest, FrozenInput
from .peer import PeerClient
from .provider import ProviderAdapter
from .store import TaskRecord, TaskStore

LOGGER = logging.getLogger("masi_analysis")


@dataclass(frozen=True, slots=True)
class WorkItem:
    input_bundle: FrozenInput
    request_digest: str


class AnalysisService:
    def __init__(self, material: RuntimeMaterial) -> None:
        self.material = material
        self.binding_state = BindingState(material)
        config = material.config
        production = config.runtime_profile == "production"
        self.store = TaskStore(
            material.store_path,
            max_tasks=config.limits.max_tasks,
            retention_seconds=config.limits.task_retention_seconds,
            max_trace_events=config.limits.max_trace_events_per_task,
        )
        self.metrics = Metrics()
        self.provider = ProviderAdapter(config.provider, production=production, max_connections=config.limits.concurrency)
        self.mcp = MCPClient(config.mcp, production=production, max_connections=config.limits.concurrency * 2)
        self.peers = PeerClient(config.a2a_peers, production=production)
        self.graph = AnalysisGraph(self.binding_state, self.provider, self.mcp, self.peers)
        self.queue: asyncio.Queue[WorkItem] = asyncio.Queue(maxsize=config.limits.queue_depth)
        self.workers: list[asyncio.Task[None]] = []
        self.draining = False
        self.admission_error = "DRAINING"
        self.stopping = False
        self.started = False
        self.in_flight = 0
        self.last_progress_monotonic = time.monotonic()

    async def start(self) -> None:
        self.binding_state.snapshot()
        self.workers = [asyncio.create_task(self._worker(index), name=f"analysis-worker-{index}") for index in range(self.material.config.limits.concurrency)]
        self.started = True
        self.last_progress_monotonic = time.monotonic()

    async def submit(self, raw: bytes, *, requester_identity: str) -> TaskRecord:
        if self.draining or self.stopping:
            message = "exact active binding unavailable" if self.admission_error == "BINDING_UNAVAILABLE" else "analysis plugin is draining"
            raise AnalysisError(self.admission_error, message, 503, True)
        try:
            request = A2ASendRequest.model_validate_json(raw)
        except ValidationError as exc:
            raise AnalysisError("MALFORMED_REQUEST", "A2A request failed strict schema", 400) from exc
        input_bundle = request.message.parts[0].data
        now_ms = time.time_ns() // 1_000_000
        if compute_input_digest(input_bundle) != input_bundle.input_digest:
            raise AnalysisError("DIGEST_MISMATCH", "frozen input digest mismatch", 400)
        if input_bundle.deadline_unix_ms <= now_ms or input_bundle.expires_at_unix_ms <= now_ms:
            raise AnalysisError("DEADLINE_EXCEEDED", "analysis task deadline or expiry elapsed", 408)
        if input_bundle.deadline_unix_ms - now_ms > input_bundle.budgets.graph_deadline_ms:
            raise AnalysisError("MALFORMED_REQUEST", "task deadline exceeds its declared graph budget", 400)
        if input_bundle.quality in {"stale", "invalid"}:
            raise AnalysisError("MALFORMED_REQUEST", "stale or invalid frozen input rejected before graph execution", 422)
        self.binding_state.matches_input(input_bundle, now_ms)
        request_digest = file_digest(raw)
        context_id = "context-" + request_digest[7:39]
        record, created = self.store.submit(input_bundle, request_digest, context_id, requester_identity)
        if not created:
            self.metrics.increment("submissions", "idempotent")
            return record
        self.store.append_trace(input_bundle.task_id, "A2A_ADMITTED", request_digest.encode())
        try:
            self.queue.put_nowait(WorkItem(input_bundle, request_digest))
        except asyncio.QueueFull:
            self.metrics.increment("submissions", "resource_exhausted")
            self.store.append_trace(input_bundle.task_id, "QUEUE_REJECTED")
            return self.store.set_state(input_bundle.task_id, TASK_REJECTED, "RESOURCE_EXHAUSTED")
        self.metrics.increment("submissions", "accepted")
        self.last_progress_monotonic = time.monotonic()
        return self.store.get(input_bundle.task_id)

    def get(self, task_id: str, requester_identity: str) -> TaskRecord:
        return self.store.get_for_requester(task_id, requester_identity)

    async def _worker(self, _index: int) -> None:
        while True:
            try:
                item = await self.queue.get()
            except asyncio.CancelledError:
                return
            self.in_flight += 1
            self.last_progress_monotonic = time.monotonic()
            started = time.monotonic()
            try:
                if self.stopping:
                    self.store.set_state(item.input_bundle.task_id, TASK_REJECTED, "DRAINING")
                    continue
                self.binding_state.matches_input(item.input_bundle, time.time_ns() // 1_000_000)
                self.store.set_state(item.input_bundle.task_id, TASK_WORKING, "GRAPH_RUNNING")
                self.store.append_trace(item.input_bundle.task_id, "GRAPH_DISPATCHED")
                artifact, trace_codes = await self.graph.run(item.input_bundle)
                self.binding_state.matches_input(item.input_bundle, time.time_ns() // 1_000_000)
                for code in trace_codes:
                    self.store.append_trace(item.input_bundle.task_id, code)
                record = self.store.complete(item.input_bundle.task_id, artifact)
                self.metrics.increment("tasks", record.artifact.analysis_outcome if record.artifact else "failed")
            except AnalysisError as exc:
                reason = "BINDING_FENCED" if exc.code in {"BINDING_FENCED", "BINDING_UNAVAILABLE"} else exc.code
                state = TASK_REJECTED if reason == "BINDING_FENCED" else TASK_FAILED
                try:
                    self.store.append_trace(item.input_bundle.task_id, reason)
                    self.store.set_state(item.input_bundle.task_id, state, reason)
                except AnalysisError:
                    pass
                self.metrics.increment("tasks", reason.lower())
                LOGGER.warning("analysis_task_limited", extra={"message_code": reason})
            except Exception:
                try:
                    self.store.append_trace(item.input_bundle.task_id, "INTERNAL_UNAVAILABLE")
                    self.store.set_state(item.input_bundle.task_id, TASK_FAILED, "INTERNAL_UNAVAILABLE")
                except AnalysisError:
                    pass
                self.metrics.increment("tasks", "internal_unavailable")
                LOGGER.exception("analysis_task_internal_failure", extra={"message_code": "INTERNAL_UNAVAILABLE"})
            finally:
                self.metrics.observe_ms("task", (time.monotonic() - started) * 1000)
                self.in_flight -= 1
                self.queue.task_done()
                self.last_progress_monotonic = time.monotonic()

    async def reload_binding(self, config_path: str) -> None:
        binding = self.binding_state.reload(config_path)
        self.material = self.binding_state.material()
        if binding.activation_state != "active" or binding.qualification_status != "qualified":
            self.draining = True
            self.admission_error = "BINDING_UNAVAILABLE"
        self.metrics.increment("binding_reload", binding.activation_state)
        self.last_progress_monotonic = time.monotonic()

    async def drain_and_close(self) -> None:
        self.draining = True
        self.admission_error = "DRAINING"
        self.stopping = True
        grace = self.material.config.limits.shutdown_grace_ms / 1000
        try:
            await asyncio.wait_for(self.queue.join(), timeout=grace)
        except TimeoutError:
            while not self.queue.empty():
                try:
                    item = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    self.store.set_state(item.input_bundle.task_id, TASK_REJECTED, "DRAIN_TIMEOUT")
                finally:
                    self.queue.task_done()
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        await asyncio.gather(self.provider.close(), self.mcp.close(), self.peers.close())
        self.store.close()
        self.started = False

    def health(self) -> dict[str, object]:
        now = time.monotonic()
        try:
            binding = self.binding_state.snapshot()
            binding_status = "active"
            generation = binding.binding_generation
        except AnalysisError:
            binding_status = "unavailable"
            generation = 0
        worker_progress = now - self.last_progress_monotonic <= 10
        ready = self.started and not self.draining and binding_status == "active" and worker_progress
        live = self.started and worker_progress
        domain_degraded = any(status == "unavailable" for status in (self.provider.last_status, self.mcp.last_status, self.peers.last_status))
        return {
            "schema_version": "masi-analysis-health/v1",
            "startup": self.started,
            "ready": ready,
            "live": live,
            "draining": self.draining,
            "binding_status": binding_status,
            "binding_generation": generation,
            "domain_status": "degraded" if domain_degraded else "available",
            "provider_status": self.provider.last_status,
            "mcp_status": self.mcp.last_status,
            "peer_status": self.peers.last_status,
            "queue_depth": self.queue.qsize(),
            "in_flight": self.in_flight,
            "updated_at_unix_ms": time.time_ns() // 1_000_000,
        }
