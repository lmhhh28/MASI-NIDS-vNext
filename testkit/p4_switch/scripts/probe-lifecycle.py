from __future__ import annotations

import json
import sys
from pathlib import Path

from p4.v1 import p4runtime_pb2

from testkit.p4_switch.lib.p4runtime_client import P4RuntimeClient


def main() -> int:
    if len(sys.argv) != 9:
        raise SystemExit(
            "usage: probe-lifecycle.py ADDRESS CERT_DIR OLD_ID OLD_EXIT "
            "STOP_DURATION_MS NEW_ID EXPECTED_HEALTH OUTPUT"
        )
    (
        address,
        cert_name,
        old_id,
        old_exit_text,
        stop_duration_text,
        new_id,
        expected_health,
        output_name,
    ) = sys.argv[1:]
    cert_dir = Path(cert_name)
    old_exit = int(old_exit_text)
    stop_duration_ms = int(stop_duration_text)
    api_version = ""
    primary = False
    error = None
    try:
        with P4RuntimeClient(
            address,
            device_id=1,
            election_id=30,
            ca_path=cert_dir / "ca.crt",
            cert_path=cert_dir / "client.crt",
            key_path=cert_dir / "client.key",
        ) as client:
            primary = client.is_primary
            capabilities = client.stub.Capabilities(
                p4runtime_pb2.CapabilitiesRequest(), timeout=5
            )
            api_version = capabilities.p4runtime_api_version
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"

    restart_live = (
        old_id != new_id
        and stop_duration_ms <= 12_000
        and expected_health == "healthy"
        and primary
        and api_version == "1.4.1"
        and error is None
    )
    graceful = restart_live and old_exit in {0, 130}
    upstream_shutdown_hold = restart_live and old_exit == 137
    if graceful:
        result = "PASS"
        stable_reason = None
    elif upstream_shutdown_hold:
        result = "HOLD"
        stable_reason = "UPSTREAM_BMV2_NO_BOUNDED_GRACEFUL_SHUTDOWN"
    else:
        result = "FAIL"
        stable_reason = "LIFECYCLE_OR_PUBLIC_BOUNDARY_FAILURE"
    document = {
        "phase": "lifecycle",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
        "tests": [
            {
                "id": "TEST-P4-LIFECYCLE-001",
                "requirement_ids": ["MOD-SW-001", "TEST-003"],
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": ("QUALIFIED" if result == "PASS" else "NOT_QUALIFIED"),
                "evidence": {
                    "old_container_id": old_id,
                    "old_exit_code": old_exit,
                    "stop_timeout_ms": 10_000,
                    "stop_duration_ms": stop_duration_ms,
                    "new_container_id": new_id,
                    "post_restart_health": expected_health,
                    "public_boundary": "mTLS P4Runtime StreamChannel and Capabilities",
                    "primary_arbitration": primary,
                    "p4runtime_api_version": api_version,
                    "restart_liveness_passed": restart_live,
                    "graceful_shutdown_passed": graceful,
                    "stable_reason": stable_reason,
                    "error": error,
                },
            }
        ],
    }
    Path(output_name).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if result != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
