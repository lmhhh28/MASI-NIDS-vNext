#!/usr/bin/env python3
"""Verify a formal module gate summary, current source identity, and latest pointer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from testkit.p4_switch.lib.source_identity import (
    current_source_identity,
    read_stable_regular_file,
)


MODULES: dict[str, dict[str, Any]] = {
    "p4": {
        "summary": "evidence/p4-switch/{run_id}/qualification-evidence.json",
        "schema": "contracts/evidence/v1/schema.json",
        "failure": "evidence/p4-switch/{run_id}/gate-failure.json",
    },
    "edge": {
        "summary": "edge-rs/evidence/module-gates/runs/{run_id}/gate-summary.json",
        "pointer": "edge-rs/evidence/module-gates/latest.json",
        "pointer_base": "pointer",
        "schema": "contracts/evidence/v1/edge-module-schema.json",
        "failure": "edge-rs/evidence/module-gates/runs/{run_id}/gate-failure.json",
    },
    "inference": {
        "summary": "infer-cpp/evidence/module-gates/runs/{run_id}/gate-summary.json",
        "pointer": "infer-cpp/evidence/module-gates/latest.json",
        "pointer_base": "pointer",
        "schema": "contracts/evidence/v1/inference-module-schema.json",
        "failure": "infer-cpp/evidence/module-gates/runs/{run_id}/gate-failure.json",
    },
    "control": {
        "summary": "control-go/evidence/module-gates/runs/{run_id}/gate-summary.json",
        "pointer": "control-go/evidence/module-gates/latest.json",
        "pointer_base": "pointer",
        "schema": "contracts/evidence/v1/control-module-schema.json",
        "failure": "control-go/evidence/module-gates/runs/{run_id}/gate-failure.json",
    },
    "db": {
        "summary": "db/evidence/module-gates/runs/{run_id}/gate-summary.json",
        "pointer": "db/evidence/module-gates/latest.json",
        "pointer_base": "pointer",
        "schema": "contracts/evidence/v1/postgresql-state-module-schema.json",
        "failure": "db/evidence/module-gates/runs/{run_id}/gate-failure.json",
    },
    "plugin-host": {
        "summary": "plugin-host-rs/evidence/module-gates/runs/{run_id}/gate-summary.json",
        "pointer": "plugin-host-rs/evidence/module-gates/latest.json",
        "pointer_base": "pointer",
        "schema": "contracts/evidence/plugin-runtime-host-module/v1/schema.json",
        "failure": "plugin-host-rs/evidence/module-gates/runs/{run_id}/gate-failure.json",
    },
    "analysis": {
        "summary": "analysis-py/evidence/module-gates/runs/{run_id}/gate-summary.json",
        "pointer": "analysis-py/evidence/module-gates/latest.json",
        "pointer_base": "pointer",
        "schema": "contracts/evidence/analysis-module/v1/schema.json",
        "failure": "analysis-py/evidence/module-gates/runs/{run_id}/gate-failure.json",
    },
    "offline-ml": {
        "summary": "ml-py/evidence/module-gates/runs/{run_id}/module-summary.json",
        "pointer": "ml-py/evidence/module-gates/latest.json",
        "pointer_base": "module",
        "schema": "contracts/evidence/offline-ml-module/v1/schema.json",
        "failure": "ml-py/evidence/module-gates/runs/{run_id}/gate-failure.json",
    },
    "web": {
        "summary": "web/evidence/module-gates/runs/{run_id}/gate-summary.json",
        "pointer": "web/evidence/module-gates/latest.json",
        "pointer_base": "pointer",
        "schema": "contracts/evidence/web-module/v1/schema.json",
        "failure": "web/evidence/module-gates/runs/{run_id}/gate-failure.json",
    },
}

DIGEST_PATTERN = re.compile(r"^sha256:(?!0{64}$)[0-9a-f]{64}$")
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
MODULE_IDS = {
    "p4": "MOD-SW-001",
    "edge": "MOD-EDGE-001",
    "inference": "MOD-INF-001",
    "control": "MOD-CTRL-001",
    "db": "MOD-DB-001",
    "plugin-host": "MOD-PLUGIN-001",
    "analysis": "MOD-AGENT-001",
    "offline-ml": "MOD-ML-001",
    "web": "MOD-WEB-001",
}

# These profiles intentionally mirror each module runner's already-published
# source closure.  Root verification independently rebuilds the same snapshot;
# it does not substitute P4's narrower closure or a repository-wide digest.
MODULE_SOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "p4": {"kind": "p4", "status": "nul-all"},
    "edge": {
        "kind": "tar", "mtime": 1786406400, "roots": ("edge-rs", "contracts"),
        "excludes": ("edge-rs/target", "edge-rs/evidence", "**/node_modules", "**/__pycache__", "*.pyc"),
        "status": "plain-newline-all",
    },
    "inference": {
        "kind": "tar", "mtime": 1786406400, "roots": ("infer-cpp", "contracts", "testkit"),
        "excludes": ("infer-cpp/build", "infer-cpp/evidence", "**/node_modules", "**/__pycache__", "*.pyc"),
        "status": "plain-newline-all",
    },
    "control": {
        "kind": "tar", "mtime": 1786406400, "roots": ("control-go", "contracts", "db"),
        "excludes": ("control-go/evidence", "db/evidence", "**/node_modules", "control-go/**/__pycache__", "contracts/**/__pycache__"),
        "status": "plain-newline-all",
    },
    "db": {"kind": "db-git", "status": "nul-default"},
    "plugin-host": {
        "kind": "tar", "mtime": 1787270400, "roots": ("plugin-host-rs", "contracts", "deploy/plugin-host"),
        "excludes": ("plugin-host-rs/target", "plugin-host-rs/evidence", "**/node_modules", "**/__pycache__", "*.pyc"),
        "status": "plain-all",
    },
    "analysis": {
        "kind": "tar", "mtime": 1787334400, "roots": ("analysis-py", "contracts", "deploy/analysis"),
        "excludes": ("analysis-py/.venv", "analysis-py/.ruff_cache", "analysis-py/evidence", "analysis-py/build", "analysis-py/dist", "analysis-py/src/masi_analysis_plugin.egg-info", "**/node_modules", "**/__pycache__", "*.pyc"),
        "status": "plain-all",
    },
    "offline-ml": {"kind": "offline-ml", "status": "nul-all"},
    "web": {
        "kind": "tar", "mtime": 1787760000,
        "roots": ("web", "contracts/generated/typescript/control-api", "contracts/web/v1", "contracts/profiles/v1/web-spa.json", "contracts/profiles/v1/web-browser.json", "contracts/profiles/v1/web-performance.json", "contracts/evidence/web-performance/v1", "contracts/evidence/web-soak/v1", "contracts/evidence/web-manual-accessibility/v1", "contracts/evidence/web-module/v1", "contracts/supply-chain/v1/web-components.json"),
        "excludes": ("web/node_modules", "web/dist", "web/output", "web/evidence", "web/.playwright-cli", "web/sbom.cdx.json"),
        "status": "plain-newline-all",
    },
}


def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def require_string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"summary lacks non-empty {key}")
    return value


def read_file(repo: Path, path: Path) -> tuple[Path, bytes]:
    candidate = path if path.is_absolute() else repo / path
    absolute = candidate.absolute()
    try:
        relative = absolute.relative_to(repo)
    except ValueError as error:
        raise ValueError(f"path escapes repository: {path}") from error
    if not relative.parts:
        raise ValueError(f"evidence path is not a file: {path}")
    descriptors: list[int] = []
    try:
        directory = os.open(repo, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        descriptors.append(directory)
        for part in relative.parts[:-1]:
            directory = os.open(
                part,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            descriptors.append(directory)
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory,
        )
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError(f"evidence is not a bounded regular file: {path}")
        payload = bytearray()
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_EVIDENCE_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_EVIDENCE_BYTES:
                raise ValueError(f"evidence file exceeds 64 MiB: {path}")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"evidence file changed while being read: {path}")
        return absolute, bytes(payload)
    except OSError as error:
        raise ValueError(f"unsafe or absent evidence file: {path}: {error}") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def decode_json(path: Path, payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except UnicodeDecodeError as error:
        raise ValueError(f"evidence is not UTF-8 JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"evidence must be a JSON object: {path}")
    return value


def load_json(repo: Path, path: Path) -> tuple[Path, dict[str, Any], bytes]:
    safe, payload = read_file(repo, path)
    return safe, decode_json(path, payload), payload


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def tar_source_digest(repo: Path, profile: dict[str, Any]) -> str:
    with tempfile.TemporaryDirectory(prefix="masi-root-source-verify-") as directory:
        archive = Path(directory) / "source-tree.tar"
        command = [
            "tar", "--sort=name", f"--mtime=@{profile['mtime']}", "--owner=0", "--group=0",
            "--numeric-owner", *[f"--exclude={value}" for value in profile["excludes"]],
            "-cf", str(archive), "-C", str(repo), *profile["roots"],
        ]
        result = subprocess.run(
            command, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=180,
        )
        if result.returncode != 0 or not archive.is_file():
            detail = result.stderr.decode("utf-8", errors="replace")[-1000:]
            raise ValueError(f"cannot rebuild module source archive: {detail}")
        if not 0 < archive.stat().st_size <= 1024 * 1024 * 1024:
            raise ValueError("rebuilt module source archive exceeds its 1 GiB bound")
        return sha256_file(archive)


def file_set_source_digest(repo: Path, roots: tuple[str, ...]) -> str:
    paths: list[Path] = []
    for relative_root in roots:
        root = repo / relative_root
        if root.is_symlink():
            raise ValueError(f"module source root is a symlink: {relative_root}")
        if root.is_file():
            paths.append(root)
            continue
        if not root.is_dir():
            raise ValueError(f"module source root is absent: {relative_root}")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"module source closure contains a symlink: {path.relative_to(repo)}")
            if path.is_file():
                paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.relative_to(repo).as_posix().encode()):
        relative = path.relative_to(repo).as_posix().encode()
        payload = read_stable_regular_file(path)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def db_source_digest(repo: Path) -> str:
    pathspecs = (
        "db", "deploy/postgresql-state", "contracts", "docs", ":(exclude)db/evidence/**",
    )
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        stdout=subprocess.PIPE, timeout=30,
    ).stdout
    difference = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", "HEAD", "--", *pathspecs],
        check=True, stdout=subprocess.PIPE, timeout=120,
    ).stdout
    names = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs],
        check=True, stdout=subprocess.PIPE, timeout=60,
    ).stdout.split(b"\0")
    digest = hashlib.sha256()
    digest.update(revision)
    digest.update(difference)
    for raw_name in sorted(name for name in names if name):
        if b"\n" in raw_name or b"\r" in raw_name:
            raise ValueError("DB source identity rejects newline-bearing paths")
        relative = Path(os.fsdecode(raw_name))
        path = repo / relative
        digest.update(raw_name)
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.fsencode(os.readlink(path)))
            digest.update(b"\0")
        elif path.is_file():
            payload_digest = hashlib.sha256(read_stable_regular_file(path)).hexdigest().encode()
            digest.update(payload_digest + b"  " + os.fsencode(path) + b"\n")
        else:
            raise ValueError(f"unsupported DB source identity path: {relative}")
    return "sha256:" + digest.hexdigest()


def module_source_digest(repo: Path, module: str) -> str:
    profile = MODULE_SOURCE_PROFILES[module]
    kind = profile["kind"]
    if kind == "p4":
        return str(current_source_identity(repo)["source_tree_digest"])
    if kind == "tar":
        return tar_source_digest(repo, profile)
    if kind == "db-git":
        return db_source_digest(repo)
    if kind == "offline-ml":
        result = subprocess.run(
            [sys.executable, str(repo / "ml-py/scripts/source-tree-digest.py"), "--repo", str(repo)],
            check=True, text=True, stdout=subprocess.PIPE, timeout=180,
        ).stdout.strip()
        digest = "sha256:" + result
        if not DIGEST_PATTERN.fullmatch(digest):
            raise ValueError("Offline ML source digest helper returned malformed output")
        return digest
    if kind == "files":
        return file_set_source_digest(repo, tuple(profile["roots"]))
    raise ValueError(f"unsupported module source profile: {kind}")


def current_status_identity(repo: Path, mode: str) -> tuple[bool, str]:
    command = ["git", "-C", str(repo), "status", "--porcelain=v1"]
    if mode.startswith("nul"):
        command.append("-z")
    if mode.endswith("-all"):
        command.append("--untracked-files=all")
    raw = subprocess.run(command, check=True, stdout=subprocess.PIPE, timeout=60).stdout
    dirty = bool(raw)
    payload = raw.rstrip(b"\n") + b"\n" if mode.startswith("plain-newline") else raw
    return dirty, sha256_bytes(payload)


def current_module_source_identity(repo: Path, module: str) -> dict[str, object]:
    if MODULE_SOURCE_PROFILES[module]["kind"] == "p4":
        return current_source_identity(repo)
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True,
        stdout=subprocess.PIPE, timeout=30,
    ).stdout.strip()
    dirty_before, status_before = current_status_identity(
        repo, str(MODULE_SOURCE_PROFILES[module]["status"])
    )
    source_digest = module_source_digest(repo, module)
    dirty_after, status_after = current_status_identity(
        repo, str(MODULE_SOURCE_PROFILES[module]["status"])
    )
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True,
        stdout=subprocess.PIPE, timeout=30,
    ).stdout.strip()
    if (head_before, dirty_before, status_before) != (head_after, dirty_after, status_after):
        raise ValueError("module source identity changed while it was rebuilt")
    return {
        "source_revision": head_before,
        "source_tree_digest": source_digest,
        "working_tree_dirty": dirty_before,
        "working_tree_status_digest": status_before,
    }


def schema_failures(schema: dict[str, Any], document: dict[str, Any]) -> list[str]:
    return [
        f"{list(error.absolute_path)}: {error.message}"
        for error in sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
                document
            ),
            key=lambda error: list(error.absolute_path),
        )
    ]


def verify_failure(
    repo: Path,
    config: dict[str, Any],
    module: str,
    run_id: str,
    require_clean: bool,
) -> str:
    failure_relative = config.get("failure")
    if not isinstance(failure_relative, str):
        raise ValueError("module has no structured failure evidence path")
    path, document, failure_payload = load_json(
        repo, Path(failure_relative.format(run_id=run_id))
    )
    _, schema, _ = load_json(
        repo, Path("contracts/evidence/module-runner-failure/v1/schema.json")
    )
    failures = schema_failures(schema, document)
    if failures:
        raise ValueError("module failure schema rejected: " + "; ".join(failures))
    if (
        document.get("module") != module
        or document.get("module_id") != MODULE_IDS[module]
        or document.get("run_id") != run_id
    ):
        raise ValueError("module failure identity differs from the requested run")
    referenced: dict[str, tuple[Path, bytes]] = {}
    for name in ("command_evidence", "log_evidence"):
        reference = document[name]
        target, payload = read_file(repo, path.parent / reference["path"])
        if (
            sha256_bytes(payload) != reference["digest"]
            or len(payload) != reference["bytes"]
        ):
            raise ValueError(f"module failure {name} does not bind the referenced file")
        referenced[name] = (target, payload)
    command_path, command_payload = referenced["command_evidence"]
    command = decode_json(command_path, command_payload)
    _, command_schema, _ = load_json(
        repo, Path("contracts/evidence/command/v1/schema.json")
    )
    command_failures = schema_failures(command_schema, command)
    if command_failures:
        raise ValueError(
            "failed command sidecar schema rejected: " + "; ".join(command_failures)
        )
    if (
        command.get("run_id") != run_id
        or command.get("command_id") != document.get("failed_command_id")
        or command.get("exit_code") != document.get("exit_code")
        or command.get("result") != document.get("result")
        or command.get("source_tree_digest") != document.get("source_tree_digest")
        or command.get("working_tree_status_digest")
        != document.get("working_tree_status_digest")
    ):
        raise ValueError("module failure does not bind the failed command identity")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True,
        stdout=subprocess.PIPE, timeout=30,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True, stdout=subprocess.PIPE, timeout=60,
    ).stdout
    if document.get("failed_command_id") == "preflight" and module == "p4":
        failure_tree_digest = str(current_source_identity(repo)["source_tree_digest"])
        failure_status_digest = sha256_bytes(status)
    elif document.get("failed_command_id") == "preflight":
        index = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-s", "-z"], check=True,
            stdout=subprocess.PIPE, timeout=60,
        ).stdout
        failure_tree_digest = sha256_bytes(index)
        failure_status_digest = sha256_bytes(status)
    else:
        current = current_module_source_identity(repo, module)
        failure_tree_digest = str(current["source_tree_digest"])
        failure_status_digest = str(current["working_tree_status_digest"])
    if document.get("source_revision") != head:
        raise ValueError("module failure evidence is not bound to the current revision")
    if document.get("source_tree_digest") != failure_tree_digest or document.get(
        "working_tree_status_digest"
    ) != failure_status_digest:
        raise ValueError("module failure evidence differs from the current preflight source identity")
    if require_clean:
        if status:
            raise ValueError(
                "formal GitHub module failure evidence must originate from a clean checkout"
            )
    return (
        f"verified {path.relative_to(repo)} {sha256_bytes(failure_payload)} result={document['result']} "
        f"qualification={document['qualification']}"
    )


def verify_completion(document: dict[str, Any]) -> None:
    if document.get("overall_module_complete") is not True:
        raise ValueError("formal gate did not derive overall_module_complete=true")
    result = require_string(document, "result")
    qualification = require_string(document, "qualification")
    if result not in {"PASS", "HOLD"}:
        raise ValueError(f"completed module has invalid result: {result}")
    if qualification not in {"QUALIFIED", "NOT_QUALIFIED"}:
        raise ValueError(f"invalid qualification: {qualification}")
    if result == "PASS" and qualification != "QUALIFIED":
        raise ValueError("PASS module must be QUALIFIED")
    completion = document.get("completion")
    if isinstance(completion, dict):
        if any(value is False or value is None for value in completion.values()):
            raise ValueError("completion contains a false or null prerequisite")
        if "status" in completion and completion["status"] != "COMPLETE":
            raise ValueError("completion status is not COMPLETE")
        if isinstance(completion.get("blockers"), list) and completion["blockers"]:
            raise ValueError("completion retains operational blockers")
        criteria = completion.get("criteria")
        if isinstance(criteria, dict) and any(
            value != "PASS" for value in criteria.values()
        ):
            raise ValueError("completion criteria are not all PASS")
    if result == "HOLD":
        if qualification != "NOT_QUALIFIED":
            raise ValueError("HOLD module must remain NOT_QUALIFIED")
        holds = document.get("remaining_holds")
        gates = document.get("qualification_gates")
        gate_values = list(gates.values()) if isinstance(gates, dict) else []
        gate_results = {
            value.get("result") if isinstance(value, dict) else value
            for value in gate_values
        }
        if "FAIL" in gate_results:
            raise ValueError("qualification-only HOLD contains a failed gate")
        held_results = {"HOLD", "NOT_RUN"}.intersection(gate_results)
        explained_gate = any(
            isinstance(value, dict)
            and value.get("result") in {"HOLD", "NOT_RUN"}
            and any(value.get(key) not in (None, "", []) for key in ("reason", "stable_reason", "evidence", "command"))
            for value in gate_values
        )
        structured_hold = bool(held_results) and (
            (isinstance(holds, list) and bool(holds)) or explained_gate
        )
        scoped_hold = isinstance(document.get("qualification_scope"), str) and bool(
            document["qualification_scope"]
        )
        if not structured_hold and not scoped_hold:
            raise ValueError("HOLD completion lacks qualification-only hold evidence")


def verify_p4_completion(document: dict[str, Any]) -> None:
    tests = document.get("tests")
    performance = document.get("performance")
    cleanup = document.get("cleanup")
    if not isinstance(tests, list) or not isinstance(performance, list) or not isinstance(cleanup, dict):
        raise ValueError("P4 completion lacks tests, performance, or cleanup evidence")
    if any(isinstance(item, dict) and item.get("result") == "FAIL" for item in [*tests, *performance]):
        raise ValueError("P4 completion contains a failed test or performance result")
    if cleanup.get("result") != "PASS" or cleanup.get("remaining_resources") != {
        "containers": [], "networks": [], "volumes": []
    }:
        raise ValueError("P4 completion cleanup is not an exact zero-residual PASS")
    if document.get("result") == "PASS":
        for item in tests:
            if not isinstance(item, dict):
                raise ValueError("P4 PASS contains a malformed test record")
            if item.get("applicability") == "APPLICABLE" and (
                item.get("result") != "PASS" or item.get("qualification") != "QUALIFIED"
            ):
                raise ValueError("P4 PASS contains a non-passing applicable test")
        if any(
            not isinstance(item, dict)
            or item.get("result") != "PASS"
            or item.get("qualification") != "QUALIFIED"
            for item in performance
        ):
            raise ValueError("P4 PASS contains a non-passing performance result")


def verify_pointer(
    repo: Path,
    config: dict[str, Any],
    run_id: str,
    summary_path: Path,
    summary: dict[str, Any],
    summary_digest: str,
    *,
    required: bool,
) -> None:
    pointer_relative = config.get("pointer")
    if not isinstance(pointer_relative, str):
        return
    pointer_candidate = repo / pointer_relative
    if not pointer_candidate.exists() and not pointer_candidate.is_symlink():
        if required:
            raise ValueError("latest pointer is absent")
        return
    pointer_path, pointer, _ = load_json(repo, Path(pointer_relative))
    if pointer.get("run_id") != run_id:
        raise ValueError("latest pointer run_id does not match requested run")
    relative_target = pointer.get("evidence", pointer.get("path"))
    pointer_digest = pointer.get("digest", pointer.get("summary_digest"))
    if not isinstance(relative_target, str) or not DIGEST_PATTERN.fullmatch(
        str(pointer_digest)
    ):
        raise ValueError("latest pointer path or digest is invalid")
    if config["pointer_base"] == "module":
        base = pointer_path.parents[2]
    else:
        base = pointer_path.parent
    resolved_target, target_payload = read_file(repo, base / relative_target)
    if (
        resolved_target != summary_path
        or pointer_digest != summary_digest
        or sha256_bytes(target_payload) != summary_digest
    ):
        raise ValueError("latest pointer does not bind the canonical summary")
    for key in ("result", "qualification", "overall_module_complete"):
        if key in pointer and pointer[key] != summary.get(key):
            raise ValueError(f"latest pointer {key} differs from canonical summary")
    source_keys = (
        "source_revision",
        "source_tree_digest",
        "working_tree_status_digest",
    )
    source_fields = [key in pointer for key in source_keys]
    if any(source_fields) and not all(source_fields):
        raise ValueError("latest pointer has a partial source binding")
    for key in source_keys:
        if key in pointer and pointer[key] != summary.get(key):
            raise ValueError(f"latest pointer {key} differs from canonical summary")


def verify(
    repo: Path,
    module: str,
    run_id: str,
    require_clean: bool,
    allow_incomplete: bool = False,
) -> str:
    config = MODULES[module]
    relative = Path(str(config["summary"]).format(run_id=run_id))
    summary_candidate = repo / relative
    if (
        allow_incomplete
        and not summary_candidate.exists()
        and not summary_candidate.is_symlink()
    ):
        return verify_failure(repo, config, module, run_id, require_clean)
    summary_path, document, summary_payload = load_json(repo, relative)
    summary_digest = sha256_bytes(summary_payload)
    _, schema, _ = load_json(repo, Path(str(config["schema"])))
    errors = schema_failures(schema, document)
    if errors:
        raise ValueError("module summary schema rejected: " + "; ".join(errors))
    if document.get("run_id") != run_id:
        raise ValueError("canonical gate summary run_id does not match requested run")
    complete = document.get("overall_module_complete") is True
    if complete:
        verify_completion(document)
        if module == "p4":
            verify_p4_completion(document)
    elif not allow_incomplete:
        raise ValueError("formal gate did not derive overall_module_complete=true")
    elif (
        document.get("result") not in {"FAIL", "HOLD", "NOT_RUN"}
        or document.get("qualification") != "NOT_QUALIFIED"
    ):
        raise ValueError(
            "incomplete evidence must remain FAIL/HOLD/NOT_RUN and NOT_QUALIFIED"
        )
    source_digest = document.get("source_tree_digest")
    if not isinstance(source_digest, str) or not DIGEST_PATTERN.fullmatch(source_digest):
        raise ValueError("canonical summary source_tree_digest is invalid")
    current = current_module_source_identity(repo, module)
    for key in (
        "source_revision",
        "source_tree_digest",
        "working_tree_dirty",
        "working_tree_status_digest",
    ):
        if document.get(key) != current[key]:
            raise ValueError(
                f"canonical summary {key} differs from current {module} source"
            )
    if require_clean:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            stdout=subprocess.PIPE,
            timeout=60,
        ).stdout
        if status or (
            "working_tree_dirty" in document
            and document.get("working_tree_dirty") is not False
        ):
            raise ValueError(
                "formal GitHub module gate must originate from a clean checkout"
            )
    verify_pointer(
        repo,
        config,
        run_id,
        summary_path,
        document,
        summary_digest,
        required=complete,
    )
    return f"verified {relative} {summary_digest} result={document['result']} qualification={document['qualification']}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("module", choices=sorted(MODULES))
    parser.add_argument("run_id")
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    try:
        print(
            verify(
                repo,
                arguments.module,
                arguments.run_id,
                arguments.require_clean,
                arguments.allow_incomplete,
            )
        )
    except (
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as error:
        raise SystemExit(f"module gate evidence rejected: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
