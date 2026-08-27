#!/usr/bin/env python3
"""Validate cross-field semantics for connected-system rehearsal evidence."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


class EvidenceSemanticError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceSemanticError(message)


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        output: list[str] = []
        for item in value.values():
            output.extend(string_values(item))
        return output
    if isinstance(value, list):
        output = []
        for item in value:
            output.extend(string_values(item))
        return output
    return []


def validate_semantics(document: dict[str, Any]) -> None:
    require(
        document.get("schema_version") == "connected-system-web-rehearsal/v1",
        "connected system schema identity drifted",
    )
    require(
        document.get("level") == "REHEARSAL"
        and document.get("qualification") == "NOT_QUALIFIED",
        "connected rehearsal must not become qualified",
    )
    started = timestamp(document["started_at"])
    finished = timestamp(document["finished_at"])
    require(started <= finished, "top-level evidence time order is invalid")

    triton = document["triton_statistics"]
    require(
        triton["inference_count_after"] - triton["inference_count_before"]
        == triton["inference_count_delta"],
        "Triton inference count delta is inconsistent",
    )
    traffic = document["traffic"]
    require(
        traffic["started_at_unix_ns"] <= traffic["finished_at_unix_ns"],
        "traffic time order is invalid",
    )

    participants = document["participants"]
    plugin_runtime = document["plugin_runtime"]
    plugin = document["plugin_statistics_evidence"]
    plugin_participants = plugin["participants"]
    plugin_core = plugin["core_evidence"]
    require(
        participants["control_binary_digest"]
        == participants["statistics_go_dispatcher_binary_digest"]
        == plugin_participants["control_binary_digest"],
        "statistics dispatcher is not bound to the running Control binary",
    )
    require(
        participants["plugin_host_binary_digest"]
        == plugin_participants["plugin_host_binary_digest"],
        "Plugin Host binary digest drifted",
    )
    require(
        participants["wasm_artifact_digest"]
        == plugin_runtime["wasm_artifact_digest"]
        == plugin_participants["wasm_artifact_digest"]
        == plugin_core["host_artifact_digest"],
        "Wasm artifact digest drifted across runtime/dispatcher evidence",
    )
    require(
        plugin_runtime["host_envelope_digest"] == plugin_core["host_envelope_digest"],
        "Plugin Host binding envelope drifted",
    )
    require(
        plugin_core["external_dispatcher"] is True
        and plugin_core["dispatcher_owner"] == "control-core-maintenance",
        "statistics run was not advanced by the running Control maintenance dispatcher",
    )

    analysis = document["analysis_evidence"]
    analysis_runtime = document["analysis_runtime"]
    require(
        participants["analysis_binary_digest"]
        and analysis_runtime["plugin_id"] == analysis["plugin_id"],
        "Analysis runtime/plugin identity drifted",
    )
    require(
        analysis_runtime["binding_generation"] == analysis["binding_generation"],
        "Analysis binding generation drifted",
    )
    require(
        analysis["non_executable"] is True and analysis["deployment_eligible"] is False,
        "Analysis artifact executable boundary drifted",
    )
    require(
        plugin_runtime["clean_shutdown"] is True
        and analysis_runtime["clean_shutdown"] is True,
        "side-plane runtime cleanup was not observed",
    )

    event = document["event_evidence"]
    edge = document["edge_evidence"]
    require(
        edge["output_digest"] == event["output_digest"],
        "Edge output digest is not the committed Event output digest",
    )
    observations = document["web_observations"]
    require(len(observations) == 3, "exactly three browser observations are required")
    require(
        {item["browser"] for item in observations} == {"chromium", "firefox", "webkit"},
        "browser observations must cover Chromium, Firefox, and WebKit exactly once",
    )
    require(
        len({item["screenshot_digest"] for item in observations}) == 3,
        "browser screenshots must be independently rendered",
    )
    for observation in observations:
        require(
            observation["event_id"] == event["event_id"]
            and observation["incident_id"] == event["incident_id"],
            f"{observation['browser']} Event/Incident identity drifted",
        )
        require(
            observation["statistics_definition_id"] == plugin_core["definition_id"]
            and observation["statistics_artifact_id"] == plugin_core["artifact_id"]
            and observation["statistics_metric_value"] == plugin_core["metric_value"],
            f"{observation['browser']} statistics artifact identity drifted",
        )
        require(
            observation["analysis_task_id"] == analysis["task_id"]
            and observation["analysis_artifact_id"] == analysis["artifact_id"]
            and observation["analysis_outcome"] == analysis["analysis_outcome"]
            and observation["non_executable"] == analysis["non_executable"]
            and observation["deployment_eligible"] == analysis["deployment_eligible"],
            f"{observation['browser']} Analysis artifact identity drifted",
        )
        browser_started = timestamp(observation["started_at"])
        browser_finished = timestamp(observation["finished_at"])
        require(
            started <= browser_started <= browser_finished <= finished,
            f"{observation['browser']} timeline is outside the parent run",
        )

    if document["central_oci_result"] == "HOLD":
        require(
            document["qualification"] == "NOT_QUALIFIED"
            and document["level"] == "REHEARSAL",
            "Central HOLD may only appear in an unqualified rehearsal",
        )
    for value in string_values(document):
        require(
            "-----BEGIN PRIVATE KEY-----" not in value,
            "private key leaked into evidence",
        )
        require(
            not value.startswith("postgres://"), "PostgreSQL DSN leaked into evidence"
        )
        require(
            not value.startswith("/tmp/"), "temporary secret path leaked into evidence"
        )
        require(
            "/home/lmhhh/MASI-NIDS-vNext" not in value,
            "workspace path leaked into portable evidence",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    schema_path = args.schema or (
        repo / "contracts/evidence/connected-system-web-rehearsal/v1/schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    document = json.loads(args.evidence.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            document
        ),
        key=lambda error: list(error.path),
    )
    if errors:
        raise EvidenceSemanticError(
            "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
        )
    validate_semantics(document)
    print(json.dumps({"schema_version": document["schema_version"], "result": "PASS"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
