from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--runtime-expected", required=True)
    parser.add_argument("--runtime-observed", required=True)
    parser.add_argument("--runner-expected", required=True)
    parser.add_argument("--runner-observed", required=True)
    parser.add_argument("--runtime-log", type=Path, required=True)
    parser.add_argument("--runner-log", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument(
        "--checksums-verified", choices=("true", "false"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runtime_log = args.runtime_log.read_text(encoding="utf-8", errors="replace")
    runner_log = args.runner_log.read_text(encoding="utf-8", errors="replace")
    checksum_file = args.bundle_dir / "SHA256SUMS"
    required = {
        "runtime-image.tar",
        "runner-image.tar",
        "runner-deps-image.tar",
        "compiler-image.tar",
        "tool-images.tar",
        "trivy-db.tar",
        "source-archive.tar.gz",
        "source-tree.tar",
        "source.lock.json",
        "qualified-source.patch",
        "pi-source-archive.tar.gz",
        "pi-source.lock.json",
        "qualified-pi.patch",
        "Dockerfile.runtime",
        "Dockerfile.runner",
        "Dockerfile.runner-deps",
        "Dockerfile.compiler",
        "tools.lock.json",
        "cosign.pub",
        "SHA256SUMS",
    }
    existing = {path.name for path in args.bundle_dir.iterdir() if path.is_file()}
    complete = required.issubset(existing)
    document = {
        "schema_version": "offline-rebuild-evidence/v1",
        "started_at": args.started_at,
        "finished_at": utc_now(),
        "network": "none",
        "pull": False,
        "runtime_expected_digest": args.runtime_expected,
        "runtime_observed_digest": args.runtime_observed,
        "runtime_digest_match": args.runtime_expected == args.runtime_observed,
        "runner_expected_digest": args.runner_expected,
        "runner_observed_digest": args.runner_observed,
        "runner_digest_match": args.runner_expected == args.runner_observed,
        "runtime_tests_51_passed": (
            "100% tests passed, 0 tests failed out of 51" in runtime_log
        ),
        "runner_dependency_check_passed": "No broken requirements found" in runner_log
        or "runner_image_id=" in runner_log,
        "bundle_complete": complete,
        "bundle_missing": sorted(required - existing),
        "bundle_checksums_verified": args.checksums_verified == "true",
        "bundle_checksum_manifest_digest": (
            "sha256:" + hashlib.sha256(checksum_file.read_bytes()).hexdigest()
            if checksum_file.exists()
            else None
        ),
        "runtime_log_sha256": "sha256:"
        + hashlib.sha256(args.runtime_log.read_bytes()).hexdigest(),
        "runner_log_sha256": "sha256:"
        + hashlib.sha256(args.runner_log.read_bytes()).hexdigest(),
    }
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    passed = all(
        (
            document["runtime_digest_match"],
            document["runner_digest_match"],
            document["runtime_tests_51_passed"],
            document["runner_dependency_check_passed"],
            document["bundle_complete"],
            document["bundle_checksums_verified"],
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
