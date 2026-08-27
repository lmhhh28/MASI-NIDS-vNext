#!/usr/bin/env python3
"""Fail closed on repository files that are unsafe or unsuitable for GitHub."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_MAX_BYTES = 95 * 1024 * 1024
FORBIDDEN_SEGMENTS = {
    ".playwright-cli",
    ".pyright",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "output",
}
PRIVATE_KEY_SUFFIXES = {".jks", ".key", ".p12", ".pfx", ".pem"}
PRIVATE_KEY_RE = re.compile(
    rb"(?m)^-----BEGIN (?:(?:[A-Z0-9]+ )*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----\r?$"
)
TOKEN_PATTERNS = {
    "AWS access key": re.compile(
        rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"
    ),
    "GitHub token": re.compile(
        rb"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}(?![A-Za-z0-9])"
    ),
    "OpenAI-style secret": re.compile(
        rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])"
    ),
}
ACTION_USE_RE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def git_paths(root: Path, include_untracked: bool) -> list[Path]:
    command = ["git", "-C", str(root), "ls-files", "-z"]
    if include_untracked:
        command.extend(["--cached", "--others", "--exclude-standard"])
    output = subprocess.run(command, check=True, stdout=subprocess.PIPE).stdout
    return sorted({root / os.fsdecode(item) for item in output.split(b"\0") if item})


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def unsafe_name(path: Path) -> str | None:
    parts = set(path.parts)
    forbidden = sorted(parts & FORBIDDEN_SEGMENTS)
    if forbidden:
        return f"generated/cache path segment: {forbidden[0]}"
    if (
        path.parts
        and path.parts[0] in {"edge-rs", "plugin-host-rs"}
        and "target" in path.parts
    ):
        return "Rust target directory"
    name = path.name.lower()
    if path.suffix.lower() in PRIVATE_KEY_SUFFIXES:
        return "private-key or keystore filename"
    if name in {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}:
        return "SSH private-key filename"
    if name == ".env" or (
        name.startswith(".env.")
        and not name.endswith((".example", ".sample", ".template"))
    ):
        return "environment file may contain credentials"
    return None


def scan_content(path: Path) -> str | None:
    overlap = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            payload = overlap + chunk
            if PRIVATE_KEY_RE.search(payload):
                return "private-key material"
            for label, pattern in TOKEN_PATTERNS.items():
                if pattern.search(payload):
                    return label
            overlap = payload[-256:]
    return None


def check_workflows(root: Path, issues: list[str]) -> None:
    workflow_root = root / ".github" / "workflows"
    if not workflow_root.is_dir():
        return
    for workflow in sorted(
        [*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")]
    ):
        text = workflow.read_text(encoding="utf-8")
        rel = relative(root, workflow)
        if re.search(r"(?m)^\s*pull_request_target\s*:", text):
            issues.append(f"{rel}: pull_request_target is forbidden")
        if re.search(r"(?m)^\s*permissions\s*:\s*write-all\s*$", text):
            issues.append(f"{rel}: permissions: write-all is forbidden")
        for match in ACTION_USE_RE.finditer(text):
            action = match.group(1).strip("'\"")
            if action.startswith("./"):
                continue
            if "@" not in action:
                issues.append(f"{rel}: action has no immutable ref: {action}")
                continue
            name, ref = action.rsplit("@", 1)
            if not FULL_SHA_RE.fullmatch(ref):
                issues.append(
                    f"{rel}: action is not pinned to a full commit SHA: {name}"
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--include-untracked", action="store_true")
    parser.add_argument(
        "--max-file-mib", type=int, default=DEFAULT_MAX_BYTES // (1024 * 1024)
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    max_bytes = arguments.max_file_mib * 1024 * 1024
    issues: list[str] = []

    for path in git_paths(root, arguments.include_untracked):
        rel_path = path.relative_to(root)
        rel = rel_path.as_posix()
        if rel_path.is_absolute() or ".." in rel_path.parts:
            issues.append(f"{rel}: path escapes repository")
            continue
        reason = unsafe_name(rel_path)
        if reason:
            issues.append(f"{rel}: {reason}")
            continue
        if path.is_symlink():
            target = Path(os.readlink(path))
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                issues.append(f"{rel}: symbolic link escapes repository")
            continue
        if not path.exists():
            issues.append(f"{rel}: indexed path is missing")
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > max_bytes:
            issues.append(
                f"{rel}: {size} bytes exceeds {arguments.max_file_mib} MiB limit"
            )
            continue
        content_issue = scan_content(path)
        if content_issue:
            issues.append(f"{rel}: detected {content_issue}; value suppressed")

    check_workflows(root, issues)
    if issues:
        print("repository hygiene check failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    scope = (
        "tracked and untracked candidates"
        if arguments.include_untracked
        else "tracked files"
    )
    print(
        f"repository hygiene check passed for {scope} (max file {arguments.max_file_mib} MiB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
