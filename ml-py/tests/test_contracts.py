from __future__ import annotations

import copy
import json
import struct
import unittest
from pathlib import Path

from masi_offline_ml.canonical import ValidationError
from masi_offline_ml.contracts import (
    feature_tensor_bytes,
    split_name,
    telemetry_cell_selector_digest,
    validate_dataset_manifest,
    validate_public_contracts,
)

REPO = Path(__file__).resolve().parents[2]


class PublicContractTests(unittest.TestCase):
    def test_public_contracts_and_goldens(self) -> None:
        result = validate_public_contracts(REPO)
        self.assertEqual(result["result"], "PASS")
        self.assertGreaterEqual(result["dataset_vectors"], 4)
        self.assertGreaterEqual(result["explanation_vectors"], 4)

    def test_feature_tensor_is_exact_little_endian_uint64(self) -> None:
        values = [2, 108, 1, 2, 108, 1]
        self.assertEqual(feature_tensor_bytes(values), struct.pack("<6Q", *values))
        self.assertEqual(len(feature_tensor_bytes(values)), 48)

    def test_invalid_feature_values_fail_closed(self) -> None:
        for values in ([0, 0, 0, 0, 0], [0, 0, 0, 0, 0, -1], [0, 0, 0, 0, 0, True]):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                feature_tensor_bytes(values)

    def test_split_is_capture_family_deterministic(self) -> None:
        first = split_name("source-r1", "capture-family-42")
        self.assertEqual(first, split_name("source-r1", "capture-family-42"))
        self.assertIn(first, {"train", "early_stop", "calibration", "blind_test"})

    def test_zero_digest_and_revision_sentinels_fail_closed(self) -> None:
        with self.assertRaises(ValidationError):
            telemetry_cell_selector_digest("target-1", "sha256:" + ("0" * 64), 1, 0)
        manifest = json.loads(
            (REPO / "contracts/dataset/v1/golden/manifest-golden-v1.json").read_text(encoding="utf-8")
        )
        zero_digest = copy.deepcopy(manifest)
        zero_digest["producer"]["config_digest"] = "sha256:" + ("0" * 64)
        with self.assertRaises(ValidationError):
            validate_dataset_manifest(zero_digest)
        zero_revision = copy.deepcopy(manifest)
        zero_revision["producer"]["source_revision"] = "0" * 40
        with self.assertRaises(ValidationError):
            validate_dataset_manifest(zero_revision)


if __name__ == "__main__":
    unittest.main()
