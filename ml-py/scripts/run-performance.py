#!/usr/bin/env python3
"""Measure complete real pipeline latency, resources, output bounds, and determinism."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, cast


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def process_resources(pid: int) -> tuple[int, int]:
    rss_bytes = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                rss_bytes = int(line.split()[1]) * 1024
                break
        file_descriptors = len(list(Path(f"/proc/{pid}/fd").iterdir()))
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        file_descriptors = 0
    return rss_bytes, file_descriptors


def directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def run_once(entrypoint: Path, repo: Path, output: Path) -> dict[str, Any]:
    started = time.monotonic_ns()
    process = subprocess.Popen(
        [str(entrypoint), "run", "--repo", str(repo), "--output", str(output)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    peak_rss = 0
    peak_fds = 0
    samples = 0
    while process.poll() is None:
        rss, fds = process_resources(process.pid)
        peak_rss = max(peak_rss, rss)
        peak_fds = max(peak_fds, fds)
        samples += 1
        time.sleep(0.02)
    stdout = process.communicate()[0]
    elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
    if process.returncode != 0:
        raise RuntimeError(f"pipeline exited {process.returncode}: {stdout}")
    result = cast(dict[str, Any], json.loads(stdout.strip().splitlines()[-1]))
    verification = subprocess.run(
        [str(entrypoint), "verify", "--repo", str(repo), "--output", str(output)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    verified = cast(dict[str, Any], json.loads(verification.stdout.strip().splitlines()[-1]))
    return {
        "elapsed_ms": elapsed_ms,
        "peak_rss_bytes": peak_rss,
        "peak_file_descriptors": peak_fds,
        "resource_samples": samples,
        "output_bytes": directory_bytes(output),
        "archive_bytes": int(cast(dict[str, Any], result["archive"])["bytes"]),
        "dataset_revision": result["dataset_revision"],
        "model_digest": cast(dict[str, Any], result["bundle"])["model_digest"],
        "repository_closure_digest": cast(dict[str, Any], result["bundle"])["repository_closure_digest"],
        "archive_digest": cast(dict[str, Any], result["archive"])["sha256"],
        "candidate_executions": result["candidate_executions"],
        "verification_result": verified["result"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--entrypoint", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    entrypoint = arguments.entrypoint.resolve(strict=True)
    if arguments.evidence.exists() or arguments.evidence.is_symlink() or arguments.runs != 5:
        raise SystemExit("formal performance requires a fresh evidence path and exactly five runs")
    profile_path = repo / "contracts/profiles/v1/offline-ml-performance.json"
    profile = cast(dict[str, Any], json.loads(profile_path.read_text(encoding="utf-8")))
    limits = cast(dict[str, int | float], profile["limits"])

    temporary = Path(tempfile.mkdtemp(prefix="masi-offline-ml-performance."))
    try:
        runs = [run_once(entrypoint, repo, temporary / f"run-{index}") for index in range(arguments.runs)]
        elapsed = [float(run["elapsed_ms"]) for run in runs]
        p95 = percentile(elapsed, 0.95)
        p99 = percentile(elapsed, 0.99)
        throughput = 2560 / (p95 / 1000)
        checks = {
            "pipeline_p95": p95 <= float(limits["pipeline_p95_ms_max"]),
            "pipeline_p99": p99 <= float(limits["pipeline_p99_ms_max"]),
            "records_per_second": throughput >= float(limits["records_per_second_min"]),
            "peak_rss": max(int(run["peak_rss_bytes"]) for run in runs) <= int(limits["peak_rss_bytes_max"]),
            "peak_file_descriptors": max(int(run["peak_file_descriptors"]) for run in runs)
            <= int(limits["peak_file_descriptors_max"]),
            "output_bytes": max(int(run["output_bytes"]) for run in runs) <= int(limits["output_bytes_max"]),
            "archive_bytes": max(int(run["archive_bytes"]) for run in runs)
            <= int(limits["repository_archive_bytes_max"]),
            "candidate_execution_count": all(run["candidate_executions"] == 9 for run in runs),
            "verification": all(run["verification_result"] == "PASS" for run in runs),
            "deterministic_dataset": len({str(run["dataset_revision"]) for run in runs}) == 1,
            "deterministic_model": len({str(run["model_digest"]) for run in runs}) == 1,
            "deterministic_repository": len({str(run["repository_closure_digest"]) for run in runs}) == 1,
            "deterministic_archive": len({str(run["archive_digest"]) for run in runs}) == 1,
        }
        evidence = {
            "schema_version": "offline-ml-performance-evidence/v1",
            "module_id": "MOD-ML-001",
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS" if all(checks.values()) else "FAIL",
            "qualification": "NOT_QUALIFIED",
            "profile_digest": sha256(profile_path),
            "entrypoint": str(entrypoint),
            "runs": runs,
            "summary": {
                "pipeline_p50_ms": percentile(elapsed, 0.50),
                "pipeline_p95_ms": p95,
                "pipeline_p99_ms": p99,
                "records_per_second_at_p95": throughput,
                "peak_rss_bytes": max(int(run["peak_rss_bytes"]) for run in runs),
                "peak_file_descriptors": max(int(run["peak_file_descriptors"]) for run in runs),
                "max_output_bytes": max(int(run["output_bytes"]) for run in runs),
                "max_archive_bytes": max(int(run["archive_bytes"]) for run in runs),
                "errors": 0,
            },
            "checks": checks,
        }
        arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
        arguments.evidence.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, sort_keys=True))
        return 0 if evidence["result"] == "PASS" else 1
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
