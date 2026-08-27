"""Command-line lifecycle and local health probe."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from .config import load_runtime_material
from .constants import VERSION
from .errors import AnalysisError
from .security import read_bounded_regular_file


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "message_code": getattr(record, "message_code", "LOG"),
            "component": "masi-analysis",
        }
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="masi-analysis")
    parser.add_argument("--config")
    parser.add_argument("--probe", choices=("startup", "ready", "live"))
    parser.add_argument("--version", action="store_true")
    return parser


def _probe(config_path: str, name: str) -> int:
    material = load_runtime_material(config_path)
    try:
        raw = read_bounded_regular_file(material.config.health_state_path, max_bytes=64 * 1024)
        health: Any = json.loads(raw)
    except (AnalysisError, json.JSONDecodeError, UnicodeDecodeError):
        return 1
    if not isinstance(health, dict) or health.get("schema_version") != "masi-analysis-health/v1":
        return 1
    pid = health.get("pid")
    updated = health.get("updated_at_unix_ms")
    if not isinstance(pid, int) or not isinstance(updated, int) or time.time_ns() // 1_000_000 - updated > 5000:
        return 1
    try:
        os.kill(pid, 0)
    except OSError:
        return 1
    return 0 if health.get(name) is True else 1


async def _run(config_path: str) -> int:
    from .server import AnalysisHTTPServer

    material = load_runtime_material(config_path)
    await AnalysisHTTPServer(material, config_path).run()
    return 0


def main() -> None:
    os.umask(0o077)
    args = _parser().parse_args()
    if args.version:
        print(VERSION)
        raise SystemExit(0)
    if not args.config:
        print("--config is required", file=sys.stderr)
        raise SystemExit(2)
    logging.root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.INFO)
    if args.probe:
        raise SystemExit(_probe(args.config, args.probe))
    try:
        raise SystemExit(asyncio.run(_run(args.config)))
    except AnalysisError as exc:
        logging.getLogger("masi_analysis").error("analysis_start_failed", extra={"message_code": exc.code})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
