from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIREMENTS = {
    "supply-chain": ["ARCH-REUSE-001", "CONTRACT-SUPPLY-001", "TEST-003"],
}
TEST_IDS = {"supply-chain": "TEST-P4-SUPPLY-001"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=tuple(REQUIREMENTS), required=True)
    parser.add_argument("--level", choices=("REHEARSAL", "MODULE"), required=True)
    parser.add_argument("--result", choices=("HOLD", "NOT_RUN", "FAIL"), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = {
        "phase": args.phase,
        "level": args.level,
        "applicability": "APPLICABLE",
        "result": args.result,
        "qualification": "NOT_QUALIFIED",
        "tests": [
            {
                "id": TEST_IDS[args.phase],
                "requirement_ids": REQUIREMENTS[args.phase],
                "level": args.level,
                "applicability": "APPLICABLE",
                "result": args.result,
                "qualification": "NOT_QUALIFIED",
                "evidence": {"stable_reason": args.reason},
            }
        ],
    }
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
