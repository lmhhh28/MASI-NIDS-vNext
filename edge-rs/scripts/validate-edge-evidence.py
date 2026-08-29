#!/usr/bin/env python3
"""Derive and validate Rust Edge module-completion evidence.

Module completion is an operational implementation decision (DEC-044).  It is
deliberately independent from release or production qualification: a dirty tree,
an absent protected tag, or an Owner-unfrozen absolute performance threshold can
keep qualification on HOLD without hiding a successful real startup/test result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker


MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_SOURCE_ARCHIVE_BYTES = 1024 * 1024 * 1024
COMPLETION_CRITERIA = (
    "public_contracts",
    "format_lint_static",
    "unit_property_contract",
    "release_binary",
    "real_binary_public_boundary",
    "os_fault_recovery",
    "real_oci_startup",
    "deep_runtime_checks",
    "supply_security_checks",
    "formal_soak",
    "traceability",
    "known_p0_zero",
)
COMPLETION_EVIDENCE = {
    "public_contracts": "public-contracts.command.json",
    "rustc_version": "rustc-version.command.json",
    "cargo_version": "cargo-version.command.json",
    "format": "format.command.json",
    "clippy": "clippy.command.json",
    "tests": "tests.command.json",
    "blackbox": "module-blackbox/module-blackbox-e2e.json",
    "release_build": "release-build.command.json",
    "os_faults": "os-faults.command.json",
    "oci": "oci-smoke/oci-smoke-evidence.json",
    "deep": "deep-checks/deep-check-summary.json",
    "supply": "supply-chain/supply-chain.json",
    "formal_soak": "formal-soak/soak-evidence.json",
    "formal_soak_validation": "formal-soak-validation.command.json",
    "traceability": "traceability-summary.json",
}


class EvidenceError(ValueError):
    """Raised for an invalid or unsafe evidence object."""


def reject_constant(value: str) -> object:
    raise EvidenceError(f"non-finite JSON number {value!r} is forbidden")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError(f"duplicate JSON member {key!r} is forbidden")
        value[key] = item
    return value


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"{path} is not a direct regular file")
    size = path.stat().st_size
    if size > MAX_JSON_BYTES:
        raise EvidenceError(f"{path} exceeds the {MAX_JSON_BYTES}-byte JSON limit")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if not isinstance(value, dict):
        raise EvidenceError(f"{path} is not a JSON object")
    return value


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def direct_run_file(run_root: Path, relative: str) -> Path | None:
    """Resolve one bounded relative evidence path without crossing a symlink."""

    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        return None
    try:
        root = run_root.resolve(strict=True)
    except OSError:
        return None
    if run_root.is_symlink() or not root.is_dir():
        return None
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved = current.resolve(strict=True)
    except OSError:
        return None
    if not inside(resolved, root) or not resolved.is_file():
        return None
    return resolved


def schema_failures(schema: dict[str, Any], value: object) -> list[str]:
    return [
        f"{list(error.path)}: {error.message}"
        for error in sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
                value
            ),
            key=lambda item: list(item.path),
        )
    ]


def safe_schema(repo: Path, relative: str) -> dict[str, Any] | None:
    try:
        return load(repo / relative)
    except (OSError, UnicodeError, json.JSONDecodeError, EvidenceError):
        return None


def digest_if_direct(run_root: Path, relative: str) -> str | None:
    path = direct_run_file(run_root, relative)
    return sha256(path) if path is not None else None


def current_source_tree_digest(repo: Path) -> str:
    """Rebuild the runner's deterministic source snapshot from the checked-out tree."""

    with tempfile.TemporaryDirectory(prefix="masi-edge-source-verify-") as directory:
        archive = Path(directory) / "source-tree.tar"
        command = [
            "tar",
            "--sort=name",
            "--mtime=@1786406400",
            "--owner=0",
            "--group=0",
            "--numeric-owner",
            "--exclude=edge-rs/target",
            "--exclude=edge-rs/evidence",
            "--exclude=**/node_modules",
            "--exclude=**/__pycache__",
            "--exclude=*.pyc",
            "-cf",
            str(archive),
            "-C",
            str(repo),
            "edge-rs",
            "contracts",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EvidenceError(
                f"cannot rebuild the source-tree snapshot: {error}"
            ) from error
        if result.returncode != 0 or not archive.is_file():
            detail = result.stderr.strip()[-1_000:]
            raise EvidenceError(f"cannot rebuild the source-tree snapshot: {detail}")
        if not 0 < archive.stat().st_size <= MAX_SOURCE_ARCHIVE_BYTES:
            raise EvidenceError(
                "rebuilt source-tree snapshot exceeds its resource bound"
            )
        return sha256(archive)


def git_identity(repo: Path) -> tuple[str, str]:
    values: list[str] = []
    for revision in ("HEAD", "HEAD^{tree}"):
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", revision],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EvidenceError(
                f"cannot resolve the source revision: {error}"
            ) from error
        value = result.stdout.strip()
        if result.returncode != 0 or len(value) != 40:
            raise EvidenceError("cannot resolve the exact Git source identity")
        values.append(value)
    return values[0], values[1]


def validate_run_snapshot(
    repo: Path,
    run_root: Path,
    source_tree_digest: str,
    working_tree_status_digest: str,
    working_tree_dirty: bool,
) -> None:
    status_file = direct_run_file(run_root, "working-tree-status.txt")
    if status_file is None or working_tree_status_digest != sha256(status_file):
        raise EvidenceError("working_tree_status_digest does not bind the run snapshot")
    status_bytes = status_file.read_bytes()
    if not status_bytes.endswith(b"\n"):
        raise EvidenceError(
            "working-tree status snapshot is not canonically terminated"
        )
    derived_dirty = status_bytes != b"\n"
    if working_tree_dirty is not derived_dirty:
        raise EvidenceError("working_tree_dirty does not match the run status snapshot")
    if source_tree_digest != current_source_tree_digest(repo):
        raise EvidenceError(
            "source_tree_digest does not bind the checked-out source tree"
        )


class CompletionDeriver:
    """Recompute completion from direct evidence, never from a claimed flag."""

    def __init__(
        self,
        repo: Path,
        run_root: Path,
        run_id: str,
        source_tree_digest: str,
        working_tree_status_digest: str,
        working_tree_dirty: bool,
    ) -> None:
        self.repo = repo
        self.run_root = run_root
        self.run_id = run_id
        self.source_tree_digest = source_tree_digest
        self.working_tree_status_digest = working_tree_status_digest
        self.working_tree_dirty = working_tree_dirty
        self.command_schema = safe_schema(
            repo, "contracts/evidence/command/v1/schema.json"
        )
        self.blackbox_schema = safe_schema(
            repo, "contracts/evidence/edge-blackbox/v1/schema.json"
        )
        self.edge_module_schema = safe_schema(
            repo, "contracts/evidence/v1/edge-module-schema.json"
        )
        self.findings_schema = safe_schema(
            repo, "contracts/evidence/module-findings/v1/schema.json"
        )
        self.traceability_schema = safe_schema(
            repo, "contracts/evidence/traceability/v1/schema.json"
        )
        self._command_cache: dict[str, tuple[str, dict[str, Any] | None]] = {}

    def command(self, command_id: str) -> tuple[str, dict[str, Any] | None]:
        cached = self._command_cache.get(command_id)
        if cached is not None:
            return cached
        path = direct_run_file(self.run_root, f"{command_id}.command.json")
        if path is None or self.command_schema is None:
            result = ("FAIL", None)
            self._command_cache[command_id] = result
            return result
        try:
            value = load(path)
        except (OSError, UnicodeError, json.JSONDecodeError, EvidenceError):
            result = ("FAIL", None)
            self._command_cache[command_id] = result
            return result
        if schema_failures(self.command_schema, value):
            result = ("FAIL", value)
            self._command_cache[command_id] = result
            return result
        log = value.get("log")
        log_path = direct_run_file(
            self.run_root,
            log.get("path", "") if isinstance(log, dict) else "",
        )
        consistent = (
            value.get("run_id") == self.run_id
            and value.get("command_id") == command_id
            and value.get("source_tree_digest") == self.source_tree_digest
            and value.get("working_tree_status_digest")
            == self.working_tree_status_digest
            and log_path is not None
            and isinstance(log, dict)
            and log.get("sha256") == sha256(log_path)
            and log.get("bytes") == log_path.stat().st_size
            and log.get("media_type") == "text/plain"
        )
        state = value.get("result") if consistent else "FAIL"
        if state not in {"PASS", "HOLD", "FAIL", "NOT_RUN"}:
            state = "FAIL"
        result = (state, value)
        self._command_cache[command_id] = result
        return result

    def json_evidence(
        self,
        relative: str,
        schema: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, Path | None]:
        path = direct_run_file(self.run_root, relative)
        if path is None or schema is None:
            return None, path
        try:
            value = load(path)
        except (OSError, UnicodeError, json.JSONDecodeError, EvidenceError):
            return None, path
        if schema_failures(schema, value):
            return None, path
        return value, path

    @staticmethod
    def combine(states: list[str]) -> str:
        if any(state == "FAIL" for state in states):
            return "FAIL"
        if any(state == "NOT_RUN" for state in states):
            return "NOT_RUN"
        if all(state == "PASS" for state in states):
            return "PASS"
        return "FAIL"

    def direct_command_group(self, *command_ids: str) -> str:
        return self.combine([self.command(command_id)[0] for command_id in command_ids])

    def blackbox(self) -> str:
        command_state, _ = self.command("tests")
        if command_state != "PASS":
            return command_state if command_state == "NOT_RUN" else "FAIL"
        value, _ = self.json_evidence(
            "module-blackbox/module-blackbox-e2e.json", self.blackbox_schema
        )
        if value is None:
            return "FAIL"
        exact = (
            value.get("schema_version") == "edge-module-e2e-evidence/v1"
            and value.get("test_id") == "TEST-EDGE-MODULE-E2E-001"
            and value.get("level") == "MODULE"
            and value.get("result") == "PASS"
            and value.get("qualification") == "QUALIFIED"
            and value.get("edge_process") == "real Cargo-built masi-edge OS process"
            and value.get("neighbor_boundary") == "deterministic independent mTLS fakes"
            and value.get("target_count") == 2
            and value.get("maximum_active_streams_per_device") == 1
            and value.get("central_retry_identity_stable") is True
            and value.get("canonical_commit_retry_digest_stable") is True
            and value.get("same_generation_equivalent_worker_retry") is True
            and value.get("effect_expected_entries") == 128
            and value.get("effect_observed_entries") == 128
        )
        return "PASS" if exact else "FAIL"

    def os_faults(self) -> str:
        command_state, _ = self.command("os-faults")
        if command_state != "PASS":
            return command_state if command_state == "NOT_RUN" else "FAIL"
        enospc, _ = self.json_evidence(
            "module-blackbox/os-enospc.json", self.blackbox_schema
        )
        fd_exhaustion, _ = self.json_evidence(
            "module-blackbox/os-fd-exhaustion.json", self.blackbox_schema
        )
        if enospc is None or fd_exhaustion is None:
            return "FAIL"
        enospc_pass = (
            enospc.get("test_id") == "TEST-EDGE-OS-ENOSPC-001"
            and enospc.get("level") == "MODULE"
            and enospc.get("result") == "PASS"
            and enospc.get("qualification") == "QUALIFIED"
            and enospc.get("errno") == 28
            and enospc.get("process_live") is True
            and enospc.get("reason_code") == "LOCAL_IO"
            and enospc.get("unacknowledged_overwrite") is False
        )
        fd_pass = (
            fd_exhaustion.get("test_id") == "TEST-EDGE-OS-FD-EXHAUSTION-001"
            and fd_exhaustion.get("level") == "MODULE"
            and fd_exhaustion.get("result") == "PASS"
            and fd_exhaustion.get("qualification") == "QUALIFIED"
            and fd_exhaustion.get("configured_nofile") == 128
            and fd_exhaustion.get("process_live") is True
            and fd_exhaustion.get("reason_code") == "LOCAL_IO"
            and fd_exhaustion.get("target_profile_overcommitted") is False
            and isinstance(fd_exhaustion.get("admitted_targets_before_exhaustion"), int)
            and fd_exhaustion.get("admitted_targets_before_exhaustion", 0) > 0
        )
        return "PASS" if enospc_pass and fd_pass else "FAIL"

    def qualification_hold_evidence(
        self,
        command_id: str,
        relative: str,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> str:
        command_state, _ = self.command(command_id)
        if command_state == "NOT_RUN":
            return "NOT_RUN"
        if command_state not in {"PASS", "HOLD"}:
            return "FAIL"
        value, _ = self.json_evidence(relative, self.edge_module_schema)
        if value is None:
            return "FAIL"
        common = (
            value.get("result") == command_state
            and value.get("source_tree_digest") == self.source_tree_digest
            and value.get("working_tree_status_digest")
            == self.working_tree_status_digest
            and value.get("working_tree_dirty") is self.working_tree_dirty
        )
        return "PASS" if common and predicate(value) else "FAIL"

    @staticmethod
    def oci_predicate(value: dict[str, Any]) -> bool:
        checks = value.get("checks")
        probe = value.get("probe")
        archive = value.get("archive")
        return (
            value.get("schema_version") == "edge-oci-startup-evidence/v1"
            and value.get("result") in {"PASS", "HOLD"}
            and isinstance(checks, dict)
            and len(checks) == 10
            and all(item == "PASS" for item in checks.values())
            and isinstance(probe, dict)
            and probe.get("result") == "PASS"
            and probe.get("process_live") is True
            and probe.get("accepting_assignments") is True
            and probe.get("rpc_method") == "masi.edge.v1.EdgeControl/GetStatus"
            and probe.get("config_digest") == value.get("config_digest")
            and value.get("read_only_rootfs") is True
            and value.get("graceful_shutdown_exit_code") == 0
            and isinstance(archive, dict)
            and archive.get("manifest_digest") == value.get("image_manifest_digest")
            and archive.get("image_manifest_descriptor_digest")
            == value.get("image_manifest_digest")
            and archive.get("config_digest") == value.get("image_config_digest")
            and archive.get("binary_digest") == value.get("image_binary_digest")
        )

    @staticmethod
    def deep_predicate(value: dict[str, Any]) -> bool:
        checks = value.get("checks")
        return (
            value.get("schema_version") == "edge-deep-check-evidence/v1"
            and value.get("result") in {"PASS", "HOLD"}
            and isinstance(checks, dict)
            and checks
            == {
                "miri_disable_isolation": "PASS",
                "address_sanitizer": "PASS",
                "thread_sanitizer": "PASS",
            }
        )

    @staticmethod
    def all_true(value: object) -> bool:
        if not isinstance(value, dict) or not value:
            return False
        for item in value.values():
            if isinstance(item, dict):
                if not CompletionDeriver.all_true(item):
                    return False
            elif item is not True:
                return False
        return True

    @staticmethod
    def supply_predicate(value: dict[str, Any]) -> bool:
        negative = value.get("negative_verification")
        findings = (
            value.get("trivy", {}).get("findings")
            if isinstance(value.get("trivy"), dict)
            else None
        )
        required_empty = (
            "critical",
            "fixable_high",
            "misconfig_high_critical",
            "secrets",
        )
        return (
            value.get("schema_version") == "rust-edge-agent-supply-verification/v1"
            and value.get("result") in {"PASS", "HOLD"}
            and CompletionDeriver.all_true(value.get("checks"))
            and value.get("failure_reasons") == []
            and isinstance(negative, dict)
            and isinstance(negative.get("mutated_exit_code"), int)
            and negative.get("mutated_exit_code", 0) > 0
            and isinstance(negative.get("wrong_publisher_exit_code"), int)
            and negative.get("wrong_publisher_exit_code", 0) > 0
            and isinstance(findings, dict)
            and all(findings.get(name) == [] for name in required_empty)
        )

    def formal_soak(self) -> str:
        execution, _ = self.command("formal-soak")
        validation, _ = self.command("formal-soak-validation")
        if execution == "NOT_RUN" or validation == "NOT_RUN":
            return "NOT_RUN"
        if execution != "PASS" or validation != "PASS":
            return "FAIL"
        evidence = direct_run_file(self.run_root, "formal-soak/soak-evidence.json")
        if evidence is None:
            return "FAIL"
        validator = self.repo / "edge-rs/scripts/validate-soak-evidence.py"
        if validator.is_symlink() or not validator.is_file():
            return "FAIL"
        try:
            checked = subprocess.run(
                [
                    sys.executable,
                    str(validator),
                    "--repo",
                    str(self.repo),
                    "--evidence",
                    str(evidence),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "FAIL"
        if checked.returncode != 0:
            return "FAIL"
        try:
            value = load(evidence)
        except (OSError, UnicodeError, json.JSONDecodeError, EvidenceError):
            return "FAIL"
        summary = value.get("summary")
        cleanup = value.get("cleanup")
        exact = (
            value.get("module") == "rust-edge-agent"
            and value.get("level") == "MODULE"
            and value.get("result") in {"PASS", "HOLD"}
            and value.get("qualified_elapsed_ms", 0) >= 3_600_000
            and value.get("interruption") == "NONE"
            and isinstance(summary, dict)
            and all(
                summary.get(name) == 0
                for name in (
                    "error_count",
                    "unclassified_gap_count",
                    "oom_events",
                    "container_restarts",
                    "oracle_mismatches",
                    "resource_limit_violations",
                )
            )
            and isinstance(cleanup, dict)
            and cleanup.get("attempted") is True
            and cleanup.get("completed") is True
            and cleanup.get("exit_code") == 0
            and cleanup.get("remaining_resources") == []
        )
        return "PASS" if exact else "FAIL"

    def traceability(self) -> str:
        states = [
            self.command(name)[0]
            for name in (
                "traceability",
                "traceability-validator-negative-cases",
                "traceability-evidence",
            )
        ]
        combined = self.combine(states)
        if combined != "PASS":
            return combined
        value, _ = self.json_evidence(
            "traceability-summary.json", self.traceability_schema
        )
        if value is None:
            return "FAIL"
        exact = (
            value.get("module_id") == "MOD-EDGE-001"
            and value.get("result") == "PASS"
            and value.get("valid") is True
            and value.get("failures") == []
            and value.get("manifest_digest")
            == sha256(self.repo / "edge-rs/requirements-traceability.json")
        )
        return "PASS" if exact else "FAIL"

    def findings(self) -> tuple[str, int, str | None]:
        path = self.repo / "edge-rs/module-findings.json"
        digest = sha256(path) if path.is_file() and not path.is_symlink() else None
        try:
            value = load(path)
        except (OSError, UnicodeError, json.JSONDecodeError, EvidenceError):
            return "FAIL", 0, digest
        findings = (
            value.get("findings") if isinstance(value.get("findings"), list) else []
        )
        open_p0 = sum(
            1
            for finding in findings
            if isinstance(finding, dict)
            and finding.get("severity") == "P0"
            and finding.get("status") == "OPEN"
        )
        valid = self.findings_schema is not None and not schema_failures(
            self.findings_schema, value
        )
        return ("PASS" if valid and open_p0 == 0 else "FAIL"), open_p0, digest

    def derive(self) -> dict[str, Any]:
        findings_state, open_p0, findings_digest = self.findings()
        binary = self.repo / "edge-rs/target/release/masi-edge"
        release_state = self.command("release-build")[0]
        release_binary_state = (
            "PASS"
            if release_state == "PASS" and binary.is_file() and not binary.is_symlink()
            else ("NOT_RUN" if release_state == "NOT_RUN" else "FAIL")
        )
        criteria = {
            "public_contracts": self.direct_command_group("public-contracts"),
            "format_lint_static": self.direct_command_group(
                "rustc-version", "cargo-version", "format", "clippy"
            ),
            "unit_property_contract": self.direct_command_group(
                "tests",
                "performance-rehearsal",
                "soak-rehearsal-validation",
                "soak-validator-negative-cases",
                "module-gate-runner-negative-cases",
            ),
            "release_binary": release_binary_state,
            "real_binary_public_boundary": self.blackbox(),
            "os_fault_recovery": self.os_faults(),
            "real_oci_startup": self.qualification_hold_evidence(
                "oci-smoke", "oci-smoke/oci-smoke-evidence.json", self.oci_predicate
            ),
            "deep_runtime_checks": self.qualification_hold_evidence(
                "deep-checks",
                "deep-checks/deep-check-summary.json",
                self.deep_predicate,
            ),
            "supply_security_checks": self.qualification_hold_evidence(
                "supply-chain",
                "supply-chain/supply-chain.json",
                self.supply_predicate,
            ),
            "formal_soak": self.formal_soak(),
            "traceability": self.traceability(),
            "known_p0_zero": findings_state,
        }
        blockers = [name for name in COMPLETION_CRITERIA if criteria[name] != "PASS"]
        evidence_digests = {
            name: digest_if_direct(self.run_root, relative)
            for name, relative in COMPLETION_EVIDENCE.items()
        }
        status = (
            "COMPLETE"
            if not blockers and all(evidence_digests.values())
            else "INCOMPLETE"
        )
        if status == "INCOMPLETE" and not blockers:
            # A missing digest is itself an evidence failure.  Associate it with
            # the criterion that owns the missing artifact so the public blocker
            # vocabulary remains closed and deterministic.
            digest_owner = {
                "public_contracts": "public_contracts",
                "rustc_version": "format_lint_static",
                "cargo_version": "format_lint_static",
                "format": "format_lint_static",
                "clippy": "format_lint_static",
                "tests": "unit_property_contract",
                "blackbox": "real_binary_public_boundary",
                "release_build": "release_binary",
                "os_faults": "os_fault_recovery",
                "oci": "real_oci_startup",
                "deep": "deep_runtime_checks",
                "supply": "supply_security_checks",
                "formal_soak": "formal_soak",
                "formal_soak_validation": "formal_soak",
                "traceability": "traceability",
            }
            for name, digest in evidence_digests.items():
                if digest is None:
                    owner = digest_owner[name]
                    criteria[owner] = "FAIL"
            blockers = [
                name for name in COMPLETION_CRITERIA if criteria[name] != "PASS"
            ]
        startup_test_blockers = sum(name != "known_p0_zero" for name in blockers)
        return {
            "decision_id": "DEC-044",
            "status": status,
            "known_p0_count": open_p0,
            "real_startup_test_blocker_count": startup_test_blockers,
            "findings_registry": "edge-rs/module-findings.json",
            "findings_registry_digest": findings_digest,
            "blockers": blockers,
            "criteria": criteria,
            "evidence_digests": evidence_digests,
        }


def expected_artifact_digests(
    repo: Path, run_root: Path, release_passed: bool
) -> dict[str, str | None]:
    paths: dict[str, Path] = {
        "cargo_lock": repo / "edge-rs/Cargo.lock",
        "edge_contract": repo / "contracts/edge/v1/edge.proto",
        "inference_contract": repo / "contracts/inference/v1/inference.proto",
        "inference_wire_profile": repo / "contracts/inference/v1/profile.json",
        "edge_evidence_schema": repo / "contracts/evidence/v1/edge-module-schema.json",
        "module_findings_schema": repo
        / "contracts/evidence/module-findings/v1/schema.json",
        "module_findings_registry": repo / "edge-rs/module-findings.json",
        "error_contract": repo / "contracts/edge/v1/error-semantics.json",
        "target_contract": repo / "contracts/target/v1/schema.json",
        "telemetry_contract": repo / "contracts/telemetry/v1/schema.json",
        "qualification_profile": repo / "contracts/profiles/v1/rust-edge-agent.json",
        "soak_profile": repo / "contracts/profiles/v1/qualification-soak-3600s.json",
        "traceability_manifest": repo / "edge-rs/requirements-traceability.json",
        "requirements_baseline": repo
        / "docs/masi-nids-vnext-system-requirements-2026-08-09.md",
    }
    result: dict[str, str | None] = {}
    for name, path in paths.items():
        result[name] = (
            sha256(path) if path.is_file() and not path.is_symlink() else None
        )
    run_paths = {
        "traceability_evidence": "traceability-summary.json",
        "oci_evidence": "oci-smoke/oci-smoke-evidence.json",
        "deep_check_evidence": "deep-checks/deep-check-summary.json",
        "supply_chain_evidence": "supply-chain/supply-chain.json",
    }
    for name, relative in run_paths.items():
        result[name] = digest_if_direct(run_root, relative)
    binary = repo / "edge-rs/target/release/masi-edge"
    result["release_binary"] = (
        sha256(binary)
        if release_passed and binary.is_file() and not binary.is_symlink()
        else None
    )
    return result


def validate_summary(repo: Path, evidence_path: Path) -> dict[str, Any]:
    if evidence_path.name != "gate-summary.json" or evidence_path.is_symlink():
        raise EvidenceError("evidence must be a direct gate-summary.json file")
    schema = load(repo / "contracts/evidence/v1/edge-module-schema.json")
    evidence = load(evidence_path)
    failures = schema_failures(schema, evidence)
    if failures:
        raise EvidenceError("\n".join(failures))
    run_root = evidence_path.parent
    if run_root.name != evidence.get("run_id"):
        raise EvidenceError("run_id does not match the immutable run directory")
    validate_run_snapshot(
        repo,
        run_root,
        str(evidence["source_tree_digest"]),
        str(evidence["working_tree_status_digest"]),
        bool(evidence["working_tree_dirty"]),
    )
    source_revision, baseline_git_tree = git_identity(repo)
    if evidence.get("source_revision") != source_revision:
        raise EvidenceError(
            "source_revision does not match the checked-out Git revision"
        )
    if evidence.get("baseline_git_tree") != baseline_git_tree:
        raise EvidenceError("baseline_git_tree does not match the checked-out Git tree")
    deriver = CompletionDeriver(
        repo,
        run_root,
        str(evidence["run_id"]),
        str(evidence["source_tree_digest"]),
        str(evidence["working_tree_status_digest"]),
        bool(evidence["working_tree_dirty"]),
    )
    completion = deriver.derive()
    if evidence.get("completion") != completion:
        raise EvidenceError("completion does not equal the semantic re-derivation")
    completed = completion["status"] == "COMPLETE"
    if evidence.get("overall_module_complete") is not completed:
        raise EvidenceError("overall_module_complete does not equal derived completion")
    release_passed = deriver.command("release-build")[0] == "PASS"
    expected_artifacts = expected_artifact_digests(repo, run_root, release_passed)
    if evidence.get("artifact_digests") != expected_artifacts:
        raise EvidenceError(
            "artifact_digests do not bind the exact public inputs and run evidence"
        )
    traceability = evidence.get("requirement_traceability")
    if not isinstance(traceability, dict):
        raise EvidenceError("requirement_traceability is absent")
    if (
        traceability.get("digest") != expected_artifacts["traceability_manifest"]
        or traceability.get("summary_digest")
        != expected_artifacts["traceability_evidence"]
        or traceability.get("result") != deriver.traceability()
    ):
        raise EvidenceError("requirement_traceability does not bind its exact evidence")
    return evidence


def require_child_file(parent: Path, name: str) -> Path:
    path = direct_run_file(parent, name)
    if path is None:
        raise EvidenceError(f"child evidence does not bind direct artifact {name}")
    return path


def require_child_directory(parent: Path, name: str) -> Path:
    path = parent / name
    if path.is_symlink() or not path.is_dir():
        raise EvidenceError(f"child evidence does not bind direct directory {name}")
    resolved_parent = parent.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved.parent != resolved_parent:
        raise EvidenceError(
            f"child evidence directory {name} escapes its run directory"
        )
    return resolved


def validate_child_evidence(
    repo: Path, evidence_path: Path, evidence: dict[str, Any]
) -> dict[str, Any]:
    schema_version = evidence.get("schema_version")
    expected_names = {
        "edge-oci-startup-evidence/v1": "oci-smoke-evidence.json",
        "edge-deep-check-evidence/v1": "deep-check-summary.json",
        "rust-edge-agent-supply-verification/v1": "supply-chain.json",
    }
    if schema_version not in expected_names:
        raise EvidenceError(f"unsupported Edge evidence schema {schema_version!r}")
    if (
        evidence_path.name != expected_names[schema_version]
        or evidence_path.is_symlink()
    ):
        raise EvidenceError("child evidence has a noncanonical direct filename")
    schema = load(repo / "contracts/evidence/v1/edge-module-schema.json")
    failures = schema_failures(schema, evidence)
    if failures:
        raise EvidenceError("\n".join(failures))

    result = evidence.get("result")
    dirty = bool(evidence["working_tree_dirty"])
    artifact_root = evidence_path.parent
    if (
        schema_version == "rust-edge-agent-supply-verification/v1"
        and result != "NOT_RUN"
    ):
        artifact_root = require_child_directory(evidence_path.parent, "supply")
    if result != "NOT_RUN":
        validate_run_snapshot(
            repo,
            artifact_root,
            str(evidence["source_tree_digest"]),
            str(evidence["working_tree_status_digest"]),
            dirty,
        )
        source_revision, _ = git_identity(repo)
        if evidence.get("source_revision") != source_revision:
            raise EvidenceError(
                "child source_revision does not match the checked-out Git revision"
            )
    if result == "PASS" and dirty:
        raise EvidenceError("dirty child evidence cannot claim PASS")
    if result == "HOLD" and not dirty:
        raise EvidenceError("clean child evidence cannot use the dirty-worktree HOLD")

    predicate: Callable[[dict[str, Any]], bool]
    if schema_version == "edge-oci-startup-evidence/v1":
        predicate = CompletionDeriver.oci_predicate
        if result in {"PASS", "HOLD"}:
            if evidence.get("qualification_reason") != (
                "DIRTY_WORKTREE_NOT_RELEASE_BASELINE"
                if dirty
                else "CLEAN_SOURCE_SNAPSHOT"
            ):
                raise EvidenceError(
                    "OCI qualification reason does not match its source state"
                )
            probe = load(require_child_file(evidence_path.parent, "oci-probe.json"))
            archive = load(
                require_child_file(evidence_path.parent, "oci-archive-inspection.json")
            )
            if evidence.get("probe") != probe or evidence.get("archive") != archive:
                raise EvidenceError(
                    "OCI parent does not exactly embed its probe/archive evidence"
                )
    elif schema_version == "edge-deep-check-evidence/v1":
        predicate = CompletionDeriver.deep_predicate
        if result in {"PASS", "HOLD"}:
            if evidence.get("qualification_reason") != (
                "DIRTY_WORKTREE_NOT_RELEASE_BASELINE"
                if dirty
                else "CLEAN_SOURCE_SNAPSHOT"
            ):
                raise EvidenceError(
                    "deep-check qualification reason does not match its source state"
                )
            artifacts = evidence.get("artifact_digests")
            expected = {
                "cargo_lock": sha256(repo / "edge-rs/Cargo.lock"),
                "edge_contract": sha256(repo / "contracts/edge/v1/edge.proto"),
                "qualification_profile": sha256(
                    repo / "contracts/profiles/v1/rust-edge-agent.json"
                ),
                "miri_log": sha256(
                    require_child_file(evidence_path.parent, "miri.log")
                ),
                "address_sanitizer_log": sha256(
                    require_child_file(evidence_path.parent, "asan.log")
                ),
                "thread_sanitizer_log": sha256(
                    require_child_file(evidence_path.parent, "tsan.log")
                ),
            }
            if artifacts != expected:
                raise EvidenceError(
                    "deep-check artifact digests do not bind their exact inputs"
                )
    else:
        predicate = CompletionDeriver.supply_predicate
        if result in {"PASS", "HOLD"}:
            findings = evidence.get("trivy", {}).get("findings", {})
            expected_holds = []
            if isinstance(findings, dict) and findings.get("unfixed_high"):
                expected_holds.append("UNFIXED_HIGH_REQUIRES_OWNER_EXCEPTION")
            if dirty:
                expected_holds.append("DIRTY_WORKTREE_NOT_RELEASE_BASELINE")
            if evidence.get("hold_reasons") != expected_holds:
                raise EvidenceError("supply hold reasons do not match its source state")
            manifest = require_child_file(artifact_root, "release-manifest.json")
            signature = require_child_file(
                artifact_root, "release-manifest.sigstore.json"
            )
            if evidence.get("manifest_digest") != sha256(manifest):
                raise EvidenceError(
                    "supply evidence does not bind its release manifest"
                )
            if evidence.get("signature_bundle_digest") != sha256(signature):
                raise EvidenceError(
                    "supply evidence does not bind its signature bundle"
                )

    if result in {"PASS", "HOLD"} and not predicate(evidence):
        raise EvidenceError("child operational checks are incomplete or inconsistent")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--derive-completion", action="store_true")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--source-tree-digest")
    parser.add_argument("--working-tree-status-digest")
    parser.add_argument("--working-tree-dirty", choices=("true", "false"))
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.derive_completion:
        required = (
            args.run_root,
            args.run_id,
            args.source_tree_digest,
            args.working_tree_status_digest,
            args.working_tree_dirty,
        )
        if any(value is None for value in required) or args.evidence is not None:
            parser.error("completion derivation requires the complete run context only")
        try:
            run_root = args.run_root.resolve()
            validate_run_snapshot(
                repo,
                run_root,
                str(args.source_tree_digest),
                str(args.working_tree_status_digest),
                args.working_tree_dirty == "true",
            )
            deriver = CompletionDeriver(
                repo,
                run_root,
                str(args.run_id),
                str(args.source_tree_digest),
                str(args.working_tree_status_digest),
                args.working_tree_dirty == "true",
            )
            completion = deriver.derive()
        except (OSError, UnicodeError, json.JSONDecodeError, EvidenceError) as error:
            raise SystemExit(str(error)) from error
        print(json.dumps(completion, separators=(",", ":"), sort_keys=True))
        return 0
    if args.evidence is None or any(
        value is not None
        for value in (
            args.run_root,
            args.run_id,
            args.source_tree_digest,
            args.working_tree_status_digest,
            args.working_tree_dirty,
        )
    ):
        parser.error("summary validation requires --evidence only")
    try:
        evidence_path = args.evidence.resolve()
        candidate = load(evidence_path)
        if candidate.get("schema_version") == "edge-module-gate-summary/v1":
            evidence = validate_summary(repo, evidence_path)
            module_completion: str | None = evidence["completion"]["status"]
        else:
            evidence = validate_child_evidence(repo, evidence_path, candidate)
            module_completion = None
    except (OSError, UnicodeError, json.JSONDecodeError, EvidenceError) as error:
        raise SystemExit(str(error)) from error
    print(
        json.dumps(
            {
                "schema_version": "edge-evidence-validation/v1",
                "evidence": str(args.evidence.resolve()),
                "evidence_schema_version": evidence["schema_version"],
                "result": evidence["result"],
                "qualification": evidence["qualification"],
                "module_completion": module_completion,
                "known_p0_count": evidence.get("completion", {}).get("known_p0_count")
                if isinstance(evidence.get("completion"), dict)
                else None,
                "valid": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
