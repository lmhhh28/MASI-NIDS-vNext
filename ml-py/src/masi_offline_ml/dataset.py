"""Deterministic P4-window module dataset generation and loading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
from jsonschema import Draft202012Validator, FormatChecker

from .canonical import (
    ValidationError,
    canonical_json_bytes,
    create_fresh_directory,
    load_json,
    resolve_repo_file,
    sha256_bytes,
    sha256_file,
    write_fresh_bytes,
    write_fresh_json,
)
from .contracts import (
    FEATURE_ORDER,
    feature_tensor_bytes,
    split_name,
    telemetry_cell_selector_digest,
    validate_dataset_manifest,
    validate_dataset_sample,
)

SOURCE_PROFILE = "masi-synthetic-p4-window/v1"
SOURCE_REVISION = "masi-synthetic-fixture-r1"
DATASET_ID = "masi-synthetic-p4-window-module"
FAMILY_QUOTAS = {"train": 24, "early_stop": 6, "calibration": 6, "blind_test": 4}
WINDOWS_PER_FAMILY = 64
LABEL_WINDOWS_PER_FAMILY = WINDOWS_PER_FAMILY // 2


@dataclass(frozen=True)
class DatasetData:
    manifest: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    features: npt.NDArray[np.uint64]
    labels: npt.NDArray[np.int64]
    weights: npt.NDArray[np.float64]
    splits: npt.NDArray[np.str_]

    def indices(self, split: str) -> npt.NDArray[np.int64]:
        return np.flatnonzero(self.splits == split).astype(np.int64, copy=False)


def _hash_words(text: str) -> tuple[int, ...]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return tuple(int.from_bytes(digest[index : index + 4], "big") for index in range(0, 24, 4))


def _feature_values(family: str, window_index: int, label: int) -> tuple[int, int, int, int, int, int]:
    words = _hash_words(f"masi-synthetic-feature-v1\0{family}\0{window_index}\0{label}")
    if label == 0:
        packets = 40 + words[0] % 41
        bytes_per_packet = 64 + words[1] % 33
        nonzero_cells = 8 + words[2] % 17
        max_cell_packets = 2 + words[3] % 4
        max_cell_bytes = max_cell_packets * (64 + words[4] % 33)
    else:
        packets = 8000 + words[0] % 4001
        bytes_per_packet = 80 + words[1] % 41
        nonzero_cells = 32 + words[2] % 33
        max_cell_packets = 1000 + words[3] % 2001
        max_cell_bytes = max_cell_packets * (80 + words[4] % 41)
    aggregate_bytes = packets * bytes_per_packet
    return packets, aggregate_bytes, nonzero_cells, max_cell_packets, max_cell_bytes, 10


def _select_families() -> list[tuple[str, str]]:
    selected: dict[str, list[str]] = {name: [] for name in FAMILY_QUOTAS}
    candidate_index = 0
    while any(len(selected[name]) < FAMILY_QUOTAS[name] for name in FAMILY_QUOTAS):
        family = f"masi-synthetic-capture-{candidate_index:06d}"
        split = split_name(SOURCE_REVISION, family)
        if len(selected[split]) < FAMILY_QUOTAS[split]:
            selected[split].append(family)
        candidate_index += 1
        if candidate_index > 100000:
            raise ValidationError("family_generation", "unable to satisfy deterministic split quotas")
    return sorted(
        ((family, split) for split, families in selected.items() for family in families),
        key=lambda item: item[0].encode("utf-8"),
    )


def _iso(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _source_tree_identity(repo: Path) -> str:
    tracked = (
        repo / "ml-py/src/masi_offline_ml/dataset.py",
        repo / "contracts/dataset/v1/schema.json",
        repo / "contracts/dataset/v1/sample-schema.json",
        repo / "contracts/profiles/v1/dataset-p4-window-binary.json",
    )
    digest_map = {path.relative_to(repo).as_posix(): sha256_file(path) for path in tracked}
    return "dirty:" + hashlib.sha256(canonical_json_bytes(digest_map)).hexdigest()


def _build_drafts(families: list[tuple[str, str]]) -> list[dict[str, Any]]:
    base_time = datetime(2031, 1, 1, tzinfo=UTC)
    drafts: list[dict[str, Any]] = []
    global_window = 0
    for family, split in families:
        for local_window in range(WINDOWS_PER_FAMILY):
            label = 0 if local_window < LABEL_WINDOWS_PER_FAMILY else 1
            values = _feature_values(family, local_window, label)
            start = base_time + timedelta(seconds=global_window * 10)
            end = start + timedelta(seconds=10)
            scenario = "synthetic-benign" if label == 0 else "synthetic-flood"
            sample_id = f"sample-{family}-{local_window:03d}"
            drafts.append(
                {
                    "sample_id": sample_id,
                    "source_profile": SOURCE_PROFILE,
                    "source_revision": SOURCE_REVISION,
                    "capture_family_id": family,
                    "scenario_id": scenario,
                    "session_id": f"session-{family}-{label}",
                    "topology_id": "bmv2-module-dataset-topology-v1",
                    "window_id": f"window-{global_window:08d}",
                    "window_start": _iso(start),
                    "window_end": _iso(end),
                    "sequence": global_window + 1,
                    "features": values,
                    "label": label,
                    "family": "benign" if label == 0 else "synthetic_flood",
                    "split": split,
                    "sort_key": f"{SOURCE_PROFILE}/{family}/{_iso(start)}/{sample_id}",
                }
            )
            global_window += 1
    drafts.sort(key=lambda item: cast(str, item["sort_key"]).encode("utf-8"))
    return drafts


def _dataset_revision(drafts: list[dict[str, Any]], feature_payload: bytes) -> str:
    identity_rows = [
        {
            "sample_id": draft["sample_id"],
            "capture_family_id": draft["capture_family_id"],
            "window_start": draft["window_start"],
            "split": draft["split"],
            "label": draft["label"],
            "feature_digest": sha256_bytes(feature_tensor_bytes(cast(tuple[int, ...], draft["features"]))),
        }
        for draft in drafts
    ]
    identity = {
        "schema_version": "dataset-p4-window-revision-input/v1",
        "dataset_id": DATASET_ID,
        "source_profile": SOURCE_PROFILE,
        "source_revision": SOURCE_REVISION,
        "family_quotas": FAMILY_QUOTAS,
        "windows_per_family": WINDOWS_PER_FAMILY,
        "feature_file_digest": sha256_bytes(feature_payload),
        "samples": identity_rows,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _sample_record(draft: dict[str, Any], *, dataset_revision: str, offset: int, source_digest: str) -> dict[str, Any]:
    values = cast(tuple[int, ...], draft["features"])
    tensor_digest = sha256_bytes(feature_tensor_bytes(values))
    sequence = _integer_from_draft(draft, "sequence")
    label = _integer_from_draft(draft, "label")
    eligible_packets = values[0]
    join_identity = {
        "source_revision": SOURCE_REVISION,
        "capture_family_id": draft["capture_family_id"],
        "window_id": draft["window_id"],
        "eligible_packets": eligible_packets,
        "label": label,
    }
    snapshot_identity = {
        "target_id": "bmv2-module-dataset-target",
        "source_runtime_epoch": "module-dataset-source-epoch-1",
        "bank": sequence % 2,
        "epoch": 1 + sequence // 256,
        "sequence": sequence,
        "features": list(values),
    }
    return {
        "schema_version": "dataset-p4-window-sample/v1",
        "sample_id": draft["sample_id"],
        "dataset_id": DATASET_ID,
        "dataset_revision": dataset_revision,
        "source_profile": SOURCE_PROFILE,
        "source_revision": SOURCE_REVISION,
        "capture_family_id": draft["capture_family_id"],
        "raw_capture_digest": source_digest,
        "scenario_id": draft["scenario_id"],
        "session_id": draft["session_id"],
        "topology_id": draft["topology_id"],
        "window": {
            "window_id": draft["window_id"],
            "start": draft["window_start"],
            "end": draft["window_end"],
            "duration_ms": 10000,
            "final": True,
            "status": "valid",
            "event_time_basis": "target-aggregate-export-time",
        },
        "snapshot_references": [
            {
                "target_id": "bmv2-module-dataset-target",
                "source_runtime_epoch": "module-dataset-source-epoch-1",
                "bank": sequence % 2,
                "epoch": 1 + sequence // 256,
                "sequence_start": sequence,
                "sequence_end": sequence,
                "snapshot_digest": sha256_bytes(canonical_json_bytes(snapshot_identity)),
                "quality": "valid",
            }
        ],
        "feature_offset": offset,
        "feature_length": 48,
        "feature_tensor_digest": tensor_digest,
        "feature_values": list(values),
        "label": {
            "binary_label": label,
            "family": draft["family"],
            "coverage": 1.0,
            "eligible_packets": eligible_packets,
            "labeled_eligible_packets": eligible_packets,
            "supported_attack_packets": eligible_packets if label == 1 else 0,
            "attack_share": 1.0 if label == 1 else 0.0,
            "purity": "fit_attack" if label == 1 else "fit_benign",
            "join_digest": sha256_bytes(canonical_json_bytes(join_identity)),
        },
        "split": draft["split"],
        "sample_weight": 1.0,
        "quality": {
            "status": "valid",
            "sampling_coverage": 1.0,
            "gap_count": 0,
            "late_after_final": 0,
            "selector_collision_quality": "approximate-hash-qualified",
        },
        "sort_key": draft["sort_key"],
        "raw_payload_included": False,
    }


def _integer_from_draft(draft: dict[str, Any], field: str) -> int:
    value = draft[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("dataset_generator", f"{field} is not integer")
    return value


def verify_reference_extractor_golden(repo: Path) -> dict[str, Any]:
    telemetry_path = resolve_repo_file(repo, "contracts/golden/telemetry/snapshot-v1.json", maximum_bytes=1024 * 1024)
    telemetry = cast(dict[str, Any], load_json(telemetry_path))
    aggregate = cast(dict[str, Any], telemetry["aggregate"])
    cells = cast(list[dict[str, Any]], telemetry["cells"])
    observed = (
        int(aggregate["packets"]),
        int(aggregate["bytes"]),
        int(telemetry["reported_nonzero_cells"]),
        max(int(cell["packets"]) for cell in cells),
        max(int(cell["bytes"]) for cell in cells),
        1,
    )
    expected = (2, 108, 1, 2, 108, 1)
    if observed != expected:
        raise ValidationError("p4_reference_golden", f"{observed} != {expected}")
    if telemetry["quality"] != {"status": "valid", "reasons": ["NONE"]}:
        raise ValidationError("p4_reference_quality", str(telemetry["quality"]))
    source_profile_digest = str(telemetry["source_profile_digest"])
    flow_identity = cast(dict[str, Any], telemetry["flow_identity_profile"])
    sampling = cast(dict[str, Any], telemetry["sampling"])
    if (
        flow_identity["selector_algorithm"] != "p4-qualified-cell-selector/v1"
        or flow_identity["selector_seed_digest"] != source_profile_digest
        or sampling["seed_digest"] != source_profile_digest
    ):
        raise ValidationError("p4_selector_profile", "selector algorithm or seed digest drifted")
    target_id = str(telemetry["target_id"])
    epoch = int(telemetry["epoch"])
    for cell in cells:
        expected_selector = telemetry_cell_selector_digest(
            target_id,
            source_profile_digest,
            epoch,
            int(cell["index"]),
        )
        if cell["selector_digest"] != expected_selector:
            raise ValidationError("p4_selector_digest", f"cell {cell['index']}")

    p4_profile_path = resolve_repo_file(
        repo,
        "contracts/profiles/v1/p4-stateless-firewall-bmv2.json",
        maximum_bytes=1024 * 1024,
    )
    p4_profile = cast(dict[str, Any], load_json(p4_profile_path))
    artifact_digests = cast(dict[str, str], p4_profile["artifact_digests"])
    expected_pipeline = {
        "p4_program_digest": artifact_digests["p4_source"],
        "p4info_digest": artifact_digests["p4info"],
        "pipeline_digest": artifact_digests["device_config_wire"],
    }
    if telemetry["pipeline_identity"] != expected_pipeline:
        raise ValidationError("p4_pipeline_identity", "telemetry golden differs from target profile")
    if sha256_file(repo / "p4/src/masi_switch.p4") != artifact_digests["p4_source"]:
        raise ValidationError("p4_source_digest", "target profile p4_source digest differs from source")
    return {
        "schema_version": "p4-window-reference-golden-result/v1",
        "result": "PASS",
        "telemetry_golden_digest": sha256_file(telemetry_path),
        "feature_tensor_hex": feature_tensor_bytes(expected).hex(),
        "feature_tensor_digest": sha256_bytes(feature_tensor_bytes(expected)),
        "feature_values": list(expected),
        **expected_pipeline,
        "selector_algorithm": "p4-qualified-cell-selector/v1",
        "selector_seed_digest": source_profile_digest,
        "selector_digest_preimage": "target_id\\0source_profile_digest\\0epoch_decimal\\0cell_index_decimal",
    }


def generate_module_dataset(repo: Path, output: Path) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    output = create_fresh_directory(output)
    data_dir = output / "data"
    data_dir.mkdir(mode=0o750)
    reference = verify_reference_extractor_golden(repo)
    families = _select_families()
    drafts = _build_drafts(families)
    feature_payload = b"".join(feature_tensor_bytes(cast(tuple[int, ...], draft["features"])) for draft in drafts)
    revision = _dataset_revision(drafts, feature_payload)
    source_identity = {
        "schema_version": "masi-synthetic-source/v1",
        "source_revision": SOURCE_REVISION,
        "families": [family for family, _ in families],
        "feature_generation": "sha256-counter-derived-no-random-library-state",
        "p4_reference_golden_digest": reference["telemetry_golden_digest"],
    }
    source_digest = sha256_bytes(canonical_json_bytes(source_identity))
    records = [
        _sample_record(draft, dataset_revision=revision, offset=index * 48, source_digest=source_digest)
        for index, draft in enumerate(drafts)
    ]
    index_payload = b"".join(canonical_json_bytes(record) + b"\n" for record in records)
    family_rows = [{"capture_family_id": family, "split": split} for family, split in families]
    split_counts = {name: sum(1 for record in records if record["split"] == name) for name in FAMILY_QUOTAS}
    split_manifest = {
        "schema_version": "dataset-p4-window-split-manifest/v1",
        "source_revision": SOURCE_REVISION,
        "algorithm": "u64be(sha256(masi-split-v1\\0||source_revision||\\0||capture_family_id)[0:8])/2^64",
        "families": family_rows,
        "family_counts": {name: sum(1 for _, split in families if split == name) for name in FAMILY_QUOTAS},
        "sample_counts": split_counts,
        "cross_split_capture_families": 0,
    }
    split_payload = (
        json.dumps(split_manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
        + b"\n"
    )
    write_fresh_bytes(data_dir / "features.bin", feature_payload)
    write_fresh_bytes(data_dir / "samples.jsonl", index_payload)
    write_fresh_bytes(data_dir / "splits.json", split_payload)

    feature_profile_path = repo / "contracts/profiles/v1/edge-feature-window.json"
    profile_path = repo / "contracts/profiles/v1/dataset-p4-window-binary.json"
    label_counts = {
        "benign": sum(1 for record in records if cast(dict[str, Any], record["label"])["binary_label"] == 0),
        "attack": sum(1 for record in records if cast(dict[str, Any], record["label"])["binary_label"] == 1),
        "mixed": 0,
        "ambiguous": 0,
        "unknown": 0,
        "not_covered": 0,
    }
    eligible_packets = sum(int(cast(dict[str, Any], record["label"])["eligible_packets"]) for record in records)
    ground_truth_digest = sha256_bytes(
        canonical_json_bytes(
            [
                {"sample_id": record["sample_id"], "label": record["label"], "window": record["window"]}
                for record in records
            ]
        )
    )
    source_tree_identity = _source_tree_identity(repo)
    manifest = {
        "schema_version": "dataset-p4-window-binary/v1",
        "dataset_id": DATASET_ID,
        "dataset_revision": revision,
        "created_at": "2031-01-01T00:00:00Z",
        "status": {
            "source_selection": "frozen",
            "dataset_revision": "frozen",
            "official_source": "not_applicable_module_fixture",
            "production_quality_scope": "hold",
        },
        "producer": {
            "producer_id": "masi-offline-ml-pipeline",
            "version": "v1",
            "config_digest": sha256_file(profile_path),
            "toolchain_digest": sha256_file(repo / "ml-py/uv.lock"),
            "source_revision": source_tree_identity,
        },
        "feature_contract": {
            "profile_id": "edge-feature-p4-window/v1",
            "profile_digest": sha256_file(feature_profile_path),
            "dtype": "uint64-le",
            "shape": [1, 6],
            "order": list(FEATURE_ORDER),
            "record_bytes": 48,
            "window_duration_ms": 10000,
            "final_required": True,
            "valid_required": True,
            "zero_fill": False,
            "graph_preprocessing": "Cast(float32)->Log1p",
        },
        "source_profiles": [
            {
                "profile_id": SOURCE_PROFILE,
                "decision": "ADOPT",
                "role": "fit_and_evaluate",
                "partition": "project-synthetic",
                "source_revision": SOURCE_REVISION,
                "artifact": {
                    "reference": "repository-fixture://ml-py-module-generated-source",
                    "sha256": source_digest,
                    "bytes": len(feature_payload),
                    "ordinary_file": True,
                    "raw_capture_in_git_or_oci": False,
                },
                "license": {
                    "id": "Apache-2.0",
                    "citation": "MASI-NIDS repository-generated deterministic module fixture",
                    "use": "allowed",
                    "redistribution": "allowed_with_attribution",
                    "verified": True,
                },
                "privacy": {
                    "classification": "synthetic_no_personal_data",
                    "raw_payload_retained": False,
                    "review_digest": sha256_bytes(
                        canonical_json_bytes(
                            {"personal_data": False, "payload": False, "addresses": "documentation-only"}
                        )
                    ),
                    "verified": True,
                },
                "ground_truth": {
                    "method": "project-generated-packet-truth",
                    "manifest_digest": ground_truth_digest,
                    "packet_join_required": True,
                    "verified": True,
                },
                "ingestion_status": "verified",
            }
        ],
        "extractor": {
            "profile_id": "p4-window-reference-extractor/v1",
            "implementation": "qualified-reference-extractor",
            "implementation_digest": sha256_file(repo / "ml-py/src/masi_offline_ml/dataset.py"),
            "p4_golden_digest": reference["telemetry_golden_digest"],
            "p4_program_digest": reference["p4_program_digest"],
            "p4info_digest": reference["p4info_digest"],
            "pipeline_digest": reference["pipeline_digest"],
            "selector_algorithm": reference["selector_algorithm"],
            "selector_seed_digest": reference["selector_seed_digest"],
            "selector_digest_preimage": reference["selector_digest_preimage"],
            "cell_count": 256,
            "bank_count": 2,
            "snapshot_strategy": "dual-bank-sequence-before-after",
            "golden_verified": True,
        },
        "files": {
            "features": {
                "path": "data/features.bin",
                "sha256": sha256_bytes(feature_payload),
                "bytes": len(feature_payload),
                "record_bytes": 48,
                "record_count": len(records),
            },
            "sample_index": {
                "path": "data/samples.jsonl",
                "sha256": sha256_bytes(index_payload),
                "bytes": len(index_payload),
                "record_count": len(records),
                "record_schema": "contracts/dataset/v1/sample-schema.json",
                "encoding": "canonical-json-lines-utf8-lf",
                "max_line_bytes": 16384,
            },
            "split_manifest": {
                "path": "data/splits.json",
                "sha256": sha256_bytes(split_payload),
                "bytes": len(split_payload),
                "capture_family_count": len(families),
            },
        },
        "split_policy": {
            "algorithm": "u64be(sha256(masi-split-v1\\0||source_revision||\\0||capture_family_id)[0:8])/2^64",
            "grouping": "capture-family-only-no-row-window-random-split",
            "intervals": [
                {"split": "train", "lower": 0.0, "upper": 0.6},
                {"split": "early_stop", "lower": 0.6, "upper": 0.75},
                {"split": "calibration", "lower": 0.75, "upper": 0.9},
                {"split": "blind_test", "lower": 0.9, "upper": 1.0},
            ],
            "source_matrix": [
                {
                    "source_partition": "project-synthetic",
                    "train": 0.6,
                    "early_stop": 0.15,
                    "calibration": 0.15,
                    "blind_test": 0.1,
                },
                {
                    "source_partition": "cic-ddos2019-official-training",
                    "train": 0.7,
                    "early_stop": 0.15,
                    "calibration": 0.15,
                    "blind_test": 0.0,
                },
            ],
            "leakage_policy": "reject-any-capture-family-in-more-than-one-split",
            "blind_open_policy": "open-once-after-all-candidates-configs-thresholds-frozen",
            "weighting": "equal-source-label-then-equal-family-then-equal-window-normalized-to-sample-count",
        },
        "label_policy": {
            "taxonomy": "binary-single-label",
            "benign": {"id": 0, "name": "benign", "attack_packet_share": 0.0},
            "attack": {"id": 1, "name": "supported_aggregate_visible_attack"},
            "fit_coverage": 1.0,
            "attack_share_minimum": 0.9,
            "mixed_ambiguous_unknown": "evaluation-only-never-fit",
            "not_covered": ["xss", "sql_injection", "heartbleed_exploit", "ransomware_payload", "content_infiltration"],
            "join": "packet-timestamp-direction-aware-five-tuple-protocol-half-open-window",
        },
        "quality_summary": {
            "sample_count": len(records),
            "capture_family_count": len(families),
            "split_counts": split_counts,
            "label_counts": label_counts,
            "ground_truth": {
                "eligible_packets": eligible_packets,
                "labeled_eligible_packets": eligible_packets,
                "coverage": 1.0,
                "conflicts": 0,
                "coverage_digest": ground_truth_digest,
            },
            "duplicates": 0,
            "cross_split_capture_families": 0,
            "raw_payload_included": False,
        },
        "limits": {
            "max_sources": 16,
            "max_records": 100000000,
            "max_feature_bytes": 4800000000,
            "max_index_bytes": 10737418240,
            "max_capture_families": 10000000,
            "max_archive_files": 100000,
            "max_archive_depth": 8,
            "max_decompression_ratio": 100,
        },
        "compatibility": {
            "unknown_major": "reject",
            "unverified_minor": "reject",
            "feature_major_change": "new-dataset-profile",
            "selector_or_window_change": "new-dataset-profile",
            "legacy_direct_import": "reject",
            "mutable_source": "reject",
            "executable_content": "reject",
        },
    }
    validate_dataset_manifest(cast(dict[str, Any], manifest))
    write_fresh_json(output / "manifest.json", manifest)
    write_fresh_json(output / "reference-extractor-golden.json", reference)
    return cast(dict[str, Any], manifest)


def _schema_validate(schema: dict[str, Any], document: object, code: str) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(cast(Any, document)),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = "/" + "/".join(str(part) for part in first.absolute_path)
        raise ValidationError(code, f"{location}: {first.message}")


def load_dataset(repo: Path, dataset_directory: Path) -> DatasetData:
    repo = repo.resolve(strict=True)
    dataset_directory = dataset_directory.resolve(strict=True)
    manifest_path = dataset_directory / "manifest.json"
    manifest = cast(dict[str, Any], load_json(manifest_path, maximum_bytes=4 * 1024 * 1024))
    manifest_schema = cast(dict[str, Any], load_json(repo / "contracts/dataset/v1/schema.json"))
    sample_schema = cast(dict[str, Any], load_json(repo / "contracts/dataset/v1/sample-schema.json"))
    _schema_validate(manifest_schema, manifest, "dataset_schema")
    validate_dataset_manifest(manifest)
    files = cast(dict[str, dict[str, Any]], manifest["files"])

    paths: dict[str, Path] = {}
    for name in ("features", "sample_index", "split_manifest"):
        relative = str(files[name]["path"])
        candidate = dataset_directory / relative
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise ValidationError("dataset_file_missing", relative) from error
        if dataset_directory not in resolved.parents or resolved.is_symlink() or not resolved.is_file():
            raise ValidationError("dataset_file_path", relative)
        if sha256_file(resolved) != files[name]["sha256"] or resolved.stat().st_size != files[name]["bytes"]:
            raise ValidationError("dataset_file_digest", relative)
        paths[name] = resolved

    feature_payload = paths["features"].read_bytes()
    if len(feature_payload) % 48:
        raise ValidationError("feature_file_alignment", str(len(feature_payload)))
    features = np.frombuffer(feature_payload, dtype="<u8").reshape((-1, 6)).copy()
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    family_splits: dict[str, str] = {}
    with paths["sample_index"].open("rb") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            if len(raw_line) > 16384 or not raw_line.endswith(b"\n"):
                raise ValidationError("sample_index_line", str(line_number))
            try:
                record = cast(dict[str, Any], json.loads(raw_line))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValidationError("sample_index_json", str(line_number)) from error
            if canonical_json_bytes(record) + b"\n" != raw_line:
                raise ValidationError("sample_index_canonical", str(line_number))
            _schema_validate(sample_schema, record, "dataset_sample_schema")
            validate_dataset_sample(record)
            if (
                record["dataset_id"] != manifest["dataset_id"]
                or record["dataset_revision"] != manifest["dataset_revision"]
            ):
                raise ValidationError("dataset_sample_identity", str(line_number))
            sample_id = str(record["sample_id"])
            if sample_id in seen_ids:
                raise ValidationError("duplicate_sample", sample_id)
            seen_ids.add(sample_id)
            family = str(record["capture_family_id"])
            split = str(record["split"])
            prior = family_splits.setdefault(family, split)
            if prior != split:
                raise ValidationError("capture_family_cross_split", family)
            offset = int(record["feature_offset"])
            if offset != (line_number - 1) * 48:
                raise ValidationError("feature_offset_sequence", str(line_number))
            if feature_payload[offset : offset + 48] != feature_tensor_bytes(cast(list[int], record["feature_values"])):
                raise ValidationError("feature_payload_mismatch", sample_id)
            records.append(record)
    if len(records) != features.shape[0]:
        raise ValidationError("dataset_record_count", f"{len(records)} != {features.shape[0]}")
    sort_keys = [str(record["sort_key"]) for record in records]
    if sort_keys != sorted(sort_keys, key=lambda value: value.encode("utf-8")):
        raise ValidationError("dataset_sort_order", "sample index is not bytewise sorted")
    labels = np.asarray(
        [int(cast(dict[str, Any], record["label"])["binary_label"]) for record in records], dtype=np.int64
    )
    weights = np.asarray([float(record["sample_weight"]) for record in records], dtype=np.float64)
    splits = np.asarray([str(record["split"]) for record in records], dtype=np.str_)
    return DatasetData(
        manifest=manifest,
        records=tuple(records),
        features=features,
        labels=labels,
        weights=weights,
        splits=splits,
    )


def reject_legacy_direct_import(path: Path) -> None:
    normalized = path.absolute().as_posix().rstrip("/")
    if normalized == "/home/lmhhh/MASI-NIDS/dataset" or normalized.startswith("/home/lmhhh/MASI-NIDS/dataset/"):
        raise ValidationError("legacy_direct_import", normalized)
