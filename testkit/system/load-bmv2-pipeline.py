#!/usr/bin/env python3
"""One-shot isolated pipeline loader; exits before the real Edge starts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from p4.v1 import p4runtime_pb2

from testkit.p4_switch.lib.p4runtime_client import (
    P4InfoIndex,
    P4RuntimeClient,
    default_action_entity,
    selector_entity,
    table_entity,
    table_key_entity,
)


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--cert-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise SystemExit("pipeline identity output must be fresh")
    index = P4InfoIndex(args.artifacts / "masi_switch.p4info.txtpb")
    with P4RuntimeClient(
        "127.0.0.1:9559",
        device_id=1,
        election_id=10,
        ca_path=args.cert_dir / "ca.crt",
        cert_path=args.cert_dir / "client.crt",
        key_path=args.cert_dir / "client.key",
    ) as client:
        capabilities = client.stub.Capabilities(
            p4runtime_pb2.CapabilitiesRequest(), timeout=5
        )
        if not client.is_primary:
            raise RuntimeError("one-shot loader did not receive primary arbitration")
        identity = client.set_pipeline(
            index.p4info, (args.artifacts / "masi_switch.json").read_bytes()
        )
        client.verify_pipeline_identity(identity)
        telemetry_selector = selector_entity(
            index,
            "telemetry_selector",
            "select_telemetry_bank_0",
            epoch=1,
        )
        client.write(
            p4runtime_pb2.Update.INSERT,
            [
                selector_entity(index, "policy_selector", "select_policy_bank_0"),
                telemetry_selector,
                table_entity(
                    index.table_entry(
                        "l2_forward",
                        {"hdr.ethernet.dst_addr": 0x000000000202},
                        "forward_to_port",
                        {"port": 2},
                    )
                ),
                table_entity(
                    index.table_entry(
                        "l2_forward",
                        {"hdr.ethernet.dst_addr": 0x000000000101},
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
        selector_readback = client.read(
            [table_key_entity(telemetry_selector.table_entry)]
        )
        if len(selector_readback) != 1:
            raise RuntimeError("initialized telemetry selector readback cardinality mismatch")
        selector_entry = selector_readback[0].table_entry
        selector_action = selector_entry.action.action
    document = {
        "schema_version": "bmv2-pipeline-loader/v1",
        "loader_election_id": 10,
        "loader_closed_before_edge": True,
        "initial_infrastructure": [
            "policy-selector-bank-0",
            "telemetry-selector-bank-0-epoch-1",
            "l2-forwarding-two-port",
            "baseline-default-permit",
        ],
        "initial_selector_readback": {
            "match_value_hex": selector_entry.match[0].exact.value.hex(),
            "action_id": selector_action.action_id,
            "epoch_value_hex": selector_action.params[0].value.hex(),
            "table_entry_wire_hex": selector_entry.SerializeToString(
                deterministic=True
            ).hex(),
        },
        "p4runtime_api_version": capabilities.p4runtime_api_version,
        "p4info_digest": identity.p4info_digest,
        "device_config_digest": identity.device_config_digest,
        "profile_digest": file_digest(args.profile),
        "cookie": identity.cookie,
        "supported_write_atomicity": ["CONTINUE_ON_ERROR"],
    }
    args.output.write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(document, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
