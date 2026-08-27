#!/usr/bin/env python3
"""Unit tests for bounded scheduling jitter in the Control soak validator."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import ModuleType


def load_validator() -> ModuleType:
    path = Path(__file__).with_name("validate-soak-evidence.py")
    spec = importlib.util.spec_from_file_location("control_soak_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load validator from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator()


class WarmupSampleCoverageTests(unittest.TestCase):
    def test_accepts_one_millisecond_ticker_jitter(self) -> None:
        self.assertTrue(
            VALIDATOR.warmup_samples_span(
                [10_001, 20_001, 30_001, 40_000, 50_000, 60_000],
                60_000,
                10_000,
            )
        )

    def test_rejects_jitter_beyond_the_one_second_bound(self) -> None:
        self.assertFalse(
            VALIDATOR.warmup_samples_span(
                [11_001, 20_001, 30_001, 40_000, 50_000, 60_000],
                60_000,
                10_000,
            )
        )

    def test_rejects_missing_tail_coverage(self) -> None:
        self.assertFalse(
            VALIDATOR.warmup_samples_span(
                [10_000, 20_000, 30_000], 60_000, 10_000
            )
        )

    def test_rejects_samples_after_the_warmup_boundary(self) -> None:
        self.assertFalse(
            VALIDATOR.warmup_samples_span(
                [10_000, 20_000, 30_000, 40_000, 50_000, 60_001],
                60_000,
                10_000,
            )
        )

    def test_rejects_an_empty_sample_set(self) -> None:
        self.assertFalse(VALIDATOR.warmup_samples_span([], 60_000, 10_000))


if __name__ == "__main__":
    unittest.main()
