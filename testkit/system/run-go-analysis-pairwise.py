#!/usr/bin/env python3
"""Run real Control Core -> real Analysis A2A over the isolated test profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import shutil
import socket
import ssl
import subprocess
import sys
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


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 180,
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


def wait_ready(
    url: str,
    processes: list[subprocess.Popen[str]],
    *,
    tls_context: ssl.SSLContext | None = None,
    timeout: float = 30,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"process exited before readiness: {process.args}")
        try:
            with urlopen(url, timeout=1, context=tls_context) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"readiness timeout: {url}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if args.evidence.exists() or args.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    analysis_root = repo / "analysis-py"
    control_root = repo / "control-go"
    started_at = datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z")
    project = f"masi-analysis-pairwise-{os.getpid()}"
    dsn = "postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable"
    temporary = Path(tempfile.mkdtemp(prefix="masi-go-analysis-pairwise."))
    processes: list[subprocess.Popen[str]] = []
    log_handles: list[Any] = []
    try:
        sys.path.insert(0, str(analysis_root / "src"))
        try:
            support = runpy.run_path(
                str(analysis_root / "tests/support.py"),
                run_name="analysis_pairwise_support",
            )
            sys.path.insert(0, str(analysis_root))
            try:
                blackbox_support = runpy.run_path(
                    str(analysis_root / "scripts/run-blackbox.py"),
                    run_name="analysis_pairwise_pki",
                )
            finally:
                sys.path.pop(0)
        finally:
            sys.path.pop(0)
        pki_root = temporary / "pki"
        pki_root.mkdir(mode=0o700)
        pki = blackbox_support["generate_pki"](pki_root)
        provider_port, mcp_port, peer_port, analysis_port = (
            free_port(),
            free_port(),
            free_port(),
            free_port(),
        )
        runtime_root = temporary / "analysis-runtime"
        config_path = support["write_runtime"](
            runtime_root,
            repo / "contracts",
            service_port=analysis_port,
            provider_port=provider_port,
            mcp_port=mcp_port,
            peer_port=None,
            tls_files=pki,
        )
        manifest_path = runtime_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scope"] = "scope-e2e"
        manifest_raw = support["json_bytes"](manifest)
        manifest_path.write_bytes(manifest_raw)
        binding_path = runtime_root / "binding.json"
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding["scope"] = "scope-e2e"
        binding["manifest_content_digest"] = support["file_digest"](manifest_raw)
        binding["binding_digest"] = support["canonical_digest"](
            {key: value for key, value in binding.items() if key != "binding_digest"}
        )
        binding_path.write_bytes(support["go_json_bytes"](binding))

        fixture_log_path, analysis_log_path, go_log_path = (
            temporary / "fixtures.log",
            temporary / "analysis.log",
            temporary / "go-test.log",
        )
        fixture_log = fixture_log_path.open("w", encoding="utf-8")
        analysis_log = analysis_log_path.open("w", encoding="utf-8")
        log_handles.extend([fixture_log, analysis_log])
        fixture = subprocess.Popen(
            [
                str(analysis_root / ".venv/bin/python"),
                str(analysis_root / "tests/fake_neighbors.py"),
                "--provider-port",
                str(provider_port),
                "--mcp-port",
                str(mcp_port),
                "--peer-port",
                str(peer_port),
                "--cert",
                pki["server_cert"],
                "--key",
                pki["server_key"],
                "--ca",
                pki["ca"],
            ],
            cwd=analysis_root,
            stdout=fixture_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(fixture)
        analysis = subprocess.Popen(
            [
                str(analysis_root / ".venv/bin/masi-analysis"),
                "--config",
                str(config_path),
            ],
            cwd=analysis_root,
            stdout=analysis_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(analysis)
        readiness_tls = ssl.create_default_context(cafile=pki["ca"])
        readiness_tls.minimum_version = ssl.TLSVersion.TLSv1_3
        readiness_tls.maximum_version = ssl.TLSVersion.TLSv1_3
        readiness_tls.load_cert_chain(pki["control_cert"], pki["control_key"])
        wait_ready(
            f"https://localhost:{analysis_port}/health/ready",
            processes,
            tls_context=readiness_tls,
        )

        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(control_root / "testdata/compose.e2e.yaml"),
        ]
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
        control_binary = temporary / "control-core"
        run(
            [
                "go",
                "build",
                "-trimpath",
                "-o",
                str(control_binary),
                "./cmd/control-core",
            ],
            cwd=control_root,
        )

        control_config = json.loads(
            (control_root / "testdata/control-e2e-config.json").read_text(
                encoding="utf-8"
            )
        )
        control_config["runtime_profile"] = "acceptance"
        control_config["external_clients"] = "test-fake"
        control_config["public_origin"] = "http://127.0.0.1:18080"
        control_config["role_mapping_path"] = str(
            control_root / "testdata/role-mapping-e2e.json"
        )
        control_config["contract_root"] = str(repo / "contracts")
        control_config["tls"] = {
            "grpc_cert_file": pki["server_cert"],
            "grpc_key_file": pki["server_key"],
            "grpc_client_ca_file": pki["ca"],
            "grpc_allowed_client_sans": ["masi-control"],
        }
        control_config["outbound"] = {
            "analysis": [
                {
                    "peer_id": "analysis-real-pairwise",
                    "plugin_id": binding["plugin_id"],
                    "base_url": f"https://127.0.0.1:{analysis_port}",
                    "server_name": "localhost",
                    "ca_file": pki["ca"],
                    "cert_file": pki["control_cert"],
                    "key_file": pki["control_key"],
                    "allowed_ips": ["127.0.0.1"],
                    "max_response_bytes": 131072,
                }
            ]
        }
        control_config_path = temporary / "control.json"
        control_config_path.write_text(
            json.dumps(control_config, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        core_evidence_path = temporary / "core-evidence.json"
        env = os.environ.copy()
        env.update(
            {
                "MASI_REAL_ANALYSIS_REQUIRED": "1",
                "MASI_CONTROL_E2E_DSN": dsn,
                "MASI_CONTROL_E2E_CONFIG": str(control_config_path),
                "MASI_CONTROL_E2E_BINARY": str(control_binary),
                "MASI_REAL_ANALYSIS_BINDING": str(binding_path),
                "MASI_REAL_ANALYSIS_EVIDENCE": str(core_evidence_path),
            }
        )
        go_result = subprocess.run(
            ["go", "test", "-count=1", "-v", "./tests/real_analysis_pairwise"],
            cwd=control_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        go_log_path.write_text(go_result.stdout, encoding="utf-8")
        if go_result.returncode != 0:
            raise RuntimeError(
                f"real Go/Analysis test failed with {go_result.returncode}: {go_result.stdout[-2000:]}"
            )
        core_evidence = json.loads(core_evidence_path.read_text(encoding="utf-8"))
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        processes.clear()
        for handle in log_handles:
            handle.flush()

        result = {
            "schema_version": "go-analysis-pairwise-rehearsal/v1",
            "run_id": args.run_id,
            "started_at": started_at,
            "finished_at": datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z"),
            "participants": {
                "control_binary_digest": digest(control_binary),
                "analysis_binary_digest": digest(
                    analysis_root / ".venv/bin/masi-analysis"
                ),
                "postgresql": "postgresql-18-test-container",
                "binding_digest": binding["binding_digest"],
            },
            "core_evidence": core_evidence,
            "transport": {
                "protocol": "https",
                "tls_version": "TLSv1.3",
                "mutual_tls": True,
                "analysis_server_name": "localhost",
                "control_client_san": "masi-control",
                "ca_digest": digest(Path(pki["ca"])),
                "analysis_server_certificate_digest": digest(Path(pki["server_cert"])),
                "control_client_certificate_digest": digest(Path(pki["control_cert"])),
            },
            "process_logs": {
                "analysis": digest(analysis_log_path),
                "external_fixtures": digest(fixture_log_path),
                "go_test": digest(go_log_path),
            },
            "level": "REHEARSAL",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "qualification_scope": (
                "REAL_GO_CONTROL_POSTGRESQL_TO_REAL_ANALYSIS_A2A; "
                "TLS1.3_MTLS_LOOPBACK_ACCEPTANCE_AND_EXTERNAL_PROVIDER_MCP_FIXTURES; "
                "FORMAL_PRODUCTION_MTLS_NOT_CLAIMED"
            ),
        }
        schema = json.loads(
            (
                repo
                / "contracts/evidence/go-analysis-pairwise-rehearsal/v1/schema.json"
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
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        for handle in log_handles:
            handle.close()
        for name in ("fixtures.log", "analysis.log", "go-test.log"):
            source = temporary / name
            destination = args.evidence.parent / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
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
