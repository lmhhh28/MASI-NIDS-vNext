#!/usr/bin/env python3
"""Shared fail-closed file and source-snapshot helpers for Plugin Host evidence."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 64 * 1024 * 1024
ZERO_DIGEST = "sha256:" + "0" * 64


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular no-symlink JSON file: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON_BYTES:
            raise ValueError(f"JSON evidence is not a bounded regular file: {path}")
        payload = bytearray()
        while True:
            chunk = os.read(
                descriptor, min(1024 * 1024, MAX_JSON_BYTES + 1 - len(payload))
            )
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_JSON_BYTES:
                raise ValueError(f"JSON evidence exceeds 64 MiB: {path}")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"JSON evidence changed while being read: {path}")
    finally:
        os.close(descriptor)
    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON evidence must be an object: {path}")
    return value


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def safe_run_file(run_dir: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise ValueError("evidence reference is not a canonical relative path")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or relative != relative_path.as_posix()
        or ".." in relative_path.parts
    ):
        raise ValueError(f"evidence reference escapes the run directory: {relative}")
    current = run_dir
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"evidence reference traverses a symlink: {relative}")
    if not current.is_file():
        raise ValueError(f"evidence reference is not a regular file: {relative}")
    current.resolve(strict=True).relative_to(run_dir)
    return current


def safe_summary_input(run_dir: Path, path: Path) -> Path:
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(run_dir).as_posix()
    except ValueError as exc:
        raise ValueError(
            "module summary must be inside the immutable run directory"
        ) from exc
    return safe_run_file(run_dir, relative)


def safe_output(run_dir: Path, path: Path) -> Path:
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(run_dir)
    except ValueError as exc:
        raise ValueError(
            "module summary output must be inside the immutable run directory"
        ) from exc
    if relative == Path(".") or len(relative.parts) != 1 or ".." in relative.parts:
        raise ValueError("module summary output path is invalid")
    if absolute.exists() or absolute.is_symlink():
        raise ValueError("refusing to overwrite module summary")
    return absolute


def write_new_output(run_dir: Path, path: Path, payload: bytes) -> None:
    if path.parent != run_dir:
        raise ValueError("module summary output must be a direct run-directory child")
    directory = os.open(
        run_dir, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)
    finally:
        os.close(directory)


def current_snapshot(repo: Path) -> tuple[str, str, bool, str]:
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        timeout=30,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        stdout=subprocess.PIPE,
        timeout=60,
    ).stdout
    status_digest = "sha256:" + hashlib.sha256(status).hexdigest()
    with tempfile.TemporaryDirectory(
        prefix="masi-plugin-host-source-digest."
    ) as temporary:
        archive = Path(temporary) / "source-tree.tar"
        subprocess.run(
            [
                "tar",
                "--sort=name",
                "--mtime=@1787270400",
                "--owner=0",
                "--group=0",
                "--numeric-owner",
                "--exclude=plugin-host-rs/target",
                "--exclude=plugin-host-rs/evidence",
                "--exclude=**/node_modules",
                "--exclude=**/__pycache__",
                "--exclude=*.pyc",
                "-cf",
                str(archive),
                "-C",
                str(repo),
                "plugin-host-rs",
                "contracts",
                "deploy/plugin-host",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        source_digest = digest(archive)
    return revision, status_digest, bool(status), source_digest


def reject_zero_digests(value: Any, location: str = "$") -> list[str]:
    failures: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            failures.extend(reject_zero_digests(item, f"{location}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            failures.extend(reject_zero_digests(item, f"{location}[{index}]"))
    elif value == ZERO_DIGEST:
        failures.append(f"all-zero digest at {location}")
    return failures
