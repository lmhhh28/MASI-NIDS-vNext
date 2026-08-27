#!/usr/bin/env python3
"""Run real Edge -> real Central Gateway -> real Triton/ORT acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

from jsonschema import Draft202012Validator, FormatChecker

from validate_connected_system_evidence import validate_semantics

UTC_ZONE = timezone.utc  # noqa: UP017 -- root Pyright targets a pre-3.11 stdlib surface


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed with {result.returncode}: {command[0]}: {result.stdout[-4000:]}"
        )
    return result


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as output:
        output.write(json.dumps(document, sort_keys=True, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def require_containers_running(repo: Path, containers: list[str]) -> None:
    observed = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", *containers],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
    )
    states = observed.stdout.strip().splitlines()
    if observed.returncode != 0 or states != ["true"] * len(containers):
        raise RuntimeError(
            f"connected live container health drifted: {containers}: {observed.stdout[-2000:]}"
        )


def generate_control_pki(root: Path) -> dict[str, Path]:
    root.mkdir(mode=0o700)
    ca_key, ca_cert = root / "ca.key", root / "ca.pem"
    server_key, server_csr, server_cert = (
        root / "control.key",
        root / "control.csr",
        root / "control.pem",
    )
    client_key, client_csr, client_cert = (
        root / "edge.key",
        root / "edge.csr",
        root / "edge.pem",
    )
    run_checked(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-sha256",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=MASI connected Control CA",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
        ],
        cwd=root,
    )
    run_checked(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:3072",
            "-sha256",
            "-nodes",
            "-subj",
            "/CN=control.test",
            "-addext",
            "subjectAltName=DNS:control.test,IP:127.0.0.1",
            "-keyout",
            str(server_key),
            "-out",
            str(server_csr),
        ],
        cwd=root,
    )
    server_ext = root / "server.ext"
    server_ext.write_text(
        "subjectAltName=DNS:control.test,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    run_checked(
        [
            "openssl",
            "x509",
            "-req",
            "-sha256",
            "-days",
            "2",
            "-in",
            str(server_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-extfile",
            str(server_ext),
            "-out",
            str(server_cert),
        ],
        cwd=root,
    )
    run_checked(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:3072",
            "-sha256",
            "-nodes",
            "-subj",
            "/CN=edge-e2e",
            "-addext",
            "subjectAltName=DNS:edge-e2e",
            "-keyout",
            str(client_key),
            "-out",
            str(client_csr),
        ],
        cwd=root,
    )
    client_ext = root / "client.ext"
    client_ext.write_text(
        "subjectAltName=DNS:edge-e2e\nextendedKeyUsage=clientAuth\n",
        encoding="utf-8",
    )
    run_checked(
        [
            "openssl",
            "x509",
            "-req",
            "-sha256",
            "-days",
            "2",
            "-in",
            str(client_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-extfile",
            str(client_ext),
            "-out",
            str(client_cert),
        ],
        cwd=root,
    )
    for key in (ca_key, server_key, client_key):
        key.chmod(0o400)
    return {
        "ca": ca_cert,
        "server_cert": server_cert,
        "server_key": server_key,
        "client_cert": client_cert,
        "client_key": client_key,
    }


def write_connected_role_mapping(source: Path, output: Path) -> str:
    mapping = json.loads(source.read_text(encoding="utf-8"))
    tenant_scope = {
        "scope_id": "tenant:test",
        "target_set_digest": "sha256:" + ("b" * 64),
        "effect_kinds": [
            "source-read",
            "plugin.statistics.run",
            "plugin-statistics-run",
            "plugin.statistics.manage",
        ],
        "levels": ["operator", "scoped-operator", "platform-admin", "analyst"],
    }
    actor_scopes = mapping.get("actor_scopes")
    if not isinstance(actor_scopes, dict) or not actor_scopes:
        raise RuntimeError("connected role mapping has no actor scopes")
    for scopes in actor_scopes.values():
        if not isinstance(scopes, list):
            raise RuntimeError("connected role mapping actor scopes are malformed")
        scopes.append(dict(tenant_scope))
    canonical = {
        "version": mapping["version"],
        "digest": "",
        "actor_scopes": {key: actor_scopes[key] for key in sorted(actor_scopes)},
        "default_deny": mapping["default_deny"],
    }
    computed = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(canonical, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    mapping["digest"] = computed
    output.write_text(
        json.dumps(mapping, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return computed


def wait_http_ready(
    url: str, process: subprocess.Popen[str], timeout: float = 30
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Control exited before readiness: {process.returncode}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("Control readiness timeout")


def seed_control(dsn: str, runtime: dict[str, Any]) -> None:
    readback = runtime["binding_readback"]
    now_ms = int(time.time() * 1000)
    variables = {
        "incarnation": readback["model_control_incarnation_id"],
        "pool": readback["logical_pool_id"],
        "pool_generation": readback["pool_generation"],
        "binding_generation": readback["binding_generation"],
        "model_revision_digest": readback["model_revision_digest"],
        "model_bundle_digest": readback["model_bundle_digest"],
        "feature_digest": readback["feature_contract_digest"],
        "label_digest": readback["label_contract_digest"],
        "adapter_digest": readback["output_adapter_digest"],
        "wire_digest": readback["wire_profile_digest"],
        "runtime_digest": readback["runtime_profile_digest"],
        "optimization_digest": readback["optimization_profile_digest"],
        "startup_digest": readback["startup_envelope_digest"],
        "pool_observation_digest": readback["pool_observation_digest"],
        "binding_digest": readback["binding_digest"],
        "runtime_profile": readback["runtime_profile"],
        "issued_ms": str(now_ms - 1_000),
        "expires_ms": str(now_ms + 299_000),
    }
    sql = """
BEGIN;
INSERT INTO model_revisions(model_revision_id,model_revision_digest,model_bundle_digest,
 feature_contract_digest,label_contract_digest,output_adapter_digest,qualification_status,
 qualified_at_unix_ms,reader_runtime_profile,actor_ref,trace_id,scope)
VALUES('rev-connected',:'model_revision_digest',:'model_bundle_digest',:'feature_digest',
 :'label_digest',:'adapter_digest','qualified',1,:'runtime_profile','connected-runner',
 'trace-connected-seed','scope-e2e');
INSERT INTO model_control_incarnations(incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id)
VALUES(:'incarnation','initial',1,'connected-runner','trace-connected-seed');
INSERT INTO model_control_state(singleton,active_incarnation_id,writer_enabled)
VALUES(true,:'incarnation',true)
ON CONFLICT(singleton) DO UPDATE SET active_incarnation_id=excluded.active_incarnation_id,
 writer_enabled=excluded.writer_enabled;
INSERT INTO logical_pools(logical_pool_id,current_generation,availability_profile,runtime_profile,
 actor_ref,trace_id)
VALUES(:'pool',:'pool_generation','availability-single/v1',:'runtime_profile','connected-runner',
 'trace-connected-seed');
INSERT INTO pool_generations(logical_pool_id,model_control_incarnation_id,pool_generation,
 model_revision_id,startup_envelope_digest,pool_observation_digest,binding_digest,status,
 min_ready_replicas,capacity_qualified,model_revision_digest,model_bundle_digest,
 feature_contract_digest,label_contract_digest,output_adapter_digest,wire_profile_digest,
 runtime_profile_digest,optimization_profile_digest)
VALUES(:'pool',:'incarnation',:'pool_generation','rev-connected',:'startup_digest',
 :'pool_observation_digest',:'binding_digest','active',1,true,:'model_revision_digest',
 :'model_bundle_digest',:'feature_digest',:'label_digest',:'adapter_digest',:'wire_digest',
 :'runtime_digest',:'optimization_digest');
INSERT INTO shard_bindings(shard_id,logical_pool_id,model_control_incarnation_id,current_generation,
 current_binding_generation,current_revision_id,route_epoch,resume_state,loaded,ready,cas_digest,scope)
VALUES('target-0',:'pool',:'incarnation',:'pool_generation',:'binding_generation','rev-connected',
 1,'current',true,true,:'binding_digest','scope-e2e');
INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,role,status,
 desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
VALUES('target-0','connected target 0','https://127.0.0.1:9559',1,'primary','active',
 :'binding_digest','p4-connected','scope-e2e','connected-runner','trace-connected-seed');
INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,edge_workload_ref,
 lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
 actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
VALUES('target-0',1,'target-control-module-1','edge-e2e','lease-connected-1',:'issued_ms',
 :'expires_ms',1000000,1009999,'pending-edge-runtime',1,'connected-runner',
 'trace-connected-seed','https://issuer.example','connected-admin');
COMMIT;
"""
    command = ["psql", dsn, "-v", "ON_ERROR_STOP=1"]
    for key, value in variables.items():
        command.extend(["-v", f"{key}={value}"])
    run_checked(command, cwd=Path.cwd(), timeout=60, input_text=sql)


def observe_control_event(
    dsn: str,
    output: Path,
    stop: threading.Event,
    errors: list[str],
) -> None:
    query = """
SELECT json_build_object(
 'schema_version','go-event-commit-observation/v1',
 'event_id',e.event_id,
 'event_idempotency_key',e.event_idempotency_key,
 'input_digest',e.input_digest,
 'output_digest',e.output_digest,
 'worker_id',e.worker_id,
 'worker_digest',e.worker_digest,
 'decision',e.decision,
 'commit_status',e.commit_status,
 'event_count',(SELECT count(*) FROM events),
 'incident_count',(SELECT count(*) FROM incident_projection_events p WHERE p.event_id=e.event_id),
 'incident_id',COALESCE((SELECT p.incident_id FROM incident_projection_events p
                         WHERE p.event_id=e.event_id LIMIT 1),'')
)::text
FROM events e WHERE e.shard_id='target-0'
ORDER BY e.committed_at_unix_ms,e.event_id LIMIT 1;
"""
    try:
        while not stop.is_set() and not output.exists():
            observed = subprocess.run(
                ["psql", dsn, "-At", "-v", "ON_ERROR_STOP=1", "-c", query],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
                check=False,
            )
            if observed.returncode != 0:
                errors.append(observed.stdout[-2_000:])
                return
            raw = observed.stdout.strip()
            if raw:
                document = json.loads(raw)
                temporary = output.with_name(f".{output.name}.{os.getpid()}")
                temporary.write_text(
                    json.dumps(document, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, output)
                return
            stop.wait(0.1)
    except Exception as error:
        errors.append(str(error))


def wait_compose_container(
    compose: list[str],
    service: str,
    *,
    one_shot: bool,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 60,
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = run_checked([*compose, "ps", "-aq", service], cwd=cwd, env=env)
        container_id = observed.stdout.strip()
        if container_id:
            state = (
                run_checked(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Running}} {{.State.ExitCode}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                        container_id,
                    ],
                    cwd=cwd,
                )
                .stdout.strip()
                .split()
            )
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
    raise RuntimeError(f"timed out waiting for compose service {service}")


def triton_inference_count(repo: Path, runtime: dict[str, Any]) -> int:
    tls = runtime["triton_tls"]
    command = [
        "grpcurl",
        "-cacert",
        tls["ca_path"],
        "-cert",
        tls["client_cert_path"],
        "-key",
        tls["client_key_path"],
        "-import-path",
        str(repo / "infer-cpp/proto/vendor/triton"),
        "-proto",
        "grpc_service.proto",
        "-proto",
        "model_config.proto",
        "-d",
        '{"name":"masi-ids-window-v1","version":"1"}',
        runtime["triton_host_endpoint"],
        "inference.GRPCInferenceService/ModelStatistics",
    ]
    result = subprocess.run(
        command,
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=True,
    )
    document = json.loads(result.stdout)
    statistics = document.get("modelStats") or document.get("model_stats") or []
    if len(statistics) != 1:
        raise RuntimeError(f"unexpected Triton statistics: {document}")
    raw = statistics[0].get("inferenceCount", statistics[0].get("inference_count"))
    return int(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--real-control", action="store_true")
    parser.add_argument("--real-p4", action="store_true")
    parser.add_argument("--with-web", action="store_true")
    parser.add_argument("--with-sideplanes", action="store_true")
    parser.add_argument("--keep-alive-seconds", type=int, default=0)
    args = parser.parse_args()
    args.evidence = args.evidence.resolve()

    def terminate_runner(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate_runner)
    signal.signal(signal.SIGINT, terminate_runner)
    if args.real_p4 and not args.real_control:
        raise SystemExit("--real-p4 requires --real-control for the connected graph")
    if args.with_web and not args.real_p4:
        raise SystemExit("--with-web requires the fully connected --real-p4 graph")
    if args.with_sideplanes and not args.with_web:
        raise SystemExit("--with-sideplanes requires --with-web")
    if args.keep_alive_seconds and not args.with_sideplanes:
        raise SystemExit(
            "--keep-alive-seconds requires the full --with-sideplanes graph"
        )
    if args.keep_alive_seconds and not 60 <= args.keep_alive_seconds <= 21600:
        raise SystemExit("--keep-alive-seconds must be in 60..21600")
    if args.evidence.exists() or args.evidence.is_symlink():
        raise SystemExit("evidence output already exists")
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    keep_alive = args.keep_alive_seconds > 0
    live_manifest_path = args.evidence.parent / "live-runtime.json"
    stop_token_path = args.evidence.parent / "stop-live-runtime"
    if keep_alive and (
        live_manifest_path.exists()
        or live_manifest_path.is_symlink()
        or stop_token_path.exists()
        or stop_token_path.is_symlink()
    ):
        raise SystemExit("live runtime manifest and stop token paths must be fresh")
    internal_hold_seconds = (
        max(3600, args.keep_alive_seconds + 1200) if keep_alive else 3600
    )
    central_hold_seconds = args.keep_alive_seconds + 1200 if keep_alive else 600
    repo = Path(__file__).resolve().parents[2]
    edge_root = repo / "edge-rs"
    inference_root = repo / "infer-cpp"
    control_root = repo / "control-go"
    temporary = Path(tempfile.mkdtemp(prefix="masi-edge-central-pairwise."))
    runtime_export = temporary / "runtime"
    central_evidence = temporary / "central-evidence"
    runtime_export.mkdir(mode=0o700)
    central_evidence.mkdir(mode=0o700)
    central_log_path = temporary / "central.log"
    edge_log_path = temporary / "edge.log"
    gateway_log_path = temporary / "gateway.log"
    triton_log_path = temporary / "triton.log"
    edge_evidence_path = temporary / "edge-evidence.json"
    control_log_path = temporary / "control.log"
    control_event_path = temporary / "control-event.json"
    control_runtime_path = temporary / "control-runtime.json"
    p4_log_path = temporary / "bmv2.log"
    p4_loader_log_path = temporary / "p4-loader.log"
    p4_traffic_log_path = temporary / "p4-traffic.log"
    p4_compiler_log_path = temporary / "p4-compiler.log"
    started_at = datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z")
    central: subprocess.Popen[str] | None = None
    edge_process: subprocess.Popen[str] | None = None
    edge_log_handle: Any | None = None
    control_process: subprocess.Popen[str] | None = None
    control_log: Any | None = None
    observer_stop = threading.Event()
    observer_errors: list[str] = []
    observer: threading.Thread | None = None
    pg_project = f"masi-edge-central-control-{os.getpid()}"
    pg_compose = [
        "docker",
        "compose",
        "-p",
        pg_project,
        "-f",
        str(control_root / "testdata/compose.e2e.yaml"),
    ]
    pg_started = False
    p4_started = False
    p4_pipeline: dict[str, Any] | None = None
    p4_traffic: dict[str, Any] | None = None
    p4_project = f"masi-connected-p4-{os.getpid()}"
    p4_compose_file = repo / "deploy/p4-switch/compose.edge-rehearsal.yaml"
    p4_compose = ["docker", "compose", "-p", p4_project, "-f", str(p4_compose_file)]
    p4_environment = os.environ.copy()
    p4_runtime_path: Path | None = None
    web_port = free_port() if args.with_web else 0
    forwarder_port = free_port() if args.with_web else 0
    while args.with_web and forwarder_port == web_port:
        forwarder_port = free_port()
    web_image = f"masi-nids/web:connected-{os.getpid()}"
    web_network = f"masi-connected-web-{os.getpid()}"
    web_container = f"masi-connected-web-{os.getpid()}"
    forwarder_container = f"masi-connected-forwarder-{os.getpid()}"
    web_started = False
    web_log_path = temporary / "web.log"
    forwarder_log_path = temporary / "web-forwarder.log"
    web_image_digest = ""
    browser_observations: list[dict[str, Any]] = []
    plugin_statistics_evidence: dict[str, Any] | None = None
    plugin_runtime: dict[str, Any] | None = None
    plugin_shutdown: dict[str, Any] | None = None
    plugin_export_process: subprocess.Popen[str] | None = None
    plugin_export_log: Any | None = None
    plugin_export_dir = temporary / "plugin-host-runtime-export"
    plugin_export_log_path = temporary / "plugin-host-export.log"
    plugin_go_log_path = temporary / "plugin-host-go-test.log"
    plugin_core_evidence_path = temporary / "plugin-host-core-evidence.json"
    analysis_evidence: dict[str, Any] | None = None
    analysis_runtime: dict[str, Any] | None = None
    analysis_shutdown: dict[str, Any] | None = None
    analysis_export_process: subprocess.Popen[str] | None = None
    analysis_export_log: Any | None = None
    analysis_export_dir = temporary / "analysis-runtime-export"
    analysis_export_log_path = temporary / "analysis-export.log"
    analysis_go_log_path = temporary / "analysis-go-test.log"
    analysis_core_evidence_path = temporary / "analysis-core-evidence.json"
    live_runtime_document: dict[str, Any] | None = None
    live_stop_reason = "runner-failure"
    run_completed = False
    central_log = central_log_path.open("w", encoding="utf-8")
    try:
        central_env = os.environ.copy()
        central_env.update(
            {
                "MASI_INF_EXPORT_RUNTIME_DIR": str(runtime_export),
                "MASI_INF_HOLD_SECONDS": str(central_hold_seconds),
                "MASI_INF_EVIDENCE_DIR": str(central_evidence),
                "MASI_INF_IMAGE_REF": f"masi-inference:edge-central-{os.getpid()}",
                "MASI_INF_BUILDER_IMAGE_REF": f"masi-inference-builder:edge-central-{os.getpid()}",
            }
        )
        central = subprocess.Popen(
            [str(inference_root / "scripts/run-oci-smoke.sh")],
            cwd=inference_root,
            env=central_env,
            stdout=central_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        runtime_path = runtime_export / "runtime.json"
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline and not runtime_path.is_file():
            if central.poll() is not None:
                raise RuntimeError(
                    f"Central runtime exited before export: {central.returncode}"
                )
            time.sleep(0.2)
        if not runtime_path.is_file():
            raise RuntimeError("Central runtime export timeout")
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        before_count = triton_inference_count(repo, runtime)

        if args.real_control:
            dsn = (
                "postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable"
            )
            pg_started = True
            run_checked([*pg_compose, "up", "-d", "--wait"], cwd=repo, timeout=120)
            run_checked(
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
                timeout=180,
            )
            if args.with_sideplanes:
                plugin_export_log = plugin_export_log_path.open("w", encoding="utf-8")
                plugin_export_process = subprocess.Popen(
                    [
                        "python3",
                        str(repo / "testkit/system/export-plugin-host-runtime.py"),
                        "--export-dir",
                        str(plugin_export_dir),
                        "--hold-seconds",
                        str(internal_hold_seconds),
                    ],
                    cwd=repo,
                    stdout=plugin_export_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            control_binary = temporary / "control-core"
            run_checked(
                [
                    "go",
                    "build",
                    "-trimpath",
                    "-o",
                    str(control_binary),
                    "./cmd/control-core",
                ],
                cwd=control_root,
                timeout=180,
            )
            if args.with_sideplanes:
                plugin_runtime_path = plugin_export_dir / "runtime.json"
                plugin_deadline = time.monotonic() + 1200
                while (
                    time.monotonic() < plugin_deadline
                    and not plugin_runtime_path.is_file()
                ):
                    if (
                        plugin_export_process is None
                        or plugin_export_process.poll() is not None
                    ):
                        if plugin_export_log is not None:
                            plugin_export_log.flush()
                        raise RuntimeError(
                            "Plugin Host runtime exporter exited before readiness: "
                            + plugin_export_log_path.read_text(encoding="utf-8")[
                                -4_000:
                            ]
                        )
                    time.sleep(0.2)
                if not plugin_runtime_path.is_file():
                    raise RuntimeError("Plugin Host runtime export timeout")
                plugin_runtime = json.loads(
                    plugin_runtime_path.read_text(encoding="utf-8")
                )
                if (
                    plugin_runtime.get("schema_version")
                    != "plugin-host-runtime-export/v1"
                    or plugin_runtime.get("state") != "READY"
                    or plugin_runtime.get("scope") != "tenant:test"
                    or plugin_runtime.get("tls", {}).get("mutual_tls") is not True
                ):
                    raise RuntimeError("Plugin Host runtime identity or TLS drifted")
            pki = generate_control_pki(temporary / "control-pki")
            http_port, grpc_port = free_port(), free_port()
            control_config = json.loads(
                (control_root / "testdata/control-e2e-config.json").read_text(
                    encoding="utf-8"
                )
            )
            role_mapping_path = control_root / "testdata/role-mapping-e2e.json"
            role_mapping_digest = str(control_config["role_mapping_digest"])
            if args.with_sideplanes:
                analysis_export_log = analysis_export_log_path.open(
                    "w", encoding="utf-8"
                )
                analysis_export_process = subprocess.Popen(
                    [
                        str(repo / "analysis-py/.venv/bin/python"),
                        str(repo / "testkit/system/export-analysis-runtime.py"),
                        "--export-dir",
                        str(analysis_export_dir),
                        "--hold-seconds",
                        str(internal_hold_seconds),
                    ],
                    cwd=repo,
                    stdout=analysis_export_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                analysis_runtime_path = analysis_export_dir / "runtime.json"
                analysis_deadline = time.monotonic() + 60
                while (
                    time.monotonic() < analysis_deadline
                    and not analysis_runtime_path.is_file()
                ):
                    if analysis_export_process.poll() is not None:
                        analysis_export_log.flush()
                        raise RuntimeError(
                            "Analysis runtime exporter exited before readiness: "
                            + analysis_export_log_path.read_text(encoding="utf-8")[
                                -4_000:
                            ]
                        )
                    time.sleep(0.1)
                if not analysis_runtime_path.is_file():
                    raise RuntimeError("Analysis runtime export timeout")
                analysis_runtime = json.loads(
                    analysis_runtime_path.read_text(encoding="utf-8")
                )
                if (
                    analysis_runtime.get("schema_version")
                    != "analysis-a2a-runtime-export/v1"
                    or analysis_runtime.get("state") != "READY"
                    or analysis_runtime.get("scope") != "scope-e2e"
                    or analysis_runtime.get("tls", {}).get("mutual_tls") is not True
                ):
                    raise RuntimeError(
                        "Analysis runtime export identity or TLS drifted"
                    )
                role_mapping_path = temporary / "connected-role-mapping.json"
                role_mapping_digest = write_connected_role_mapping(
                    control_root / "testdata/role-mapping-e2e.json",
                    role_mapping_path,
                )
            control_config.update(
                {
                    "runtime_profile": "acceptance",
                    "external_clients": "test-fake",
                    "http_listen": f"127.0.0.1:{http_port}",
                    "grpc_listen": f"127.0.0.1:{grpc_port}",
                    "public_origin": (
                        f"http://127.0.0.1:{web_port}"
                        if args.with_web
                        else f"http://127.0.0.1:{http_port}"
                    ),
                    "role_mapping_path": str(role_mapping_path),
                    "role_mapping_digest": role_mapping_digest,
                    "contract_root": str(repo / "contracts"),
                    "tls": {
                        "grpc_cert_file": str(pki["server_cert"]),
                        "grpc_key_file": str(pki["server_key"]),
                        "grpc_client_ca_file": str(pki["ca"]),
                        "grpc_allowed_client_sans": ["edge-e2e"],
                    },
                    "outbound": {
                        "plugin_statistics": (
                            {
                                "endpoint": plugin_runtime["endpoint"],
                                "server_name": plugin_runtime["server_name"],
                                "ca_file": plugin_runtime["tls"]["ca_path"],
                                "cert_file": plugin_runtime["tls"]["certificate_path"],
                                "key_file": plugin_runtime["tls"]["private_key_path"],
                                "max_message_bytes": plugin_runtime[
                                    "max_message_bytes"
                                ],
                            }
                            if plugin_runtime is not None
                            else {}
                        ),
                        "analysis": (
                            [
                                {
                                    "peer_id": analysis_runtime["peer_id"],
                                    "plugin_id": analysis_runtime["plugin_id"],
                                    "base_url": analysis_runtime["service_endpoint"],
                                    "server_name": analysis_runtime["server_name"],
                                    "ca_file": analysis_runtime["tls"]["ca_path"],
                                    "cert_file": analysis_runtime["tls"][
                                        "control_certificate_path"
                                    ],
                                    "key_file": analysis_runtime["tls"][
                                        "control_private_key_path"
                                    ],
                                    "allowed_ips": analysis_runtime["allowed_ips"],
                                    "max_response_bytes": analysis_runtime[
                                        "max_response_bytes"
                                    ],
                                }
                            ]
                            if analysis_runtime is not None
                            else []
                        ),
                    },
                }
            )
            control_config_path = temporary / "control.json"
            control_config_path.write_text(
                json.dumps(control_config, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            seed_control(dsn, runtime)
            control_log = control_log_path.open("w", encoding="utf-8")
            control_process = subprocess.Popen(
                [str(control_binary), "--config", str(control_config_path)],
                cwd=control_root,
                stdout=control_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            wait_http_ready(f"http://127.0.0.1:{http_port}/readyz", control_process)
            control_runtime = {
                "schema_version": "control-core-acceptance-runtime/v1",
                "state": "READY",
                "http_endpoint": f"http://127.0.0.1:{http_port}",
                "grpc_endpoint": f"https://127.0.0.1:{grpc_port}",
                "tls": {
                    "server_name": "control.test",
                    "client_san": "edge-e2e",
                    "ca_path": str(pki["ca"]),
                    "client_cert_path": str(pki["client_cert"]),
                    "client_key_path": str(pki["client_key"]),
                },
                "event_evidence_path": str(control_event_path),
            }
            control_runtime_path.write_text(
                json.dumps(control_runtime, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            observer = threading.Thread(
                target=observe_control_event,
                args=(dsn, control_event_path, observer_stop, observer_errors),
                daemon=True,
            )
            observer.start()

        if args.real_p4:
            p4_cert_dir = temporary / "p4-certs"
            p4_runtime_dir = temporary / "p4-runtime"
            p4_artifacts_dir = temporary / "p4-artifacts"
            p4_edge_tls_dir = p4_runtime_dir / "edge-p4-tls"
            for directory in (
                p4_cert_dir,
                p4_runtime_dir,
                p4_artifacts_dir,
                p4_edge_tls_dir,
            ):
                directory.mkdir(mode=0o700)
            p4_environment.update(
                {
                    "MASI_P4_CERT_DIR": str(p4_cert_dir),
                    "MASI_P4_RUNTIME_EXPORT_DIR": str(p4_runtime_dir),
                }
            )
            run_checked(
                [
                    str(repo / "testkit/p4_switch/scripts/prepare-certs.sh"),
                    str(p4_cert_dir),
                ],
                cwd=repo,
            )
            for source_name, destination_name, mode in (
                ("ca.crt", "ca.crt", 0o444),
                ("client.crt", "client.crt", 0o444),
                ("client.key", "client.key", 0o400),
            ):
                destination = p4_edge_tls_dir / destination_name
                shutil.copyfile(p4_cert_dir / source_name, destination)
                destination.chmod(mode)
            image_check = subprocess.run(
                ["docker", "image", "inspect", "masi-nids/p4-switch-e2e-runner:local"],
                cwd=repo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if image_check.returncode != 0:
                run_checked(
                    [*p4_compose, "build", "runner"],
                    cwd=repo,
                    env=p4_environment,
                    timeout=900,
                )
            p4_started = True
            run_checked(
                [*p4_compose, "up", "-d", "compiler", "switch", "net-init"],
                cwd=repo,
                env=p4_environment,
                timeout=180,
            )
            compiler_id = wait_compose_container(
                p4_compose, "compiler", one_shot=True, cwd=repo, env=p4_environment
            )
            wait_compose_container(
                p4_compose, "net-init", one_shot=True, cwd=repo, env=p4_environment
            )
            switch_id = wait_compose_container(
                p4_compose, "switch", one_shot=False, cwd=repo, env=p4_environment
            )
            p4_compiler_log_path.write_text(
                run_checked(
                    [*p4_compose, "logs", "--no-color", "compiler"],
                    cwd=repo,
                    env=p4_environment,
                ).stdout,
                encoding="utf-8",
            )
            run_checked(
                ["docker", "cp", f"{compiler_id}:/artifacts/.", str(p4_artifacts_dir)],
                cwd=repo,
            )
            loader = run_checked(
                [
                    *p4_compose,
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
                env=p4_environment,
                timeout=120,
            )
            p4_loader_log_path.write_text(loader.stdout, encoding="utf-8")
            p4_pipeline = json.loads(
                (p4_runtime_dir / "pipeline.json").read_text(encoding="utf-8")
            )
            host_port = int(
                run_checked(["docker", "port", switch_id, "9559/tcp"], cwd=repo)
                .stdout.strip()
                .rsplit(":", 1)[1]
            )
            p4_edge_ready_path = p4_runtime_dir / "edge.ready"
            p4_traffic_done_path = p4_runtime_dir / "traffic.done"
            p4_runtime_path = p4_runtime_dir / "runtime.json"
            p4_runtime_path.write_text(
                json.dumps(
                    {
                        "schema_version": "bmv2-edge-runtime-export/v1",
                        "state": "READY",
                        "p4runtime_endpoint": f"https://127.0.0.1:{host_port}",
                        "switch_container": switch_id,
                        "tls": {
                            "server_name": "masi-switch",
                            "client_san": "masi-p4-e2e-controller",
                            "ca_path": str(p4_edge_tls_dir / "ca.crt"),
                            "client_cert_path": str(p4_edge_tls_dir / "client.crt"),
                            "client_key_path": str(p4_edge_tls_dir / "client.key"),
                        },
                        "pipeline": {
                            key: p4_pipeline[key]
                            for key in (
                                "p4runtime_api_version",
                                "p4info_digest",
                                "device_config_digest",
                                "profile_digest",
                                "cookie",
                                "supported_write_atomicity",
                            )
                        },
                        "edge_ready_path": str(p4_edge_ready_path),
                        "traffic_done_path": str(p4_traffic_done_path),
                        "expected_traffic_packets": 64,
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        edge_env = os.environ.copy()
        edge_env.update(
            {
                "MASI_EDGE_REAL_CENTRAL_RUNTIME": str(runtime_path),
                "MASI_EDGE_REAL_CENTRAL_EVIDENCE": str(edge_evidence_path),
            }
        )
        if args.real_control:
            edge_env["MASI_EDGE_REAL_CONTROL_RUNTIME"] = str(control_runtime_path)
        if p4_runtime_path is not None:
            edge_env["MASI_EDGE_REAL_P4_RUNTIME"] = str(p4_runtime_path)
        if keep_alive:
            edge_env.update(
                {
                    "MASI_EDGE_KEEP_ALIVE_TOKEN": str(stop_token_path),
                    "MASI_EDGE_KEEP_ALIVE_SECONDS": str(internal_hold_seconds),
                }
            )
        edge_command = [
            "cargo",
            "test",
            "--locked",
            "--release",
            "--test",
            "module_blackbox",
            "real_edge_routes_to_real_central_triton",
            "--",
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ]
        if args.real_p4:
            edge_log_handle = edge_log_path.open("w", encoding="utf-8")
            edge_process = subprocess.Popen(
                edge_command,
                cwd=edge_root,
                env=edge_env,
                text=True,
                stdout=edge_log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            edge_ready_deadline = time.monotonic() + 900
            while (
                time.monotonic() < edge_ready_deadline
                and not p4_edge_ready_path.is_file()
            ):
                if edge_process.poll() is not None:
                    raise RuntimeError(
                        f"connected Edge exited before traffic readiness: {edge_process.returncode}"
                    )
                time.sleep(0.05)
            if not p4_edge_ready_path.is_file():
                raise RuntimeError("connected Edge traffic readiness timeout")
            traffic_result = run_checked(
                [
                    *p4_compose,
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
                env=p4_environment,
                timeout=60,
            )
            p4_traffic_log_path.write_text(traffic_result.stdout, encoding="utf-8")
            p4_traffic = json.loads(traffic_result.stdout.strip().splitlines()[-1])
            p4_traffic_done_path.write_text(
                json.dumps(p4_traffic, sort_keys=True) + "\n", encoding="utf-8"
            )
            if keep_alive:
                edge_evidence_deadline = time.monotonic() + 180
                while (
                    time.monotonic() < edge_evidence_deadline
                    and not edge_evidence_path.is_file()
                ):
                    if edge_process.poll() is not None:
                        raise RuntimeError(
                            f"connected Edge exited before evidence readiness: {edge_process.returncode}"
                        )
                    time.sleep(0.05)
                if not edge_evidence_path.is_file():
                    raise RuntimeError("connected Edge evidence readiness timeout")
                edge_status = 0
                edge_log_handle.flush()
            else:
                edge_status = edge_process.wait(timeout=180)
                edge_process = None
                edge_log_handle.close()
                edge_log_handle = None
            edge_stdout = edge_log_path.read_text(encoding="utf-8")
            edge_result = subprocess.CompletedProcess(
                edge_command, edge_status, stdout=edge_stdout, stderr=None
            )
            p4_log_path.write_text(
                run_checked(
                    [*p4_compose, "logs", "--no-color", "switch"],
                    cwd=repo,
                    env=p4_environment,
                ).stdout,
                encoding="utf-8",
            )
        else:
            edge_result = subprocess.run(
                edge_command,
                cwd=edge_root,
                env=edge_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=900,
                check=False,
            )
            edge_log_path.write_text(edge_result.stdout, encoding="utf-8")
        for container, log_path in (
            (runtime["gateway_container"], gateway_log_path),
            (runtime["triton_container"], triton_log_path),
        ):
            observed = subprocess.run(
                ["docker", "logs", container],
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            log_path.write_text(observed.stdout, encoding="utf-8")
        after_count = triton_inference_count(repo, runtime)
        if edge_result.returncode != 0:
            raise RuntimeError(
                "real Edge/Central test failed with "
                f"{edge_result.returncode}; Triton inference_count "
                f"{before_count}->{after_count}: {edge_result.stdout[-4000:]}"
            )
        edge_evidence = json.loads(edge_evidence_path.read_text(encoding="utf-8"))
        if after_count <= before_count:
            raise RuntimeError(
                f"Triton inference_count did not grow: {before_count}->{after_count}"
            )
        control_event: dict[str, Any] | None = None
        if args.real_control:
            observer_stop.set()
            if observer is not None:
                observer.join(timeout=5)
            if observer_errors:
                raise RuntimeError(f"Control Event observer failed: {observer_errors}")
            control_event = json.loads(control_event_path.read_text(encoding="utf-8"))
            if control_log is not None:
                control_log.flush()

        if args.with_sideplanes:
            if analysis_runtime is None or plugin_runtime is None:
                raise RuntimeError(
                    "connected side planes require live Host and Analysis runtimes"
                )
            plugin_env = os.environ.copy()
            plugin_env.update(
                {
                    "MASI_REAL_PLUGIN_HOST_REQUIRED": "1",
                    "MASI_CONTROL_E2E_DSN": dsn,
                    "MASI_CONTROL_E2E_CONFIG": str(control_config_path),
                    "MASI_REAL_PLUGIN_HOST_RUNTIME": plugin_runtime["artifacts"][
                        "fixture_runtime_path"
                    ],
                    "MASI_REAL_PLUGIN_HOST_EVIDENCE": str(plugin_core_evidence_path),
                    "MASI_EXTERNAL_STATISTICS_DISPATCHER": "1",
                    "MASI_PRESERVE_PLUGIN_EVIDENCE": "1",
                }
            )
            plugin_result = subprocess.run(
                [
                    plugin_runtime["artifacts"]["go_seed_test_binary"],
                    "-test.run",
                    "^TestRealGoDispatcherToHostWasm$",
                    "-test.v",
                ],
                cwd=control_root,
                env=plugin_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=False,
            )
            plugin_go_log_path.write_text(plugin_result.stdout, encoding="utf-8")
            if plugin_result.returncode != 0:
                raise RuntimeError(
                    "control-core statistics dispatcher test failed with "
                    f"{plugin_result.returncode}: {plugin_result.stdout[-4_000:]}"
                )
            plugin_core_evidence = json.loads(
                plugin_core_evidence_path.read_text(encoding="utf-8")
            )
            plugin_statistics_evidence = {
                "schema_version": "go-plugin-host-control-process-evidence/v1",
                "participants": {
                    "control_binary_digest": digest(control_binary),
                    "go_seed_test_binary_digest": plugin_runtime[
                        "go_seed_test_binary_digest"
                    ],
                    "plugin_host_binary_digest": plugin_runtime[
                        "plugin_host_binary_digest"
                    ],
                    "plugin_hostctl_binary_digest": plugin_runtime[
                        "plugin_hostctl_binary_digest"
                    ],
                    "wasm_artifact_digest": plugin_runtime["wasm_artifact_digest"],
                },
                "transport": {
                    "protocol": "grpc",
                    "tls_version": plugin_runtime["tls"]["version"],
                    "mutual_tls": plugin_runtime["tls"]["mutual_tls"],
                    "server_name": plugin_runtime["server_name"],
                    "ca_digest": plugin_runtime["tls"]["ca_digest"],
                    "manager_certificate_digest": plugin_runtime["tls"][
                        "manager_certificate_digest"
                    ],
                },
                "core_evidence": plugin_core_evidence,
                "level": "REHEARSAL",
                "applicability": "APPLICABLE",
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "qualification_scope": (
                    "RUNNING_CONTROL_CORE_MAINTENANCE_DISPATCHER_TO_REAL_PLUGIN_HOST_WASM; "
                    "SHARED_TEST_DATABASE_AND_FIXTURE_BINDING; FORMAL_P7_P8_NOT_CLAIMED"
                ),
            }
            analysis_env = os.environ.copy()
            analysis_env.update(
                {
                    "MASI_REAL_ANALYSIS_REQUIRED": "1",
                    "MASI_CONTROL_E2E_DSN": dsn,
                    "MASI_CONTROL_E2E_CONFIG": str(control_config_path),
                    "MASI_REAL_CONTROL_BASE_URL": f"http://127.0.0.1:{http_port}",
                    "MASI_REAL_CONTROL_ORIGIN": f"http://127.0.0.1:{web_port}",
                    "MASI_REAL_ANALYSIS_BINDING": analysis_runtime["artifacts"][
                        "binding_path"
                    ],
                    "MASI_REAL_ANALYSIS_EVIDENCE": str(analysis_core_evidence_path),
                }
            )
            analysis_result = subprocess.run(
                [
                    "go",
                    "test",
                    "-count=1",
                    "-run",
                    "^TestRealControlToRealAnalysisA2A$",
                    "-v",
                    "./tests/real_analysis_pairwise",
                ],
                cwd=control_root,
                env=analysis_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
                check=False,
            )
            analysis_go_log_path.write_text(analysis_result.stdout, encoding="utf-8")
            if analysis_result.returncode != 0:
                raise RuntimeError(
                    "same-topology Go/Analysis test failed with "
                    f"{analysis_result.returncode}: {analysis_result.stdout[-4_000:]}"
                )
            analysis_evidence = json.loads(
                analysis_core_evidence_path.read_text(encoding="utf-8")
            )

        if args.with_web:
            if not control_event or not control_event.get("incident_id"):
                raise RuntimeError(
                    "connected Web requires one projected incident identity"
                )
            run_checked(
                [
                    "docker",
                    "build",
                    "--pull=false",
                    "-f",
                    str(repo / "web/Dockerfile"),
                    "-t",
                    web_image,
                    str(repo),
                ],
                cwd=repo,
                timeout=900,
            )
            web_image_digest = run_checked(
                ["docker", "image", "inspect", "--format", "{{.Id}}", web_image],
                cwd=repo,
            ).stdout.strip()
            forwarder_config = temporary / "web-forwarder.conf"
            forwarder_config.write_text(
                "server {\n"
                f"  listen 127.0.0.1:{forwarder_port};\n"
                "  server_name _;\n"
                "  server_tokens off;\n"
                "  location / {\n"
                f"    proxy_pass http://127.0.0.1:{http_port};\n"
                "    proxy_http_version 1.1;\n"
                "    proxy_set_header Host $http_host;\n"
                "    proxy_set_header X-Forwarded-Proto $scheme;\n"
                "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "    proxy_buffering off;\n"
                "    proxy_cache off;\n"
                "    proxy_read_timeout 65s;\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            web_nginx_source = (repo / "web/nginx.conf").read_text(encoding="utf-8")
            if (
                web_nginx_source.count("listen 8080;") != 1
                or web_nginx_source.count("listen [::]:8080;") != 1
                or web_nginx_source.count("http://control-core:8080") != 2
            ):
                raise RuntimeError("Web nginx runtime template drifted")
            web_runtime_config = temporary / "web-runtime.conf"
            web_runtime_config.write_text(
                web_nginx_source.replace(
                    "listen 8080;", f"listen 127.0.0.1:{web_port};"
                )
                .replace("listen [::]:8080;", f"listen [::1]:{web_port};")
                .replace(
                    "http://control-core:8080",
                    f"http://127.0.0.1:{forwarder_port}",
                ),
                encoding="utf-8",
            )
            web_started = True
            run_checked(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    forwarder_container,
                    "--network",
                    "host",
                    "-v",
                    f"{forwarder_config}:/etc/nginx/conf.d/default.conf:ro",
                    "docker.io/nginxinc/nginx-unprivileged:1.29.5-alpine@sha256:42a7d7f2ee23e9f5a1dcdf3647ba5c585bbd18f79e79cd817e70e8cd61c55779",
                ],
                cwd=repo,
            )
            run_checked(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    web_container,
                    "--network",
                    "host",
                    "-v",
                    f"{web_runtime_config}:/etc/nginx/conf.d/default.conf:ro",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=16m",
                    "--tmpfs",
                    "/var/cache/nginx:rw,noexec,nosuid,size=16m",
                    "--tmpfs",
                    "/var/run:rw,noexec,nosuid,size=1m",
                    "--health-cmd",
                    f"wget -q -O - http://127.0.0.1:{web_port}/healthz",
                    "--health-interval",
                    "5s",
                    "--health-timeout",
                    "2s",
                    "--health-start-period",
                    "5s",
                    "--health-retries",
                    "3",
                    web_image,
                ],
                cwd=repo,
            )
            web_url = f"http://127.0.0.1:{web_port}"
            web_deadline = time.monotonic() + 60
            while time.monotonic() < web_deadline:
                try:
                    with urlopen(f"{web_url}/healthz", timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(0.2)
            else:
                raise RuntimeError("connected production Web readiness timeout")
            web_api_deadline = time.monotonic() + 30
            while time.monotonic() < web_api_deadline:
                try:
                    urlopen(f"{web_url}/api/session", timeout=2)
                    raise RuntimeError(
                        "anonymous Web session probe unexpectedly authenticated"
                    )
                except HTTPError as error:
                    if error.code == 401:
                        break
                    time.sleep(0.2)
                except Exception:
                    time.sleep(0.2)
            else:
                raise RuntimeError("connected production Web API reachability timeout")
            screenshot_root = repo / "output/playwright" / args.run_id
            screenshot_root.mkdir(parents=True, exist_ok=False)
            for browser in ("chromium", "firefox", "webkit"):
                browser_evidence_path = temporary / f"web-{browser}.json"
                screenshot_path = screenshot_root / f"connected-{browser}.png"
                web_oracle_command = [
                    "node",
                    str(repo / "testkit/system/connected-web-oracle.mjs"),
                    "--browser",
                    browser,
                    "--web-base-url",
                    web_url,
                    "--control-base-url",
                    f"http://127.0.0.1:{http_port}",
                    "--event-id",
                    str(control_event["event_id"]),
                    "--decision",
                    str(control_event["decision"]),
                    "--incident-id",
                    str(control_event["incident_id"]),
                    "--screenshot",
                    str(screenshot_path),
                    "--evidence",
                    str(browser_evidence_path),
                ]
                if args.with_sideplanes:
                    if plugin_statistics_evidence is None or analysis_evidence is None:
                        raise RuntimeError("side-plane Web identities are unavailable")
                    web_oracle_command.extend(
                        [
                            "--statistics-definition-id",
                            str(
                                plugin_statistics_evidence["core_evidence"][
                                    "definition_id"
                                ]
                            ),
                            "--statistics-artifact-id",
                            str(
                                plugin_statistics_evidence["core_evidence"][
                                    "artifact_id"
                                ]
                            ),
                            "--analysis-task-id",
                            str(analysis_evidence["task_id"]),
                            "--analysis-artifact-id",
                            str(analysis_evidence["artifact_id"]),
                        ]
                    )
                run_checked(
                    web_oracle_command,
                    cwd=repo,
                    timeout=120,
                )
                browser_observations.append(
                    json.loads(browser_evidence_path.read_text(encoding="utf-8"))
                )
            web_log_path.write_text(
                run_checked(["docker", "logs", web_container], cwd=repo).stdout,
                encoding="utf-8",
            )
            forwarder_log_path.write_text(
                run_checked(["docker", "logs", forwarder_container], cwd=repo).stdout,
                encoding="utf-8",
            )

        if keep_alive:
            if (
                edge_process is None
                or central is None
                or control_process is None
                or plugin_export_process is None
                or analysis_export_process is None
                or p4_runtime_path is None
                or plugin_runtime is None
                or analysis_runtime is None
            ):
                raise RuntimeError("full connected live lifecycle state is incomplete")
            ready_time = datetime.now(UTC_ZONE)
            expires_time = ready_time + timedelta(seconds=args.keep_alive_seconds)
            p4_runtime = json.loads(p4_runtime_path.read_text(encoding="utf-8"))
            live_runtime_document = {
                "schema_version": "connected-system-live-runtime/v1",
                "state": "READY",
                "run_id": args.run_id,
                "started_at": started_at,
                "ready_at": ready_time.isoformat().replace("+00:00", "Z"),
                "expires_at": expires_time.isoformat().replace("+00:00", "Z"),
                "keep_alive_seconds": args.keep_alive_seconds,
                "runner_pid": os.getpid(),
                "stop_token_path": str(stop_token_path),
                "endpoints": {
                    "web": web_url,
                    "control_http": f"http://127.0.0.1:{http_port}",
                    "control_grpc": f"https://127.0.0.1:{grpc_port}",
                    "postgresql": "postgresql://127.0.0.1:55433/masi_control_test",
                    "p4runtime": p4_runtime["p4runtime_endpoint"],
                    "central_gateway": runtime["gateway_endpoint"],
                    "triton": runtime["triton_host_endpoint"],
                    "plugin_host": plugin_runtime["endpoint"],
                    "analysis": analysis_runtime["service_endpoint"],
                },
                "processes": {
                    "edge_test_runner": edge_process.pid,
                    "control": control_process.pid,
                    "central_runner": central.pid,
                    "plugin_host_exporter": plugin_export_process.pid,
                    "analysis_exporter": analysis_export_process.pid,
                },
                "containers": {
                    "bmv2": switch_id,
                    "gateway": runtime["gateway_container"],
                    "triton": runtime["triton_container"],
                    "web": web_container,
                    "web_forwarder": forwarder_container,
                },
                "fixtures": {
                    "pipeline_loader": "isolated-one-shot-closed-before-live-state",
                    "traffic_sender": "packet-only-no-p4runtime-credentials-closed-before-live-state",
                    "analysis_provider": "deterministic-tls13-mtls-fixture",
                    "analysis_mcp": "deterministic-tls13-mtls-fixture",
                },
                "level": "REHEARSAL",
                "qualification": "NOT_QUALIFIED",
            }
            live_schema = json.loads(
                (
                    repo
                    / "contracts/evidence/connected-system-live-runtime/v1/schema.json"
                ).read_text(encoding="utf-8")
            )

            def publish_live_runtime() -> None:
                if live_runtime_document is None:
                    raise RuntimeError("live runtime document is unavailable")
                live_errors = sorted(
                    Draft202012Validator(
                        live_schema, format_checker=FormatChecker()
                    ).iter_errors(live_runtime_document),
                    key=lambda error: list(error.path),
                )
                if live_errors:
                    raise RuntimeError(
                        "; ".join(
                            f"{list(error.path)}: {error.message}"
                            for error in live_errors
                        )
                    )
                write_json_atomic(live_manifest_path, live_runtime_document)

            publish_live_runtime()
            print(
                json.dumps(
                    {
                        "schema_version": "connected-system-live-runtime/v1",
                        "state": "READY",
                        "web": web_url,
                        "manifest": str(live_manifest_path),
                        "stop_token": str(stop_token_path),
                        "expires_at": live_runtime_document["expires_at"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            live_deadline = time.monotonic() + args.keep_alive_seconds
            next_health_check = 0.0
            live_processes = {
                "edge": edge_process,
                "control": control_process,
                "central": central,
                "plugin-host": plugin_export_process,
                "analysis": analysis_export_process,
            }
            live_containers = [
                switch_id,
                runtime["gateway_container"],
                runtime["triton_container"],
                web_container,
                forwarder_container,
            ]
            while True:
                if stop_token_path.is_file() and not stop_token_path.is_symlink():
                    live_stop_reason = "requested"
                    break
                if stop_token_path.exists() or stop_token_path.is_symlink():
                    raise RuntimeError("live stop token must be a regular file")
                dead_processes = [
                    name
                    for name, process in live_processes.items()
                    if process.poll() is not None
                ]
                if dead_processes:
                    raise RuntimeError(
                        f"connected live process exited: {', '.join(dead_processes)}"
                    )
                now = time.monotonic()
                if now >= live_deadline:
                    stop_token_path.touch(mode=0o600, exist_ok=False)
                    live_stop_reason = "bounded-expiry"
                    break
                if now >= next_health_check:
                    require_containers_running(repo, live_containers)
                    with urlopen(f"{web_url}/healthz", timeout=2) as response:
                        if response.status != 200:
                            raise RuntimeError("connected live Web health drifted")
                    with urlopen(
                        f"http://127.0.0.1:{http_port}/readyz", timeout=2
                    ) as response:
                        if response.status != 200:
                            raise RuntimeError(
                                "connected live Control readiness drifted"
                            )
                    run_checked(
                        ["psql", dsn, "-At", "-v", "ON_ERROR_STOP=1", "-c", "SELECT 1"],
                        cwd=repo,
                        timeout=10,
                    )
                    next_health_check = now + 5
                time.sleep(0.5)
            live_runtime_document["state"] = "STOPPING"
            live_runtime_document["stop_reason"] = live_stop_reason
            live_runtime_document["stopping_at"] = (
                datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z")
            )
            publish_live_runtime()
            edge_status = edge_process.wait(timeout=30)
            edge_process = None
            if edge_log_handle is not None:
                edge_log_handle.flush()
                edge_log_handle.close()
                edge_log_handle = None
            if edge_status != 0:
                raise RuntimeError(
                    "connected Edge keep-alive shutdown failed with "
                    f"{edge_status}: {edge_log_path.read_text(encoding='utf-8')[-4000:]}"
                )

        if args.with_sideplanes:
            if (
                analysis_runtime is None
                or analysis_export_process is None
                or plugin_runtime is None
                or plugin_export_process is None
            ):
                raise RuntimeError("side-plane runtime lifecycle state is unavailable")
            Path(plugin_runtime["consumer_done_path"]).touch(mode=0o600, exist_ok=False)
            plugin_status = plugin_export_process.wait(timeout=30)
            plugin_export_process = None
            if plugin_status != 0:
                raise RuntimeError(f"Plugin Host runtime exporter exit={plugin_status}")
            if plugin_export_log is not None:
                plugin_export_log.flush()
            plugin_shutdown = json.loads(
                (plugin_export_dir / "shutdown.json").read_text(encoding="utf-8")
            )
            if not plugin_shutdown.get("clean_shutdown"):
                raise RuntimeError("Plugin Host runtime did not shut down cleanly")
            Path(analysis_runtime["consumer_done_path"]).touch(
                mode=0o600, exist_ok=False
            )
            analysis_status = analysis_export_process.wait(timeout=30)
            analysis_export_process = None
            if analysis_status != 0:
                raise RuntimeError(f"Analysis runtime exporter exit={analysis_status}")
            if analysis_export_log is not None:
                analysis_export_log.flush()
            analysis_shutdown = json.loads(
                (analysis_export_dir / "shutdown.json").read_text(encoding="utf-8")
            )
            if not analysis_shutdown.get("clean_shutdown"):
                raise RuntimeError("Analysis runtime did not shut down cleanly")

        Path(runtime["consumer_done_path"]).touch(mode=0o600, exist_ok=False)
        central_status = central.wait(timeout=180)
        central = None
        if central_status not in {0, 2}:
            raise RuntimeError(f"Central OCI runner exit={central_status}")
        central_log.flush()
        central_summary = json.loads(
            (central_evidence / "oci-smoke-evidence.json").read_text(encoding="utf-8")
        )
        edge_binary = edge_root / "target/release/masi-edge"
        profile = json.loads(
            (repo / "contracts/profiles/v1/central-inference-cpu.json").read_text(
                encoding="utf-8"
            )
        )
        participants = {
            "edge_binary_digest": digest(edge_binary),
            "gateway_image_manifest_digest": runtime["image_manifest_digest"],
            "triton_image_digest": profile["triton"]["image_digest"],
            "startup_envelope_digest": runtime["startup_envelope_digest"],
        }
        process_logs = {
            "edge": digest(edge_log_path),
            "central": digest(central_log_path),
            "gateway": digest(gateway_log_path),
            "triton": digest(triton_log_path),
        }
        if args.real_p4:
            participants.update(
                {
                    "control_binary_digest": digest(temporary / "control-core"),
                    "bmv2_image_digest": run_checked(
                        ["docker", "inspect", "--format", "{{.Image}}", switch_id],
                        cwd=repo,
                    ).stdout.strip(),
                    "p4_source_digest": digest(repo / "p4/src/masi_switch.p4"),
                    "bmv2_json_digest": digest(p4_artifacts_dir / "masi_switch.json"),
                }
            )
            process_logs.update(
                {
                    "control": digest(control_log_path),
                    "bmv2": digest(p4_log_path),
                    "p4_compiler": digest(p4_compiler_log_path),
                    "p4_loader": digest(p4_loader_log_path),
                    "p4_traffic": digest(p4_traffic_log_path),
                }
            )
            result = {
                "schema_version": "connected-detection-graph-rehearsal/v1",
                "run_id": args.run_id,
                "started_at": started_at,
                "finished_at": datetime.now(UTC_ZONE)
                .isoformat()
                .replace("+00:00", "Z"),
                "participants": participants,
                "real_boundaries": {
                    "bmv2_edge_p4runtime_mtls": True,
                    "edge_streamchannel_mastership": True,
                    "edge_gateway_infer_mtls": True,
                    "gateway_triton_model_infer": True,
                    "edge_control_sink_mtls": True,
                    "control_postgresql_event_commit": True,
                    "postgresql_commit_edge_ack": True,
                },
                "fixtures": {
                    "pipeline_loader": "isolated-one-shot-closed-before-edge",
                    "traffic_sender": "packet-only-no-p4runtime-credentials",
                },
                "pipeline_loader": p4_pipeline,
                "traffic": p4_traffic,
                "triton_statistics": {
                    "inference_count_before": before_count,
                    "inference_count_after": after_count,
                    "inference_count_delta": after_count - before_count,
                },
                "edge_evidence": edge_evidence,
                "event_evidence": control_event,
                "central_oci_result": central_summary["result"],
                "process_logs": process_logs,
                "level": "REHEARSAL",
                "applicability": "APPLICABLE",
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "qualification_scope": (
                    "REAL_BMV2_TO_EDGE_TO_GATEWAY_TO_TRITON_ORT_TO_GO_POSTGRESQL_OVER_MTLS; "
                    "ISOLATED_ONE_SHOT_PIPELINE_LOADER_AND_PACKET_SENDER; "
                    "FORMAL_PAIRWISE_SYSTEM_AND_PRODUCTION_NOT_CLAIMED"
                ),
            }
            if args.with_web:
                participants["web_image_digest"] = web_image_digest
                process_logs["web"] = digest(web_log_path)
                process_logs["web_forwarder"] = digest(forwarder_log_path)
                result["schema_version"] = "connected-detection-web-rehearsal/v1"
                result["real_boundaries"]["go_web_same_origin"] = True
                result["real_boundaries"]["browser_event_incident_readback"] = True
                result["web_observations"] = browser_observations
                result["qualification_scope"] = (
                    "REAL_BMV2_TO_EDGE_TO_GATEWAY_TO_TRITON_ORT_TO_GO_POSTGRESQL_TO_PRODUCTION_WEB; "
                    "THREE_PLAYWRIGHT_ENGINES; ISOLATED_ONE_SHOT_PIPELINE_LOADER_AND_PACKET_SENDER; "
                    "FORMAL_SYSTEM_AND_PRODUCTION_NOT_CLAIMED"
                )
                if args.with_sideplanes:
                    if (
                        plugin_statistics_evidence is None
                        or plugin_runtime is None
                        or plugin_shutdown is None
                        or analysis_evidence is None
                        or analysis_runtime is None
                        or analysis_shutdown is None
                    ):
                        raise RuntimeError(
                            "connected side-plane evidence is incomplete"
                        )
                    participants.update(
                        {
                            "statistics_go_dispatcher_binary_digest": plugin_statistics_evidence[
                                "participants"
                            ]["control_binary_digest"],
                            "plugin_host_binary_digest": plugin_statistics_evidence[
                                "participants"
                            ]["plugin_host_binary_digest"],
                            "wasm_artifact_digest": plugin_statistics_evidence[
                                "participants"
                            ]["wasm_artifact_digest"],
                            "analysis_binary_digest": analysis_runtime[
                                "analysis_binary_digest"
                            ],
                        }
                    )
                    process_logs.update(
                        {
                            "plugin_host": digest(
                                plugin_export_dir / "plugin-host.log"
                            ),
                            "plugin_hostctl": digest(
                                plugin_export_dir / "plugin-hostctl.log"
                            ),
                            "plugin_host_export": digest(plugin_export_log_path),
                            "plugin_go_test": digest(plugin_go_log_path),
                            "analysis": digest(analysis_export_dir / "analysis.log"),
                            "analysis_external_fixtures": digest(
                                analysis_export_dir / "external-fixtures.log"
                            ),
                            "analysis_export": digest(analysis_export_log_path),
                            "analysis_go_test": digest(analysis_go_log_path),
                        }
                    )
                    result["schema_version"] = "connected-system-web-rehearsal/v1"
                    result["real_boundaries"].update(
                        {
                            "go_statistics_dispatcher_plugin_host_mtls": True,
                            "plugin_statistics_postgresql_web_readback": True,
                            "go_analysis_a2a_mtls": True,
                            "analysis_postgresql_web_readback": True,
                        }
                    )
                    result["fixtures"].update(
                        {
                            "analysis_provider": "deterministic-tls13-mtls-fixture",
                            "analysis_mcp": "deterministic-tls13-mtls-fixture",
                        }
                    )
                    result["plugin_statistics_evidence"] = plugin_statistics_evidence
                    result["plugin_runtime"] = {
                        "schema_version": plugin_runtime["schema_version"],
                        "state": plugin_runtime["state"],
                        "plugin_id": plugin_runtime["plugin_id"],
                        "binding_generation": plugin_runtime["binding_generation"],
                        "host_envelope_digest": plugin_runtime["host_envelope_digest"],
                        "wasm_artifact_digest": plugin_runtime["wasm_artifact_digest"],
                        "tls_version": plugin_runtime["tls"]["version"],
                        "mutual_tls": plugin_runtime["tls"]["mutual_tls"],
                        "ca_digest": plugin_runtime["tls"]["ca_digest"],
                        "manager_certificate_digest": plugin_runtime["tls"][
                            "manager_certificate_digest"
                        ],
                        "clean_shutdown": plugin_shutdown["clean_shutdown"],
                    }
                    result["analysis_evidence"] = analysis_evidence
                    result["analysis_runtime"] = {
                        "schema_version": analysis_runtime["schema_version"],
                        "state": analysis_runtime["state"],
                        "plugin_id": analysis_runtime["plugin_id"],
                        "binding_generation": analysis_runtime["binding_generation"],
                        "binding_digest": analysis_runtime["binding_digest"],
                        "tls_version": analysis_runtime["tls"]["version"],
                        "mutual_tls": analysis_runtime["tls"]["mutual_tls"],
                        "server_certificate_digest": analysis_runtime["tls"][
                            "server_certificate_digest"
                        ],
                        "control_certificate_digest": analysis_runtime["tls"][
                            "control_certificate_digest"
                        ],
                        "ca_digest": analysis_runtime["tls"]["ca_digest"],
                        "clean_shutdown": analysis_shutdown["clean_shutdown"],
                    }
                    result["qualification_scope"] = (
                        "REAL_BMV2_EDGE_CENTRAL_TRITON_GO_POSTGRESQL_PLUGIN_HOST_WASM_ANALYSIS_AND_PRODUCTION_WEB; "
                        "THREE_PLAYWRIGHT_ENGINES; EXTERNAL_PROVIDER_MCP_AND_ISOLATED_PIPELINE_TRAFFIC_FIXTURES; "
                        "FORMAL_SYSTEM_AND_PRODUCTION_NOT_CLAIMED"
                    )
        elif args.real_control:
            participants["control_binary_digest"] = digest(temporary / "control-core")
            process_logs["control"] = digest(control_log_path)
            result = {
                "schema_version": "edge-central-control-pairwise-rehearsal/v1",
                "run_id": args.run_id,
                "started_at": started_at,
                "finished_at": datetime.now(UTC_ZONE)
                .isoformat()
                .replace("+00:00", "Z"),
                "participants": participants,
                "real_boundaries": {
                    "edge_gateway_get_binding": True,
                    "edge_gateway_infer": True,
                    "gateway_triton_model_infer": True,
                    "edge_control_sink_mtls": True,
                    "control_postgresql_event_commit": True,
                    "postgresql_commit_edge_ack": True,
                },
                "fixtures": {"p4runtime": "deterministic-mtls-fake"},
                "triton_statistics": {
                    "inference_count_before": before_count,
                    "inference_count_after": after_count,
                    "inference_count_delta": after_count - before_count,
                },
                "edge_evidence": edge_evidence,
                "event_evidence": control_event,
                "central_oci_result": central_summary["result"],
                "process_logs": process_logs,
                "level": "REHEARSAL",
                "applicability": "APPLICABLE",
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "qualification_scope": (
                    "REAL_EDGE_TO_REAL_GATEWAY_TO_TRITON_ORT_AND_REAL_GO_POSTGRESQL_OVER_MTLS; "
                    "DETERMINISTIC_P4_FIXTURE; FORMAL_P2_P3_P5_P6_NOT_CLAIMED"
                ),
            }
        else:
            result = {
                "schema_version": "edge-central-pairwise-rehearsal/v1",
                "run_id": args.run_id,
                "started_at": started_at,
                "finished_at": datetime.now(UTC_ZONE)
                .isoformat()
                .replace("+00:00", "Z"),
                "participants": participants,
                "real_boundaries": {
                    "edge_gateway_get_binding": True,
                    "edge_gateway_infer": True,
                    "gateway_triton_model_infer": True,
                    "edge_control_canonical_ack": True,
                },
                "fixtures": {
                    "p4runtime": "deterministic-mtls-fake",
                    "control_sink": "deterministic-mtls-fake",
                },
                "triton_statistics": {
                    "inference_count_before": before_count,
                    "inference_count_after": after_count,
                    "inference_count_delta": after_count - before_count,
                },
                "edge_evidence": edge_evidence,
                "central_oci_result": central_summary["result"],
                "process_logs": process_logs,
                "level": "REHEARSAL",
                "applicability": "APPLICABLE",
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "qualification_scope": (
                    "REAL_EDGE_TO_REAL_GATEWAY_TO_REAL_TRITON_ORT_OVER_MTLS; "
                    "DETERMINISTIC_P4_AND_CONTROL_FIXTURES; FORMAL_P2_P3_NOT_CLAIMED"
                ),
            }
        schema = json.loads(
            (
                repo
                / (
                    "contracts/evidence/connected-system-web-rehearsal/v1/schema.json"
                    if args.with_sideplanes
                    else (
                        "contracts/evidence/connected-detection-web-rehearsal/v1/schema.json"
                        if args.with_web
                        else (
                            "contracts/evidence/connected-detection-graph-rehearsal/v1/schema.json"
                            if args.real_p4
                            else (
                                "contracts/evidence/edge-central-control-pairwise-rehearsal/v1/schema.json"
                                if args.real_control
                                else "contracts/evidence/edge-central-pairwise-rehearsal/v1/schema.json"
                            )
                        )
                    )
                )
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
        if args.with_sideplanes:
            validate_semantics(result)
        args.evidence.write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        run_completed = True
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        if edge_process is not None:
            try:
                os.killpg(edge_process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                edge_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(edge_process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    edge_process.kill()
                edge_process.wait(timeout=5)
        if edge_log_handle is not None:
            edge_log_handle.close()
        if web_started:
            if not web_log_path.is_file():
                observed = subprocess.run(
                    ["docker", "logs", web_container],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                    check=False,
                )
                web_log_path.write_text(observed.stdout, encoding="utf-8")
            if not forwarder_log_path.is_file():
                observed = subprocess.run(
                    ["docker", "logs", forwarder_container],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                    check=False,
                )
                forwarder_log_path.write_text(observed.stdout, encoding="utf-8")
            subprocess.run(
                ["docker", "rm", "-f", web_container, forwarder_container],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
            subprocess.run(
                ["docker", "network", "rm", web_network],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        if analysis_export_process is not None:
            if analysis_runtime is not None:
                try:
                    analysis_done = Path(analysis_runtime["consumer_done_path"])
                    if not analysis_done.exists():
                        analysis_done.touch(mode=0o600)
                except OSError:
                    pass
                try:
                    analysis_export_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            if analysis_export_process.poll() is None:
                analysis_export_process.terminate()
                try:
                    analysis_export_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    analysis_export_process.kill()
                    analysis_export_process.wait(timeout=5)
        if analysis_export_log is not None:
            analysis_export_log.close()
        if plugin_export_process is not None:
            if plugin_runtime is not None:
                try:
                    plugin_done = Path(plugin_runtime["consumer_done_path"])
                    if not plugin_done.exists():
                        plugin_done.touch(mode=0o600)
                except OSError:
                    pass
                try:
                    plugin_export_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            if plugin_export_process.poll() is None:
                plugin_export_process.terminate()
                try:
                    plugin_export_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    plugin_export_process.kill()
                    plugin_export_process.wait(timeout=5)
        if plugin_export_log is not None:
            plugin_export_log.close()
        observer_stop.set()
        if observer is not None and observer.is_alive():
            observer.join(timeout=5)
        if control_process is not None and control_process.poll() is None:
            control_process.terminate()
            try:
                control_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                control_process.kill()
                control_process.wait(timeout=5)
        if control_log is not None:
            control_log.close()
        try:
            done_path = runtime_export / "consumer.done"
            if not done_path.exists():
                done_path.touch(mode=0o600)
        except OSError:
            pass
        if central is not None and central.poll() is None:
            central.terminate()
            try:
                central.wait(timeout=30)
            except subprocess.TimeoutExpired:
                central.kill()
                central.wait(timeout=5)
        central_log.close()
        if p4_started and not p4_log_path.is_file():
            observed = subprocess.run(
                [*p4_compose, "logs", "--no-color", "switch"],
                cwd=repo,
                env=p4_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            p4_log_path.write_text(observed.stdout, encoding="utf-8")
        for name in (
            "central.log",
            "edge.log",
            "gateway.log",
            "triton.log",
            "control.log",
            "bmv2.log",
            "p4-compiler.log",
            "p4-loader.log",
            "p4-traffic.log",
            "web.log",
            "web-forwarder.log",
            "analysis-export.log",
            "analysis-go-test.log",
            "plugin-host-export.log",
            "plugin-host-go-test.log",
        ):
            source = temporary / name
            destination = args.evidence.parent / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        for source, destination_name in (
            (plugin_export_dir / "plugin-host.log", "plugin-host.log"),
            (plugin_export_dir / "plugin-hostctl.log", "plugin-hostctl.log"),
            (analysis_export_dir / "analysis.log", "analysis.log"),
            (
                analysis_export_dir / "external-fixtures.log",
                "analysis-external-fixtures.log",
            ),
        ):
            destination = args.evidence.parent / destination_name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        central_failure_evidence = args.evidence.parent / "central-runtime-evidence"
        if central_evidence.is_dir() and not central_failure_evidence.exists():
            shutil.copytree(central_evidence, central_failure_evidence)
        if pg_started:
            subprocess.run(
                [*pg_compose, "down", "--volumes", "--remove-orphans"],
                cwd=repo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        if p4_started:
            subprocess.run(
                [*p4_compose, "down", "--volumes", "--remove-orphans"],
                cwd=repo,
                env=p4_environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        if live_runtime_document is not None:
            live_runtime_document["state"] = "STOPPED" if run_completed else "FAILED"
            if not run_completed:
                live_runtime_document["stop_reason"] = "runner-failure"
            live_runtime_document["stopped_at"] = (
                datetime.now(UTC_ZONE).isoformat().replace("+00:00", "Z")
            )
            try:
                write_json_atomic(live_manifest_path, live_runtime_document)
            except OSError:
                pass
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
