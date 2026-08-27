"""Fixed LangGraph analysis plan with bounded external calls and deterministic guards."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, NotRequired, Required, TypedDict

from langgraph.graph import END, START, StateGraph

from .canonical import (
    canonical_digest,
    compute_artifact_digest,
    compute_provider_request_digest,
    go_json_bytes,
)
from .config import BindingState
from .constants import ARTIFACT_SCHEMA, GRAPH_VERSION, PLUGIN_ID
from .errors import AnalysisError, UnsafeOutput
from .mcp import MCPClient, ToolResult
from .models import (
    AnalysisArtifact,
    FrozenInput,
    GeneratedContent,
    GroundedClaim,
    ModelExplanationFact,
    ProviderFact,
    ProviderMetadata,
    ProviderRequest,
    ProviderResponse,
    Recommendation,
    RemainingBudget,
)
from .peer import PeerClient, PeerResult
from .provider import ProviderAdapter
from .security import require_safe_output

_CAUSAL = re.compile(r"(?i)\b(?:cause|caused|root cause|because of)\b|导致|根因|引发")
_ZERO_DIGEST = "sha256:" + "0" * 64
_TOPOLOGY = {
    "version": GRAPH_VERSION,
    "nodes": ["context", "hypothesis", "bounded_evidence", "synthesis", "grounding", "artifact"],
    "edges": [
        ["START", "context"],
        ["context", "hypothesis"],
        ["hypothesis", "bounded_evidence|grounding"],
        ["bounded_evidence", "synthesis"],
        ["synthesis", "grounding"],
        ["grounding", "artifact"],
        ["artifact", "END"],
    ],
}
TOPOLOGY_DIGEST = canonical_digest(_TOPOLOGY)


class GraphState(TypedDict):
    input: Required[FrozenInput]
    started_monotonic: Required[float]
    context: NotRequired[dict[str, Any]]
    provider_response: Required[ProviderResponse | None]
    tool_results: Required[list[ToolResult]]
    peer_results: Required[list[PeerResult]]
    external_errors: Required[list[str]]
    trace_codes: Required[list[str]]
    grounded: NotRequired[dict[str, Any]]
    artifact: NotRequired[AnalysisArtifact]


class AnalysisGraph:
    def __init__(
        self,
        binding_state: BindingState,
        provider: ProviderAdapter,
        mcp: MCPClient,
        peers: PeerClient,
    ) -> None:
        self._binding_state = binding_state
        self._provider = provider
        self._mcp = mcp
        self._peers = peers
        builder = StateGraph(GraphState)
        builder.add_node("context", self._context)
        builder.add_node("hypothesis", self._hypothesis)
        builder.add_node("bounded_evidence", self._bounded_evidence)
        builder.add_node("synthesis", self._synthesis)
        builder.add_node("grounding", self._grounding)
        builder.add_node("artifact", self._artifact)
        builder.add_edge(START, "context")
        builder.add_edge("context", "hypothesis")
        builder.add_conditional_edges("hypothesis", self._evidence_route, {"gather": "bounded_evidence", "ground": "grounding"})
        builder.add_edge("bounded_evidence", "synthesis")
        builder.add_edge("synthesis", "grounding")
        builder.add_edge("grounding", "artifact")
        builder.add_edge("artifact", END)
        self._compiled = builder.compile()

    async def run(self, input_bundle: FrozenInput) -> tuple[AnalysisArtifact, list[str]]:
        timeout_seconds = min(
            input_bundle.budgets.graph_deadline_ms / 1000,
            max(0.001, (input_bundle.deadline_unix_ms - time.time_ns() // 1_000_000) / 1000),
        )
        try:
            result = await asyncio.wait_for(
                self._compiled.ainvoke(
                    {
                        "input": input_bundle,
                        "started_monotonic": time.monotonic(),
                        "provider_response": None,
                        "tool_results": [],
                        "peer_results": [],
                        "external_errors": [],
                        "trace_codes": ["GRAPH_STARTED"],
                    }
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise AnalysisError("DEADLINE_EXCEEDED", "analysis graph exceeded its hard deadline", 408, True) from exc
        artifact = result.get("artifact")
        if not isinstance(artifact, AnalysisArtifact):
            raise AnalysisError("INTERNAL_UNAVAILABLE", "analysis graph produced no artifact", 500)
        return artifact, list(result.get("trace_codes", []))

    async def _context(self, state: GraphState) -> dict[str, Any]:
        input_bundle = state["input"]
        context = {
            "rate_and_impact": {
                "event_count": len(input_bundle.event_refs),
                "incident_count": len(input_bundle.incident_refs),
                "target_set_digest": input_bundle.target_set_digest,
            },
            "quality_and_provenance": {
                "quality": input_bundle.quality,
                "provenance_digest": input_bundle.provenance_digest,
                "redaction_profile_digest": input_bundle.redaction_profile_digest,
            },
            "runtime_and_p4_read_context": {
                "runtime_ref_count": len(input_bundle.runtime_refs),
                "runtime_digest": canonical_digest([item.model_dump(mode="json") for item in input_bundle.runtime_refs]),
            },
            "incident_and_replay_context": {
                "evidence_ref_count": len(input_bundle.evidence_refs),
                "evidence_digest": canonical_digest([item.model_dump(mode="json") for item in input_bundle.evidence_refs]),
            },
        }
        return {"context": context, "trace_codes": [*state["trace_codes"], "DETERMINISTIC_CONTEXT_BUILT"]}

    def _provider_request(self, state: GraphState, phase: str) -> ProviderRequest:
        input_bundle = state["input"]
        tool_results = state.get("tool_results", [])
        peer_results = state.get("peer_results", [])
        facts = [ProviderFact(id=item.id, digest=item.digest, kind="event") for item in input_bundle.event_refs]
        facts.extend(ProviderFact(id=item.id, digest=item.digest, kind="incident") for item in input_bundle.incident_refs)
        facts.extend(ProviderFact(id=item.id, digest=item.digest, kind="evidence") for item in input_bundle.evidence_refs)
        facts.extend(ProviderFact(id=item.id, digest=item.digest, kind="runtime") for item in input_bundle.runtime_refs)
        tool_facts = [
            ProviderFact(
                id=f"mcp-{result.request_id}",
                digest=result.content_digest,
                kind="tool-result",
                summary=require_safe_output(str(result.structured)[:2048], self._provider.secret_values),
            )
            for result in tool_results
        ]
        peer_facts = [
            ProviderFact(id=f"peer-{result.peer_id}", digest=result.content_digest, kind="peer-result", summary=result.summary) for result in peer_results
        ]
        used_llm = 0 if phase == "hypothesis" else 1
        request = ProviderRequest(
            schema_version="masi-analysis-provider-request/v1",
            request_id=f"provider-{input_bundle.run_id}-{phase}",
            phase=phase,  # type: ignore[arg-type]
            task_id=input_bundle.task_id,
            run_id=input_bundle.run_id,
            skill=input_bundle.skill,
            input_digest=input_bundle.input_digest,
            locale=input_bundle.locale,
            system_policy_id="analysis-system-policy/v1",
            facts=facts,
            tool_results=tool_facts,
            peer_results=peer_facts,
            untrusted_content_request=input_bundle.content_request,
            remaining_budget=RemainingBudget(
                llm_calls=max(0, input_bundle.budgets.llm_calls - used_llm),
                mcp_rounds=max(0, input_bundle.budgets.mcp_rounds - (1 if tool_results else 0)),
                tool_calls=max(0, input_bundle.budgets.tool_calls - len(tool_results)),
                outbound_delegations=max(0, input_bundle.budgets.outbound_delegations - len(peer_results)),
                deadline_unix_ms=input_bundle.deadline_unix_ms,
            ),
            trace_id=input_bundle.trace_id,
            request_digest=_ZERO_DIGEST,
        )
        return request.model_copy(update={"request_digest": compute_provider_request_digest(request)})

    async def _hypothesis(self, state: GraphState) -> dict[str, Any]:
        input_bundle = state["input"]
        if input_bundle.budgets.llm_calls == 0:
            return {
                "external_errors": [*state["external_errors"], "PROVIDER_DISABLED_BY_BUDGET"],
                "trace_codes": [*state["trace_codes"], "PROVIDER_SKIPPED"],
            }
        try:
            remaining_ms = self._remaining_result_ms(state)
            if remaining_ms <= 0:
                raise AnalysisError("RESULT_BUDGET_EXHAUSTED", "result budget exhausted before provider call", 408)
            response = await self._provider.analyze(
                self._provider_request(state, "hypothesis"),
                timeout_ms=min(input_bundle.budgets.llm_timeout_ms, remaining_ms),
                response_bytes=input_bundle.budgets.artifact_bytes,
            )
            return {"provider_response": response, "trace_codes": [*state["trace_codes"], "HYPOTHESIS_COMPLETED"]}
        except AnalysisError as exc:
            return {
                "provider_response": None,
                "external_errors": [*state["external_errors"], exc.code],
                "trace_codes": [*state["trace_codes"], "HYPOTHESIS_LIMITED"],
            }

    @staticmethod
    def _evidence_route(state: GraphState) -> str:
        response = state.get("provider_response")
        if response is not None and (response.tool_requests or response.delegations):
            return "gather"
        return "ground"

    async def _bounded_evidence(self, state: GraphState) -> dict[str, Any]:
        input_bundle = state["input"]
        binding = self._binding_state.matches_input(input_bundle, time.time_ns() // 1_000_000)
        response = state.get("provider_response")
        if response is None:
            return {}
        remaining_seconds = self._remaining_result_ms(state) / 1000
        if remaining_seconds <= 0:
            return {
                "external_errors": [*state["external_errors"], "RESULT_BUDGET_EXHAUSTED"],
                "trace_codes": [*state["trace_codes"], "BOUNDED_EVIDENCE_SKIPPED"],
            }
        errors = list(state["external_errors"])
        tool_results: list[ToolResult] = []
        peer_results: list[PeerResult] = []

        async def tools() -> list[ToolResult]:
            return await self._mcp.execute(response.tool_requests, input_bundle, binding)

        async def peers() -> list[PeerResult]:
            return await self._peers.delegate(
                response.delegations,
                input_bundle,
                allowed_peer_ids=set(binding.a2a_peer_allowlist),
                secret_values=self._provider.secret_values,
            )

        try:
            gathered = await asyncio.wait_for(
                asyncio.gather(tools(), peers(), return_exceptions=True),
                timeout=remaining_seconds,
            )
        except TimeoutError:
            return {
                "tool_results": [],
                "peer_results": [],
                "external_errors": [*errors, "RESULT_BUDGET_EXHAUSTED"],
                "trace_codes": [*state["trace_codes"], "BOUNDED_EVIDENCE_TIMED_OUT"],
            }
        if isinstance(gathered[0], BaseException):
            error = gathered[0]
            errors.append(error.code if isinstance(error, AnalysisError) else "MCP_UNAVAILABLE")
        else:
            tool_results = gathered[0]
        if isinstance(gathered[1], BaseException):
            error = gathered[1]
            errors.append(error.code if isinstance(error, AnalysisError) else "PEER_UNAVAILABLE")
        else:
            peer_results = gathered[1]
        return {
            "tool_results": tool_results,
            "peer_results": peer_results,
            "external_errors": errors,
            "trace_codes": [*state["trace_codes"], "BOUNDED_EVIDENCE_COMPLETED"],
        }

    async def _synthesis(self, state: GraphState) -> dict[str, Any]:
        input_bundle = state["input"]
        if input_bundle.budgets.llm_calls < 2 or (not state.get("tool_results") and not state.get("peer_results")):
            return {"trace_codes": [*state["trace_codes"], "SYNTHESIS_NOT_REQUIRED"]}
        try:
            remaining_ms = self._remaining_result_ms(state)
            if remaining_ms <= 0:
                raise AnalysisError("RESULT_BUDGET_EXHAUSTED", "result budget exhausted before synthesis", 408)
            response = await self._provider.analyze(
                self._provider_request(state, "synthesis"),
                timeout_ms=min(input_bundle.budgets.llm_timeout_ms, remaining_ms),
                response_bytes=input_bundle.budgets.artifact_bytes,
            )
            if response.tool_requests or response.delegations:
                return {
                    "provider_response": response.model_copy(update={"tool_requests": [], "delegations": []}),
                    "external_errors": [*state["external_errors"], "SECOND_ROUND_REQUEST_NOT_SYNTHESIZED"],
                    "trace_codes": [*state["trace_codes"], "SYNTHESIS_BOUNDED"],
                }
            return {"provider_response": response, "trace_codes": [*state["trace_codes"], "SYNTHESIS_COMPLETED"]}
        except AnalysisError as exc:
            return {
                "external_errors": [*state["external_errors"], exc.code],
                "trace_codes": [*state["trace_codes"], "SYNTHESIS_LIMITED"],
            }

    @staticmethod
    def _remaining_result_ms(state: GraphState) -> int:
        elapsed_ms = int((time.monotonic() - state["started_monotonic"]) * 1000)
        return max(0, state["input"].budgets.result_budget_ms - elapsed_ms)

    async def _grounding(self, state: GraphState) -> dict[str, Any]:
        input_bundle = state["input"]
        response = state.get("provider_response")
        evidence_ids = {item.id for item in input_bundle.evidence_refs}
        model_result_ids = set(input_bundle.model_result_evidence_refs)
        explanation_ids = set(input_bundle.model_explanation_evidence_refs)
        rejected = 0

        def claims(values: list[GroundedClaim], allowed: set[str]) -> list[GroundedClaim]:
            nonlocal rejected
            accepted: list[GroundedClaim] = []
            for value in values:
                try:
                    clean = require_safe_output(value.claim, self._provider.secret_values)
                except UnsafeOutput:
                    rejected += 1
                    continue
                if not set(value.evidence_refs).issubset(allowed):
                    rejected += 1
                    continue
                accepted.append(GroundedClaim(claim=clean, evidence_refs=sorted(set(value.evidence_refs))))
            return accepted

        observed = claims(response.observed_claims if response else [], evidence_ids)
        model_results = claims(response.model_result_facts if response else [], model_result_ids)
        explanation_claims = claims(response.model_explanation_facts if response else [], explanation_ids)
        explanations: list[ModelExplanationFact] = []
        for claim in explanation_claims:
            if _CAUSAL.search(claim.claim):
                rejected += 1
                continue
            metadata = self._explanation_metadata(state.get("tool_results", []), claim.evidence_refs)
            if metadata is None:
                rejected += 1
                continue
            explanations.append(ModelExplanationFact(claim=claim.claim, evidence_refs=claim.evidence_refs, **metadata))

        if not observed and input_bundle.evidence_refs:
            first = input_bundle.evidence_refs[0]
            observed.append(
                GroundedClaim(
                    claim="The frozen analysis input contains an authorized evidence reference.",
                    evidence_refs=[first.id],
                )
            )
        clean_lists: dict[str, list[str]] = {}
        source_lists = {
            "inferred_claims": response.inferred_claims if response else [],
            "llm_interpretations": response.inferred_claims if response else [],
            "uncertainties": response.uncertainties if response else [],
            "limitations": response.limitations if response else [],
            "missing_evidence": response.missing_evidence if response else [],
        }
        for name, values in source_lists.items():
            accepted_text: list[str] = []
            for value in values:
                try:
                    accepted_text.append(require_safe_output(value, self._provider.secret_values))
                except UnsafeOutput:
                    rejected += 1
            clean_lists[name] = accepted_text
        if input_bundle.model_result_evidence_refs and not model_results:
            clean_lists["limitations"].append("Model result references were present, but no grounded model-result statement was accepted.")
        if input_bundle.model_explanation_evidence_refs and not explanations:
            clean_lists["limitations"].append("No qualified model-native explanation metadata was available; attribution was not guessed.")
        if rejected:
            clean_lists["limitations"].append("One or more ungrounded, unsafe, causal, or incomplete provider statements were rejected.")
        if state["external_errors"]:
            clean_lists["limitations"].append("One or more bounded external analysis dependencies were unavailable or rejected.")
        grounded = {
            "observed_claims": observed,
            "model_result_facts": model_results,
            "model_explanation_facts": explanations,
            **clean_lists,
            "rejected": rejected,
        }
        return {"grounded": grounded, "trace_codes": [*state["trace_codes"], "GROUNDING_VALIDATED"]}

    @staticmethod
    def _explanation_metadata(results: list[ToolResult], refs: list[str]) -> dict[str, Any] | None:
        for result in results:
            value = result.structured.get("explanation")
            if not isinstance(value, dict) or value.get("evidence_id") not in refs:
                continue
            required = {"method", "model_digest", "sample_digest", "coverage", "truncated", "limitations"}
            if not required.issubset(value):
                continue
            try:
                candidate = ModelExplanationFact(
                    claim="validated",
                    evidence_refs=[str(value["evidence_id"])],
                    method=value["method"],
                    background_digest=value.get("background_digest"),
                    model_digest=value["model_digest"],
                    scaler_digest=value.get("scaler_digest"),
                    sample_digest=value["sample_digest"],
                    coverage=value["coverage"],
                    truncated=value["truncated"],
                    limitations=value["limitations"],
                )
            except Exception:
                continue
            payload = candidate.model_dump(mode="python")
            payload.pop("claim")
            payload.pop("evidence_refs")
            return payload
        return None

    async def _artifact(self, state: GraphState) -> dict[str, Any]:
        input_bundle = state["input"]
        grounded = state.get("grounded")
        if grounded is None:
            raise AnalysisError("INTERNAL_UNAVAILABLE", "grounding state missing", 500)
        response = state.get("provider_response")
        errors = state["external_errors"]
        if input_bundle.quality in {"low", "partial"} or errors or grounded["rejected"]:
            outcome = "limited"
            quality = "low" if input_bundle.quality == "low" else "limited"
        elif response is None:
            outcome = "insufficient_evidence"
            quality = "limited"
        else:
            outcome = response.outcome_hint
            quality = "valid" if outcome == "succeeded" else "limited"
        if not input_bundle.evidence_refs:
            outcome = "insufficient_evidence"
            quality = "limited"
        recommendation_refs = sorted({ref for claim in grounded["observed_claims"] for ref in claim.evidence_refs})[:32]
        recommendations: list[Recommendation] = []
        for text in response.recommendations if response else []:
            try:
                clean = require_safe_output(text, self._provider.secret_values)
            except UnsafeOutput:
                continue
            recommendations.append(
                Recommendation(
                    text=clean,
                    risk="human-review-required",
                    preconditions=["Re-read current Go-owned facts before any separate proposal."],
                    expires_at_unix_ms=input_bundle.expires_at_unix_ms,
                    evidence_refs=recommendation_refs,
                )
            )
        generated: list[GeneratedContent] = []
        for body in response.generated_content if response else []:
            try:
                clean = require_safe_output(body, self._provider.secret_values)
            except UnsafeOutput:
                continue
            generated.append(GeneratedContent(media_type="text/markdown", body=clean))
        if input_bundle.skill == "generate_incident_content" and not generated:
            generated.append(
                GeneratedContent(
                    media_type="text/markdown",
                    body="Analysis content is limited because no grounded provider content was accepted.",
                )
            )
        source_refs = sorted(
            {
                item.id
                for group in (input_bundle.event_refs, input_bundle.incident_refs, input_bundle.evidence_refs, input_bundle.runtime_refs)
                for item in group
            }
        )
        trace_codes = [*state["trace_codes"], "ARTIFACT_ASSEMBLED"]
        trajectory = {
            "tools": [result.trajectory_record() for result in state.get("tool_results", [])],
            "peers": [result.trajectory_record() for result in state.get("peer_results", [])],
        }
        produced_ms = time.time_ns() // 1_000_000
        artifact = AnalysisArtifact(
            schema_version=ARTIFACT_SCHEMA,
            artifact_id="artifact-" + canonical_digest({"task": input_bundle.task_id, "input": input_bundle.input_digest})[7:39],
            artifact_digest=_ZERO_DIGEST,
            task_id=input_bundle.task_id,
            run_id=input_bundle.run_id,
            plugin_id=PLUGIN_ID,
            plugin_revision=input_bundle.plugin_revision,
            config_digest=input_bundle.config_digest,
            binding_generation=input_bundle.binding_generation,
            input_digest=input_bundle.input_digest,
            analysis_outcome=outcome,
            quality=quality,
            source_fact_refs=source_refs,
            observed_claims=grounded["observed_claims"],
            model_result_facts=grounded["model_result_facts"],
            model_explanation_facts=grounded["model_explanation_facts"],
            inferred_claims=grounded["inferred_claims"],
            llm_interpretations=grounded["llm_interpretations"],
            uncertainties=grounded["uncertainties"],
            limitations=grounded["limitations"] or ["No additional limitation was reported."],
            missing_evidence=grounded["missing_evidence"] or ["No additional evidence gap was reported."],
            recommendations=recommendations,
            generated_content=generated,
            provider_metadata=ProviderMetadata(
                provider_id=self._provider.provider_id,
                model_id=self._provider.model_id,
                provider_digest=self._provider.profile_digest,
            ),
            tool_trajectory_digest=canonical_digest(trajectory),
            graph_version=GRAPH_VERSION,
            topology_digest=TOPOLOGY_DIGEST,
            trace_id=input_bundle.trace_id,
            trace_digest=canonical_digest(trace_codes),
            produced_at_unix_ms=produced_ms,
            expires_at_unix_ms=input_bundle.expires_at_unix_ms,
            media_type="application/json",
            non_executable=True,
            deployment_eligible=False,
        )
        artifact = artifact.model_copy(update={"artifact_digest": compute_artifact_digest(artifact)})
        raw = go_json_bytes(artifact.model_dump(mode="json"))
        if len(raw) > min(input_bundle.budgets.artifact_bytes, 65_536):
            raise AnalysisError("RESOURCE_EXHAUSTED", "analysis artifact exceeded the exact task bound", 422)
        return {"artifact": artifact, "trace_codes": trace_codes}
