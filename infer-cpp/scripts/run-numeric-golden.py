#!/usr/bin/env python3
"""Central Inference numeric golden reference runner.

Loads the pinned r3 fixture ONNX through ONNX Runtime CPU, runs every golden
vector (single-record and multi-record), and compares both layers:

  1. the raw model output (logits), and
  2. the canonical adapter output (class-ordered float32 scores, predicted
     label, decision and canonical output digest).

The adapter parameters are read from the digest-pinned bundle manifest, and the
decision rule is the one frozen in
`contracts/inference/v1/profile.json#output_adapter_binding`, so this reference
runner and the C++ Gateway cannot drift into different score semantics.

This is a reference-implementation check. Evidence that the *serving* path
produces the same values comes from the real-process black-box test, which
asserts the same golden values through Gateway -> Triton.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any

REVISION = "r3"


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def load(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def float_within_tolerance(a: float, b: float, tol: dict[str, float]) -> bool:
    if math.isnan(a) or math.isnan(b):
        return False
    abs_diff = abs(a - b)
    if abs_diff <= tol["absolute"]:
        return True
    if abs_diff <= tol["relative"] * max(abs(a), abs(b)):
        return True
    a_bits = struct.unpack("<I", struct.pack("<f", a))[0]
    b_bits = struct.unpack("<I", struct.pack("<f", b))[0]
    return abs(a_bits - b_bits) <= tol["ulp"]


def stable_softmax(row: list[float]) -> list[float]:
    m = max(row)
    exps = [math.exp(float(v) - m) for v in row]
    total = sum(exps)
    if not (total > 0.0) or not math.isfinite(total):
        raise ValueError("softmax sum not finite")
    return [struct.unpack("<f", struct.pack("<f", e / total))[0] for e in exps]


def apply_adapter(row: list[float], taxonomy: dict, adapter: dict) -> dict:
    """The frozen `masi-window-adapter-v1` rule, shared with the C++ Gateway."""
    domain = taxonomy["score_domain"]
    if domain == "logit":
        normalized = stable_softmax(row)
    elif domain == "probability":
        normalized = list(row)
    else:
        raise ValueError(f"score_domain not supported by adapter: {domain}")

    class_order = adapter["class_order"]
    scores = [normalized[label] for label in class_order]
    top_index = max(range(len(scores)), key=lambda i: scores[i])
    predicted_label = int(class_order[top_index])
    top_score = scores[top_index]
    baseline_label = int(class_order[0])

    ood_policy = adapter["ood_policy"]
    if ood_policy["mode"] != "max-probability-below-threshold":
        raise ValueError("unsupported OOD policy")
    out_of_distribution = top_score < float(ood_policy["threshold"])
    abstain = out_of_distribution or top_score < float(taxonomy["threshold"]["value"])
    if abstain:
        decision = "abstain"
    elif predicted_label != baseline_label and top_score >= float(adapter["threshold"]):
        decision = "alert"
    else:
        decision = "benign"

    score_bytes = b"".join(struct.pack("<f", s) for s in scores)
    return {
        "scores": scores,
        "predicted_label": predicted_label,
        "decision": decision,
        "out_of_distribution": out_of_distribution,
        "abstain": abstain,
        "quality": "valid",
        "output_digest": sha256_hex(score_bytes),
    }


def not_run_evidence(reason: str, manifest: dict, evidence_dir: Path) -> None:
    evidence = {
        "schema_version": "central-inference-numeric-evidence/v1",
        "test_id": "TEST-INF-NUMERIC-001",
        "requirement_ids": ["MOD-INF-001", "CONTRACT-MODEL-001", "TEST-INF-001"],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "NOT_RUN",
        "qualification": "NOT_QUALIFIED",
        "stable_reason": reason,
        "model_id": manifest.get("model_id", "unknown"),
        "model_digest": manifest.get("model_digest", "unknown"),
        "runtime_profile": "model-runtime-central-cpu/v1",
        "vectors": [],
        "class_order": manifest.get("output_adapter", {}).get("class_order", []),
        "tolerance": {"absolute": 1e-6, "relative": 1e-5, "ulp": 4},
    }
    (evidence_dir / "numeric-golden-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence/numeric-golden"))
    parser.add_argument("--model", type=Path, default=None)
    args = parser.parse_args()
    repo = args.repo.resolve()
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    fixtures = repo / "testkit" / "fixtures" / "models"
    model_path = args.model or (fixtures / f"masi-ids-window-v1-{REVISION}.onnx")
    expected_path = fixtures / f"masi-ids-window-v1-{REVISION}-expected-outputs.json"
    manifest_path = fixtures / f"masi-ids-window-v1-{REVISION}-manifest.json"
    bundle_path = (
        repo / "testkit" / "fixtures" / "repositories"
        / f"masi-ids-window-v1-{REVISION}" / "bundle-manifest.json"
    )

    for path in (model_path, expected_path, manifest_path, bundle_path):
        if not path.is_file():
            print(f"SKIP: pinned fixture artifact not found: {path}", file=sys.stderr)
            return 77

    expected_doc = load(expected_path)
    manifest = load(manifest_path)
    bundle = load(bundle_path)
    if expected_doc.get("schema_version") != "masi-fixture-expected-outputs/v2":
        print("FAIL: expected-outputs schema drifted", file=sys.stderr)
        return 1

    taxonomy = bundle["label_taxonomy"]
    adapter = bundle["output_adapter"]
    # The adapter parameters must be digest-pinned, exactly as the Gateway
    # verifies them at startup.
    adapter_body = {k: v for k, v in adapter.items() if k != "adapter_digest"}
    recomputed = sha256_hex(canonical_json(adapter_body).encode("utf-8"))
    if recomputed != adapter["adapter_digest"]:
        print(
            f"FAIL: adapter_digest mismatch: observed {recomputed}", file=sys.stderr
        )
        return 1
    if bundle["contract_digests"]["output_adapter_digest"] != adapter["adapter_digest"]:
        print("FAIL: output_adapter_digest != adapter_digest", file=sys.stderr)
        return 1

    tolerance = expected_doc["tolerance"]

    try:
        import onnxruntime as ort
    except ImportError:
        print(
            "SKIP: onnxruntime is not installed; install onnxruntime==1.19.0 "
            "to run the numeric golden reference.",
            file=sys.stderr,
        )
        not_run_evidence("ONNXRUNTIME_NOT_INSTALLED", manifest, evidence_dir)
        return 77

    import numpy as np

    session = ort.InferenceSession(
        str(model_path), ort.SessionOptions(), providers=["CPUExecutionProvider"]
    )
    available = session.get_providers()
    if available != ["CPUExecutionProvider"]:
        print(f"FAIL: expected only CPUExecutionProvider, got {available}", file=sys.stderr)
        return 1

    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name

    vector_results: list[dict[str, Any]] = []
    all_pass = True

    for v in expected_doc["vectors"]:
        arr = np.array(v["input"], dtype=np.uint64)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        raw = session.run([output_name], {input_name: arr})[0]
        rows_expected = v["rows"]
        record_count = int(v["record_count"])
        result: dict[str, Any] = {
            "vector_id": v["vector_id"],
            "record_count": record_count,
            "input_digest": v["input_digest"],
            "rows": [],
        }
        vector_ok = raw.shape == (record_count, len(adapter["class_order"]))
        if not vector_ok:
            result["result"] = "FAIL"
            result["reason"] = f"model output shape {raw.shape} != [{record_count},N]"
            vector_results.append(result)
            all_pass = False
            continue

        for i in range(record_count):
            row_raw = [float(x) for x in raw[i].tolist()]
            adapted = apply_adapter(row_raw, taxonomy, adapter)
            exp = rows_expected[i]
            raw_match = all(
                float_within_tolerance(a, b, tolerance)
                for a, b in zip(row_raw, exp["raw_scores"])
            )
            scores_match = all(
                float_within_tolerance(a, b, tolerance)
                for a, b in zip(adapted["scores"], exp["canonical_scores"])
            )
            label_match = adapted["predicted_label"] == exp["predicted_label"]
            decision_match = adapted["decision"] == exp["decision"]
            ood_match = adapted["out_of_distribution"] == exp["out_of_distribution"]
            abstain_match = adapted["abstain"] == exp["abstain"]
            digest_match = adapted["output_digest"] == exp["output_digest"]
            row_ok = all(
                (
                    raw_match,
                    scores_match,
                    label_match,
                    decision_match,
                    ood_match,
                    abstain_match,
                    digest_match,
                )
            )
            vector_ok = vector_ok and row_ok
            result["rows"].append(
                {
                    "row": i,
                    "observed_raw_scores": row_raw,
                    "observed_scores": adapted["scores"],
                    "observed_predicted_label": adapted["predicted_label"],
                    "observed_decision": adapted["decision"],
                    "observed_output_digest": adapted["output_digest"],
                    "expected_decision": exp["decision"],
                    "raw_match": raw_match,
                    "scores_match": scores_match,
                    "label_match": label_match,
                    "decision_match": decision_match,
                    "ood_match": ood_match,
                    "abstain_match": abstain_match,
                    "output_digest_match": digest_match,
                    "result": "PASS" if row_ok else "FAIL",
                }
            )
        result["result"] = "PASS" if vector_ok else "FAIL"
        if not vector_ok:
            all_pass = False
            print(f"FAIL: vector {v['vector_id']} did not match", file=sys.stderr)
        vector_results.append(result)

    evidence = {
        "schema_version": "central-inference-numeric-evidence/v1",
        "test_id": "TEST-INF-NUMERIC-001",
        "requirement_ids": ["MOD-INF-001", "CONTRACT-MODEL-001", "TEST-INF-001"],
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "PASS" if all_pass else "FAIL",
        "qualification": "QUALIFIED" if all_pass else "NOT_QUALIFIED",
        "claim_scope": "reference implementation on ONNX Runtime CPU; serving-path "
        "equivalence is asserted by the real-process black-box test",
        "model_id": manifest["model_id"],
        "model_digest": manifest["model_digest"],
        "model_revision": manifest["revision"],
        "adapter_id": adapter["adapter_id"],
        "adapter_digest": adapter["adapter_digest"],
        "score_domain": taxonomy["score_domain"],
        "canonical_score_encoding": "repeated-float32-le-class-ordered",
        "runtime_profile": "model-runtime-central-cpu/v1",
        "observed_providers": available,
        "selected_equals_observed": available == ["CPUExecutionProvider"],
        "vectors": vector_results,
        "class_order": adapter["class_order"],
        "tolerance": tolerance,
        "nan_policy": expected_doc.get("nan_policy", "reject"),
        "inf_policy": expected_doc.get("inf_policy", "reject"),
    }

    (evidence_dir / "numeric-golden-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
