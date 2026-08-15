#!/usr/bin/env python3
"""Negative cases for the run-bound Edge traceability evidence validator."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def invoke(
    repo: Path, manifest: Path, evidence_root: Path, summary: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(repo / "edge-rs/scripts/validate-traceability.py"),
            "--repo",
            str(repo),
            "--manifest",
            str(manifest),
            "--evidence-root",
            str(evidence_root),
            "--summary-out",
            str(summary),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def write_json_atomic(path: Path, value: Any) -> None:
    replacement = path.with_name(f".{path.name}.replacement")
    replacement.write_text(json.dumps(value), encoding="utf-8")
    os.replace(replacement, path)


def replace_json(path: Path, mutate: Any) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    write_json_atomic(path, value)


def inject_json_tamper(value: Any) -> None:
    if isinstance(value, dict):
        value["traceability_tamper"] = True
    elif isinstance(value, list):
        value.append({"traceability_tamper": True})
    else:
        raise ValueError("supply JSON mutation requires an object or array")


def replace_with_internal_symlink(path: Path) -> None:
    target = path.with_name(f".{path.name}.symlink-target")
    os.replace(path, target)
    path.symlink_to(target.name, target_is_directory=target.is_dir())


def supply_artifact(run: Path, name: str) -> Path:
    latest_path = run / "supply-chain/latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    pointed = latest_path.parent / str(latest["evidence"])
    return pointed.parent / "supply" / name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    source_manifest = args.manifest.resolve()
    evidence_root = args.evidence_root.resolve()
    manifest: dict[str, Any] = json.loads(source_manifest.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="masi-edge-trace-validator-") as directory:
        root = Path(directory)
        if (
            invoke(repo, source_manifest, evidence_root, root / "valid.json").returncode
            != 0
        ):
            raise SystemExit(
                "traceability validator rejected the unmodified run evidence"
            )

        mutations: dict[str, Any] = {}
        missing = copy.deepcopy(manifest)
        missing["requirements"][0]["evidence_files"] = [
            "module-blackbox/does-not-exist.json"
        ]
        mutations["missing-evidence"] = missing

        traversal = copy.deepcopy(manifest)
        traversal["requirements"][0]["evidence_files"] = ["../outside.json"]
        mutations["path-traversal"] = traversal

        unrelated = copy.deepcopy(manifest)
        unrelated["requirements"][0]["scenario_ids"] = ["TEST-UNRELATED-001"]
        mutations["unrelated-test-id"] = unrelated

        catalog_schema = copy.deepcopy(manifest)
        catalog_schema["evidence_catalog"][0]["schema_version"] = "unknown-evidence/v1"
        mutations["unknown-catalog-schema"] = catalog_schema

        catalog_requirements = copy.deepcopy(manifest)
        catalog_requirements["evidence_catalog"][0]["requirement_ids"] = [
            "MOD-EDGE-001"
        ]
        mutations["catalog-requirement-drift"] = catalog_requirements

        producer_target_drift = copy.deepcopy(manifest)
        actor_catalog = next(
            item
            for item in producer_target_drift["evidence_catalog"]
            if item["path"] == "module-blackbox/actor-queue-saturation.json"
        )
        actor_catalog["producer_test_targets"] = [
            "central_pool_outage_is_bounded_and_recovers_without_fallback"
        ]
        mutations["producer-test-target-drift"] = producer_target_drift

        missing_catalog = copy.deepcopy(manifest)
        missing_catalog["evidence_catalog"] = [
            item
            for item in missing_catalog["evidence_catalog"]
            if item["path"] != "module-blackbox/security-boundaries.json"
        ]
        mutations["uncataloged-qualification-claim"] = missing_catalog

        ignored_as_passed = copy.deepcopy(manifest)
        ignored_target = "os_enospc_fails_closed_without_process_loss"
        original_target = ignored_as_passed["requirements"][0]["test_targets"][0]
        ignored_as_passed["requirements"][0]["test_targets"] = [ignored_target]
        tests_binding = next(
            item
            for item in ignored_as_passed["execution_bindings"]
            if item["command_id"] == "tests"
        )
        os_binding = next(
            item
            for item in ignored_as_passed["execution_bindings"]
            if item["command_id"] == "os-faults"
        )
        tests_binding["targets"].append(ignored_target)
        os_binding["targets"].remove(ignored_target)
        if not any(
            original_target in item["test_targets"]
            for item in ignored_as_passed["requirements"]
        ):
            tests_binding["targets"].remove(original_target)
        mutations["ignored-libtest-is-not-pass"] = ignored_as_passed

        conditional_profile_drift = copy.deepcopy(manifest)
        conditional_profile_drift["conditional_applicability"][0]["profile"] = (
            "fake-profile/v1"
        )
        mutations["conditional-profile-drift"] = conditional_profile_drift

        conditional_duplicate = copy.deepcopy(manifest)
        conditional_duplicate["conditional_applicability"][3] = copy.deepcopy(
            conditional_duplicate["conditional_applicability"][0]
        )
        mutations["conditional-profile-duplicate"] = conditional_duplicate

        conditional_reason_drift = copy.deepcopy(manifest)
        conditional_reason_drift["conditional_applicability"][1]["stable_reason"] = (
            "UNRELATED_STABLE_REASON"
        )
        mutations["conditional-reason-drift"] = conditional_reason_drift

        for name, candidate in mutations.items():
            candidate_path = root / f"{name}.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            if (
                invoke(
                    repo,
                    candidate_path,
                    evidence_root,
                    root / f"{name}-summary.json",
                ).returncode
                == 0
            ):
                raise SystemExit(
                    f"traceability validator accepted forbidden mutation: {name}"
                )

        evidence_mutations: dict[str, Any] = {
            "failed-command-sidecar": lambda run: replace_json(
                run / "tests.command.json",
                lambda item: item.update(
                    {
                        "exit_code": 1,
                        "result": "FAIL",
                        "qualification": "NOT_QUALIFIED",
                        "stable_reason": "COMMAND_EXITED_FAILURE",
                    }
                ),
            ),
            "forged-log-digest": lambda run: replace_json(
                run / "tests.command.json",
                lambda item: item["log"].__setitem__("sha256", "sha256:" + ("0" * 64)),
            ),
            "failed-json-evidence": lambda run: replace_json(
                run / "module-blackbox/module-blackbox-e2e.json",
                lambda item: item.update(
                    {"result": "FAIL", "qualification": "NOT_QUALIFIED"}
                ),
            ),
            "unknown-json-schema": lambda run: replace_json(
                run / "module-blackbox/module-blackbox-e2e.json",
                lambda item: item.__setitem__(
                    "schema_version", "edge-module-e2e-evidence/v2"
                ),
            ),
            "json-requirement-drift": lambda run: replace_json(
                run / "module-blackbox/module-blackbox-e2e.json",
                lambda item: item["requirement_ids"].pop(),
            ),
            "run-id-directory-mismatch": lambda run: replace_json(
                run / "tests.command.json",
                lambda item: item.__setitem__("run_id", "different-valid-run-id"),
            ),
            "unlisted-result-json": lambda run: (
                run / "module-blackbox/unlisted-result.json"
            ).write_text(
                json.dumps(
                    {
                        "schema_version": "edge-fault-evidence/v1",
                        "result": "PASS",
                        "qualification": "QUALIFIED",
                    }
                ),
                encoding="utf-8",
            ),
            "unlisted-nonclaim-json": lambda run: (
                run / "module-blackbox/unlisted-metadata.json"
            ).write_text(
                json.dumps(
                    {
                        "schema_version": "unbound-metadata/v1",
                        "status": "apparently-benign",
                        "digest": "sha256:" + ("0" * 64),
                    }
                ),
                encoding="utf-8",
            ),
            "unbound-command-sidecar": lambda run: shutil.copy2(
                run / "tests.command.json", run / "unknown.command.json"
            ),
            "aggregator-schema-drift": lambda run: write_json_atomic(
                run / "gate-summary.json",
                {"schema_version": "edge-module-gate-summary/v2"},
            ),
            "execution-log-symlink": lambda run: replace_with_internal_symlink(
                run / "tests.log"
            ),
            "command-sidecar-symlink": lambda run: replace_with_internal_symlink(
                run / "tests.command.json"
            ),
            "catalog-evidence-symlink": lambda run: replace_with_internal_symlink(
                run / "module-blackbox/module-blackbox-e2e.json"
            ),
        }
        if (evidence_root / "oci-smoke/oci-probe.json").is_file():
            evidence_mutations.update(
                {
                    "oci-probe-parent-mismatch": lambda run: replace_json(
                        run / "oci-smoke/oci-probe.json",
                        lambda item: item.__setitem__("traceability_tamper", True),
                    ),
                    "oci-probe-missing": lambda run: (
                        run / "oci-smoke/oci-probe.json"
                    ).unlink(),
                }
            )
        if (evidence_root / "oci-smoke/oci-archive-inspection.json").is_file():
            evidence_mutations.update(
                {
                    "oci-archive-summary-parent-mismatch": lambda run: replace_json(
                        run / "oci-smoke/oci-archive-inspection.json",
                        lambda item: item.__setitem__(
                            "archive_digest", "sha256:" + ("0" * 64)
                        ),
                    ),
                    "oci-archive-config-mismatch": lambda run: replace_json(
                        run / "oci-smoke/image-config.json",
                        lambda item: item.__setitem__("architecture", "arm64"),
                    ),
                    "oci-archive-index-missing": lambda run: (
                        run / "oci-smoke/image-archive-manifest.json"
                    ).unlink(),
                }
            )
        if (evidence_root / "supply-chain/latest.json").is_file():
            evidence_mutations["supply-latest-missing"] = lambda run: (
                run / "supply-chain/latest.json"
            ).unlink()
            evidence_mutations["supply-latest-symlink"] = lambda run: (
                replace_with_internal_symlink(run / "supply-chain/latest.json")
            )
            evidence_mutations["supply-subordinate-directory-symlink"] = lambda run: (
                replace_with_internal_symlink(
                    supply_artifact(run, "release-manifest.json").parent
                )
            )
            evidence_mutations["supply-latest-digest-mismatch"] = lambda run: (
                replace_json(
                    run / "supply-chain/latest.json",
                    lambda item: item.__setitem__("digest", "sha256:" + ("0" * 64)),
                )
            )
            image_config = supply_artifact(evidence_root, "image-config.json")
            if image_config.is_file():
                evidence_mutations["supply-subordinate-digest-mismatch"] = lambda run: (
                    replace_json(
                        supply_artifact(run, "image-config.json"),
                        inject_json_tamper,
                    )
                )
            if (evidence_root / "oci-smoke/oci-smoke-evidence.json").is_file():
                evidence_mutations["oci-supply-config-digest-drift"] = lambda run: (
                    replace_json(
                        run / "oci-smoke/oci-smoke-evidence.json",
                        lambda item: item.__setitem__(
                            "image_config_digest", "sha256:" + ("0" * 64)
                        ),
                    )
                )
        for name, mutate in evidence_mutations.items():
            case_root = root / f"case-{name}"
            run = case_root / evidence_root.name
            shutil.copytree(evidence_root, run, copy_function=os.link)
            mutate(run)
            if (
                invoke(
                    repo,
                    source_manifest,
                    run,
                    root / f"{name}-summary.json",
                ).returncode
                == 0
            ):
                raise SystemExit(
                    f"traceability validator accepted forbidden evidence mutation: {name}"
                )
            shutil.rmtree(case_root)

    print(
        json.dumps(
            {
                "valid_run": "accepted",
                "forbidden_manifest_mutations": sorted(mutations),
                "forbidden_evidence_mutations": sorted(evidence_mutations),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
