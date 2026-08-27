#!/usr/bin/env python3
"""Export one live Analysis A2A runtime for a bounded connected rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_ready(
    url: str,
    processes: list[subprocess.Popen[str]],
    tls_context: ssl.SSLContext,
    timeout: float = 30,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(
                    f"process exited before Analysis readiness: {process.args}"
                )
        try:
            with urlopen(url, timeout=1, context=tls_context) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Analysis readiness timeout: {url}")


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def load_support(analysis_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    sys.path.insert(0, str(analysis_root / "src"))
    try:
        support = runpy.run_path(
            str(analysis_root / "tests/support.py"),
            run_name="connected_analysis_support",
        )
        sys.path.insert(0, str(analysis_root))
        try:
            pki_support = runpy.run_path(
                str(analysis_root / "scripts/run-blackbox.py"),
                run_name="connected_analysis_pki",
            )
        finally:
            sys.path.pop(0)
    finally:
        sys.path.pop(0)
    return support, pki_support


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--hold-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.hold_seconds < 60 or args.hold_seconds > 28800:
        raise SystemExit("--hold-seconds must be between 60 and 28800")
    if args.export_dir.exists() or args.export_dir.is_symlink():
        raise SystemExit("Analysis runtime export directory already exists")
    args.export_dir.mkdir(parents=True, mode=0o700)

    def terminate_exporter(_signum: int, _frame: Any) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate_exporter)

    repo = Path(__file__).resolve().parents[2]
    analysis_root = repo / "analysis-py"
    python = analysis_root / ".venv/bin/python"
    analysis_binary = analysis_root / ".venv/bin/masi-analysis"
    if not python.is_file() or not analysis_binary.is_file():
        raise SystemExit("Analysis Python 3.12 virtual environment is unavailable")

    processes: list[subprocess.Popen[str]] = []
    log_handles: list[Any] = []
    shutdown: dict[str, Any] = {
        "schema_version": "analysis-a2a-runtime-shutdown/v1",
        "consumer_signaled": False,
        "clean_shutdown": False,
    }
    try:
        support, pki_support = load_support(analysis_root)
        pki_root = args.export_dir / "pki"
        pki_root.mkdir(mode=0o700)
        pki = pki_support["generate_pki"](pki_root)
        provider_port, mcp_port, peer_port, analysis_port = (
            free_port(),
            free_port(),
            free_port(),
            free_port(),
        )
        state_root = args.export_dir / "state"
        config_path = support["write_runtime"](
            state_root,
            repo / "contracts",
            service_port=analysis_port,
            provider_port=provider_port,
            mcp_port=mcp_port,
            peer_port=None,
            tls_files=pki,
        )
        manifest_path = state_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scope"] = "scope-e2e"
        manifest_raw = support["json_bytes"](manifest)
        manifest_path.write_bytes(manifest_raw)
        binding_path = state_root / "binding.json"
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding["scope"] = "scope-e2e"
        binding["manifest_content_digest"] = support["file_digest"](manifest_raw)
        binding["binding_digest"] = support["canonical_digest"](
            {key: value for key, value in binding.items() if key != "binding_digest"}
        )
        binding_path.write_bytes(support["go_json_bytes"](binding))

        fixtures_log_path = args.export_dir / "external-fixtures.log"
        analysis_log_path = args.export_dir / "analysis.log"
        fixtures_log = fixtures_log_path.open("w", encoding="utf-8")
        analysis_log = analysis_log_path.open("w", encoding="utf-8")
        log_handles.extend((fixtures_log, analysis_log))
        fixture = subprocess.Popen(
            [
                str(python),
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
            stdout=fixtures_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(fixture)
        analysis = subprocess.Popen(
            [str(analysis_binary), "--config", str(config_path)],
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
        readiness_url = f"https://localhost:{analysis_port}/health/ready"
        wait_ready(readiness_url, processes, readiness_tls)

        consumer_done = args.export_dir / "consumer.done"
        runtime = {
            "schema_version": "analysis-a2a-runtime-export/v1",
            "state": "READY",
            "service_endpoint": f"https://127.0.0.1:{analysis_port}",
            "readiness_endpoint": readiness_url,
            "server_name": "localhost",
            "peer_id": "analysis-real-connected",
            "allowed_ips": ["127.0.0.1"],
            "max_response_bytes": 131072,
            "plugin_id": binding["plugin_id"],
            "plugin_revision": binding["plugin_revision"],
            "binding_generation": binding["binding_generation"],
            "scope": binding["scope"],
            "manifest_content_digest": binding["manifest_content_digest"],
            "config_digest": binding["config_digest"],
            "binding_digest": binding["binding_digest"],
            "tls": {
                "version": "TLSv1.3",
                "mutual_tls": True,
                "ca_path": pki["ca"],
                "control_certificate_path": pki["control_cert"],
                "control_private_key_path": pki["control_key"],
                "control_client_san": "masi-control",
                "server_certificate_digest": digest(Path(pki["server_cert"])),
                "control_certificate_digest": digest(Path(pki["control_cert"])),
                "ca_digest": digest(Path(pki["ca"])),
            },
            "artifacts": {
                "config_path": str(config_path),
                "manifest_path": str(manifest_path),
                "binding_path": str(binding_path),
            },
            "external_fixtures": {
                "provider": f"https://localhost:{provider_port}",
                "mcp": f"https://localhost:{mcp_port}",
                "peer_listener_unused": f"https://localhost:{peer_port}",
                "transport": "TLSv1.3-mTLS",
            },
            "analysis_binary_digest": digest(analysis_binary),
            "consumer_done_path": str(consumer_done),
        }
        temporary_runtime = args.export_dir / f".runtime.json.{os.getpid()}"
        temporary_runtime.write_text(
            json.dumps(runtime, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_runtime, args.export_dir / "runtime.json")

        deadline = time.monotonic() + args.hold_seconds
        while time.monotonic() < deadline and not consumer_done.is_file():
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Analysis runtime process exited while exported: {process.args}"
                    )
            time.sleep(0.1)
        if not consumer_done.is_file():
            raise RuntimeError("Analysis runtime consumer signal timeout")
        shutdown["consumer_signaled"] = True
        return 0
    finally:
        for process in reversed(processes):
            stop_process(process)
        for handle in log_handles:
            handle.close()
        shutdown["process_exit_codes"] = [process.returncode for process in processes]
        shutdown["clean_shutdown"] = shutdown["consumer_signaled"] and all(
            process.returncode in {0, -15} for process in processes
        )
        (args.export_dir / "shutdown.json").write_text(
            json.dumps(shutdown, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    raise SystemExit(main())
