#!/usr/bin/env python3
"""Strict bounded JSON Schema validation for PostgreSQL State evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

MAX_JSON_BYTES = 67_108_864


def chain_has_symlink(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def load(path: Path) -> dict[str, Any]:
    if chain_has_symlink(path) or path.is_symlink() or not path.is_file():
        raise ValueError(f"unsafe or missing JSON input: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON_BYTES:
            raise ValueError(f"unbounded JSON input: {path}")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(1_048_576, MAX_JSON_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_JSON_BYTES:
                raise ValueError(f"JSON input exceeds {MAX_JSON_BYTES} bytes: {path}")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"JSON input changed while read: {path}")
    finally:
        os.close(descriptor)
    value = json.loads(
        payload.decode("utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON input is not an object: {path}")
    return value


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def validate_module(document: dict[str, Any]) -> None:
    if document.get("schema_version") != "postgresql-state-module-gate-summary/v1":
        raise ValueError("module kind requires PostgreSQL State summary")
    if document.get("overall_module_complete") is not True:
        return
    if document.get("result") not in {"PASS", "HOLD"}:
        raise ValueError("complete module cannot be FAIL/NOT_RUN")
    for name in ("blackbox_e2e", "recovery", "performance", "formal_soak"):
        if document.get(name, {}).get("result") != "PASS":
            raise ValueError(f"complete module requires {name}=PASS")
    if document.get("findings", {}).get("open_p0") != 0:
        raise ValueError("complete module requires zero open P0")
    completion = document.get("completion", {})
    if not all(completion.get(key) is True for key in (
        "operational_gates_pass", "open_p0_zero", "real_runtime_started",
        "formal_soak_executed", "no_required_not_run",
    )):
        raise ValueError("completion derivation is not fully true")
    if any(value != "PASS" for value in document.get("executed_gates", {}).values()):
        raise ValueError("complete module has a non-PASS executed gate")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--document", required=True, type=Path)
    parser.add_argument("--kind", choices=("generic", "module"), default="generic")
    args = parser.parse_args()
    schema, document = load(args.schema), load(args.document)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ValueError("\n".join(
            f"schema error at {list(error.absolute_path)}: {error.message}" for error in errors
        ))
    if args.kind == "module":
        validate_module(document)
    print(json.dumps({
        "schema_version": "postgresql-state-evidence-validation/v1",
        "document": str(args.document),
        "document_digest": digest(args.document),
        "schema_digest": digest(args.schema),
        "result": "PASS",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
