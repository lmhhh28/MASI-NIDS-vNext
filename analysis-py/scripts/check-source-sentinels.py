#!/usr/bin/env python3
"""Reject unfinished or ownership-expanding Analysis implementation paths."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    unfinished = re.compile(r"\b(?:TODO|FIXME|placeholder|hardcoded\s+success)\b|NotImplementedError", re.IGNORECASE)
    failures: list[str] = []
    candidates = [*sorted((root / "src").rglob("*.py")), root / "Dockerfile"]
    for path in candidates:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if unfinished.search(line):
                failures.append(f"{path.relative_to(root)}:{line_number}")
    if failures:
        raise SystemExit("unfinished Analysis implementation sentinel(s): " + ", ".join(failures))

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    if any("==" not in item for item in dependencies):
        raise SystemExit("all direct Analysis dependencies must be exact pins")
    forbidden_dependencies = {"asyncpg", "boto3", "docker", "kubernetes", "paramiko", "psycopg", "psycopg2", "p4runtime", "pytest", "sqlalchemy"}
    names = {item.split("==", 1)[0].lower().replace("_", "-") for item in dependencies}
    unexpected = sorted(names & forbidden_dependencies)
    if unexpected:
        raise SystemExit("forbidden ownership-bearing dependency: " + ", ".join(unexpected))

    source = "\n".join(path.read_text(encoding="utf-8") for path in sorted((root / "src").rglob("*.py")))
    forbidden_code = (
        r"(?m)^\s*(?:from|import)\s+(?:asyncpg|docker|kubernetes|p4runtime|psycopg|sqlalchemy)\b",
        r"\bsubprocess\.",
        r"\bos\.system\s*\(",
        r"\beval\s*\(",
        r"\bexec\s*\(",
        r"/var/run/docker\.sock",
    )
    for forbidden in forbidden_code:
        if re.search(forbidden, source):
            raise SystemExit(f"forbidden core, device, process, or dynamic-code path in Analysis source: {forbidden}")
    graph = (root / "src/masi_analysis/graph.py").read_text(encoding="utf-8")
    if "StateGraph" not in graph or "compile()" not in graph:
        raise SystemExit("fixed real LangGraph topology is absent")
    print("analysis source sentinels: PASS")


if __name__ == "__main__":
    main()
