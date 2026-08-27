#!/usr/bin/env python3
"""Verify that a successful formal gate emitted its canonical summary."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


MODULE_PATHS = {
    "p4": Path("evidence/p4-switch/{run_id}/qualification-evidence.json"),
    "edge": Path("edge-rs/evidence/module-gates/runs/{run_id}/gate-summary.json"),
    "inference": Path(
        "infer-cpp/evidence/module-gates/runs/{run_id}/gate-summary.json"
    ),
    "control": Path("control-go/evidence/module-gates/runs/{run_id}/gate-summary.json"),
    "db": Path("db/evidence/module-gates/runs/{run_id}/gate-summary.json"),
    "plugin-host": Path(
        "plugin-host-rs/evidence/module-gates/runs/{run_id}/gate-summary.json"
    ),
    "analysis": Path(
        "analysis-py/evidence/module-gates/runs/{run_id}/gate-summary.json"
    ),
    "offline-ml": Path("ml-py/evidence/module-gates/runs/{run_id}/module-summary.json"),
    "web": Path("web/evidence/module-gates/runs/{run_id}/gate-summary.json"),
}


def require_string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"summary lacks non-empty {key}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("module", choices=sorted(MODULE_PATHS))
    parser.add_argument("run_id")
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[2]
    )
    arguments = parser.parse_args()
    repo = arguments.repo.resolve()
    relative = Path(str(MODULE_PATHS[arguments.module]).format(run_id=arguments.run_id))
    summary = repo / relative
    if summary.is_symlink() or not summary.is_file():
        raise SystemExit(f"canonical gate summary is absent or unsafe: {relative}")
    document = json.loads(summary.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("canonical gate summary must be a JSON object")
    require_string(document, "schema_version")
    qualification = require_string(document, "qualification")
    if qualification not in {"QUALIFIED", "NOT_QUALIFIED"}:
        raise SystemExit(f"invalid qualification: {qualification}")
    if document.get("overall_module_complete") is not True:
        raise SystemExit(
            "successful formal gate did not derive overall_module_complete=true"
        )
    top_level_run_id = document.get("run_id")
    if top_level_run_id is not None and top_level_run_id != arguments.run_id:
        raise SystemExit("canonical gate summary run_id does not match requested run")
    digest = hashlib.sha256(summary.read_bytes()).hexdigest()
    print(f"verified {relative} sha256:{digest} qualification={qualification}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
