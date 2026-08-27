"""Real Mininet host namespaces connected to the pinned BMv2 OCI runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import Switch


RUNTIME_IMAGE = (
    "masi-nids/p4-switch-runtime@sha256:"
    "24669f08df3583b0d170226f6969f1090235604ef6d5bb5e2b2bb06062d9a571"
)
RUNTIME_DIGEST = (
    "sha256:24669f08df3583b0d170226f6969f1090235604ef6d5bb5e2b2bb06062d9a571"
)
SRC_MAC = bytes.fromhex("000000000101")
DST_MAC = bytes.fromhex("000000000202")
SRC_IP = bytes((192, 0, 2, 60))
DST_IP = bytes((198, 51, 100, 60))
SRC_PORT = 46000
DST_PORT = 8080
MININET_PACKAGE_VERSION = "2.3.0-1ubuntu1"


class ControllerArgs(TypedDict):
    runner_image: str
    switch_id: str
    repo: Path
    artifacts: Path
    cert_dir: Path


class LinkState(TypedDict):
    ifname: str
    flags: list[str]
    operstate: str
    mtu: int


class ExternalBmv2Switch(Switch):
    """Mininet switch node whose two ports are moved to the BMv2 netns."""

    def start(self, controllers) -> None:  # noqa: ANN001
        del controllers

    def stop(self, deleteIntfs: bool = True) -> None:  # noqa: N803
        del deleteIntfs


def run(
    command: list[str],
    *,
    check: bool = True,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def checksum(payload: bytes) -> int:
    if len(payload) % 2:
        payload += b"\x00"
    total = sum(struct.unpack(f"!{len(payload) // 2}H", payload))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def test_frame() -> bytes:
    body = b"masi-mininet-e2e"
    tcp_without_checksum = struct.pack(
        "!HHLLBBHHH",
        SRC_PORT,
        DST_PORT,
        1,
        0,
        5 << 4,
        0x02,
        8192,
        0,
        0,
    )
    pseudo = (
        SRC_IP
        + DST_IP
        + struct.pack("!BBH", 0, 6, len(tcp_without_checksum) + len(body))
    )
    tcp_sum = checksum(pseudo + tcp_without_checksum + body)
    tcp = (
        tcp_without_checksum[:16]
        + struct.pack("!H", tcp_sum)
        + tcp_without_checksum[18:]
    )
    ipv4_without_checksum = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(tcp) + len(body),
        0x4D41,
        0x4000,
        64,
        6,
        0,
        SRC_IP,
        DST_IP,
    )
    ipv4 = (
        ipv4_without_checksum[:10]
        + struct.pack("!H", checksum(ipv4_without_checksum))
        + ipv4_without_checksum[12:]
    )
    return DST_MAC + SRC_MAC + struct.pack("!H", 0x0800) + ipv4 + tcp + body


def controller_command(
    action: str,
    *,
    runner_image: str,
    switch_id: str,
    repo: Path,
    artifacts: Path,
    cert_dir: Path,
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        f"container:{switch_id}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--read-only",
        "--tmpfs",
        "/tmp:size=32m,mode=1777",
        "--volume",
        f"{repo}:/workspace:ro",
        "--volume",
        f"{artifacts}:/artifacts:ro",
        "--volume",
        f"{cert_dir}:/certs:ro",
        runner_image,
        "python",
        "/workspace/testkit/p4_switch/mininet/mininet_controller.py",
        action,
        "/artifacts",
        "/certs",
    ]


def invoke_controller(
    action: str,
    *,
    runner_image: str,
    switch_id: str,
    repo: Path,
    artifacts: Path,
    cert_dir: Path,
) -> dict[str, object]:
    attempts = 5 if action == "setup" else 1
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        completed = run(
            controller_command(
                action,
                runner_image=runner_image,
                switch_id=switch_id,
                repo=repo,
                artifacts=artifacts,
                cert_dir=cert_dir,
            ),
            check=False,
            timeout=30,
        )
        if completed.returncode == 0:
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise RuntimeError(f"controller {action} returned no JSON")
            return json.loads(lines[-1])
        errors.append(
            f"attempt={attempt} rc={completed.returncode} stderr={completed.stderr[-2000:]}"
        )
        if attempt < attempts:
            time.sleep(0.5)
    raise RuntimeError(f"controller {action} failed: {' | '.join(errors)}")


def send_and_observe(
    host1, host2, frame: bytes, *, expect_forward: bool
) -> dict[str, object]:  # noqa: ANN001
    capture = host2.popen(
        [
            "timeout",
            "1.5",
            "tcpdump",
            "-U",
            "-nn",
            "-Q",
            "in",
            "-i",
            "h2-eth0",
            "-c",
            "1",
            "tcp and src host 192.0.2.60 and dst host 198.51.100.60 "
            "and src port 46000 and dst port 8080",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.15)
    sender = (
        "import socket;"
        "s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW);"
        "s.bind(('h1-eth0',0));"
        f"n=s.send(bytes.fromhex('{frame.hex()}'));"
        "print(n)"
    )
    sender_output = host1.cmd(f"python3 -c {shlex.quote(sender)}")
    stdout, stderr = capture.communicate(timeout=3)
    if capture.returncode not in (0, 124):
        raise RuntimeError(
            f"tcpdump oracle failed with rc={capture.returncode}: {stderr[-500:]}"
        )
    observed = capture.returncode == 0
    sender_bytes = int(sender_output.strip())
    if sender_bytes != len(frame):
        raise RuntimeError(f"sender reported {sender_bytes}/{len(frame)} bytes")
    if observed != expect_forward:
        raise RuntimeError(
            f"packet oracle expected forward={expect_forward}, observed={observed}; "
            f"tcpdump_rc={capture.returncode} stderr={stderr[-500:]}"
        )
    return {
        "requested": "forward" if expect_forward else "drop",
        "sender": {
            "interface": "h1-eth0",
            "bytes": sender_bytes,
            "frame_digest": "sha256:" + hashlib.sha256(frame).hexdigest(),
        },
        "test_ingress": {"mininet_host": "h1", "bmv2_port": 1},
        "dut": {"runtime": "simple_switch_grpc", "device_id": 1},
        "outcome": {
            "capture_interface": "h2-eth0",
            "packet_observed": observed,
            "tcpdump_exit": capture.returncode,
            "tcpdump_stdout": stdout[-500:],
        },
    }


def link_state(pid: int, interface: str) -> LinkState:
    completed = run(
        [
            "nsenter",
            "-t",
            str(pid),
            "-n",
            "ip",
            "-details",
            "-json",
            "link",
            "show",
            "dev",
            interface,
        ],
        timeout=5,
    )
    links = json.loads(completed.stdout)
    if not isinstance(links, list) or len(links) != 1:
        raise RuntimeError(f"expected one interface readback for {interface}")
    item = links[0]
    if not isinstance(item, dict):
        raise RuntimeError(f"invalid interface readback for {interface}")
    flags = item.get("flags", [])
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise RuntimeError(f"invalid interface flags for {interface}")
    return {
        "ifname": str(item["ifname"]),
        "flags": flags,
        "operstate": str(item.get("operstate", "UNKNOWN")),
        "mtu": int(item["mtu"]),
    }


def phase_document(result: str, evidence: dict[str, object]) -> dict[str, object]:
    return {
        "phase": "mininet",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
        "tests": [
            {
                "id": "TEST-P4-MININET-001",
                "requirement_ids": ["TEST-TRAFFIC-001", "TEST-P4-FW-001", "TEST-003"],
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
                "evidence": evidence,
            }
        ],
        "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def main() -> int:
    if len(sys.argv) != 8:
        raise SystemExit(
            "usage: run_mininet_e2e.py REPO ARTIFACTS CERT_DIR RUNNER_IMAGE "
            "RUN_ID OUTPUT BMv2_LOG"
        )
    repo = Path(sys.argv[1]).resolve()
    artifacts = Path(sys.argv[2]).resolve()
    cert_dir = Path(sys.argv[3]).resolve()
    runner_image = sys.argv[4]
    run_id = sys.argv[5]
    output = Path(sys.argv[6]).resolve()
    bmv2_log = Path(sys.argv[7]).resolve()
    container_name = (
        "masi-p4-mininet-"
        + "".join(character.lower() for character in run_id if character.isalnum())[:48]
    )

    net: Mininet | None = None
    switch_id = ""
    evidence: dict[str, object] = {
        "profile": "p4-mininet-bmv2/v1",
        "runtime_image": RUNTIME_IMAGE,
        "runner_image": runner_image,
        "topology": {
            "engine": "Mininet",
            "hosts": ["h1", "h2"],
            "switch": "p4dut",
            "links": ["h1-eth0<->masi-s1", "masi-s2<->h2-eth0"],
        },
        "steps": [],
        "cleanup": {},
        "module_complete": False,
    }
    result = "FAIL"
    mininet_host_pids: list[int] = []
    try:
        mininet_version = run(
            ["dpkg-query", "-W", "-f=${Version}", "mininet"], timeout=5
        ).stdout.strip()
        if mininet_version != MININET_PACKAGE_VERSION:
            raise RuntimeError(
                f"Mininet package drift: expected {MININET_PACKAGE_VERSION}, "
                f"observed {mininet_version}"
            )
        evidence["mininet_version"] = mininet_version
        net = Mininet(controller=None, link=TCLink, build=False, autoSetMacs=False)
        host1 = net.addHost("h1", ip=None, mac="00:00:00:00:01:01")
        host2 = net.addHost("h2", ip=None, mac="00:00:00:00:02:02")
        dut = net.addSwitch("p4dut", cls=ExternalBmv2Switch)
        net.addLink(
            host1,
            dut,
            intfName1="h1-eth0",
            intfName2="masi-s1",
            cls=TCLink,
            bw=100,
            max_queue_size=1000,
        )
        net.addLink(
            dut,
            host2,
            intfName1="masi-s2",
            intfName2="h2-eth0",
            cls=TCLink,
            bw=100,
            max_queue_size=1000,
        )
        net.build()
        net.start()
        if host1.pid is None or host2.pid is None:
            raise RuntimeError("Mininet host PID unavailable after startup")
        mininet_host_pids = [host1.pid, host2.pid]
        host1.cmd("ip link set dev h1-eth0 mtu 9500 up")
        host2.cmd("ip link set dev h2-eth0 mtu 9500 up")

        created = run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container_name,
                "--network",
                "none",
                "--stop-signal",
                "SIGINT",
                "--user",
                "0:65532",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_RAW",
                "--read-only",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/tmp:size=32m,mode=1777",
                "--volume",
                f"{cert_dir}:/certs:ro",
                "--volume",
                f"{repo / 'deploy/p4-switch/start-switch.sh'}:/startup/start-switch.sh:ro",
                "--entrypoint",
                "/bin/bash",
                RUNTIME_IMAGE,
                "/startup/start-switch.sh",
            ]
        )
        switch_id = created.stdout.strip()
        container_inspect = json.loads(
            run(["docker", "inspect", switch_id], timeout=5).stdout
        )[0]
        expected_image_id = run(
            ["docker", "image", "inspect", RUNTIME_IMAGE, "--format", "{{.Id}}"],
            timeout=5,
        ).stdout.strip()
        actual_image_id = container_inspect["Image"]
        if actual_image_id != expected_image_id:
            raise RuntimeError(
                f"runtime image drift: expected {expected_image_id}, "
                f"observed {actual_image_id}"
            )
        host_config = container_inspect["HostConfig"]
        expected_capabilities = {"CAP_NET_RAW"}
        observed_capabilities = set(host_config["CapAdd"] or [])
        if observed_capabilities != expected_capabilities:
            raise RuntimeError(
                "runtime capability drift: expected "
                f"{sorted(expected_capabilities)}, observed "
                f"{sorted(observed_capabilities)}"
            )
        evidence["runtime_readback"] = {
            "expected_image_id": expected_image_id,
            "container_image_id": actual_image_id,
            "exact_match": True,
            "network_mode": host_config["NetworkMode"],
            "readonly_rootfs": host_config["ReadonlyRootfs"],
            "cap_drop": host_config["CapDrop"],
            "cap_add": host_config["CapAdd"],
            "security_opt": host_config["SecurityOpt"],
        }
        switch_pid = int(
            run(
                ["docker", "inspect", switch_id, "--format", "{{.State.Pid}}"],
                timeout=5,
            ).stdout.strip()
        )
        for interface in ("masi-s1", "masi-s2"):
            run(["ip", "link", "set", "dev", interface, "netns", str(switch_pid)])
            run(
                [
                    "nsenter",
                    "-t",
                    str(switch_pid),
                    "-n",
                    "ip",
                    "link",
                    "set",
                    "dev",
                    interface,
                    "mtu",
                    "9500",
                    "up",
                ]
            )
        evidence["namespace"] = {
            "container_id": switch_id,
            "pid": switch_pid,
            "interfaces_moved": ["masi-s1", "masi-s2"],
        }

        controller_args: ControllerArgs = {
            "runner_image": runner_image,
            "switch_id": switch_id,
            "repo": repo,
            "artifacts": artifacts,
            "cert_dir": cert_dir,
        }
        setup_result = invoke_controller("setup", **controller_args)
        evidence["steps"].append(setup_result)  # type: ignore[union-attr]
        frame = test_frame()
        evidence["steps"].append(  # type: ignore[union-attr]
            {
                "baseline_forward": send_and_observe(
                    host1, host2, frame, expect_forward=True
                )
            }
        )
        evidence["steps"].append(  # type: ignore[union-attr]
            invoke_controller("overlay-insert", **controller_args)
        )
        evidence["steps"].append(  # type: ignore[union-attr]
            {
                "overlay_drop": send_and_observe(
                    host1, host2, frame, expect_forward=False
                )
            }
        )
        counter_result = invoke_controller("counter-read", **controller_args)
        if int(str(counter_result["packet_count"])) < 1:
            raise RuntimeError("overlay direct counter did not increment")
        evidence["steps"].append(counter_result)  # type: ignore[union-attr]
        evidence["steps"].append(  # type: ignore[union-attr]
            invoke_controller("overlay-delete", **controller_args)
        )
        evidence["steps"].append(  # type: ignore[union-attr]
            {
                "post_delete_forward": send_and_observe(
                    host1, host2, frame, expect_forward=True
                )
            }
        )

        run(
            [
                "nsenter",
                "-t",
                str(switch_pid),
                "-n",
                "ip",
                "link",
                "set",
                "dev",
                "masi-s2",
                "down",
            ]
        )
        down_state = link_state(switch_pid, "masi-s2")
        if "UP" in down_state["flags"]:
            raise RuntimeError(
                "masi-s2 remained administratively up after fault injection"
            )
        evidence["steps"].append(  # type: ignore[union-attr]
            {
                "link_down": {
                    "interface_readback": down_state,
                    "packet": send_and_observe(
                        host1, host2, frame, expect_forward=False
                    ),
                }
            }
        )
        run(
            [
                "nsenter",
                "-t",
                str(switch_pid),
                "-n",
                "ip",
                "link",
                "set",
                "dev",
                "masi-s2",
                "up",
            ]
        )
        up_state = link_state(switch_pid, "masi-s2")
        if "UP" not in up_state["flags"]:
            raise RuntimeError(
                "masi-s2 did not become administratively up after recovery"
            )
        evidence["steps"].append(  # type: ignore[union-attr]
            {
                "link_recovered": {
                    "interface_readback": up_state,
                    "packet": send_and_observe(
                        host1, host2, frame, expect_forward=True
                    ),
                }
            }
        )
        evidence["counter_semantics"] = (
            "direct counter proves the exact overlay entry matched; independent tcpdump "
            "proves packet outcome"
        )
        evidence["runtime_digest"] = RUNTIME_DIGEST
        result = "PASS"
    except BaseException as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup_errors: list[str] = []
        if switch_id:
            logs = run(
                ["docker", "logs", "--tail", "200", switch_id],
                check=False,
                timeout=10,
            )
            bmv2_log.write_text(logs.stdout + logs.stderr, encoding="utf-8")
            removed = run(
                ["docker", "rm", "--force", switch_id], check=False, timeout=20
            )
            if removed.returncode != 0:
                cleanup_errors.append(f"docker-rm: {removed.stderr[-500:]}")
        if net is not None:
            try:
                net.stop()
            except BaseException as exc:
                cleanup_errors.append(f"mininet-stop: {type(exc).__name__}: {exc}")
        process_deadline = time.monotonic() + 2
        while time.monotonic() < process_deadline and any(
            Path(f"/proc/{pid}").exists() for pid in mininet_host_pids
        ):
            time.sleep(0.05)
        host_processes_absent = all(
            not Path(f"/proc/{pid}").exists() for pid in mininet_host_pids
        )
        interfaces_absent = all(
            run(
                ["ip", "link", "show", "dev", interface],
                check=False,
                timeout=5,
            ).returncode
            != 0
            for interface in ("masi-s1", "masi-s2")
        )
        stale = (
            run(
                ["docker", "inspect", container_name], check=False, timeout=5
            ).returncode
            == 0
        )
        evidence["cleanup"] = {
            "container_absent": not stale,
            "mininet_host_processes_absent": host_processes_absent,
            "switch_interfaces_absent": interfaces_absent,
            "errors": cleanup_errors,
        }
        if (
            stale
            or not host_processes_absent
            or not interfaces_absent
            or cleanup_errors
        ):
            result = "FAIL"
        output.write_text(
            json.dumps(phase_document(result, evidence), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("the Mininet module E2E must run as root")
    raise SystemExit(main())
