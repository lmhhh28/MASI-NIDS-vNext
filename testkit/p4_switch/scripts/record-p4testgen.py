from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: record-p4testgen.py GENERATED_PY LOG OUTPUT_JSON")
    generated_path, log_path, output_path = map(Path, sys.argv[1:])
    source = generated_path.read_text(encoding="utf-8")
    log = log_path.read_text(encoding="utf-8")
    test_count = sum(1 for line in source.splitlines() if line.startswith("class Test"))
    if test_count != 8:
        raise SystemExit(f"expected 8 generated tests, found {test_count}")
    document = {
        "phase": "p4testgen",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS",
        "qualification": "QUALIFIED",
        "tests": [
            {
                "id": "TEST-P4TESTGEN-001",
                "requirement_ids": ["TEST-P4-FW-001", "TEST-TRAFFIC-001"],
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": "PASS",
                "qualification": "QUALIFIED",
                "evidence": {
                    "seed": 1,
                    "backend": "PTF",
                    "generated_tests": test_count,
                    "known_tool_limitations_observed": [
                        item
                        for item in (
                            "digest not fully implemented",
                            "tainted emit calls",
                            "Ingress parser exception handler not fully implemented",
                        )
                        if item in log
                    ],
                    "interpretation": "generation passed; real BMv2 PTF is the outcome oracle",
                },
            }
        ],
    }
    output_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
