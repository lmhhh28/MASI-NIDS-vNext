from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


REQUIREMENTS = {
    "TEST-P4-STATIC-CONTRACT-001": ["CONTRACT-P4-001", "CONTRACT-P4-FW-001"],
    "TEST-P4-COMPILE-001": ["CONTRACT-P4-001", "MOD-SW-001"],
    "TEST-P4TESTGEN-001": ["TEST-P4-FW-001", "TEST-TRAFFIC-001"],
}


def qualification(result: str) -> str:
    return "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED"


def main() -> int:
    if len(sys.argv) not in {7, 8}:
        raise SystemExit(
            "usage: record-command.py PHASE TEST_ID RESULT EXIT_CODE LOG OUTPUT [DURATION_MS]"
        )
    phase, test_id, result, exit_code, log_name, output_name = sys.argv[1:7]
    duration_ms = float(sys.argv[7]) if len(sys.argv) == 8 else None
    log_path = Path(log_name)
    log_bytes = log_path.read_bytes() if log_path.exists() else b""
    document = {
        "phase": phase,
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": qualification(result),
        "tests": [
            {
                "id": test_id,
                "requirement_ids": REQUIREMENTS.get(test_id, ["CONTRACT-P4-001"]),
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": qualification(result),
                "evidence": {
                    "exit_code": int(exit_code),
                    "log": log_path.name,
                    "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
                    **({"duration_ms": duration_ms} if duration_ms is not None else {}),
                },
            }
        ],
    }
    Path(output_name).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
