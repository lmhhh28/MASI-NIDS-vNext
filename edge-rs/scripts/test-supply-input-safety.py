#!/usr/bin/env python3
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


def regular_member(name: str, payload: bytes = b"bounded\n") -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = 0o644
    return member, payload


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    verifier: dict[str, Any] = runpy.run_path(
        str(script_dir / "verify-supply-chain.py"), run_name="supply_verifier_library"
    )
    safe_relative = verifier["safe_relative"]
    absolute_path_chain_has_symlink = verifier["absolute_path_chain_has_symlink"]
    regular_file_within = verifier["regular_file_within"]
    safe_tar_archive = verifier["safe_tar_archive"]
    sorted_file_digest = verifier["sorted_file_digest"]
    max_json_bytes = int(verifier["MAX_JSON_BYTES"])
    max_path_bytes = int(verifier["MAX_PATH_BYTES"])

    rejected: list[str] = []
    with tempfile.TemporaryDirectory(prefix="masi-edge-supply-safety-") as directory:
        root = Path(directory)
        checks = root / "checks"
        checks.mkdir()
        canonical = checks / "canonical.json"
        canonical.write_text('{"frozen":true}\n', encoding="utf-8")

        digest, file_count, total_bytes = sorted_file_digest(checks)
        require(
            isinstance(digest, str) and file_count == 1 and total_bytes > 0,
            "bounded regular checks directory was not accepted",
        )
        require(
            regular_file_within(checks, "canonical.json", max_bytes=max_json_bytes)
            == canonical,
            "bounded regular file was not accepted",
        )

        symbolic = checks / "symbolic.json"
        symbolic.symlink_to(canonical)
        require(
            sorted_file_digest(checks) == (None, 0, 0),
            "checks directory symlink was accepted",
        )
        require(
            regular_file_within(checks, "symbolic.json", max_bytes=max_json_bytes)
            is None,
            "symbolic inventory file was accepted",
        )
        rejected.extend(["checks-symlink", "inventory-symlink"])
        symbolic.unlink()

        symbolic_root = root / "symbolic-root"
        symbolic_root.symlink_to(checks, target_is_directory=True)
        require(
            absolute_path_chain_has_symlink(symbolic_root / "canonical.json"),
            "symbolic root path component was not detected",
        )
        rejected.append("root-path-symlink")

        require(not safe_relative("../escape"), "parent traversal was accepted")
        require(
            safe_relative("vendor/example/output.repr(C).rs"),
            "frozen Cargo vendor punctuation was rejected",
        )
        require(not safe_relative("/absolute"), "absolute path was accepted")
        require(not safe_relative("a//b"), "noncanonical path was accepted")
        require(
            not safe_relative("x" * (max_path_bytes + 1)),
            "oversized path was accepted",
        )
        rejected.extend(
            ["parent-traversal", "absolute-path", "noncanonical-path", "oversized-path"]
        )

        valid_archive = root / "valid.tar"
        write_tar(valid_archive, [regular_member("root/file.txt")])
        require(safe_tar_archive(valid_archive), "bounded regular tar was not accepted")

        traversal_archive = root / "traversal.tar"
        write_tar(traversal_archive, [regular_member("../escape")])
        require(
            not safe_tar_archive(traversal_archive), "tar parent traversal was accepted"
        )
        rejected.append("tar-parent-traversal")

        symbolic_archive = root / "symbolic.tar"
        link = tarfile.TarInfo("root/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "file.txt"
        write_tar(symbolic_archive, [(link, b"")])
        require(not safe_tar_archive(symbolic_archive), "tar symlink was accepted")
        rejected.append("tar-symlink")

        duplicate_archive = root / "duplicate.tar"
        write_tar(
            duplicate_archive,
            [regular_member("root/file.txt", b"one"), regular_member("root/file.txt", b"two")],
        )
        require(not safe_tar_archive(duplicate_archive), "duplicate tar path was accepted")
        rejected.append("tar-duplicate")

        special_archive = root / "special.tar"
        fifo = tarfile.TarInfo("root/fifo")
        fifo.type = tarfile.FIFOTYPE
        write_tar(special_archive, [(fifo, b"")])
        require(not safe_tar_archive(special_archive), "tar special file was accepted")
        rejected.append("tar-special-file")

        archive_symlink = root / "archive-link.tar"
        archive_symlink.symlink_to(valid_archive)
        require(not safe_tar_archive(archive_symlink), "symbolic tar input was accepted")
        rejected.append("tar-input-symlink")

    print(
        json.dumps(
            {
                "schema_version": "rust-edge-agent-supply-input-safety/v1",
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
