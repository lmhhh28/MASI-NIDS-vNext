from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from masi_offline_ml.canonical import ValidationError, sha256_file
from masi_offline_ml.contracts import telemetry_cell_selector_digest
from masi_offline_ml.dataset import generate_module_dataset, load_dataset, reject_legacy_direct_import

REPO = Path(__file__).resolve().parents[2]


class DatasetTests(unittest.TestCase):
    def test_generation_is_reproducible_and_split_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_manifest = generate_module_dataset(REPO, root / "first")
            second_manifest = generate_module_dataset(REPO, root / "second")
            first = load_dataset(REPO, root / "first")
            second = load_dataset(REPO, root / "second")

            self.assertEqual(first_manifest["dataset_revision"], second_manifest["dataset_revision"])
            self.assertEqual(
                sha256_file(root / "first/data/features.bin"), sha256_file(root / "second/data/features.bin")
            )
            self.assertTrue(np.array_equal(first.features, second.features))
            self.assertEqual(first.features.shape, (2560, 6))
            self.assertEqual(
                {name: first.indices(name).size for name in set(first.splits)},
                {
                    "train": 1536,
                    "early_stop": 384,
                    "calibration": 384,
                    "blind_test": 256,
                },
            )

            family_splits: dict[str, set[str]] = {}
            for record in first.records:
                family_splits.setdefault(str(record["capture_family_id"]), set()).add(str(record["split"]))
            self.assertTrue(all(len(splits) == 1 for splits in family_splits.values()))

            extractor = first_manifest["extractor"]
            p4_profile = json.loads((REPO / "contracts/profiles/v1/p4-stateless-firewall-bmv2.json").read_text())
            self.assertEqual(extractor["p4_program_digest"], p4_profile["artifact_digests"]["p4_source"])
            self.assertNotEqual(extractor["p4_program_digest"], p4_profile["artifact_digests"]["bmv2_json"])
            self.assertEqual(extractor["selector_algorithm"], "p4-qualified-cell-selector/v1")
            self.assertEqual(
                extractor["selector_digest_preimage"],
                "target_id\\0source_profile_digest\\0epoch_decimal\\0cell_index_decimal",
            )
            self.assertNotIn("selector_digest", extractor)

            telemetry = json.loads((REPO / "contracts/golden/telemetry/snapshot-v1.json").read_text())
            cell = telemetry["cells"][0]
            self.assertEqual(
                cell["selector_digest"],
                telemetry_cell_selector_digest(
                    telemetry["target_id"],
                    telemetry["source_profile_digest"],
                    telemetry["epoch"],
                    cell["index"],
                ),
            )

    def test_legacy_dataset_direct_import_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as context:
            reject_legacy_direct_import(Path("/home/lmhhh/MASI-NIDS/dataset/raw.pcap"))
        self.assertEqual(context.exception.code, "legacy_direct_import")


if __name__ == "__main__":
    unittest.main()
