"""Canonical current-source identity for P4 module evidence and CI verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "evidence",
    "node_modules",
    "out",
    "target",
}
DIGEST_PATTERN = re.compile(r"^sha256:(?!0{64}$)[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAX_SOURCE_FILE_BYTES = 128 * 1024 * 1024


def read_stable_regular_file(path: Path, maximum_bytes: int = MAX_SOURCE_FILE_BYTES) -> bytes:
    absolute = path.absolute()
    directory_descriptors: list[int] = []
    directory = os.open(absolute.anchor, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    directory_descriptors.append(directory)
    try:
        for part in absolute.parts[1:-1]:
            directory = os.open(
                part,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            directory_descriptors.append(directory)
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory,
        )
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > maximum_bytes:
            raise ValueError(f"P4 source is not a bounded regular file: {path}")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise ValueError(f"P4 source exceeds {maximum_bytes} bytes: {path}")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"P4 source changed while being read: {path}")
        return bytes(payload)
    finally:
        os.close(descriptor)


def source_paths(repo: Path) -> list[Path]:
    if repo.is_symlink():
        raise ValueError("P4 source repository root is a symlink")
    roots = (
        (repo / "p4", "directory"),
        (repo / "testkit/p4_switch", "directory"),
        (repo / "deploy/p4-switch", "directory"),
        (repo / "deploy/supply-chain", "directory"),
        (repo / "contracts", "directory"),
        (repo / "docs/masi-nids-vnext-system-requirements-2026-08-09.md", "file"),
    )
    files: list[Path] = []
    for root, expected_kind in roots:
        if root.is_symlink():
            raise ValueError(
                f"P4 source closure root is a symlink: {root.relative_to(repo)}"
            )
        if expected_kind == "file" and not root.is_file():
            raise ValueError(f"P4 source closure file is absent: {root.relative_to(repo)}")
        if expected_kind == "directory" and not root.is_dir():
            raise ValueError(f"P4 source closure directory is absent: {root.relative_to(repo)}")
        if expected_kind == "file":
            files.append(root)
            continue
        for path in root.rglob("*"):
            relative = path.relative_to(repo)
            if EXCLUDED_PARTS.intersection(relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"P4 source closure contains a symlink: {relative}")
            if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
                files.append(path)
    return sorted(set(files), key=lambda item: item.relative_to(repo).as_posix().encode())


def source_tree_digest(repo: Path) -> str:
    files = source_paths(repo)
    before = {
        path: (
            (metadata := path.stat(follow_symlinks=False)).st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        for path in files
    }
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(repo).as_posix().encode()
        payload = read_stable_regular_file(path)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    after_files = source_paths(repo)
    if after_files != files:
        raise ValueError("P4 source file set changed while hashing")
    for path in files:
        metadata = path.stat(follow_symlinks=False)
        if before[path] != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise ValueError(f"P4 source changed during tree hashing: {path}")
    return "sha256:" + digest.hexdigest()


def current_source_identity(repo: Path) -> dict[str, object]:
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=30,
    ).stdout.strip()
    status = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        check=True,
        stdout=subprocess.PIPE,
        timeout=60,
    ).stdout
    return {
        "source_revision": revision,
        "source_tree_digest": source_tree_digest(repo),
        "working_tree_dirty": bool(status),
        "working_tree_status_digest": "sha256:" + hashlib.sha256(status).hexdigest(),
    }


def validate_source_identity(
    repo: Path, document: dict[str, object]
) -> dict[str, object]:
    expected_keys = {
        "source_revision",
        "source_tree_digest",
        "working_tree_dirty",
        "working_tree_status_digest",
    }
    if set(document) != expected_keys:
        raise ValueError("P4 source identity shape mismatch")
    revision = document["source_revision"]
    tree_digest = document["source_tree_digest"]
    status_digest = document["working_tree_status_digest"]
    if not isinstance(revision, str) or REVISION_PATTERN.fullmatch(revision) is None:
        raise ValueError("P4 source revision is malformed")
    if (
        not isinstance(tree_digest, str)
        or DIGEST_PATTERN.fullmatch(tree_digest) is None
    ):
        raise ValueError("P4 source tree digest is malformed")
    if (
        not isinstance(status_digest, str)
        or DIGEST_PATTERN.fullmatch(status_digest) is None
    ):
        raise ValueError("P4 working tree status digest is malformed")
    if type(document["working_tree_dirty"]) is not bool:
        raise ValueError("P4 working tree dirty flag is malformed")
    observed_tree_digest = source_tree_digest(repo)
    if tree_digest != observed_tree_digest:
        raise ValueError(
            f"P4 source tree changed during the run: expected {tree_digest}, observed {observed_tree_digest}"
        )
    return dict(document)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: source_identity.py REPOSITORY")
    supplied = Path(sys.argv[1]).absolute()
    if supplied.is_symlink():
        raise SystemExit("unsafe P4 source repository")
    repo = supplied.resolve(strict=True)
    if not repo.is_dir():
        raise SystemExit("unsafe P4 source repository")
    print(
        json.dumps(current_source_identity(repo), sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
