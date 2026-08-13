from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 7:
        raise SystemExit(
            "usage: record-crash.py OLD_ID OLD_STARTED EXIT_CODE NEW_ID NEW_STARTED OUTPUT"
        )
    old_id, old_started, exit_code, new_id, new_started, output = sys.argv[1:]
    passed = old_id != new_id and int(exit_code) == 137
    document = {
        "phase": "crash-orchestration",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS" if passed else "FAIL",
        "qualification": "QUALIFIED" if passed else "NOT_QUALIFIED",
        "tests": [
            {
                "id": "TEST-P4-FAULT-002-sigkill-orchestration",
                "requirement_ids": ["CONTRACT-P4-001", "TEST-003"],
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": "PASS" if passed else "FAIL",
                "qualification": "QUALIFIED" if passed else "NOT_QUALIFIED",
                "evidence": {
                    "signal": "SIGKILL",
                    "old_container_id": old_id,
                    "old_started_at": old_started,
                    "old_exit_code": int(exit_code),
                    "new_container_id": new_id,
                    "new_started_at": new_started,
                },
            }
        ],
    }
    Path(output).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
