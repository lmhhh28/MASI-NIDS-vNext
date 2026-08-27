"""Canonical JSON, digest, path, and finite-number helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    """Stable fail-closed validation error."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def canonical_json_bytes(value: object) -> bytes:
    """Return the project canonical UTF-8 JSON encoding."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path, *, maximum_bytes: int = 10 * 1024 * 1024 * 1024) -> str:
    ensure_ordinary_file(path, maximum_bytes=maximum_bytes)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def ensure_ordinary_file(path: Path, *, maximum_bytes: int) -> None:
    """Reject links and non-regular or oversized files before reading."""

    try:
        stat_result = path.lstat()
    except FileNotFoundError as error:
        raise ValidationError("file_missing", str(path)) from error
    if path.is_symlink() or not path.is_file():
        raise ValidationError("file_not_ordinary", str(path))
    if stat_result.st_size > maximum_bytes:
        raise ValidationError("file_too_large", f"{path}: {stat_result.st_size}>{maximum_bytes}")


def load_json(path: Path, *, maximum_bytes: int = 4 * 1024 * 1024) -> Any:
    ensure_ordinary_file(path, maximum_bytes=maximum_bytes)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("invalid_json", f"{path}: {error}") from error


def resolve_repo_file(repo: Path, relative: str, *, maximum_bytes: int) -> Path:
    """Resolve a normalized repository-relative path without link traversal."""

    if not relative or relative.startswith("/") or "\x00" in relative:
        raise ValidationError("invalid_relative_path", relative)
    parts = Path(relative).parts
    if any(part in ("", ".", "..") for part in parts):
        raise ValidationError("invalid_relative_path", relative)
    resolved_repo = repo.resolve(strict=True)
    candidate = repo.joinpath(*parts)
    parent = candidate.parent.resolve(strict=True)
    if os.path.commonpath((str(resolved_repo), str(parent))) != str(resolved_repo):
        raise ValidationError("path_escape", relative)
    ensure_ordinary_file(candidate, maximum_bytes=maximum_bytes)
    return candidate


def ensure_finite(value: object, *, path: str = "$") -> None:
    """Reject non-finite numbers recursively, including permissive Python JSON values."""

    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("non_finite", path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            ensure_finite(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            ensure_finite(item, path=f"{path}.{key}")
        return
    raise ValidationError("unsupported_json_value", f"{path}: {type(value).__name__}")


def write_fresh_json(path: Path, value: object) -> None:
    """Atomically create a fresh JSON result without overwriting or following links."""

    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise ValidationError("output_exists", str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValidationError("temporary_output_exists", str(temporary))
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_fresh_bytes(path: Path, payload: bytes, *, mode: int = 0o640) -> None:
    """Atomically create a fresh binary file and fsync it."""

    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise ValidationError("output_exists", str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValidationError("temporary_output_exists", str(temporary))
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ValidationError("short_write", str(path))
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def create_fresh_directory(path: Path, *, mode: int = 0o750) -> Path:
    """Create an exact new directory; existing or linked targets are rejected."""

    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise ValidationError("output_directory_exists", str(path))
    path.mkdir(parents=True, mode=mode)
    return path
