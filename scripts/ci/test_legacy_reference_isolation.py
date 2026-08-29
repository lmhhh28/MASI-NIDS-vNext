#!/usr/bin/env python3
"""Fail closed when the legacy frontend snapshot or its isolation drifts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = REPOSITORY_ROOT / "legacy-reference" / "masi-nids-frontend"
SOURCE_ROOT = SNAPSHOT_ROOT / "source"
MANIFEST_PATH = SNAPSHOT_ROOT / "files.sha256"
METADATA_PATH = SNAPSHOT_ROOT / "SNAPSHOT.json"
REQUIRED_FILE_COUNT = 236
REQUIRED_TOTAL_BYTES = 1_905_214
REQUIRED_MANIFEST_DIGEST = "e65aa4cbfbe0c6a67d47eef7afd8d3058e1dea7e2162252e0680e628c71e4d67"

FORBIDDEN_SEGMENTS = {
    ".next",
    "node_modules",
    "coverage",
    "build",
    "dist",
    "out",
    "test-results",
}
RUNTIME_ROOTS = (
    "analysis-py",
    "contracts",
    "control-go",
    "db",
    "deploy",
    "edge-rs",
    "infer-cpp",
    "ml-py",
    "p4",
    "plugin-host-rs",
    "testkit",
    "web",
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    re.compile(rb"npm_[A-Za-z0-9]{20,}"),
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def regular_files(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in names:
            candidate = directory_path / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise AssertionError(f"snapshot directory symlink rejected: {candidate.relative_to(root)}")
            if not stat.S_ISDIR(mode):
                raise AssertionError(f"snapshot special directory entry rejected: {candidate.relative_to(root)}")
        for name in filenames:
            candidate = directory_path / name
            mode = candidate.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise AssertionError(f"snapshot non-regular file rejected: {candidate.relative_to(root)}")
            relative = candidate.relative_to(root).as_posix()
            result[relative] = candidate
    return result


def load_manifest() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, raw_line in enumerate(MANIFEST_PATH.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  \./(.+)", raw_line)
        if match is None:
            raise AssertionError(f"malformed manifest line {line_number}")
        digest, relative = match.groups()
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise AssertionError(f"unsafe manifest path at line {line_number}: {relative}")
        if relative in entries:
            raise AssertionError(f"duplicate manifest path: {relative}")
        entries[relative] = digest
    if list(entries) != sorted(entries):
        raise AssertionError("manifest paths are not byte-sorted")
    return entries


class LegacyReferenceIsolationTest(unittest.TestCase):
    maxDiff = None

    def test_manifest_metadata_and_files_are_exact(self) -> None:
        metadata = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
        manifest = load_manifest()
        actual = regular_files(SOURCE_ROOT)

        self.assertEqual(set(manifest), set(actual))
        self.assertEqual(len(actual), REQUIRED_FILE_COUNT)
        self.assertEqual(sum(path.stat().st_size for path in actual.values()), REQUIRED_TOTAL_BYTES)
        self.assertEqual(sha256_bytes(MANIFEST_PATH.read_bytes()), REQUIRED_MANIFEST_DIGEST)
        for relative, expected_digest in manifest.items():
            self.assertEqual(sha256_bytes(actual[relative].read_bytes()), expected_digest, relative)

        self.assertEqual(metadata["capture"]["file_count"], REQUIRED_FILE_COUNT)
        self.assertEqual(metadata["capture"]["total_bytes"], REQUIRED_TOTAL_BYTES)
        self.assertEqual(
            metadata["capture"]["file_manifest_sha256"], f"sha256:{REQUIRED_MANIFEST_DIGEST}"
        )
        self.assertEqual(metadata["runtime_adoption"], "REJECT")
        self.assertEqual(metadata["qualification"], "NOT_QUALIFIED")
        self.assertFalse(metadata["license"]["downstream_license_grant"])
        self.assertTrue(metadata["known_risks"]["direct_docker_build_technically_possible"])
        self.assertTrue(metadata["known_risks"]["npm_ci_runs_dependency_install_scripts"])
        self.assertFalse(metadata["known_risks"]["backend_ssrf_enforcement_audited"])

    def test_generated_secret_and_special_inputs_are_absent(self) -> None:
        for relative, path in regular_files(SOURCE_ROOT).items():
            parts = set(PurePosixPath(relative).parts)
            self.assertFalse(parts & FORBIDDEN_SEGMENTS, relative)
            name = PurePosixPath(relative).name
            self.assertFalse(name == ".env" or name.startswith(".env."), relative)
            self.assertFalse(name.endswith((".pem", ".key", ".crt", ".p12", ".pfx")), relative)
            payload = path.read_bytes()
            for pattern in SECRET_PATTERNS:
                self.assertIsNone(pattern.search(payload), f"secret-shaped value in {relative}")

    def test_vnext_build_and_runtime_roots_do_not_reference_snapshot(self) -> None:
        dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        patterns = {line.strip().lstrip("/").rstrip("/") for line in dockerignore if line.strip() and not line.lstrip().startswith("#")}
        self.assertIn("legacy-reference", patterns)
        self.assertTrue((SNAPSHOT_ROOT / "AGENTS.md").is_file())

        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", *RUNTIME_ROOTS],
            cwd=REPOSITORY_ROOT,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.split(b"\0")
        offenders: list[str] = []
        for encoded in tracked:
            if not encoded:
                continue
            relative = encoded.decode("utf-8")
            path = REPOSITORY_ROOT / relative
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                continue
            if b"legacy-reference" in path.read_bytes():
                offenders.append(relative)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
