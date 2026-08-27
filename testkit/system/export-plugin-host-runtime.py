#!/usr/bin/env python3
"""Export one live Plugin Host/Wasm runtime for a bounded connected rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen


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
            raise RuntimeError("Plugin Host exited before readiness")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("Plugin Host readiness timeout")


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--hold-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.hold_seconds < 60 or args.hold_seconds > 28800:
        raise SystemExit("--hold-seconds must be between 60 and 28800")
    if args.export_dir.exists() or args.export_dir.is_symlink():
        raise SystemExit("Plugin Host runtime export directory already exists")
    args.export_dir.mkdir(parents=True, mode=0o700)

    def terminate_exporter(_signum: int, _frame: Any) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate_exporter)
    repo = Path(__file__).resolve().parents[2]
    control_root = repo / "control-go"
    host_root = repo / "plugin-host-rs"
    host: subprocess.Popen[str] | None = None
    host_log: Any | None = None
    shutdown: dict[str, Any] = {
        "schema_version": "plugin-host-runtime-shutdown/v1",
        "consumer_signaled": False,
        "clean_shutdown": False,
    }
    try:
        go_test_binary = args.export_dir / "go-plugin-host-pairwise.test"
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
        definition_path = args.export_dir / "definition.json"
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
        fixture_root = args.export_dir / "fixture"
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
        fixture_runtime_path = fixture_root / "runtime.json"
        fixture_runtime = json.loads(fixture_runtime_path.read_text(encoding="utf-8"))

        host_binary = host_root / "target/release/masi-plugin-host"
        hostctl_binary = host_root / "target/release/masi-plugin-hostctl"
        host_log_path = args.export_dir / "plugin-host.log"
        hostctl_log_path = args.export_dir / "plugin-hostctl.log"
        host_log = host_log_path.open("w", encoding="utf-8")
        host = subprocess.Popen(
            [str(host_binary), "--config", fixture_runtime["config_path"]],
            cwd=host_root,
            stdout=host_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_ready(fixture_runtime["health_endpoint"] + "/readyz", host)
        hostctl = run(
            [
                str(hostctl_binary),
                "--endpoint",
                fixture_runtime["endpoint"],
                "--server-name",
                fixture_runtime["server_name"],
                "--ca",
                fixture_runtime["ca_path"],
                "--cert",
                fixture_runtime["manager_certificate_path"],
                "--key",
                fixture_runtime["manager_private_key_path"],
                "apply-envelope",
                "--input",
                fixture_runtime["binding_path"],
            ],
            cwd=host_root,
        )
        hostctl_log_path.write_text(hostctl.stdout, encoding="utf-8")

        consumer_done = args.export_dir / "consumer.done"
        endpoint = str(fixture_runtime["endpoint"])
        runtime = {
            "schema_version": "plugin-host-runtime-export/v1",
            "state": "READY",
            "endpoint": endpoint.removeprefix("https://"),
            "server_name": fixture_runtime["server_name"],
            "max_message_bytes": 4 << 20,
            "plugin_id": fixture_runtime["plugin_id"],
            "plugin_revision": fixture_runtime["plugin_revision"],
            "binding_generation": fixture_runtime["binding_generation"],
            "scope": fixture_runtime["scope"],
            "definition_id": fixture_runtime["definition_id"],
            "definition_digest": fixture_runtime["definition_digest"],
            "host_envelope_digest": fixture_runtime["envelope_digest"],
            "wasm_artifact_digest": fixture_runtime["artifact_digest"],
            "tls": {
                "version": "TLSv1.3",
                "mutual_tls": True,
                "ca_path": fixture_runtime["ca_path"],
                "certificate_path": fixture_runtime["manager_certificate_path"],
                "private_key_path": fixture_runtime["manager_private_key_path"],
                "ca_digest": digest(Path(fixture_runtime["ca_path"])),
                "manager_certificate_digest": digest(
                    Path(fixture_runtime["manager_certificate_path"])
                ),
            },
            "artifacts": {
                "fixture_runtime_path": str(fixture_runtime_path),
                "go_seed_test_binary": str(go_test_binary),
            },
            "go_seed_test_binary_digest": digest(go_test_binary),
            "plugin_host_binary_digest": digest(host_binary),
            "plugin_hostctl_binary_digest": digest(hostctl_binary),
            "consumer_done_path": str(consumer_done),
        }
        temporary_runtime = args.export_dir / f".runtime.json.{os.getpid()}"
        temporary_runtime.write_text(
            json.dumps(runtime, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_runtime, args.export_dir / "runtime.json")

        deadline = time.monotonic() + args.hold_seconds
        while time.monotonic() < deadline and not consumer_done.is_file():
            if host.poll() is not None:
                raise RuntimeError("Plugin Host exited while runtime was exported")
            time.sleep(0.1)
        if not consumer_done.is_file():
            raise RuntimeError("Plugin Host runtime consumer signal timeout")
        shutdown["consumer_signaled"] = True
        return 0
    finally:
        if host is not None:
            stop_process(host)
            shutdown["host_exit_code"] = host.returncode
        if host_log is not None:
            host_log.close()
        shutdown["clean_shutdown"] = (
            shutdown["consumer_signaled"]
            and host is not None
            and host.returncode in {0, -15}
        )
        (args.export_dir / "shutdown.json").write_text(
            json.dumps(shutdown, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    raise SystemExit(main())
