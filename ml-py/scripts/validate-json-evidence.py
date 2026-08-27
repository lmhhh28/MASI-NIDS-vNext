#!/usr/bin/env python3
"""Validate one ordinary JSON evidence file against an ordinary schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not an ordinary file: {path}")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--expected-result", default="PASS")
    arguments = parser.parse_args()
    schema = load(arguments.schema)
    evidence = load(arguments.evidence)
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(evidence))
    if errors:
        raise SystemExit(
            "schema rejected evidence: "
            + "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors)
        )
    if arguments.expected_result and evidence.get("result") != arguments.expected_result:
        raise SystemExit(f"result mismatch: {evidence.get('result')} != {arguments.expected_result}")
    print(json.dumps({"schema_version": "json-evidence-validation/v1", "result": "PASS"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
