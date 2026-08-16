#!/usr/bin/env python3
"""Central Inference numeric golden reference runner.

Loads the testkit fixture ONNX model via ONNX Runtime CPU, runs the 3 golden
inputs from the expected-outputs fixture, compares against the expected
scores/predicted_label/decision within tolerance, and produces a structured
numeric evidence JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load(path: Path) -> dict[str, Any] | list[Any]:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def float_within_tolerance(a: float, b: float, tol: dict[str, float]) -> bool:
    import math

    if math.isnan(a) or math.isnan(b):
        return False
    abs_diff = abs(a - b)
    if abs_diff <= tol["absolute"]:
        return True
    rel = tol["relative"] * max(abs(a), abs(b))
    if abs_diff <= rel:
        return True
    # ULP comparison.
    a_bits = float(a)
    b_bits = float(b)
    a_u = int.from_bytes(
        struct_pack_be32(a_bits), "big", signed=False
    ) if abs(a_bits) < 3.4e38 else 0
    b_u = int.from_bytes(
        struct_pack_be32(b_bits), "big", signed=False
    ) if abs(b_bits) < 3.4e38 else 0
    ulp = abs(a_u - b_u)
    return ulp <= tol["ulp"]


def struct_pack_be32(f: float) -> bytes:
    import struct

    return struct.pack(">f", f)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("evidence/numeric-golden"),
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        help="Path to the fixture ONNX model. Defaults to testkit fixture.",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    model_path = args.model or (
        repo / "testkit" / "fixtures" / "models" / "masi-ids-window-v1-r1.onnx"
    )
    expected_path = (
        repo / "testkit" / "fixtures" / "models" / "masi-ids-window-v1-r1-expected-outputs.json"
    )
    manifest_path = (
        repo / "testkit" / "fixtures" / "models" / "masi-ids-window-v1-r1-manifest.json"
    )

    if not model_path.is_file():
        print(f"SKIP: fixture model not found: {model_path}", file=sys.stderr)
        return 77
    if not expected_path.is_file():
        print(f"SKIP: expected outputs not found: {expected_path}", file=sys.stderr)
        return 77

    expected = load(expected_path)
    manifest = load(manifest_path)
    tolerance = expected["tolerance"]
    vectors = expected["vectors"]

    # Import ONNX Runtime.
    try:
        import onnxruntime as ort
    except ImportError:
        print(
            "SKIP: onnxruntime is not installed; install onnxruntime==1.19.0 "
            "to run the numeric golden reference.",
            file=sys.stderr,
        )
        # Write a NOT_RUN evidence file so the gate runner records it.
        evidence = {
            "schema_version": "central-inference-numeric-evidence/v1",
            "test_id": "TEST-INF-NUMERIC-001",
            "requirement_ids": [
                "MOD-INF-001",
                "CONTRACT-MODEL-001",
                "TEST-INF-001",
            ],
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "NOT_RUN",
            "qualification": "NOT_QUALIFIED",
            "stable_reason": "ONNXRUNTIME_NOT_INSTALLED",
            "model_id": manifest["model_id"],
            "model_digest": manifest["model_digest"],
            "runtime_profile": "model-runtime-central-cpu/v1",
            "vectors": [],
            "class_order": expected["class_order"],
            "tolerance": tolerance,
        }
        out_path = evidence_dir / "numeric-golden-evidence.json"
        out_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        return 77

    # Load the model with CPU EP only.
    sess_opts = ort.SessionOptions()
    session = ort.InferenceSession(
        str(model_path), sess_opts, providers=["CPUExecutionProvider"]
    )
    available = session.get_providers()
    if available != ["CPUExecutionProvider"]:
        print(f"FAIL: expected only CPUExecutionProvider, got {available}", file=sys.stderr)
        return 1

    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name

    vector_results: list[dict[str, Any]] = []
    all_pass = True

    for v in vectors:
        input_values = v["input"]
        expected_output = v["expected_output"]
        import numpy as np

        arr = np.array([input_values], dtype=np.uint64)
        raw = session.run([output_name], {input_name: arr})[0]
        # The fixture model outputs a [1, 2] float32 array.
        scores = raw.flatten().tolist()
        if len(scores) != len(expected_output["scores"]):
            print(
                f"FAIL: vector {v['vector_id']} score count mismatch: "
                f"{len(scores)} vs {len(expected_output['scores'])}",
                file=sys.stderr,
            )
            all_pass = False
            vector_results.append(
                {
                    "vector_id": v["vector_id"],
                    "result": "FAIL",
                    "reason": "score count mismatch",
                }
            )
            continue

        scores_match = all(
            float_within_tolerance(s, e, tolerance)
            for s, e in zip(scores, expected_output["scores"])
        )
        # argmax over scores.
        predicted_label = int(max(range(len(scores)), key=lambda i: scores[i]))
        label_match = predicted_label == expected_output["predicted_label"]

        # Decision: alert if max score >= 0.5, else benign (matches the
        # output adapter in the manifest).
        threshold = manifest["output_adapter"]["threshold"]
        if scores[predicted_label] >= threshold:
            decision = "alert"
        else:
            decision = "benign"
        decision_match = decision == expected_output["decision"]

        vector_result = {
            "vector_id": v["vector_id"],
            "input_digest": v["input_digest"],
            "expected_output_digest": v["output_digest"],
            "observed_scores": scores,
            "expected_scores": expected_output["scores"],
            "observed_predicted_label": predicted_label,
            "expected_predicted_label": expected_output["predicted_label"],
            "observed_decision": decision,
            "expected_decision": expected_output["decision"],
            "scores_match": scores_match,
            "label_match": label_match,
            "decision_match": decision_match,
            "result": "PASS" if (scores_match and label_match and decision_match) else "FAIL",
        }
        vector_results.append(vector_result)
        if not (scores_match and label_match and decision_match):
            all_pass = False
            print(f"FAIL: vector {v['vector_id']} did not match", file=sys.stderr)

    # Compute observed output digests.
    import struct

    for vr in vector_results:
        if "observed_scores" in vr:
            score_bytes = b"".join(struct.pack("<f", s) for s in vr["observed_scores"])
            vr["observed_output_digest"] = sha256_hex(score_bytes)

    evidence = {
        "schema_version": "central-inference-numeric-evidence/v1",
        "test_id": "TEST-INF-NUMERIC-001",
        "requirement_ids": [
            "MOD-INF-001",
            "CONTRACT-MODEL-001",
            "TEST-INF-001",
        ],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS" if all_pass else "FAIL",
        "qualification": "QUALIFIED" if all_pass else "NOT_QUALIFIED",
        "model_id": manifest["model_id"],
        "model_digest": manifest["model_digest"],
        "runtime_profile": "model-runtime-central-cpu/v1",
        "observed_providers": available,
        "selected_equals_observed": available == ["CPUExecutionProvider"],
        "vectors": vector_results,
        "class_order": expected["class_order"],
        "tolerance": tolerance,
        "nan_policy": expected.get("nan_policy", "reject"),
        "inf_policy": expected.get("inf_policy", "reject"),
    }

    out_path = evidence_dir / "numeric-golden-evidence.json"
    out_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())