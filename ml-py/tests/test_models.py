from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from masi_offline_ml.artifacts import finalize_candidate_metadata, select_winner
from masi_offline_ml.dataset import generate_module_dataset, load_dataset
from masi_offline_ml.explanations import build_explanations
from masi_offline_ml.models import train_autoencoder, train_logistic, train_xgboost
from masi_offline_ml.onnx_export import run_ort

REPO = Path(__file__).resolve().parents[2]


class ModelQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        dataset_path = Path(cls.temporary.name) / "dataset"
        generate_module_dataset(REPO, dataset_path)
        cls.dataset = load_dataset(REPO, dataset_path)
        cls.candidates = []
        metadata = {
            "dataset_id": str(cls.dataset.manifest["dataset_id"]),
            "dataset_revision": str(cls.dataset.manifest["dataset_revision"]),
        }
        for trainer in (train_logistic, train_xgboost, train_autoencoder):
            for seed in (17, 29, 43):
                cls.candidates.append(finalize_candidate_metadata(trainer(cls.dataset, seed, metadata), cls.dataset))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_all_mandatory_candidates_and_seeds_pass(self) -> None:
        identities = {(candidate.candidate_id, candidate.seed) for candidate in self.candidates}
        self.assertEqual(len(identities), 9)
        for candidate in self.candidates:
            with self.subTest(candidate=candidate.candidate_id, seed=candidate.seed):
                self.assertTrue(candidate.metrics["passes_thresholds"])
                self.assertGreaterEqual(candidate.metrics["average_precision"], 0.95)
                self.assertLessEqual(candidate.metrics["false_positive_rate"], 0.01)
                scores = run_ort(candidate.onnx_payload, self.dataset.features[candidate.blind_indices[:32]])
                self.assertEqual(scores.shape, (32, 2))
                self.assertEqual(candidate.onnx_evidence["providers"], ["CPUExecutionProvider"])
                self.assertFalse(candidate.onnx_evidence["zipmap"])
                self.assertFalse(candidate.onnx_evidence["custom_operators"])

    def test_explanations_are_bound_and_non_executable(self) -> None:
        documents = build_explanations(REPO, self.dataset, self.candidates)
        self.assertEqual(len(documents), 9)
        for candidate in self.candidates:
            document = documents[(candidate.candidate_id, candidate.seed)]
            metadata = cast(dict[str, str], candidate.onnx_evidence["metadata"])
            binding = cast(dict[str, Any], document["binding"])
            self.assertEqual(binding["feature_schema_digest"], metadata["feature_contract_digest"])
            self.assertEqual(binding["label_taxonomy_digest"], metadata["label_contract_digest"])
            self.assertEqual(binding["output_adapter_digest"], metadata["output_adapter_digest"])
            self.assertTrue(all(value is False for value in cast(dict[str, bool], document["safety"]).values()))

    def test_winner_selection_is_deterministic_and_has_no_fallback(self) -> None:
        selection = select_winner(self.candidates)
        self.assertEqual(selection["result"], "PASS")
        self.assertEqual(selection["winner"], "lr-window-binary/v1")
        self.assertEqual(selection["winner_seed"], 17)
        self.assertFalse(selection["ensemble"])
        self.assertFalse(selection["fallback"])


if __name__ == "__main__":
    unittest.main()
