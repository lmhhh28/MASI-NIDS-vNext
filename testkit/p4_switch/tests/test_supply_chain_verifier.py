from __future__ import annotations

import importlib.util
import unittest
from datetime import timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify-supply-chain.py"
SPEC = importlib.util.spec_from_file_location("verify_supply_chain", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load {SCRIPT}")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class SupplyChainVerifierTests(unittest.TestCase):
    def test_trivy_nanosecond_timestamp_is_normalized(self) -> None:
        observed = VERIFIER.parse_time("2026-08-12T16:32:36.576513121Z")

        self.assertEqual(576513, observed.microsecond)
        self.assertEqual(timezone.utc, observed.tzinfo)

    def test_timezone_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            VERIFIER.parse_time("2026-08-12T16:32:36.576513")


if __name__ == "__main__":
    unittest.main()
