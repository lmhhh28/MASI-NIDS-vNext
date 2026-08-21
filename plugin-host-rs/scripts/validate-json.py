#!/usr/bin/env python3
"""Validate one JSON document against one Draft 2020-12 schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    args = parser.parse_args()
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    document = json.loads(args.document.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        for error in errors:
            print(f"{list(error.absolute_path)}: {error.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
