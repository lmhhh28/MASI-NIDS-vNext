"""Strict typed records for the Analysis contracts and external adapters."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .constants import (
    HARD_A2A_DELEGATIONS,
    HARD_A2A_DEPTH,
    HARD_A2A_POLLS,
    HARD_A2A_RESPONSE_BYTES,
    HARD_ARTIFACT_BYTES,
    HARD_GRAPH_DEADLINE_MS,
    HARD_LLM_CALLS,
    HARD_LLM_TIMEOUT_MS,
    HARD_MCP_ROUNDS,
    HARD_RESULT_BUDGET_MS,
    HARD_TOOL_CALLS,
    HARD_TOOL_PARALLELISM,
    HARD_TOOL_RESPONSE_BYTES,
    HARD_TOOL_TIMEOUT_MS,
    HARD_TOOL_TOTAL_RESPONSE_BYTES,
    PLUGIN_ID,
    SKILLS,
)


def _nonzero_digest(value: str) -> str:
    if value == "sha256:" + ("0" * 64):
        raise ValueError("all-zero digest sentinel is forbidden")
    return value


def _nonzero_revision(value: str) -> str:
    if value == "0" * 40:
        raise ValueError("all-zero revision sentinel is forbidden")
    return value


Identity = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"), AfterValidator(_nonzero_digest)]
Revision = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$"), AfterValidator(_nonzero_revision)]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class FactRef(StrictModel):
    id: Identity
    digest: Digest


class AnalysisBudgets(StrictModel):
    llm_calls: int = Field(ge=0, le=HARD_LLM_CALLS)
    mcp_rounds: int = Field(ge=0, le=HARD_MCP_ROUNDS)
    tool_calls: int = Field(ge=0, le=HARD_TOOL_CALLS)
    tool_parallelism: int = Field(ge=1, le=HARD_TOOL_PARALLELISM)
    tool_timeout_ms: int = Field(ge=1, le=HARD_TOOL_TIMEOUT_MS)
    tool_response_bytes: int = Field(ge=1, le=HARD_TOOL_RESPONSE_BYTES)
    tool_total_response_bytes: int = Field(ge=1, le=HARD_TOOL_TOTAL_RESPONSE_BYTES)
    llm_timeout_ms: int = Field(ge=1, le=HARD_LLM_TIMEOUT_MS)
    result_budget_ms: int = Field(ge=1, le=HARD_RESULT_BUDGET_MS)
    graph_deadline_ms: int = Field(ge=1, le=HARD_GRAPH_DEADLINE_MS)
    artifact_bytes: int = Field(ge=1024, le=HARD_ARTIFACT_BYTES)
    outbound_delegations: int = Field(ge=0, le=HARD_A2A_DELEGATIONS)
    delegation_depth: int = Field(ge=0, le=HARD_A2A_DEPTH)
    polls_per_task: int = Field(ge=0, le=HARD_A2A_POLLS)
    a2a_response_bytes: int = Field(ge=1024, le=HARD_A2A_RESPONSE_BYTES)


class FrozenInput(StrictModel):
    schema_version: Literal["masi-analysis-input/v1"]
    task_id: Identity
    run_id: Identity
    skill: Literal[
        "analyze_nids_incident",
        "compare_event_windows",
        "draft_mitigation_advice",
        "generate_incident_content",
    ]
    plugin_id: Literal["masi.analysis.langgraph"]
    plugin_revision: Revision
    config_digest: Digest
    binding_generation: int = Field(ge=1)
    scope: str = Field(min_length=1, max_length=256)
    target_set_digest: Digest
    event_refs: list[FactRef] = Field(max_length=128)
    incident_refs: list[FactRef] = Field(max_length=32)
    evidence_refs: list[FactRef] = Field(max_length=128)
    runtime_refs: list[FactRef] = Field(max_length=64)
    model_result_evidence_refs: list[Identity] = Field(max_length=128)
    model_explanation_evidence_refs: list[Identity] = Field(max_length=128)
    quality: Literal["valid", "partial", "low", "stale", "invalid"]
    provider_profile_digest: Digest
    prompt_profile_digest: Digest
    tool_policy_digest: Digest
    redaction_profile_digest: Digest
    provenance_digest: Digest
    budgets: AnalysisBudgets
    input_digest: Digest
    deadline_unix_ms: int = Field(ge=1)
    expires_at_unix_ms: int = Field(ge=1)
    locale: str = Field(min_length=1, max_length=32)
    content_request: str = Field(max_length=2048)
    idempotency_key: Identity
    delegation_path: list[Identity] = Field(max_length=2)
    trace_id: Identity

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        if not self.event_refs and not self.incident_refs and not self.evidence_refs:
            raise ValueError("at least one canonical event, incident, or evidence reference is required")
        for group in (self.event_refs, self.incident_refs, self.evidence_refs, self.runtime_refs):
            ids = [item.id for item in group]
            if len(ids) != len(set(ids)):
                raise ValueError("fact references must be unique inside each projection")
        evidence_ids = {item.id for item in self.evidence_refs}
        if not set(self.model_result_evidence_refs).issubset(evidence_ids):
            raise ValueError("model result references must be frozen evidence references")
        if not set(self.model_explanation_evidence_refs).issubset(evidence_ids):
            raise ValueError("model explanation references must be frozen evidence references")
        if len(self.delegation_path) != len(set(self.delegation_path)) or PLUGIN_ID in self.delegation_path:
            raise ValueError("delegation loop detected")
        if self.deadline_unix_ms > self.expires_at_unix_ms:
            raise ValueError("deadline must not exceed input expiry")
        return self


class GroundedClaim(StrictModel):
    claim: str = Field(min_length=1, max_length=4096)
    evidence_refs: list[Identity] = Field(min_length=1, max_length=32)


class ModelExplanationFact(StrictModel):
    claim: str = Field(min_length=1, max_length=4096)
    evidence_refs: list[Identity] = Field(min_length=1, max_length=32)
    method: Literal["logistic-contribution", "tree-shap", "reconstruction-residual", "other-qualified"]
    background_digest: Digest | None
    model_digest: Digest
    scaler_digest: Digest | None
    sample_digest: Digest
    coverage: float = Field(gt=0, le=1)
    truncated: bool
    limitations: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def qualified_method_inputs(self) -> Self:
        if self.method == "tree-shap" and self.background_digest is None:
            raise ValueError("TreeSHAP explanation requires a qualified background digest")
        if self.method in {"logistic-contribution", "reconstruction-residual"} and self.scaler_digest is None:
            raise ValueError("scaled explanation method requires the exact scaler digest")
        if len(self.limitations) != len(set(self.limitations)) or any(not value or len(value) > 1024 for value in self.limitations):
            raise ValueError("explanation limitations must be non-empty, bounded and unique")
        return self


class Recommendation(StrictModel):
    text: str = Field(min_length=1, max_length=4096)
    risk: str = Field(min_length=1, max_length=128)
    preconditions: list[str] = Field(max_length=32)
    expires_at_unix_ms: int = Field(ge=1)
    evidence_refs: list[Identity] = Field(max_length=32)


class GeneratedContent(StrictModel):
    media_type: Literal["application/json", "text/markdown"]
    body: str = Field(max_length=32768)


class ProviderMetadata(StrictModel):
    provider_id: Identity
    model_id: Identity
    provider_digest: Digest


class AnalysisArtifact(StrictModel):
    schema_version: Literal["masi-analysis-artifact/v1"]
    artifact_id: Identity
    artifact_digest: Digest
    task_id: Identity
    run_id: Identity
    plugin_id: Literal["masi.analysis.langgraph"]
    plugin_revision: Revision
    config_digest: Digest
    binding_generation: int = Field(ge=1)
    input_digest: Digest
    analysis_outcome: Literal["succeeded", "limited", "insufficient_evidence", "failed"]
    quality: Literal["valid", "limited", "low", "invalid"]
    source_fact_refs: list[Identity] = Field(max_length=352)
    observed_claims: list[GroundedClaim] = Field(max_length=128)
    model_result_facts: list[GroundedClaim] = Field(max_length=64)
    model_explanation_facts: list[ModelExplanationFact] = Field(max_length=64)
    inferred_claims: list[str] = Field(max_length=128)
    llm_interpretations: list[str] = Field(max_length=128)
    uncertainties: list[str] = Field(max_length=128)
    limitations: list[str] = Field(max_length=128)
    missing_evidence: list[str] = Field(max_length=128)
    recommendations: list[Recommendation] = Field(max_length=64)
    generated_content: list[GeneratedContent] = Field(max_length=16)
    provider_metadata: ProviderMetadata
    tool_trajectory_digest: Digest
    graph_version: Literal["masi-analysis-graph/v1"]
    topology_digest: Digest
    trace_id: Identity
    trace_digest: Digest
    produced_at_unix_ms: int = Field(ge=1)
    expires_at_unix_ms: int = Field(ge=1)
    media_type: Literal["application/json", "text/markdown"]
    non_executable: Literal[True]
    deployment_eligible: Literal[False]


class A2AInputPart(StrictModel):
    data: FrozenInput
    media_type: Literal["application/json"] = Field(alias="mediaType")


class A2AMessageMetadata(StrictModel):
    schema_version: Literal["masi-analysis-input/v1"] = Field(alias="schemaVersion")
    input_digest: Digest = Field(alias="inputDigest")
    plugin_id: Literal["masi.analysis.langgraph"] = Field(alias="pluginId")
    binding_generation: int = Field(ge=1, alias="bindingGeneration")
    deadline_unix_ms: int = Field(ge=1, alias="deadlineUnixMs")


class A2AMessage(StrictModel):
    message_id: Identity = Field(alias="messageId")
    role: Literal["ROLE_USER"]
    parts: list[A2AInputPart] = Field(min_length=1, max_length=1)
    metadata: A2AMessageMetadata


class A2AConfiguration(StrictModel):
    accepted_output_modes: list[Literal["application/json"]] = Field(
        min_length=1,
        max_length=1,
        alias="acceptedOutputModes",
    )


class A2ARequestMetadata(StrictModel):
    trace_id: Identity = Field(alias="traceId")
    no_push: Literal[True] = Field(alias="noPush")
    no_streaming: Literal[True] = Field(alias="noStreaming")
    delegation_depth: int = Field(ge=0, le=1, alias="delegationDepth")
    delegation_path: list[Identity] = Field(max_length=2, alias="delegationPath")


class A2ASendRequest(StrictModel):
    message: A2AMessage
    configuration: A2AConfiguration
    metadata: A2ARequestMetadata

    @model_validator(mode="after")
    def match_input(self) -> Self:
        input_bundle = self.message.parts[0].data
        metadata = self.message.metadata
        if (
            metadata.input_digest != input_bundle.input_digest
            or metadata.binding_generation != input_bundle.binding_generation
            or metadata.deadline_unix_ms != input_bundle.deadline_unix_ms
            or self.metadata.trace_id != input_bundle.trace_id
            or self.metadata.delegation_depth != input_bundle.budgets.delegation_depth
            or self.metadata.delegation_path != input_bundle.delegation_path
        ):
            raise ValueError("A2A message metadata does not match the frozen input")
        return self


class PeerArtifactData(StrictModel):
    schema_version: Literal["masi-analysis-peer-artifact/v1"]
    summary: str = Field(min_length=1, max_length=2048)
    source_refs: list[FactRef] = Field(max_length=32)
    content_digest: Digest


class PeerArtifactPart(StrictModel):
    data: PeerArtifactData
    media_type: Literal["application/json"] = Field(alias="mediaType")


class PeerArtifactMetadata(StrictModel):
    content_digest: Digest = Field(alias="contentDigest")
    non_executable: Literal[True] = Field(alias="nonExecutable")


class PeerTaskArtifact(StrictModel):
    artifact_id: Identity = Field(alias="artifactId")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(max_length=512)
    parts: list[PeerArtifactPart] = Field(min_length=1, max_length=1)
    metadata: PeerArtifactMetadata


class PeerTaskStatus(StrictModel):
    state: Literal[
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_AUTH_REQUIRED",
    ]
    timestamp: str = Field(min_length=20, max_length=40)


class PeerTaskMetadata(StrictModel):
    schema_version: Literal["masi-analysis-peer-task/v1"] = Field(alias="schemaVersion")
    plugin_id: Identity = Field(alias="pluginId")
    binding_generation: int = Field(ge=1, alias="bindingGeneration")
    input_digest: Digest = Field(alias="inputDigest")
    trace_id: Identity = Field(alias="traceId")
    reason_code: ReasonCode = Field(alias="reasonCode")


class PeerTask(StrictModel):
    task_id: Identity = Field(alias="id")
    context_id: Identity = Field(alias="contextId")
    status: PeerTaskStatus
    artifacts: list[PeerTaskArtifact] = Field(max_length=8)
    history: list[dict[str, Any]] = Field(max_length=8)
    metadata: PeerTaskMetadata

    @model_validator(mode="after")
    def terminal_artifact_semantics(self) -> Self:
        if self.history:
            raise ValueError("restricted peer profile does not accept history payloads")
        if self.status.state == "TASK_STATE_COMPLETED":
            if not self.artifacts:
                raise ValueError("completed peer task requires an Artifact")
        elif self.artifacts:
            raise ValueError("non-completed peer task cannot return an Artifact")
        return self


class PeerTaskResponse(StrictModel):
    task: PeerTask


class ProviderFact(StrictModel):
    id: Identity
    digest: Digest
    kind: Literal["event", "incident", "evidence", "runtime", "tool-result", "peer-result"]
    summary: str | None = Field(default=None, max_length=2048)


class RemainingBudget(StrictModel):
    llm_calls: int = Field(ge=0, le=2)
    mcp_rounds: int = Field(ge=0, le=2)
    tool_calls: int = Field(ge=0, le=6)
    outbound_delegations: int = Field(ge=0, le=2)
    deadline_unix_ms: int = Field(ge=1)


class ProviderRequest(StrictModel):
    schema_version: Literal["masi-analysis-provider-request/v1"]
    request_id: Identity
    phase: Literal["hypothesis", "synthesis"]
    task_id: Identity
    run_id: Identity
    skill: str
    input_digest: Digest
    locale: str = Field(min_length=1, max_length=32)
    system_policy_id: Literal["analysis-system-policy/v1"]
    facts: list[ProviderFact] = Field(max_length=352)
    tool_results: list[ProviderFact] = Field(max_length=6)
    peer_results: list[ProviderFact] = Field(max_length=2)
    untrusted_content_request: str = Field(max_length=2048)
    remaining_budget: RemainingBudget
    trace_id: Identity
    request_digest: Digest


class ProviderToolRequest(StrictModel):
    kind: Literal["tool", "resource"]
    name: Identity
    arguments: dict[str, Any] = Field(max_length=8)


class ProviderDelegation(StrictModel):
    peer_id: Identity
    question: str = Field(min_length=1, max_length=1024)


class ProviderResponse(StrictModel):
    schema_version: Literal["masi-analysis-provider-response/v1"]
    request_id: Identity
    input_digest: Digest
    outcome_hint: Literal["succeeded", "limited", "insufficient_evidence"]
    observed_claims: list[GroundedClaim] = Field(max_length=64)
    model_result_facts: list[GroundedClaim] = Field(max_length=32)
    model_explanation_facts: list[GroundedClaim] = Field(max_length=32)
    inferred_claims: list[str] = Field(max_length=64)
    uncertainties: list[str] = Field(max_length=64)
    limitations: list[str] = Field(max_length=64)
    missing_evidence: list[str] = Field(max_length=64)
    recommendations: list[str] = Field(max_length=64)
    generated_content: list[str] = Field(max_length=64)
    tool_requests: list[ProviderToolRequest] = Field(max_length=6)
    delegations: list[ProviderDelegation] = Field(max_length=2)
    response_digest: Digest


class BindingObservation(StrictModel):
    schema_version: Literal["masi-analysis-binding/v1"]
    plugin_id: Literal["masi.analysis.langgraph"]
    plugin_revision: Revision
    manifest_digest: Digest
    manifest_content_digest: Digest
    config_digest: Digest
    capability_digest: Digest
    resource_profile_digest: Digest
    qualification_digest: Digest
    binding_generation: int = Field(ge=1)
    scope: str = Field(min_length=1, max_length=256)
    activation_state: Literal["active", "draining", "disabled", "revoked", "expired"]
    qualification_status: Literal["qualified", "unqualified", "hold"]
    skill_ids: list[str] = Field(min_length=4, max_length=4)
    tool_allowlist: list[Identity] = Field(max_length=64)
    resource_allowlist: list[Identity] = Field(max_length=64)
    a2a_peer_allowlist: list[Identity] = Field(max_length=8)
    provider_profile_digest: Digest
    prompt_profile_digest: Digest
    tool_policy_digest: Digest
    redaction_profile_digest: Digest
    issued_at_unix_ms: int = Field(ge=1)
    expires_at_unix_ms: int = Field(ge=1)
    manager_identity: Identity
    binding_digest: Digest

    @model_validator(mode="after")
    def exact_skills(self) -> Self:
        if tuple(sorted(self.skill_ids)) != tuple(sorted(SKILLS)):
            raise ValueError("binding must declare exactly the four qualified skills")
        return self


class TLSConfig(StrictModel):
    enabled: bool
    cert_file: str | None
    key_file: str | None
    client_ca_file: str | None
    allowed_client_sans: list[str] = Field(min_length=1, max_length=32)


class ListenConfig(StrictModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1024, le=65535)


class RuntimeLimits(StrictModel):
    request_bytes: int = Field(ge=1024, le=65536)
    response_bytes: int = Field(ge=1024, le=131072)
    artifact_bytes: int = Field(ge=1024, le=65536)
    queue_depth: int = Field(ge=1, le=64)
    concurrency: int = Field(ge=1, le=16)
    task_retention_seconds: int = Field(ge=1, le=2592000)
    max_tasks: int = Field(ge=16, le=100000)
    max_trace_events_per_task: int = Field(ge=8, le=128)
    shutdown_grace_ms: int = Field(ge=100, le=30000)


class OutboundTLS(StrictModel):
    base_url: str = Field(max_length=512)
    allowed_ips: list[str] = Field(min_length=1, max_length=16)
    server_name: str = Field(min_length=1, max_length=253)
    ca_file: str | None
    cert_file: str | None
    key_file: str | None


class MCPConfig(OutboundTLS):
    profile_id: Literal["masi-mcp-readonly/v1"]
    profile_digest: Digest
    origin: str = Field(max_length=512)
    tool_allowlist: list[Identity] = Field(max_length=64)
    resource_allowlist: list[Identity] = Field(max_length=64)


class ProviderConfig(OutboundTLS):
    profile_id: Literal["analysis-provider/v1"]
    profile_digest: Digest
    provider_id: Identity
    model_id: Identity
    auth_secret_file: str | None


class PeerConfig(OutboundTLS):
    peer_id: Identity
    plugin_id: Identity
    binding_generation: int = Field(ge=1)


class AnalysisConfig(StrictModel):
    schema_version: Literal["masi-analysis-config/v1"]
    runtime_profile: Literal["acceptance", "production"]
    plugin_id: Literal["masi.analysis.langgraph"]
    contract_root: str
    manifest_path: str
    binding_path: str
    store_path: str
    health_state_path: str
    listen: ListenConfig
    tls: TLSConfig
    limits: RuntimeLimits
    mcp: MCPConfig
    provider: ProviderConfig
    a2a_peers: list[PeerConfig] = Field(max_length=8)
    public_agent_card_url: str = Field(max_length=512)
    config_id: Identity
