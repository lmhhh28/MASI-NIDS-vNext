#!/usr/bin/env python3
"""Exact 60-second warmup plus four 900-second real-process Analysis soak phases."""

from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import json
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from masi_analysis.canonical import canonical_digest, file_digest, go_json_bytes


def account_state(
    counters: dict[str, int],
    state_counts: dict[str, int],
    unclassified_samples: list[str],
    *,
    state: str,
    phase: str,
    crash_expected: bool = False,
) -> None:
    state_key = f"{phase}:{state}"
    state_counts[state_key] = state_counts.get(state_key, 0) + 1
    if state == "TASK_STATE_COMPLETED":
        counters["completed"] += 1
    elif state == "TASK_STATE_REJECTED":
        counters["typed_rejected"] += 1
    elif state == "TASK_STATE_FAILED":
        counters["failed"] += 1
    elif crash_expected and state in {"http_error", "submitted"}:
        if state == "http_error":
            counters["http_error"] += 1
    else:
        if state == "http_error":
            counters["http_error"] += 1
        counters["unclassified"] += 1
        if len(unclassified_samples) < 16:
            unclassified_samples.append(state_key)


def wait_http_ready(client: httpx.Client, *, timeout: float = 20.0, poll_interval: float = 0.05) -> None:
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("readiness timeout and poll interval must be positive")
    deadline = time.monotonic() + timeout
    last_reason = "no readiness response"
    while True:
        try:
            response = client.get("/health/ready")
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("ready") is True:
                    return
                last_reason = f"status=200 ready={payload.get('ready') if isinstance(payload, dict) else 'non-object'}"
            else:
                last_reason = f"status={response.status_code}"
        except (httpx.HTTPError, TypeError, ValueError) as error:
            last_reason = f"{type(error).__name__}: {str(error)[:256]}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Analysis HTTPS readiness timed out: {last_reason}")
        time.sleep(min(poll_interval, remaining))


def resources(pid: int) -> tuple[int, int]:
    rss = 0
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) * 1024
            break
    return rss, len(list(Path(f"/proc/{pid}/fd").iterdir()))


def main() -> None:
    parser = argparse.ArgumentParser()
    module_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--binary", default=str(module_root / ".venv/bin/masi-analysis"))
    parser.add_argument("--warmup-seconds", type=int, default=60)
    parser.add_argument("--phase-seconds", type=int, default=900)
    parser.add_argument("--source-tree-digest", default="sha256:" + "0" * 64)
    parser.add_argument("--evidence")
    args = parser.parse_args()
    if args.warmup_seconds < 1 or args.phase_seconds < 1:
        raise SystemExit("soak durations must be positive")
    repository = module_root.parent
    profile_path = repository / "contracts/profiles/v1/analysis-soak.json"
    profile_digest = file_digest(profile_path.read_bytes())
    run_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    support = runpy.run_path(str(module_root / "scripts/run-blackbox.py"), run_name="analysis_blackbox_support")
    free_port = cast(Callable[[], int], support["free_port"])
    generate_pki = cast(Callable[[Path], dict[str, str]], support["generate_pki"])
    httpx_context = support["httpx_context"]
    exact_input = support["exact_input"]
    a2a_body = support["a2a_body"]
    headers = support["headers"]
    wait_ready = support["wait_ready"]
    write_runtime = support["write_runtime"]
    with tempfile.TemporaryDirectory(prefix="masi-analysis-soak-") as temporary:
        root = Path(temporary)
        pki = generate_pki(root)
        service_port, provider_port, mcp_port, peer_port = free_port(), free_port(), free_port(), free_port()
        config_path = write_runtime(
            root / "runtime",
            repository / "contracts",
            service_port=service_port,
            provider_port=provider_port,
            mcp_port=mcp_port,
            peer_port=peer_port,
            tls_files=pki,
            limits_override={
                "queue_depth": 16,
                "concurrency": 8,
                "max_tasks": 100000,
                "task_retention_seconds": 120,
                "shutdown_grace_ms": 5000,
            },
            # The binding must cover warmup, all qualified phases, startup,
            # restart, drain, and evidence finalization. Expiry behavior has
            # its own black-box cases and must not invalidate the soak itself.
            binding_ttl_seconds=args.warmup_seconds + args.phase_seconds * 4 + 300,
        )
        material = support["load_runtime_material"](str(config_path))
        neighbor_log = (root / "neighbors.log").open("wb")
        analysis_log = (root / "analysis.log").open("ab")
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
            stdout=neighbor_log,
            stderr=subprocess.STDOUT,
        )
        analysis: subprocess.Popen[bytes] | None = None
        client: httpx.Client | None = None
        sequence = itertools.count(1)
        counters = {"completed": 0, "typed_rejected": 0, "failed": 0, "http_error": 0, "unclassified": 0}
        state_counts: dict[str, int] = {}
        unclassified_samples: list[str] = []
        http_error_details: list[dict[str, object]] = []
        samples: list[dict[str, Any]] = []
        phases: list[dict[str, Any]] = []
        crash_recovered = False
        recovered_incomplete = False

        def start_analysis() -> subprocess.Popen[bytes]:
            process = subprocess.Popen([args.binary, "--config", str(config_path)], stdout=analysis_log, stderr=subprocess.STDOUT)
            wait_ready(args.binary, config_path, process, timeout=20)
            return process

        def new_client() -> httpx.Client:
            return httpx.Client(
                base_url=f"https://localhost:{service_port}",
                verify=httpx_context(pki["ca"], pki["control_cert"], pki["control_key"]),
                trust_env=False,
                timeout=12,
                limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
            )

        def record_http_error(
            *,
            phase: str,
            task_id: str,
            operation: str,
            status_code: int | None = None,
            error: BaseException | None = None,
        ) -> str:
            if len(http_error_details) < 32:
                http_error_details.append(
                    {
                        "phase": phase,
                        "task_id": task_id,
                        "operation": operation,
                        "status_code": status_code,
                        "error_type": type(error).__name__ if error is not None else "unexpected_status",
                        "error_message": str(error)[:256] if error is not None else "",
                    }
                )
            return "http_error"

        def execute(content: str = "soak", *, phase: str) -> str:
            task_id = f"soak-{next(sequence)}"
            if client is None:
                return record_http_error(
                    phase=phase,
                    task_id=task_id,
                    operation="client-not-initialized",
                    error=RuntimeError("HTTP client is not initialized"),
                )
            input_bundle = exact_input(material, task_id=task_id, content=content)
            operation = "submit"
            try:
                response = client.post("/message:send", headers=headers(), content=a2a_body(input_bundle))
                if response.status_code != 200:
                    return record_http_error(
                        phase=phase,
                        task_id=task_id,
                        operation=operation,
                        status_code=response.status_code,
                    )
                task = response.json()["task"]
                deadline = time.monotonic() + 10
                while task["status"]["state"] in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"} and time.monotonic() < deadline:
                    time.sleep(0.02)
                    operation = "poll"
                    response = client.get(f"/tasks/{task['id']}", headers={"A2A-Version": "1.0"})
                    if response.status_code != 200:
                        return record_http_error(
                            phase=phase,
                            task_id=task_id,
                            operation=operation,
                            status_code=response.status_code,
                        )
                    task = response.json()["task"]
                return str(task["status"]["state"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                return record_http_error(phase=phase, task_id=task_id, operation=operation, error=error)

        def account(state: str, *, phase: str, crash_expected: bool = False) -> None:
            account_state(
                counters,
                state_counts,
                unclassified_samples,
                state=state,
                phase=phase,
                crash_expected=crash_expected,
            )

        def sample(phase: str, started: float, process: subprocess.Popen[bytes]) -> None:
            rss, fds = resources(process.pid)
            health = client.get("/health/live").json() if client is not None else {}
            samples.append(
                {
                    "phase": phase,
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                    "rss_bytes": rss,
                    "fd_count": fds,
                    "queue_depth": int(health.get("queue_depth", -1)),
                    "in_flight": int(health.get("in_flight", -1)),
                }
            )

        def run_rate_phase(phase_id: str, seconds: int, rate: int) -> None:
            assert analysis is not None
            started = time.monotonic()
            end = started + seconds
            next_sample = started
            count_before = dict(counters)
            while time.monotonic() < end:
                tick = time.monotonic()
                for _ in range(rate):
                    account(execute(phase=phase_id), phase=phase_id)
                if tick >= next_sample:
                    sample(phase_id, started, analysis)
                    next_sample += min(10, max(1, seconds / 4))
                remaining = min(1.0, end - time.monotonic())
                if remaining > 0:
                    time.sleep(remaining)
            phases.append(
                {
                    "phase_id": phase_id,
                    "required_seconds": seconds,
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                    "completed_delta": counters["completed"] - count_before["completed"],
                    "typed_rejected_delta": counters["typed_rejected"] - count_before["typed_rejected"],
                }
            )

        try:
            time.sleep(0.2)
            if neighbors.poll() is not None:
                raise RuntimeError("soak neighbors failed startup")
            analysis = start_analysis()
            client = new_client()
            wait_http_ready(client)

            warmup_started = time.monotonic()
            while time.monotonic() - warmup_started < args.warmup_seconds:
                account(execute(phase="warmup"), phase="warmup")
                time.sleep(max(0, min(1.0, warmup_started + args.warmup_seconds - time.monotonic())))
            warmup_elapsed_ms = (time.monotonic() - warmup_started) * 1000

            qualified_start_ns = time.monotonic_ns()
            run_rate_phase("steady", args.phase_seconds, 1)
            run_rate_phase("peak", args.phase_seconds, 5)

            def execute_saturated(_index: int) -> str:
                return execute("provider-delay-200ms", phase="saturation")

            saturation_started = time.monotonic()
            saturation_end = saturation_started + args.phase_seconds
            count_before = dict(counters)
            next_sample = saturation_started
            while time.monotonic() < saturation_end:
                with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
                    states = list(executor.map(execute_saturated, range(64)))
                for state in states:
                    account(state, phase="saturation")
                if time.monotonic() >= next_sample:
                    sample("saturation", saturation_started, analysis)
                    next_sample += min(10, max(1, args.phase_seconds / 4))
                remaining = min(5.0, saturation_end - time.monotonic())
                if remaining > 0:
                    time.sleep(remaining)
            phases.append(
                {
                    "phase_id": "saturation",
                    "required_seconds": args.phase_seconds,
                    "elapsed_ms": (time.monotonic() - saturation_started) * 1000,
                    "completed_delta": counters["completed"] - count_before["completed"],
                    "typed_rejected_delta": counters["typed_rejected"] - count_before["typed_rejected"],
                }
            )

            recovery_started = time.monotonic()
            recovery_end = recovery_started + args.phase_seconds
            count_before = dict(counters)
            crash_at = recovery_started + args.phase_seconds / 3
            crashed = False
            next_sample = recovery_started
            while time.monotonic() < recovery_end:
                if not crashed and time.monotonic() >= crash_at:
                    assert analysis is not None and client is not None
                    crash_input = exact_input(material, task_id="soak-crash-inflight", content="provider-timeout")
                    crash_client = client
                    crash_body = a2a_body(crash_input)

                    def crash_request(bound_client: httpx.Client = crash_client, body: bytes = crash_body) -> str:
                        try:
                            response = bound_client.post("/message:send", headers=headers(), content=body)
                            if response.status_code != 200:
                                return record_http_error(
                                    phase="recovery-crash",
                                    task_id="soak-crash-inflight",
                                    operation="submit",
                                    status_code=response.status_code,
                                )
                            time.sleep(0.2)
                            return "submitted"
                        except httpx.HTTPError as error:
                            return record_http_error(
                                phase="recovery-crash",
                                task_id="soak-crash-inflight",
                                operation="submit",
                                error=error,
                            )

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(crash_request)
                        time.sleep(0.1)
                        analysis.kill()
                        analysis.wait(timeout=5)
                        crash_request_result = future.result()
                        account(crash_request_result, phase="recovery-crash", crash_expected=True)
                    client.close()
                    analysis = start_analysis()
                    client = new_client()
                    wait_http_ready(client)
                    try:
                        recovered = client.get(
                            "/tasks/soak-crash-inflight",
                            headers={"A2A-Version": "1.0", "Accept": "application/a2a+json"},
                        )
                        if recovered.status_code == 200:
                            recovered_payload = cast(dict[str, Any], recovered.json())
                        else:
                            account(
                                record_http_error(
                                    phase="recovery-readback",
                                    task_id="soak-crash-inflight",
                                    operation="poll",
                                    status_code=recovered.status_code,
                                ),
                                phase="recovery-readback",
                            )
                            recovered_payload = {}
                    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as error:
                        account(
                            record_http_error(
                                phase="recovery-readback",
                                task_id="soak-crash-inflight",
                                operation="poll",
                                error=error,
                            ),
                            phase="recovery-readback",
                        )
                        recovered_payload = {}
                    recovered_task = cast(dict[str, Any], recovered_payload.get("task", {}))
                    recovered_status = cast(dict[str, Any], recovered_task.get("status", {}))
                    recovered_incomplete = recovered_status.get("state") == "TASK_STATE_FAILED"
                    recovery_probe_state = execute(phase="recovery-probe")
                    account(recovery_probe_state, phase="recovery-probe")
                    crash_recovered = recovered_incomplete and recovery_probe_state == "TASK_STATE_COMPLETED"
                    crashed = True
                account(execute(phase="recovery"), phase="recovery")
                if time.monotonic() >= next_sample:
                    sample("recovery", recovery_started, analysis)
                    next_sample += min(10, max(1, args.phase_seconds / 4))
                remaining = min(0.5, recovery_end - time.monotonic())
                if remaining > 0:
                    time.sleep(remaining)
            phases.append(
                {
                    "phase_id": "recovery",
                    "required_seconds": args.phase_seconds,
                    "elapsed_ms": (time.monotonic() - recovery_started) * 1000,
                    "completed_delta": counters["completed"] - count_before["completed"],
                    "typed_rejected_delta": counters["typed_rejected"] - count_before["typed_rejected"],
                }
            )
            qualified_end_ns = time.monotonic_ns()

            assert analysis is not None and client is not None
            drain_deadline = time.monotonic() + 15
            health: dict[str, Any] = {}
            while time.monotonic() < drain_deadline:
                health = client.get("/health/live").json()
                if health.get("queue_depth") == 0 and health.get("in_flight") == 0:
                    break
                time.sleep(0.1)
            first_samples = [item for item in samples if item["phase"] == "steady"]
            final_samples = [item for item in samples if item["phase"] == "recovery"]
            rss_growth = (final_samples[-1]["rss_bytes"] - first_samples[0]["rss_bytes"]) if first_samples and final_samples else 0
            fd_growth = (final_samples[-1]["fd_count"] - first_samples[0]["fd_count"]) if first_samples and final_samples else 0
            formal = args.warmup_seconds == 60 and args.phase_seconds == 900
            formal_seconds = args.phase_seconds * 4
            qualified_elapsed_ms = (qualified_end_ns - qualified_start_ns) / 1_000_000
            if formal and args.source_tree_digest == "sha256:" + "0" * 64:
                raise RuntimeError("formal soak requires an exact source-tree digest")
            checks = {
                "durations": all(item["elapsed_ms"] >= item["required_seconds"] * 1000 for item in phases),
                "qualified_elapsed": qualified_elapsed_ms >= formal_seconds * 1000,
                "unclassified_errors": counters["unclassified"] == 0,
                "typed_saturation_rejections": counters["typed_rejected"] >= 1,
                "crash_restart_recovered": crash_recovered,
                "recovered_incomplete_fenced": recovered_incomplete,
                "final_queue_depth": health.get("queue_depth") == 0,
                "final_in_flight": health.get("in_flight") == 0,
                "rss_growth": rss_growth <= 67108864,
                "fd_growth": fd_growth <= 32,
            }
            if not all(checks.values()):
                raise RuntimeError(
                    "soak thresholds failed: "
                    f"checks={checks}, counters={counters}, state_counts={state_counts}, "
                    f"unclassified_samples={unclassified_samples}, http_error_details={http_error_details}"
                )
            analysis.send_signal(signal.SIGTERM)
            analysis.wait(timeout=10)
            cleanup = analysis.returncode == 0
            if not cleanup:
                raise RuntimeError("soak cleanup failed")
            claim_scope = {
                "runtime_profile": "analysis-agent-runtime/v1",
                "deployment_tier": "operational-single-domain",
                "provider": "deterministic-fixture-not-real-provider-qualification",
                "a2a": "real-mtls-http-json",
                "mcp": "real-mtls-streamable-http-fixture",
            }
            evidence = {
                "schema_version": "analysis-soak-evidence/v1",
                "module_id": "MOD-AGENT-001",
                "profile_id": "analysis-soak/v1",
                "level": "MODULE" if formal else "REHEARSAL",
                "applicability": "APPLICABLE",
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "source_tree_digest": args.source_tree_digest,
                "profile_digest": profile_digest,
                "claim_scope": claim_scope,
                "claim_scope_digest": canonical_digest(claim_scope),
                "started_at": run_started_at,
                "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "formal": formal,
                "warmup_seconds": args.warmup_seconds,
                "warmup_elapsed_ms": warmup_elapsed_ms,
                "warmup_not_counted": True,
                "formal_seconds": formal_seconds,
                "qualified_start_monotonic_ns": qualified_start_ns,
                "qualified_end_monotonic_ns": qualified_end_ns,
                "qualified_elapsed_ms": qualified_elapsed_ms,
                "sample_interval_seconds": 10 if formal else min(10, max(1, args.phase_seconds / 4)),
                "phases": phases,
                "counters": counters,
                "crash_restart_recovered": crash_recovered,
                "recovered_incomplete_fenced": recovered_incomplete,
                "resource_samples": samples,
                "rss_growth_bytes": rss_growth,
                "fd_growth": fd_growth,
                "final_queue_depth": health.get("queue_depth"),
                "final_in_flight": health.get("in_flight"),
                "binary_digest": file_digest(Path(args.binary).read_bytes()),
                "config_digest": material.config_digest,
                "cleanup": cleanup,
                "process_exit_code": analysis.returncode,
                "checks": checks,
            }
            raw = go_json_bytes(evidence) + b"\n"
            if args.evidence:
                Path(args.evidence).write_bytes(raw)
            print(json.dumps(evidence, sort_keys=True))
        finally:
            if client is not None:
                client.close()
            if analysis is not None and analysis.poll() is None:
                analysis.terminate()
                try:
                    analysis.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    analysis.kill()
                    analysis.wait()
            neighbors.terminate()
            try:
                neighbors.wait(timeout=5)
            except subprocess.TimeoutExpired:
                neighbors.kill()
                neighbors.wait()
            analysis_log.close()
            neighbor_log.close()


if __name__ == "__main__":
    main()
