"""Public Offline ML contract and golden semantic validation."""

from __future__ import annotations

import copy
import hashlib
import math
import re
import struct
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from .canonical import (
    ValidationError,
    canonical_json_bytes,
    ensure_finite,
    load_json,
    resolve_repo_file,
    sha256_bytes,
    sha256_file,
)

FEATURE_ORDER = (
    "aggregate_packets",
    "aggregate_bytes",
    "reported_nonzero_cells",
    "max_cell_packets",
    "max_cell_bytes",
    "snapshot_count",
)
SEEDS = (17, 29, 43)
_MUTATION_TOKEN = re.compile(r"([^.[\]]+)|\[([0-9]+)\]")
_DIGEST = re.compile(r"sha256:(?!0{64}$)[0-9a-f]{64}")
_SOURCE_REVISION = re.compile(r"(?:(?!0{40}$)[0-9a-f]{40}|dirty:(?!0{64}$)[0-9a-f]{64})")
_REVISION64 = re.compile(r"(?!0{64}$)[0-9a-f]{64}")


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("expected_object", where)
    return cast(dict[str, Any], value)


def _sequence(value: object, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError("expected_array", where)
    return cast(list[Any], value)


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("expected_number", where)
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError("non_finite", where)
    return result


def _integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("expected_integer", where)
    return value


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("expected_string", where)
    return value


def _validate_digest_members(value: object, where: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            location = f"{where}.{key}" if where else str(key)
            if (str(key).endswith("_digest") or key == "sha256") and (
                not isinstance(item, str) or _DIGEST.fullmatch(item) is None
            ):
                raise ValidationError("digest", location)
            _validate_digest_members(item, location)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_digest_members(item, f"{where}[{index}]")


def _validate_schema(schema: Mapping[str, Any], document: object, *, code: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(cast(Any, document)), key=lambda error: list(error.absolute_path))
    if errors:
        error = errors[0]
        location = "/" + "/".join(str(part) for part in error.absolute_path)
        raise ValidationError(code, f"{location}: {error.message}")


def split_fraction(source_revision: str, capture_family_id: str) -> float:
    payload = b"masi-split-v1\0" + source_revision.encode("utf-8") + b"\0" + capture_family_id.encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") / 2**64


def split_name(source_revision: str, capture_family_id: str) -> str:
    fraction = split_fraction(source_revision, capture_family_id)
    if fraction < 0.6:
        return "train"
    if fraction < 0.75:
        return "early_stop"
    if fraction < 0.9:
        return "calibration"
    return "blind_test"


def telemetry_cell_selector_digest(
    target_id: str,
    source_profile_digest: str,
    epoch: int,
    cell_index: int,
) -> str:
    if not target_id or "\0" in target_id:
        raise ValidationError("selector_target", "target identity is empty or contains NUL")
    if _DIGEST.fullmatch(source_profile_digest) is None:
        raise ValidationError("selector_seed", "source profile digest is malformed")
    if epoch < 1 or not 0 <= cell_index <= 255:
        raise ValidationError("selector_coordinates", f"{epoch}:{cell_index}")
    preimage = f"{target_id}\0{source_profile_digest}\0{epoch}\0{cell_index}".encode()
    return sha256_bytes(preimage)


def feature_tensor_bytes(values: Sequence[object]) -> bytes:
    if len(values) != 6:
        raise ValidationError("feature_count", str(len(values)))
    integers: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0xFFFFFFFFFFFFFFFF:
            raise ValidationError("feature_value", "features must be six uint64 values")
        integers.append(value)
    return struct.pack("<6Q", *integers)


def validate_dataset_manifest(document: Mapping[str, Any]) -> None:
    ensure_finite(document)
    _validate_digest_members(document)
    if _REVISION64.fullmatch(_string(document["dataset_revision"], "dataset_revision")) is None:
        raise ValidationError("dataset_revision", "dataset_revision")
    producer = _mapping(document["producer"], "producer")
    if _SOURCE_REVISION.fullmatch(_string(producer["source_revision"], "producer.source_revision")) is None:
        raise ValidationError("source_revision", "producer.source_revision")
    feature = _mapping(document["feature_contract"], "feature_contract")
    if tuple(_sequence(feature["order"], "feature_contract.order")) != FEATURE_ORDER:
        raise ValidationError("feature_order", "dataset feature order drift")
    extractor = _mapping(document["extractor"], "extractor")
    if (
        extractor["selector_algorithm"] != "p4-qualified-cell-selector/v1"
        or extractor["selector_digest_preimage"]
        != "target_id\\0source_profile_digest\\0epoch_decimal\\0cell_index_decimal"
    ):
        raise ValidationError("selector_identity", "dataset selector identity semantics drifted")

    files = _mapping(document["files"], "files")
    features = _mapping(files["features"], "files.features")
    index = _mapping(files["sample_index"], "files.sample_index")
    summary = _mapping(document["quality_summary"], "quality_summary")
    record_count = _integer(features["record_count"], "features.record_count")
    if record_count != _integer(index["record_count"], "sample_index.record_count") or record_count != _integer(
        summary["sample_count"], "quality_summary.sample_count"
    ):
        raise ValidationError("file_record_count", "feature/index/summary record counts differ")
    if _integer(features["bytes"], "features.bytes") != record_count * 48:
        raise ValidationError("feature_bytes", "feature bytes must equal record_count*48")
    split_counts = _mapping(summary["split_counts"], "quality_summary.split_counts")
    if (
        sum(
            _integer(split_counts[name], f"split_counts.{name}")
            for name in ("train", "early_stop", "calibration", "blind_test")
        )
        != record_count
    ):
        raise ValidationError("split_count", "split counts do not sum to record count")

    ground_truth = _mapping(summary["ground_truth"], "quality_summary.ground_truth")
    eligible = _integer(ground_truth["eligible_packets"], "ground_truth.eligible_packets")
    labeled = _integer(ground_truth["labeled_eligible_packets"], "ground_truth.labeled_eligible_packets")
    expected_coverage = labeled / eligible
    if not math.isclose(expected_coverage, _number(ground_truth["coverage"], "ground_truth.coverage"), abs_tol=1e-12):
        raise ValidationError("ground_truth_coverage", "coverage ratio mismatch")

    for offset, source_value in enumerate(_sequence(document["source_profiles"], "source_profiles")):
        source = _mapping(source_value, f"source_profiles[{offset}]")
        status = _string(source["ingestion_status"], "source.ingestion_status")
        decision = _string(source["decision"], "source.decision")
        license_value = _mapping(source["license"], "source.license")
        privacy = _mapping(source["privacy"], "source.privacy")
        truth = _mapping(source["ground_truth"], "source.ground_truth")
        all_verified = all(bool(value) for value in (license_value["verified"], privacy["verified"], truth["verified"]))
        if status == "verified" and (not all_verified or decision in ("CONDITIONAL", "REJECT_DIRECT_IMPORT")):
            raise ValidationError("source_decision_status", _string(source["profile_id"], "source.profile_id"))
        if decision == "REJECT_DIRECT_IMPORT" and status != "rejected":
            raise ValidationError("legacy_direct_import", "rejected source cannot be ingested")


def validate_dataset_sample(document: Mapping[str, Any]) -> None:
    ensure_finite(document)
    values = [
        _integer(item, f"feature_values[{index}]")
        for index, item in enumerate(_sequence(document["feature_values"], "feature_values"))
    ]
    actual_digest = sha256_bytes(feature_tensor_bytes(values))
    if actual_digest != _string(document["feature_tensor_digest"], "feature_tensor_digest"):
        raise ValidationError("feature_tensor_digest", f"{actual_digest} != {document['feature_tensor_digest']}")
    if _integer(document["feature_offset"], "feature_offset") % 48:
        raise ValidationError("feature_offset", "offset is not 48-byte aligned")

    source_profile = _string(document["source_profile"], "source_profile")
    expected_split = split_name(
        _string(document["source_revision"], "source_revision"),
        _string(document["capture_family_id"], "capture_family_id"),
    )
    observed_split = _string(document["split"], "split")
    if source_profile == "masi-synthetic-p4-window/v1" and expected_split != observed_split:
        raise ValidationError("capture_family_split", f"{expected_split} != {observed_split}")

    label = _mapping(document["label"], "label")
    eligible = _integer(label["eligible_packets"], "label.eligible_packets")
    labeled = _integer(label["labeled_eligible_packets"], "label.labeled_eligible_packets")
    coverage = _number(label["coverage"], "label.coverage")
    expected = labeled / eligible if eligible else 0.0
    if not math.isclose(expected, coverage, abs_tol=1e-12):
        raise ValidationError("sample_label_coverage", "packet coverage ratio mismatch")
    attacks = _integer(label["supported_attack_packets"], "label.supported_attack_packets")
    attack_share = _number(label["attack_share"], "label.attack_share")
    if not math.isclose(attacks / eligible if eligible else 0.0, attack_share, abs_tol=1e-12):
        raise ValidationError("sample_attack_share", "attack share ratio mismatch")
    purity = _string(label["purity"], "label.purity")
    binary_label = label["binary_label"]
    if purity == "fit_benign" and not (binary_label == 0 and coverage == 1.0 and attack_share == 0.0):
        raise ValidationError("fit_benign_semantics", "invalid benign fit sample")
    if purity == "fit_attack" and not (binary_label == 1 and coverage == 1.0 and attack_share >= 0.9):
        raise ValidationError("fit_attack_semantics", "invalid attack fit sample")
    if purity not in ("fit_benign", "fit_attack") and observed_split != "evaluation_only":
        raise ValidationError("evaluation_only_label", purity)


def _features_and_ranks(document: Mapping[str, Any]) -> None:
    if tuple(_sequence(document["feature_order"], "feature_order")) != FEATURE_ORDER:
        raise ValidationError("feature_order", "explanation feature order drift")
    global_rows = [
        _mapping(item, "global_importance[]") for item in _sequence(document["global_importance"], "global_importance")
    ]
    features = {_string(item["feature_id"], "global feature") for item in global_rows}
    ranks = {_integer(item["rank"], "global rank") for item in global_rows}
    if features != set(FEATURE_ORDER) or ranks != set(range(1, 7)):
        raise ValidationError("global_feature_ranks", "features or ranks are not exact")


def validate_explanation(document: Mapping[str, Any]) -> None:
    ensure_finite(document)
    _features_and_ranks(document)
    method = _mapping(document["method"], "method")
    method_id = _string(method["method_id"], "method.method_id")
    tolerance = _mapping(method["numeric_tolerance"], "method.numeric_tolerance")
    absolute = _number(tolerance["absolute"], "numeric_tolerance.absolute")
    relative = _number(tolerance["relative"], "numeric_tolerance.relative")
    for sample_value in _sequence(document["samples"], "samples"):
        sample = _mapping(sample_value, "sample")
        contributions = [
            _number(value, "contribution") for value in _sequence(sample["contributions"], "contributions")
        ]
        base = _number(sample["base_value"], "base_value")
        raw = _number(sample["raw_output"], "raw_output")
        reconstructed = _number(sample["reconstructed_raw_output"], "reconstructed_raw_output")
        expected = (
            sum(contributions) / len(contributions)
            if method_id == "ae-scaled-log-smoothl1-residual/v1"
            else base + sum(contributions)
        )
        allowed = max(absolute, relative * max(abs(raw), abs(expected)))
        if abs(expected - raw) > allowed or abs(reconstructed - raw) > allowed:
            raise ValidationError("additivity_mismatch", _string(sample["sample_id"], "sample_id"))
        declared_error = _number(sample["additivity_absolute_error"], "additivity_absolute_error")
        if not math.isclose(declared_error, abs(expected - raw), abs_tol=max(absolute, 1e-12)):
            raise ValidationError("additivity_error_field", _string(sample["sample_id"], "sample_id"))
        if method_id == "ae-scaled-log-smoothl1-residual/v1":
            if any(value < 0 for value in contributions):
                raise ValidationError("negative_residual", _string(sample["sample_id"], "sample_id"))
            residual = _number(sample.get("residual_mean"), "residual_mean")
            mean = expected
            if abs(mean - residual) > allowed or abs(residual - raw) > allowed:
                raise ValidationError("residual_mean_mismatch", _string(sample["sample_id"], "sample_id"))

    coverage = _mapping(document["coverage"], "coverage")
    eligible = _integer(coverage["eligible_samples"], "coverage.eligible_samples")
    included = _integer(coverage["included_samples"], "coverage.included_samples")
    if not math.isclose(included / eligible, _number(coverage["coverage"], "coverage.coverage"), abs_tol=1e-12):
        raise ValidationError("explanation_coverage", "coverage ratio mismatch")
    for group_value in _sequence(coverage["per_source_scenario"], "coverage.per_source_scenario"):
        group = _mapping(group_value, "coverage group")
        group_eligible = _integer(group["eligible"], "coverage group eligible")
        group_included = _integer(group["included"], "coverage group included")
        if not math.isclose(
            group_included / group_eligible, _number(group["coverage"], "coverage group coverage"), abs_tol=1e-12
        ):
            raise ValidationError("explanation_group_coverage", _string(group["scenario_id"], "scenario_id"))

    truncation = _mapping(document["truncation"], "truncation")
    if _integer(truncation["included"], "truncation.included") != included:
        raise ValidationError("truncation_included", "truncation and coverage included differ")
    stability = _mapping(document["stability"], "stability")
    if tuple(_sequence(stability["seed_set"], "stability.seed_set")) != SEEDS:
        raise ValidationError("seed_set", "explanation seed set drift")
    correlation = _number(stability["global_rank_spearman_median"], "stability correlation")
    expected_status = "stable" if correlation >= 0.8 else "unstable"
    if stability["status"] != expected_status:
        raise ValidationError("stability_status", f"expected {expected_status}")
    if any(bool(value) for value in _mapping(document["safety"], "safety").values()):
        raise ValidationError("explanation_safety", "all safety flags must be false")


def _mutation_parts(path: str) -> list[str | int]:
    parts: list[str | int] = []
    position = 0
    for match in _MUTATION_TOKEN.finditer(path):
        if match.start() != position and path[position : match.start()] != ".":
            raise ValidationError("invalid_mutation_path", path)
        if match.group(1) is not None:
            parts.append(match.group(1))
        else:
            parts.append(int(match.group(2)))
        position = match.end()
        if position < len(path) and path[position] == ".":
            position += 1
    if position != len(path):
        raise ValidationError("invalid_mutation_path", path)
    return parts


def apply_mutation(document: Mapping[str, Any], path: str, value: object) -> dict[str, Any]:
    mutated = copy.deepcopy(dict(document))
    current: Any = mutated
    parts = _mutation_parts(path)
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value
    return mutated


def _expect_rejection(callable_: Any, reason: str) -> None:
    try:
        callable_()
    except ValidationError as error:
        if error.code != reason:
            raise ValidationError("wrong_rejection_reason", f"{error.code} != {reason}") from error
        return
    raise ValidationError("missing_rejection", reason)


def _validate_catalog(repo: Path, catalog_relative: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    catalog_path = resolve_repo_file(repo, catalog_relative, maximum_bytes=1024 * 1024)
    catalog = _mapping(load_json(catalog_path), catalog_relative)
    vectors = [_mapping(value, f"{catalog_relative}.vectors[]") for value in _sequence(catalog["vectors"], "vectors")]
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    digests: dict[str, str] = {catalog_relative: sha256_file(catalog_path)}
    base = catalog_path.parent
    for vector in vectors:
        vector_id = _string(vector["id"], "vector.id")
        relative = _string(vector["path"], "vector.path")
        if vector_id in seen_ids or relative in seen_paths:
            raise ValidationError("duplicate_golden", f"{vector_id}:{relative}")
        seen_ids.add(vector_id)
        seen_paths.add(relative)
        path = base / relative
        resolve_repo_file(repo, path.relative_to(repo).as_posix(), maximum_bytes=4 * 1024 * 1024)
        observed = sha256_file(path)
        expected = vector.get("sha256")
        if expected is not None and expected != observed:
            raise ValidationError("golden_digest", f"{relative}: {observed} != {expected}")
        digests[path.relative_to(repo).as_posix()] = observed
    return vectors, digests


def validate_public_contracts(repo: Path) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    dataset_schema_path = resolve_repo_file(repo, "contracts/dataset/v1/schema.json", maximum_bytes=4 * 1024 * 1024)
    sample_schema_path = resolve_repo_file(
        repo, "contracts/dataset/v1/sample-schema.json", maximum_bytes=4 * 1024 * 1024
    )
    explanation_schema_path = resolve_repo_file(
        repo, "contracts/model-explanation/v1/schema.json", maximum_bytes=4 * 1024 * 1024
    )
    dataset_schema = _mapping(load_json(dataset_schema_path), "dataset schema")
    sample_schema = _mapping(load_json(sample_schema_path), "sample schema")
    explanation_schema = _mapping(load_json(explanation_schema_path), "explanation schema")
    for schema in (dataset_schema, sample_schema, explanation_schema):
        Draft202012Validator.check_schema(schema)

    dataset_vectors, dataset_digests = _validate_catalog(repo, "contracts/dataset/v1/golden/catalog.json")
    explanation_vectors, explanation_digests = _validate_catalog(
        repo, "contracts/model-explanation/v1/golden/catalog.json"
    )
    dataset_base = repo / "contracts/dataset/v1/golden"
    explanation_base = repo / "contracts/model-explanation/v1/golden"

    manifest = _mapping(load_json(dataset_base / "manifest-golden-v1.json"), "dataset manifest golden")
    sample = _mapping(load_json(dataset_base / "sample-golden-v1.json"), "dataset sample golden")
    _validate_schema(dataset_schema, manifest, code="dataset_schema")
    _validate_schema(sample_schema, sample, code="dataset_sample_schema")
    validate_dataset_manifest(manifest)
    validate_dataset_sample(sample)

    split_golden = _mapping(load_json(dataset_base / "split-hash-golden-v1.json"), "split golden")
    source_revision = _string(split_golden["source_revision"], "split source revision")
    for vector_value in _sequence(split_golden["vectors"], "split vectors"):
        vector = _mapping(vector_value, "split vector")
        family = _string(vector["capture_family_id"], "capture family")
        fraction = split_fraction(source_revision, family)
        if not math.isclose(fraction, _number(vector["fraction"], "split fraction"), abs_tol=1e-15):
            raise ValidationError("split_fraction", family)
        if split_name(source_revision, family) != vector["expected_split"]:
            raise ValidationError("capture_family_split", family)

    rejection = _mapping(load_json(dataset_base / "rejection-golden-v1.json"), "dataset rejection golden")
    expected_dataset_reasons = {
        "legacy-direct-import": "legacy_direct_import",
        "capture-family-leak": "capture_family_cross_split",
        "invalid-window-zero-fill": "invalid_window_cannot_be_fitted",
        "unknown-major": "unknown_major",
    }
    for vector_value in _sequence(rejection["vectors"], "dataset rejection vectors"):
        vector = _mapping(vector_value, "dataset rejection vector")
        vector_id = _string(vector["id"], "dataset rejection id")
        if vector["reason"] != expected_dataset_reasons.get(vector_id):
            raise ValidationError("dataset_rejection_vector", vector_id)

    explanation_documents: dict[str, dict[str, Any]] = {}
    for name in ("lr-golden-v1.json", "xgb-golden-v1.json", "ae-golden-v1.json"):
        document = _mapping(load_json(explanation_base / name), name)
        _validate_schema(explanation_schema, document, code="explanation_schema")
        validate_explanation(document)
        explanation_documents[name] = document

    explanation_rejection = _mapping(
        load_json(explanation_base / "rejection-golden-v1.json"), "explanation rejection golden"
    )
    bases = {
        "causal-claim": explanation_documents["lr-golden-v1.json"],
        "realtime-injection": explanation_documents["lr-golden-v1.json"],
        "xgb-no-background": explanation_documents["xgb-golden-v1.json"],
        "additivity-mismatch": explanation_documents["lr-golden-v1.json"],
        "ae-residual-mismatch": explanation_documents["ae-golden-v1.json"],
        "nan": explanation_documents["lr-golden-v1.json"],
        "unknown-major": explanation_documents["lr-golden-v1.json"],
    }
    semantic_reasons = {
        "additivity-mismatch": "additivity_mismatch",
        "ae-residual-mismatch": "residual_mean_mismatch",
    }
    for vector_value in _sequence(explanation_rejection["vectors"], "explanation rejection vectors"):
        vector = _mapping(vector_value, "explanation rejection vector")
        vector_id = _string(vector["id"], "explanation rejection id")
        mutation = _mapping(vector["mutation"], "mutation")
        if len(mutation) != 1 or vector_id not in bases:
            raise ValidationError("explanation_rejection_vector", vector_id)
        path, value = next(iter(mutation.items()))
        mutated = apply_mutation(bases[vector_id], path, value)
        reason = semantic_reasons.get(vector_id)
        if reason is not None:
            _validate_schema(explanation_schema, mutated, code="explanation_schema")

            def reject_mutated(document: Mapping[str, Any] = mutated) -> None:
                validate_explanation(document)

            _expect_rejection(reject_mutated, reason)
        else:
            try:
                _validate_schema(explanation_schema, mutated, code="explanation_schema")
            except ValidationError:
                pass
            else:
                raise ValidationError("missing_rejection", vector_id)

    contract_digests = {
        "dataset_schema": sha256_file(dataset_schema_path),
        "dataset_sample_schema": sha256_file(sample_schema_path),
        "dataset_profile": sha256_file(repo / "contracts/profiles/v1/dataset-p4-window-binary.json"),
        "model_explanation_schema": sha256_file(explanation_schema_path),
        "model_explanation_profile": sha256_file(repo / "contracts/profiles/v1/model-explanation-evidence.json"),
        "edge_feature_profile": sha256_file(repo / "contracts/profiles/v1/edge-feature-window.json"),
    }
    contract_digests.update(dataset_digests)
    contract_digests.update(explanation_digests)
    return {
        "schema_version": "offline-ml-contract-validation/v1",
        "result": "PASS",
        "dataset_vectors": len(dataset_vectors),
        "explanation_vectors": len(explanation_vectors),
        "feature_order": list(FEATURE_ORDER),
        "contract_set_digest": sha256_bytes(canonical_json_bytes(contract_digests)),
        "artifact_digests": dict(sorted(contract_digests.items())),
    }


def iter_validation_errors(repo: Path) -> Iterable[str]:
    try:
        validate_public_contracts(repo)
    except ValidationError as error:
        yield str(error)
