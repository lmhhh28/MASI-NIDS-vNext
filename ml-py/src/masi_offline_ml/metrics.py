"""ADR-0019 calibration, threshold selection, and quality metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


@dataclass(frozen=True)
class PlattCalibration:
    slope: float
    intercept: float

    def probability(self, raw: npt.ArrayLike) -> FloatArray:
        values = np.asarray(raw, dtype=np.float64)
        logits = np.clip(self.slope * values + self.intercept, -80.0, 80.0)
        return (1.0 / (1.0 + np.exp(-logits))).astype(np.float64, copy=False)

    def as_dict(self) -> dict[str, float]:
        return {"slope": self.slope, "intercept": self.intercept}


@dataclass(frozen=True)
class Thresholds:
    alert: float
    confidence: float

    def as_dict(self) -> dict[str, float]:
        return {"alert": self.alert, "confidence": self.confidence}


def fit_platt(raw: npt.ArrayLike, labels: npt.ArrayLike, weights: npt.ArrayLike) -> PlattCalibration:
    raw_values = np.asarray(raw, dtype=np.float64).reshape((-1, 1))
    label_values = np.asarray(labels, dtype=np.int64)
    weight_values = np.asarray(weights, dtype=np.float64)
    model = LogisticRegression(
        C=1_000_000.0,
        solver="lbfgs",
        max_iter=1000,
        tol=1e-9,
        fit_intercept=True,
        random_state=0,
    )
    model.fit(raw_values, label_values, sample_weight=weight_values)
    slope = float(model.coef_[0, 0])
    intercept = float(np.asarray(model.intercept_, dtype=np.float64).reshape(-1)[0])
    if not np.isfinite(slope) or not np.isfinite(intercept) or slope <= 0.0:
        raise ValueError("platt_slope_non_positive")
    return PlattCalibration(slope=slope, intercept=intercept)


def decisions(probability: npt.ArrayLike, thresholds: Thresholds) -> IntArray:
    """Return 0=benign, 1=alert, 2=abstain using masi-window-adapter-v1."""

    attack = np.asarray(probability, dtype=np.float64)
    maximum = np.maximum(attack, 1.0 - attack)
    result = np.zeros(attack.shape, dtype=np.int64)
    result[attack >= thresholds.alert] = 1
    result[maximum < thresholds.confidence] = 2
    return result


def _threshold_constraints(
    probability: FloatArray, labels: IntArray, thresholds: Thresholds
) -> tuple[bool, tuple[float, ...]]:
    predicted = decisions(probability, thresholds)
    benign = labels == 0
    attack = labels == 1
    false_positive_rate = float(np.count_nonzero((predicted == 1) & benign) / max(1, np.count_nonzero(benign)))
    recall = float(np.count_nonzero((predicted == 1) & attack) / max(1, np.count_nonzero(attack)))
    coverage = float(np.count_nonzero(predicted != 2) / max(1, labels.size))
    benign_abstain = float(np.count_nonzero((predicted == 2) & benign) / max(1, np.count_nonzero(benign)))
    feasible = false_positive_rate <= 0.01 and recall >= 0.90 and coverage >= 0.95 and benign_abstain <= 0.05
    rank = (-recall, false_positive_rate, -coverage, thresholds.alert, thresholds.confidence)
    return feasible, rank


def select_thresholds(probability: npt.ArrayLike, labels: npt.ArrayLike) -> Thresholds:
    probabilities = np.asarray(probability, dtype=np.float64)
    label_values = np.asarray(labels, dtype=np.int64)
    finite = probabilities[np.isfinite(probabilities)]
    if finite.size != probabilities.size or finite.size == 0:
        raise ValueError("threshold_non_finite")
    candidates = np.unique(np.concatenate((finite.astype(np.float32).astype(np.float64), np.asarray([0.5, 1.0]))))
    candidates = candidates[(candidates >= 0.5) & (candidates <= 1.0)]
    feasible: list[tuple[tuple[float, ...], Thresholds]] = []
    for confidence in candidates:
        for alert in candidates[candidates >= confidence]:
            thresholds = Thresholds(alert=float(alert), confidence=float(confidence))
            accepted, rank = _threshold_constraints(probabilities, label_values, thresholds)
            if accepted:
                feasible.append((rank, thresholds))
    if not feasible:
        raise ValueError("no_feasible_threshold")
    feasible.sort(key=lambda item: item[0])
    return feasible[0][1]


def _binary_f1(predicted: IntArray, labels: IntArray, positive: int) -> float:
    positive_prediction = predicted == positive
    positive_truth = labels == positive
    true_positive = int(np.count_nonzero(positive_prediction & positive_truth))
    false_positive = int(np.count_nonzero(positive_prediction & ~positive_truth))
    false_negative = int(np.count_nonzero(~positive_prediction & positive_truth))
    denominator = 2 * true_positive + false_positive + false_negative
    return 0.0 if denominator == 0 else 2.0 * true_positive / denominator


def equal_frequency_ece(probability: npt.ArrayLike, labels: npt.ArrayLike, bins: int = 15) -> float:
    probabilities = np.asarray(probability, dtype=np.float64)
    label_values = np.asarray(labels, dtype=np.float64)
    order = np.argsort(probabilities, kind="stable")
    total = probabilities.size
    error = 0.0
    for indices in np.array_split(order, min(bins, total)):
        if indices.size == 0:
            continue
        error += indices.size / total * abs(float(probabilities[indices].mean() - label_values[indices].mean()))
    return error


def quality_metrics(
    probability: npt.ArrayLike,
    labels: npt.ArrayLike,
    thresholds: Thresholds,
    families: npt.ArrayLike,
) -> dict[str, Any]:
    probabilities = np.asarray(probability, dtype=np.float64)
    label_values = np.asarray(labels, dtype=np.int64)
    family_values = np.asarray(families, dtype=np.str_)
    predicted = decisions(probabilities, thresholds)
    benign = label_values == 0
    attack = label_values == 1
    alerts = predicted == 1
    true_positive = int(np.count_nonzero(alerts & attack))
    false_positive = int(np.count_nonzero(alerts & benign))
    false_negative = int(np.count_nonzero(~alerts & attack))
    true_negative = int(np.count_nonzero((predicted == 0) & benign))
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    fpr = false_positive / max(1, false_positive + true_negative + int(np.count_nonzero((predicted == 2) & benign)))
    coverage = float(np.count_nonzero(predicted != 2) / max(1, predicted.size))
    benign_abstain = float(np.count_nonzero((predicted == 2) & benign) / max(1, np.count_nonzero(benign)))
    family_recall: dict[str, dict[str, float | int | str]] = {}
    for family in sorted(set(str(value) for value in family_values[attack])):
        selected = attack & (family_values == family)
        count = int(np.count_nonzero(selected))
        if count < 100:
            family_recall[family] = {"samples": count, "status": "not_applicable", "recall": 0.0}
        else:
            family_recall[family] = {
                "samples": count,
                "status": "measurable",
                "recall": float(np.count_nonzero(alerts & selected) / count),
            }
    metrics: dict[str, Any] = {
        "average_precision": float(average_precision_score(label_values, probabilities)),
        "pr_auc_definition": "sklearn-average_precision-non-interpolated",
        "precision": float(precision),
        "recall": float(recall),
        "macro_f1": float((_binary_f1(predicted, label_values, 0) + _binary_f1(predicted, label_values, 1)) / 2.0),
        "false_positive_rate": float(fpr),
        "ece_15_equal_frequency": float(equal_frequency_ece(probabilities, label_values)),
        "brier": float(np.mean((probabilities - label_values) ** 2)),
        "coverage": coverage,
        "benign_abstain_rate": benign_abstain,
        "confusion": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
            "abstain": int(np.count_nonzero(predicted == 2)),
        },
        "family_recall": family_recall,
    }
    metrics["passes_thresholds"] = quality_passes(metrics)
    return metrics


def quality_passes(metrics: dict[str, Any]) -> bool:
    if not (
        float(metrics["average_precision"]) >= 0.95
        and float(metrics["precision"]) >= 0.90
        and float(metrics["recall"]) >= 0.90
        and float(metrics["macro_f1"]) >= 0.90
        and float(metrics["false_positive_rate"]) <= 0.01
        and float(metrics["ece_15_equal_frequency"]) <= 0.05
        and float(metrics["brier"]) <= 0.10
        and float(metrics["coverage"]) >= 0.95
        and float(metrics["benign_abstain_rate"]) <= 0.05
    ):
        return False
    family_recall = metrics["family_recall"]
    if not isinstance(family_recall, dict):
        return False
    return all(
        value.get("status") != "measurable" or float(value.get("recall", 0.0)) >= 0.80
        for value in family_recall.values()
        if isinstance(value, dict)
    )
