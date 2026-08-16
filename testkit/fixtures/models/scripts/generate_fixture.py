#!/usr/bin/env python3
"""Generate the minimal ONNX fixture for Central Inference module E2E.

This is a SERVING-FRAMEWORK fixture, not a trained detection model. It produces
a deterministic linear classifier: input [1,6] uint64 -> Dense -> sigmoid -> 2-class.
The purpose is to prove the Gateway+Triton+ORT CPU serving stack loads, warms up,
reads back, and executes correctly. It does NOT prove detection accuracy (that is
the Offline ML module's responsibility).

Output files (in the directory containing this script's parent):
  - masi-ids-window-v1-r1.onnx   (the ONNX model)
  - masi-ids-window-v1-r1-manifest.json  (immutable identity + digests)
  - masi-ids-window-v1-r1-expected-outputs.json  (golden expected outputs for numeric check)
"""
import json
import hashlib
import os

import numpy as np
import onnx
import onnx.helper
import onnx.numpy_helper
from onnx import TensorProto, ModelProto, helper


MODEL_ID = "masi-ids-window-v1"
REVISION = "0000000000000000000000000000000000000001"
INPUT_NAME = "features"
OUTPUT_NAME = "scores"
INPUT_SHAPE = [1, 6]
NUM_CLASSES = 2
IR_VERSION = 10
OPSET = 21

# Deterministic weights (fixed seed; NOT trained — framework fixture only).
# Dense layer: W shape [6,2] (input, output), B shape [2]. Gemm standard: input[1,6] @ W[6,2] = [1,2].
# Class order: [0=benign, 1=alert].
np.random.seed(20260816)
W = np.random.randn(6, NUM_CLASSES).astype(np.float32)
W[:, 0] = -0.5  # benign weights negative
W[:, 1] = 0.5   # alert weights positive
B = np.array([0.1, -0.1], dtype=np.float32)


def build_model() -> ModelProto:
    features = helper.make_tensor_value_info(INPUT_NAME, TensorProto.UINT64, INPUT_SHAPE)
    features_f32 = "features_f32"
    scores = helper.make_tensor_value_info(OUTPUT_NAME, TensorProto.FLOAT, [1, NUM_CLASSES])

    W_init = helper.make_tensor("W", TensorProto.FLOAT, [6, NUM_CLASSES], W.flatten().tolist())
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
        "masi-ids-window-v1-r1-graph",
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


def compute_expected_output(features: np.ndarray) -> dict:
    """Deterministic expected output for a [1,6] uint64 input."""
    features_f32 = features.astype(np.float32)
    logits = features_f32 @ W + B  # [1,6] @ [6,2] + [2] = [1,2]
    logits = logits.reshape(1, -1)  # ensure [1,2]
    predicted_label = int(np.argmax(logits, axis=1)[0])
    decision = "benign" if predicted_label == 0 else "alert"
    return {
        "scores": logits.flatten().tolist(),
        "predicted_label": predicted_label,
        "decision": decision,
    }


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.dirname(here)

    model = build_model()
    onnx_path = os.path.join(out_dir, "masi-ids-window-v1-r1.onnx")
    onnx.save(model, onnx_path)
    onnx_bytes = open(onnx_path, "rb").read()

    # Metadata props (redundant self-description, must match manifest).
    model.metadata_props.add(key="masi.model_id", value=MODEL_ID)
    model.metadata_props.add(key="masi.revision", value=REVISION)
    onnx.save(model, onnx_path)
    onnx_bytes = open(onnx_path, "rb").read()

    bundle_digest = sha256_bytes(onnx_bytes)
    model_digest = sha256_bytes(onnx_bytes)

    # Expected outputs for golden numeric check.
    golden_inputs = [
        np.array([[5, 10, 3, 1, 2, 1]], dtype=np.uint64),   # benign-leaning
        np.array([[100, 200, 50, 10, 20, 10]], dtype=np.uint64),  # alert-leaning
        np.array([[0, 0, 0, 0, 0, 0]], dtype=np.uint64),     # zero input
    ]
    expected = []
    for i, inp in enumerate(golden_inputs):
        out = compute_expected_output(inp)
        expected.append({
            "vector_id": f"fixture-expected-{i:04d}",
            "input": inp.flatten().tolist(),
            "input_digest": sha256_bytes(inp.tobytes()),
            "expected_output": out,
            "output_digest": sha256_bytes(
                np.array(out["scores"], dtype=np.float32).tobytes()
            ),
        })

    expected_path = os.path.join(out_dir, "masi-ids-window-v1-r1-expected-outputs.json")
    with open(expected_path, "w") as f:
        json.dump({
            "schema_version": "masi-fixture-expected-outputs/v1",
            "model_id": MODEL_ID,
            "revision": REVISION,
            "input_shape": INPUT_SHAPE,
            "input_dtype": "uint64-le",
            "class_order": [0, 1],
            "tolerance": {"absolute": 1e-6, "relative": 1e-5, "ulp": 4},
            "nan_policy": "reject",
            "inf_policy": "reject",
            "vectors": expected,
        }, f, indent=2)

    expected_digest = sha256_file(expected_path)

    # Manifest (immutable identity).
    manifest = {
        "schema_version": "masi-fixture-manifest/v1",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "bundle_digest": bundle_digest,
        "model_digest": model_digest,
        "feature_schema": {
            "field_order": ["packets", "bytes", "nonzero_cells", "max_cell_packets", "max_cell_bytes", "snapshots"],
            "dtype": "uint64-le",
            "shape": INPUT_SHAPE,
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
        "expected_outputs_digest": expected_digest,
    }
    manifest_path = os.path.join(out_dir, "masi-ids-window-v1-r1-manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Generated:")
    print(f"  {onnx_path} ({len(onnx_bytes)} bytes, {bundle_digest})")
    print(f"  {manifest_path}")
    print(f"  {expected_path}")
    print(f"Expected outputs: {len(expected)} vectors")


if __name__ == "__main__":
    main()