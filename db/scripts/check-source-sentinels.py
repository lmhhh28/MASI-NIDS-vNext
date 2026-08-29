#!/usr/bin/env python3
"""Reject live implementation sentinels while auditing immutable migration 0001."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


TOKEN = re.compile(
    "|".join(
        (
            "TO" + "DO",
            "FIX" + "ME",
            "place" + "holder",
            "st" + "ub",
            "hardcoded success",
            "temporary compatibility",
        )
    ),
    re.IGNORECASE,
)
ALLOWED_IMMUTABLE_LINES = {
    81: "-- Incident rollup (Phase 2 stub table; full aggregation wired with rule/model",
    110: "VALUES ('version', '1', 'placeholder-checksum', 'placeholder-source-digest')",
}
TEXT_SUFFIXES = {".go", ".json", ".md", ".py", ".sh", ".sql", ".yaml", ".yml"}
ZERO_DIGEST = "sha256:" + "0" * 64
ALLOWED_ZERO_DEFAULTS = {
    ("db/migrations/0025_go_control_review_hardening.sql", 88),
    ("db/migrations/0025_go_control_review_hardening.sql", 105),
    ("db/migrations/0025_go_control_review_hardening.sql", 108),
    ("db/migrations/0027_analysis_a2a_polling.sql", 29),
    ("db/migrations/0027_analysis_a2a_polling.sql", 32),
}


def candidate(path: Path) -> bool:
    return path.name == "Dockerfile" or path.suffix in TEXT_SUFFIXES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    roots = (repo / "db", repo / "deploy/postgresql-state")
    immutable = repo / "db/migrations/0001_core_event.sql"
    findings = repo / "db/module-findings.json"
    self_path = Path(__file__).resolve(strict=True)
    observed_allowed: dict[int, str] = {}
    observed_zero_defaults: set[tuple[str, int]] = set()
    unexpected: list[str] = []

    for root in roots:
        for path in sorted(root.rglob("*")):
            relative_parts = path.relative_to(root).parts
            if (
                path in {self_path, findings}
                or any(
                    part in {"evidence", "out", "__pycache__"}
                    for part in relative_parts
                )
                or path.is_symlink()
                or not path.is_file()
                or not candidate(path)
            ):
                continue
            if path.stat().st_size > 8_388_608:
                raise ValueError(f"source sentinel input exceeds 8 MiB: {path}")
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                relative = path.relative_to(repo).as_posix()
                if ZERO_DIGEST in line:
                    if relative == "db/migrations/0031_reject_zero_digest_defaults.sql":
                        pass
                    elif (
                        relative,
                        number,
                    ) in ALLOWED_ZERO_DEFAULTS and "DEFAULT" in line:
                        observed_zero_defaults.add((relative, number))
                    else:
                        unexpected.append(
                            f"{relative}:{number}:unexpected all-zero digest"
                        )
                if not TOKEN.search(line):
                    continue
                if path == immutable and ALLOWED_IMMUTABLE_LINES.get(number) == line:
                    observed_allowed[number] = line
                    continue
                unexpected.append(f"{path.relative_to(repo)}:{number}:{line[:256]}")
    if observed_allowed != ALLOWED_IMMUTABLE_LINES:
        raise ValueError("immutable migration sentinel audit mismatch")
    if observed_zero_defaults != ALLOWED_ZERO_DEFAULTS:
        raise ValueError("historical zero-digest default audit mismatch")
    if unexpected:
        raise ValueError(
            "live implementation sentinel(s):\n" + "\n".join(unexpected[:64])
        )

    runner = (repo / "db/internal/migrate/runner.go").read_text(encoding="utf-8")
    blackbox = (repo / "db/tests/blackbox/blackbox_test.go").read_text(encoding="utf-8")
    incident = (
        repo / "db/migrations/0014_event_outcome_incident_projection.sql"
    ).read_text(encoding="utf-8")
    zero_hardening = (
        repo / "db/migrations/0031_reject_zero_digest_defaults.sql"
    ).read_text(encoding="utf-8")
    if (
        "SET checksum=$1,source_digest=$2" not in runner
        or '"migration-chain/"+cfg.ExpectedSchemaVersion' not in runner
    ):
        raise ValueError(
            "migration runner no longer publishes the exact final chain digest"
        )
    if "inspection.SchemaChainDigest != first.ChainDigest" not in blackbox:
        raise ValueError("blackbox no longer asserts the published chain digest")
    if "CREATE TABLE IF NOT EXISTS incident_projection_events" not in incident:
        raise ValueError(
            "incident projection did not supersede the immutable phase-2 bootstrap comment"
        )
    for required in (
        "firewall_activation_expected_cas_nonzero_v22",
        "fleet_operation_digest_nonzero_v22",
        "analysis_artifact_digest_nonzero_v22",
        "ALTER COLUMN expected_cas_digest DROP DEFAULT",
        "ALTER COLUMN operation_digest DROP DEFAULT",
        "ALTER COLUMN input_digest DROP DEFAULT",
    ):
        if required not in zero_hardening:
            raise ValueError(
                "schema v22 no longer closes historical zero-digest defaults"
            )
    print("postgresql-state-source-sentinels-ok live=0 immutable_audited=7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
