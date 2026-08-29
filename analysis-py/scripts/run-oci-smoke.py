#!/usr/bin/env python3
"""Build-identity-aware OCI startup, hardening, health, business, and shutdown smoke."""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

if __name__ == "__main__" and os.environ.get("MASI_RUNTIME_SMOKE_WRAPPED") != "1":
    _repository = Path(__file__).resolve().parents[2]
    os.execv(
        sys.executable,
        [
            sys.executable,
            str(_repository / "scripts/ci/run_bounded_runtime_smoke.py"),
            "--repo",
            str(_repository),
            "--module",
            "analysis",
            "--timeout-seconds",
            os.environ.get("MASI_ANALYSIS_OCI_TOTAL_TIMEOUT_SECONDS", "3600"),
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            *sys.argv[1:],
        ],
    )

import httpx

from masi_analysis.canonical import canonical_digest, file_digest, go_json_bytes
from masi_analysis.models import BindingObservation


def docker_output(*args: str, timeout: int = 30) -> str:
    return subprocess.run(
        ["docker", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    ).stdout


def docker_json(*args: str, timeout: int = 30) -> Any:
    return json.loads(docker_output(*args, timeout=timeout))


def prepare_evidence_path(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(os.path.abspath(value))
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise ValueError("OCI evidence parent path is unsafe")
    if path.exists() or path.is_symlink():
        raise ValueError("OCI evidence output already exists or is a symlink")
    return path


def write_new_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PortReservation:
    def __init__(self) -> None:
        self._listener: socket.socket | None = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self.port = int(self._listener.getsockname()[1])

    def release(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None


def main() -> None:
    parser = argparse.ArgumentParser()
    module_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence")
    parser.add_argument("--allow-local-candidate", action="store_true")
    args = parser.parse_args()
    evidence_path = prepare_evidence_path(args.evidence)
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    repository = module_root.parent
    support = runpy.run_path(str(module_root / "scripts/run-blackbox.py"), run_name="analysis_blackbox_support")
    generate_pki = cast(Callable[[Path], dict[str, str]], support["generate_pki"])
    write_runtime = support["write_runtime"]
    httpx_context = support["httpx_context"]
    exact_input = support["exact_input"]
    submit_and_poll = support["submit_and_poll"]
    image = docker_json("image", "inspect", args.image)[0]
    image_id = str(image["Id"])
    repo_digests = [str(value) for value in image.get("RepoDigests") or []]
    immutable_match = re.fullmatch(r"([^@]+)@(sha256:[0-9a-f]{64})", args.image)
    immutable_reference = False
    if immutable_match:
        requested_repository, requested_digest = immutable_match.groups()
        requested_repository = requested_repository.removeprefix("docker.io/library/").removeprefix("docker.io/")
        immutable_reference = any(
            value.rsplit("@", 1)[-1] == requested_digest
            and value.rsplit("@", 1)[0].removeprefix("docker.io/library/").removeprefix("docker.io/") == requested_repository
            for value in repo_digests
        )
        if not immutable_reference:
            raise RuntimeError("requested immutable image digest did not match RepoDigests")
    elif not args.allow_local_candidate:
        raise RuntimeError("OCI smoke requires repository@sha256 or explicit --allow-local-candidate")
    image_config = image["Config"]
    if image_config.get("User") != "65532:65532" or not image_config.get("Healthcheck"):
        raise RuntimeError("image non-root user or healthcheck missing")
    labels = image_config.get("Labels") or {}
    if labels.get("io.masi-nids.module") != "MOD-AGENT-001" or labels.get("io.masi-nids.runtime-profile") != "analysis-agent-runtime/v1":
        raise RuntimeError("image module/runtime labels drifted")

    with tempfile.TemporaryDirectory(prefix="masi-analysis-oci-") as temporary:
        root = Path(temporary)
        pki = generate_pki(root)
        provider_reservation = PortReservation()
        mcp_reservation = PortReservation()
        peer_reservation = PortReservation()
        service_port = 7446
        provider_port = provider_reservation.port
        mcp_port = mcp_reservation.port
        peer_port = peer_reservation.port
        gateway = docker_output(
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ).strip()
        if not gateway:
            raise RuntimeError("Docker bridge gateway unavailable")
        config_dir = root / "config"
        secrets_dir = root / "secrets"
        secrets_dir.mkdir()
        for directory in (config_dir, secrets_dir):
            if directory.exists():
                directory.chmod(0o700)
                os.chown(directory, 65532, 65532)
        host_config_path = write_runtime(
            config_dir,
            repository / "contracts",
            service_port=7446,
            provider_port=provider_port,
            mcp_port=mcp_port,
            peer_port=peer_port,
            tls_files=pki,
            limits_override={"max_tasks": 1024, "task_retention_seconds": 300},
        )
        secret_names = {
            "ca": "ca.pem",
            "server_cert": "server.pem",
            "server_key": "server.key",
            "module_cert": "module.pem",
            "module_key": "module.key",
        }
        for source_id, target_name in secret_names.items():
            target = secrets_dir / target_name
            shutil.copyfile(pki[source_id], target)
            target.chmod(0o600 if target_name.endswith(".key") else 0o644)
            os.chown(target, 65532, 65532)
        config = json.loads(host_config_path.read_bytes())
        config.update(
            {
                "contract_root": "/opt/masi/contracts",
                "manifest_path": "/run/masi-analysis-config/manifest.json",
                "binding_path": "/run/masi-analysis-config/binding.json",
                "store_path": "/var/lib/masi-analysis/analysis.sqlite3",
                "health_state_path": "/var/run/masi-analysis/health.json",
                "listen": {"host": "0.0.0.0", "port": 7446},
                "public_agent_card_url": "https://localhost:7446/.well-known/agent-card.json",
            }
        )
        config["tls"] = {
            "enabled": True,
            "cert_file": "/run/masi-analysis-secrets/server.pem",
            "key_file": "/run/masi-analysis-secrets/server.key",
            "client_ca_file": "/run/masi-analysis-secrets/ca.pem",
            "allowed_client_sans": ["masi-control"],
        }
        for name in ("mcp", "provider"):
            config[name]["base_url"] = f"https://host.docker.internal:{mcp_port if name == 'mcp' else provider_port}"
            config[name]["allowed_ips"] = [gateway]
            config[name]["server_name"] = "localhost"
            config[name]["ca_file"] = "/run/masi-analysis-secrets/ca.pem"
            config[name]["cert_file"] = "/run/masi-analysis-secrets/module.pem"
            config[name]["key_file"] = "/run/masi-analysis-secrets/module.key"
        config["a2a_peers"][0].update(
            {
                "base_url": f"https://host.docker.internal:{peer_port}",
                "allowed_ips": [gateway],
                "server_name": "localhost",
                "ca_file": "/run/masi-analysis-secrets/ca.pem",
                "cert_file": "/run/masi-analysis-secrets/module.pem",
                "key_file": "/run/masi-analysis-secrets/module.key",
            }
        )
        config_raw = go_json_bytes(config)
        host_config_path.write_bytes(config_raw)
        binding_path = config_dir / "binding.json"
        binding = json.loads(binding_path.read_bytes())
        binding["config_digest"] = file_digest(config_raw)
        binding["binding_digest"] = canonical_digest({key: value for key, value in binding.items() if key != "binding_digest"})
        binding_path.write_bytes(go_json_bytes(binding))
        for path in config_dir.iterdir():
            path.chmod(0o644)
            os.chown(path, 65532, 65532)
        config_dir.chmod(0o700)
        os.chown(config_dir, 65532, 65532)

        mount_preflight = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--volume",
                f"{config_dir}:/run/masi-analysis-config:ro",
                "--volume",
                f"{secrets_dir}:/run/masi-analysis-secrets:ro",
                "--entrypoint",
                "/usr/local/bin/python3.12",
                image_id,
                "-c",
                "from pathlib import Path; "
                "required=["
                "Path('/run/masi-analysis-config/config.json'),"
                "Path('/run/masi-analysis-config/manifest.json'),"
                "Path('/run/masi-analysis-config/binding.json'),"
                "Path('/run/masi-analysis-secrets/server.key'),"
                "Path('/run/masi-analysis-secrets/module.key')"
                "]; "
                "assert all(path.is_file() for path in required); "
                "assert sum(path.is_file() for path in Path('/opt/masi/contracts').rglob('*')) == 3",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if mount_preflight.returncode != 0:
            raise RuntimeError("OCI mount or minimized-contract preflight failed")

        neighbor_log = (root / "neighbors.log").open("wb")
        neighbors: subprocess.Popen[bytes] | None = None
        for neighbor_attempt in range(3):
            if neighbor_attempt:
                provider_reservation = PortReservation()
                mcp_reservation = PortReservation()
                peer_reservation = PortReservation()
                provider_port = provider_reservation.port
                mcp_port = mcp_reservation.port
                peer_port = peer_reservation.port
                config["provider"]["base_url"] = f"https://host.docker.internal:{provider_port}"
                config["mcp"]["base_url"] = f"https://host.docker.internal:{mcp_port}"
                config["a2a_peers"][0]["base_url"] = f"https://host.docker.internal:{peer_port}"
                config_raw = go_json_bytes(config)
                host_config_path.write_bytes(config_raw)
                binding["config_digest"] = file_digest(config_raw)
                binding["binding_digest"] = canonical_digest({key: value for key, value in binding.items() if key != "binding_digest"})
                binding_path.write_bytes(go_json_bytes(binding))
            provider_reservation.release()
            mcp_reservation.release()
            peer_reservation.release()
            neighbors = subprocess.Popen(
                [
                    sys.executable,
                    str(module_root / "tests/fake_neighbors.py"),
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
                    "--listen-host",
                    "0.0.0.0",
                ],
                stdout=neighbor_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            time.sleep(0.2)
            if neighbors.poll() is None:
                break
            neighbors.wait(timeout=5)
            neighbors = None
        if neighbors is None:
            raise RuntimeError("OCI neighbors exhausted bounded port retries")
        container_name = f"masi-analysis-oci-{uuid.uuid4().hex[:12]}"
        container_id = ""
        try:
            if neighbors.poll() is not None:
                raise RuntimeError("OCI neighbors failed startup")
            container_id = docker_output(
                "run",
                "--detach",
                "--name",
                container_name,
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "64",
                "--memory",
                "384m",
                "--cpus",
                "1.0",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m,uid=65532,gid=65532,mode=0700",
                "--tmpfs",
                "/var/lib/masi-analysis:rw,noexec,nosuid,size=96m,uid=65532,gid=65532,mode=0700",
                "--tmpfs",
                "/var/run/masi-analysis:rw,noexec,nosuid,size=4m,uid=65532,gid=65532,mode=0700",
                "--volume",
                f"{config_dir}:/run/masi-analysis-config:ro",
                "--volume",
                f"{secrets_dir}:/run/masi-analysis-secrets:ro",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--publish",
                "127.0.0.1::7446",
                image_id,
                timeout=60,
            ).strip()
            published = docker_output("port", container_id, "7446/tcp").strip()
            published_match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", published)
            if published_match is None:
                raise RuntimeError("Docker did not allocate an exact loopback Analysis port")
            service_port = int(published_match.group(1))
            deadline = time.monotonic() + 30
            health = ""
            while time.monotonic() < deadline:
                state = docker_json("inspect", container_id)[0]["State"]
                if not state.get("Running"):
                    logs = docker_output("logs", container_id)
                    raise RuntimeError(f"OCI Analysis exited early: {logs[-2000:]}")
                health = (state.get("Health") or {}).get("Status", "")
                if health == "healthy":
                    break
                time.sleep(0.2)
            if health != "healthy":
                logs = docker_output("logs", container_id)
                health_log = docker_json("inspect", container_id)[0]["State"].get("Health", {}).get("Log", [])
                raise RuntimeError(f"OCI health deadline exceeded: logs={logs[-2000:]!r} health={health_log[-3:]!r}")
            runtime = docker_json("inspect", container_id)[0]
            host_config = runtime["HostConfig"]
            if not host_config.get("ReadonlyRootfs") or "ALL" not in (host_config.get("CapDrop") or []):
                raise RuntimeError("OCI runtime hardening drifted")
            if any("docker.sock" in mount.get("Source", "") for mount in runtime.get("Mounts", [])):
                raise RuntimeError("Docker socket unexpectedly mounted")
            write_attempt = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_id,
                    "/usr/local/bin/python3.12",
                    "-c",
                    "from pathlib import Path; Path('/oci-rootfs-write-must-fail').touch()",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            if write_attempt.returncode == 0:
                raise RuntimeError("read-only root filesystem write unexpectedly succeeded")

            client = httpx.Client(
                base_url=f"https://localhost:{service_port}",
                verify=httpx_context(pki["ca"], pki["control_cert"], pki["control_key"]),
                trust_env=False,
                timeout=15,
            )
            observed_binding = BindingObservation.model_validate(binding)
            material = SimpleNamespace(config_digest=binding["config_digest"], binding=observed_binding)
            task, artifact = submit_and_poll(client, exact_input(material, task_id="oci-task-1", content="needs-tool"))
            if task["status"]["state"] != "TASK_STATE_COMPLETED" or artifact is None or not artifact.model_explanation_facts:
                raise RuntimeError("OCI public A2A/MCP business oracle failed")
            started_stop = time.monotonic()
            subprocess.run(
                ["docker", "stop", "--time", "10", container_id],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
            stop_seconds = time.monotonic() - started_stop
            state = docker_json("inspect", container_id)[0]["State"]
            if state.get("ExitCode") != 0 or stop_seconds > 10:
                raise RuntimeError("OCI graceful shutdown failed")
            evidence = {
                "schema_version": "analysis-oci-evidence/v1",
                "module_id": "MOD-AGENT-001",
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": "PASS",
                "qualification": "NOT_QUALIFIED",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "actual_oci_started": True,
                "mount_preflight": True,
                "runtime_contract_file_count": 3,
                "image_id": image_id,
                "image_repo_digests": repo_digests,
                "immutable_image_reference": immutable_reference,
                "local_candidate": not immutable_reference,
                "image_user": image_config.get("User"),
                "health_status": health,
                "read_only_rootfs": True,
                "cap_drop_all": True,
                "no_new_privileges": True,
                "docker_socket_absent": True,
                "rootfs_write_negative": True,
                "real_mtls_a2a_business": True,
                "real_mtls_mcp_business": True,
                "artifact_digest": artifact.artifact_digest,
                "config_digest": binding["config_digest"],
                "binding_digest": binding["binding_digest"],
                "stop_seconds": stop_seconds,
                "exit_code": state.get("ExitCode"),
                "source_revision": labels.get("org.opencontainers.image.revision"),
                "source_tree_digest": labels.get("io.masi-nids.source-tree.digest"),
                "python_builder_digest": labels.get("io.masi-nids.python-builder.digest"),
                "runtime_base_digest": labels.get("io.masi-nids.runtime-base.digest"),
                "libcrypto_digest": labels.get("io.masi-nids.runtime-library.libcrypto.digest"),
                "libssl_digest": labels.get("io.masi-nids.runtime-library.libssl.digest"),
                "runtime_profile": labels.get("io.masi-nids.runtime-profile"),
            }
            raw = go_json_bytes(evidence) + b"\n"
            if evidence_path is not None:
                write_new_file(evidence_path, raw)
            print(json.dumps(evidence, sort_keys=True))
        finally:
            for reservation in (
                provider_reservation,
                mcp_reservation,
                peer_reservation,
            ):
                reservation.release()
            if container_id:
                try:
                    subprocess.run(
                        ["docker", "rm", "--force", container_id],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    pass
            if neighbors.poll() is None:
                os.killpg(neighbors.pid, signal.SIGTERM)
            try:
                neighbors.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(neighbors.pid, signal.SIGKILL)
                neighbors.wait(timeout=5)
            neighbor_log.close()


if __name__ == "__main__":
    main()
