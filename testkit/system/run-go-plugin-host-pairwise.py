#!/usr/bin/env python3
"""Run the real Go statistics dispatcher against real Plugin Host/Wasm."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from jsonschema import Draft202012Validator, FormatChecker

UTC_ZONE = timezone.utc  # noqa: UP017 -- root Pyright targets a pre-3.11 stdlib surface


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=True,
    )


def wait_ready(url: str, process: subprocess.Popen[str], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Plugin Host exited before readiness: {process.args}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("Plugin Host readiness timeout")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--external-dsn",
        help="Reuse an already migrated test PostgreSQL database instead of owning one",
    )
    parser.add_argument(
        "--external-control-config",
        type=Path,
        help="Control config whose PostgreSQL DSN matches --external-dsn",
    )
    parser.add_argument(
        "--preserve-database",
        action="store_true",
        help="Preserve the fixture statistics facts for a later same-topology Web oracle",
    )
    args = parser.parse_args()
    if bool(args.external_dsn) != bool(args.external_control_config):
        raise SystemExit(
            "--external-dsn and --external-control-config must be provided together"
        )
    if args.preserve_database and not args.external_dsn:
        raise SystemExit("--preserve-database requires --external-dsn")
    if args.external_control_config and not args.external_control_config.is_file():
        raise SystemExit("external Control config does not exist")
    if args.evidence.exists() or args.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    control_root = repo / "control-go"
    host_root = repo / "plugin-host-rs"
    started_at = datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z")
    temporary = Path(tempfile.mkdtemp(prefix="masi-go-plugin-host-pairwise."))
    project = f"masi-plugin-host-pairwise-{os.getpid()}"
    dsn = args.external_dsn or (
        "postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable"
    )
    control_config_path = args.external_control_config or (
        control_root / "testdata/control-e2e-config.json"
    )
    owns_postgres = False
    host: subprocess.Popen[str] | None = None
    log_handles: list[Any] = []
    try:
        go_test_binary = temporary / "go-plugin-host-pairwise.test"
        run(
            [
                "go",
                "test",
                "-c",
                "-o",
                str(go_test_binary),
                "./tests/real_plugin_host_pairwise",
            ],
            cwd=control_root,
        )
        definition_path = temporary / "definition.json"
        export_env = os.environ.copy()
        export_env["MASI_PLUGIN_DEFINITION_EXPORT"] = str(definition_path)
        run(
            [
                str(go_test_binary),
                "-test.run",
                "^TestExportDefinitionFixture$",
                "-test.v",
            ],
            cwd=control_root,
            env=export_env,
        )
        definition = json.loads(definition_path.read_text(encoding="utf-8"))

        run(
            ["cargo", "build", "--locked", "--release", "--bins"],
            cwd=host_root,
            timeout=900,
        )
        fixture_root = temporary / "host-fixture"
        fixture_root.mkdir(mode=0o700)
        fixture_env = os.environ.copy()
        fixture_env.update(
            {
                "MASI_PLUGIN_FIXTURE_EXPORT_DIR": str(fixture_root),
                "MASI_PLUGIN_FIXTURE_DEFINITION_DIGEST": definition[
                    "definition_digest"
                ],
            }
        )
        run(
            [
                "cargo",
                "test",
                "--locked",
                "--test",
                "export_statistics_fixture",
                "--",
                "--nocapture",
            ],
            cwd=host_root,
            env=fixture_env,
            timeout=600,
        )
        runtime_path = fixture_root / "runtime.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))

        host_log_path = temporary / "plugin-host.log"
        host_log = host_log_path.open("w", encoding="utf-8")
        log_handles.append(host_log)
        host_binary = host_root / "target/release/masi-plugin-host"
        hostctl_binary = host_root / "target/release/masi-plugin-hostctl"
        host = subprocess.Popen(
            [str(host_binary), "--config", runtime["config_path"]],
            cwd=host_root,
            stdout=host_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_ready(runtime["health_endpoint"] + "/readyz", host)

        hostctl_result = run(
            [
                str(hostctl_binary),
                "--endpoint",
                runtime["endpoint"],
                "--server-name",
                runtime["server_name"],
                "--ca",
                runtime["ca_path"],
                "--cert",
                runtime["manager_certificate_path"],
                "--key",
                runtime["manager_private_key_path"],
                "apply-envelope",
                "--input",
                runtime["binding_path"],
            ],
            cwd=host_root,
        )
        hostctl_log_path = temporary / "hostctl.log"
        hostctl_log_path.write_text(hostctl_result.stdout, encoding="utf-8")

        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(control_root / "testdata/compose.e2e.yaml"),
        ]
        if not args.external_dsn:
            owns_postgres = True
            run([*compose, "up", "-d", "--wait"], cwd=repo)
            run(
                [
                    "go",
                    "run",
                    "./cmd/migrate-test",
                    "--dsn",
                    dsn,
                    "--dir",
                    "../db/migrations",
                ],
                cwd=control_root,
            )
        core_evidence_path = temporary / "core-evidence.json"
        go_env = os.environ.copy()
        go_env.update(
            {
                "MASI_REAL_PLUGIN_HOST_REQUIRED": "1",
                "MASI_CONTROL_E2E_DSN": dsn,
                "MASI_CONTROL_E2E_CONFIG": str(control_config_path),
                "MASI_REAL_PLUGIN_HOST_RUNTIME": str(runtime_path),
                "MASI_REAL_PLUGIN_HOST_EVIDENCE": str(core_evidence_path),
            }
        )
        if args.preserve_database:
            go_env["MASI_PRESERVE_PLUGIN_EVIDENCE"] = "1"
        go_result = subprocess.run(
            [
                str(go_test_binary),
                "-test.run",
                "^TestRealGoDispatcherToHostWasm$",
                "-test.v",
            ],
            cwd=control_root,
            env=go_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        go_log_path = temporary / "go-test.log"
        go_log_path.write_text(go_result.stdout, encoding="utf-8")
        if go_result.returncode != 0:
            raise RuntimeError(
                f"real Go/Host test failed with {go_result.returncode}: {go_result.stdout[-3000:]}"
            )
        core_evidence = json.loads(core_evidence_path.read_text(encoding="utf-8"))

        host.terminate()
        host.wait(timeout=10)
        host = None
        for handle in log_handles:
            handle.flush()

        result = {
            "schema_version": "go-plugin-host-pairwise-rehearsal/v1",
            "run_id": args.run_id,
            "started_at": started_at,
            "finished_at": datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z"),
            "participants": {
                "go_dispatcher_binary_digest": digest(go_test_binary),
                "plugin_host_binary_digest": digest(host_binary),
                "hostctl_binary_digest": digest(hostctl_binary),
                "postgresql": "postgresql-18-test-container",
                "host_binding_envelope_digest": runtime["envelope_digest"],
                "wasm_artifact_digest": runtime["artifact_digest"],
            },
            "transport": {
                "protocol": "grpc",
                "tls_version": "TLSv1.3",
                "mutual_tls": True,
                "server_name": runtime["server_name"],
                "manager_certificate_digest": digest(
                    Path(runtime["manager_certificate_path"])
                ),
                "ca_digest": digest(Path(runtime["ca_path"])),
            },
            "core_evidence": core_evidence,
            "process_logs": {
                "plugin_host": digest(host_log_path),
                "hostctl": digest(hostctl_log_path),
                "go_test": digest(go_log_path),
            },
            "level": "REHEARSAL",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "qualification_scope": (
                "REAL_GO_DURABLE_STATISTICS_DISPATCHER_TO_REAL_PLUGIN_HOST_WASM_OVER_TLS1.3_MTLS; "
                + (
                    "SHARED_CONNECTED_TEST_DATABASE_AND_FIXTURE_BINDING; "
                    if args.external_dsn
                    else "TEST_DATABASE_AND_FIXTURE_BINDING; "
                )
                + "FORMAL_P7_P8_NOT_CLAIMED"
            ),
        }
        schema = json.loads(
            (
                repo
                / "contracts/evidence/go-plugin-host-pairwise-rehearsal/v1/schema.json"
            ).read_text(encoding="utf-8")
        )
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
                result
            ),
            key=lambda error: list(error.path),
        )
        if errors:
            raise RuntimeError(
                "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
            )
        args.evidence.write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        if host is not None and host.poll() is None:
            host.terminate()
            try:
                host.wait(timeout=10)
            except subprocess.TimeoutExpired:
                host.kill()
        for handle in log_handles:
            handle.close()
        for name in ("plugin-host.log", "hostctl.log", "go-test.log"):
            source = temporary / name
            destination = args.evidence.parent / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        if owns_postgres:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    str(control_root / "testdata/compose.e2e.yaml"),
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                cwd=repo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
