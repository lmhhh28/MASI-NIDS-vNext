from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not an object")
        result.append(value)
    return result


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def numeric(container: dict[str, object], key: str) -> float:
    value = container.get(key, 0)
    if not isinstance(value, (int, float)):
        return 0
    return float(value)


def max_metric(
    samples: list[dict[str, object]], container_name: str, key: str
) -> float:
    values = []
    for sample in samples:
        container = sample.get(container_name)
        if isinstance(container, dict) and container.get("running"):
            values.append(numeric(container, key))
    return max(values, default=0)


def first_running(
    samples: list[dict[str, object]], container_name: str
) -> dict[str, object] | None:
    for sample in samples:
        container = sample.get(container_name)
        if isinstance(container, dict) and container.get("running"):
            return container
    return None


def hint_queue_peak(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    return max(
        int(value.get("request_queue_before", 0)),
        int(value.get("response_queue_before", 0)),
        int(value.get("request_queue_after", 0)),
        int(value.get("response_queue_after", 0)),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workload = load_json(args.workload)
    resources = load_jsonl(args.resources)
    compiler = load_json(args.compiler)
    profile = load_json(
        args.repo / "contracts/profiles/v1/p4-bmv2-functional-reference.json"
    )
    environment = load_json(
        args.repo / "contracts/profiles/v1/performance-environment-p4-bmv2-wsl2.json"
    )
    thresholds = profile["resource_thresholds"]
    if not isinstance(thresholds, dict):
        raise ValueError("resource_thresholds must be an object")
    switch_threshold = thresholds["switch"]
    runner_threshold = thresholds["runner"]
    assert isinstance(switch_threshold, dict)
    assert isinstance(runner_threshold, dict)
    switch_observed = {
        "cpu_pct": max_metric(resources, "switch", "cpu_pct"),
        "rss_bytes": int(max_metric(resources, "switch", "rss_bytes")),
        "fd_count": int(max_metric(resources, "switch", "fd_count")),
        "thread_count": int(max_metric(resources, "switch", "thread_count")),
        "oom_events": int(max_metric(resources, "switch", "oom_events")),
        "restart_count": int(max_metric(resources, "switch", "restart_count")),
    }
    runner_observed = {
        "cpu_pct": max_metric(resources, "runner", "cpu_pct"),
        "rss_bytes": int(max_metric(resources, "runner", "rss_bytes")),
        "fd_count": int(max_metric(resources, "runner", "fd_count")),
        "thread_count": int(max_metric(resources, "runner", "thread_count")),
        "oom_events": int(max_metric(resources, "runner", "oom_events")),
        "restart_count": int(max_metric(resources, "runner", "restart_count")),
    }
    resource_errors = []
    for sample in resources:
        for name in ("switch", "runner"):
            container = sample.get(name)
            if isinstance(container, dict) and container.get("error"):
                resource_errors.append(f"{name}: {container['error']}")
    first_switch = first_running(resources, "switch")
    first_runner = first_running(resources, "runner")
    partitions = environment["resource_partitions"]
    assert isinstance(partitions, dict)

    def host_config_matches(
        observed: dict[str, object] | None, expected_name: str
    ) -> bool:
        if observed is None:
            return False
        config = observed.get("host_config")
        expected = partitions[expected_name]
        if not isinstance(config, dict) or not isinstance(expected, dict):
            return False
        return (
            config.get("cpuset_cpus") == expected["cpuset"]
            and config.get("memory_bytes") == expected["memory_bytes"]
            and config.get("pids_limit") == expected["pids"]
            and config.get("nano_cpus") == 4_000_000_000
        )

    warmup = workload.get("warmup", {})
    queue_peak = hint_queue_peak(
        warmup.get("supplemental_hints") if isinstance(warmup, dict) else None
    )
    matrix = workload.get("matrix", [])
    if isinstance(matrix, list):
        for row in matrix:
            if not isinstance(row, dict):
                continue
            repeats = row.get("repeats", [])
            if not isinstance(repeats, list):
                continue
            for repeat in repeats:
                if not isinstance(repeat, dict):
                    continue
                queues = repeat.get("queue_depths", {})
                if isinstance(queues, dict):
                    queue_peak = max(
                        queue_peak,
                        int(queues.get("request", 0)),
                        int(queues.get("response", 0)),
                    )
                queue_peak = max(
                    queue_peak,
                    hint_queue_peak(repeat.get("supplemental_hints")),
                )
    compiler_tests = compiler.get("tests", [])
    compile_ms = 0.0
    if isinstance(compiler_tests, list) and compiler_tests:
        first_test = compiler_tests[0]
        if isinstance(first_test, dict):
            evidence = first_test.get("evidence", {})
            if isinstance(evidence, dict):
                compile_ms = float(evidence.get("duration_ms", 0))
    resource_checks = {
        "resource_samples_present": bool(resources),
        "resource_samples_error_free": not resource_errors,
        "switch_partition_exact": host_config_matches(first_switch, "switch"),
        "runner_partition_exact": host_config_matches(first_runner, "runner"),
        "switch_cpu": switch_observed["cpu_pct"] <= switch_threshold["cpu_pct"],
        "switch_rss": switch_observed["rss_bytes"] <= switch_threshold["rss_bytes"],
        "switch_fd": switch_observed["fd_count"] <= switch_threshold["fd_count"],
        "switch_threads": switch_observed["thread_count"]
        <= switch_threshold["thread_count"],
        "runner_cpu": runner_observed["cpu_pct"] <= runner_threshold["cpu_pct"],
        "runner_rss": runner_observed["rss_bytes"] <= runner_threshold["rss_bytes"],
        "runner_fd": runner_observed["fd_count"] <= runner_threshold["fd_count"],
        "runner_threads": runner_observed["thread_count"]
        <= runner_threshold["thread_count"],
        "queue": queue_peak <= thresholds["p4rpc_entity_queue"],
        "oom": switch_observed["oom_events"] == 0
        and runner_observed["oom_events"] == 0,
        "restart": switch_observed["restart_count"] == 0
        and runner_observed["restart_count"] == 0,
        "compile": compiler.get("result") == "PASS"
        and compile_ms <= profile["operation_thresholds_ms"]["compile"],
    }
    formal = workload.get("mode") == "formal"
    raw_result = workload.get("result")
    passes = formal and raw_result == "PASS" and all(resource_checks.values())
    if raw_result == "FAIL" or any(
        not value
        for key, value in resource_checks.items()
        if key not in {"resource_samples_present"}
    ):
        result = "FAIL"
    elif passes:
        result = "PASS"
    else:
        result = "HOLD"
    qualification = "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED"
    simplified = []
    if isinstance(matrix, list):
        for row in matrix:
            if not isinstance(row, dict):
                continue
            operation = row.get("operation_max_ms", {})
            if not isinstance(operation, dict):
                operation = {}
            simplified.append(
                {
                    "rule_count": row.get("rule_count"),
                    "write_ms": operation.get("write_ms", 0),
                    "readback_ms": operation.get("readback_ms", 0),
                    "selector_flip_ms": operation.get("selector_flip_ms", 0),
                    "packet_oracle_pps": row.get("minimum_achieved_pps", 0),
                    "result": row.get("result", "FAIL"),
                    "qualification": (
                        "QUALIFIED" if row.get("result") == "PASS" else "NOT_QUALIFIED"
                    ),
                }
            )
    document = {
        "phase": "performance",
        "level": "MODULE" if formal else "REHEARSAL",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": qualification,
        "tests": [
            {
                "id": "TEST-P4-PERF-ABSOLUTE-001",
                "requirement_ids": [
                    "PERF-001",
                    "PERF-002",
                    "PERF-P4-FW-001",
                    "TEST-P4-FW-001",
                ],
                "level": "MODULE" if formal else "REHEARSAL",
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": qualification,
                "evidence": {
                    "workload_file": args.workload.name,
                    "workload_sha256": sha256(args.workload),
                    "resource_file": args.resources.name,
                    "resource_sha256": sha256(args.resources),
                    "compiler_file": args.compiler.name,
                    "compiler_sha256": sha256(args.compiler),
                    "compile_ms": compile_ms,
                    "resource_observed": {
                        "switch": switch_observed,
                        "runner": runner_observed,
                        "queue_peak": queue_peak,
                    },
                    "resource_checks": resource_checks,
                    "resource_errors": resource_errors,
                    "profile_digest": workload.get("profiles", {}).get("performance")
                    if isinstance(workload.get("profiles"), dict)
                    else None,
                    "matrix_result": [
                        {
                            "rule_count": item.get("rule_count"),
                            "result": item.get("result"),
                        }
                        for item in matrix
                        if isinstance(item, dict)
                    ],
                },
            }
        ],
        "performance": simplified,
    }
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if result == "FAIL":
        return 1
    if result == "HOLD":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
