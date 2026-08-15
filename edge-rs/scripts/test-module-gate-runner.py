#!/usr/bin/env python3
"""Deterministic negative cases for the Rust Edge module gate runner."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def invoke(
    edge_root: Path,
    evidence_base: Path,
    run_id: str,
    *,
    path_prefix: Path | None = None,
    formal_soak: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "MASI_EDGE_EVIDENCE_DIR": str(evidence_base),
            "MASI_EDGE_GATE_RUN_ID": run_id,
            "MASI_EDGE_SKIP_OCI": "1",
            "MASI_EDGE_SKIP_DEEP": "1",
            "MASI_EDGE_SKIP_SUPPLY": "1",
            "MASI_EDGE_RUNNER_SELF_TEST_CHILD": "1",
        }
    )
    if formal_soak:
        environment["MASI_EDGE_FORMAL_SOAK"] = "1"
    else:
        environment.pop("MASI_EDGE_FORMAL_SOAK", None)
    if path_prefix is not None:
        environment["PATH"] = f"{path_prefix}{os.pathsep}{environment['PATH']}"
    return subprocess.run(
        [str(edge_root / "scripts/run-module-gates.sh")],
        cwd=edge_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def invoke_supply(
    edge_root: Path, evidence_base: Path, run_id: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "MASI_EDGE_SUPPLY_EVIDENCE_DIR": str(evidence_base),
            "MASI_EDGE_SUPPLY_RUN_ID": run_id,
        }
    )
    return subprocess.run(
        [str(edge_root / "scripts/run-supply-chain.sh")],
        cwd=edge_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def require(
    condition: bool, message: str, result: subprocess.CompletedProcess[str]
) -> None:
    if condition:
        return
    output = (result.stdout + "\n" + result.stderr)[-4_000:]
    raise SystemExit(f"{message}; runner output tail:\n{output}")


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{path} is not a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    edge_root = repo / "edge-rs"

    with tempfile.TemporaryDirectory(prefix="masi-edge-runner-negative-") as directory:
        root = Path(directory)

        invalid_base = root / "invalid-run-id"
        invalid = invoke(edge_root, invalid_base, "../escape")
        require(
            invalid.returncode == 64,
            "invalid run id was not rejected with exit 64",
            invalid,
        )
        require(
            not invalid_base.exists(),
            "invalid run id created an evidence directory",
            invalid,
        )

        invalid_supply_base = root / "invalid-supply-run-id"
        invalid_supply = invoke_supply(edge_root, invalid_supply_base, "../escape")
        require(
            invalid_supply.returncode == 1,
            "invalid supply-chain run id was not rejected",
            invalid_supply,
        )
        require(
            not invalid_supply_base.exists(),
            "invalid supply-chain run id created an evidence directory",
            invalid_supply,
        )

        symlink_target = root / "symlink-target"
        symlink_target.mkdir()
        symlink_base = root / "symlink-base"
        symlink_base.symlink_to(symlink_target, target_is_directory=True)
        linked = invoke(edge_root, symlink_base, "runner-symlink-base")
        require(
            linked.returncode == 64, "symlink evidence base was not rejected", linked
        )
        require(
            not any(symlink_target.iterdir()),
            "symlink evidence base received writes",
            linked,
        )

        runs_base = root / "runs-symlink-base"
        runs_base.mkdir()
        runs_outside = root / "runs-symlink-outside"
        runs_outside.mkdir()
        (runs_base / "runs").symlink_to(runs_outside, target_is_directory=True)
        linked_runs = invoke(edge_root, runs_base, "runner-symlink-runs")
        require(
            linked_runs.returncode == 64,
            "symlink runs directory was not rejected",
            linked_runs,
        )
        require(
            not any(runs_outside.iterdir()),
            "symlink runs directory received writes",
            linked_runs,
        )

        root_base = root / "run-root-symlink-base"
        (root_base / "runs").mkdir(parents=True)
        root_outside = root / "run-root-symlink-outside"
        root_outside.mkdir()
        root_run_id = "runner-symlink-run-root"
        (root_base / "runs" / root_run_id).symlink_to(
            root_outside, target_is_directory=True
        )
        linked_root = invoke(edge_root, root_base, root_run_id)
        require(
            linked_root.returncode != 0,
            "symlink run root was not rejected",
            linked_root,
        )
        require(
            not any(root_outside.iterdir()),
            "symlink run root received writes",
            linked_root,
        )

        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        fake_cargo = fake_bin / "cargo"
        fake_cargo.write_text(
            "#!/bin/sh\n"
            'if [ "${1:-}" = fmt ]; then\n'
            "  echo injected-cargo-fmt-failure\n"
            "  exit 1\n"
            "fi\n"
            'echo fake-cargo-success "$@"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake_cargo.chmod(0o700)
        failure_base = root / "command-failure"
        failure_run_id = "runner-command-failure"
        failed = invoke(
            edge_root,
            failure_base,
            failure_run_id,
            path_prefix=fake_bin,
        )
        require(
            failed.returncode == 1,
            "injected command failure did not return exit 1",
            failed,
        )
        failure_root = failure_base / "runs" / failure_run_id
        format_sidecar = load(failure_root / "format.command.json")
        require(
            format_sidecar.get("exit_code") == 1
            and format_sidecar.get("result") == "FAIL"
            and format_sidecar.get("qualification") == "NOT_QUALIFIED"
            and format_sidecar.get("stable_reason") == "COMMAND_EXITED_FAILURE",
            "format failure sidecar is inconsistent",
            failed,
        )
        require(
            (failure_root / "soak-validator-negative-cases.command.json").is_file(),
            "runner stopped before a later command sidecar",
            failed,
        )
        require(
            (
                failure_root / "traceability-validator-negative-cases.command.json"
            ).is_file(),
            "runner stopped before final traceability negative cases",
            failed,
        )
        summary = load(failure_root / "gate-summary.json")
        require(
            summary.get("result") == "FAIL" and summary.get("overall_status") == "FAIL",
            "runner did not publish an exact FAIL summary",
            failed,
        )
        completion = summary.get("completion")
        require(
            isinstance(completion, dict)
            and completion.get("status") == "INCOMPLETE"
            and completion.get("real_startup_test_blocker_count", 0) > 0
            and "format_lint_static" in completion.get("blockers", [])
            and summary.get("overall_module_complete") is False,
            "runner did not fail closed on derived module completion",
            failed,
        )

        summary_path = failure_root / "gate-summary.json"
        original_summary = summary_path.read_bytes()
        tampered_summary = load(summary_path)
        tampered_completion = tampered_summary.get("completion")
        if not isinstance(tampered_completion, dict):
            raise SystemExit("failure fixture has no completion object to tamper")
        tampered_digests = tampered_completion.get("evidence_digests")
        if not isinstance(tampered_digests, dict):
            raise SystemExit("failure fixture has no completion evidence digests")
        tampered_digests["format"] = "sha256:" + "f" * 64
        summary_path.write_text(
            json.dumps(tampered_summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        semantic = subprocess.run(
            [
                "python3",
                str(edge_root / "scripts/validate-edge-evidence.py"),
                "--repo",
                str(repo),
                "--evidence",
                str(summary_path),
            ],
            cwd=edge_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(
            semantic.returncode != 0
            and "completion does not equal the semantic re-derivation"
            in semantic.stderr + semantic.stdout,
            "hand-edited completion digest was not rejected",
            semantic,
        )
        summary_path.write_bytes(original_summary)

        derive_command = [
            "python3",
            str(edge_root / "scripts/validate-edge-evidence.py"),
            "--repo",
            str(repo),
            "--derive-completion",
            "--run-root",
            str(failure_root),
            "--run-id",
            failure_run_id,
            "--source-tree-digest",
            str(summary["source_tree_digest"]),
            "--working-tree-status-digest",
            str(summary["working_tree_status_digest"]),
            "--working-tree-dirty",
            "true" if summary["working_tree_dirty"] else "false",
        ]
        bogus_source = derive_command.copy()
        source_argument = bogus_source.index("--source-tree-digest") + 1
        bogus_source[source_argument] = "sha256:" + "e" * 64
        source_binding = subprocess.run(
            bogus_source,
            cwd=edge_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(
            source_binding.returncode != 0
            and "source_tree_digest does not bind the checked-out source tree"
            in source_binding.stderr + source_binding.stdout,
            "declared source-tree digest was not independently recomputed",
            source_binding,
        )

        bogus_dirty = derive_command.copy()
        dirty_argument = bogus_dirty.index("--working-tree-dirty") + 1
        bogus_dirty[dirty_argument] = (
            "false" if summary["working_tree_dirty"] else "true"
        )
        dirty_binding = subprocess.run(
            bogus_dirty,
            cwd=edge_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(
            dirty_binding.returncode != 0
            and "working_tree_dirty does not match the run status snapshot"
            in dirty_binding.stderr + dirty_binding.stdout,
            "working-tree dirty flag was not derived from its snapshot",
            dirty_binding,
        )

        child_root = root / "valid-oci-child"
        child_root.mkdir()
        (child_root / "working-tree-status.txt").write_bytes(
            (failure_root / "working-tree-status.txt").read_bytes()
        )
        child = load(
            repo / "contracts/golden/evidence/edge-oci-startup-v1.json"
        )
        child_dirty = bool(summary["working_tree_dirty"])
        child.update(
            {
                "result": "HOLD" if child_dirty else "PASS",
                "qualification": "NOT_QUALIFIED" if child_dirty else "QUALIFIED",
                "qualification_reason": (
                    "DIRTY_WORKTREE_NOT_RELEASE_BASELINE"
                    if child_dirty
                    else "CLEAN_SOURCE_SNAPSHOT"
                ),
                "source_revision": summary["source_revision"],
                "source_tree_digest": summary["source_tree_digest"],
                "working_tree_dirty": child_dirty,
                "working_tree_status_digest": summary["working_tree_status_digest"],
            }
        )
        child_path = child_root / "oci-smoke-evidence.json"
        (child_root / "oci-probe.json").write_text(
            json.dumps(child["probe"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (child_root / "oci-archive-inspection.json").write_text(
            json.dumps(child["archive"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        child_path.write_text(
            json.dumps(child, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        child_command = [
            "python3",
            str(edge_root / "scripts/validate-edge-evidence.py"),
            "--repo",
            str(repo),
            "--evidence",
            str(child_path),
        ]
        child_valid = subprocess.run(
            child_command,
            cwd=edge_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(
            child_valid.returncode == 0,
            "valid operational child evidence was rejected",
            child_valid,
        )
        child["probe"]["observed_at_unix_ms"] += 1
        child_path.write_text(
            json.dumps(child, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        child_tampered = subprocess.run(
            child_command,
            cwd=edge_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(
            child_tampered.returncode != 0
            and "OCI parent does not exactly embed its probe/archive evidence"
            in child_tampered.stderr + child_tampered.stdout,
            "tampered operational child evidence was accepted",
            child_tampered,
        )

        formal_fake_bin = root / "formal-fake-bin"
        formal_fake_bin.mkdir()
        formal_fake_cargo = formal_fake_bin / "cargo"
        formal_fake_cargo.write_text(
            "#!/bin/sh\n"
            'if [ "${1:-}" = test ]; then\n'
            '  case " $* " in\n'
            '    *" --all-targets "*)\n'
            '      if [ "${MASI_EDGE_FORMAL_SOAK:-}" = 1 ]; then\n'
            "        echo formal-flag-leaked-into-ordinary-tests\n"
            "        exit 99\n"
            "      fi\n"
            "      echo ordinary-tests-formal-flag-isolated\n"
            "      ;;\n"
            "  esac\n"
            "fi\n"
            'echo fake-cargo-success "$@"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        formal_fake_cargo.chmod(0o700)
        formal_base = root / "formal-flag-isolation"
        formal_run_id = "runner-formal-flag-isolation"
        formal = invoke(
            edge_root,
            formal_base,
            formal_run_id,
            path_prefix=formal_fake_bin,
            formal_soak=True,
        )
        require(
            formal.returncode == 1,
            "formal isolation fixture did not finish as a failed synthetic run",
            formal,
        )
        formal_root = formal_base / "runs" / formal_run_id
        formal_tests_sidecar = load(formal_root / "tests.command.json")
        formal_tests_log = (formal_root / "tests.log").read_text(encoding="utf-8")
        require(
            formal_tests_sidecar.get("exit_code") == 0
            and "ordinary-tests-formal-flag-isolated" in formal_tests_log
            and "formal-flag-leaked-into-ordinary-tests" not in formal_tests_log,
            "formal flag leaked into the ordinary all-target test command",
            formal,
        )
        formal_summary = load(formal_root / "gate-summary.json")
        formal_completion = formal_summary.get("completion")
        require(
            isinstance(formal_completion, dict)
            and formal_completion.get("status") == "INCOMPLETE"
            and "formal_soak" in formal_completion.get("blockers", [])
            and formal_summary.get("overall_module_complete") is False,
            "synthetic formal failure did not block derived completion",
            formal,
        )

    print(
        json.dumps(
            {
                "schema_version": "edge-module-gate-runner-negative-cases/v1",
                "cases": [
                    "invalid-run-id",
                    "invalid-supply-chain-run-id",
                    "symlink-evidence-base",
                    "symlink-runs-directory",
                    "symlink-run-root",
                    "command-failure-final-summary",
                    "semantic-completion-tamper-rejected",
                    "source-tree-digest-independently-recomputed",
                    "working-tree-dirty-derived-from-snapshot",
                    "operational-child-evidence-dispatch",
                    "operational-child-embedding-tamper-rejected",
                    "formal-flag-isolated-to-dedicated-gate",
                ],
                "result": "PASS",
                "qualification": "QUALIFIED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
