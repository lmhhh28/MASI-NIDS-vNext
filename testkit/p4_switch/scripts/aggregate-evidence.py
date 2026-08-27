from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from testkit.p4_switch.lib.p4runtime_client import bmv2_device_config


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


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    paths = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if any(part in EXCLUDED_TREE_PARTS for part in relative_path.parts):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        paths.append(path)
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
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


def load_phase(path: Path, name: str) -> dict[str, object]:
    if not path.exists():
        return {
            "phase": name,
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": "FAIL",
            "qualification": "NOT_QUALIFIED",
            "tests": [
                test_record(
                    f"{name}-phase-present",
                    ["TEST-003"],
                    "FAIL",
                    {"error": f"missing phase evidence {path.name}"},
                )
            ],
        }
    return json.loads(path.read_text(encoding="utf-8"))


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
        record.get("result") == "PASS" for record in applicable_tests
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


def main() -> int:
    if len(sys.argv) != 11:
        raise SystemExit(
            "usage: aggregate-evidence.py REPO EVIDENCE ARTIFACTS RUN_ID STARTED_AT "
            "RUNNER_IMAGE RUNTIME_IMAGE FIREWALL_BEFORE FIREWALL_AFTER OUTPUT"
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
        output_name,
    ) = sys.argv[1:]
    repo = Path(repo_name)
    evidence_dir = Path(evidence_name)
    artifacts = Path(artifacts_name)
    output = Path(output_name)

    runner_environment_phase = load_phase(
        evidence_dir / "runner-environment.json", "runner-environment"
    )
    phases = [
        load_phase(evidence_dir / "unit.json", "unit"),
        runner_environment_phase,
        load_phase(evidence_dir / "compiler.json", "compiler"),
        load_phase(evidence_dir / "p4testgen.json", "p4testgen"),
        load_phase(evidence_dir / "mininet.json", "mininet"),
        load_phase(evidence_dir / "functional.json", "functional"),
        load_phase(evidence_dir / "performance.json", "performance"),
        load_phase(evidence_dir / "crash-orchestration.json", "crash-orchestration"),
        load_phase(evidence_dir / "crash-recovery.json", "crash-recovery"),
        load_phase(evidence_dir / "lifecycle.json", "lifecycle"),
        load_phase(evidence_dir / "soak.json", "soak"),
        load_phase(evidence_dir / "supply-chain.json", "supply-chain"),
    ]
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
    compiled_json = (artifacts / "masi_switch.json").read_bytes()
    artifact_checks = {
        "p4_source": sha256(repo / "p4/src/masi_switch.p4"),
        "bmv2_json": sha256_bytes(compiled_json),
        "p4info": sha256(artifacts / "masi_switch.p4info.txtpb"),
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
    document = {
        "schema_version": "qualification-evidence/v1",
        "module": "p4-switch",
        "run_id": run_id,
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
            "machine": platform.machine(),
            "python": platform.python_version(),
            "p4runtime_python": importlib.metadata.version("p4runtime"),
            "grpcio": importlib.metadata.version("grpcio"),
            "ptf": importlib.metadata.version("ptf"),
            "scapy": importlib.metadata.version("scapy"),
            "runner_image_id": runner_image,
            "runner_environment_readback": runner_environment_readback,
            "p4_source_digest": sha256(repo / "p4/src/masi_switch.p4"),
            "compiler_image": COMPILER_IMAGE,
            "runtime_image": p4_profile["target"]["runtime_image"],
            "mininet_profile_digest": sha256(
                repo / "contracts/profiles/v1/p4-mininet-bmv2.json"
            ),
        },
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
    output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    checksum_lines = []
    for path in sorted(evidence_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            checksum_lines.append(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(evidence_dir)}"
            )
    (evidence_dir / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    if document["result"] == "FAIL":
        return 1
    if document["result"] in {"HOLD", "NOT_RUN"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
