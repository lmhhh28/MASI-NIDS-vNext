"""Deterministic standard-operator ONNX export and ORT verification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import onnx
import onnxruntime as ort
from onnx import TensorProto, helper, numpy_helper
from onnxmltools import convert_xgboost
from onnxmltools.convert.common.data_types import FloatTensorType

from .canonical import ValidationError, sha256_bytes
from .metrics import PlattCalibration

Float32Array = npt.NDArray[np.float32]
ALLOWED_STANDARD_OPERATORS = {
    "Abs",
    "Add",
    "Cast",
    "Concat",
    "Div",
    "Less",
    "Log",
    "MatMul",
    "Mul",
    "ReduceMean",
    "Relu",
    "Sigmoid",
    "Sub",
    "Where",
}
ALLOWED_ML_OPERATORS = {"TreeEnsembleRegressor"}


def _initializer(name: str, value: npt.ArrayLike) -> onnx.TensorProto:
    return numpy_helper.from_array(np.asarray(value, dtype=np.float32), name=name)


def _preprocess_nodes(
    *, scaler_mean: Float32Array | None, scaler_scale: Float32Array | None
) -> tuple[list[onnx.NodeProto], list[onnx.TensorProto], str]:
    nodes = [
        helper.make_node("Cast", ["features"], ["features_f32"], name="CastFeatures", to=TensorProto.FLOAT),
        helper.make_node("Add", ["features_f32", "one"], ["features_plus_one"], name="AddOne"),
        helper.make_node("Log", ["features_plus_one"], ["log_features"], name="Log1pFeatures"),
    ]
    initializers = [_initializer("one", np.asarray(1.0, dtype=np.float32))]
    output = "log_features"
    if scaler_mean is not None or scaler_scale is not None:
        if scaler_mean is None or scaler_scale is None or scaler_mean.shape != (6,) or scaler_scale.shape != (6,):
            raise ValidationError("scaler_shape", "mean and scale must both be [6]")
        if np.any(scaler_scale <= 0) or not np.all(np.isfinite(scaler_mean)) or not np.all(np.isfinite(scaler_scale)):
            raise ValidationError("scaler_value", "scaler values must be finite and scale positive")
        initializers.extend((_initializer("scaler_mean", scaler_mean), _initializer("scaler_scale", scaler_scale)))
        nodes.extend(
            (
                helper.make_node("Sub", [output, "scaler_mean"], ["centered_features"], name="CenterFeatures"),
                helper.make_node(
                    "Div", ["centered_features", "scaler_scale"], ["scaled_features"], name="ScaleFeatures"
                ),
            )
        )
        output = "scaled_features"
    return nodes, initializers, output


def _calibration_nodes(
    raw_name: str, calibration: PlattCalibration
) -> tuple[list[onnx.NodeProto], list[onnx.TensorProto]]:
    nodes = [
        helper.make_node("Mul", [raw_name, "platt_slope"], ["platt_scaled"], name="PlattScale"),
        helper.make_node("Add", ["platt_scaled", "platt_intercept"], ["platt_logit"], name="PlattIntercept"),
        helper.make_node("Sigmoid", ["platt_logit"], ["attack_probability"], name="AttackProbability"),
        helper.make_node(
            "Sub", ["one_probability", "attack_probability"], ["benign_probability"], name="BenignProbability"
        ),
        helper.make_node(
            "Concat", ["benign_probability", "attack_probability"], ["scores"], name="CanonicalScores", axis=1
        ),
    ]
    initializers = [
        _initializer("platt_slope", np.asarray(calibration.slope, dtype=np.float32)),
        _initializer("platt_intercept", np.asarray(calibration.intercept, dtype=np.float32)),
        _initializer("one_probability", np.asarray(1.0, dtype=np.float32)),
    ]
    return nodes, initializers


def _metadata(model: onnx.ModelProto, metadata: Mapping[str, str]) -> None:
    del model.metadata_props[:]
    for key in sorted(metadata):
        entry = model.metadata_props.add()
        entry.key = key
        entry.value = metadata[key]
    model.producer_name = "masi-offline-ml"
    model.producer_version = "1.0.0"
    model.domain = "masi-nids.example/offline-ml"
    model.model_version = 1
    model.doc_string = ""
    model.graph.doc_string = ""


def _model(graph: onnx.GraphProto, metadata: Mapping[str, str], *, ml_opset: bool = False) -> bytes:
    imports = [helper.make_opsetid("", 15)]
    if ml_opset:
        imports.append(helper.make_opsetid("ai.onnx.ml", 3))
    model = helper.make_model(graph, producer_name="masi-offline-ml", opset_imports=imports, ir_version=10)
    _metadata(model, metadata)
    onnx.checker.check_model(model, full_check=True)
    return model.SerializeToString(deterministic=True)


def export_logistic(
    coefficient: Float32Array,
    intercept: float,
    scaler_mean: Float32Array,
    scaler_scale: Float32Array,
    calibration: PlattCalibration,
    metadata: Mapping[str, str],
) -> bytes:
    if coefficient.shape != (6,):
        raise ValidationError("coefficient_shape", str(coefficient.shape))
    pre_nodes, initializers, transformed = _preprocess_nodes(scaler_mean=scaler_mean, scaler_scale=scaler_scale)
    initializers.extend(
        (
            _initializer("lr_coefficient", coefficient.reshape((6, 1))),
            _initializer("lr_intercept", np.asarray([intercept], dtype=np.float32)),
        )
    )
    nodes = [
        *pre_nodes,
        helper.make_node("MatMul", [transformed, "lr_coefficient"], ["lr_linear"], name="LogisticLinear"),
        helper.make_node("Add", ["lr_linear", "lr_intercept"], ["raw_margin"], name="LogisticIntercept"),
    ]
    calibration_nodes, calibration_initializers = _calibration_nodes("raw_margin", calibration)
    nodes.extend(calibration_nodes)
    initializers.extend(calibration_initializers)
    graph = helper.make_graph(
        nodes,
        "masi-lr-window-binary-v1",
        [helper.make_tensor_value_info("features", TensorProto.UINT64, [None, 6])],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [None, 2])],
        initializer=initializers,
    )
    return _model(graph, metadata)


def export_autoencoder(
    parameters: Mapping[str, Float32Array],
    scaler_mean: Float32Array,
    scaler_scale: Float32Array,
    calibration: PlattCalibration,
    metadata: Mapping[str, str],
) -> bytes:
    required_shapes = {
        "w1": (6, 8),
        "b1": (8,),
        "w2": (8, 3),
        "b2": (3,),
        "w3": (3, 8),
        "b3": (8,),
        "w4": (8, 6),
        "b4": (6,),
    }
    for name, shape in required_shapes.items():
        if name not in parameters or parameters[name].shape != shape or not np.all(np.isfinite(parameters[name])):
            raise ValidationError("autoencoder_parameter", f"{name}:{parameters.get(name, np.empty(0)).shape}!={shape}")
    pre_nodes, initializers, transformed = _preprocess_nodes(scaler_mean=scaler_mean, scaler_scale=scaler_scale)
    initializers.extend(_initializer(name, parameters[name]) for name in required_shapes)
    initializers.extend(
        (
            _initializer("smooth_half", np.asarray(0.5, dtype=np.float32)),
            _initializer("smooth_one", np.asarray(1.0, dtype=np.float32)),
        )
    )
    nodes = [
        *pre_nodes,
        helper.make_node("MatMul", [transformed, "w1"], ["ae_l1_mm"], name="AELayer1MatMul"),
        helper.make_node("Add", ["ae_l1_mm", "b1"], ["ae_l1_bias"], name="AELayer1Bias"),
        helper.make_node("Relu", ["ae_l1_bias"], ["ae_l1"], name="AELayer1Relu"),
        helper.make_node("MatMul", ["ae_l1", "w2"], ["ae_l2_mm"], name="AELayer2MatMul"),
        helper.make_node("Add", ["ae_l2_mm", "b2"], ["ae_l2_bias"], name="AELayer2Bias"),
        helper.make_node("Relu", ["ae_l2_bias"], ["ae_l2"], name="AELayer2Relu"),
        helper.make_node("MatMul", ["ae_l2", "w3"], ["ae_l3_mm"], name="AELayer3MatMul"),
        helper.make_node("Add", ["ae_l3_mm", "b3"], ["ae_l3_bias"], name="AELayer3Bias"),
        helper.make_node("Relu", ["ae_l3_bias"], ["ae_l3"], name="AELayer3Relu"),
        helper.make_node("MatMul", ["ae_l3", "w4"], ["ae_output_mm"], name="AEOutputMatMul"),
        helper.make_node("Add", ["ae_output_mm", "b4"], ["ae_reconstruction"], name="AEOutputBias"),
        helper.make_node("Sub", [transformed, "ae_reconstruction"], ["ae_difference"], name="AEDifference"),
        helper.make_node("Abs", ["ae_difference"], ["ae_absolute"], name="AEAbsoluteDifference"),
        helper.make_node("Less", ["ae_absolute", "smooth_one"], ["ae_quadratic_mask"], name="AEQuadraticMask"),
        helper.make_node("Mul", ["ae_difference", "ae_difference"], ["ae_squared"], name="AESquaredDifference"),
        helper.make_node("Mul", ["ae_squared", "smooth_half"], ["ae_quadratic"], name="AEQuadraticResidual"),
        helper.make_node("Sub", ["ae_absolute", "smooth_half"], ["ae_linear"], name="AELinearResidual"),
        helper.make_node(
            "Where", ["ae_quadratic_mask", "ae_quadratic", "ae_linear"], ["ae_residuals"], name="AESmoothL1"
        ),
        helper.make_node(
            "ReduceMean", ["ae_residuals"], ["raw_anomaly_score"], name="AEResidualMean", axes=[1], keepdims=1
        ),
    ]
    calibration_nodes, calibration_initializers = _calibration_nodes("raw_anomaly_score", calibration)
    nodes.extend(calibration_nodes)
    initializers.extend(calibration_initializers)
    graph = helper.make_graph(
        nodes,
        "masi-ae-window-benign-v1",
        [helper.make_tensor_value_info("features", TensorProto.UINT64, [None, 6])],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [None, 2])],
        initializer=initializers,
    )
    return _model(graph, metadata)


def export_xgboost(model: Any, calibration: PlattCalibration, metadata: Mapping[str, str]) -> bytes:
    converted = convert_xgboost(
        model,
        name=f"masi-xgb-window-binary-seed-{metadata.get('seed', 'unknown')}",
        initial_types=[("log_features", FloatTensorType([None, 6]))],
        target_opset=15,
    )
    tree_nodes = [
        node for node in converted.graph.node if node.domain == "ai.onnx.ml" and node.op_type == "TreeEnsembleRegressor"
    ]
    if len(tree_nodes) != 1 or len(converted.graph.node) != 1:
        raise ValidationError(
            "xgboost_operator_closure", str([(node.domain, node.op_type) for node in converted.graph.node])
        )
    raw_name = converted.graph.output[0].name
    converted.graph.input[0].name = "features"
    converted.graph.input[0].type.tensor_type.elem_type = TensorProto.UINT64
    pre_nodes, pre_initializers, transformed = _preprocess_nodes(scaler_mean=None, scaler_scale=None)
    if transformed != "log_features":
        raise ValidationError("xgboost_preprocess", transformed)
    calibration_nodes, calibration_initializers = _calibration_nodes(raw_name, calibration)
    existing_nodes = list(converted.graph.node)
    del converted.graph.node[:]
    converted.graph.node.extend(pre_nodes + existing_nodes + calibration_nodes)
    converted.graph.initializer.extend(pre_initializers + calibration_initializers)
    del converted.graph.output[:]
    converted.graph.output.extend([helper.make_tensor_value_info("scores", TensorProto.FLOAT, [None, 2])])
    converted.graph.name = "masi-xgb-window-binary-v1"
    converted.ir_version = 10
    if not any(entry.domain in ("", "ai.onnx") for entry in converted.opset_import):
        default_opset = converted.opset_import.add()
        default_opset.domain = ""
        default_opset.version = 15
    _metadata(converted, metadata)
    onnx.checker.check_model(converted, full_check=True)
    return converted.SerializeToString(deterministic=True)


def inspect_model(payload: bytes) -> dict[str, Any]:
    model = onnx.load_model_from_string(payload)
    onnx.checker.check_model(model, full_check=True)
    operators: list[dict[str, str]] = []
    for node in model.graph.node:
        domain = node.domain or "ai.onnx"
        if node.domain in ("", "ai.onnx"):
            if node.op_type not in ALLOWED_STANDARD_OPERATORS:
                raise ValidationError("onnx_operator", f"{domain}:{node.op_type}")
        elif node.domain == "ai.onnx.ml":
            if node.op_type not in ALLOWED_ML_OPERATORS:
                raise ValidationError("onnx_ml_operator", node.op_type)
        else:
            raise ValidationError("onnx_custom_operator", f"{domain}:{node.op_type}")
        if node.op_type == "ZipMap":
            raise ValidationError("onnx_zipmap", node.name)
        operators.append({"domain": domain, "operator": node.op_type})
    if len(model.graph.input) != 1 or model.graph.input[0].name != "features":
        raise ValidationError("onnx_input", str([value.name for value in model.graph.input]))
    if model.graph.input[0].type.tensor_type.elem_type != TensorProto.UINT64:
        raise ValidationError("onnx_input_dtype", str(model.graph.input[0].type.tensor_type.elem_type))
    if len(model.graph.output) != 1 or model.graph.output[0].name != "scores":
        raise ValidationError("onnx_output", str([value.name for value in model.graph.output]))
    if model.graph.output[0].type.tensor_type.elem_type != TensorProto.FLOAT:
        raise ValidationError("onnx_output_dtype", str(model.graph.output[0].type.tensor_type.elem_type))
    session = ort.InferenceSession(payload, providers=["CPUExecutionProvider"])
    if session.get_providers() != ["CPUExecutionProvider"]:
        raise ValidationError("ort_provider", str(session.get_providers()))
    imports = {entry.domain or "ai.onnx": entry.version for entry in model.opset_import}
    metadata = {entry.key: entry.value for entry in model.metadata_props}
    return {
        "model_digest": sha256_bytes(payload),
        "bytes": len(payload),
        "ir_version": model.ir_version,
        "opsets": imports,
        "operators": operators,
        "input": {"name": "features", "dtype": "uint64", "shape": ["N", 6]},
        "output": {"name": "scores", "dtype": "float32", "shape": ["N", 2]},
        "providers": session.get_providers(),
        "metadata": dict(sorted(metadata.items())),
        "zipmap": False,
        "custom_operators": False,
    }


def rewrite_metadata(payload: bytes, metadata: Mapping[str, str]) -> bytes:
    """Replace redundant ONNX metadata without changing the qualified graph."""

    model = onnx.load_model_from_string(payload)
    _metadata(model, metadata)
    onnx.checker.check_model(model, full_check=True)
    return model.SerializeToString(deterministic=True)


def run_ort(payload: bytes, features: npt.NDArray[np.uint64]) -> npt.NDArray[np.float32]:
    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = 1
    session_options.inter_op_num_threads = 1
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(payload, sess_options=session_options, providers=["CPUExecutionProvider"])
    result = session.run(["scores"], {"features": np.asarray(features, dtype=np.uint64)})[0]
    scores = np.asarray(result, dtype=np.float32)
    if scores.ndim != 2 or scores.shape[1] != 2 or not np.all(np.isfinite(scores)):
        raise ValidationError("ort_output", str(scores.shape))
    return scores
