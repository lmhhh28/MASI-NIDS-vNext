"""Atomic end-to-end Offline ML artifact pipeline and output verification."""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from .artifacts import (
    build_winner_bundle,
    deterministic_bundle_tar,
    finalize_candidate_metadata,
    select_winner,
    write_candidate_artifacts,
)
from .canonical import (
    ValidationError,
    canonical_json_bytes,
    ensure_finite,
    load_json,
    sha256_bytes,
    sha256_file,
    write_fresh_json,
)
from .contracts import validate_public_contracts
from .dataset import DatasetData, generate_module_dataset, load_dataset
from .explanations import build_explanations
from .models import TrainedCandidate, train_autoencoder, train_logistic, train_xgboost
from .onnx_export import inspect_model, run_ort

SEEDS = (17, 29, 43)
TRAINERS = (train_logistic, train_xgboost, train_autoencoder)
_STAGING_OWNER = ".offline-ml-staging-owner.json"


def _candidate_directory_name(candidate_id: str) -> str:
    normalized = candidate_id.replace("/", "-")
    if not normalized or normalized in (".", "..") or "/" in normalized or "\\" in normalized:
        raise ValidationError("candidate_directory", candidate_id)
    return normalized


def _base_metadata(dataset: DatasetData) -> dict[str, str]:
    return {
        "dataset_id": str(dataset.manifest["dataset_id"]),
        "dataset_revision": str(dataset.manifest["dataset_revision"]),
    }


def train_mandatory_candidates(dataset: DatasetData) -> list[TrainedCandidate]:
    """Train every mandatory recipe and seed; no candidate may be skipped."""

    candidates: list[TrainedCandidate] = []
    for trainer in TRAINERS:
        for seed in SEEDS:
            trained = trainer(dataset, seed, _base_metadata(dataset))
            candidates.append(finalize_candidate_metadata(trained, dataset))
    if len(candidates) != len(TRAINERS) * len(SEEDS):
        raise ValidationError("mandatory_candidate_count", str(len(candidates)))
    return candidates


def _safe_remove_staging(staging: Path) -> None:
    """Remove only the exact private staging tree, including read-only repository members."""

    if not staging.exists() or staging.is_symlink():
        return
    for root, directories, files in os.walk(staging, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            path = root_path / name
            if path.is_symlink():
                path.unlink()
            else:
                os.chmod(path, 0o600)
                path.unlink()
        for name in directories:
            path = root_path / name
            if path.is_symlink():
                path.unlink()
            else:
                os.chmod(path, 0o700)
                path.rmdir()
    os.chmod(staging, 0o700)
    staging.rmdir()


def _process_start_ticks(pid: int) -> str | None:
    try:
        payload = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
        return None
    closing_parenthesis = payload.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields = payload[closing_parenthesis + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def _cleanup_stale_staging(output: Path) -> int:
    """Recover only abandoned staging directories carrying our exact ownership marker."""

    removed = 0
    for staging in output.parent.glob(f".{output.name}.staging-*"):
        if staging.is_symlink() or not staging.is_dir():
            continue
        owner_path = staging / _STAGING_OWNER
        try:
            owner = cast(dict[str, Any], load_json(owner_path, maximum_bytes=4096))
            pid = int(owner["pid"])
            expected_ticks = str(owner["process_start_ticks"])
            expected_output = str(owner["output"])
        except (ValidationError, KeyError, TypeError, ValueError):
            continue
        observed_ticks = _process_start_ticks(pid)
        owner_is_live = (
            observed_ticks == expected_ticks if expected_ticks != "unavailable" else Path(f"/proc/{pid}").is_dir()
        )
        if (
            owner.get("schema_version") != "offline-ml-staging-owner/v1"
            or expected_output != str(output)
            or owner_is_live
        ):
            continue
        _safe_remove_staging(staging)
        removed += 1
    return removed


def _require_fresh_output(output: Path) -> Path:
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValidationError("output_directory_exists", str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = output.parent.resolve(strict=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValidationError("output_parent", str(parent))
    return output


def run_pipeline(repo: Path, output: Path) -> dict[str, Any]:
    """Run the real nine-candidate pipeline and atomically publish its output."""

    repo = repo.resolve(strict=True)
    output = _require_fresh_output(output)
    validate_public_contracts(repo)
    recovered_staging_directories = _cleanup_stale_staging(output)
    started = time.monotonic_ns()
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        process_start_ticks = _process_start_ticks(os.getpid()) or "unavailable"
        write_fresh_json(
            staging / _STAGING_OWNER,
            {
                "schema_version": "offline-ml-staging-owner/v1",
                "pid": os.getpid(),
                "process_start_ticks": process_start_ticks,
                "output": str(output),
            },
        )
        dataset_directory = staging / "dataset"
        generate_module_dataset(repo, dataset_directory)
        dataset = load_dataset(repo, dataset_directory)
        candidates = train_mandatory_candidates(dataset)
        explanations = build_explanations(repo, dataset, candidates)

        candidate_manifests: dict[tuple[str, int], dict[str, Any]] = {}
        for candidate in sorted(candidates, key=lambda value: (value.candidate_id, value.seed)):
            key = (candidate.candidate_id, candidate.seed)
            candidate_directory = (
                staging / "candidates" / _candidate_directory_name(candidate.candidate_id) / f"seed-{candidate.seed}"
            )
            candidate_manifests[key] = write_candidate_artifacts(
                candidate_directory,
                candidate,
                dataset,
                explanations[key],
            )

        selection = select_winner(candidates)
        if selection.get("result") != "PASS" or selection.get("winner") == "none":
            raise ValidationError("no_qualified_winner", str(selection.get("reason", "unknown")))
        winner = next(
            candidate
            for candidate in candidates
            if candidate.candidate_id == selection["winner"] and candidate.seed == selection["winner_seed"]
        )
        winner_key = (winner.candidate_id, winner.seed)
        bundle = build_winner_bundle(
            repo,
            staging / "bundle",
            winner,
            selection,
            dataset,
            explanations[winner_key],
            candidate_manifests,
        )
        archive = deterministic_bundle_tar(
            staging / "bundle" / "repository",
            staging / "model-repository.tar",
        )
        result = {
            "schema_version": "offline-ml-pipeline-result/v1",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "qualification_reason": "MODULE_FIXTURE_DOES_NOT_REPLACE_OFFICIAL_CORPUS_OR_PRODUCTION_BASELINE",
            "dataset_id": dataset.manifest["dataset_id"],
            "dataset_revision": dataset.manifest["dataset_revision"],
            "candidate_executions": len(candidates),
            "candidate_recipes": len(TRAINERS),
            "seeds": list(SEEDS),
            "winner": selection["winner"],
            "winner_seed": selection["winner_seed"],
            "bundle": bundle,
            "archive": archive,
            "runtime": {
                "duration_ms": (time.monotonic_ns() - started) / 1_000_000,
                "recovered_staging_directories": recovered_staging_directories,
                "network_required": False,
                "postgresql_access": False,
                "p4runtime_access": False,
                "effect_authority": False,
            },
        }
        ensure_finite(result)
        write_fresh_json(staging / "pipeline-result.json", result)
        (staging / _STAGING_OWNER).unlink()
        os.replace(staging, output)
        return result
    except BaseException:
        _safe_remove_staging(staging)
        raise


def _ordinary_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValidationError("output_symlink", relative)
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            if mode & 0o111:
                raise ValidationError("output_executable", relative)
            files[relative] = path
        elif not stat.S_ISDIR(mode):
            raise ValidationError("output_special_file", relative)
    return files


def _verify_candidate_artifacts(root: Path) -> int:
    manifests = sorted(root.glob("*/seed-*/candidate-manifest.json"))
    if len(manifests) != 9:
        raise ValidationError("candidate_manifest_count", str(len(manifests)))
    identities: set[tuple[str, int]] = set()
    for manifest_path in manifests:
        manifest = cast(dict[str, Any], load_json(manifest_path))
        identity = (str(manifest["candidate_id"]), int(manifest["seed"]))
        if identity in identities:
            raise ValidationError("candidate_identity_duplicate", f"{identity[0]}:{identity[1]}")
        identities.add(identity)
        directory = manifest_path.parent
        files = cast(dict[str, dict[str, Any]], manifest["files"])
        for name, expected in files.items():
            path = directory / name
            if path.parent != directory or sha256_file(path) != expected["sha256"]:
                raise ValidationError("candidate_file_digest", f"{identity}:{name}")
            if path.stat().st_size != int(expected["bytes"]):
                raise ValidationError("candidate_file_size", f"{identity}:{name}")
        if not bool(manifest["eligible"]):
            raise ValidationError("candidate_not_eligible", f"{identity[0]}:{identity[1]}")
    return len(identities)


def _verify_repository(repository: Path, dataset: DatasetData) -> dict[str, Any]:
    if repository.stat().st_mode & 0o222:
        raise ValidationError("repository_writable", str(repository))
    files = _ordinary_files(repository)
    for path in files.values():
        if path.stat().st_mode & 0o222:
            raise ValidationError("repository_member_writable", str(path.relative_to(repository)))
    closure = cast(dict[str, Any], load_json(repository / "closure-manifest.json"))
    members = cast(list[dict[str, Any]], closure["members"])
    member_paths: set[str] = set()
    closure_lines: list[str] = []
    allowed_roles = {"model", "config", "version", "backend", "bundle-manifest"}
    for member in members:
        role = str(member["role"])
        relative = str(member["rel_path"])
        if role not in allowed_roles or relative in member_paths:
            raise ValidationError("repository_member", f"{role}:{relative}")
        path = repository / relative
        if path.resolve(strict=True).parent == repository.parent or repository not in path.resolve(strict=True).parents:
            raise ValidationError("repository_member_path", relative)
        observed = sha256_file(path)
        if observed != member["member_digest"]:
            raise ValidationError("repository_member_digest", relative)
        member_paths.add(relative)
        closure_lines.append(f"{role}\t{relative}\t{observed}")
    expected_files = member_paths | {"closure-manifest.json"}
    if set(files) != expected_files:
        raise ValidationError("repository_closure", f"{sorted(set(files) ^ expected_files)}")
    observed_closure = sha256_bytes(("\n".join(sorted(closure_lines)) + "\n").encode("utf-8"))
    if observed_closure != closure["closure_digest"]:
        raise ValidationError("repository_closure_digest", observed_closure)

    manifest = cast(dict[str, Any], load_json(repository / "bundle-manifest.json"))
    model_path = next(repository / relative for relative in member_paths if relative.endswith("/model.onnx"))
    model_digest = sha256_file(model_path)
    if model_digest != manifest["model_digest"] or model_digest != manifest["binding_identity"]["model_digest"]:
        raise ValidationError("repository_model_digest", model_digest)
    identity_digest = sha256_bytes(canonical_json_bytes(manifest["binding_identity"]))
    if identity_digest != manifest["model_revision_digest"]:
        raise ValidationError("model_revision_digest", identity_digest)
    evidence = inspect_model(model_path.read_bytes())
    blind = dataset.indices("blind_test")[:64]
    scores = run_ort(model_path.read_bytes(), dataset.features[blind])
    if scores.shape != (blind.size, 2):
        raise ValidationError("repository_model_output_shape", str(scores.shape))
    return {
        "identity": closure["identity"],
        "closure_digest": observed_closure,
        "model_digest": model_digest,
        "operator_count": len(cast(list[dict[str, str]], evidence["operators"])),
        "numeric_records": int(blind.size),
    }


def verify_repository_archive(path: Path, expected_digest: str | None = None) -> dict[str, Any]:
    """Validate the immutable repository tar without extracting or following links."""

    path = path.resolve(strict=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024 * 1024:
        raise ValidationError("repository_archive_file", str(path))
    observed_digest = sha256_file(path)
    if expected_digest is not None and observed_digest != expected_digest:
        raise ValidationError("repository_archive_digest", observed_digest)
    names: list[str] = []
    total_bytes = 0
    try:
        with tarfile.open(path, mode="r:") as archive:
            members = archive.getmembers()
            if not 1 <= len(members) <= 128:
                raise ValidationError("repository_archive_member_count", str(len(members)))
            for member in members:
                member_path = Path(member.name)
                if (
                    member.name.startswith("/")
                    or "\\" in member.name
                    or any(part in ("", ".", "..") for part in member_path.parts)
                    or len(member_path.parts) > 8
                ):
                    raise ValidationError("repository_archive_path", member.name)
                if not member.isfile() or member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ValidationError("repository_archive_type", member.name)
                if member.mode != 0o440 or member.uid != 0 or member.gid != 0 or member.mtime != 1787334400:
                    raise ValidationError("repository_archive_metadata", member.name)
                if member.size > 512 * 1024 * 1024:
                    raise ValidationError("repository_archive_member_size", member.name)
                total_bytes += member.size
                if total_bytes > 1024 * 1024 * 1024:
                    raise ValidationError("repository_archive_total_size", str(total_bytes))
                names.append(member.name)
    except (tarfile.TarError, OSError) as error:
        raise ValidationError("repository_archive_format", str(error)) from error
    expected_order = sorted(names, key=lambda name: name.encode("utf-8"))
    if names != expected_order or len(names) != len(set(names)):
        raise ValidationError("repository_archive_order", str(names))
    return {
        "sha256": observed_digest,
        "members": len(names),
        "payload_bytes": total_bytes,
        "paths": names,
    }


def verify_pipeline_output(repo: Path, output: Path) -> dict[str, Any]:
    """Verify a published output without trusting its result document."""

    repo = repo.resolve(strict=True)
    output = output.resolve(strict=True)
    if output.is_symlink() or not output.is_dir():
        raise ValidationError("pipeline_output", str(output))
    _ordinary_files(output)
    result = cast(dict[str, Any], load_json(output / "pipeline-result.json"))
    ensure_finite(result)
    if result.get("result") != "PASS" or result.get("candidate_executions") != 9:
        raise ValidationError("pipeline_result", str(result.get("result")))
    dataset = load_dataset(repo, output / "dataset")
    candidates = _verify_candidate_artifacts(output / "candidates")
    repository = _verify_repository(output / "bundle" / "repository", dataset)
    archive = cast(dict[str, Any], result["archive"])
    archive_path = output / str(archive["path"])
    archive_verification = verify_repository_archive(archive_path, str(archive["sha256"]))
    if archive_path.stat().st_size != int(archive["bytes"]):
        raise ValidationError("repository_archive", str(archive_path))
    bundle = cast(dict[str, Any], result["bundle"])
    if repository["closure_digest"] != bundle["repository_closure_digest"]:
        raise ValidationError("bundle_closure_binding", str(repository["closure_digest"]))
    verification = {
        "schema_version": "offline-ml-pipeline-verification/v1",
        "result": "PASS",
        "dataset_revision": dataset.manifest["dataset_revision"],
        "candidate_executions": candidates,
        "repository": repository,
        "archive": archive_verification,
        "archive_digest": archive["sha256"],
    }
    ensure_finite(verification)
    return verification
