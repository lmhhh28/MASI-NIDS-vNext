from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles-dir", type=Path, required=True)
    parser.add_argument("--runtime-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cycles = []
    for path in sorted(args.cycles_dir.glob("cycle-*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{path} is not an object")
        cycles.append(value)
    signal_counts = {
        signal: sum(
            1
            for cycle in cycles
            if cycle.get("signal") == signal and cycle.get("result") == "PASS"
        )
        for signal in ("SIGINT", "SIGTERM")
    }
    complete = (
        len(cycles) == 20
        and signal_counts == {"SIGINT": 10, "SIGTERM": 10}
        and all(cycle.get("result") == "PASS" for cycle in cycles)
    )
    result = "PASS" if complete else "FAIL"
    document = {
        "phase": "lifecycle",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": "QUALIFIED" if complete else "NOT_QUALIFIED",
        "tests": [
            {
                "id": "TEST-P4-LIFECYCLE-001",
                "requirement_ids": ["MOD-SW-001", "TEST-003"],
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": "QUALIFIED" if complete else "NOT_QUALIFIED",
                "evidence": {
                    "runtime_digest": args.runtime_digest,
                    "required_cycles": {"SIGINT": 10, "SIGTERM": 10},
                    "passed_cycles": signal_counts,
                    "forced_kill_count": sum(
                        int(bool(cycle.get("forced_kill"))) for cycle in cycles
                    ),
                    "exit_137_count": sum(
                        int(cycle.get("old_exit_code") == 137) for cycle in cycles
                    ),
                    "maximum_stop_duration_ms": max(
                        (int(cycle.get("stop_duration_ms", 0)) for cycle in cycles),
                        default=0,
                    ),
                    "cycles": cycles,
                    "cycles_tree_sha256": hashlib.sha256(
                        b"".join(
                            path.name.encode() + b"\x00" + path.read_bytes()
                            for path in sorted(args.cycles_dir.glob("cycle-*.json"))
                        )
                    ).hexdigest(),
                },
            }
        ],
    }
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
