#!/usr/bin/env python3
"""Validate the Offline ML findings registry and require no open finding."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    arguments = parser.parse_args()
    repo = arguments.repo.resolve(strict=True)
    schema = json.loads((repo / "contracts/evidence/module-findings/v1/schema.json").read_text(encoding="utf-8"))
    registry = cast(dict[str, Any], json.loads((repo / "ml-py/module-findings.json").read_text(encoding="utf-8")))
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(registry))
    if errors:
        raise SystemExit("findings schema failure: " + "; ".join(error.message for error in errors))
    opened = [item for item in registry["findings"] if item["status"] == "OPEN"]
    if opened:
        raise SystemExit("open findings: " + ",".join(str(item["finding_id"]) for item in opened))
    print(json.dumps({"schema_version": "offline-ml-findings-validation/v1", "result": "PASS", "open": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
