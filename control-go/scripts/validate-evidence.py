#!/usr/bin/env python3
"""Strict, bounded JSON Schema validation for Go Control Core evidence.

Mirrors infer-cpp/scripts/validate-evidence.py (load/validate generic + module
kinds) with control-core constants. Soak evidence is validated by the dedicated
validate-soak-evidence.py, not here.
"""

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


def absolute_path_chain_has_symlink(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def load_object(path: Path) -> dict[str, Any]:
    if absolute_path_chain_has_symlink(path) or path.is_symlink() or not path.is_file():
        raise ValueError(f"unsafe or missing JSON input: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON_BYTES:
            raise ValueError(f"JSON input is not a bounded regular file: {path}")
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
            raise ValueError(f"JSON input changed while being read: {path}")
    finally:
        os.close(descriptor)
    value = json.loads(
        payload.decode("utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON input is not an object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def validate_schema(schema: dict[str, Any], document: dict[str, Any]) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        raise ValueError(
            "\n".join(
                f"schema error at {list(error.absolute_path)}: {error.message}"
                for error in errors
            )
        )


def validate_module(document: dict[str, Any]) -> None:
    if document.get("schema_version") != "control-core-module-gate-summary/v1":
        raise ValueError("module validation requires a control-core module gate summary")
    if document.get("result") != document.get("overall_status"):
        raise ValueError("module result and overall_status disagree")
    if document.get("overall_module_complete") is not True:
        return
    if document.get("result") not in {"PASS", "HOLD"}:
        raise ValueError("a complete module cannot have FAIL/NOT_RUN status")
    if document.get("blackbox_e2e", {}).get("result") != "PASS":
        raise ValueError("a complete module requires real-process black-box PASS")
    if int(document.get("findings", {}).get("open_p0", -1)) != 0:
        raise ValueError("a complete module requires zero open P0 findings")
    formal = document.get("qualification_gates", {}).get("formal_soak_3600_seconds", {})
    artifacts = document.get("artifact_digests", {})
    formal_digest = artifacts.get("formal_soak_evidence")
    # control-core process thresholds are owner-unfrozen, so the formal soak records
    # HOLD/NOT_QUALIFIED (the module-complete candidate state); a digest-bound formal
    # soak evidence file is still required.
    if (
        formal.get("result") not in {"PASS", "HOLD"}
        or formal.get("evidence") != "formal-soak/formal-soak-evidence.json"
        or formal.get("evidence_digest") != formal_digest
        or not isinstance(formal_digest, str)
        or not formal_digest.startswith("sha256:")
    ):
        raise ValueError("a complete module is not bound to a formal-soak evidence digest")
    for name in ("release_binary",):
        digest = artifacts.get(name)
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError(f"a complete module is missing the {name} digest")
    executed = document.get("executed_gates", {})
    for name in (
        "go_version",
        "public_contract_schema_golden_negative",
        "format",
        "vet",
        "staticcheck",
        "unit_property_contract_golden",
        "race",
        "coverage",
        "migration_integration",
        "release_build",
        "real_process_public_boundary_blackbox",
        "postgres_e2e",
        "formal_soak_validation",
    ):
        if executed.get(name) != "PASS":
            raise ValueError(f"a complete module requires {name}=PASS")
    if executed.get("oci_startup", {}).get("result") != "PASS":
        raise ValueError("a complete module requires oci_startup=PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--kind", choices=("generic", "module"), default="generic")
    args = parser.parse_args()

    schema = load_object(args.schema)
    document = load_object(args.document)
    validate_schema(schema, document)
    if args.kind == "module":
        validate_module(document)
    print(
        json.dumps(
            {
                "schema_version": "control-core-evidence-validation/v1",
                "document": str(args.document),
                "document_digest": sha256(args.document),
                "schema_digest": sha256(args.schema),
                "result": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())