from __future__ import annotations

import argparse
import json
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


STOP = False


def request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def command(arguments: list[str], *, timeout: float = 10) -> str:
    completed = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout


def inspect(container_id: str) -> dict[str, Any]:
    return json.loads(command(["docker", "inspect", container_id]))[0]


def process_metrics(container_id: str) -> dict[str, int]:
    script = """
printf 'rss_kib='; awk '$1 == "VmRSS:" {print $2}' /proc/1/status
printf 'fd_count='; find /proc/1/fd -mindepth 1 -maxdepth 1 2>/dev/null | wc -l
printf 'thread_count='; find /proc/1/task -mindepth 1 -maxdepth 1 2>/dev/null | wc -l
printf 'oom_events='; awk '$1 == "oom_kill" {print $2}' /sys/fs/cgroup/memory.events
printf 'pids_current='; cat /sys/fs/cgroup/pids.current
printf 'throttled_usec='; awk '$1 == "throttled_usec" {print $2}' /sys/fs/cgroup/cpu.stat
""".strip()
    output = command(["docker", "exec", container_id, "sh", "-c", script])
    parsed: dict[str, int] = {}
    for line in output.splitlines():
        key, value = line.split("=", 1)
        parsed[key] = int(value.strip() or "0")
    parsed["rss_bytes"] = parsed.pop("rss_kib", 0) * 1024
    return parsed


def stats(container_id: str) -> dict[str, object]:
    payload = json.loads(
        command(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                container_id,
            ],
            timeout=15,
        )
    )
    cpu_text = str(payload.get("CPUPerc", "0%")).rstrip("%")
    return {
        "cpu_pct": float(cpu_text or "0"),
        "docker_memory_usage": payload.get("MemUsage"),
        "docker_pids": int(payload.get("PIDs", 0)),
    }


def collect(container_id: str) -> dict[str, object]:
    try:
        metadata = inspect(container_id)
        state = metadata["State"]
        host = metadata["HostConfig"]
        running = bool(state["Running"])
        result: dict[str, object] = {
            "container_id": container_id,
            "running": running,
            "oom_killed": bool(state["OOMKilled"]),
            "restart_count": int(metadata.get("RestartCount", 0)),
            "host_config": {
                "nano_cpus": int(host.get("NanoCpus", 0)),
                "cpuset_cpus": str(host.get("CpusetCpus", "")),
                "memory_bytes": int(host.get("Memory", 0)),
                "pids_limit": int(host.get("PidsLimit", 0)),
            },
        }
        if running:
            try:
                result.update(stats(container_id))
                result.update(process_metrics(container_id))
            except BaseException:
                # The workload can exit between the initial inspect and either
                # stats / exec call.  Re-read state so a normal terminal sample
                # is not misclassified as a resource collection failure.
                final_metadata = inspect(container_id)
                final_state = final_metadata["State"]
                if bool(final_state["Running"]):
                    raise
                result["running"] = False
                result["oom_killed"] = bool(final_state["OOMKilled"])
                result["restart_count"] = int(final_metadata.get("RestartCount", 0))
                result["sampling_transition"] = "stopped_during_sample"
        return result
    except BaseException as exc:
        return {
            "container_id": container_id,
            "running": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--switch-id", required=True)
    parser.add_argument("--runner-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, required=True)
    parser.add_argument("--max-samples", type=int, required=True)
    args = parser.parse_args()
    if args.interval_seconds <= 0 or args.max_samples <= 0:
        raise ValueError("sampling bounds must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    started_ns = time.perf_counter_ns()
    next_sample_ns = started_ns
    with args.output.open("w", encoding="utf-8") as stream:
        for sequence in range(args.max_samples):
            if STOP:
                break
            remaining_ns = next_sample_ns - time.perf_counter_ns()
            if remaining_ns > 0:
                time.sleep(remaining_ns / 1e9)
            sample = {
                "sequence": sequence,
                "wall_time_ns": time.time_ns(),
                "monotonic_ns": time.perf_counter_ns(),
                "switch": collect(args.switch_id),
                "runner": collect(args.runner_id),
            }
            stream.write(json.dumps(sample, sort_keys=True) + "\n")
            stream.flush()
            runner = sample["runner"]
            if sequence > 0 and isinstance(runner, dict) and not runner.get("running"):
                break
            next_sample_ns = started_ns + int(
                (sequence + 1) * args.interval_seconds * 1e9
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
