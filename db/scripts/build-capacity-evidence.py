#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    if raw.get("mode") != "capacity" or raw.get("result") != "PASS":
        raise ValueError("capacity workload did not pass")
    document = {
        "schema_version": "postgresql-state-capacity/v1",
        "run_id": args.run_id,
        "module_id": "MOD-DB-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS",
        "qualification": "QUALIFIED",
        "identity": raw["identity"],
        "matrix": raw["capacity"],
        "threshold_status": "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001",
    }
    if args.output.exists() or args.output.is_symlink() or not args.output.is_absolute():
        raise ValueError("output must be a fresh absolute path")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
