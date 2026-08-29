from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ZERO_DIGEST = "sha256:" + "0" * 64


def patterns(value: Any):
    if isinstance(value, dict):
        pattern = value.get("pattern")
        if isinstance(pattern, str) and (
            "sha256:" in pattern
            or ("[0-9a-f]" in pattern and ("{40}" in pattern or "{64}" in pattern))
        ):
            yield pattern
        for child in value.values():
            yield from patterns(child)
    elif isinstance(value, list):
        for child in value:
            yield from patterns(child)


class ContractDigestPatternTests(unittest.TestCase):
    def test_every_digest_or_revision_pattern_rejects_all_zero_sentinel(self) -> None:
        failures: list[str] = []
        candidates = [
            ZERO_DIGEST,
            "0" * 64,
            "0" * 40,
            f"image@{ZERO_DIGEST}",
            f"repo/image@{ZERO_DIGEST}",
        ]
        for path in sorted((ROOT / "contracts").rglob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            for pattern in patterns(document):
                expression = re.compile(pattern)
                if any(expression.fullmatch(candidate) for candidate in candidates):
                    failures.append(f"{path.relative_to(ROOT)}: {pattern}")
        for path in sorted((ROOT / "contracts").rglob("*.yaml")):
            for pattern in re.findall(
                r"\bpattern:\s*['\"]([^'\"]+)['\"]",
                path.read_text(encoding="utf-8"),
            ):
                if "sha256:" not in pattern and not (
                    "[0-9a-f]" in pattern and ("{40}" in pattern or "{64}" in pattern)
                ):
                    continue
                expression = re.compile(pattern)
                if any(expression.fullmatch(candidate) for candidate in candidates):
                    failures.append(f"{path.relative_to(ROOT)}: {pattern}")
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
