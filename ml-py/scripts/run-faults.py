#!/usr/bin/env python3
"""Exercise fail-closed archive, tamper, interruption, and atomic-publication behavior."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from masi_offline_ml.canonical import ValidationError
from masi_offline_ml.pipeline import verify_repository_archive


def run(entrypoint: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(entrypoint), *arguments], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
    )


def wait_for_staging(parent: Path, output_name: str, timeout: float = 10.0) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = list(parent.glob(f".{output_name}.staging-*"))
        if matches:
            return matches[0]
        time.sleep(0.02)
    raise RuntimeError("staging directory did not appear")


def malicious_archive(path: Path, *, name: str, kind: str) -> None:
    with tarfile.open(path, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(name)
        info.uid = 0
        info.gid = 0
        info.mtime = 1787334400
        info.mode = 0o440 if kind != "executable" else 0o550
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        else:
            payload = b"x"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def expect_archive_rejection(path: Path, code: str) -> None:
    try:
        verify_repository_archive(path)
    except ValidationError as error:
        if error.code != code:
            raise RuntimeError(f"{error.code} != {code}") from error
    else:
        raise RuntimeError(f"malicious archive accepted: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--entrypoint", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    entrypoint = arguments.entrypoint.resolve(strict=True)
    if arguments.evidence.exists() or arguments.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    temporary = Path(tempfile.mkdtemp(prefix="masi-offline-ml-faults."))
    scenarios: dict[str, dict[str, Any]] = {}
    try:
        baseline = temporary / "baseline"
        baseline_run = run(entrypoint, "run", "--repo", str(repo), "--output", str(baseline))
        if baseline_run.returncode != 0:
            raise RuntimeError(baseline_run.stdout)
        baseline_result = cast(dict[str, Any], json.loads(baseline_run.stdout.strip().splitlines()[-1]))
        scenarios["baseline-real-pipeline"] = {"result": "PASS", "exit_code": 0}

        marker = baseline / "user-marker"
        marker.write_text("preserve", encoding="utf-8")
        existing = run(entrypoint, "run", "--repo", str(repo), "--output", str(baseline))
        if existing.returncode != 2 or marker.read_text(encoding="utf-8") != "preserve":
            raise RuntimeError("existing output was not protected")
        scenarios["existing-output-no-overwrite"] = {"result": "PASS", "exit_code": existing.returncode}

        metrics = next((baseline / "candidates").glob("*/seed-17/metrics.json"))
        original_metrics = metrics.read_bytes()
        metrics.write_bytes(original_metrics + b" ")
        tampered = run(entrypoint, "verify", "--repo", str(repo), "--output", str(baseline))
        if tampered.returncode != 2 or "candidate_file_digest" not in tampered.stdout:
            raise RuntimeError("candidate tamper was not rejected")
        metrics.write_bytes(original_metrics)
        scenarios["candidate-artifact-tamper"] = {"result": "PASS", "exit_code": tampered.returncode}

        traversal = temporary / "traversal.tar"
        malicious_archive(traversal, name="../escape", kind="file")
        expect_archive_rejection(traversal, "repository_archive_path")
        scenarios["archive-path-traversal"] = {"result": "PASS"}
        symlink = temporary / "symlink.tar"
        malicious_archive(symlink, name="model.onnx", kind="symlink")
        expect_archive_rejection(symlink, "repository_archive_type")
        scenarios["archive-symlink"] = {"result": "PASS"}
        executable = temporary / "executable.tar"
        malicious_archive(executable, name="model.onnx", kind="executable")
        expect_archive_rejection(executable, "repository_archive_metadata")
        scenarios["archive-executable"] = {"result": "PASS"}

        symlink_target = temporary / "symlink-target"
        symlink_target.mkdir()
        symlink_output = temporary / "symlink-output"
        symlink_output.symlink_to(symlink_target, target_is_directory=True)
        linked = run(entrypoint, "run", "--repo", str(repo), "--output", str(symlink_output))
        if linked.returncode != 2 or list(symlink_target.iterdir()):
            raise RuntimeError("symlink output was not rejected")
        scenarios["output-symlink"] = {"result": "PASS", "exit_code": linked.returncode}

        term_output = temporary / "term-output"
        term_process = subprocess.Popen(
            [str(entrypoint), "run", "--repo", str(repo), "--output", str(term_output)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        wait_for_staging(temporary, term_output.name)
        term_process.send_signal(signal.SIGTERM)
        term_stdout = term_process.communicate(timeout=30)[0]
        if term_process.returncode != 130 or term_output.exists() or list(temporary.glob(".term-output.staging-*")):
            raise RuntimeError(f"SIGTERM cleanup failed: {term_process.returncode}: {term_stdout}")
        scenarios["sigterm-cleanup"] = {"result": "PASS", "exit_code": term_process.returncode}

        kill_output = temporary / "kill-output"
        kill_process = subprocess.Popen(
            [str(entrypoint), "run", "--repo", str(repo), "--output", str(kill_output)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        wait_for_staging(temporary, kill_output.name)
        os.kill(kill_process.pid, signal.SIGKILL)
        kill_process.communicate(timeout=30)
        if kill_process.returncode != -signal.SIGKILL or kill_output.exists():
            raise RuntimeError("SIGKILL atomic publication failed")
        abandoned = list(temporary.glob(".kill-output.staging-*"))
        if len(abandoned) != 1:
            raise RuntimeError("SIGKILL did not leave one fenced staging directory")
        recovered = run(entrypoint, "run", "--repo", str(repo), "--output", str(kill_output))
        if recovered.returncode != 0 or list(temporary.glob(".kill-output.staging-*")):
            raise RuntimeError(f"stale staging recovery failed: {recovered.stdout}")
        recovered_result = cast(dict[str, Any], json.loads(recovered.stdout.strip().splitlines()[-1]))
        if cast(dict[str, Any], recovered_result["runtime"])["recovered_staging_directories"] != 1:
            raise RuntimeError("stale staging recovery count mismatch")
        scenarios["sigkill-fenced-staging-recovery"] = {
            "result": "PASS",
            "killed_exit_code": kill_process.returncode,
            "recovered": 1,
        }

        expected = {
            "model_digest": cast(dict[str, Any], baseline_result["bundle"])["model_digest"],
            "repository_closure_digest": cast(dict[str, Any], baseline_result["bundle"])["repository_closure_digest"],
            "archive_digest": cast(dict[str, Any], baseline_result["archive"])["sha256"],
        }
        recovered_binding = {
            "model_digest": cast(dict[str, Any], recovered_result["bundle"])["model_digest"],
            "repository_closure_digest": cast(dict[str, Any], recovered_result["bundle"])["repository_closure_digest"],
            "archive_digest": cast(dict[str, Any], recovered_result["archive"])["sha256"],
        }
        if recovered_binding != expected:
            raise RuntimeError("recovered pipeline artifact identity drift")
        evidence = {
            "schema_version": "offline-ml-fault-evidence/v1",
            "module_id": "MOD-ML-001",
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "scenarios": scenarios,
            "scenario_count": len(scenarios),
            "unexpected_errors": 0,
            "atomic_publication": True,
            "cleanup_complete": True,
            "artifact_identity": expected,
        }
        arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
        arguments.evidence.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, sort_keys=True))
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
