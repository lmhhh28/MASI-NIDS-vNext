#!/usr/bin/env python3
"""Validate Offline ML public contracts and semantic golden vectors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from masi_offline_ml.canonical import ValidationError, write_fresh_json
from masi_offline_ml.contracts import validate_public_contracts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = validate_public_contracts(args.repo)
        if args.output is not None:
            write_fresh_json(args.output, result)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except ValidationError as error:
        print(
            json.dumps(
                {
                    "schema_version": "offline-ml-contract-validation/v1",
                    "result": "FAIL",
                    "error": error.code,
                    "detail": error.detail,
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
