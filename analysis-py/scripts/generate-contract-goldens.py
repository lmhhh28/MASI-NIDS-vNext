#!/usr/bin/env python3
"""Generate the shared deterministic Analysis/A2A/MCP/provider golden records."""

from __future__ import annotations

import hashlib
from pathlib import Path

from masi_analysis.canonical import (
    canonical_digest,
    compute_artifact_digest,
    compute_input_digest,
    compute_provider_request_digest,
    compute_provider_response_digest,
    go_json_bytes,
)
from masi_analysis.models import (
    AnalysisArtifact,
    AnalysisBudgets,
    FrozenInput,
    GeneratedContent,
    GroundedClaim,
    ModelExplanationFact,
    ProviderFact,
    ProviderMetadata,
    ProviderRequest,
    ProviderResponse,
    ProviderToolRequest,
    Recommendation,
    RemainingBudget,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
ZERO = "sha256:" + "0" * 64
REVISION = "1" * 40


def write(path: Path, value: object) -> str:
    raw = go_json_bytes(value) + b"\n"
    path.write_bytes(raw)
    return "sha256:" + hashlib.sha256(raw[:-1]).hexdigest()


def main() -> None:
    repository = Path(__file__).resolve().parents[2]
    destination = repository / "contracts/analysis/v1/golden"
    destination.mkdir(parents=True, exist_ok=True)
    budgets = AnalysisBudgets(
        llm_calls=2,
        mcp_rounds=2,
        tool_calls=6,
        tool_parallelism=3,
        tool_timeout_ms=2000,
        tool_response_bytes=32768,
        tool_total_response_bytes=131072,
        llm_timeout_ms=6000,
        result_budget_ms=8000,
        graph_deadline_ms=30000,
        artifact_bytes=65536,
        outbound_delegations=2,
        delegation_depth=0,
        polls_per_task=3,
        a2a_response_bytes=131072,
    )
    input_bundle = FrozenInput.model_validate(
        {
            "schema_version": "masi-analysis-input/v1",
            "task_id": "task-golden-1",
            "run_id": "run-golden-1",
            "skill": "analyze_nids_incident",
            "plugin_id": "masi.analysis.langgraph",
            "plugin_revision": REVISION,
            "config_digest": DIGEST_A,
            "binding_generation": 7,
            "scope": "tenant-golden",
            "target_set_digest": DIGEST_B,
            "event_refs": [{"id": "event-golden-1", "digest": DIGEST_A}],
            "incident_refs": [{"id": "incident-golden-1", "digest": DIGEST_B}],
            "evidence_refs": [
                {"id": "evidence-golden-1", "digest": DIGEST_A},
                {"id": "explanation-golden-1", "digest": DIGEST_B},
            ],
            "runtime_refs": [{"id": "runtime-golden-1", "digest": DIGEST_C}],
            "model_result_evidence_refs": ["evidence-golden-1"],
            "model_explanation_evidence_refs": ["explanation-golden-1"],
            "quality": "valid",
            "provider_profile_digest": DIGEST_A,
            "prompt_profile_digest": DIGEST_B,
            "tool_policy_digest": DIGEST_C,
            "redaction_profile_digest": DIGEST_A,
            "provenance_digest": DIGEST_B,
            "budgets": budgets.model_dump(mode="json"),
            "input_digest": ZERO,
            "deadline_unix_ms": 2_000_000_000_000,
            "expires_at_unix_ms": 2_000_003_600_000,
            "locale": "zh-CN",
            "content_request": "生成有证据约束的分析摘要",
            "idempotency_key": "idem-golden-1",
            "delegation_path": [],
            "trace_id": "trace-golden-1",
        }
    )
    input_bundle = input_bundle.model_copy(update={"input_digest": compute_input_digest(input_bundle)})
    input_file_digest = write(destination / "input-v1.json", input_bundle.model_dump(mode="json"))

    artifact = AnalysisArtifact(
        schema_version="masi-analysis-artifact/v1",
        artifact_id="artifact-golden-1",
        artifact_digest=ZERO,
        task_id=input_bundle.task_id,
        run_id=input_bundle.run_id,
        plugin_id=input_bundle.plugin_id,
        plugin_revision=input_bundle.plugin_revision,
        config_digest=input_bundle.config_digest,
        binding_generation=input_bundle.binding_generation,
        input_digest=input_bundle.input_digest,
        analysis_outcome="succeeded",
        quality="valid",
        source_fact_refs=["event-golden-1", "evidence-golden-1", "explanation-golden-1", "incident-golden-1", "runtime-golden-1"],
        observed_claims=[GroundedClaim(claim="Authorized evidence is present.", evidence_refs=["evidence-golden-1"])],
        model_result_facts=[GroundedClaim(claim="The frozen model result is an input fact.", evidence_refs=["evidence-golden-1"])],
        model_explanation_facts=[
            ModelExplanationFact(
                claim="TreeSHAP records an association for this frozen sample.",
                evidence_refs=["explanation-golden-1"],
                method="tree-shap",
                background_digest=DIGEST_A,
                model_digest=DIGEST_B,
                scaler_digest=DIGEST_C,
                sample_digest=DIGEST_A,
                coverage=1.0,
                truncated=False,
                limitations=["Association is not causality."],
            )
        ],
        inferred_claims=["Further investigation may be useful."],
        llm_interpretations=["The model result is interpreted without changing the model decision."],
        uncertainties=["Independent outcome evidence remains bounded."],
        limitations=["The Artifact is non-executable."],
        missing_evidence=["Independent packet outcome evidence is not part of this Artifact."],
        recommendations=[
            Recommendation(
                text="Review current Go-owned facts before preparing a separate proposal.",
                risk="human-review-required",
                preconditions=["Re-read current canonical facts."],
                expires_at_unix_ms=input_bundle.expires_at_unix_ms,
                evidence_refs=["evidence-golden-1"],
            )
        ],
        generated_content=[GeneratedContent(media_type="text/markdown", body="Bounded analysis draft.")],
        provider_metadata=ProviderMetadata(provider_id="fixture-provider", model_id="fixture-model", provider_digest=DIGEST_A),
        tool_trajectory_digest=DIGEST_B,
        graph_version="masi-analysis-graph/v1",
        topology_digest=DIGEST_C,
        trace_id=input_bundle.trace_id,
        trace_digest=DIGEST_A,
        produced_at_unix_ms=1_999_999_999_000,
        expires_at_unix_ms=input_bundle.expires_at_unix_ms,
        media_type="application/json",
        non_executable=True,
        deployment_eligible=False,
    )
    artifact = artifact.model_copy(update={"artifact_digest": compute_artifact_digest(artifact)})
    artifact_file_digest = write(destination / "artifact-v1.json", artifact.model_dump(mode="json"))

    message_id = "msg-" + hashlib.sha256(f"{input_bundle.task_id}:{input_bundle.input_digest}".encode()).hexdigest()[:32]
    send_request = {
        "message": {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": [{"data": input_bundle.model_dump(mode="json"), "mediaType": "application/json"}],
            "metadata": {
                "bindingGeneration": input_bundle.binding_generation,
                "deadlineUnixMs": input_bundle.deadline_unix_ms,
                "inputDigest": input_bundle.input_digest,
                "pluginId": input_bundle.plugin_id,
                "schemaVersion": input_bundle.schema_version,
            },
        },
        "configuration": {"acceptedOutputModes": ["application/json"]},
        "metadata": {
            "delegationDepth": 0,
            "delegationPath": [],
            "noPush": True,
            "noStreaming": True,
            "traceId": input_bundle.trace_id,
        },
    }
    send_request_digest = write(destination / "a2a-send-request-v1.json", send_request)
    completed = {
        "task": {
            "id": input_bundle.task_id,
            "contextId": "context-golden-1",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2033-05-18T03:33:20Z"},
            "artifacts": [
                {
                    "artifactId": artifact.artifact_id,
                    "name": "MASI-NIDS bounded analysis",
                    "description": "Grounded, non-executable AnalysisArtifact.",
                    "parts": [{"data": artifact.model_dump(mode="json"), "mediaType": "application/json"}],
                    "metadata": {"artifactDigest": artifact.artifact_digest, "nonExecutable": True},
                }
            ],
            "history": [],
            "metadata": {
                "schemaVersion": "masi-analysis-task/v1",
                "pluginId": input_bundle.plugin_id,
                "bindingGeneration": input_bundle.binding_generation,
                "inputDigest": input_bundle.input_digest,
                "traceId": input_bundle.trace_id,
                "reasonCode": "ANALYSIS_SUCCEEDED",
            },
        }
    }
    completed_digest = write(destination / "a2a-task-completed-v1.json", completed)
    peer_data = {
        "schema_version": "masi-analysis-peer-artifact/v1",
        "summary": "Untrusted bounded peer context.",
        "source_refs": [{"id": "evidence-golden-1", "digest": DIGEST_A}],
        "content_digest": ZERO,
    }
    peer_data["content_digest"] = canonical_digest({key: value for key, value in peer_data.items() if key != "content_digest"})
    peer_completed = {
        "task": {
            "id": "peer-task-golden-1",
            "contextId": "peer-context-golden-1",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2033-05-18T03:33:21Z"},
            "artifacts": [
                {
                    "artifactId": "peer-artifact-golden-1",
                    "name": "Bounded peer context",
                    "description": "Untrusted read-only peer Artifact.",
                    "parts": [{"data": peer_data, "mediaType": "application/json"}],
                    "metadata": {"contentDigest": peer_data["content_digest"], "nonExecutable": True},
                }
            ],
            "history": [],
            "metadata": {
                "schemaVersion": "masi-analysis-peer-task/v1",
                "pluginId": "fixture.peer.agent",
                "bindingGeneration": 3,
                "inputDigest": DIGEST_A,
                "traceId": "trace-peer-golden-1",
                "reasonCode": "PEER_SUCCEEDED",
            },
        }
    }
    peer_completed_digest = write(destination / "a2a-peer-task-completed-v1.json", peer_completed)
    agent_card = {
        "name": "MASI-NIDS Analysis Agent",
        "description": "Grounded and non-executable security evidence analysis.",
        "url": "https://analysis.masi.internal",
        "protocolVersion": "1.0",
        "preferredTransport": "HTTP+JSON",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": skill,
                "name": skill.replace("_", " ").title(),
                "description": "Bounded qualified Analysis skill.",
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }
            for skill in (
                "analyze_nids_incident",
                "compare_event_windows",
                "draft_mitigation_advice",
                "generate_incident_content",
            )
        ],
        "security": {"scheme": "mutualTLS", "mtlsRequired": True, "dynamicDiscovery": False},
    }
    agent_card_digest = write(destination / "a2a-agent-card-v1.json", agent_card)
    error_digest = write(
        destination / "a2a-error-v1.json",
        {
            "schema_version": "masi-a2a-error/v1",
            "error_code": "VERSION_NOT_SUPPORTED",
            "message": "A2A 1.0 is required",
            "retryable": False,
            "trace_id": "trace-golden-error",
        },
    )

    provider_request = ProviderRequest(
        schema_version="masi-analysis-provider-request/v1",
        request_id="provider-golden-hypothesis",
        phase="hypothesis",
        task_id=input_bundle.task_id,
        run_id=input_bundle.run_id,
        skill=input_bundle.skill,
        input_digest=input_bundle.input_digest,
        locale=input_bundle.locale,
        system_policy_id="analysis-system-policy/v1",
        facts=[ProviderFact(id="evidence-golden-1", digest=DIGEST_A, kind="evidence")],
        tool_results=[],
        peer_results=[],
        untrusted_content_request=input_bundle.content_request,
        remaining_budget=RemainingBudget(llm_calls=2, mcp_rounds=2, tool_calls=6, outbound_delegations=2, deadline_unix_ms=input_bundle.deadline_unix_ms),
        trace_id=input_bundle.trace_id,
        request_digest=ZERO,
    )
    provider_request = provider_request.model_copy(update={"request_digest": compute_provider_request_digest(provider_request)})
    provider_request_digest = write(destination / "provider-request-v1.json", provider_request.model_dump(mode="json"))
    provider_response = ProviderResponse(
        schema_version="masi-analysis-provider-response/v1",
        request_id=provider_request.request_id,
        input_digest=input_bundle.input_digest,
        outcome_hint="limited",
        observed_claims=[GroundedClaim(claim="Authorized evidence is present.", evidence_refs=["evidence-golden-1"])],
        model_result_facts=[],
        model_explanation_facts=[],
        inferred_claims=["Further investigation may be useful."],
        uncertainties=["Coverage is bounded."],
        limitations=["Provider output remains untrusted until grounded."],
        missing_evidence=["Qualified explanation metadata is requested."],
        recommendations=[],
        generated_content=[],
        tool_requests=[ProviderToolRequest(kind="tool", name="masi.evidence.get", arguments={"evidence_id": "explanation-golden-1"})],
        delegations=[],
        response_digest=ZERO,
    )
    provider_response = provider_response.model_copy(update={"response_digest": compute_provider_response_digest(provider_response)})
    provider_response_digest = write(destination / "provider-response-v1.json", provider_response.model_dump(mode="json"))

    catalog = {
        "schema_version": "analysis-golden-catalog/v1",
        "input": {"path": "input-v1.json", "file_digest": input_file_digest, "content_digest": input_bundle.input_digest},
        "artifact": {"path": "artifact-v1.json", "file_digest": artifact_file_digest, "content_digest": artifact.artifact_digest},
        "a2a_send": {"path": "a2a-send-request-v1.json", "content_digest": send_request_digest},
        "a2a_completed": {"path": "a2a-task-completed-v1.json", "content_digest": completed_digest},
        "a2a_peer_completed": {"path": "a2a-peer-task-completed-v1.json", "content_digest": peer_completed_digest},
        "a2a_agent_card": {"path": "a2a-agent-card-v1.json", "content_digest": agent_card_digest},
        "a2a_error": {"path": "a2a-error-v1.json", "content_digest": error_digest},
        "provider_request": {"path": "provider-request-v1.json", "content_digest": provider_request_digest},
        "provider_response": {"path": "provider-response-v1.json", "content_digest": provider_response_digest},
    }
    write(destination / "catalog.json", catalog)


if __name__ == "__main__":
    main()
