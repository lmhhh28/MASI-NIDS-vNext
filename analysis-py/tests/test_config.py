from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from masi_analysis.config import load_runtime_material
from masi_analysis.errors import AnalysisError

from .support import write_runtime


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.contract_root = Path(__file__).resolve().parents[2] / "contracts"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_material_loads(self) -> None:
        path = write_runtime(self.root, self.contract_root)
        material = load_runtime_material(str(path))
        self.assertEqual(material.binding.binding_generation, 1)
        self.assertEqual(material.config.runtime_profile, "acceptance")

    def test_runtime_binding_ttl_is_explicit(self) -> None:
        path = write_runtime(self.root, self.contract_root, binding_ttl_seconds=7200)
        material = load_runtime_material(str(path))
        self.assertEqual(material.binding.expires_at_unix_ms - material.binding.issued_at_unix_ms, 7_200_000)

    def test_runtime_binding_ttl_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "binding_ttl_seconds must be positive"):
            write_runtime(self.root, self.contract_root, binding_ttl_seconds=0)

    def test_manifest_tamper_is_rejected(self) -> None:
        path = write_runtime(self.root, self.contract_root)
        manifest_path = self.root / "manifest.json"
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        with self.assertRaises(AnalysisError) as caught:
            load_runtime_material(str(path))
        self.assertEqual(caught.exception.code, "BINDING_DIGEST_MISMATCH")


if __name__ == "__main__":
    unittest.main()
