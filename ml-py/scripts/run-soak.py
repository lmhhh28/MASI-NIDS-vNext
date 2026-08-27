#!/usr/bin/env python3
"""Exact-duration Offline ML repeated-pipeline soak with recovery and resource evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import statistics
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


def process_resources(pid: int) -> tuple[int, int]:
    rss = 0
    fds = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
                break
        fds = len(list(Path(f"/proc/{pid}/fd").iterdir()))
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
    return rss, fds


def remove_tree(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    for root, directories, files in os.walk(path, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            target = root_path / name
            if not target.is_symlink():
                os.chmod(target, 0o600)
            target.unlink()
        for name in directories:
            target = root_path / name
            if target.is_symlink():
                target.unlink()
            else:
                os.chmod(target, 0o700)
                target.rmdir()
    os.chmod(path, 0o700)
    path.rmdir()


def wait_for_staging(parent: Path, output_name: str, timeout: float = 10.0) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = list(parent.glob(f".{output_name}.staging-*"))
        if matches:
            return matches[0]
        time.sleep(0.02)
    raise RuntimeError("staging directory did not appear")


def launch_pipeline(entrypoint: Path, repo: Path, output: Path) -> tuple[dict[str, Any], dict[str, Any]]:
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
        time.sleep(0.05)
    stdout = process.communicate()[0]
    elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
    if process.returncode != 0:
        raise RuntimeError(f"pipeline exited {process.returncode}: {stdout}")
    result = cast(dict[str, Any], json.loads(stdout.strip().splitlines()[-1]))
    verification = subprocess.run(
        [str(entrypoint), "verify", "--repo", str(repo), "--output", str(output)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if verification.returncode != 0:
        raise RuntimeError(verification.stdout)
    verified = cast(dict[str, Any], json.loads(verification.stdout.strip().splitlines()[-1]))
    observation = {
        "elapsed_ms": elapsed_ms,
        "peak_rss_bytes": peak_rss,
        "peak_file_descriptors": peak_fds,
        "resource_samples": samples,
        "recovered_staging_directories": cast(dict[str, Any], result["runtime"])["recovered_staging_directories"],
    }
    return result, {"verification": verified, "observation": observation}


def identity(result: dict[str, Any]) -> dict[str, str]:
    return {
        "dataset_revision": str(result["dataset_revision"]),
        "model_digest": str(cast(dict[str, Any], result["bundle"])["model_digest"]),
        "repository_closure_digest": str(cast(dict[str, Any], result["bundle"])["repository_closure_digest"]),
        "archive_digest": str(cast(dict[str, Any], result["archive"])["sha256"]),
    }


def recovery_faults(entrypoint: Path, repo: Path, root: Path) -> dict[str, Any]:
    term_output = root / "recovery-term"
    process = subprocess.Popen(
        [str(entrypoint), "run", "--repo", str(repo), "--output", str(term_output)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    wait_for_staging(root, term_output.name)
    process.send_signal(signal.SIGTERM)
    term_stdout = process.communicate(timeout=30)[0]
    term_ok = process.returncode == 130 and not term_output.exists() and not list(root.glob(".recovery-term.staging-*"))
    if not term_ok:
        raise RuntimeError(f"SIGTERM recovery failed: {process.returncode}: {term_stdout}")

    kill_output = root / "recovery-kill"
    process = subprocess.Popen(
        [str(entrypoint), "run", "--repo", str(repo), "--output", str(kill_output)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    wait_for_staging(root, kill_output.name)
    os.kill(process.pid, signal.SIGKILL)
    process.communicate(timeout=30)
    fenced = process.returncode == -signal.SIGKILL and not kill_output.exists()
    if not fenced or len(list(root.glob(".recovery-kill.staging-*"))) != 1:
        raise RuntimeError("SIGKILL fencing failed")
    recovered, details = launch_pipeline(entrypoint, repo, kill_output)
    recovered_count = cast(dict[str, Any], details["observation"])["recovered_staging_directories"]
    if recovered_count != 1 or list(root.glob(".recovery-kill.staging-*")):
        raise RuntimeError("SIGKILL staging recovery failed")
    recovered_identity = identity(recovered)
    remove_tree(kill_output)
    return {
        "sigterm_exit_code": 130,
        "sigterm_cleanup": True,
        "sigkill_exit_code": -signal.SIGKILL,
        "sigkill_fenced": True,
        "staging_recovered": 1,
        "recovered_identity": recovered_identity,
        "result": "PASS",
    }


def phase_run(
    entrypoint: Path,
    repo: Path,
    root: Path,
    *,
    name: str,
    seconds: int,
    interval: int,
    reference: dict[str, str] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    phase_started_ns = time.monotonic_ns()
    deadline_ns = phase_started_ns + seconds * 1_000_000_000
    next_launch_ns = phase_started_ns
    runs: list[dict[str, Any]] = []
    run_index = 0
    while True:
        now_ns = time.monotonic_ns()
        remaining_seconds = (deadline_ns - now_ns) / 1_000_000_000
        if remaining_seconds <= 0:
            break
        if now_ns < next_launch_ns or remaining_seconds < 8.0:
            time.sleep(min(0.25, max(0.0, remaining_seconds)))
            continue
        output = root / f"{name}-{run_index:04d}"
        result, details = launch_pipeline(entrypoint, repo, output)
        observed_identity = identity(result)
        if reference is None:
            reference = observed_identity
        if observed_identity != reference:
            raise RuntimeError(f"artifact identity drift in {name}: {observed_identity} != {reference}")
        observation = cast(dict[str, Any], details["observation"])
        runs.append(observation)
        remove_tree(output)
        run_index += 1
        next_launch_ns += interval * 1_000_000_000
    observed_elapsed_ms = (time.monotonic_ns() - phase_started_ns) / 1_000_000
    if reference is None:
        raise RuntimeError(f"phase {name} completed without a real pipeline run")
    return (
        {
            "name": name,
            "configured_seconds": seconds,
            "observed_elapsed_ms": observed_elapsed_ms,
            "successful_runs": len(runs),
            "errors": 0,
            "repository_digest_drifts": 0,
            "peak_rss_bytes": max((int(run["peak_rss_bytes"]) for run in runs), default=0),
            "peak_file_descriptors": max((int(run["peak_file_descriptors"]) for run in runs), default=0),
            "resource_samples": sum(int(run["resource_samples"]) for run in runs),
            "pipeline_elapsed_ms": [float(run["elapsed_ms"]) for run in runs],
        },
        reference,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--entrypoint", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--mode", choices=("formal", "rehearsal"), required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    entrypoint = arguments.entrypoint.resolve(strict=True)
    if arguments.evidence.exists() or arguments.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    profile_path = repo / "contracts/profiles/v1/offline-ml-soak.json"
    profile = cast(dict[str, Any], json.loads(profile_path.read_text(encoding="utf-8")))
    if arguments.mode == "formal":
        warmup_seconds = int(profile["warmup_seconds"])
        phase_specs = [
            (str(item["name"]), int(item["seconds"]), int(item["launch_interval_seconds"]))
            for item in cast(list[dict[str, Any]], profile["phases"])
        ]
    else:
        warmup_seconds = 16
        phase_specs = [(name, 16, 8) for name in ("steady", "peak", "saturation", "recovery")]

    source_digest_start = subprocess.run(
        [str(entrypoint.parent / "python"), str(repo / "ml-py/scripts/source-tree-digest.py"), "--repo", str(repo)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    temporary = Path(tempfile.mkdtemp(prefix="masi-offline-ml-soak."))
    overall_started_ns = time.monotonic_ns()
    reference: dict[str, str] | None = None
    try:
        warmup, reference = phase_run(
            entrypoint,
            repo,
            temporary,
            name="warmup",
            seconds=warmup_seconds,
            interval=8,
            reference=reference,
        )
        recovery = recovery_faults(entrypoint, repo, temporary)
        if reference != cast(dict[str, str], recovery["recovered_identity"]):
            raise RuntimeError("recovery artifact identity drift")
        phases: list[dict[str, Any]] = []
        qualified_started_ns = time.monotonic_ns()
        for name, seconds, interval in phase_specs:
            phase, reference = phase_run(
                entrypoint,
                repo,
                temporary,
                name=name,
                seconds=seconds,
                interval=interval,
                reference=reference,
            )
            phases.append(phase)
        observed_qualified_ms = (time.monotonic_ns() - qualified_started_ns) / 1_000_000
        source_digest_end = subprocess.run(
            [str(entrypoint.parent / "python"), str(repo / "ml-py/scripts/source-tree-digest.py"), "--repo", str(repo)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        successes = sum(int(phase["successful_runs"]) for phase in phases)
        rss_values = [int(phase["peak_rss_bytes"]) for phase in phases]
        first_half_rss = statistics.mean(rss_values[:2])
        last_half_rss = statistics.mean(rss_values[2:])
        formal = arguments.mode == "formal"
        configured_qualified_seconds = sum(seconds for _, seconds, _ in phase_specs)
        checks = {
            "source_tree_stable": source_digest_start == source_digest_end,
            "phase_order_exact": [phase["name"] for phase in phases] == ["steady", "peak", "saturation", "recovery"],
            "configured_duration_exact": configured_qualified_seconds == (3600 if formal else 64),
            "observed_duration_complete": observed_qualified_ms >= configured_qualified_seconds * 1000,
            "successful_runs": successes >= (120 if formal else 4),
            "unexpected_errors": all(phase["errors"] == 0 for phase in phases),
            "repository_digest_stable": all(phase["repository_digest_drifts"] == 0 for phase in phases),
            "rss_limit": max(rss_values) <= 2147483648,
            "fd_limit": max(int(phase["peak_file_descriptors"]) for phase in phases) <= 256,
            "rss_growth": last_half_rss - first_half_rss <= 134217728,
            "sigterm_cleanup": recovery["sigterm_cleanup"] is True,
            "sigkill_recovery": recovery["staging_recovered"] == 1,
            "final_staging_directories": not list(temporary.glob(".*.staging-*")),
        }
        evidence = {
            "schema_version": "offline-ml-soak-evidence/v1",
            "module_id": "MOD-ML-001",
            "level": "MODULE" if formal else "REHEARSAL",
            "applicability": "APPLICABLE",
            "result": "PASS" if all(checks.values()) else "FAIL",
            "qualification": "NOT_QUALIFIED",
            "mode": arguments.mode,
            "profile_digest": sha256(profile_path),
            "warmup": warmup,
            "configured_qualified_seconds": configured_qualified_seconds,
            "observed_qualified_elapsed_ms": observed_qualified_ms,
            "overall_elapsed_ms": (time.monotonic_ns() - overall_started_ns) / 1_000_000,
            "phases": phases,
            "successful_pipeline_runs": successes,
            "recovery": recovery,
            "artifact_identity": reference,
            "source_tree_digest_start": "sha256:" + source_digest_start,
            "source_tree_digest_end": "sha256:" + source_digest_end,
            "resource_trend": {
                "first_half_peak_rss_mean": first_half_rss,
                "last_half_peak_rss_mean": last_half_rss,
                "growth_bytes": last_half_rss - first_half_rss,
            },
            "checks": checks,
            "cleanup_complete": True,
        }
        arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
        arguments.evidence.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, sort_keys=True))
        return 0 if evidence["result"] == "PASS" else 1
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
