#!/usr/bin/env python3
"""Run the release Analysis process over real mTLS A2A/MCP/provider boundaries."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, cast

import httpx

from masi_analysis.canonical import canonical_digest, compute_artifact_digest, compute_input_digest, file_digest, go_json_bytes
from masi_analysis.config import load_runtime_material
from masi_analysis.constants import A2A_VERSION, SKILLS
from masi_analysis.models import AnalysisArtifact, FrozenInput

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support import DIGEST_A, frozen_input, write_runtime


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def command(argv: list[str]) -> None:
    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def generate_pki(root: Path) -> dict[str, str]:
    ca_key = root / "ca.key"
    ca_cert = root / "ca.pem"
    command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=MASI Analysis Test CA",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
        ]
    )

    def leaf(name: str, sans: str) -> tuple[str, str]:
        key = root / f"{name}.key"
        csr = root / f"{name}.csr"
        cert = root / f"{name}.pem"
        command(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                f"/CN={name}",
                "-addext",
                f"subjectAltName={sans}",
                "-keyout",
                str(key),
                "-out",
                str(csr),
            ]
        )
        command(
            [
                "openssl",
                "x509",
                "-req",
                "-days",
                "2",
                "-in",
                str(csr),
                "-CA",
                str(ca_cert),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-copy_extensions",
                "copy",
                "-out",
                str(cert),
            ]
        )
        key.chmod(0o600)
        return str(cert), str(key)

    server_cert, server_key = leaf("localhost", "DNS:localhost,IP:127.0.0.1")
    module_cert, module_key = leaf("masi-analysis", "DNS:masi-analysis")
    control_cert, control_key = leaf("masi-control", "DNS:masi-control")
    auditor_cert, auditor_key = leaf("masi-auditor", "DNS:masi-auditor")
    return {
        "ca": str(ca_cert),
        "server_cert": server_cert,
        "server_key": server_key,
        "module_cert": module_cert,
        "module_key": module_key,
        "control_cert": control_cert,
        "control_key": control_key,
        "auditor_cert": auditor_cert,
        "auditor_key": auditor_key,
    }


def exact_input(material: Any, *, task_id: str, content: str, quality: str = "valid", skill: str = "analyze_nids_incident") -> FrozenInput:
    value = frozen_input(task_id=task_id, quality=quality, content_request=content)
    value = value.model_copy(
        update={
            "skill": skill,
            "config_digest": material.config_digest,
            "provider_profile_digest": material.binding.provider_profile_digest,
            "prompt_profile_digest": material.binding.prompt_profile_digest,
            "tool_policy_digest": material.binding.tool_policy_digest,
            "redaction_profile_digest": material.binding.redaction_profile_digest,
            "input_digest": DIGEST_A,
        }
    )
    return value.model_copy(update={"input_digest": compute_input_digest(value)})


def a2a_body(input_bundle: FrozenInput) -> bytes:
    value = {
        "message": {
            "messageId": "msg-" + input_bundle.task_id,
            "role": "ROLE_USER",
            "parts": [{"data": input_bundle.model_dump(mode="json"), "mediaType": "application/json"}],
            "metadata": {
                "schemaVersion": input_bundle.schema_version,
                "inputDigest": input_bundle.input_digest,
                "pluginId": input_bundle.plugin_id,
                "bindingGeneration": input_bundle.binding_generation,
                "deadlineUnixMs": input_bundle.deadline_unix_ms,
            },
        },
        "configuration": {"acceptedOutputModes": ["application/json"]},
        "metadata": {
            "traceId": input_bundle.trace_id,
            "noPush": True,
            "noStreaming": True,
            "delegationDepth": input_bundle.budgets.delegation_depth,
            "delegationPath": input_bundle.delegation_path,
        },
    }
    return go_json_bytes(value)


def headers(version: str = A2A_VERSION) -> dict[str, str]:
    return {"A2A-Version": version, "Content-Type": "application/a2a+json", "Accept": "application/a2a+json"}


def wait_process(process: subprocess.Popen[bytes], label: str) -> None:
    code = process.poll()
    if code is not None:
        raise RuntimeError(f"{label} exited early with {code}")


def wait_ready(binary: str, config: Path, process: subprocess.Popen[bytes], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wait_process(process, "analysis")
        probe = subprocess.run([binary, "--config", str(config), "--probe", "ready"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            return
        time.sleep(0.1)
    raise RuntimeError("analysis readiness deadline exceeded")


def submit_and_poll(client: httpx.Client, input_bundle: FrozenInput, *, timeout: float = 15) -> tuple[dict[str, Any], AnalysisArtifact | None]:
    response = client.post("/message:send", headers=headers(), content=a2a_body(input_bundle))
    if response.status_code != 200 or response.headers.get("A2A-Version") != A2A_VERSION:
        raise RuntimeError(f"submit failed: {response.status_code} {response.text[:256]}")
    task = response.json()["task"]
    deadline = time.monotonic() + timeout
    while task["status"]["state"] in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"} and time.monotonic() < deadline:
        time.sleep(0.05)
        polled = client.get(f"/tasks/{task['id']}", headers={"A2A-Version": A2A_VERSION, "Accept": "application/a2a+json"})
        if polled.status_code != 200:
            raise RuntimeError(f"poll failed: {polled.status_code}")
        task = polled.json()["task"]
    if task["status"]["state"] == "TASK_STATE_COMPLETED":
        artifact = AnalysisArtifact.model_validate(task["artifacts"][0]["parts"][0]["data"])
        if compute_artifact_digest(artifact) != artifact.artifact_digest:
            raise RuntimeError("artifact digest mismatch")
        return task, artifact
    return task, None


def tls_version(port: int, pki: dict[str, str], *, maximum: ssl.TLSVersion = ssl.TLSVersion.TLSv1_3) -> str:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=pki["ca"])
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = maximum
    context.load_cert_chain(pki["control_cert"], pki["control_key"])
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname="localhost") as secured:
            return secured.version() or "unknown"


def httpx_context(ca: str, cert: str | None = None, key: str | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    if cert and key:
        context.load_cert_chain(cert, key)
    return context


def revoke_binding(binding_path: Path) -> None:
    value = json.loads(binding_path.read_bytes())
    value["activation_state"] = "revoked"
    value["binding_digest"] = canonical_digest({key: item for key, item in value.items() if key != "binding_digest"})
    temporary = binding_path.with_suffix(".next")
    temporary.write_bytes(go_json_bytes(value))
    os.replace(temporary, binding_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=str(Path(__file__).resolve().parents[1] / ".venv/bin/masi-analysis"))
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--source-tree-digest", required=True)
    parser.add_argument("--working-tree-status-digest", required=True)
    args = parser.parse_args()
    for name in ("source_tree_digest", "working_tree_status_digest"):
        value = str(getattr(args, name))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value) or value == "sha256:" + "0" * 64:
            raise SystemExit(f"{name} must be a non-sentinel exact digest")
    module_root = Path(__file__).resolve().parents[1]
    repository_root = module_root.parent
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    scenarios: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="masi-analysis-blackbox-") as temporary:
        root = Path(temporary)
        pki = generate_pki(root)
        service_port, provider_port, mcp_port, peer_port = free_port(), free_port(), free_port(), free_port()
        config_path = write_runtime(
            root / "runtime",
            repository_root / "contracts",
            service_port=service_port,
            mcp_port=mcp_port,
            provider_port=provider_port,
            peer_port=peer_port,
            tls_files=pki,
        )
        material = load_runtime_material(str(config_path))
        neighbors_log = (root / "neighbors.log").open("wb")
        service_log = (root / "analysis.log").open("wb")
        neighbors = subprocess.Popen(
            [
                sys.executable,
                str(module_root / "tests/fake_neighbors.py"),
                "--provider-port",
                str(provider_port),
                "--mcp-port",
                str(mcp_port),
                "--peer-port",
                str(peer_port),
                "--cert",
                pki["server_cert"],
                "--key",
                pki["server_key"],
                "--ca",
                pki["ca"],
            ],
            stdout=neighbors_log,
            stderr=subprocess.STDOUT,
        )
        analysis: subprocess.Popen[bytes] | None = None
        try:
            fixture_client = httpx.Client(verify=httpx_context(pki["ca"], pki["module_cert"], pki["module_key"]), trust_env=False, timeout=3)
            fixture_deadline = time.monotonic() + 10
            fixture_error = "no response"
            while time.monotonic() < fixture_deadline:
                wait_process(neighbors, "neighbors")
                try:
                    if fixture_client.get(f"https://localhost:{provider_port}/stats").status_code == 200:
                        break
                except httpx.TransportError as exc:
                    fixture_error = repr(exc)
                    time.sleep(0.1)
            else:
                neighbors_log.flush()
                detail = (root / "neighbors.log").read_text(errors="replace")[-2000:]
                raise RuntimeError(f"neighbor readiness deadline exceeded: {fixture_error}; {detail}")
            analysis = subprocess.Popen([args.binary, "--config", str(config_path)], stdout=service_log, stderr=subprocess.STDOUT)
            wait_ready(args.binary, config_path, analysis)
            client = httpx.Client(
                base_url=f"https://localhost:{service_port}",
                verify=httpx_context(pki["ca"], pki["control_cert"], pki["control_key"]),
                trust_env=False,
                timeout=20,
            )

            card = client.get("/.well-known/agent-card.json", headers={"A2A-Version": A2A_VERSION})
            if card.status_code != 200 or len(card.json().get("skills", [])) != 4:
                raise RuntimeError("protected Agent Card failed")
            scenarios.append({"scenario_id": "A2A_AGENT_CARD", "result": "PASS"})

            success_input = exact_input(material, task_id="task-success", content="needs-tool")
            task, artifact = submit_and_poll(client, success_input)
            if artifact is None or artifact.analysis_outcome != "succeeded" or len(artifact.model_explanation_facts) != 1:
                service_log.flush()
                detail = (root / "analysis.log").read_text(errors="replace")[-2000:]
                neighbor_stats = fixture_client.get(f"https://localhost:{provider_port}/stats").json()
                raise RuntimeError(
                    f"grounded MCP/tool Analysis scenario failed: state={task['status']} "
                    f"artifact={artifact.model_dump(mode='json') if artifact else None}; stats={neighbor_stats}; log={detail}"
                )
            if artifact.non_executable is not True or artifact.deployment_eligible is not False:
                raise RuntimeError("artifact execution fence failed")
            scenarios.append({"scenario_id": "A2A_MCP_GROUNDED_SUCCESS", "result": "PASS"})

            peer_task, peer_artifact = submit_and_poll(client, exact_input(material, task_id="task-peer", content="needs-peer"))
            if peer_artifact is None or peer_task["status"]["state"] != "TASK_STATE_COMPLETED":
                raise RuntimeError("bounded outbound A2A delegation failed")
            peer_stats_before_loop = fixture_client.get(f"https://localhost:{provider_port}/stats").json()
            peer_submits = peer_stats_before_loop.get("peer_calls", {}).get("submit", 0)
            loop_input = exact_input(material, task_id="task-peer-loop", content="needs-peer")
            loop_budgets = loop_input.budgets.model_copy(update={"delegation_depth": 1})
            loop_input = loop_input.model_copy(update={"budgets": loop_budgets, "delegation_path": ["fixture-peer"], "input_digest": DIGEST_A})
            loop_input = loop_input.model_copy(update={"input_digest": compute_input_digest(loop_input)})
            _, loop_artifact = submit_and_poll(client, loop_input)
            peer_stats_after_loop = fixture_client.get(f"https://localhost:{provider_port}/stats").json()
            if (
                loop_artifact is None
                or loop_artifact.analysis_outcome != "limited"
                or peer_stats_after_loop.get("peer_calls", {}).get("submit", 0) != peer_submits
            ):
                raise RuntimeError("A2A delegation loop was not rejected before peer traffic")
            scenarios.append({"scenario_id": "A2A_OUTBOUND_DELEGATION_LOOP_FENCE", "result": "PASS"})

            same, same_artifact = submit_and_poll(client, success_input)
            if same_artifact is None or same_artifact.artifact_digest != artifact.artifact_digest or same["id"] != task["id"]:
                raise RuntimeError("idempotent response drifted")
            conflict_input = exact_input(material, task_id="task-success", content="different")
            conflict = client.post("/message:send", headers=headers(), content=a2a_body(conflict_input))
            if conflict.status_code != 409 or conflict.json().get("error_code") != "IDEMPOTENCY_CONFLICT":
                raise RuntimeError("idempotency conflict was not stable")
            scenarios.append({"scenario_id": "A2A_IDEMPOTENCY_CONFLICT", "result": "PASS"})

            auditor = httpx.Client(
                base_url=f"https://localhost:{service_port}",
                verify=httpx_context(pki["ca"], pki["auditor_cert"], pki["auditor_key"]),
                trust_env=False,
                timeout=5,
            )
            cross_poll = auditor.get(f"/tasks/{success_input.task_id}", headers={"A2A-Version": A2A_VERSION})
            cross_trace = auditor.get(f"/traces/{success_input.task_id}", headers={"A2A-Version": A2A_VERSION})
            cross_submit = auditor.post("/message:send", headers=headers(), content=a2a_body(success_input))
            if any(response.status_code != 404 for response in (cross_poll, cross_trace, cross_submit)):
                raise RuntimeError("authorized cross-client private task isolation failed")
            auditor.close()
            scenarios.append({"scenario_id": "A2A_REQUESTER_TASK_ISOLATION", "result": "PASS"})

            version = client.post("/message:send", headers=headers("0.3"), content=a2a_body(success_input))
            missing_version = client.post(
                "/message:send",
                headers={"Content-Type": "application/a2a+json", "Accept": "application/a2a+json"},
                content=a2a_body(success_input),
            )
            oversize = client.post("/message:send", headers=headers(), content=b"x" * 70_000)
            if any(response.status_code != expected for response, expected in ((version, 400), (missing_version, 400), (oversize, 413))):
                raise RuntimeError("version/body negative matrix failed")
            scenarios.append({"scenario_id": "A2A_VERSION_FRAMING_NEGATIVES", "result": "PASS"})

            low_task, low_artifact = submit_and_poll(client, exact_input(material, task_id="task-low", content="bounded", quality="low"))
            del low_task
            if low_artifact is None or low_artifact.analysis_outcome != "limited" or low_artifact.quality != "low":
                raise RuntimeError("low-quality degradation failed")
            timeout_task, timeout_artifact = submit_and_poll(
                client, exact_input(material, task_id="task-provider-timeout", content="provider-timeout"), timeout=15
            )
            del timeout_task
            if timeout_artifact is None or timeout_artifact.analysis_outcome != "limited":
                raise RuntimeError("provider timeout did not produce a bounded limited Artifact")
            unsafe_task, unsafe_artifact = submit_and_poll(client, exact_input(material, task_id="task-unsafe", content="unsafe-output"))
            del unsafe_task
            unsafe_bytes = go_json_bytes(unsafe_artifact.model_dump(mode="json")) if unsafe_artifact else b""
            if unsafe_artifact is None or unsafe_artifact.analysis_outcome != "limited" or b"P4Runtime" in unsafe_bytes:
                raise RuntimeError("unsafe provider output was not rejected")
            missing_task, missing_artifact = submit_and_poll(client, exact_input(material, task_id="task-missing-explanation", content="missing-explanation"))
            del missing_task
            if missing_artifact is None or missing_artifact.model_explanation_facts:
                raise RuntimeError("explanation metadata absence was not preserved")
            _, insufficient_artifact = submit_and_poll(client, exact_input(material, task_id="task-insufficient", content="insufficient"))
            if insufficient_artifact is None or insufficient_artifact.analysis_outcome != "insufficient_evidence":
                raise RuntimeError("insufficient-evidence outcome drifted")
            _, malformed_artifact = submit_and_poll(client, exact_input(material, task_id="task-malformed-provider", content="malformed-provider"))
            if malformed_artifact is None or malformed_artifact.analysis_outcome != "limited":
                raise RuntimeError("malformed provider response did not degrade")
            invalid_started = time.monotonic()
            _, invalid_tool_artifact = submit_and_poll(client, exact_input(material, task_id="task-invalid-tool", content="invalid-tool-target"))
            _, tool_timeout_artifact = submit_and_poll(client, exact_input(material, task_id="task-tool-timeout", content="tool-timeout"))
            invalid_duration = time.monotonic() - invalid_started
            if (
                invalid_tool_artifact is None
                or invalid_tool_artifact.analysis_outcome != "limited"
                or tool_timeout_artifact is None
                or tool_timeout_artifact.analysis_outcome != "limited"
                or invalid_duration > 6
            ):
                raise RuntimeError(
                    "tool target/timeout bounded degradation failed: "
                    f"invalid={invalid_tool_artifact.analysis_outcome if invalid_tool_artifact else None} "
                    f"timeout={tool_timeout_artifact.analysis_outcome if tool_timeout_artifact else None} "
                    f"duration={invalid_duration:.3f}"
                )
            scenarios.append({"scenario_id": "LIMITED_TIMEOUT_SECURITY_XAI_NEGATIVES", "result": "PASS"})

            for index, skill in enumerate(SKILLS, 1):
                skill_task, skill_artifact = submit_and_poll(
                    client,
                    exact_input(material, task_id=f"task-skill-{index}", content="bounded content", skill=skill),
                )
                if skill_task["status"]["state"] != "TASK_STATE_COMPLETED" or skill_artifact is None:
                    raise RuntimeError(f"skill {skill} failed")
            scenarios.append({"scenario_id": "FOUR_SKILLS", "result": "PASS"})

            trace = client.get(f"/traces/{success_input.task_id}", headers={"A2A-Version": A2A_VERSION})
            metrics = client.get("/metrics")
            if trace.status_code != 200 or "GROUNDING_VALIDATED" not in trace.text or success_input.task_id in metrics.text:
                raise RuntimeError("trace or low-cardinality metrics oracle failed")
            scenarios.append({"scenario_id": "TRACE_METRICS_REDACTION", "result": "PASS"})

            if tls_version(service_port, pki) != "TLSv1.3":
                raise RuntimeError("TLS 1.3 positive failed")
            try:
                tls_version(service_port, pki, maximum=ssl.TLSVersion.TLSv1_2)
            except (ssl.SSLError, OSError):
                pass
            else:
                raise RuntimeError("TLS 1.2 was accepted")
            wrong = httpx.Client(
                base_url=f"https://localhost:{service_port}",
                verify=httpx_context(pki["ca"], pki["module_cert"], pki["module_key"]),
                trust_env=False,
            )
            wrong_identity = wrong.get("/.well-known/agent-card.json", headers={"A2A-Version": A2A_VERSION})
            if wrong_identity.status_code != 401:
                raise RuntimeError("wrong trusted client SAN was not rejected")
            try:
                httpx.get(
                    f"https://localhost:{service_port}/.well-known/agent-card.json",
                    headers={"A2A-Version": A2A_VERSION},
                    verify=pki["ca"],
                    trust_env=False,
                    timeout=2,
                )
            except httpx.TransportError:
                pass
            else:
                raise RuntimeError("client-certificate omission was accepted")
            scenarios.append({"scenario_id": "TLS13_MTLS_IDENTITY", "result": "PASS"})

            stats_before_budget = fixture_client.get(f"https://localhost:{provider_port}/stats").json()
            budget_input = exact_input(
                material,
                task_id="task-external-budget-zero",
                content="needs-tool needs-peer",
            )
            zero_external_budget = budget_input.budgets.model_copy(update={"tool_calls": 0, "outbound_delegations": 0})
            budget_input = budget_input.model_copy(update={"budgets": zero_external_budget, "input_digest": DIGEST_A})
            budget_input = budget_input.model_copy(update={"input_digest": compute_input_digest(budget_input)})
            _, budget_artifact = submit_and_poll(client, budget_input)
            stats = fixture_client.get(f"https://localhost:{provider_port}/stats").json()
            if (
                budget_artifact is None
                or budget_artifact.analysis_outcome != "limited"
                or stats.get("mcp_calls", {}).get("initialize", 0) != stats_before_budget.get("mcp_calls", {}).get("initialize", 0)
                or stats.get("peer_calls", {}).get("submit", 0) != stats_before_budget.get("peer_calls", {}).get("submit", 0)
            ):
                raise RuntimeError("zero external-call budget was not rejected before network traffic")
            if stats.get("active_sessions") != 0 or stats.get("mcp_calls", {}).get("tools/call", 0) > 6:
                raise RuntimeError("MCP session/call budget leaked")
            scenarios.append({"scenario_id": "MCP_BUDGET_SESSION_CLEANUP", "result": "PASS"})

            late_input = exact_input(material, task_id="task-late-fence", content="provider-timeout")
            submitted = client.post("/message:send", headers=headers(), content=a2a_body(late_input))
            if submitted.status_code != 200:
                raise RuntimeError("late-fence setup failed")
            revoke_binding(Path(material.config.binding_path))
            analysis.send_signal(signal.SIGHUP)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                probe = subprocess.run(
                    [args.binary, "--config", str(config_path), "--probe", "ready"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if probe.returncode != 0:
                    break
                time.sleep(0.05)
            late_task: dict[str, Any] | None = None
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                polled = client.get(f"/tasks/{late_input.task_id}", headers={"A2A-Version": A2A_VERSION})
                late_task = cast(dict[str, Any], polled.json()["task"])
                status = cast(dict[str, Any], late_task["status"])
                if status["state"] not in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                    break
                time.sleep(0.1)
            if not late_task or late_task["status"]["state"] != "TASK_STATE_REJECTED" or late_task["artifacts"]:
                raise RuntimeError("revoked-generation late Artifact was not fenced")
            post_revoke = client.post(
                "/message:send",
                headers=headers(),
                content=a2a_body(exact_input(material, task_id="task-after-revoke", content="bounded")),
            )
            if post_revoke.status_code != 503 or post_revoke.json().get("error_code") != "BINDING_UNAVAILABLE":
                raise RuntimeError("post-revoke admission did not fail closed")
            scenarios.append({"scenario_id": "BINDING_REVOKE_LATE_FENCE", "result": "PASS"})

            connection = sqlite3.connect(material.config.store_path)
            table_names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            connection.close()
            if not table_names or any(not name.startswith("analysis_") for name in table_names):
                raise RuntimeError("private state contains non-analysis table ownership")
            scenarios.append({"scenario_id": "ZERO_CORE_P4_EFFECT_MUTATION", "result": "PASS"})

            started_stop = time.monotonic()
            analysis.send_signal(signal.SIGTERM)
            analysis.wait(timeout=5)
            if analysis.returncode != 0 or time.monotonic() - started_stop > 5:
                raise RuntimeError("bounded graceful shutdown failed")
            scenarios.append({"scenario_id": "BOUNDED_DRAIN_SHUTDOWN", "result": "PASS"})

            evidence = {
                "schema_version": "analysis-blackbox-evidence/v1",
                "module_id": "MOD-AGENT-001",
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "source_tree_digest": args.source_tree_digest,
                "working_tree_status_digest": args.working_tree_status_digest,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "real_release_process": True,
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "langgraph_version": package_version("langgraph"),
                "mcp_sdk_version": package_version("mcp"),
                "real_a2a_boundary": True,
                "real_mcp_client_boundary": True,
                "deterministic_provider_fixture": True,
                "provider_fixture_is_real_provider_qualification": False,
                "tls_version": "TLSv1.3",
                "binary_digest": file_digest(Path(args.binary).read_bytes()),
                "config_digest": material.config_digest,
                "binding_digest": material.binding.binding_digest,
                "manifest_content_digest": material.manifest_content_digest,
                "golden_catalog_digest": file_digest((repository_root / "contracts/analysis/v1/golden/catalog.json").read_bytes()),
                "compatibility_matrix_digest": file_digest((module_root / "compatibility-matrix.json").read_bytes()),
                "artifact_digest": artifact.artifact_digest,
                "zero_core_p4_effect_mutation": True,
                "shutdown_seconds": time.monotonic() - started_stop,
                "scenario_count": len(scenarios),
                "scenarios": scenarios,
            }
            if args.evidence:
                Path(args.evidence).write_bytes(go_json_bytes(evidence) + b"\n")
            print(json.dumps(evidence, sort_keys=True))
        finally:
            if analysis is not None and analysis.poll() is None:
                analysis.terminate()
                try:
                    analysis.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    analysis.kill()
                    analysis.wait()
            neighbors.terminate()
            try:
                neighbors.wait(timeout=5)
            except subprocess.TimeoutExpired:
                neighbors.kill()
                neighbors.wait()
            service_log.close()
            neighbors_log.close()


if __name__ == "__main__":
    main()
