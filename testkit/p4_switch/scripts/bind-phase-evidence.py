from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path


BINDING_KEYS = (
    "run_id",
    "source_revision",
    "source_tree_digest",
    "working_tree_dirty",
    "working_tree_status_digest",
)


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON member: {key}")
        document[key] = value
    return document


def read_regular(path: Path, maximum_bytes: int = 64 * 1024 * 1024) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise ValueError(f"unsafe bounded phase input: {path}")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise ValueError(f"phase input exceeds {maximum_bytes} bytes")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("phase input changed while being read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=Path, required=True)
    parser.add_argument("--phase-name", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-identity", type=Path, required=True)
    args = parser.parse_args()

    phase = json.loads(
        read_regular(args.phase).decode("utf-8"), object_pairs_hook=reject_duplicate_pairs
    )
    source = json.loads(
        read_regular(args.source_identity, 16 * 1024).decode("utf-8"),
        object_pairs_hook=reject_duplicate_pairs,
    )
    if not isinstance(phase, dict) or phase.get("phase") != args.phase_name:
        raise SystemExit("phase document identity mismatch")
    if not isinstance(source, dict) or set(source) != set(BINDING_KEYS[1:]):
        raise SystemExit("source identity shape mismatch")
    if any(key in phase for key in BINDING_KEYS):
        raise SystemExit("phase document is already bound")
    phase["run_id"] = args.run_id
    for key in BINDING_KEYS[1:]:
        phase[key] = source[key]

    payload = (json.dumps(phase, indent=2, sort_keys=True) + "\n").encode()
    parent_descriptor = os.open(
        args.phase.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    temporary_name = f".{args.phase.name}.bound-{os.getpid()}"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o660,
            dir_fd=parent_descriptor,
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(
            temporary_name,
            args.phase.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
