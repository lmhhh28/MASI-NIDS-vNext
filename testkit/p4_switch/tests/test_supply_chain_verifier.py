from __future__ import annotations

import copy
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

    def test_runner_spdx_versions_and_alpine_distro_are_exact(self) -> None:
        document = {
            "packages": [
                {
                    "name": "iproute2",
                    "versionInfo": "7.0.0-r0",
                    "externalRefs": [
                        {
                            "referenceType": "purl",
                            "referenceLocator": (
                                "pkg:apk/alpine/iproute2@7.0.0-r0?"
                                "arch=x86_64&distro=alpine-3.24.1"
                            ),
                        }
                    ],
                },
                {
                    "name": "tcpreplay",
                    "versionInfo": "4.5.2-r1",
                    "externalRefs": [
                        {
                            "referenceType": "purl",
                            "referenceLocator": (
                                "pkg:apk/alpine/tcpreplay@4.5.2-r1?"
                                "arch=x86_64&distro=alpine-3.24.1"
                            ),
                        }
                    ],
                },
            ]
        }
        expected = {"iproute2": "7.0.0-r0", "tcpreplay": "4.5.2-r1"}
        records = VERIFIER.extract_runner_package_records(document, set(expected))

        self.assertEqual(
            {
                "runner_packages_exact": True,
                "runner_package_distro_exact": True,
            },
            VERIFIER.runner_package_checks(records, expected, "alpine-3.24.1"),
        )

        stale_version = copy.deepcopy(document)
        stale_version["packages"][1]["versionInfo"] = "4.5.1-r0"
        stale_records = VERIFIER.extract_runner_package_records(
            stale_version, set(expected)
        )
        self.assertFalse(
            VERIFIER.runner_package_checks(
                stale_records, expected, "alpine-3.24.1"
            )["runner_packages_exact"]
        )
        self.assertFalse(
            VERIFIER.runner_package_checks(records, expected, "alpine-3.23.3")[
                "runner_package_distro_exact"
            ]
        )


if __name__ == "__main__":
    unittest.main()
