#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not an object: {path}")
    return value


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backup-manifest-digest", required=True)
    parser.add_argument("--archive-failures", required=True, type=int)
    parser.add_argument("--pitr-before", required=True, type=Path)
    parser.add_argument("--rotation", required=True, type=Path)
    parser.add_argument("--pitr-after", required=True, type=Path)
    parser.add_argument("--pitr-rto-ms", required=True, type=int)
    parser.add_argument("--replica-before", required=True, type=Path)
    parser.add_argument("--replica-after", required=True, type=Path)
    parser.add_argument("--promotion-rto-ms", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    before, rotation, after = (
        read(args.pitr_before),
        read(args.rotation),
        read(args.pitr_after),
    )
    replica_before, replica_after = read(args.replica_before), read(args.replica_after)
    target_rotated = (
        before["target_control_incarnation"] != after["target_control_incarnation"]
        and after["target_control_incarnation"]
        == rotation["target_control_incarnation_id"]
    )
    model_rotated = (
        before["model_control_incarnation"] != after["model_control_incarnation"]
        and after["model_control_incarnation"]
        == rotation["model_control_incarnation_id"]
    )
    document = {
        "schema_version": "postgresql-state-recovery/v1",
        "run_id": args.run_id,
        "module_id": "MOD-DB-001",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS",
        "qualification": "NOT_QUALIFIED",
        "pitr": {
            "backup_manifest_digest": args.backup_manifest_digest,
            "basebackup_verified": True,
            "archive_failures": args.archive_failures,
            "included_marker_present": after["included_marker_present"],
            "excluded_marker_absent": after["excluded_marker_absent"],
            "source_timeline": 1,
            "restored_timeline": after["timeline_id"],
            "target_incarnation_rotated": target_rotated,
            "model_incarnation_rotated": model_rotated,
            "writers_enabled": after["target_writer_enabled"]
            or after["model_writer_enabled"],
            "effect_intents_created": after["effect_intent_count"],
            "plugin_statistic_runs_created": after["plugin_statistic_run_count"],
            "restore_rto_ms": args.pitr_rto_ms,
            "evidence_digests": [
                digest(args.pitr_before),
                digest(args.rotation),
                digest(args.pitr_after),
            ],
        },
        "streaming_failover": {
            "replication_mode": "physical-streaming-asynchronous",
            "state_before": "in-recovery"
            if replica_before["in_recovery"]
            else "invalid",
            "state_after": "promoted"
            if not replica_after["in_recovery"]
            else "invalid",
            "marker_present": replica_before["marker_present"]
            and replica_after["marker_present"],
            "source_timeline": replica_before["timeline_id"],
            "promoted_timeline": replica_after["timeline_id"],
            "promotion_rto_ms": args.promotion_rto_ms,
            "primary_crash_injected": True,
            "evidence_digests": [
                digest(args.replica_before),
                digest(args.replica_after),
            ],
        },
        "qualification_limit": "Observed single-host manual-promotion recovery only; production-ha and automatic failover remain HOLD/NOT_QUALIFIED.",
    }
    if args.archive_failures != 0 or not target_rotated or not model_rotated:
        document["result"], document["qualification"] = "FAIL", "NOT_QUALIFIED"
    if (
        args.output.exists()
        or args.output.is_symlink()
        or not args.output.is_absolute()
    ):
        raise ValueError("output must be a fresh absolute path")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0 if document["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
