#!/usr/bin/env python3
"""Discriminating negative vectors for strict evidence validation."""

from __future__ import annotations

import copy
import json
import re
import runpy
import tempfile
from pathlib import Path
from typing import Any, Callable


def rejected(action: Callable[[], None]) -> bool:
    try:
        action()
    except ValueError:
        return True
    return False


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    repo = script_dir.parent.parent
    library: dict[str, Any] = runpy.run_path(
        str(script_dir / "validate-evidence.py"), run_name="evidence_validator_library"
    )
    load_object = library["load_object"]
    validate_schema = library["validate_schema"]
    validate_soak = library["validate_soak"]
    validate_module = library["validate_module"]
    sha256 = library["sha256"]

    with tempfile.TemporaryDirectory(
        prefix="masi-inference-evidence-negative."
    ) as temporary:
        duplicate = Path(temporary) / "duplicate.json"
        duplicate.write_text('{"result":"PASS","result":"FAIL"}', encoding="utf-8")
        if not rejected(lambda: load_object(duplicate)):
            raise AssertionError("duplicate JSON member was accepted")

    supply_schema = load_object(
        repo / "contracts/evidence/central-inference-supply/v1/schema.json"
    )
    supply = load_object(
        repo / "contracts/golden/evidence/central-inference-supply-verification-v1.json"
    )
    validate_schema(supply_schema, supply)
    oci_schema = load_object(
        repo / "contracts/evidence/central-inference-oci/v1/schema.json"
    )
    triton_pattern = oci_schema["properties"]["triton_image"]["pattern"]
    if re.fullmatch(triton_pattern, "triton@sha256:" + "0" * 64):
        raise AssertionError("Central OCI schema accepted an all-zero Triton digest")
    bad_supply = copy.deepcopy(supply)
    bad_supply["signature"]["valid"] = False
    if not rejected(lambda: validate_schema(supply_schema, bad_supply)):
        raise AssertionError("PASS supply evidence accepted an invalid signature")

    profile_path = repo / "contracts/profiles/v1/qualification-soak-3600s.json"
    phases = []
    phase_start = 60_000
    batch_sizes = (32, 128, 256, 1)
    for name, records_per_batch in zip(
        ("steady", "peak", "saturation", "recovery-or-activation"),
        batch_sizes,
        strict=True,
    ):
        phase_end = phase_start + 900_000
        requested_rate = 100 * records_per_batch
        phases.append(
            {
                "name": name,
                "planned_ms": 900_000,
                "elapsed_ms": 900_000,
                "result": "PASS",
                "errors": 0,
                "requested_rate_pps": requested_rate,
                "achieved_rate_pps": requested_rate,
                "module_metrics": {
                    "phase_start_offset_ms": phase_start,
                    "phase_end_offset_ms": phase_end,
                    "planned_duration_met": True,
                    "rate_target_met": True,
                    "records_per_batch": records_per_batch,
                    "target_batches_per_second": 100,
                    "batches": 90_000,
                },
            }
        )
        phase_start = phase_end
    soak: dict[str, Any] = {
        "result": "PASS",
        "level": "MODULE",
        "qualification": "QUALIFIED",
        "profile_digest": sha256(profile_path),
        "monotonic_start_ns": 1,
        "monotonic_end_ns": 3_660_000_000_001,
        "warmup_elapsed_ms": 60_000,
        "phases": phases,
        "samples": [
            {
                "offset_ms": 10_000,
                "quality": "valid",
                "module_metrics": {
                    "actual_sample_interval_ms": 10_000,
                    "provider_statistics_observed": True,
                    "queue_ownership": "none-gateway-submits-directly-to-triton",
                    "queue_depth_source": "gateway-has-no-owned-queue",
                    "provider_inference_count": 1,
                    "provider_execution_count": 1,
                    "provider_success_count": 1,
                },
            },
            {
                "offset_ms": 20_000,
                "quality": "valid",
                "module_metrics": {
                    "actual_sample_interval_ms": 10_000,
                    "provider_statistics_observed": True,
                    "queue_ownership": "none-gateway-submits-directly-to-triton",
                    "queue_depth_source": "gateway-has-no-owned-queue",
                    "provider_inference_count": 2,
                    "provider_execution_count": 2,
                    "provider_success_count": 2,
                },
            },
        ],
        "summary": {
            "module_metrics": {
                "formal_schedule_executed": True,
                "not_measurable_samples": 0,
                "warmup_actual_ms": 60_000,
                "provider_statistics_samples": 3,
                "successful_records": 37_530_000,
                "provider_baseline_inference_count": 0,
                "provider_final_inference_count": 37_530_000,
                "provider_restart_signal": "triton-model-statistics-counter-reset",
                "resource_measurement_scope": "gateway-proc-and-provider-monotonic-statistics;absolute-process-thresholds-unfrozen",
                "fatal_runtime_diagnostic": "none",
            }
        },
    }
    validate_soak(soak, profile_path)
    bad_soak = copy.deepcopy(soak)
    bad_soak["level"] = "REHEARSAL"
    if not rejected(lambda: validate_soak(bad_soak, profile_path)):
        raise AssertionError("REHEARSAL evidence was accepted as a formal PASS")
    bad_soak = copy.deepcopy(soak)
    bad_soak["samples"][0]["quality"] = "not_measurable"
    if not rejected(lambda: validate_soak(bad_soak, profile_path)):
        raise AssertionError("not-measurable sample was accepted in a formal PASS")
    bad_soak = copy.deepcopy(soak)
    bad_soak["phases"][2]["achieved_rate_pps"] = 1
    if not rejected(lambda: validate_soak(bad_soak, profile_path)):
        raise AssertionError("fabricated soak rate_target_met was accepted")

    module = load_object(
        repo / "contracts/golden/evidence/central-inference-module-v1.json"
    )
    validate_module(module)
    bad_module = copy.deepcopy(module)
    bad_module["qualification_gates"]["formal_soak_3600_seconds"]["evidence_digest"] = (
        "sha256:" + "2" * 64
    )
    if not rejected(lambda: validate_module(bad_module)):
        raise AssertionError("module completion accepted an unbound formal soak digest")
    bad_module = copy.deepcopy(module)
    bad_module["artifact_digests"]["release_binary"] = "sha256:" + "0" * 64
    if not rejected(lambda: validate_module(bad_module)):
        raise AssertionError("module completion accepted an all-zero artifact digest")

    print(
        json.dumps(
            {
                "schema_version": "central-inference-evidence-validator-tests/v1",
                "negative_vectors": 8,
                "result": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
