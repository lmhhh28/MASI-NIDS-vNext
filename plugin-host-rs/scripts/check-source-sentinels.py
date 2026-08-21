#!/usr/bin/env python3
"""Reject unfinished implementation sentinels in the Plugin Host source tree."""

from __future__ import annotations

import re
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(
        r"\b(?:TODO|FIXME|placeholder|stub|hardcoded\s+success)\b|todo!\s*\(|unimplemented!\s*\(",
        re.IGNORECASE,
    )
    failures: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in {"target", "evidence", ".cargo-vendor"} for part in path.parts):
            continue
        if path.suffix not in {".rs", ".py", ".sh", ".md", ".toml", ".json"} and path.name != "Dockerfile":
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if path.name == "Cargo.toml" and re.match(r"^todo\s*=\s*\"deny\"$", line.strip()):
                continue
            if pattern.search(line) and path.name != Path(__file__).name:
                failures.append(f"{path.relative_to(root)}:{line_number}")
    if failures:
        raise SystemExit("unfinished implementation sentinel(s): " + ", ".join(failures))
    cargo_text = (root / "Cargo.toml").read_text(encoding="utf-8")
    dependency_block = cargo_text.split("[dependencies]", 1)[1].split("\n[", 1)[0]
    forbidden_dependencies = {
        "bollard", "diesel", "kube", "reqwest", "sqlx", "tokio-postgres"
    }
    observed_dependencies = {
        match.group(1)
        for line in dependency_block.splitlines()
        if (match := re.match(r"^([A-Za-z0-9_-]+)\s*=", line))
    }
    unexpected = sorted(observed_dependencies & forbidden_dependencies)
    if unexpected:
        raise SystemExit("forbidden ownership-bearing dependencies: " + ", ".join(unexpected))
    rust_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / "src").rglob("*.rs"))
    )
    for forbidden in [
        "/var/run/docker.sock", "effect_intents", "p4runtime::",
        "tonic::include_proto!(\"p4"
    ]:
        if forbidden in rust_source.lower():
            raise SystemExit(f"forbidden core/orchestrator ownership path in Rust source: {forbidden}")
    compose = (root.parent / "deploy/plugin-host/compose.acceptance.yaml").read_text(
        encoding="utf-8"
    )
    for forbidden in ["docker.sock", "privileged: true", "network_mode: host", "host_pid"]:
        if forbidden in compose.lower():
            raise SystemExit(f"forbidden deployment authority: {forbidden}")
    print("plugin-host source sentinels: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
