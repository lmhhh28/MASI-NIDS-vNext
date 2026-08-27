#!/usr/bin/env python3
"""Run real BMv2/P4Runtime -> real Edge with traffic-only packet injection."""

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

from jsonschema import Draft202012Validator, FormatChecker

UTC_ZONE = timezone.utc


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=check,
    )


def wait_container(
    compose: list[str],
    service: str,
    *,
    one_shot: bool,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 60,
) -> str:
    deadline = time.monotonic() + timeout
    container_id = ""
    while time.monotonic() < deadline:
        observed = run([*compose, "ps", "-aq", service], cwd=cwd, env=env)
        container_id = observed.stdout.strip()
        if container_id:
            state = run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}} {{.State.ExitCode}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                    container_id,
                ],
                cwd=cwd,
            ).stdout.strip().split()
            running = state[0] == "true"
            exit_code = int(state[1])
            health = state[2] if len(state) > 2 else ""
            if one_shot and not running:
                if exit_code != 0:
                    raise RuntimeError(f"{service} exited with {exit_code}")
                return container_id
            if not one_shot and running and health == "healthy":
                return container_id
            if not one_shot and not running:
                raise RuntimeError(f"{service} exited before healthy: {exit_code}")
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {service}: {container_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if args.evidence.exists() or args.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    edge_root = repo / "edge-rs"
    compose_file = repo / "deploy/p4-switch/compose.edge-rehearsal.yaml"
    temporary = Path(tempfile.mkdtemp(prefix="masi-edge-bmv2-pairwise."))
    cert_dir = temporary / "certs"
    runtime_dir = temporary / "runtime"
    artifacts_dir = temporary / "compiled-artifacts"
    cert_dir.mkdir(mode=0o700)
    runtime_dir.mkdir(mode=0o700)
    artifacts_dir.mkdir(mode=0o700)
    edge_tls_dir = runtime_dir / "edge-p4-tls"
    edge_tls_dir.mkdir(mode=0o700)
    edge_log_path = temporary / "edge.log"
    loader_log_path = temporary / "pipeline-loader.log"
    traffic_log_path = temporary / "traffic.log"
    switch_log_path = temporary / "bmv2.log"
    compiler_log_path = temporary / "compiler.log"
    edge_evidence_path = temporary / "edge-evidence.json"
    edge_ready_path = runtime_dir / "edge.ready"
    traffic_done_path = runtime_dir / "traffic.done"
    runtime_path = runtime_dir / "runtime.json"
    pipeline_path = runtime_dir / "pipeline.json"
    started_at = datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z")
    project = f"masiedgebmv2{os.getpid()}"
    environment = os.environ.copy()
    environment.update(
        {
            "MASI_P4_CERT_DIR": str(cert_dir),
            "MASI_P4_RUNTIME_EXPORT_DIR": str(runtime_dir),
        }
    )
    compose = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    edge_process: subprocess.Popen[str] | None = None
    edge_log_handle: Any | None = None
    try:
        run(
            [str(repo / "testkit/p4_switch/scripts/prepare-certs.sh"), str(cert_dir)],
            cwd=repo,
        )
        for source_name, destination_name, mode in (
            ("ca.crt", "ca.crt", 0o444),
            ("client.crt", "client.crt", 0o444),
            ("client.key", "client.key", 0o400),
        ):
            destination = edge_tls_dir / destination_name
            shutil.copyfile(cert_dir / source_name, destination)
            destination.chmod(mode)
        image_check = run(
            ["docker", "image", "inspect", "masi-nids/p4-switch-e2e-runner:local"],
            cwd=repo,
            check=False,
        )
        if image_check.returncode != 0:
            run([*compose, "build", "runner"], cwd=repo, env=environment, timeout=900)
        run(
            [*compose, "up", "-d", "compiler", "switch", "net-init"],
            cwd=repo,
            env=environment,
            timeout=180,
        )
        compiler_id = wait_container(
            compose, "compiler", one_shot=True, cwd=repo, env=environment
        )
        wait_container(compose, "net-init", one_shot=True, cwd=repo, env=environment)
        switch_id = wait_container(
            compose, "switch", one_shot=False, cwd=repo, env=environment
        )
        compiler_log_path.write_text(
            run(
                [*compose, "logs", "--no-color", "compiler"],
                cwd=repo,
                env=environment,
            ).stdout,
            encoding="utf-8",
        )
        run(
            ["docker", "cp", f"{compiler_id}:/artifacts/.", str(artifacts_dir)],
            cwd=repo,
        )
        loader = run(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "runner",
                "python",
                "testkit/system/load-bmv2-pipeline.py",
                "--artifacts",
                "/artifacts",
                "--cert-dir",
                "/certs",
                "--profile",
                "/workspace/contracts/profiles/v1/p4-stateless-firewall-bmv2.json",
                "--output",
                "/runtime/pipeline.json",
            ],
            cwd=repo,
            env=environment,
            timeout=120,
        )
        loader_log_path.write_text(loader.stdout, encoding="utf-8")
        pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
        port_output = run(
            ["docker", "port", switch_id, "9559/tcp"], cwd=repo
        ).stdout.strip()
        host_port = int(port_output.rsplit(":", 1)[1])
        runtime: dict[str, Any] = {
            "schema_version": "bmv2-edge-runtime-export/v1",
            "state": "READY",
            "p4runtime_endpoint": f"https://127.0.0.1:{host_port}",
            "switch_container": switch_id,
            "tls": {
                "server_name": "masi-switch",
                "client_san": "masi-p4-e2e-controller",
                "ca_path": str(edge_tls_dir / "ca.crt"),
                "client_cert_path": str(edge_tls_dir / "client.crt"),
                "client_key_path": str(edge_tls_dir / "client.key"),
            },
            "pipeline": {
                key: pipeline[key]
                for key in (
                    "p4runtime_api_version",
                    "p4info_digest",
                    "device_config_digest",
                    "profile_digest",
                    "cookie",
                    "supported_write_atomicity",
                )
            },
            "edge_ready_path": str(edge_ready_path),
            "traffic_done_path": str(traffic_done_path),
            "expected_traffic_packets": 64,
        }
        runtime_path.write_text(
            json.dumps(runtime, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        edge_env = os.environ.copy()
        edge_env.update(
            {
                "MASI_EDGE_REAL_P4_RUNTIME": str(runtime_path),
                "MASI_EDGE_REAL_P4_EVIDENCE": str(edge_evidence_path),
            }
        )
        edge_log_handle = edge_log_path.open("w", encoding="utf-8")
        edge_process = subprocess.Popen(
            [
                "cargo",
                "test",
                "--locked",
                "--release",
                "--test",
                "module_blackbox",
                "real_edge_reads_real_bmv2",
                "--",
                "--exact",
                "--nocapture",
                "--test-threads=1",
            ],
            cwd=edge_root,
            env=edge_env,
            stdout=edge_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not edge_ready_path.is_file():
            if edge_process.poll() is not None:
                raise RuntimeError(f"Edge test exited before traffic readiness: {edge_process.returncode}")
            time.sleep(0.05)
        if not edge_ready_path.is_file():
            raise RuntimeError("Edge traffic readiness timeout")
        traffic = run(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "runner",
                "python",
                "testkit/system/send-bmv2-traffic.py",
                "--count",
                "64",
            ],
            cwd=repo,
            env=environment,
            timeout=60,
        )
        traffic_log_path.write_text(traffic.stdout, encoding="utf-8")
        traffic_done_path.write_text(
            json.dumps(traffic_evidence := json.loads(traffic.stdout.strip().splitlines()[-1]), sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        edge_status = edge_process.wait(timeout=180)
        edge_process = None
        edge_log_handle.close()
        edge_log_handle = None
        if edge_status != 0:
            raise RuntimeError(f"real Edge/BMv2 test failed with {edge_status}")
        edge_evidence = json.loads(edge_evidence_path.read_text(encoding="utf-8"))
        switch_log_path.write_text(
            run(
                [*compose, "logs", "--no-color", "switch"],
                cwd=repo,
                env=environment,
            ).stdout,
            encoding="utf-8",
        )
        runtime_image = run(
            ["docker", "inspect", "--format", "{{.Image}}", switch_id], cwd=repo
        ).stdout.strip()
        edge_binary = edge_root / "target/release/masi-edge"
        result = {
            "schema_version": "edge-bmv2-pairwise-rehearsal/v1",
            "run_id": args.run_id,
            "started_at": started_at,
            "finished_at": datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z"),
            "participants": {
                "edge_binary_digest": digest(edge_binary),
                "bmv2_image_digest": runtime_image,
                "p4_source_digest": digest(repo / "p4/src/masi_switch.p4"),
                "bmv2_json_digest": digest(artifacts_dir / "masi_switch.json"),
            },
            "pipeline_loader": pipeline,
            "real_boundaries": {
                "edge_p4runtime_mtls": True,
                "edge_streamchannel_mastership": True,
                "edge_pipeline_exact_readback": True,
                "edge_telemetry_snapshot_wal": True,
            },
            "fixtures": {
                "pipeline_loader": "isolated-one-shot-closed-before-edge",
                "traffic_sender": "packet-only-no-p4runtime-credentials",
                "central_inference": "deterministic-mtls-fake",
                "control_sink": "deterministic-mtls-fake",
            },
            "traffic": traffic_evidence,
            "edge_evidence": edge_evidence,
            "process_logs": {
                "edge": digest(edge_log_path),
                "bmv2": digest(switch_log_path),
                "compiler": digest(compiler_log_path),
                "pipeline_loader": digest(loader_log_path),
                "traffic_sender": digest(traffic_log_path),
            },
            "level": "REHEARSAL",
            "applicability": "APPLICABLE",
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "qualification_scope": (
                "REAL_BMV2_P4RUNTIME_TO_REAL_EDGE_OVER_MTLS; ISOLATED_ONE_SHOT_PIPELINE_LOADER; "
                "PACKET_ONLY_TRAFFIC_SENDER; CENTRAL_AND_CONTROL_FIXTURES; FORMAL_P1_NOT_CLAIMED"
            ),
        }
        schema = json.loads(
            (repo / "contracts/evidence/edge-bmv2-pairwise-rehearsal/v1/schema.json").read_text(
                encoding="utf-8"
            )
        )
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(result),
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
        if edge_process is not None and edge_process.poll() is None:
            edge_process.terminate()
            try:
                edge_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                edge_process.kill()
        if edge_log_handle is not None:
            edge_log_handle.close()
        if not switch_log_path.is_file():
            observed = run(
                [*compose, "logs", "--no-color", "switch"],
                cwd=repo,
                env=environment,
                check=False,
            )
            switch_log_path.write_text(observed.stdout, encoding="utf-8")
        for name in (
            "edge.log",
            "bmv2.log",
            "compiler.log",
            "pipeline-loader.log",
            "traffic.log",
        ):
            source = temporary / name
            destination = args.evidence.parent / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        run(
            [*compose, "down", "--volumes", "--remove-orphans"],
            cwd=repo,
            env=environment,
            timeout=120,
            check=False,
        )
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
