#!/usr/bin/env python3
"""Measure the actual release A2A process at normal, concurrent, saturated, and recovered load."""

from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import json
import os
import platform
import runpy
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from masi_analysis.canonical import canonical_digest, file_digest, go_json_bytes


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def process_resources(pid: int) -> dict[str, int | float]:
    rss_bytes = 0
    threads = 0
    voluntary = 0
    involuntary = 0
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            rss_bytes = int(line.split()[1]) * 1024
        elif line.startswith("Threads:"):
            threads = int(line.split()[1])
        elif line.startswith("voluntary_ctxt_switches:"):
            voluntary = int(line.split()[1])
        elif line.startswith("nonvoluntary_ctxt_switches:"):
            involuntary = int(line.split()[1])
    stat_fields = Path(f"/proc/{pid}/stat").read_text().split()
    ticks = os.sysconf("SC_CLK_TCK")
    io_values: dict[str, int] = {}
    for line in Path(f"/proc/{pid}/io").read_text().splitlines():
        key, value = line.split(":", 1)
        io_values[key] = int(value.strip())
    return {
        "rss_bytes": rss_bytes,
        "fd_count": len(list(Path(f"/proc/{pid}/fd").iterdir())),
        "thread_count": threads,
        "cpu_user_seconds": int(stat_fields[13]) / ticks,
        "cpu_system_seconds": int(stat_fields[14]) / ticks,
        "voluntary_context_switches": voluntary,
        "involuntary_context_switches": involuntary,
        "io_read_bytes": io_values.get("read_bytes", 0),
        "io_write_bytes": io_values.get("write_bytes", 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    module_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--binary", default=str(module_root / ".venv/bin/masi-analysis"))
    parser.add_argument("--evidence")
    parser.add_argument("--source-tree-digest", default="sha256:" + "0" * 64)
    parser.add_argument("--working-tree-status-digest", default="sha256:" + "0" * 64)
    args = parser.parse_args()
    repository = module_root.parent
    profile_path = repository / "contracts/profiles/v1/analysis-performance.json"
    profile_digest = file_digest(profile_path.read_bytes())
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    support = runpy.run_path(str(module_root / "scripts/run-blackbox.py"), run_name="analysis_blackbox_support")
    free_port = cast(Callable[[], int], support["free_port"])
    generate_pki = cast(Callable[[Path], dict[str, str]], support["generate_pki"])
    httpx_context = support["httpx_context"]
    exact_input = support["exact_input"]
    a2a_body = support["a2a_body"]
    headers = support["headers"]
    wait_ready = support["wait_ready"]
    write_runtime = support["write_runtime"]
    with tempfile.TemporaryDirectory(prefix="masi-analysis-performance-") as temporary:
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
            limits_override={"queue_depth": 64, "concurrency": 16, "max_tasks": 10000, "task_retention_seconds": 3600},
        )
        material = support["load_runtime_material"](str(config_path))
        neighbors_log = (root / "neighbors.log").open("wb")
        analysis_log = (root / "analysis.log").open("wb")
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
            time.sleep(0.2)
            if neighbors.poll() is not None:
                raise RuntimeError("performance neighbors failed startup")
            analysis = subprocess.Popen([args.binary, "--config", str(config_path)], stdout=analysis_log, stderr=subprocess.STDOUT)
            wait_ready(args.binary, config_path, analysis)
            client = httpx.Client(
                base_url=f"https://localhost:{service_port}",
                verify=httpx_context(pki["ca"], pki["control_cert"], pki["control_key"]),
                trust_env=False,
                timeout=10,
                limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
            )
            sequence = itertools.count(1)
            wire_bytes = {"request": 0, "response": 0}
            wire_lock = threading.Lock()

            def execute(content: str = "performance") -> tuple[str, float]:
                task_id = f"perf-{next(sequence)}"
                input_bundle = exact_input(material, task_id=task_id, content=content)
                started = time.monotonic()
                body = a2a_body(input_bundle)
                response = client.post("/message:send", headers=headers(), content=body)
                with wire_lock:
                    wire_bytes["request"] += len(body)
                    wire_bytes["response"] += len(response.content)
                if response.status_code != 200:
                    return "http_error", (time.monotonic() - started) * 1000
                task = response.json()["task"]
                deadline = time.monotonic() + 9
                while task["status"]["state"] in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    polled = client.get(f"/tasks/{task['id']}", headers={"A2A-Version": "1.0"})
                    with wire_lock:
                        wire_bytes["response"] += len(polled.content)
                    task = polled.json()["task"]
                return str(task["status"]["state"]), (time.monotonic() - started) * 1000

            for _ in range(10):
                state, _ = execute()
                if state != "TASK_STATE_COMPLETED":
                    raise RuntimeError("performance warmup failed")

            trials: list[dict[str, Any]] = []
            normal_errors = 0

            def execute_normal(_index: int) -> tuple[str, float]:
                return execute()

            def execute_saturated(_index: int) -> tuple[str, float]:
                return execute("provider-delay-200ms")

            for concurrency in (1, 4, 16):
                for trial in range(1, 4):
                    started = time.monotonic()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                        results = list(executor.map(execute_normal, range(20)))
                    elapsed = time.monotonic() - started
                    latencies = [latency for _, latency in results]
                    errors = sum(state != "TASK_STATE_COMPLETED" for state, _ in results)
                    normal_errors += errors
                    trials.append(
                        {
                            "concurrency": concurrency,
                            "trial": trial,
                            "tasks": len(results),
                            "errors": errors,
                            "elapsed_ms": elapsed * 1000,
                            "tasks_per_second": len(results) / elapsed,
                            "p50_ms": percentile(latencies, 0.50),
                            "p95_ms": percentile(latencies, 0.95),
                            "p99_ms": percentile(latencies, 0.99),
                            "max_ms": max(latencies),
                        }
                    )
            saturation_started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=128) as executor:
                saturation = list(executor.map(execute_saturated, range(128)))
            saturation_elapsed = time.monotonic() - saturation_started
            typed_rejections = sum(state == "TASK_STATE_REJECTED" for state, _ in saturation)
            recovery_state, recovery_ms = execute()
            resources = process_resources(analysis.pid)
            all_normal_p95 = max(trial["p95_ms"] for trial in trials)
            concurrency_16_tps = statistics.median(trial["tasks_per_second"] for trial in trials if trial["concurrency"] == 16)
            checks = {
                "normal_terminal_error_count": normal_errors == 0,
                "normal_p95_ms": all_normal_p95 <= 1000,
                "concurrency_16_tasks_per_second": concurrency_16_tps >= 20,
                "saturation_typed_rejections": typed_rejections >= 1,
                "recovery_task_ms": recovery_state == "TASK_STATE_COMPLETED" and recovery_ms <= 1000,
                "rss_bytes": int(resources["rss_bytes"]) <= 402653184,
                "fd_count": int(resources["fd_count"]) <= 512,
            }
            if not all(checks.values()):
                raise RuntimeError(f"performance thresholds failed: {checks}")
            analysis.send_signal(signal.SIGTERM)
            analysis.wait(timeout=5)
            cleanup = analysis.returncode == 0
            if not cleanup:
                raise RuntimeError("performance release process did not cleanly stop")
            claim_scope = {
                "runtime_profile": "analysis-agent-runtime/v1",
                "deployment_tier": "operational-single-domain",
                "provider": "deterministic-fixture-not-real-provider-qualification",
            }
            evidence = {
                "schema_version": "analysis-performance-evidence/v1",
                "module_id": "MOD-AGENT-001",
                "profile_id": "analysis-performance/v1",
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "source_tree_digest": args.source_tree_digest,
                "working_tree_status_digest": args.working_tree_status_digest,
                "profile_digest": profile_digest,
                "config_digest": material.config_digest,
                "claim_scope": claim_scope,
                "claim_scope_digest": canonical_digest(claim_scope),
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "python_version": platform.python_version(),
                "kernel": platform.release(),
                "machine": platform.machine(),
                "cpu_count": os.cpu_count(),
                "binary_digest": file_digest(Path(args.binary).read_bytes()),
                "trials": trials,
                "normal_error_count": normal_errors,
                "normal_task_count": sum(int(trial["tasks"]) for trial in trials),
                "normal_error_rate": normal_errors / sum(int(trial["tasks"]) for trial in trials),
                "normal_p95_ms_max": all_normal_p95,
                "concurrency_16_tps_median": concurrency_16_tps,
                "saturation_tasks": len(saturation),
                "saturation_elapsed_ms": saturation_elapsed * 1000,
                "saturation_typed_rejections": typed_rejections,
                "recovery_ms": recovery_ms,
                "wire_request_bytes": wire_bytes["request"],
                "wire_response_bytes": wire_bytes["response"],
                "resources": resources,
                "cleanup": cleanup,
                "process_exit_code": analysis.returncode,
                "checks": checks,
            }
            raw = go_json_bytes(evidence) + b"\n"
            if args.evidence:
                Path(args.evidence).write_bytes(raw)
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
            analysis_log.close()
            neighbors_log.close()


if __name__ == "__main__":
    main()
