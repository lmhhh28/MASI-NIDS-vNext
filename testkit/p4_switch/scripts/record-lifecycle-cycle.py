from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle", type=int, required=True)
    parser.add_argument("--signal", choices=("SIGINT", "SIGTERM"), required=True)
    parser.add_argument("--old-id", required=True)
    parser.add_argument("--old-exit", type=int, required=True)
    parser.add_argument("--stop-duration-ms", type=int, required=True)
    parser.add_argument("--forced-kill", choices=("true", "false"), required=True)
    parser.add_argument("--oom-killed", choices=("true", "false"), required=True)
    parser.add_argument("--new-id", required=True)
    parser.add_argument("--old-removed", choices=("true", "false"), required=True)
    parser.add_argument("--pre-probe", type=Path, required=True)
    parser.add_argument("--post-probe", type=Path, required=True)
    parser.add_argument("--shutdown-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pre = load(args.pre_probe)
    post = load(args.post_probe)
    log_bytes = args.shutdown_log.read_bytes()
    log = log_bytes.decode("utf-8", errors="replace")
    markers = {
        marker: marker in log
        for marker in (
            "shutdown: signal received",
            "shutdown: servers stopped",
            "shutdown: server wait returned",
            "shutdown: leaving main",
            "shutdown: PI cleanup started",
            "shutdown: PI cleanup finished",
        )
    }
    checks = {
        "pre_public_boundary": pre.get("result") == "PASS",
        "clean_exit": args.old_exit == 0,
        "within_grace": 0 <= args.stop_duration_ms <= 10_000,
        "not_forced": args.forced_kill == "false",
        "not_oom": args.oom_killed == "false",
        "new_container": args.old_id != args.new_id,
        "old_container_and_namespace_removed": args.old_removed == "true",
        "post_public_boundary": post.get("result") == "PASS",
        "shutdown_timeline_complete": all(markers.values()),
    }
    document = {
        "cycle": args.cycle,
        "signal": args.signal,
        "old_container_id": args.old_id,
        "old_exit_code": args.old_exit,
        "stop_duration_ms": args.stop_duration_ms,
        "forced_kill": args.forced_kill == "true",
        "oom_killed": args.oom_killed == "true",
        "new_container_id": args.new_id,
        "old_container_removed": args.old_removed == "true",
        "pre_probe": pre,
        "post_probe": post,
        "shutdown_log": args.shutdown_log.name,
        "shutdown_log_sha256": hashlib.sha256(log_bytes).hexdigest(),
        "shutdown_markers": markers,
        "checks": checks,
        "result": "PASS" if all(checks.values()) else "FAIL",
    }
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if document["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
