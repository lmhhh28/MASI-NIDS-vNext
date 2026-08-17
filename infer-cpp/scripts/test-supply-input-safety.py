#!/usr/bin/env python3
"""Negative vectors for the Central Inference supply input boundary."""

from __future__ import annotations

import io
import json
import runpy
import tarfile
import tempfile
from pathlib import Path
from typing import Any


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with tarfile.open(path, "w") as archive:
        for member, payload in members:
            archive.addfile(member, io.BytesIO(payload) if member.isfile() else None)


def regular(name: str, payload: bytes = b"bounded\n") -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = 0o644
    return member, payload


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    verifier: dict[str, Any] = runpy.run_path(
        str(script_dir / "verify-supply-chain.py"), run_name="central_supply_verifier_library"
    )
    safe_relative = verifier["safe_relative"]
    safe_tar_archive = verifier["safe_tar_archive"]
    rejected: list[str] = []
    with tempfile.TemporaryDirectory(prefix="masi-inf-supply-safety-") as directory:
        root = Path(directory)
        require(safe_relative("root/file.txt"), "safe path was rejected")
        require(not safe_relative("../escape"), "path traversal was accepted")
        rejected.append("path-traversal")

        valid = root / "valid.tar"
        write_tar(valid, [regular("root/file.txt")])
        require(safe_tar_archive(valid), "safe tar was rejected")

        traversal = root / "traversal.tar"
        write_tar(traversal, [regular("../escape")])
        require(not safe_tar_archive(traversal), "tar traversal was accepted")
        rejected.append("tar-traversal")

        symbolic = root / "symbolic.tar"
        link = tarfile.TarInfo("root/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "file.txt"
        write_tar(symbolic, [(link, b"")])
        require(not safe_tar_archive(symbolic), "tar symlink was accepted")
        rejected.append("symlink")

        duplicate = root / "duplicate.tar"
        write_tar(duplicate, [regular("root/file", b"one"), regular("root/file", b"two")])
        require(not safe_tar_archive(duplicate), "duplicate tar member was accepted")
        rejected.append("duplicate")

        special = root / "special.tar"
        fifo = tarfile.TarInfo("root/fifo")
        fifo.type = tarfile.FIFOTYPE
        write_tar(special, [(fifo, b"")])
        require(not safe_tar_archive(special), "special tar member was accepted")
        rejected.append("special-file")

    print(
        json.dumps(
            {
                "schema_version": "central-inference-supply-input-safety/v1",
                "test_id": "SEC-SUPPLY-NEGATIVE-001",
                "result": "PASS",
                "rejected_cases": rejected,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
