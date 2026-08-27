"""Deterministic ADR-0019 LR, XGBoost, and NumPy autoencoder candidates."""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import xgboost as xgb
from sklearn.linear_model import LogisticRegression, SGDClassifier
from sklearn.metrics import average_precision_score, log_loss

from .canonical import ValidationError, sha256_bytes
from .dataset import DatasetData
from .metrics import PlattCalibration, Thresholds, fit_platt, quality_metrics, select_thresholds
from .onnx_export import export_autoencoder, export_logistic, export_xgboost, inspect_model, run_ort

Float32Array = npt.NDArray[np.float32]
Float64Array = npt.NDArray[np.float64]


@dataclass(frozen=True)
class Standardization:
    mean: Float32Array
    scale: Float32Array

    def transform(self, values: Float32Array) -> Float32Array:
        return ((values - self.mean) / self.scale).astype(np.float32, copy=False)

    def as_dict(self) -> dict[str, list[float]]:
        return {"mean": self.mean.astype(float).tolist(), "scale": self.scale.astype(float).tolist()}


@dataclass(frozen=True)
class AutoencoderState:
    parameters: dict[str, Float32Array]
    scaler: Standardization
    epochs: int
    early_stop_loss: float


@dataclass(frozen=True)
class TrainedCandidate:
    candidate_id: str
    seed: int
    model: object
    scaler: Standardization | None
    calibration: PlattCalibration
    thresholds: Thresholds
    hyperparameters: dict[str, Any]
    training_evidence: dict[str, Any]
    metrics: dict[str, Any]
    onnx_payload: bytes
    onnx_evidence: dict[str, Any]
    blind_indices: npt.NDArray[np.int64]
    blind_raw: Float64Array
    blind_probability: Float64Array


def log_features(features: npt.ArrayLike) -> Float32Array:
    values = np.asarray(features, dtype=np.float32)
    transformed = np.log1p(values).astype(np.float32, copy=False)
    if transformed.ndim != 2 or transformed.shape[1] != 6 or not np.all(np.isfinite(transformed)):
        raise ValidationError("feature_transform", str(transformed.shape))
    return transformed


def weighted_standardization(values: Float32Array, weights: npt.ArrayLike) -> Standardization:
    weight_values = np.asarray(weights, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 6 or weight_values.shape != (values.shape[0],):
        raise ValidationError("standardization_shape", f"{values.shape}:{weight_values.shape}")
    total = float(weight_values.sum())
    if total <= 0 or not np.isfinite(total):
        raise ValidationError("standardization_weight", str(total))
    mean64 = np.average(values.astype(np.float64), axis=0, weights=weight_values)
    variance64 = np.average((values.astype(np.float64) - mean64) ** 2, axis=0, weights=weight_values)
    scale64 = np.sqrt(variance64)
    scale64[scale64 == 0.0] = 1.0
    mean = mean64.astype(np.float32)
    scale = scale64.astype(np.float32)
    if not np.all(np.isfinite(mean)) or not np.all(np.isfinite(scale)) or np.any(scale <= 0):
        raise ValidationError("standardization_finite", "mean/scale invalid")
    return Standardization(mean=mean, scale=scale)


def _sigmoid(raw: npt.ArrayLike) -> Float64Array:
    values = np.clip(np.asarray(raw, dtype=np.float64), -80.0, 80.0)
    return (1.0 / (1.0 + np.exp(-values))).astype(np.float64, copy=False)


def _families(dataset: DatasetData, indices: npt.NDArray[np.int64]) -> npt.NDArray[np.str_]:
    return np.asarray(
        [str(cast(dict[str, Any], dataset.records[int(index)]["label"])["family"]) for index in indices], dtype=np.str_
    )


def _metadata(base: dict[str, str], candidate_id: str, seed: int) -> dict[str, str]:
    result = dict(base)
    result.update(
        {
            "candidate_id": candidate_id,
            "class_order": "benign=0,attack=1",
            "feature_dtype": "uint64-le",
            "feature_order": (
                "aggregate_packets,aggregate_bytes,reported_nonzero_cells,"
                "max_cell_packets,max_cell_bytes,snapshot_count"
            ),
            "output_domain": "finite-float32-probability",
            "runtime_profile": "model-runtime-central-cpu/v1",
            "seed": str(seed),
        }
    )
    return result


def _ort_numeric(
    payload: bytes,
    features: npt.NDArray[np.uint64],
    reference_probability: Float64Array,
) -> dict[str, Any]:
    scores = run_ort(payload, features)
    probabilities = scores[:, 1].astype(np.float64)
    error = np.abs(probabilities - reference_probability)
    row_sums = scores.astype(np.float64).sum(axis=1)
    if np.max(error, initial=0.0) > 2e-5 or np.max(np.abs(row_sums - 1.0), initial=0.0) > 2e-6:
        raise ValidationError("ort_numeric", f"max_abs={np.max(error)} row_sum={np.max(np.abs(row_sums - 1.0))}")
    return {
        "records": int(features.shape[0]),
        "max_absolute_error": float(np.max(error, initial=0.0)),
        "mean_absolute_error": float(np.mean(error)),
        "max_probability_sum_error": float(np.max(np.abs(row_sums - 1.0), initial=0.0)),
        "result": "PASS",
    }


def train_logistic(dataset: DatasetData, seed: int, metadata: dict[str, str]) -> TrainedCandidate:
    started = time.monotonic_ns()
    transformed = log_features(dataset.features)
    train_indices = dataset.indices("train")
    early_indices = dataset.indices("early_stop")
    calibration_indices = dataset.indices("calibration")
    blind_indices = dataset.indices("blind_test")
    scaler = weighted_standardization(transformed[train_indices], dataset.weights[train_indices])
    scaled = scaler.transform(transformed)
    candidates: list[tuple[tuple[float, float, float], LogisticRegression, dict[str, float]]] = []
    for c_value in (0.01, 0.1, 1.0, 10.0):
        model = LogisticRegression(
            C=c_value,
            penalty="l2",
            solver="lbfgs",
            max_iter=1000,
            tol=1e-6,
            random_state=seed,
        )
        model.fit(
            scaled[train_indices],
            dataset.labels[train_indices],
            sample_weight=dataset.weights[train_indices],
        )
        early_probability = model.predict_proba(scaled[early_indices])[:, 1]
        ap = float(average_precision_score(dataset.labels[early_indices], early_probability))
        loss = float(
            log_loss(dataset.labels[early_indices], early_probability, sample_weight=dataset.weights[early_indices])
        )
        candidates.append(
            (
                (-ap, loss, c_value),
                model,
                {"C": c_value, "early_stop_average_precision": ap, "early_stop_log_loss": loss},
            )
        )
    candidates.sort(key=lambda item: item[0])
    selected_model = candidates[0][1]
    selected_hyperparameters = candidates[0][2]
    raw_calibration = np.asarray(selected_model.decision_function(scaled[calibration_indices]), dtype=np.float64)
    calibration = fit_platt(raw_calibration, dataset.labels[calibration_indices], dataset.weights[calibration_indices])
    calibration_probability = calibration.probability(raw_calibration)
    thresholds = select_thresholds(calibration_probability, dataset.labels[calibration_indices])
    blind_raw = np.asarray(selected_model.decision_function(scaled[blind_indices]), dtype=np.float64)
    blind_probability = calibration.probability(blind_raw)
    metrics = quality_metrics(
        blind_probability, dataset.labels[blind_indices], thresholds, _families(dataset, blind_indices)
    )

    sgd = SGDClassifier(loss="log_loss", random_state=seed, shuffle=False, alpha=0.0001, learning_rate="optimal")
    classes = np.asarray([0, 1], dtype=np.int64)
    for start in range(0, train_indices.size, 256):
        batch = train_indices[start : start + 256]
        sgd.partial_fit(scaled[batch], dataset.labels[batch], classes=classes, sample_weight=dataset.weights[batch])
    sgd_probability = sgd.predict_proba(scaled[early_indices])[:, 1]
    partial_fit_evidence = {
        "records": int(train_indices.size),
        "batch_size": 256,
        "finite": bool(np.all(np.isfinite(sgd_probability))),
        "average_precision": float(average_precision_score(dataset.labels[early_indices], sgd_probability)),
        "production_artifact": False,
    }
    coefficient = np.asarray(selected_model.coef_[0], dtype=np.float32)
    intercept = float(np.asarray(selected_model.intercept_, dtype=np.float64).reshape(-1)[0])
    payload = export_logistic(
        coefficient,
        intercept,
        scaler.mean,
        scaler.scale,
        calibration,
        _metadata(metadata, "lr-window-binary/v1", seed),
    )
    onnx_evidence = inspect_model(payload)
    onnx_evidence["numeric"] = _ort_numeric(payload, dataset.features[blind_indices], blind_probability)
    training = {
        "duration_ms": (time.monotonic_ns() - started) / 1_000_000,
        "grid": [item[2] for item in candidates],
        "selected": selected_hyperparameters,
        "coefficient": coefficient.astype(float).tolist(),
        "intercept": intercept,
        "scaler": scaler.as_dict(),
        "partial_fit": partial_fit_evidence,
        "model_digest": sha256_bytes(payload),
    }
    return TrainedCandidate(
        candidate_id="lr-window-binary/v1",
        seed=seed,
        model=selected_model,
        scaler=scaler,
        calibration=calibration,
        thresholds=thresholds,
        hyperparameters=selected_hyperparameters,
        training_evidence=training,
        metrics=metrics,
        onnx_payload=payload,
        onnx_evidence=onnx_evidence,
        blind_indices=blind_indices,
        blind_raw=blind_raw,
        blind_probability=blind_probability,
    )


def train_xgboost(dataset: DatasetData, seed: int, metadata: dict[str, str]) -> TrainedCandidate:
    started = time.monotonic_ns()
    transformed = log_features(dataset.features)
    train_indices = dataset.indices("train")
    early_indices = dataset.indices("early_stop")
    calibration_indices = dataset.indices("calibration")
    blind_indices = dataset.indices("blind_test")
    candidates: list[tuple[tuple[float, float, int, float, float, float], xgb.XGBRegressor, dict[str, Any]]] = []
    for max_depth, eta, min_child_weight, reg_lambda in itertools.product((3, 6), (0.03, 0.1), (1.0, 5.0), (1.0, 10.0)):
        model = xgb.XGBRegressor(
            objective="binary:logitraw",
            n_estimators=512,
            early_stopping_rounds=32,
            max_depth=max_depth,
            learning_rate=eta,
            min_child_weight=min_child_weight,
            reg_lambda=reg_lambda,
            subsample=1.0,
            colsample_bytree=1.0,
            max_bin=256,
            tree_method="hist",
            n_jobs=1,
            random_state=seed,
            eval_metric="logloss",
            verbosity=0,
        )
        model.fit(
            transformed[train_indices],
            dataset.labels[train_indices].astype(np.float32),
            sample_weight=dataset.weights[train_indices],
            eval_set=[(transformed[early_indices], dataset.labels[early_indices].astype(np.float32))],
            sample_weight_eval_set=[dataset.weights[early_indices]],
            verbose=False,
        )
        early_raw = np.asarray(model.predict(transformed[early_indices]), dtype=np.float64)
        early_probability = _sigmoid(early_raw)
        ap = float(average_precision_score(dataset.labels[early_indices], early_probability))
        loss = float(
            log_loss(dataset.labels[early_indices], early_probability, sample_weight=dataset.weights[early_indices])
        )
        evidence = {
            "max_depth": max_depth,
            "eta": eta,
            "min_child_weight": min_child_weight,
            "reg_lambda": reg_lambda,
            "best_iteration": int(model.best_iteration),
            "early_stop_average_precision": ap,
            "early_stop_log_loss": loss,
        }
        candidates.append(((-ap, loss, max_depth, eta, min_child_weight, reg_lambda), model, evidence))
    candidates.sort(key=lambda item: item[0])
    selected_model = candidates[0][1]
    selected_hyperparameters = candidates[0][2]
    raw_calibration = np.asarray(selected_model.predict(transformed[calibration_indices]), dtype=np.float64)
    calibration = fit_platt(raw_calibration, dataset.labels[calibration_indices], dataset.weights[calibration_indices])
    calibration_probability = calibration.probability(raw_calibration)
    thresholds = select_thresholds(calibration_probability, dataset.labels[calibration_indices])
    blind_raw = np.asarray(selected_model.predict(transformed[blind_indices]), dtype=np.float64)
    blind_probability = calibration.probability(blind_raw)
    metrics = quality_metrics(
        blind_probability, dataset.labels[blind_indices], thresholds, _families(dataset, blind_indices)
    )
    payload = export_xgboost(selected_model, calibration, _metadata(metadata, "xgb-window-binary/v1", seed))
    onnx_evidence = inspect_model(payload)
    onnx_evidence["numeric"] = _ort_numeric(payload, dataset.features[blind_indices], blind_probability)
    training = {
        "duration_ms": (time.monotonic_ns() - started) / 1_000_000,
        "grid_size": len(candidates),
        "grid": [item[2] for item in candidates],
        "selected": selected_hyperparameters,
        "objective": "binary:logitraw",
        "tree_method": "hist",
        "max_bin": 256,
        "subsample": 1.0,
        "colsample_bytree": 1.0,
        "n_jobs": 1,
        "model_digest": sha256_bytes(payload),
    }
    return TrainedCandidate(
        candidate_id="xgb-window-binary/v1",
        seed=seed,
        model=selected_model,
        scaler=None,
        calibration=calibration,
        thresholds=thresholds,
        hyperparameters=selected_hyperparameters,
        training_evidence=training,
        metrics=metrics,
        onnx_payload=payload,
        onnx_evidence=onnx_evidence,
        blind_indices=blind_indices,
        blind_raw=blind_raw,
        blind_probability=blind_probability,
    )


def _initialize_autoencoder(seed: int) -> dict[str, Float32Array]:
    generator = np.random.Generator(np.random.PCG64(seed))

    def weight(fan_in: int, fan_out: int) -> Float32Array:
        return (generator.standard_normal((fan_in, fan_out)) * math.sqrt(2.0 / fan_in)).astype(np.float32)

    return {
        "w1": weight(6, 8),
        "b1": np.zeros(8, dtype=np.float32),
        "w2": weight(8, 3),
        "b2": np.zeros(3, dtype=np.float32),
        "w3": weight(3, 8),
        "b3": np.zeros(8, dtype=np.float32),
        "w4": weight(8, 6),
        "b4": np.zeros(6, dtype=np.float32),
    }


def _autoencoder_forward(
    parameters: dict[str, Float32Array], values: Float32Array
) -> tuple[Float32Array, tuple[Float32Array, ...]]:
    z1 = values @ parameters["w1"] + parameters["b1"]
    a1 = np.maximum(z1, 0.0)
    z2 = a1 @ parameters["w2"] + parameters["b2"]
    a2 = np.maximum(z2, 0.0)
    z3 = a2 @ parameters["w3"] + parameters["b3"]
    a3 = np.maximum(z3, 0.0)
    output = a3 @ parameters["w4"] + parameters["b4"]
    return output.astype(np.float32, copy=False), (z1, a1, z2, a2, z3, a3)


def smooth_l1_residual(values: Float32Array, reconstruction: Float32Array) -> Float32Array:
    difference = values - reconstruction
    absolute = np.abs(difference)
    return np.where(absolute < 1.0, 0.5 * difference**2, absolute - 0.5).astype(np.float32, copy=False)


def _train_autoencoder_grid(
    train_values: Float32Array,
    early_values: Float32Array,
    *,
    seed: int,
    learning_rate: float,
    weight_decay: float,
) -> AutoencoderState:
    parameters = _initialize_autoencoder(seed)
    first_moment = {name: np.zeros_like(value) for name, value in parameters.items()}
    second_moment = {name: np.zeros_like(value) for name, value in parameters.items()}
    best = {name: value.copy() for name, value in parameters.items()}
    best_loss = math.inf
    patience = 0
    step = 0
    completed_epochs = 0
    for epoch in range(1, 201):
        completed_epochs = epoch
        for start in range(0, train_values.shape[0], 1024):
            batch = train_values[start : start + 1024]
            reconstruction, cache = _autoencoder_forward(parameters, batch)
            z1, a1, z2, a2, z3, a3 = cache
            difference = reconstruction - batch
            absolute = np.abs(difference)
            output_gradient = np.where(absolute < 1.0, difference, np.sign(difference)) / difference.size
            gradients: dict[str, Float32Array] = {}
            gradients["w4"] = (a3.T @ output_gradient).astype(np.float32)
            gradients["b4"] = output_gradient.sum(axis=0).astype(np.float32)
            dz3 = (output_gradient @ parameters["w4"].T) * (z3 > 0)
            gradients["w3"] = (a2.T @ dz3).astype(np.float32)
            gradients["b3"] = dz3.sum(axis=0).astype(np.float32)
            dz2 = (dz3 @ parameters["w3"].T) * (z2 > 0)
            gradients["w2"] = (a1.T @ dz2).astype(np.float32)
            gradients["b2"] = dz2.sum(axis=0).astype(np.float32)
            dz1 = (dz2 @ parameters["w2"].T) * (z1 > 0)
            gradients["w1"] = (batch.T @ dz1).astype(np.float32)
            gradients["b1"] = dz1.sum(axis=0).astype(np.float32)
            step += 1
            correction1 = 1.0 - 0.9**step
            correction2 = 1.0 - 0.999**step
            for name, parameter in parameters.items():
                gradient = gradients[name]
                first_moment[name] = (np.float32(0.9) * first_moment[name] + np.float32(0.1) * gradient).astype(
                    np.float32
                )
                second_moment[name] = (
                    np.float32(0.999) * second_moment[name] + np.float32(0.001) * gradient**2
                ).astype(np.float32)
                update = (first_moment[name] / correction1) / (np.sqrt(second_moment[name] / correction2) + 1e-8)
                if name.startswith("w"):
                    update = update + weight_decay * parameter
                parameter -= np.float32(learning_rate) * update.astype(np.float32)
        early_reconstruction, _ = _autoencoder_forward(parameters, early_values)
        early_loss = float(smooth_l1_residual(early_values, early_reconstruction).mean())
        if early_loss < best_loss - 1e-9:
            best_loss = early_loss
            best = {name: value.copy() for name, value in parameters.items()}
            patience = 0
        else:
            patience += 1
        if patience >= 20:
            break
    return AutoencoderState(
        parameters=best,
        scaler=Standardization(np.zeros(6, dtype=np.float32), np.ones(6, dtype=np.float32)),
        epochs=completed_epochs,
        early_stop_loss=best_loss,
    )


def _autoencoder_raw(state: AutoencoderState, scaled_values: Float32Array) -> tuple[Float64Array, Float32Array]:
    reconstruction, _ = _autoencoder_forward(state.parameters, scaled_values)
    residuals = smooth_l1_residual(scaled_values, reconstruction)
    return residuals.mean(axis=1).astype(np.float64), residuals


def train_autoencoder(dataset: DatasetData, seed: int, metadata: dict[str, str]) -> TrainedCandidate:
    started = time.monotonic_ns()
    transformed = log_features(dataset.features)
    train_indices = dataset.indices("train")
    early_indices = dataset.indices("early_stop")
    calibration_indices = dataset.indices("calibration")
    blind_indices = dataset.indices("blind_test")
    benign_train = train_indices[dataset.labels[train_indices] == 0]
    benign_early = early_indices[dataset.labels[early_indices] == 0]
    scaler = weighted_standardization(transformed[benign_train], dataset.weights[benign_train])
    scaled = scaler.transform(transformed)
    candidates: list[tuple[tuple[float, float, float], AutoencoderState, dict[str, Any]]] = []
    for learning_rate, weight_decay in itertools.product((0.0003, 0.001), (0.0, 0.0001)):
        state = _train_autoencoder_grid(
            scaled[benign_train],
            scaled[benign_early],
            seed=seed,
            learning_rate=learning_rate,
            weight_decay=weight_decay,
        )
        state = AutoencoderState(
            parameters=state.parameters,
            scaler=scaler,
            epochs=state.epochs,
            early_stop_loss=state.early_stop_loss,
        )
        evidence = {
            "learning_rate": learning_rate,
            "weight_decay": weight_decay,
            "epochs": state.epochs,
            "early_stop_loss": state.early_stop_loss,
        }
        candidates.append(((state.early_stop_loss, learning_rate, weight_decay), state, evidence))
    candidates.sort(key=lambda item: item[0])
    selected_state = candidates[0][1]
    selected_hyperparameters = candidates[0][2]
    raw_calibration, _ = _autoencoder_raw(selected_state, scaled[calibration_indices])
    calibration = fit_platt(raw_calibration, dataset.labels[calibration_indices], dataset.weights[calibration_indices])
    calibration_probability = calibration.probability(raw_calibration)
    thresholds = select_thresholds(calibration_probability, dataset.labels[calibration_indices])
    blind_raw, _ = _autoencoder_raw(selected_state, scaled[blind_indices])
    blind_probability = calibration.probability(blind_raw)
    metrics = quality_metrics(
        blind_probability, dataset.labels[blind_indices], thresholds, _families(dataset, blind_indices)
    )
    payload = export_autoencoder(
        selected_state.parameters,
        scaler.mean,
        scaler.scale,
        calibration,
        _metadata(metadata, "ae-window-benign/v1", seed),
    )
    onnx_evidence = inspect_model(payload)
    onnx_evidence["numeric"] = _ort_numeric(payload, dataset.features[blind_indices], blind_probability)
    parameter_digest = sha256_bytes(
        b"".join(
            selected_state.parameters[name].astype("<f4", copy=False).tobytes(order="C")
            for name in sorted(selected_state.parameters)
        )
    )
    training = {
        "duration_ms": (time.monotonic_ns() - started) / 1_000_000,
        "architecture": [6, 8, 3, 8, 6],
        "activation": "relu-hidden-linear-output",
        "loss": "SmoothL1-beta-1",
        "optimizer": "AdamW",
        "batch_size": 1024,
        "grid_size": len(candidates),
        "grid": [item[2] for item in candidates],
        "selected": selected_hyperparameters,
        "scaler": scaler.as_dict(),
        "parameter_digest": parameter_digest,
        "model_digest": sha256_bytes(payload),
    }
    return TrainedCandidate(
        candidate_id="ae-window-benign/v1",
        seed=seed,
        model=selected_state,
        scaler=scaler,
        calibration=calibration,
        thresholds=thresholds,
        hyperparameters=selected_hyperparameters,
        training_evidence=training,
        metrics=metrics,
        onnx_payload=payload,
        onnx_evidence=onnx_evidence,
        blind_indices=blind_indices,
        blind_raw=blind_raw,
        blind_probability=blind_probability,
    )


def predict_raw(candidate: TrainedCandidate, features: npt.NDArray[np.uint64]) -> Float64Array:
    transformed = log_features(features)
    if candidate.candidate_id == "lr-window-binary/v1":
        if candidate.scaler is None or not isinstance(candidate.model, LogisticRegression):
            raise ValidationError("candidate_model", candidate.candidate_id)
        return np.asarray(candidate.model.decision_function(candidate.scaler.transform(transformed)), dtype=np.float64)
    if candidate.candidate_id == "xgb-window-binary/v1":
        if not isinstance(candidate.model, xgb.XGBRegressor):
            raise ValidationError("candidate_model", candidate.candidate_id)
        return np.asarray(candidate.model.predict(transformed), dtype=np.float64)
    if candidate.candidate_id == "ae-window-benign/v1":
        if candidate.scaler is None or not isinstance(candidate.model, AutoencoderState):
            raise ValidationError("candidate_model", candidate.candidate_id)
        raw, _ = _autoencoder_raw(candidate.model, candidate.scaler.transform(transformed))
        return raw
    raise ValidationError("candidate_id", candidate.candidate_id)


def autoencoder_residuals(candidate: TrainedCandidate, features: npt.NDArray[np.uint64]) -> Float32Array:
    if (
        candidate.candidate_id != "ae-window-benign/v1"
        or candidate.scaler is None
        or not isinstance(candidate.model, AutoencoderState)
    ):
        raise ValidationError("candidate_model", candidate.candidate_id)
    transformed = candidate.scaler.transform(log_features(features))
    _, residuals = _autoencoder_raw(candidate.model, transformed)
    return residuals
