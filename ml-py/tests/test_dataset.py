from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from masi_offline_ml.canonical import ValidationError, sha256_file
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

    def test_legacy_dataset_direct_import_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as context:
            reject_legacy_direct_import(Path("/home/lmhhh/MASI-NIDS/dataset/raw.pcap"))
        self.assertEqual(context.exception.code, "legacy_direct_import")


if __name__ == "__main__":
    unittest.main()
