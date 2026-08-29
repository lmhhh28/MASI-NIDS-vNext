"""Exact full-repository source identity for connected-system rehearsal evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from testkit.p4_switch.lib.source_identity import read_stable_regular_file


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
    "output",
    "target",
}


def current_source_identity(repo: Path) -> dict[str, object]:
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=30,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True,
        stdout=subprocess.PIPE,
        timeout=60,
    ).stdout
    def paths() -> list[Path]:
        names = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            timeout=60,
        ).stdout.split(b"\0")
        output: list[Path] = []
        for raw_name in sorted(value for value in names if value):
            relative = Path(os.fsdecode(raw_name))
            if EXCLUDED_PARTS.intersection(relative.parts):
                continue
            path = repo / relative
            if path.is_symlink():
                raise ValueError(f"system source closure contains a symlink: {relative}")
            if path.is_file():
                output.append(path)
        return output

    source_paths = paths()
    before = {
        path: (
            (metadata := path.stat(follow_symlinks=False)).st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        for path in source_paths
    }
    digest = hashlib.sha256()
    for path in source_paths:
        relative = path.relative_to(repo)
        payload = read_stable_regular_file(path)
        encoded = relative.as_posix().encode()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    if paths() != source_paths:
        raise ValueError("system source file set changed while hashing")
    for path in source_paths:
        metadata = path.stat(follow_symlinks=False)
        if before[path] != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise ValueError(f"system source changed during hashing: {path}")
    return {
        "source_revision": revision,
        "source_tree_digest": "sha256:" + digest.hexdigest(),
        "working_tree_dirty": bool(status),
        "working_tree_status_digest": "sha256:" + hashlib.sha256(status).hexdigest(),
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: source_identity.py REPOSITORY")
    repo = Path(sys.argv[1]).resolve(strict=True)
    print(json.dumps(current_source_identity(repo), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
