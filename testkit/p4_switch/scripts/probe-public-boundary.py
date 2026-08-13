from __future__ import annotations

import argparse
import json
from pathlib import Path

from p4.v1 import p4runtime_pb2

from testkit.p4_switch.lib.p4runtime_client import P4InfoIndex, P4RuntimeClient
from testkit.p4_switch.lib.qualification_workload import (
    TrafficHarness,
    WorkloadPhase,
    install_infrastructure,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--cert-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-identity", required=True)
    parser.add_argument("--election-id", type=int, required=True)
    args = parser.parse_args()
    index = P4InfoIndex(args.artifacts / "masi_switch.p4info.txtpb")
    program = (args.artifacts / "masi_switch.json").read_bytes()
    primary = False
    api_version = ""
    identity_match = False
    packet_oracle: dict[str, object] = {}
    errors: list[str] = []
    client: P4RuntimeClient | None = None
    try:
        client = P4RuntimeClient(
            "127.0.0.1:9559",
            device_id=1,
            election_id=args.election_id,
            ca_path=args.cert_dir / "ca.crt",
            cert_path=args.cert_dir / "client.crt",
            key_path=args.cert_dir / "client.key",
        )
        primary = client.is_primary
        capabilities = client.stub.Capabilities(
            p4runtime_pb2.CapabilitiesRequest(), timeout=5
        )
        api_version = capabilities.p4runtime_api_version
        identity = client.set_pipeline(index.p4info, program)
        identity_match = client.verify_pipeline_identity(identity) == identity
        install_infrastructure(index, client)
        traffic = TrafficHarness(args.run_identity).run(
            [WorkloadPhase("lifecycle-probe", 0.2, 20, 1)]
        )
        phases = traffic.get("phases")
        if (
            not isinstance(phases, list)
            or not phases
            or not isinstance(phases[0], dict)
        ):
            raise RuntimeError("lifecycle probe returned no valid traffic phase")
        phase = phases[0]
        packet_oracle = {
            "sender_attempted": phase["sender_attempted"],
            "sender_accepted": phase["sender_accepted"],
            "test_ingress_packets": phase["test_ingress_packets"],
            "dut_egress_packets": phase["dut_egress_packets"],
            "ingress_capture_gap": phase["ingress_capture_gap"],
            "egress_capture_or_outcome_gap": phase["egress_capture_or_outcome_gap"],
            "errors": traffic["errors"],
        }
    except BaseException as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            client.close()
    sender_attempted = packet_oracle.get("sender_attempted", 0)
    passed = (
        primary
        and api_version.startswith("1.")
        and identity_match
        and not errors
        and isinstance(sender_attempted, int)
        and not isinstance(sender_attempted, bool)
        and sender_attempted > 0
        and packet_oracle.get("sender_attempted")
        == packet_oracle.get("sender_accepted")
        and packet_oracle.get("ingress_capture_gap") == 0
        and packet_oracle.get("egress_capture_or_outcome_gap") == 0
        and not packet_oracle.get("errors")
    )
    document = {
        "result": "PASS" if passed else "FAIL",
        "public_boundary": "mTLS P4Runtime StreamChannel/Capabilities/pipeline readback plus AF_PACKET outcome",
        "primary_arbitration": primary,
        "p4runtime_api_version": api_version,
        "pipeline_identity_match": identity_match,
        "packet_oracle": packet_oracle,
        "errors": errors,
    }
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
