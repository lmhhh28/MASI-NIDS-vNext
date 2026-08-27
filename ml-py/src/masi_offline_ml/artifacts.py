"""Candidate evidence, winner selection, and immutable repository/bundle output."""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import numpy as np

from .canonical import (
    ValidationError,
    canonical_json_bytes,
    create_fresh_directory,
    sha256_bytes,
    sha256_file,
    write_fresh_bytes,
    write_fresh_json,
)
from .contracts import FEATURE_ORDER
from .dataset import DatasetData
from .models import TrainedCandidate
from .onnx_export import inspect_model, rewrite_metadata, run_ort


@dataclass(frozen=True)
class CandidateContracts:
    feature_schema: dict[str, Any]
    label_taxonomy: dict[str, Any]
    output_adapter: dict[str, Any]
    calibration: dict[str, Any]
    feature_digest: str
    label_digest: str
    adapter_digest: str
    calibration_digest: str


def candidate_contracts(candidate: TrainedCandidate) -> CandidateContracts:
    calibration = {
        "schema_version": "masi-platt-calibration/v1",
        "kind": "platt-in-onnx-graph",
        "slope": candidate.calibration.slope,
        "intercept": candidate.calibration.intercept,
        "positive_slope_required": True,
    }
    calibration_digest = sha256_bytes(canonical_json_bytes(calibration))
    feature_schema = {
        "dtype": "uint64-le",
        "field_order": list(FEATURE_ORDER),
        "shape": [1, 6],
        "units": "mixed",
        "window_duration_ms": 10000,
        "preprocessing_in_graph": "Cast(float32)->Log1p",
    }
    label_taxonomy = {
        "calibration": {"config_digest": calibration_digest, "kind": "none"},
        "class_order_policy": "stable-explicit-in-manifest",
        "id_reuse_policy": "never-reuse",
        "label_ids": [0, 1],
        "mode": "single-label",
        "score_domain": "probability",
        "threshold": {"kind": "fixed", "value": candidate.thresholds.confidence},
        "unknown_label_policy": "old-reader-preserves-unknown-without-triggering-old-effect-policy",
    }
    feature_digest = sha256_bytes(canonical_json_bytes(feature_schema))
    label_digest = sha256_bytes(canonical_json_bytes(label_taxonomy))
    adapter_body = {
        "adapter_id": "masi-window-adapter-v1",
        "version": "v1",
        "mapping_kind": "deterministic-implementation",
        "class_order": [0, 1],
        "axis": 1,
        "top_k": 1,
        "threshold": candidate.thresholds.alert,
        "calibration": calibration_digest,
        "executable_policy": "no-executable-code-injected-from-bundle",
        "ood_policy": {
            "mode": "max-probability-below-threshold",
            "threshold": candidate.thresholds.confidence,
            "decision": "abstain",
        },
    }
    adapter_digest = sha256_bytes(canonical_json_bytes(adapter_body))
    output_adapter = dict(adapter_body)
    output_adapter["adapter_digest"] = adapter_digest
    return CandidateContracts(
        feature_schema=feature_schema,
        label_taxonomy=label_taxonomy,
        output_adapter=output_adapter,
        calibration=calibration,
        feature_digest=feature_digest,
        label_digest=label_digest,
        adapter_digest=adapter_digest,
        calibration_digest=calibration_digest,
    )


def finalize_candidate_metadata(candidate: TrainedCandidate, dataset: DatasetData) -> TrainedCandidate:
    contracts = candidate_contracts(candidate)
    metadata = {
        "candidate_id": candidate.candidate_id,
        "class_order": "benign=0,attack=1",
        "dataset_id": str(dataset.manifest["dataset_id"]),
        "dataset_revision": str(dataset.manifest["dataset_revision"]),
        "feature_contract_digest": contracts.feature_digest,
        "feature_dtype": "uint64-le",
        "feature_order": ",".join(FEATURE_ORDER),
        "label_contract_digest": contracts.label_digest,
        "output_adapter_digest": contracts.adapter_digest,
        "output_domain": "finite-float32-probability",
        "runtime_profile": "model-runtime-central-cpu/v1",
        "seed": str(candidate.seed),
    }
    payload = rewrite_metadata(candidate.onnx_payload, metadata)
    evidence = inspect_model(payload)
    scores = run_ort(payload, dataset.features[candidate.blind_indices])
    error = np.abs(scores[:, 1].astype(np.float64) - candidate.blind_probability)
    if float(np.max(error, initial=0.0)) > 2e-5:
        raise ValidationError("final_metadata_numeric", f"max_abs={float(np.max(error))}")
    observed_metadata = cast(dict[str, str], evidence["metadata"])
    if observed_metadata != metadata:
        raise ValidationError("onnx_metadata", f"{observed_metadata} != {metadata}")
    evidence["numeric"] = {
        "records": int(candidate.blind_indices.size),
        "max_absolute_error": float(np.max(error, initial=0.0)),
        "mean_absolute_error": float(np.mean(error)),
        "result": "PASS",
    }
    training = dict(candidate.training_evidence)
    training["model_digest"] = sha256_bytes(payload)
    return replace(candidate, onnx_payload=payload, onnx_evidence=evidence, training_evidence=training)


def candidate_revision(candidate: TrainedCandidate) -> str:
    identity = {
        "candidate_id": candidate.candidate_id,
        "seed": candidate.seed,
        "model_digest": candidate.onnx_evidence["model_digest"],
        "calibration": candidate.calibration.as_dict(),
        "thresholds": candidate.thresholds.as_dict(),
    }
    return sha256_bytes(canonical_json_bytes(identity)).split(":", 1)[1][:40]


def write_candidate_artifacts(
    directory: Path,
    candidate: TrainedCandidate,
    dataset: DatasetData,
    explanation: dict[str, Any],
) -> dict[str, Any]:
    directory = create_fresh_directory(directory)
    contracts = candidate_contracts(candidate)
    write_fresh_bytes(directory / "model.onnx", candidate.onnx_payload, mode=0o440)
    write_fresh_json(directory / "training.json", candidate.training_evidence)
    write_fresh_json(directory / "metrics.json", candidate.metrics)
    write_fresh_json(directory / "onnx-evidence.json", candidate.onnx_evidence)
    write_fresh_json(directory / "calibration.json", contracts.calibration)
    write_fresh_json(directory / "thresholds.json", candidate.thresholds.as_dict())
    write_fresh_json(directory / "feature-schema.json", contracts.feature_schema)
    write_fresh_json(directory / "label-taxonomy.json", contracts.label_taxonomy)
    write_fresh_json(directory / "output-adapter.json", contracts.output_adapter)
    write_fresh_json(directory / "explanation.json", explanation)
    if candidate.scaler is not None:
        write_fresh_json(directory / "scaler.json", candidate.scaler.as_dict())
    manifest_files: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir(), key=lambda value: value.name.encode("utf-8")):
        if path.is_file():
            manifest_files[path.name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = {
        "schema_version": "offline-ml-candidate-artifact/v1",
        "candidate_id": candidate.candidate_id,
        "seed": candidate.seed,
        "revision": candidate_revision(candidate),
        "dataset_id": dataset.manifest["dataset_id"],
        "dataset_revision": dataset.manifest["dataset_revision"],
        "runtime_profiles": {
            "model-runtime-central-cpu/v1": {"applicability": "APPLICABLE", "result": "PASS"},
            "model-runtime-central-cuda/v1": {
                "applicability": "NOT_APPLICABLE",
                "result": "NOT_RUN",
                "stable_reason": "EXACT_BUNDLE_DOES_NOT_DECLARE_CUDA_RUNTIME",
            },
        },
        "contract_digests": {
            "feature_contract_digest": contracts.feature_digest,
            "label_contract_digest": contracts.label_digest,
            "output_adapter_digest": contracts.adapter_digest,
            "calibration_digest": contracts.calibration_digest,
        },
        "quality": candidate.metrics,
        "eligible": bool(candidate.metrics["passes_thresholds"]),
        "files": manifest_files,
    }
    write_fresh_json(directory / "candidate-manifest.json", manifest)
    return manifest


def select_winner(candidates: list[TrainedCandidate]) -> dict[str, Any]:
    grouped: dict[str, list[TrainedCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.candidate_id, []).append(candidate)
    required = ("lr-window-binary/v1", "xgb-window-binary/v1", "ae-window-benign/v1")
    summaries: dict[str, dict[str, Any]] = {}
    eligible: list[tuple[str, float, float, float]] = []
    complexity = {name: rank for rank, name in enumerate(required)}
    for candidate_id in required:
        values = sorted(grouped.get(candidate_id, []), key=lambda value: value.seed)
        if [value.seed for value in values] != [17, 29, 43]:
            raise ValidationError("mandatory_candidate_execution", candidate_id)
        passes = all(bool(value.metrics["passes_thresholds"]) for value in values)
        robust_ap = min(float(value.metrics["average_precision"]) for value in values)
        worst_fpr = max(float(value.metrics["false_positive_rate"]) for value in values)
        cpu_numeric_error = max(
            float(cast(dict[str, Any], value.onnx_evidence["numeric"])["max_absolute_error"]) for value in values
        )
        summaries[candidate_id] = {
            "seeds": [17, 29, 43],
            "executions": 3,
            "eligible": passes,
            "robust_average_precision": robust_ap,
            "worst_false_positive_rate": worst_fpr,
            "max_ort_absolute_error": cpu_numeric_error,
            "result": "ELIGIBLE" if passes else "REJECTED",
        }
        if passes:
            eligible.append((candidate_id, robust_ap, worst_fpr, cpu_numeric_error))
    if not eligible:
        return {
            "schema_version": "offline-ml-candidate-selection/v1",
            "result": "FAIL",
            "qualification": "NOT_QUALIFIED",
            "winner": "none",
            "reason": "no_eligible_candidate",
            "candidates": summaries,
            "ensemble": False,
            "fallback": False,
        }
    best_ap = max(item[1] for item in eligible)
    best_fpr = min(item[2] for item in eligible if best_ap - item[1] <= 0.01)
    simple = [item for item in eligible if best_ap - item[1] <= 0.01 and item[2] - best_fpr <= 0.002]
    if simple:
        winner = min(simple, key=lambda item: complexity[item[0]])
        rule = "simplicity-within-robust-ap-and-fpr-band"
    else:
        winner = min(eligible, key=lambda item: (-item[1], item[2], item[3], complexity[item[0]]))
        rule = "robust-ap-fpr-cpu-numeric-complexity"
    return {
        "schema_version": "offline-ml-candidate-selection/v1",
        "result": "PASS",
        "qualification": "QUALIFIED",
        "winner": winner[0],
        "winner_seed": 17,
        "selection_rule": rule,
        "candidates": summaries,
        "ensemble": False,
        "fallback": False,
    }


def _config_pbtxt(model_id: str) -> bytes:
    text = f'''name: "{model_id}"
backend: "onnxruntime"
max_batch_size: 256
input [
  {{
    name: "features"
    data_type: TYPE_UINT64
    dims: [ 6 ]
  }}
]
output [
  {{
    name: "scores"
    data_type: TYPE_FP32
    dims: [ 2 ]
  }}
]
dynamic_batching {{
  preferred_batch_size: [ 32, 64, 128, 256 ]
  max_queue_delay_microseconds: 200
  default_queue_policy {{
    max_queue_size: 1024
  }}
}}
instance_group [
  {{
    count: 1
    kind: KIND_CPU
  }}
]
'''
    return text.encode("utf-8")


def _repository_closure_digest(members: list[dict[str, Any]]) -> str:
    lines = sorted(f"{member['role']}\t{member['rel_path']}\t{member['member_digest']}" for member in members)
    return sha256_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def build_winner_bundle(
    repo: Path,
    output: Path,
    winner: TrainedCandidate,
    selection: dict[str, Any],
    dataset: DatasetData,
    explanation: dict[str, Any],
    candidate_manifests: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    if selection.get("winner") != winner.candidate_id or selection.get("winner_seed") != winner.seed:
        raise ValidationError("winner_identity", f"{selection.get('winner')}:{selection.get('winner_seed')}")
    output = create_fresh_directory(output)
    repository = output / "repository"
    repository.mkdir(mode=0o750)
    model_id = "masi-ids-window-v1"
    model_directory = repository / model_id
    version_directory = model_directory / "1"
    version_directory.mkdir(parents=True, mode=0o750)
    contracts = candidate_contracts(winner)
    model_digest = sha256_bytes(winner.onnx_payload)
    revision = candidate_revision(winner)
    runtime_digest = sha256_file(repo / "contracts/inference/v1/model-runtime-central-cpu.json")
    optimization_digest = sha256_file(repo / "contracts/inference/v1/optimization-profile-central-cpu.json")
    wire_digest = sha256_file(repo / "contracts/inference/v1/profile.json")
    binding_identity = {
        "feature_contract_digest": contracts.feature_digest,
        "inference_wire_profile_digest": wire_digest,
        "label_contract_digest": contracts.label_digest,
        "model_bundle_digest": model_digest,
        "model_digest": model_digest,
        "model_id": model_id,
        "optimization_profile_digest": optimization_digest,
        "optimization_profile_id": "optimization-profile-central-cpu/v1",
        "output_adapter_digest": contracts.adapter_digest,
        "revision": revision,
        "runtime_profile_digest": runtime_digest,
        "runtime_profile_id": "model-runtime-central-cpu/v1",
    }
    model_revision_digest = sha256_bytes(canonical_json_bytes(binding_identity))
    bundle_manifest = {
        "schema_version": "masi-model-bundle-manifest/v1",
        "model_id": model_id,
        "revision": revision,
        "bundle_digest": model_digest,
        "model_digest": model_digest,
        "model_revision_digest": model_revision_digest,
        "binding_identity": binding_identity,
        "feature_schema": contracts.feature_schema,
        "label_taxonomy": contracts.label_taxonomy,
        "output_adapter": contracts.output_adapter,
        "contract_digests": {
            "feature_contract_digest": contracts.feature_digest,
            "label_contract_digest": contracts.label_digest,
            "output_adapter_digest": contracts.adapter_digest,
        },
        "triton": {
            "max_batch_size": 256,
            "preferred_batch_size": [32, 64, 128, 256],
            "max_queue_delay_microseconds": 200,
            "max_queue_size": 1024,
            "instance_group": {"kind": "KIND_CPU", "count": 1},
        },
        "offline_qualification": {
            "dataset_id": dataset.manifest["dataset_id"],
            "dataset_revision": dataset.manifest["dataset_revision"],
            "candidate_id": winner.candidate_id,
            "seed": winner.seed,
            "selection_digest": sha256_bytes(canonical_json_bytes(selection)),
            "explanation_digest": sha256_bytes(canonical_json_bytes(explanation)),
            "cuda": {
                "applicability": "NOT_APPLICABLE",
                "result": "NOT_RUN",
                "stable_reason": "EXACT_BUNDLE_DOES_NOT_DECLARE_CUDA_RUNTIME",
            },
        },
    }
    model_path = version_directory / "model.onnx"
    config_path = model_directory / "config.pbtxt"
    bundle_manifest_path = repository / "bundle-manifest.json"
    write_fresh_bytes(model_path, winner.onnx_payload, mode=0o440)
    write_fresh_bytes(config_path, _config_pbtxt(model_id), mode=0o440)
    write_fresh_json(bundle_manifest_path, bundle_manifest)
    os.chmod(bundle_manifest_path, 0o440)
    members = [
        {
            "rel_path": "bundle-manifest.json",
            "member_digest": sha256_file(bundle_manifest_path),
            "role": "bundle-manifest",
        },
        {
            "rel_path": f"{model_id}/1/model.onnx",
            "member_digest": sha256_file(model_path),
            "role": "model",
        },
        {
            "rel_path": f"{model_id}/config.pbtxt",
            "member_digest": sha256_file(config_path),
            "role": "config",
        },
    ]
    closure_digest = _repository_closure_digest(members)
    closure_manifest = {
        "schema_version": "masi-repository-closure/v1",
        "identity": f"repo-{model_id}-{revision[:12]}",
        "closure_digest": closure_digest,
        "members": members,
    }
    closure_path = repository / "closure-manifest.json"
    write_fresh_json(closure_path, closure_manifest)
    os.chmod(closure_path, 0o440)
    os.chmod(version_directory, 0o550)
    os.chmod(model_directory, 0o550)
    os.chmod(repository, 0o550)
    write_fresh_json(output / "candidate-selection.json", selection)
    write_fresh_json(output / "dataset-manifest.json", dataset.manifest)
    write_fresh_json(output / "explanation.json", explanation)
    write_fresh_json(
        output / "candidate-manifests.json",
        {
            f"{candidate_id}:seed-{seed}": manifest
            for (candidate_id, seed), manifest in sorted(candidate_manifests.items())
        },
    )
    bundle_result = {
        "schema_version": "offline-ml-immutable-bundle/v1",
        "model_id": model_id,
        "revision": revision,
        "model_digest": model_digest,
        "model_revision_digest": model_revision_digest,
        "repository_identity": closure_manifest["identity"],
        "repository_closure_digest": closure_digest,
        "runtime_profiles": {
            "model-runtime-central-cpu/v1": {
                "applicability": "APPLICABLE",
                "runtime_profile_digest": runtime_digest,
                "result": "PASS",
            },
            "model-runtime-central-cuda/v1": {
                "applicability": "NOT_APPLICABLE",
                "result": "NOT_RUN",
                "stable_reason": "EXACT_BUNDLE_DOES_NOT_DECLARE_CUDA_RUNTIME",
            },
        },
        "contract_digests": bundle_manifest["contract_digests"],
        "selection_digest": sha256_bytes(canonical_json_bytes(selection)),
        "dataset_manifest_digest": sha256_bytes(canonical_json_bytes(dataset.manifest)),
        "explanation_digest": sha256_bytes(canonical_json_bytes(explanation)),
    }
    write_fresh_json(output / "bundle-result.json", bundle_result)
    return bundle_result


def deterministic_bundle_tar(
    bundle_directory: Path, output: Path, *, source_date_epoch: int = 1787334400
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise ValidationError("output_exists", str(output))
    members = [path for path in bundle_directory.rglob("*") if path.is_file()]
    members.sort(key=lambda path: path.relative_to(bundle_directory).as_posix().encode("utf-8"))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in members:
            if path.is_symlink() or not path.is_file():
                raise ValidationError("bundle_member", str(path))
            relative = path.relative_to(bundle_directory).as_posix()
            info = tarfile.TarInfo(relative)
            info.size = path.stat().st_size
            info.mode = 0o440
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = source_date_epoch
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    payload = buffer.getvalue()
    write_fresh_bytes(output, payload, mode=0o440)
    return {
        "schema_version": "offline-ml-bundle-archive/v1",
        "path": output.name,
        "sha256": sha256_bytes(payload),
        "bytes": len(payload),
        "members": len(members),
        "source_date_epoch": source_date_epoch,
        "format": "pax-uncompressed-tar",
    }
