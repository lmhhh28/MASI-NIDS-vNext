#!/usr/bin/env python3
"""Generate the minimal ONNX fixture for Central Inference module E2E.

This is a SERVING-FRAMEWORK fixture, not a trained detection model. It produces
a deterministic linear classifier: uint64 features -> Cast -> Gemm -> 2 logits.
The purpose is to prove the Gateway+Triton+ORT CPU serving stack loads, warms up,
reads back, and executes correctly. It does NOT prove detection accuracy (that is
the Offline ML module's responsibility).

Two immutable revisions exist and are never regenerated in place:

  r1 (revision ...0001): fixed input shape [1, 6]. Cannot be batched by Triton
     (`max_batch_size: 0`). Kept only for reproducibility of already-published
     digests; do not use for new evidence.
  r2 (revision ...0002): symbolic batch dimension ["N", 6] so the frozen
     `inference-central-grpc-batch/v1` batch profile (max_batch_size 256,
     dynamic batching) is actually exercisable.

Output files (in the parent directory of this script's directory):
  - masi-ids-window-v1-<rev>.onnx
  - masi-ids-window-v1-<rev>-manifest.json
  - masi-ids-window-v1-<rev>-expected-outputs.json

r2 additionally carries `contracts/model/v1`-shaped `label_taxonomy` and
`output_adapter` blocks plus the derived feature/label/adapter contract digests,
so the Gateway can bind the output adapter to data instead of hardcoded C++
constants. See `contracts/inference/v1/profile.json#output_adapter_binding`.

Usage:
  ./generate_fixture.py --revision r2
"""
import argparse
import hashlib
import json
import os

import numpy as np
import onnx
import onnx.helper
import onnx.numpy_helper
from onnx import ModelProto, TensorProto, helper

MODEL_ID = "masi-ids-window-v1"
INPUT_NAME = "features"
OUTPUT_NAME = "scores"
FEATURE_COUNT = 6
NUM_CLASSES = 2
IR_VERSION = 10
OPSET = 21

REVISIONS = {
    "r1": {
        "revision": "0000000000000000000000000000000000000001",
        "batch_dim": 1,
    },
    "r2": {
        "revision": "0000000000000000000000000000000000000002",
        "batch_dim": "N",
    },
    "r3": {
        "revision": "0000000000000000000000000000000000000003",
        "batch_dim": "N",
    },
}

FIELD_ORDER = [
    "packets",
    "bytes",
    "nonzero_cells",
    "max_cell_packets",
    "max_cell_bytes",
    "snapshots",
]

# Deterministic weights (fixed seed; NOT trained -- framework fixture only).
# Dense layer: W shape [6,2] (input, output), B shape [2].
# Gemm standard: input[N,6] @ W[6,2] + B[2] = [N,2].
# Class order: [0=benign, 1=alert].
np.random.seed(20260816)
W = np.random.randn(FEATURE_COUNT, NUM_CLASSES).astype(np.float32)
W[:, 0] = -0.5  # benign weights negative
W[:, 1] = 0.5  # alert weights positive
B = np.array([0.1, -0.1], dtype=np.float32)


def weights_for_revision(rev_tag: str) -> np.ndarray:
    weights = W.copy()
    if rev_tag == "r3":
        # r3 deliberately makes feature 0 evidence for the baseline class.
        # This gives the real serving E2E observable benign, alert and OOD/
        # abstain rows instead of a fixture that can only ever alert.
        weights[0, 0] = 0.5
        weights[0, 1] = -0.5
    return weights


def canonical_json(obj) -> str:
    """Canonical JSON form used for every contract digest in this fixture.

    Sorted keys, no whitespace. nlohmann::json::dump() produces the identical
    byte string for the same object, so C++ recomputes the same digests.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def build_model(rev_tag: str, batch_dim) -> ModelProto:
    input_shape = [batch_dim, FEATURE_COUNT]
    output_shape = [batch_dim, NUM_CLASSES]
    features = helper.make_tensor_value_info(INPUT_NAME, TensorProto.UINT64, input_shape)
    features_f32 = "features_f32"
    scores = helper.make_tensor_value_info(OUTPUT_NAME, TensorProto.FLOAT, output_shape)

    weights = weights_for_revision(rev_tag)
    W_init = helper.make_tensor(
        "W", TensorProto.FLOAT, [FEATURE_COUNT, NUM_CLASSES], weights.flatten().tolist()
    )
    B_init = helper.make_tensor("B", TensorProto.FLOAT, [NUM_CLASSES], B.flatten().tolist())

    cast_node = helper.make_node(
        "Cast",
        inputs=[INPUT_NAME],
        outputs=[features_f32],
        name="cast-uint64-to-float32",
        to=TensorProto.FLOAT,
    )
    gemm = helper.make_node(
        "Gemm",
        inputs=[features_f32, "W", "B"],
        outputs=[OUTPUT_NAME],
        name="linear-classifier",
        alpha=1.0,
        beta=1.0,
    )

    graph = helper.make_graph(
        [cast_node, gemm],
        f"{MODEL_ID}-{rev_tag}-graph",
        [features],
        [scores],
        [W_init, B_init],
    )

    model = helper.make_model(
        graph,
        producer_name="testkit-fixtures-models",
        producer_version="1",
        model_version=1,
        ir_version=IR_VERSION,
        opset_imports=[helper.make_operatorsetid("", OPSET)],
    )
    onnx.checker.check_model(model)
    return model


def raw_logits(features: np.ndarray, rev_tag: str) -> np.ndarray:
    """Deterministic raw model output for an [N,6] uint64 input."""
    return features.astype(np.float32) @ weights_for_revision(rev_tag) + B


def stable_softmax(row: np.ndarray) -> np.ndarray:
    """Same stable softmax the qualified adapter implementation performs."""
    shifted = row.astype(np.float64) - float(np.max(row))
    exp = np.exp(shifted)
    return (exp / np.sum(exp)).astype(np.float32)


def apply_adapter(row_logits: np.ndarray, taxonomy: dict, adapter: dict) -> dict:
    """Reference implementation of `masi-window-adapter-v1`.

    Mirrors `contracts/inference/v1/profile.json#output_adapter_binding`:
      - normalization by `label_taxonomy.score_domain`
      - canonical scores ordered by `output_adapter.class_order`
      - top-1 over canonical scores
      - baseline label is `class_order[0]`
      - alert iff predicted != baseline and score >= output_adapter.threshold
      - abstain iff top-1 score < label_taxonomy.threshold.value
      - OOD iff the maximum normalized score is below the adapter threshold;
        OOD always forces abstention
    """
    domain = taxonomy["score_domain"]
    if domain == "logit":
        normalized = stable_softmax(row_logits)
    elif domain == "probability":
        normalized = row_logits.astype(np.float32)
    else:
        raise ValueError(f"score_domain not supported by adapter: {domain}")

    class_order = adapter["class_order"]
    scores = np.array([normalized[label] for label in class_order], dtype=np.float32)
    top_index = int(np.argmax(scores))
    predicted_label = int(class_order[top_index])
    top_score = float(scores[top_index])
    baseline_label = int(class_order[0])

    ood_policy = adapter.get("ood_policy")
    if ood_policy is None:
        out_of_distribution = False
    else:
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

    return {
        "canonical_scores": [float(s) for s in scores],
        "predicted_label": predicted_label,
        "decision": decision,
        "out_of_distribution": out_of_distribution,
        "abstain": abstain,
        "quality": "valid",
        "output_digest": sha256_bytes(scores.tobytes()),
    }


def build_contract_blocks(enable_ood: bool) -> tuple[dict, dict, dict]:
    """Build immutable adapter blocks; OOD was introduced by r3."""
    none_calibration_digest = sha256_text("none")

    feature_schema = {
        "field_order": FIELD_ORDER,
        "dtype": "uint64-le",
        "shape": [1, FEATURE_COUNT],
        "units": "mixed",
    }

    label_taxonomy = {
        "label_ids": [0, 1],
        "id_reuse_policy": "never-reuse",
        "mode": "single-label",
        "score_domain": "logit",
        "threshold": {"kind": "fixed", "value": 0.0},
        "calibration": {"kind": "none", "config_digest": none_calibration_digest},
        "class_order_policy": "stable-explicit-in-manifest",
        "unknown_label_policy": (
            "old-reader-preserves-unknown-without-triggering-old-effect-policy"
        ),
    }

    adapter_body = {
        "adapter_id": "masi-window-adapter-v1",
        "version": "v1",
        "mapping_kind": "deterministic-implementation",
        "class_order": [0, 1],
        "axis": 1,
        "top_k": 1,
        "threshold": 0.5,
        "calibration": none_calibration_digest,
        "executable_policy": "no-executable-code-injected-from-bundle",
    }
    if enable_ood:
        adapter_body["ood_policy"] = {
            "mode": "max-probability-below-threshold",
            "threshold": 0.55,
            "decision": "abstain",
        }
    # adapter_digest is the digest of the adapter body without the digest field.
    adapter_digest = sha256_text(canonical_json(adapter_body))
    output_adapter = dict(adapter_body)
    output_adapter["adapter_digest"] = adapter_digest

    return feature_schema, label_taxonomy, output_adapter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", choices=sorted(REVISIONS), default="r2")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Override output directory (default: the fixtures/models directory).",
    )
    args = parser.parse_args()

    rev_tag = args.revision
    revision = REVISIONS[rev_tag]["revision"]
    batch_dim = REVISIONS[rev_tag]["batch_dim"]

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out_dir or os.path.dirname(here)
    os.makedirs(out_dir, exist_ok=True)

    model = build_model(rev_tag, batch_dim)
    onnx_path = os.path.join(out_dir, f"{MODEL_ID}-{rev_tag}.onnx")
    onnx.save(model, onnx_path)

    # Metadata props (redundant self-description, must match manifest).
    model.metadata_props.add(key="masi.model_id", value=MODEL_ID)
    model.metadata_props.add(key="masi.revision", value=revision)
    onnx.save(model, onnx_path)
    onnx_bytes = open(onnx_path, "rb").read()

    bundle_digest = sha256_bytes(onnx_bytes)
    model_digest = sha256_bytes(onnx_bytes)

    golden_inputs = [
        np.array([[5, 10, 3, 1, 2, 1]], dtype=np.uint64),  # benign-leaning
        np.array([[100, 200, 50, 10, 20, 10]], dtype=np.uint64),  # alert-leaning
        np.array([[0, 0, 0, 0, 0, 0]], dtype=np.uint64),  # zero input
        np.array([[100, 0, 0, 0, 0, 0]], dtype=np.uint64),  # r3 benign
    ]

    if rev_tag == "r1":
        # Legacy revision: single-row only, raw logits, no adapter block.
        expected = []
        for i, inp in enumerate(golden_inputs):
            logits = raw_logits(inp, rev_tag).reshape(1, -1)
            predicted_label = int(np.argmax(logits, axis=1)[0])
            out = {
                "scores": logits.flatten().tolist(),
                "predicted_label": predicted_label,
                "decision": "benign" if predicted_label == 0 else "alert",
            }
            expected.append(
                {
                    "vector_id": f"fixture-expected-{i:04d}",
                    "input": inp.flatten().tolist(),
                    "input_digest": sha256_bytes(inp.tobytes()),
                    "expected_output": out,
                    "output_digest": sha256_bytes(
                        np.array(out["scores"], dtype=np.float32).tobytes()
                    ),
                }
            )
        expected_doc = {
            "schema_version": "masi-fixture-expected-outputs/v1",
            "model_id": MODEL_ID,
            "revision": revision,
            "input_shape": [1, FEATURE_COUNT],
            "input_dtype": "uint64-le",
            "class_order": [0, 1],
            "tolerance": {"absolute": 1e-6, "relative": 1e-5, "ulp": 4},
            "nan_policy": "reject",
            "inf_policy": "reject",
            "vectors": expected,
        }
        manifest = {
            "schema_version": "masi-fixture-manifest/v1",
            "model_id": MODEL_ID,
            "revision": revision,
            "bundle_digest": bundle_digest,
            "model_digest": model_digest,
            "feature_schema": {
                "field_order": FIELD_ORDER,
                "dtype": "uint64-le",
                "shape": [1, FEATURE_COUNT],
                "units": "mixed",
            },
            "label_taxonomy": {
                "label_ids": [0, 1],
                "id_reuse_policy": "never-reuse",
                "mode": "single-label",
                "class_order": [0, 1],
                "class_names": {0: "benign", 1: "alert"},
            },
            "output_adapter": {
                "adapter_id": "masi-window-adapter-v1",
                "version": "v1",
                "mapping_kind": "deterministic-implementation",
                "class_order": [0, 1],
                "axis": 1,
                "top_k": 1,
                "threshold": 0.5,
            },
            "onnx": {
                "ir_version": IR_VERSION,
                "opset": OPSET,
                "producer": "testkit-fixtures-models",
            },
            "purpose": "serving-framework-e2e-not-detection-accuracy",
        }
    else:
        feature_schema, label_taxonomy, output_adapter = build_contract_blocks(
            enable_ood=rev_tag == "r3"
        )

        repo_root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
        wire_profile_path = os.path.join(repo_root, "contracts/inference/v1/profile.json")
        runtime_profile_path = os.path.join(
            repo_root, "contracts/inference/v1/model-runtime-central-cpu.json"
        )
        optimization_profile_path = os.path.join(
            repo_root, "contracts/inference/v1/optimization-profile-central-cpu.json"
        )
        for contract_path in (
            wire_profile_path,
            runtime_profile_path,
            optimization_profile_path,
        ):
            if not os.path.isfile(contract_path):
                raise FileNotFoundError(contract_path)

        feature_contract_digest = sha256_text(canonical_json(feature_schema))
        label_contract_digest = sha256_text(canonical_json(label_taxonomy))
        output_adapter_digest = output_adapter["adapter_digest"]
        binding_identity = None
        model_revision_digest = None
        if rev_tag == "r3":
            binding_identity = {
                "model_id": MODEL_ID,
                "revision": revision,
                "model_digest": model_digest,
                "model_bundle_digest": bundle_digest,
                "feature_contract_digest": feature_contract_digest,
                "label_contract_digest": label_contract_digest,
                "output_adapter_digest": output_adapter_digest,
                "inference_wire_profile_digest": sha256_file(wire_profile_path),
                "runtime_profile_id": "model-runtime-central-cpu/v1",
                "runtime_profile_digest": sha256_file(runtime_profile_path),
                "optimization_profile_id": "optimization-profile-central-cpu/v1",
                "optimization_profile_digest": sha256_file(optimization_profile_path),
            }
            model_revision_digest = sha256_text(canonical_json(binding_identity))

        # Multi-row batch vectors so the batched wire path has golden coverage.
        batch_inputs = [
            np.concatenate(golden_inputs, axis=0),  # N=3
            np.array([[5, 10, 3, 16, 32, 1], [0, 0, 0, 0, 0, 0]], dtype=np.uint64),  # N=2
        ]

        vectors = []
        for i, inp in enumerate(golden_inputs + batch_inputs):
            logits = raw_logits(inp, rev_tag)
            rows = []
            for row in range(logits.shape[0]):
                adapted = apply_adapter(logits[row], label_taxonomy, output_adapter)
                adapted["raw_scores"] = [float(v) for v in logits[row]]
                rows.append(adapted)
            vectors.append(
                {
                    "vector_id": f"fixture-expected-{i:04d}",
                    "record_count": int(inp.shape[0]),
                    "input": inp.tolist(),
                    "input_digest": sha256_bytes(inp.tobytes()),
                    "rows": rows,
                }
            )

        expected_doc = {
            "schema_version": "masi-fixture-expected-outputs/v2",
            "model_id": MODEL_ID,
            "revision": revision,
            "input_shape": ["N", FEATURE_COUNT],
            "record_shape": [1, FEATURE_COUNT],
            "input_dtype": "uint64-le",
            "class_order": output_adapter["class_order"],
            "score_domain": label_taxonomy["score_domain"],
            "canonical_score_encoding": "repeated-float32-le-class-ordered",
            "adapter_id": output_adapter["adapter_id"],
            "adapter_digest": output_adapter["adapter_digest"],
            "tolerance": {"absolute": 1e-6, "relative": 1e-5, "ulp": 4},
            "nan_policy": "reject",
            "inf_policy": "reject",
            "vectors": vectors,
        }

        manifest = {
            "schema_version": "masi-fixture-manifest/v2",
            "model_id": MODEL_ID,
            "revision": revision,
            "bundle_digest": bundle_digest,
            "model_digest": model_digest,
            "feature_schema": feature_schema,
            "label_taxonomy": label_taxonomy,
            "output_adapter": output_adapter,
            "contract_digests": {
                "feature_contract_digest": feature_contract_digest,
                "label_contract_digest": label_contract_digest,
                "output_adapter_digest": output_adapter_digest,
            },
            "onnx": {
                "ir_version": IR_VERSION,
                "opset": OPSET,
                "producer": "testkit-fixtures-models",
                "input_shape": ["N", FEATURE_COUNT],
                "output_shape": ["N", NUM_CLASSES],
            },
            "triton": {
                "max_batch_size": 256,
                "preferred_batch_size": [32, 64, 128, 256],
                "max_queue_delay_microseconds": 200,
                "max_queue_size": 1024,
                "instance_group": {"kind": "KIND_CPU", "count": 1},
            },
            "purpose": "serving-framework-e2e-not-detection-accuracy",
        }
        if binding_identity is not None:
            manifest["model_revision_digest"] = model_revision_digest
            manifest["binding_identity"] = binding_identity

    expected_path = os.path.join(out_dir, f"{MODEL_ID}-{rev_tag}-expected-outputs.json")
    with open(expected_path, "w") as f:
        json.dump(expected_doc, f, indent=2)
    manifest["expected_outputs_digest"] = sha256_file(expected_path)

    manifest_path = os.path.join(out_dir, f"{MODEL_ID}-{rev_tag}-manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print("Generated:")
    print(f"  {onnx_path} ({len(onnx_bytes)} bytes, {bundle_digest})")
    print(f"  {manifest_path}")
    print(f"  {expected_path}")
    if rev_tag != "r1":
        print(f"  adapter_digest: {manifest['contract_digests']['output_adapter_digest']}")
        print(f"  feature_contract_digest: {manifest['contract_digests']['feature_contract_digest']}")
        print(f"  label_contract_digest: {manifest['contract_digests']['label_contract_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
