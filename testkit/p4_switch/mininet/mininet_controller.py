"""Bounded P4Runtime controller used only by the isolated Mininet module test.

This process deliberately owns no durable state and performs one explicit test
operation per invocation.  It is not an Edge implementation or a production
P4Runtime writer.
"""

# Enum members in the pinned protobuf package are generated dynamically and do
# not have static attribute stubs.  Keep this exception at the adapter boundary.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from p4.v1 import p4runtime_pb2

from testkit.p4_switch.lib.p4runtime_client import (
    P4InfoIndex,
    P4RuntimeClient,
    default_action_entity,
    direct_counter_entity,
    selector_entity,
    table_entity,
    table_key_entity,
)


DEVICE_ID = 1
ADDRESS = "127.0.0.1:9559"
MAC_PORT_1 = "00:00:00:00:01:01"
MAC_PORT_2 = "00:00:00:00:02:02"
SRC_IPV4 = "192.0.2.60"
DST_IPV4 = "198.51.100.60"
SRC_PORT = 46000
DST_PORT = 8080


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def overlay_entry(index: P4InfoIndex):
    return index.overlay_entry(
        src_ipv4=SRC_IPV4,
        dst_ipv4=DST_IPV4,
        protocol=6,
        src_port=SRC_PORT,
        dst_port=DST_PORT,
        action="drop",
    )


def connect(cert_dir: Path, election_id: int) -> P4RuntimeClient:
    return P4RuntimeClient(
        ADDRESS,
        device_id=DEVICE_ID,
        election_id=election_id,
        ca_path=cert_dir / "ca.crt",
        cert_path=cert_dir / "client.crt",
        key_path=cert_dir / "client.key",
    )


def setup(index: P4InfoIndex, artifacts: Path, cert_dir: Path) -> dict[str, object]:
    json_bytes = (artifacts / "masi_switch.json").read_bytes()
    with connect(cert_dir, 1000) as client:
        identity = client.set_pipeline(index.p4info, json_bytes)
        observed = client.verify_pipeline_identity(identity)
        client.write(
            p4runtime_pb2.Update.INSERT,
            [
                selector_entity(index, "policy_selector", "select_policy_bank_0"),
                selector_entity(
                    index,
                    "telemetry_selector",
                    "select_telemetry_bank_0",
                    epoch=1,
                ),
                table_entity(
                    index.table_entry(
                        "l2_forward",
                        {"hdr.ethernet.dst_addr": int(MAC_PORT_2.replace(":", ""), 16)},
                        "forward_to_port",
                        {"port": 2},
                    )
                ),
                table_entity(
                    index.table_entry(
                        "l2_forward",
                        {"hdr.ethernet.dst_addr": int(MAC_PORT_1.replace(":", ""), 16)},
                        "forward_to_port",
                        {"port": 1},
                    )
                ),
            ],
        )
        client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                default_action_entity(index, 0, "permit"),
                default_action_entity(index, 1, "permit"),
            ],
        )
        return {
            "operation": "setup",
            "pipeline_readback": {
                "p4info_proto_digest": observed.p4info_digest,
                "device_config_digest": observed.device_config_digest,
                "cookie": str(observed.cookie),
            },
            "artifacts": {
                "bmv2_json_digest": digest(json_bytes),
                "p4info_artifact_digest": digest(
                    (artifacts / "masi_switch.p4info.txtpb").read_bytes()
                ),
            },
            "selector_bank": 0,
            "default_action": "permit-and-continue",
            "l2_entries": 2,
        }


def insert_overlay(index: P4InfoIndex, cert_dir: Path) -> dict[str, object]:
    entry = overlay_entry(index)
    with connect(cert_dir, 1001) as client:
        client.write(p4runtime_pb2.Update.INSERT, [table_entity(entry)])
        readback = client.read([table_key_entity(entry)])
    exact = len(readback) == 1 and index.readback_key(
        readback[0].table_entry
    ) == index.readback_key(entry)
    if not exact:
        raise RuntimeError("overlay exact installation readback mismatch")
    return {
        "operation": "overlay-insert",
        "entry_count": len(readback),
        "exact_readback": exact,
        "action": "drop",
    }


def read_counter(index: P4InfoIndex, cert_dir: Path) -> dict[str, object]:
    entry = overlay_entry(index)
    with connect(cert_dir, 1002) as client:
        readback = client.read([direct_counter_entity(entry)])
    if len(readback) != 1 or not readback[0].HasField("direct_counter_entry"):
        raise RuntimeError("overlay direct-counter readback missing")
    data = readback[0].direct_counter_entry.data
    return {
        "operation": "counter-read",
        "packet_count": str(data.packet_count),
        "byte_count": str(data.byte_count),
        "packet_count_hex": f"0x{data.packet_count & ((1 << 64) - 1):016x}",
        "byte_count_hex": f"0x{data.byte_count & ((1 << 64) - 1):016x}",
    }


def delete_overlay(index: P4InfoIndex, cert_dir: Path) -> dict[str, object]:
    entry = overlay_entry(index)
    with connect(cert_dir, 1003) as client:
        client.write(p4runtime_pb2.Update.DELETE, [table_entity(entry)])
        readback = client.read([table_key_entity(entry)])
    if readback:
        raise RuntimeError("overlay remained after delete readback")
    return {
        "operation": "overlay-delete",
        "remaining_entries": 0,
        "exact_absence_readback": True,
    }


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: mininet_controller.py ACTION ARTIFACT_DIR CERT_DIR")
    action = sys.argv[1]
    artifacts = Path(sys.argv[2])
    cert_dir = Path(sys.argv[3])
    index = P4InfoIndex(artifacts / "masi_switch.p4info.txtpb")

    operations = {
        "setup": lambda: setup(index, artifacts, cert_dir),
        "overlay-insert": lambda: insert_overlay(index, cert_dir),
        "counter-read": lambda: read_counter(index, cert_dir),
        "overlay-delete": lambda: delete_overlay(index, cert_dir),
    }
    if action not in operations:
        raise SystemExit(f"unknown action {action!r}")
    print(json.dumps(operations[action](), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
