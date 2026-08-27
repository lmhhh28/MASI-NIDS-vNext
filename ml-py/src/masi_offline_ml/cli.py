"""Command-line entrypoint for the Offline ML qualification pipeline."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .canonical import ValidationError
from .contracts import validate_public_contracts
from .pipeline import run_pipeline, verify_pipeline_output


def _terminate(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt("termination requested")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="masi-offline-ml")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    contracts = subcommands.add_parser("validate-contracts", help="validate public dataset/explanation contracts")
    contracts.add_argument("--repo", type=Path, required=True)

    run = subcommands.add_parser("run", help="run the real nine-candidate pipeline")
    run.add_argument("--repo", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)

    verify = subcommands.add_parser("verify", help="verify an existing pipeline output")
    verify.add_argument("--repo", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _terminate)
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate-contracts":
            result = validate_public_contracts(arguments.repo)
        elif arguments.command == "run":
            result = run_pipeline(arguments.repo, arguments.output)
        elif arguments.command == "verify":
            result = verify_pipeline_output(arguments.repo, arguments.output)
        else:
            raise ValidationError("command", str(arguments.command))
    except (ValidationError, FileNotFoundError, PermissionError, OSError) as error:
        print(json.dumps({"result": "FAIL", "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"result": "HOLD", "error": "interrupted"}, sort_keys=True), file=sys.stderr)
        return 130
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
