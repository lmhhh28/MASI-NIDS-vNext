#!/usr/bin/env python3
"""Strict, bounded JSON Schema validation for Central Inference evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


MAX_JSON_BYTES = 67_108_864


def absolute_path_chain_has_symlink(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def load_object(path: Path) -> dict[str, Any]:
    if absolute_path_chain_has_symlink(path) or path.is_symlink() or not path.is_file():
        raise ValueError(f"unsafe or missing JSON input: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON_BYTES:
            raise ValueError(f"JSON input is not a bounded regular file: {path}")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(1_048_576, MAX_JSON_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_JSON_BYTES:
                raise ValueError(f"JSON input exceeds {MAX_JSON_BYTES} bytes: {path}")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"JSON input changed while being read: {path}")
    finally:
        os.close(descriptor)
    value = json.loads(
        payload.decode("utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON input is not an object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def validate_schema(schema: dict[str, Any], document: dict[str, Any]) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        raise ValueError(
            "\n".join(
                f"schema error at {list(error.absolute_path)}: {error.message}"
                for error in errors
            )
        )


def validate_soak(document: dict[str, Any], profile_path: Path | None) -> None:
    if document.get("result") != "PASS":
        return
    if profile_path is None:
        raise ValueError("PASS soak validation requires --profile")
    profile = load_object(profile_path)
    if document.get("profile_digest") != sha256(profile_path):
        raise ValueError("soak profile digest does not bind the frozen profile")
    if document.get("level") != "MODULE" or document.get("qualification") != "QUALIFIED":
        raise ValueError("PASS soak must be MODULE/QUALIFIED")
    if int(document.get("monotonic_end_ns", 0)) <= int(document.get("monotonic_start_ns", 0)):
        raise ValueError("soak monotonic interval is not positive")
    phases = document.get("phases")
    if not isinstance(phases, list) or len(phases) != len(profile.get("phases", [])):
        raise ValueError("soak phase count does not match the frozen profile")
    prior_phase_end = int(document.get("warmup_elapsed_ms", 0))
    batch_sizes = (32, 128, 256, 1)
    target_batches_per_second = int(profile.get("load", {}).get("batch_requests_per_second", -1))
    if target_batches_per_second <= 0:
        raise ValueError("soak profile has no positive batch request rate")
    for expected, observed, records_per_batch in zip(
        profile["phases"], phases, batch_sizes, strict=True
    ):
        metrics = observed.get("module_metrics", {})
        elapsed_ms = int(observed.get("elapsed_ms", -1))
        batches = int(metrics.get("batches", -1))
        requested_rate = float(observed.get("requested_rate_pps", -1))
        achieved_rate = float(observed.get("achieved_rate_pps", -1))
        expected_rate = float(target_batches_per_second * records_per_batch)
        recomputed_rate = (
            float(batches * records_per_batch) * 1000.0 / float(elapsed_ms)
            if elapsed_ms > 0 and batches >= 0
            else -1.0
        )
        rate_tolerance = max(0.001, abs(recomputed_rate) * 0.001)
        if (
            observed.get("name") != expected.get("name")
            or int(observed.get("planned_ms", -1))
            != int(expected.get("duration_seconds", -1)) * 1000
            or observed.get("result") != "PASS"
            or int(observed.get("errors", -1)) != 0
            or elapsed_ms < int(observed.get("planned_ms", 0))
            or metrics.get("planned_duration_met") is not True
            or metrics.get("rate_target_met") is not True
            or int(metrics.get("records_per_batch", -1)) != records_per_batch
            or int(metrics.get("target_batches_per_second", -1))
            != target_batches_per_second
            or batches <= 0
            or abs(requested_rate - expected_rate) > 0.001
            or abs(achieved_rate - recomputed_rate) > rate_tolerance
            or achieved_rate < expected_rate * 0.99
            or int(metrics.get("phase_start_offset_ms", -1)) < prior_phase_end
            or int(metrics.get("phase_end_offset_ms", -1))
            <= int(metrics.get("phase_start_offset_ms", -1))
        ):
            raise ValueError(f"soak phase is not a complete formal PASS: {expected.get('name')}")
        prior_phase_end = int(metrics["phase_end_offset_ms"])
    samples = document.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("PASS soak has no samples")
    offsets: list[int] = []
    provider_counts: list[tuple[int, int, int]] = []
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("quality") != "valid":
            raise ValueError("PASS soak contains an invalid/not-measurable sample")
        offsets.append(int(sample.get("offset_ms", -1)))
        sample_metrics = sample.get("module_metrics", {})
        if (
            not isinstance(sample_metrics, dict)
            or sample_metrics.get("provider_statistics_observed") is not True
            or sample_metrics.get("queue_ownership")
            != "none-gateway-submits-directly-to-triton"
            or sample_metrics.get("queue_depth_source") != "gateway-has-no-owned-queue"
        ):
            raise ValueError("PASS soak sample lacks provider/queue observation provenance")
        provider_counts.append(
            (
                int(sample_metrics.get("provider_inference_count", -1)),
                int(sample_metrics.get("provider_execution_count", -1)),
                int(sample_metrics.get("provider_success_count", -1)),
            )
        )
    if offsets != sorted(offsets) or len(offsets) != len(set(offsets)):
        raise ValueError("soak sample offsets are not strictly monotonic")
    for index in range(1, len(samples)):
        delta = offsets[index] - offsets[index - 1]
        declared = int(samples[index].get("module_metrics", {}).get("actual_sample_interval_ms", -1))
        if not 9_000 <= delta <= 11_000 or abs(declared - delta) > 250:
            raise ValueError("PASS soak sample interval drifted from the frozen 10-second cadence")
    if any(
        current[dimension] < previous[dimension]
        for previous, current in zip(provider_counts, provider_counts[1:])
        for dimension in range(3)
    ):
        raise ValueError("PASS soak provider statistics are not monotonic")
    summary = document.get("summary", {})
    metrics = summary.get("module_metrics", {}) if isinstance(summary, dict) else {}
    baseline_inferences = int(metrics.get("provider_baseline_inference_count", -1))
    final_inferences = int(metrics.get("provider_final_inference_count", -1))
    successful_records = int(metrics.get("successful_records", -1))
    if (
        metrics.get("formal_schedule_executed") is not True
        or int(metrics.get("not_measurable_samples", -1)) != 0
        or int(metrics.get("warmup_actual_ms", -1))
        != int(document.get("warmup_elapsed_ms", -2))
        or int(metrics.get("provider_statistics_samples", -1)) < len(samples) + 1
        or successful_records <= 0
        or baseline_inferences < 0
        or final_inferences < baseline_inferences + successful_records
        or metrics.get("provider_restart_signal")
        != "triton-model-statistics-counter-reset"
        or metrics.get("resource_measurement_scope")
        != "gateway-proc-and-provider-monotonic-statistics;absolute-process-thresholds-unfrozen"
        or metrics.get("fatal_runtime_diagnostic") != "none"
    ):
        raise ValueError("PASS soak summary does not prove the formal schedule")


def validate_module(document: dict[str, Any]) -> None:
    if document.get("schema_version") != "central-inference-module-gate-summary/v1":
        raise ValueError("module validation requires a module gate summary")
    if document.get("result") != document.get("overall_status"):
        raise ValueError("module result and overall_status disagree")
    if document.get("overall_module_complete") is not True:
        return
    if document.get("result") not in {"PASS", "HOLD"}:
        raise ValueError("a complete module cannot have FAIL/NOT_RUN status")
    if document.get("blackbox_e2e", {}).get("result") != "PASS":
        raise ValueError("a complete module requires real-process black-box PASS")
    if int(document.get("findings", {}).get("open_p0", -1)) != 0:
        raise ValueError("a complete module requires zero open P0 findings")
    formal = document.get("qualification_gates", {}).get("formal_soak_3600_seconds", {})
    artifacts = document.get("artifact_digests", {})
    formal_digest = artifacts.get("formal_soak_evidence")
    if (
        formal.get("result") != "PASS"
        or formal.get("qualification") != "QUALIFIED"
        or formal.get("evidence") != "formal-soak/formal-soak-evidence.json"
        or formal.get("evidence_digest") != formal_digest
        or not isinstance(formal_digest, str)
        or not formal_digest.startswith("sha256:")
    ):
        raise ValueError("a complete module is not bound to a qualified formal-soak evidence digest")
    for name in (
        "release_binary",
        "oci_evidence",
        "deep_check_evidence",
        "supply_chain_evidence",
        "numeric_golden_evidence",
    ):
        digest = artifacts.get(name)
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError(f"a complete module is missing the {name} digest")
    executed = document.get("executed_gates", {})
    for name in (
        "gcc_version",
        "cmake_version",
        "protoc_version",
        "public_contract_schema_golden_negative",
        "format",
        "clang_tidy_deny_warnings",
        "unit_property_contract_golden",
        "numeric_golden_python_reference",
        "real_process_public_boundary_blackbox",
        "release_build",
    ):
        if executed.get(name) != "PASS":
            raise ValueError(f"a complete module requires {name}=PASS")
    for name in (
        "oci_startup_mtls_probe_graceful_shutdown",
        "asan_ubsan_tsan_library_scope",
        "supply_chain",
    ):
        if executed.get(name, {}).get("result") not in {"PASS", "HOLD"}:
            raise ValueError(f"a complete module requires executed {name} evidence")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--kind", choices=("generic", "soak", "module"), default="generic")
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()

    schema = load_object(args.schema)
    document = load_object(args.document)
    validate_schema(schema, document)
    if args.kind == "soak":
        validate_soak(document, args.profile)
    elif args.kind == "module":
        validate_module(document)
    print(
        json.dumps(
            {
                "schema_version": "central-inference-evidence-validation/v1",
                "document": str(args.document),
                "document_digest": sha256(args.document),
                "schema_digest": sha256(args.schema),
                "result": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
