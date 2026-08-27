#!/usr/bin/env python3
"""Run the full current-source Offline ML formal Module Complete gate."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def write_json(path: Path, value: object, *, replace: bool = False) -> None:
    if not replace and (path.exists() or path.is_symlink()):
        raise ValueError(f"output exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"temporary output exists: {temporary}")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class GateFailure(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument(
        "--stop-before-soak",
        action="store_true",
        help="run all pre-soak gates as an explicitly incomplete diagnostic run",
    )
    arguments = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,127}", arguments.run_id):
        raise SystemExit("invalid run ID")
    module = Path(__file__).resolve().parents[1]
    repo = module.parent
    run_dir = module / "evidence/module-gates/runs" / arguments.run_id
    if run_dir.exists() or run_dir.is_symlink():
        raise SystemExit(f"run directory exists: {run_dir}")
    run_dir.mkdir(parents=True)

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    source_revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()
    source_digest = (
        "sha256:"
        + subprocess.run(
            [str(module / ".venv/bin/python"), str(module / "scripts/source-tree-digest.py"), "--repo", str(repo)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
    )
    status_digest = digest_bytes(status)
    working_dirty = bool(status)
    (run_dir / "working-tree-status.txt").write_bytes(status)
    metadata = {
        "schema_version": "offline-ml-module-run-metadata/v1",
        "run_id": arguments.run_id,
        "started_at": utc_now(),
        "source_revision": source_revision,
        "source_tree_digest": source_digest,
        "working_tree_status_digest": status_digest,
        "working_tree_dirty": working_dirty,
        "soak_mode": "formal",
    }
    write_json(run_dir / "run-metadata.json", metadata)
    command_schema = json.loads((repo / "contracts/evidence/command/v1/schema.json").read_text(encoding="utf-8"))
    environment = dict(os.environ)
    environment.update(
        {
            "MASI_ML_WORKING_TREE_STATUS_DIGEST": status_digest,
            "MASI_ML_WORKING_TREE_DIRTY": "true" if working_dirty else "false",
            "MASI_ML_PYTHON": str(module / ".venv/bin/python"),
        }
    )
    image_tag = re.sub(r"[^a-z0-9_.-]+", "-", arguments.run_id.lower())
    image_ref = f"masi-offline-ml:{image_tag}"

    def run_gate(command_id: str, argv: list[str], *, extra_environment: dict[str, str] | None = None) -> None:
        print(f"[offline-ml gate] START {command_id}", flush=True)
        started_at = utc_now()
        started_ns = time.monotonic_ns()
        log_path = run_dir / f"{command_id}.log"
        command_environment = dict(environment)
        if extra_environment:
            command_environment.update(extra_environment)
        with log_path.open("wb") as log:
            process = subprocess.run(
                argv,
                cwd=module,
                env=command_environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        finished_at = utc_now()
        duration_ms = max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
        if process.returncode == 0:
            result = "PASS"
            qualification = "QUALIFIED"
            stable_reason: str | None = None
            exit_code: int | None = 0
        elif process.returncode == 2:
            result = "HOLD"
            qualification = "NOT_QUALIFIED"
            stable_reason = "COMMAND_EXITED_HOLD"
            exit_code = 2
        else:
            result = "FAIL"
            qualification = "NOT_QUALIFIED"
            stable_reason = "COMMAND_EXITED_FAILURE"
            exit_code = min(255, max(1, process.returncode))
            if exit_code == 2:
                exit_code = 1
        sidecar = {
            "schema_version": "edge-command-execution/v1",
            "run_id": arguments.run_id,
            "command_id": command_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "working_directory": "ml-py",
            "argv": argv,
            "exit_code": exit_code,
            "result": result,
            "qualification": qualification,
            "source_tree_digest": source_digest,
            "working_tree_status_digest": status_digest,
            "stable_reason": stable_reason,
            "log": {
                "path": log_path.name,
                "sha256": digest(log_path),
                "bytes": log_path.stat().st_size,
                "media_type": "text/plain",
            },
        }
        errors = list(Draft202012Validator(command_schema, format_checker=FormatChecker()).iter_errors(sidecar))
        if errors:
            raise GateFailure(
                f"command sidecar schema rejected {command_id}: " + "; ".join(error.message for error in errors)
            )
        write_json(run_dir / f"{command_id}.command.json", sidecar)
        print(f"[offline-ml gate] {result} {command_id} ({duration_ms} ms)", flush=True)
        if process.returncode != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise GateFailure(f"{command_id} exited {process.returncode}\n{tail}")

    python = str(module / ".venv/bin/python")
    run_gate(
        "contract-schema",
        [python, "scripts/validate-static-contracts.py", "--repo", "..", "--output", str(run_dir / "static.json")],
    )
    run_gate(
        "source-sentinels",
        [
            python,
            "scripts/validate-json-evidence.py",
            "--schema",
            "../contracts/evidence/offline-ml-static/v1/schema.json",
            "--evidence",
            str(run_dir / "static.json"),
        ],
    )
    run_gate("findings", [python, "scripts/validate-findings.py", "--repo", ".."])
    run_gate("format", [str(module / ".venv/bin/ruff"), "format", "--check", "src", "tests", "scripts"])
    run_gate("lint", [str(module / ".venv/bin/ruff"), "check", "src", "tests", "scripts"])
    run_gate("typecheck", [str(module / ".venv/bin/pyright")])
    run_gate("unit-contract", [python, "-m", "unittest", "discover", "-s", "tests", "-v"])
    release_dir = run_dir / "release"
    run_gate(
        "release-build",
        ["scripts/build-release-runtime.sh", str(release_dir), str(run_dir / "release.json")],
    )
    entrypoint = release_dir / "venv/bin/masi-offline-ml"
    candidate_output = run_dir / "candidate-output"
    run_gate(
        "release-blackbox",
        [
            python,
            "scripts/run-release-blackbox.py",
            "--repo",
            "..",
            "--entrypoint",
            str(entrypoint),
            "--output",
            str(candidate_output),
            "--evidence",
            str(run_dir / "blackbox.json"),
        ],
    )
    central_build = run_dir / "central-build"
    run_gate("central-consumer-build", ["scripts/build-central-consumer.sh", str(central_build)])
    run_gate(
        "central-consumer",
        [
            python,
            "scripts/run-central-consumer.py",
            "--validator",
            str(central_build / "masi_repository_validator"),
            "--pipeline-output",
            str(candidate_output),
            "--evidence",
            str(run_dir / "central-consumer.json"),
        ],
    )
    run_gate(
        "triton-smoke",
        [
            python,
            "scripts/run-triton-smoke.py",
            "--pipeline-output",
            str(candidate_output),
            "--evidence",
            str(run_dir / "triton.json"),
        ],
    )
    run_gate(
        "fault",
        [
            python,
            "scripts/run-faults.py",
            "--repo",
            "..",
            "--entrypoint",
            str(entrypoint),
            "--evidence",
            str(run_dir / "fault.json"),
        ],
    )
    run_gate(
        "performance",
        [
            python,
            "scripts/run-performance.py",
            "--repo",
            "..",
            "--entrypoint",
            str(entrypoint),
            "--evidence",
            str(run_dir / "performance.json"),
        ],
    )
    run_gate(
        "image-build",
        ["scripts/build-image.sh"],
        extra_environment={
            "MASI_ML_IMAGE_REF": image_ref,
            "MASI_ML_BUILD_EVIDENCE": str(run_dir / "image-build.json"),
        },
    )
    run_gate(
        "oci-smoke",
        [
            python,
            "scripts/run-oci-smoke.py",
            "--image",
            image_ref,
            "--expected-blackbox",
            str(run_dir / "blackbox.json"),
            "--evidence",
            str(run_dir / "oci.json"),
        ],
    )
    run_gate(
        "deployment-policy",
        [python, "scripts/validate-deployment.py", "--repo", "..", "--output", str(run_dir / "deployment.json")],
    )
    run_gate(
        "supply-chain",
        ["scripts/run-supply-chain.sh"],
        extra_environment={
            "MASI_ML_IMAGE_REF": image_ref,
            "MASI_ML_SUPPLY_EVIDENCE_DIR": str(run_dir / "supply"),
        },
    )
    if arguments.stop_before_soak:
        print("[offline-ml gate] DIAGNOSTIC STOP before formal soak; no completion summary produced", flush=True)
        return 0
    run_gate(
        "formal-soak",
        [
            python,
            "scripts/run-soak.py",
            "--repo",
            "..",
            "--entrypoint",
            str(entrypoint),
            "--evidence",
            str(run_dir / "soak.json"),
            "--mode",
            "formal",
        ],
    )
    run_gate(
        "traceability",
        [
            python,
            "scripts/build-traceability.py",
            "--repo",
            "..",
            "--run-dir",
            str(run_dir),
            "--output",
            str(run_dir / "traceability.json"),
        ],
    )
    summary_path = run_dir / "module-summary.json"
    print("[offline-ml gate] DERIVE module summary", flush=True)
    subprocess.run(
        [
            python,
            "scripts/build-module-summary.py",
            "--repo",
            "..",
            "--run-dir",
            str(run_dir),
            "--output",
            str(summary_path),
        ],
        cwd=module,
        env=environment,
        check=True,
    )
    subprocess.run(
        [
            python,
            "scripts/validate-module-evidence.py",
            "--repo",
            "..",
            "--run-dir",
            str(run_dir),
            "--summary",
            str(summary_path),
        ],
        cwd=module,
        env=environment,
        check=True,
    )
    summary = cast(dict[str, Any], json.loads(summary_path.read_text(encoding="utf-8")))
    pointer = {
        "schema_version": "offline-ml-module-gate-pointer/v1",
        "run_id": arguments.run_id,
        "path": summary_path.relative_to(module).as_posix(),
        "summary_digest": digest(summary_path),
        "result": summary["result"],
        "qualification": summary["qualification"],
        "overall_module_complete": summary["overall_module_complete"],
    }
    write_json(module / "evidence/module-gates/latest.json", pointer, replace=True)
    write_json(module / "evidence/module-gates/gate-summary.json", summary, replace=True)
    print(json.dumps(pointer, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateFailure as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
