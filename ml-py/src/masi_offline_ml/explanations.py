"""Generate deterministic, non-causal ADR-0019 explanation evidence."""

from __future__ import annotations

import importlib.metadata
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import shap
import xgboost as xgb
from jsonschema import Draft202012Validator, FormatChecker
from sklearn.linear_model import LogisticRegression

from .canonical import ValidationError, canonical_json_bytes, load_json, sha256_bytes, sha256_file
from .contracts import FEATURE_ORDER, validate_explanation
from .dataset import DatasetData
from .models import (
    AutoencoderState,
    TrainedCandidate,
    autoencoder_residuals,
    log_features,
)

Float64Array = npt.NDArray[np.float64]
EMPTY_DIGEST = sha256_bytes(b"")


@dataclass(frozen=True)
class ExplanationMaterial:
    candidate: TrainedCandidate
    indices: npt.NDArray[np.int64]
    transformed: Float64Array
    contributions: Float64Array
    base_values: Float64Array
    raw_values: Float64Array
    probabilities: Float64Array
    background_digest: str
    background_count: int
    method_id: str
    domain: str
    contribution_semantics: str
    feature_perturbation: str
    model_output: str
    tool_name: str
    tool_version: str
    limitations: tuple[str, ...]


def _selected_indices(dataset: DatasetData, per_group: int = 32) -> npt.NDArray[np.int64]:
    groups: dict[tuple[str, str], list[int]] = {}
    for index in dataset.indices("blind_test"):
        record = dataset.records[int(index)]
        key = (str(record["source_profile"]), str(record["scenario_id"]))
        groups.setdefault(key, []).append(int(index))
    selected: list[int] = []
    for key in sorted(groups):
        values = groups[key]
        if len(values) <= per_group:
            selected.extend(values)
        else:
            positions = np.linspace(0, len(values) - 1, num=per_group, dtype=np.int64)
            selected.extend(values[int(position)] for position in positions)
    selected.sort(key=lambda index: str(dataset.records[index]["sort_key"]).encode("utf-8"))
    return np.asarray(selected, dtype=np.int64)


def _background_indices(dataset: DatasetData, maximum: int = 256) -> npt.NDArray[np.int64]:
    values = dataset.indices("train")
    if values.size <= maximum:
        return values
    positions = np.linspace(0, values.size - 1, num=maximum, dtype=np.int64)
    return values[positions]


def _lr_material(
    candidate: TrainedCandidate, dataset: DatasetData, indices: npt.NDArray[np.int64]
) -> ExplanationMaterial:
    if candidate.scaler is None or not isinstance(candidate.model, LogisticRegression):
        raise ValidationError("explanation_model", candidate.candidate_id)
    transformed = candidate.scaler.transform(log_features(dataset.features[indices])).astype(np.float64)
    coefficient = np.asarray(candidate.model.coef_[0], dtype=np.float64)
    contributions = transformed * coefficient
    intercept = float(np.asarray(candidate.model.intercept_, dtype=np.float64).reshape(-1)[0])
    base_values = np.full(indices.size, intercept, dtype=np.float64)
    raw = np.asarray(candidate.model.decision_function(transformed.astype(np.float32)), dtype=np.float64)
    return ExplanationMaterial(
        candidate=candidate,
        indices=indices,
        transformed=transformed,
        contributions=contributions,
        base_values=base_values,
        raw_values=raw,
        probabilities=candidate.calibration.probability(raw),
        background_digest=EMPTY_DIGEST,
        background_count=0,
        method_id="lr-raw-margin/v1",
        domain="raw-margin",
        contribution_semantics="coefficient-times-scaled-log-feature",
        feature_perturbation="not_applicable",
        model_output="raw",
        tool_name="scikit-learn",
        tool_version=importlib.metadata.version("scikit-learn"),
        limitations=("Raw-margin contribution is associative attribution, not a causal claim.",),
    )


def _xgb_material(
    candidate: TrainedCandidate, dataset: DatasetData, indices: npt.NDArray[np.int64]
) -> ExplanationMaterial:
    if not isinstance(candidate.model, xgb.XGBRegressor):
        raise ValidationError("explanation_model", candidate.candidate_id)
    transformed_all = log_features(dataset.features)
    transformed = transformed_all[indices].astype(np.float64)
    background_indices = _background_indices(dataset)
    background = transformed_all[background_indices].astype(np.float64)
    background_digest = sha256_bytes(background.astype("<f4", copy=False).tobytes(order="C"))
    masker = cast(Any, shap).maskers.Independent(background, max_samples=256)
    explainer = shap.TreeExplainer(
        candidate.model.get_booster(),
        data=masker,
        model_output="raw",
        feature_perturbation="interventional",
    )
    shap_values = np.asarray(explainer.shap_values(transformed, check_additivity=True), dtype=np.float64)
    if shap_values.shape != (indices.size, 6):
        raise ValidationError("treeshap_shape", str(shap_values.shape))
    expected = np.asarray(explainer.expected_value, dtype=np.float64)
    if expected.size != 1:
        raise ValidationError("treeshap_base", str(expected.shape))
    base_values = np.full(indices.size, float(expected.reshape(-1)[0]), dtype=np.float64)
    raw = np.asarray(candidate.model.predict(transformed.astype(np.float32)), dtype=np.float64)
    return ExplanationMaterial(
        candidate=candidate,
        indices=indices,
        transformed=transformed,
        contributions=shap_values,
        base_values=base_values,
        raw_values=raw,
        probabilities=candidate.calibration.probability(raw),
        background_digest=background_digest,
        background_count=int(background.shape[0]),
        method_id="xgb-treeshap-interventional-raw/v1",
        domain="raw-margin",
        contribution_semantics="interventional-treeshap-phi",
        feature_perturbation="interventional",
        model_output="raw",
        tool_name="shap",
        tool_version=importlib.metadata.version("shap"),
        limitations=("TreeSHAP additivity is in raw-margin space; calibrated probability is not the additive domain.",),
    )


def _ae_material(
    candidate: TrainedCandidate, dataset: DatasetData, indices: npt.NDArray[np.int64]
) -> ExplanationMaterial:
    if candidate.scaler is None or not isinstance(candidate.model, AutoencoderState):
        raise ValidationError("explanation_model", candidate.candidate_id)
    transformed = candidate.scaler.transform(log_features(dataset.features[indices])).astype(np.float64)
    residuals = autoencoder_residuals(candidate, dataset.features[indices]).astype(np.float64)
    raw = residuals.mean(axis=1)
    return ExplanationMaterial(
        candidate=candidate,
        indices=indices,
        transformed=transformed,
        contributions=residuals,
        base_values=np.zeros(indices.size, dtype=np.float64),
        raw_values=raw,
        probabilities=candidate.calibration.probability(raw),
        background_digest=EMPTY_DIGEST,
        background_count=0,
        method_id="ae-scaled-log-smoothl1-residual/v1",
        domain="scaled-log-residual",
        contribution_semantics="per-feature-smoothl1-residual",
        feature_perturbation="not_applicable",
        model_output="not_applicable",
        tool_name="masi-numpy-autoencoder",
        tool_version="1.0.0",
        limitations=(
            "Residual is reconstruction deviation in scaled-log space, not an attack field or causal explanation.",
        ),
    )


def _material(candidate: TrainedCandidate, dataset: DatasetData, indices: npt.NDArray[np.int64]) -> ExplanationMaterial:
    if candidate.candidate_id == "lr-window-binary/v1":
        return _lr_material(candidate, dataset, indices)
    if candidate.candidate_id == "xgb-window-binary/v1":
        return _xgb_material(candidate, dataset, indices)
    if candidate.candidate_id == "ae-window-benign/v1":
        return _ae_material(candidate, dataset, indices)
    raise ValidationError("candidate_id", candidate.candidate_id)


def _rank_vector(importance: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    ordering = sorted(range(6), key=lambda index: (-float(importance[index]), index))
    ranks = np.empty(6, dtype=np.float64)
    for rank, index in enumerate(ordering, 1):
        ranks[index] = rank
    return ranks


def _spearman(first: npt.NDArray[np.float64], second: npt.NDArray[np.float64]) -> float:
    first_centered = first - first.mean()
    second_centered = second - second.mean()
    denominator = math.sqrt(float(np.sum(first_centered**2) * np.sum(second_centered**2)))
    return 0.0 if denominator == 0.0 else float(np.sum(first_centered * second_centered) / denominator)


def _stability(materials: list[ExplanationMaterial]) -> tuple[float, str, str]:
    if [material.candidate.seed for material in materials] != [17, 29, 43]:
        raise ValidationError("explanation_seed_set", str([material.candidate.seed for material in materials]))
    importance = [np.mean(np.abs(material.contributions), axis=0) for material in materials]
    ranks = [_rank_vector(values) for values in importance]
    correlations = [_spearman(ranks[left], ranks[right]) for left, right in ((0, 1), (0, 2), (1, 2))]
    median = float(np.median(np.asarray(correlations, dtype=np.float64)))
    status = "stable" if median >= 0.8 else "unstable"
    digest = sha256_bytes(
        canonical_json_bytes(
            {
                "seeds": [17, 29, 43],
                "global_importance": [values.astype(float).tolist() for values in importance],
                "ranks": [values.astype(int).tolist() for values in ranks],
                "correlations": correlations,
            }
        )
    )
    return median, status, digest


def _revision(candidate: TrainedCandidate) -> str:
    model_digest = str(candidate.onnx_evidence["model_digest"])
    return sha256_bytes(
        canonical_json_bytes(
            {"candidate_id": candidate.candidate_id, "seed": candidate.seed, "model_digest": model_digest}
        )
    ).split(":", 1)[1][:40]


def _bundle_digest(candidate: TrainedCandidate) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "model_digest": candidate.onnx_evidence["model_digest"],
                "calibration": candidate.calibration.as_dict(),
                "thresholds": candidate.thresholds.as_dict(),
                "hyperparameters": candidate.hyperparameters,
            }
        )
    )


def _tool_digest(repo: Path, material: ExplanationMaterial) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "tool": material.tool_name,
                "version": material.tool_version,
                "lock_digest": sha256_file(repo / "ml-py/uv.lock"),
            }
        )
    )


def _coverage(dataset: DatasetData, indices: npt.NDArray[np.int64]) -> tuple[dict[str, Any], dict[str, Any]]:
    all_blind = dataset.indices("blind_test")
    groups: dict[tuple[str, str], dict[str, int]] = {}
    for index in all_blind:
        record = dataset.records[int(index)]
        key = (str(record["source_profile"]), str(record["scenario_id"]))
        groups.setdefault(key, {"eligible": 0, "included": 0})["eligible"] += 1
    selected = set(int(index) for index in indices)
    for index in selected:
        record = dataset.records[index]
        key = (str(record["source_profile"]), str(record["scenario_id"]))
        groups[key]["included"] += 1
    rows = [
        {
            "source_profile": key[0],
            "scenario_id": key[1],
            "eligible": value["eligible"],
            "included": value["included"],
            "coverage": value["included"] / value["eligible"],
        }
        for key, value in sorted(groups.items())
    ]
    coverage = {
        "eligible_samples": int(all_blind.size),
        "included_samples": int(indices.size),
        "coverage": float(indices.size / all_blind.size),
        "per_source_scenario": rows,
    }
    truncation = {
        "requested": int(all_blind.size),
        "included": int(indices.size),
        "maximum": 4096,
        "per_source_scenario_maximum": 4096,
        "reason": "none" if indices.size == all_blind.size else "bounded_sample_limit",
    }
    return coverage, truncation


def _document(
    repo: Path,
    dataset: DatasetData,
    material: ExplanationMaterial,
    stability: tuple[float, str, str],
) -> dict[str, Any]:
    candidate = material.candidate
    model_digest = str(candidate.onnx_evidence["model_digest"])
    metadata = cast(dict[str, str], candidate.onnx_evidence.get("metadata", {}))
    required_metadata = (
        "feature_contract_digest",
        "label_contract_digest",
        "output_adapter_digest",
    )
    missing_metadata = [key for key in required_metadata if key not in metadata]
    if missing_metadata:
        raise ValidationError("explanation_binding_metadata", ",".join(missing_metadata))
    contribution_digest = sha256_bytes(
        canonical_json_bytes(
            {
                "sample_ids": [dataset.records[int(index)]["sample_id"] for index in material.indices],
                "base_values": material.base_values.astype(float).tolist(),
                "raw_values": material.raw_values.astype(float).tolist(),
                "contributions": material.contributions.astype(float).tolist(),
            }
        )
    )
    sample_manifest_digest = sha256_bytes(
        canonical_json_bytes(
            [
                {
                    "sample_id": dataset.records[int(index)]["sample_id"],
                    "sample_digest": dataset.records[int(index)]["feature_tensor_digest"],
                }
                for index in material.indices
            ]
        )
    )
    importance = np.mean(np.abs(material.contributions), axis=0)
    ranks = _rank_vector(importance)
    global_rows = [
        {
            "feature_id": FEATURE_ORDER[index],
            "mean_absolute_contribution": float(importance[index]),
            "rank": int(ranks[index]),
        }
        for index in range(6)
    ]
    samples: list[dict[str, Any]] = []
    for row, index in enumerate(material.indices):
        record = dataset.records[int(index)]
        raw = float(material.raw_values[row])
        base = float(material.base_values[row])
        contributions = material.contributions[row].astype(float)
        reconstructed = (
            float(contributions.mean()) if material.method_id.startswith("ae-") else base + float(contributions.sum())
        )
        sample = {
            "sample_id": record["sample_id"],
            "sample_digest": record["feature_tensor_digest"],
            "source_profile": record["source_profile"],
            "scenario_id": record["scenario_id"],
            "feature_values": record["feature_values"],
            "transformed_values": material.transformed[row].astype(float).tolist(),
            "contributions": contributions.tolist(),
            "base_value": base,
            "raw_output": raw,
            "reconstructed_raw_output": reconstructed,
            "additivity_absolute_error": abs(reconstructed - raw),
            "probability": float(material.probabilities[row]),
        }
        if material.method_id.startswith("ae-"):
            sample["residual_mean"] = reconstructed
        samples.append(sample)
    coverage, truncation = _coverage(dataset, material.indices)
    scaler_digest = (
        sha256_bytes(canonical_json_bytes(candidate.scaler.as_dict())) if candidate.scaler is not None else EMPTY_DIGEST
    )
    median, stability_status, stability_digest = stability
    background_status = "verified" if material.background_count else "not_applicable"
    document = {
        "schema_version": "model-explanation-evidence/v1",
        "evidence_id": f"explanation-{candidate.candidate_id.split('/', 1)[0]}-seed-{candidate.seed}",
        "generated_at": "2031-01-01T00:00:00Z",
        "candidate": {
            "candidate_id": candidate.candidate_id,
            "model_id": f"masi-{candidate.candidate_id.split('/', 1)[0]}",
            "revision": _revision(candidate),
            "bundle_digest": _bundle_digest(candidate),
            "model_digest": model_digest,
            "seed": candidate.seed,
        },
        "binding": {
            "dataset_id": dataset.manifest["dataset_id"],
            "dataset_revision": dataset.manifest["dataset_revision"],
            "dataset_manifest_digest": sha256_bytes(canonical_json_bytes(dataset.manifest)),
            "feature_schema_digest": metadata["feature_contract_digest"],
            "scaler_digest": scaler_digest,
            "label_taxonomy_digest": metadata["label_contract_digest"],
            "output_adapter_digest": metadata["output_adapter_digest"],
            "sample_manifest_digest": sample_manifest_digest,
        },
        "method": {
            "method_id": material.method_id,
            "domain": material.domain,
            "contribution_semantics": material.contribution_semantics,
            "feature_perturbation": material.feature_perturbation,
            "model_output": material.model_output,
            "tool": {
                "name": material.tool_name,
                "version": material.tool_version,
                "artifact_digest": _tool_digest(repo, material),
            },
            "background": {
                "status": background_status,
                "digest": material.background_digest,
                "sample_count": material.background_count,
                "maximum": 256,
                "selection": (
                    "deterministic-train-source-class-capture-family"
                    if material.background_count
                    else "not-applicable-method-does-not-use-background"
                ),
            },
            "numeric_tolerance": {"absolute": 0.00002, "relative": 0.00002, "ulp": 8},
            "method_output_digest": contribution_digest,
        },
        "feature_order": list(FEATURE_ORDER),
        "global_importance": global_rows,
        "samples": samples,
        "coverage": coverage,
        "truncation": truncation,
        "stability": {
            "seed_set": [17, 29, 43],
            "global_rank_spearman_median": median,
            "threshold": 0.8,
            "status": stability_status,
            "comparison_digest": stability_digest,
        },
        "limitations": list(material.limitations),
        "safety": {
            "causal_claim": False,
            "realtime_inference_payload": False,
            "canonical_event_identity": False,
            "effect_eligibility": False,
            "llm_generated": False,
            "executable_content": False,
        },
    }
    schema = cast(dict[str, Any], load_json(repo / "contracts/model-explanation/v1/schema.json"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        raise ValidationError("explanation_schema", f"{list(first.absolute_path)}: {first.message}")
    validate_explanation(cast(dict[str, Any], document))
    return cast(dict[str, Any], document)


def build_explanations(
    repo: Path,
    dataset: DatasetData,
    candidates: list[TrainedCandidate],
) -> dict[tuple[str, int], dict[str, Any]]:
    repo = repo.resolve(strict=True)
    selected = _selected_indices(dataset)
    grouped: dict[str, list[ExplanationMaterial]] = {}
    for candidate in sorted(candidates, key=lambda value: (value.candidate_id, value.seed)):
        grouped.setdefault(candidate.candidate_id, []).append(_material(candidate, dataset, selected))
    documents: dict[tuple[str, int], dict[str, Any]] = {}
    for candidate_id, materials in grouped.items():
        materials.sort(key=lambda value: value.candidate.seed)
        stability = _stability(materials)
        for material in materials:
            documents[(candidate_id, material.candidate.seed)] = _document(repo, dataset, material, stability)
    return documents
