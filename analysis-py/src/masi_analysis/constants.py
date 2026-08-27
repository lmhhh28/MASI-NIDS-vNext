"""Frozen module identities and hard security ceilings."""

from __future__ import annotations

PLUGIN_ID = "masi.analysis.langgraph"
VERSION = "1.0.0"
A2A_VERSION = "1.0"
INPUT_SCHEMA = "masi-analysis-input/v1"
ARTIFACT_SCHEMA = "masi-analysis-artifact/v1"
GRAPH_VERSION = "masi-analysis-graph/v1"
MCP_PROFILE = "masi-mcp-readonly/v1"
MCP_VERSION = "2025-11-25"
PROVIDER_PROFILE = "analysis-provider/v1"

SKILLS = (
    "analyze_nids_incident",
    "compare_event_windows",
    "draft_mitigation_advice",
    "generate_incident_content",
)

HARD_LLM_CALLS = 2
HARD_MCP_ROUNDS = 2
HARD_TOOL_CALLS = 6
HARD_TOOL_PARALLELISM = 4
HARD_TOOL_TIMEOUT_MS = 5_000
HARD_TOOL_RESPONSE_BYTES = 65_536
HARD_TOOL_TOTAL_RESPONSE_BYTES = 262_144
HARD_LLM_TIMEOUT_MS = 6_000
HARD_RESULT_BUDGET_MS = 8_000
HARD_GRAPH_DEADLINE_MS = 30_000
HARD_ARTIFACT_BYTES = 65_536
HARD_A2A_DELEGATIONS = 2
HARD_A2A_DEPTH = 1
HARD_A2A_POLLS = 3
HARD_A2A_RESPONSE_BYTES = 131_072

TOOL_ARGUMENT_FIELDS: dict[str, tuple[str, str]] = {
    "masi.events.get": ("event_id", "event"),
    "masi.incidents.get": ("incident_id", "incident"),
    "masi.evidence.get": ("evidence_id", "evidence"),
    "masi.targets.get": ("target_id", "runtime"),
}

RESOURCE_URIS: dict[str, str] = {
    "masi.incidents.open": "masi://incidents/open",
    "masi.evidence.recent": "masi://evidence/recent",
}

TASK_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_WORKING = "TASK_STATE_WORKING"
TASK_COMPLETED = "TASK_STATE_COMPLETED"
TASK_FAILED = "TASK_STATE_FAILED"
TASK_CANCELED = "TASK_STATE_CANCELED"
TASK_REJECTED = "TASK_STATE_REJECTED"
