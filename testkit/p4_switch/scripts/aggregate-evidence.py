from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

# Direct script execution puts this file's directory, rather than the repository
# root, on sys.path.  Resolve the trusted source root from this file so the host
# aggregator imports the exact checked-out testkit closure regardless of the
# caller's working directory.
SOURCE_REPOSITORY = Path(__file__).resolve().parents[3]
if str(SOURCE_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_REPOSITORY))

from testkit.p4_switch.lib.p4runtime_client import bmv2_device_config
from testkit.p4_switch.lib.source_identity import (
    current_source_identity,
    read_stable_regular_file,
)


COMPILER_IMAGE = "masi-nids/p4-switch-p4c@sha256:8c26666dfa1041b0f9a29b5051c92dbf4ce5df807273f80dd54b3aff5412c926"
EXCLUDED_TREE_PARTS = {
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
MANDATORY_OPERATIONAL_TEST_IDS = frozenset(
    {
        "TEST-P4-STATIC-CONTRACT-001",
        "TEST-TRAFFIC-001-runner-runtime-version",
        "TEST-P4-COMPILE-001",
        "TEST-P4TESTGEN-001",
        "TEST-P4-MININET-001",
        "TEST-P4-STARTUP-001",
        "TEST-P4-SECURITY-001",
        "TEST-P4-FW-001-capacity-activation",
        "TEST-P4-RESOURCE-001",
        "TEST-P4-FW-001-priority-conflict-shadow",
        "TEST-P4-FW-001-partial-selector-loss",
        "TEST-P4-COMPAT-001-pipeline-drift",
        "TEST-P4-FW-001-overlay-order-default",
        "TEST-P4-FW-001-fragment-malformed",
        "TEST-TRAFFIC-001-four-modes",
        "TEST-P4-OBS-001-counter-readback",
        "TEST-TEL-INF-001-bounded-snapshot",
        "TEST-TEL-INF-001-best-effort-hints",
        "TEST-TEL-INF-001-hint-loss",
        "TEST-P4-FW-001-host-filter-contamination",
        "TEST-P4-FAULT-001-link-recovery",
        "TEST-P4-FAULT-003-netem-recovery",
        "TEST-P4-PERF-ABSOLUTE-001",
        "TEST-P4-FAULT-002-sigkill-orchestration",
        "TEST-P4-FAULT-002-process-crash-recovery",
        "TEST-P4-LIFECYCLE-001",
        "TEST-P4-SOAK-3600S-001",
        "TEST-P4-SUPPLY-001",
        "TEST-P4-FW-001-host-filter-negative",
        "TEST-P4-ARTIFACT-BINDING-001",
        "TEST-TRAFFIC-001-runner-binding",
        "TEST-P4-RUNTIME-BINDING-001",
        "TEST-P4-FINDINGS-001",
        "TEST-P4-CLEANUP-001",
    }
)
CONTRACT_BINDING_TEST_IDS = frozenset(
    {
        "TEST-P4-STATIC-CONTRACT-001",
        "TEST-P4-ARTIFACT-BINDING-001",
        "TEST-TRAFFIC-001-runner-binding",
        "TEST-P4-RUNTIME-BINDING-001",
    }
)
PHASE_TEST_IDS = {
    "unit": {"TEST-P4-STATIC-CONTRACT-001"},
    "runner-environment": {"TEST-TRAFFIC-001-runner-runtime-version"},
    "compiler": {"TEST-P4-COMPILE-001"},
    "p4testgen": {"TEST-P4TESTGEN-001"},
    "mininet": {"TEST-P4-MININET-001"},
    "functional": {
        "TEST-P4-STARTUP-001",
        "TEST-P4-SECURITY-001",
        "TEST-P4-FW-001-capacity-activation",
        "TEST-P4-RESOURCE-001",
        "TEST-P4-FW-001-priority-conflict-shadow",
        "TEST-P4-FW-001-partial-selector-loss",
        "TEST-P4-COMPAT-001-pipeline-drift",
        "TEST-P4-FW-001-overlay-order-default",
        "TEST-P4-FW-001-fragment-malformed",
        "TEST-TRAFFIC-001-four-modes",
        "TEST-P4-OBS-001-counter-readback",
        "TEST-TEL-INF-001-bounded-snapshot",
        "TEST-TEL-INF-001-best-effort-hints",
        "TEST-TEL-INF-001-hint-loss",
        "TEST-P4-FW-001-host-filter-contamination",
        "TEST-P4-FAULT-001-link-recovery",
        "TEST-P4-FAULT-003-netem-recovery",
    },
    "performance": {"TEST-P4-PERF-ABSOLUTE-001"},
    "crash-orchestration": {"TEST-P4-FAULT-002-sigkill-orchestration"},
    "crash-recovery": {"TEST-P4-FAULT-002-process-crash-recovery"},
    "lifecycle": {"TEST-P4-LIFECYCLE-001"},
    "soak": {"TEST-P4-SOAK-3600S-001"},
    "supply-chain": {"TEST-P4-SUPPLY-001"},
}
PHASE_BINDING_KEYS = (
    "run_id",
    "source_revision",
    "source_tree_digest",
    "working_tree_dirty",
    "working_tree_status_digest",
)
EXPECTED_COMPILED_ARTIFACTS = frozenset(
    {"masi_switch.json", "masi_switch.p4info.txtpb"}
)


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(read_stable_regular_file(path))


def read_bounded_file(path: Path, maximum_bytes: int = 64 * 1024 * 1024) -> bytes:
    return read_stable_regular_file(path, maximum_bytes)


def stable_exact_regular_files(root: Path, expected: frozenset[str]) -> dict[str, bytes]:
    """Read one exact flat artifact closure and reject every unbound entry."""

    if root.is_symlink() or not root.is_dir():
        raise ValueError("compiled artifact root is not an ordinary directory")

    def collect() -> dict[str, tuple[int, int, int, int]]:
        entries: dict[str, tuple[int, int, int, int]] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"compiled artifact closure contains a symlink: {relative}")
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"compiled artifact closure contains a non-regular entry: {relative}"
                )
            entries[relative] = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
        if set(entries) != expected:
            raise ValueError(
                "compiled artifact closure differs from the exact profile: "
                f"expected={sorted(expected)} observed={sorted(entries)}"
            )
        return entries

    before = collect()
    payloads = {name: read_bounded_file(root / name) for name in sorted(expected)}
    if collect() != before:
        raise ValueError("compiled artifact closure changed while being read")
    return payloads


def evidence_checksum_snapshot(
    root: Path,
) -> tuple[dict[str, tuple[str, int, int, int, int, str]], list[str]]:
    """Freeze the evidence entry set and content used by SHA256SUMS."""

    def collect() -> dict[str, tuple[str, int, int, int, int]]:
        entries: dict[str, tuple[str, int, int, int, int]] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"evidence tree contains a symlink: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                kind = "directory"
            elif stat.S_ISREG(metadata.st_mode):
                kind = "file"
            else:
                raise ValueError(f"evidence tree contains a special entry: {relative}")
            if relative == "SHA256SUMS":
                if kind != "file":
                    raise ValueError("SHA256SUMS is not an ordinary file")
                continue
            entries[relative] = (
                kind,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
        return entries

    before = collect()
    snapshot: dict[str, tuple[str, int, int, int, int, str]] = {}
    checksum_lines: list[str] = []
    for relative, metadata in sorted(before.items()):
        content_digest = ""
        if metadata[0] == "file":
            content_digest = hashlib.sha256(read_bounded_file(root / relative)).hexdigest()
            checksum_lines.append(f"{content_digest}  {relative}")
        snapshot[relative] = (*metadata, content_digest)
    if collect() != before:
        raise ValueError("evidence tree changed while checksums were calculated")
    return snapshot, checksum_lines


def verify_evidence_tree_unchanged(
    root: Path, expected: dict[str, tuple[str, int, int, int, int, str]]
) -> None:
    observed, _ = evidence_checksum_snapshot(root)
    if observed != expected:
        raise ValueError("evidence tree changed after SHA256SUMS publication")


def tree_digest(root: Path) -> str:
    def collect() -> list[Path]:
        paths: list[Path] = []
        for path in root.rglob("*"):
            relative_path = path.relative_to(root)
            if any(part in EXCLUDED_TREE_PARTS for part in relative_path.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"tree digest contains a symlink: {path}")
            if not path.is_file():
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            paths.append(path)
        return sorted(paths)

    paths = collect()
    before = {
        path: (
            (metadata := path.stat(follow_symlinks=False)).st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        for path in paths
    }
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix().encode()
        payload = read_stable_regular_file(path)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    if collect() != paths:
        raise ValueError("tree file set changed while hashing")
    for path in paths:
        metadata = path.stat(follow_symlinks=False)
        if before[path] != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise ValueError(f"tree changed while hashing: {path}")
    return "sha256:" + digest.hexdigest()


def test_record(
    test_id: str,
    requirement_ids: list[str],
    result: str,
    evidence: dict[str, object],
    *,
    applicability: str = "APPLICABLE",
) -> dict[str, object]:
    return {
        "id": test_id,
        "requirement_ids": requirement_ids,
        "level": "MODULE",
        "applicability": applicability,
        "result": result,
        "qualification": (
            "QUALIFIED"
            if result == "PASS" and applicability == "APPLICABLE"
            else "NOT_QUALIFIED"
        ),
        "evidence": evidence,
    }


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def result_qualification_consistent(result: object, qualification: object) -> bool:
    return (result == "PASS" and qualification == "QUALIFIED") or (
        result in {"FAIL", "HOLD", "NOT_RUN"} and qualification == "NOT_QUALIFIED"
    )


def validate_phase_document(
    document: dict[str, object],
    name: str,
    run_id: str | None = None,
    source_identity: dict[str, object] | None = None,
) -> None:
    if (
        document.get("phase") != name
        or document.get("level") not in {"REHEARSAL", "MODULE"}
        or document.get("applicability") != "APPLICABLE"
        or not result_qualification_consistent(
            document.get("result"), document.get("qualification")
        )
    ):
        raise ValueError(f"{name} phase identity/result/qualification is invalid")
    if run_id is not None and document.get("run_id") != run_id:
        raise ValueError(f"{name} phase run identity mismatch")
    if source_identity is not None:
        for key in PHASE_BINDING_KEYS[1:]:
            if document.get(key) != source_identity.get(key):
                raise ValueError(f"{name} phase {key} mismatch")
    tests = document.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError(f"{name} phase has no test records")
    observed_test_ids = {
        record.get("id") for record in tests if isinstance(record, dict)
    }
    if observed_test_ids != PHASE_TEST_IDS[name] or len(tests) != len(
        observed_test_ids
    ):
        raise ValueError(f"{name} phase test identity set is incomplete or duplicated")
    for index, record in enumerate(tests):
        if not isinstance(record, dict):
            raise ValueError(f"{name} test {index} is not an object")
        requirement_ids = record.get("requirement_ids")
        if (
            not isinstance(record.get("id"), str)
            or not record["id"]
            or record.get("level") not in {"REHEARSAL", "MODULE"}
            or record.get("applicability") not in {"APPLICABLE", "NOT_APPLICABLE"}
            or not isinstance(requirement_ids, list)
            or not requirement_ids
            or not all(isinstance(item, str) and item for item in requirement_ids)
            or not isinstance(record.get("evidence"), dict)
            or not result_qualification_consistent(
                record.get("result"), record.get("qualification")
            )
        ):
            raise ValueError(
                f"{name} test {index} is structurally or semantically invalid"
            )
        if (
            record.get("applicability") == "APPLICABLE"
            and record.get("result") == "PASS"
            and not record.get("evidence")
        ):
            raise ValueError(f"{name} PASS test {index} has no bound observation")
    applicable = [
        record for record in tests if record.get("applicability") == "APPLICABLE"
    ]
    expected_result = "PASS"
    if any(record.get("result") == "FAIL" for record in applicable):
        expected_result = "FAIL"
    elif any(record.get("result") in {"HOLD", "NOT_RUN"} for record in applicable):
        expected_result = "HOLD"
    if document.get("result") != expected_result:
        raise ValueError(
            f"{name} phase result does not match its applicable test records"
        )
    performance = document.get("performance", [])
    if not isinstance(performance, list):
        raise ValueError(f"{name} performance rows are invalid")
    for index, row in enumerate(performance):
        if not isinstance(row, dict) or not result_qualification_consistent(
            row.get("result"), row.get("qualification")
        ):
            raise ValueError(
                f"{name} performance row {index} has invalid result/qualification"
            )


def validate_phase_references(
    document: dict[str, object], evidence_dir: Path, name: str
) -> None:
    pairs = {
        "log": "log_sha256",
        "compiler_file": "compiler_sha256",
        "resource_file": "resource_sha256",
        "workload_file": "workload_sha256",
        "soak_evidence": "soak_evidence_sha256",
        "shutdown_log": "shutdown_log_sha256",
        "manifest": "manifest_digest",
        "signature_bundle": "signature_bundle_digest",
        "verification": "verification_digest",
    }

    def walk(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for reference_key, digest_key in pairs.items():
            if reference_key not in value and digest_key not in value:
                continue
            reference = value.get(reference_key)
            expected = value.get(digest_key)
            if not isinstance(reference, str) or not isinstance(expected, str):
                raise ValueError(f"{name} has an incomplete {reference_key} binding")
            relative = Path(reference)
            if relative.is_absolute() or len(relative.parts) != 1 or relative.name != reference:
                raise ValueError(f"{name} has an unsafe {reference_key} path")
            base = evidence_dir / "lifecycle-cycles" if reference_key == "shutdown_log" else evidence_dir
            payload = read_bounded_file(base / reference)
            observed = hashlib.sha256(payload).hexdigest()
            normalized = expected.removeprefix("sha256:")
            if normalized != observed:
                raise ValueError(f"{name} {reference_key} digest mismatch")
        for item in value.values():
            walk(item)

    walk(document)
    if name == "lifecycle":
        records = object_list(document.get("tests", []))
        evidence = records[0].get("evidence", {}) if records else {}
        if not isinstance(evidence, dict):
            raise ValueError("lifecycle evidence is missing")
        expected_tree = evidence.get("cycles_tree_sha256")
        if not isinstance(expected_tree, str):
            raise ValueError("lifecycle cycle tree digest is missing")
        cycle_paths = sorted((evidence_dir / "lifecycle-cycles").glob("cycle-*.json"))
        observed_tree = hashlib.sha256(
            b"".join(path.name.encode() + b"\x00" + read_bounded_file(path) for path in cycle_paths)
        ).hexdigest()
        if observed_tree != expected_tree.removeprefix("sha256:"):
            raise ValueError("lifecycle cycle tree digest mismatch")


def failure_phase(
    name: str,
    message: str,
    input_digest: str | None,
    run_id: str | None,
    source_identity: dict[str, object] | None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "phase": name,
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": "FAIL",
        "qualification": "NOT_QUALIFIED",
        "tests": [
            test_record(
                f"{name}-phase-valid",
                ["TEST-003"],
                "FAIL",
                {"error": message[:1024], "input_sha256": input_digest},
            )
        ],
    }
    if run_id is not None:
        document["run_id"] = run_id
    if source_identity is not None:
        for key in PHASE_BINDING_KEYS[1:]:
            document[key] = source_identity[key]
    return document


def load_phase_record(
    path: Path,
    name: str,
    run_id: str | None = None,
    source_identity: dict[str, object] | None = None,
) -> tuple[dict[str, object], str]:
    if not path.exists():
        document = failure_phase(
            name,
            f"missing phase evidence {path.name}",
            None,
            run_id,
            source_identity,
        )
        return document, sha256_bytes(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )
    payload: bytes | None = None
    try:
        payload = read_bounded_file(path)
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
        if not isinstance(document, dict):
            raise ValueError("phase evidence is not an object")
        validate_phase_document(document, name, run_id, source_identity)
        validate_phase_references(document, path.parent, name)
        return document, sha256_bytes(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        input_digest = sha256_bytes(payload) if payload is not None else None
        document = failure_phase(name, str(error), input_digest, run_id, source_identity)
        return document, input_digest or sha256_bytes(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )


def load_phase(path: Path, name: str) -> dict[str, object]:
    return load_phase_record(path, name)[0]


def load_cleanup(
    path: Path, run_id: str, source_identity: dict[str, object]
) -> dict[str, object]:
    try:
        document = json.loads(
            read_bounded_file(path, 256 * 1024).decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
        if not isinstance(document, dict):
            raise ValueError("cleanup evidence is not an object")
        if (
            document.get("schema_version") != "p4-global-cleanup/v1"
            or document.get("run_id") != run_id
            or document.get("attempted") is not True
            or not result_qualification_consistent(
                document.get("result"), document.get("qualification")
            )
        ):
            raise ValueError("cleanup identity/result is invalid")
        for key in PHASE_BINDING_KEYS[1:]:
            if document.get(key) != source_identity.get(key):
                raise ValueError(f"cleanup {key} mismatch")
        remaining = document.get("remaining_resources")
        if not isinstance(remaining, dict) or set(remaining) != {
            "containers",
            "networks",
            "volumes",
        }:
            raise ValueError("cleanup remaining resource shape is invalid")
        host_checks = document.get("host_checks")
        expected_host_checks = {
            "mininet_phase_cleanup_valid",
            "mininet_host_processes_absent",
            "mininet_switch_interfaces_absent",
            "host_interface_masi_s1_absent",
            "host_interface_masi_s2_absent",
        }
        if (
            not isinstance(host_checks, dict)
            or set(host_checks) != expected_host_checks
            or not all(type(value) is bool for value in host_checks.values())
            or not isinstance(document.get("host_error"), str)
        ):
            raise ValueError("cleanup host residual shape is invalid")
        return document
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return {
            "schema_version": "p4-global-cleanup/v1",
            "run_id": run_id,
            **source_identity,
            "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "attempted": False,
            "result": "FAIL",
            "qualification": "NOT_QUALIFIED",
            "commands": [],
            "remaining_resources": {"containers": [], "networks": [], "volumes": []},
            "host_checks": {
                "mininet_phase_cleanup_valid": False,
                "mininet_host_processes_absent": False,
                "mininet_switch_interfaces_absent": False,
                "host_interface_masi_s1_absent": False,
                "host_interface_masi_s2_absent": False,
            },
            "query_error": str(error)[:1024],
            "host_error": str(error)[:1024],
        }


def object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def module_findings_summary(repo: Path) -> dict[str, object]:
    registry_path = repo / "p4/module-findings.json"
    schema_path = repo / "contracts/evidence/module-findings/v1/schema.json"
    if not registry_path.is_file():
        raise ValueError(f"missing P4 module findings registry: {registry_path}")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(registry), key=lambda item: list(item.path))
    if errors:
        raise ValueError("; ".join(error.message for error in errors))
    if registry.get("module_id") != "MOD-SW-001":
        raise ValueError("P4 module findings registry has the wrong module_id")
    findings = object_list(registry.get("findings", []))
    open_findings = [item for item in findings if item.get("status") == "OPEN"]
    return {
        "open_total": len(open_findings),
        "open_p0": sum(item.get("severity") == "P0" for item in open_findings),
        "registry_digest": sha256(registry_path),
    }


def derive_operational_completion(
    tests: list[dict[str, object]], findings: dict[str, object]
) -> tuple[dict[str, bool], bool]:
    records_by_id: dict[str, list[dict[str, object]]] = {}
    for record in tests:
        test_id = record.get("id")
        if isinstance(test_id, str):
            records_by_id.setdefault(test_id, []).append(record)

    def passed(test_id: str) -> bool:
        records = records_by_id.get(test_id, [])
        return (
            len(records) == 1
            and records[0].get("applicability") == "APPLICABLE"
            and records[0].get("result") == "PASS"
            and records[0].get("qualification") == "QUALIFIED"
        )

    applicable_tests = [
        record for record in tests if record.get("applicability") == "APPLICABLE"
    ]
    mandatory_tests_executed = all(
        passed(test_id) for test_id in MANDATORY_OPERATIONAL_TEST_IDS
    )
    contracts_frozen = all(passed(test_id) for test_id in CONTRACT_BINDING_TEST_IDS)
    no_required_not_run = bool(applicable_tests) and all(
        record.get("result") not in {"HOLD", "NOT_RUN"} for record in applicable_tests
    )
    operational_gates_pass = bool(applicable_tests) and all(
        record.get("result") == "PASS" and record.get("qualification") == "QUALIFIED"
        for record in applicable_tests
    )
    real_runtime_started = all(
        passed(test_id)
        for test_id in (
            "TEST-P4-STARTUP-001",
            "TEST-P4-FW-001-capacity-activation",
            "TEST-P4-RUNTIME-BINDING-001",
        )
    )

    formal_soak_executed = False
    soak_records = records_by_id.get("TEST-P4-SOAK-3600S-001", [])
    if len(soak_records) == 1 and passed("TEST-P4-SOAK-3600S-001"):
        evidence = soak_records[0].get("evidence", {})
        if isinstance(evidence, dict):
            cleanup = evidence.get("cleanup", {})
            summary = evidence.get("summary", {})
            module_metrics = (
                summary.get("module_metrics", {}) if isinstance(summary, dict) else {}
            )
            formal_soak_executed = (
                isinstance(cleanup, dict)
                and isinstance(summary, dict)
                and isinstance(module_metrics, dict)
                and evidence.get("qualified_elapsed_ms", 0) >= 3_600_000
                and cleanup.get("completed") is True
                and cleanup.get("remaining_resources") == []
                and summary.get("error_count") == 0
                and summary.get("container_restarts") == 0
                and summary.get("oom_events") == 0
                and summary.get("oracle_mismatches") == 0
                and summary.get("resource_limit_violations") == 0
                and summary.get("unclassified_gap_count") == 0
                and module_metrics.get("formal_schedule_executed") is True
            )

    completion = {
        "contracts_frozen": contracts_frozen,
        "mandatory_tests_executed": mandatory_tests_executed,
        "formal_soak_executed": formal_soak_executed,
        "no_required_not_run": no_required_not_run,
        "open_p0_zero": findings.get("open_p0") == 0,
        "operational_gates_pass": operational_gates_pass,
        "real_runtime_started": real_runtime_started,
    }
    return completion, all(completion.values())


def safe_directory(path: Path, label: str) -> Path:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} path traverses a symlink")
    if not absolute.is_dir():
        raise ValueError(f"{label} is not an ordinary directory")
    return absolute


def write_new_file(directory: Path, name: str, payload: bytes) -> Path:
    if Path(name).name != name or not name:
        raise ValueError("output name must be a direct child")
    descriptor_directory = os.open(
        directory, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o660,
            dir_fd=descriptor_directory,
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(descriptor_directory)
    finally:
        os.close(descriptor_directory)
    return directory / name


def main() -> int:
    if len(sys.argv) != 12:
        raise SystemExit(
            "usage: aggregate-evidence.py REPO EVIDENCE ARTIFACTS RUN_ID STARTED_AT "
            "RUNNER_IMAGE RUNTIME_IMAGE FIREWALL_BEFORE FIREWALL_AFTER CLEANUP OUTPUT"
        )
    (
        repo_name,
        evidence_name,
        artifacts_name,
        run_id,
        started_at,
        runner_image,
        runtime_image,
        firewall_before,
        firewall_after,
        cleanup_name,
        output_name,
    ) = sys.argv[1:]
    repo = safe_directory(Path(repo_name), "repository")
    expected_repo = SOURCE_REPOSITORY
    if repo != expected_repo:
        raise SystemExit(
            "repository argument does not match the running source closure"
        )
    evidence_dir = safe_directory(Path(evidence_name), "evidence")
    artifacts = safe_directory(Path(artifacts_name), "compiled artifacts")
    output = Path(output_name).absolute()
    if output.parent != evidence_dir or output.name != "qualification-evidence.json":
        raise SystemExit("qualification output must be the fresh direct evidence child")
    if output.exists() or output.is_symlink() or (evidence_dir / "SHA256SUMS").exists():
        raise SystemExit("qualification output or checksum manifest already exists")

    source_identity = current_source_identity(repo)
    phase_records = [
        load_phase_record(evidence_dir / "unit.json", "unit", run_id, source_identity),
        load_phase_record(
            evidence_dir / "runner-environment.json",
            "runner-environment",
            run_id,
            source_identity,
        ),
        load_phase_record(evidence_dir / "compiler.json", "compiler", run_id, source_identity),
        load_phase_record(evidence_dir / "p4testgen.json", "p4testgen", run_id, source_identity),
        load_phase_record(evidence_dir / "mininet.json", "mininet", run_id, source_identity),
        load_phase_record(evidence_dir / "functional.json", "functional", run_id, source_identity),
        load_phase_record(evidence_dir / "performance.json", "performance", run_id, source_identity),
        load_phase_record(
            evidence_dir / "crash-orchestration.json",
            "crash-orchestration",
            run_id,
            source_identity,
        ),
        load_phase_record(
            evidence_dir / "crash-recovery.json",
            "crash-recovery",
            run_id,
            source_identity,
        ),
        load_phase_record(evidence_dir / "lifecycle.json", "lifecycle", run_id, source_identity),
        load_phase_record(evidence_dir / "soak.json", "soak", run_id, source_identity),
        load_phase_record(
            evidence_dir / "supply-chain.json", "supply-chain", run_id, source_identity
        ),
    ]
    phases = [record[0] for record in phase_records]
    runner_environment_phase = next(
        phase for phase in phases if phase.get("phase") == "runner-environment"
    )
    tests: list[dict[str, object]] = []
    failed = False
    blocked = False
    for phase in phases:
        phase_tests = object_list(phase.get("tests", []))
        tests.extend(phase_tests)
        failed = (
            failed
            or phase.get("result") == "FAIL"
            or any(test.get("result") == "FAIL" for test in phase_tests)
        )
        blocked = (
            blocked
            or phase.get("result") in {"HOLD", "NOT_RUN"}
            or any(
                test.get("applicability") == "APPLICABLE"
                and test.get("result") in {"HOLD", "NOT_RUN"}
                for test in phase_tests
            )
        )

    firewall_unchanged = firewall_before == firewall_after
    tests.append(
        test_record(
            "TEST-P4-FW-001-host-filter-negative",
            ["ARCH-FW-001", "TEST-P4-FW-001"],
            "PASS" if firewall_unchanged else "FAIL",
            {
                "before_sha256": firewall_before,
                "after_sha256": firewall_after,
                "unchanged": firewall_unchanged,
                "normalization": (
                    "nft stateless view; iptables-save timestamp comments removed"
                ),
                "interpretation": (
                    "the PTF contamination test proves host forwarding rules are not "
                    "the backend; this fingerprint proves exact host cleanup"
                ),
            },
        )
    )
    failed = failed or not firewall_unchanged

    p4_profile = json.loads(
        (repo / "contracts/profiles/v1/p4-stateless-firewall-bmv2.json").read_text(
            encoding="utf-8"
        )
    )
    runner_profile = json.loads(
        (repo / "contracts/profiles/v1/e2e-runner-compose.json").read_text(
            encoding="utf-8"
        )
    )
    compiled = stable_exact_regular_files(artifacts, EXPECTED_COMPILED_ARTIFACTS)
    compiled_json = compiled["masi_switch.json"]
    artifact_checks = {
        "p4_source": sha256(repo / "p4/src/masi_switch.p4"),
        "bmv2_json": sha256_bytes(compiled_json),
        "p4info": sha256_bytes(compiled["masi_switch.p4info.txtpb"]),
        "device_config_wire": sha256_bytes(bmv2_device_config(compiled_json)),
    }
    artifacts_match = artifact_checks == p4_profile["artifact_digests"]
    tests.append(
        test_record(
            "TEST-P4-ARTIFACT-BINDING-001",
            ["CONTRACT-P4-001", "TEST-003"],
            "PASS" if artifacts_match else "FAIL",
            {
                "observed": artifact_checks,
                "expected": p4_profile["artifact_digests"],
                "exact_match": artifacts_match,
            },
        )
    )
    runner_matches = runner_image == runner_profile["runner_image_digest"]
    tests.append(
        test_record(
            "TEST-TRAFFIC-001-runner-binding",
            ["CONTRACT-TRAFFIC-001", "TEST-TRAFFIC-001"],
            "PASS" if runner_matches else "FAIL",
            {
                "observed_runner_image_digest": runner_image,
                "expected_runner_image_digest": runner_profile["runner_image_digest"],
                "fixed_build_project": runner_profile["build_project_name"],
                "source_date_epoch": runner_profile["source_date_epoch"],
            },
        )
    )
    expected_runtime_digest = str(p4_profile["target"]["runtime_image"]).split("@", 1)[
        1
    ]
    runtime_matches = runtime_image == expected_runtime_digest
    tests.append(
        test_record(
            "TEST-P4-RUNTIME-BINDING-001",
            ["CONTRACT-P4-001", "MOD-SW-001", "TEST-003"],
            "PASS" if runtime_matches else "FAIL",
            {
                "observed_runtime_image_digest": runtime_image,
                "expected_runtime_image_digest": expected_runtime_digest,
                "exact_match": runtime_matches,
            },
        )
    )
    failed = failed or not artifacts_match or not runner_matches or not runtime_matches

    try:
        findings = module_findings_summary(repo)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid P4 module findings registry: {error}") from error
    findings_gate_passed = findings["open_p0"] == 0
    tests.append(
        test_record(
            "TEST-P4-FINDINGS-001",
            ["DEC-044", "MOD-SW-001", "TEST-GATE-001"],
            "PASS" if findings_gate_passed else "FAIL",
            {
                "registry": "p4/module-findings.json",
                "registry_digest": findings["registry_digest"],
                "open_total": findings["open_total"],
                "open_p0": findings["open_p0"],
            },
        )
    )
    failed = failed or not findings_gate_passed

    cleanup_path = Path(cleanup_name).absolute()
    if cleanup_path.parent != evidence_dir or cleanup_path.name != "global-cleanup.json":
        raise SystemExit("cleanup evidence must be the direct canonical evidence child")
    cleanup = load_cleanup(cleanup_path, run_id, source_identity)
    cleanup_passed = (
        cleanup.get("result") == "PASS"
        and cleanup.get("qualification") == "QUALIFIED"
        and cleanup.get("query_error") == ""
        and cleanup.get("host_error") == ""
        and all(cleanup.get("host_checks", {}).values())
        and all(not values for values in cleanup["remaining_resources"].values())
    )
    tests.append(
        test_record(
            "TEST-P4-CLEANUP-001",
            ["MOD-SW-001", "TEST-003"],
            "PASS" if cleanup_passed else "FAIL",
            cleanup,
        )
    )
    failed = failed or not cleanup_passed

    tests.extend(
        [
            test_record(
                "TEST-P4-HARDWARE-001",
                ["TEST-P4-FW-001"],
                "NOT_RUN",
                {
                    "stable_reason": (
                        "claim scope is the exact BMv2 simple_switch_grpc software profile"
                    )
                },
                applicability="NOT_APPLICABLE",
            ),
            test_record(
                "TEST-P4-CONDITIONAL-CAPABILITIES-001",
                ["CONTRACT-P4-001"],
                "NOT_RUN",
                {
                    "stable_reason": (
                        "IPv6 effect, stateful, NAT, rate-limit, gNMI, and hardware "
                        "profiles were not activated"
                    )
                },
                applicability="NOT_APPLICABLE",
            ),
        ]
    )

    performance_phase = next(
        phase for phase in phases if phase.get("phase") == "performance"
    )
    performance = object_list(performance_phase.get("performance", []))
    runner_environment_readback: dict[str, object] = {}
    runner_environment_tests = object_list(runner_environment_phase.get("tests", []))
    if runner_environment_tests:
        evidence = runner_environment_tests[0].get("evidence", {})
        if isinstance(evidence, dict):
            observed = evidence.get("observed", {})
            if isinstance(observed, dict):
                runner_environment_readback = observed
    holds = [
        f"{test.get('id')}: result={test.get('result')} qualification={test.get('qualification')}"
        for test in tests
        if test.get("applicability") == "APPLICABLE"
        and test.get("result") in {"HOLD", "NOT_RUN"}
    ]
    overall_result = "FAIL" if failed else "HOLD" if blocked or holds else "PASS"
    completion, overall_module_complete = derive_operational_completion(tests, findings)
    if overall_result == "PASS" and not overall_module_complete:
        overall_result = "FAIL"
    json_bytes = compiled_json
    p4info_path = artifacts / "masi_switch.p4info.txtpb"
    phase_bindings = [
        {
            "phase": str(phase["phase"]),
            "run_id": phase["run_id"],
            "source_revision": phase["source_revision"],
            "source_tree_digest": phase["source_tree_digest"],
            "working_tree_dirty": phase["working_tree_dirty"],
            "working_tree_status_digest": phase["working_tree_status_digest"],
            "document_digest": phase_record[1],
        }
        for phase, phase_record in zip(phases, phase_records, strict=True)
    ]
    if {binding["phase"] for binding in phase_bindings} != set(PHASE_TEST_IDS):
        raise SystemExit("P4 phase binding identity set is incomplete")
    document = {
        "schema_version": "qualification-evidence/v1",
        "module": "p4-switch",
        "run_id": run_id,
        **source_identity,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": overall_result,
        "qualification": "QUALIFIED" if overall_result == "PASS" else "NOT_QUALIFIED",
        "claim_scope": {
            "release_id": f"p4-switch-module-e2e-{run_id}",
            "target_profile": "p4-stateless-firewall/v1",
            "runtime_profile": "simple-switch-grpc-v1model/v1",
            "availability_profile": "availability-single/v1",
            "deployment_tier": "acceptance",
            "topology_profile": "p4-traffic-replay-bmv2-compose/v1",
            "environment_profile_digest": sha256(
                repo / "contracts/profiles/v1/e2e-runner-compose.json"
            ),
            "runtime_digest": runtime_image,
            "program_digest": sha256_bytes(json_bytes),
            "p4info_digest": sha256(p4info_path),
            "device_config_digest": sha256_bytes(bmv2_device_config(json_bytes)),
            "config_digest": tree_digest(repo / "deploy/p4-switch"),
            "contract_set_digest": tree_digest(repo / "contracts"),
        },
        "environment": {
            "kernel": platform.release(),
            "machine": runner_environment_readback.get(
                "architecture", platform.machine()
            ),
            "python": runner_environment_readback.get("python", "unknown"),
            "p4runtime_python": runner_environment_readback.get("p4runtime", "unknown"),
            "grpcio": runner_environment_readback.get("grpcio", "unknown"),
            "ptf": runner_environment_readback.get("ptf", "unknown"),
            "scapy": runner_environment_readback.get("scapy", "unknown"),
            "runner_image_id": runner_image,
            "runner_environment_readback": runner_environment_readback,
            "p4_source_digest": sha256(repo / "p4/src/masi_switch.p4"),
            "compiler_image": COMPILER_IMAGE,
            "runtime_image": p4_profile["target"]["runtime_image"],
            "mininet_profile_digest": sha256(
                repo / "contracts/profiles/v1/p4-mininet-bmv2.json"
            ),
        },
        "phase_bindings": phase_bindings,
        "cleanup": cleanup,
        "tests": tests,
        "performance": performance,
        "remaining_holds": holds,
        "findings": findings,
        "completion": completion,
        "overall_module_complete": overall_module_complete,
    }

    schema = json.loads(
        (repo / "contracts/evidence/v1/schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.path))
    if errors:
        raise SystemExit("\n".join(error.message for error in errors))
    write_new_file(
        evidence_dir,
        output.name,
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
    )

    evidence_snapshot, checksum_lines = evidence_checksum_snapshot(evidence_dir)
    checksum_payload = ("\n".join(checksum_lines) + "\n").encode()
    checksum_path = write_new_file(
        evidence_dir,
        "SHA256SUMS",
        checksum_payload,
    )
    verify_evidence_tree_unchanged(evidence_dir, evidence_snapshot)
    if read_bounded_file(checksum_path) != checksum_payload:
        raise ValueError("SHA256SUMS changed after publication")
    if document["result"] == "FAIL":
        return 1
    if document["result"] in {"HOLD", "NOT_RUN"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
