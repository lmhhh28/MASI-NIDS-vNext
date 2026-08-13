"""Real BMv2 black-box PTF tests for the P4 Switch module."""

# PTF exports and the pinned protobuf messages expose descriptor-backed members
# without static stubs.  Optional and return-path diagnostics remain enabled.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import time
import unittest
from dataclasses import replace
from pathlib import Path

import grpc
import ptf
from ptf import base_tests, packet, testutils
from p4.v1 import p4runtime_pb2
from scapy.utils import wrpcap

from testkit.p4_switch.lib.p4runtime_client import (
    P4InfoIndex,
    PipelineIdentity,
    PipelineIdentityMismatch,
    P4RuntimeClient,
    bmv2_device_config,
    clone_session_entity,
    counter_entity,
    default_action_entity,
    digest_entity,
    direct_counter_entity,
    readback_selectors,
    reject_equal_priority_conflicts,
    selector_entity,
    table_entity,
    table_key_entity,
)


DEVICE_ID = 1
PRIMARY_ELECTION_ID = 10
CPU_PORT = 510
CLONE_SESSION = 99
MAC_PORT_1 = "00:00:00:00:01:01"
MAC_PORT_2 = "00:00:00:00:02:02"
SRC_MAC = MAC_PORT_1
DST_MAC = MAC_PORT_2
DST_IPV4 = "198.51.100.10"
RUNTIME_DIGEST = (
    "sha256:8b8655c2fb7bc5563706ee853fb668ba70457633cda7d93633d022b30b7ead42"
)

TEST_REQUIREMENTS = {
    "TEST-P4-STARTUP-001": ["CONTRACT-P4-001", "MOD-SW-001"],
    "TEST-P4-SECURITY-001": ["CONTRACT-P4-001", "TEST-003"],
    "TEST-P4-FW-001-capacity-activation": ["TEST-P4-FW-001"],
    "TEST-P4-PERF-ABSOLUTE-001": ["PERF-P4-FW-001", "TEST-GATE-001"],
    "TEST-P4-RESOURCE-001": ["CONTRACT-P4-FW-001", "TEST-P4-FW-001"],
    "TEST-P4-FW-001-priority-conflict-shadow": ["CONTRACT-P4-FW-001", "TEST-P4-FW-001"],
    "TEST-P4-FW-001-partial-selector-loss": ["CONTRACT-P4-001", "TEST-P4-FW-001"],
    "TEST-P4-COMPAT-001-pipeline-drift": ["CONTRACT-P4-001", "TEST-003"],
    "TEST-P4-FW-001-overlay-order-default": ["CONTRACT-P4-FW-001", "TEST-P4-FW-001"],
    "TEST-P4-FW-001-fragment-malformed": ["CONTRACT-P4-FW-001", "TEST-P4-FW-001"],
    "TEST-TRAFFIC-001-four-modes": ["CONTRACT-TRAFFIC-001", "TEST-TRAFFIC-001"],
    "TEST-P4-OBS-001-counter-readback": ["CONTRACT-RULE-001", "TEST-RULE-001"],
    "TEST-TEL-INF-001-bounded-snapshot": ["CONTRACT-TELEMETRY-001", "TEST-TEL-INF-001"],
    "TEST-TEL-INF-001-best-effort-hints": ["CONTRACT-P4-001", "TEST-TEL-INF-001"],
    "TEST-TEL-INF-001-hint-loss": ["CONTRACT-TELEMETRY-001", "TEST-TEL-INF-001"],
    "TEST-P4-FW-001-host-filter-contamination": ["ARCH-FW-001", "TEST-P4-FW-001"],
    "TEST-P4-FAULT-001-link-recovery": ["MOD-SW-001", "TEST-003"],
    "TEST-P4-FAULT-003-netem-recovery": ["CONTRACT-TRAFFIC-001", "TEST-003"],
    "TEST-P4-FAULT-002-process-crash-recovery": ["CONTRACT-P4-001", "TEST-003"],
}


class _EvidenceTest(base_tests.BaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.dataplane = ptf.dataplane_instance
        self.artifacts = Path(testutils.test_param_get("artifacts"))
        self.cert_dir = Path(testutils.test_param_get("cert_dir"))
        self.evidence_dir = Path(testutils.test_param_get("evidence_dir"))
        self.address = testutils.test_param_get("address") or "127.0.0.1:9559"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.index = P4InfoIndex(self.artifacts / "masi_switch.p4info.txtpb")
        self.client: P4RuntimeClient | None = None
        self.pipeline_identity: PipelineIdentity | None = None
        self.results: list[dict[str, object]] = []

    def connect(self, election_id: int = PRIMARY_ELECTION_ID) -> P4RuntimeClient:
        return P4RuntimeClient(
            self.address,
            device_id=DEVICE_ID,
            election_id=election_id,
            ca_path=self.cert_dir / "ca.crt",
            cert_path=self.cert_dir / "client.crt",
            key_path=self.cert_dir / "client.key",
        )

    def configure_pipeline(self) -> None:
        assert self.client is not None
        self.pipeline_identity = self.client.set_pipeline(
            self.index.p4info,
            (self.artifacts / "masi_switch.json").read_bytes(),
        )
        self.client.verify_pipeline_identity(self.pipeline_identity)

    def record(self, test_id: str, function, *, result: str = "PASS"):
        if result not in {"PASS", "HOLD", "NOT_RUN"}:
            raise ValueError(f"unsupported declared result {result}")
        requirement_ids = TEST_REQUIREMENTS[test_id]
        started = time.perf_counter()
        try:
            evidence = function() or {}
        except BaseException as exc:
            self.results.append(
                {
                    "id": test_id,
                    "requirement_ids": requirement_ids,
                    "level": "MODULE",
                    "applicability": "APPLICABLE",
                    "result": "FAIL",
                    "qualification": "NOT_QUALIFIED",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "evidence": {"error": f"{type(exc).__name__}: {exc}"},
                }
            )
            self.write_phase_evidence("FAIL")
            raise
        self.results.append(
            {
                "id": test_id,
                "requirement_ids": requirement_ids,
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": ("QUALIFIED" if result == "PASS" else "NOT_QUALIFIED"),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "evidence": evidence,
            }
        )
        return evidence

    def phase_result(self) -> str:
        results = {str(item["result"]) for item in self.results}
        if "FAIL" in results:
            return "FAIL"
        if "HOLD" in results:
            return "HOLD"
        if "NOT_RUN" in results:
            return "NOT_RUN"
        return "PASS"

    def write_phase_evidence(self, result: str) -> None:
        name = getattr(self, "phase_name", self.__class__.__name__)
        json_bytes = (self.artifacts / "masi_switch.json").read_bytes()
        p4info_bytes = (self.artifacts / "masi_switch.p4info.txtpb").read_bytes()
        document = {
            "phase": name,
            "level": "MODULE",
            "applicability": "APPLICABLE",
            "result": result,
            "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
            "claim_scope": {
                "target_profile": "p4-stateless-firewall/v1",
                "runtime_digest": RUNTIME_DIGEST,
                "program_digest": "sha256:" + hashlib.sha256(json_bytes).hexdigest(),
                "p4info_digest": "sha256:" + hashlib.sha256(p4info_bytes).hexdigest(),
                "device_config_digest": "sha256:"
                + hashlib.sha256(bmv2_device_config(json_bytes)).hexdigest(),
            },
            "tests": self.results,
            "finished_unix_ns": time.time_ns(),
        }
        (self.evidence_dir / f"{name}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        if self.client is not None:
            self.client.close()
        super().tearDown()

    def ipv4_tcp(
        self,
        *,
        src: str,
        sport: int,
        dport: int,
        payload: bytes = b"masi-p4-e2e",
    ):
        return (
            packet.Ether(src=SRC_MAC, dst=DST_MAC)
            / packet.IP(src=src, dst=DST_IPV4)
            / packet.TCP(sport=sport, dport=dport, flags="S")
            / payload
        )

    def expect_forward(self, frame, *, timeout: float = 1.0) -> None:
        self.dataplane.flush()
        testutils.send_packet(self, 1, frame)
        testutils.verify_packet(self, frame, 2, timeout=timeout)

    def expect_drop(self, frame, *, timeout: float = 0.25) -> None:
        self.dataplane.flush()
        testutils.send_packet(self, 1, frame)
        testutils.verify_no_packet(self, frame, 2, timeout=timeout)

    def expect_forward_on(self, frame, ingress_port: int, egress_port: int) -> None:
        self.dataplane.flush()
        testutils.send_packet(self, ingress_port, frame)
        testutils.verify_packet(self, frame, egress_port, timeout=1.0)

    def install_infrastructure(self, *, with_hints: bool) -> None:
        assert self.client is not None
        entities = [
            selector_entity(self.index, "policy_selector", "select_policy_bank_0"),
            selector_entity(
                self.index,
                "telemetry_selector",
                "select_telemetry_bank_0",
                epoch=1,
            ),
            table_entity(
                self.index.table_entry(
                    "l2_forward",
                    {"hdr.ethernet.dst_addr": int(MAC_PORT_2.replace(":", ""), 16)},
                    "forward_to_port",
                    {"port": 2},
                )
            ),
            table_entity(
                self.index.table_entry(
                    "l2_forward",
                    {"hdr.ethernet.dst_addr": int(MAC_PORT_1.replace(":", ""), 16)},
                    "forward_to_port",
                    {"port": 1},
                )
            ),
        ]
        if with_hints:
            entities.extend(
                [
                    clone_session_entity(CLONE_SESSION, CPU_PORT),
                    digest_entity(self.index.digest_id("telemetry_hint_t")),
                ]
            )
        self.client.write(p4runtime_pb2.Update.INSERT, entities)
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                default_action_entity(self.index, 0, "permit"),
                default_action_entity(self.index, 1, "permit"),
            ],
        )


class MasiSwitchE2E(_EvidenceTest):
    """Functional, security, readback, and 0/128/1024/4096 tests."""

    phase_name = "functional"

    def runTest(self) -> None:
        self.client = self.connect()
        self.record("TEST-P4-STARTUP-001", self._startup)
        self.record("TEST-P4-SECURITY-001", self._security)
        self.record("TEST-P4-FW-001-capacity-activation", self._capacity_matrix)
        self.record("TEST-P4-RESOURCE-001", self._resource_bounds)
        self.record(
            "TEST-P4-FW-001-priority-conflict-shadow",
            self._priority_conflict_shadow,
        )
        self.record(
            "TEST-P4-FW-001-partial-selector-loss",
            self._partial_bank_and_selector_response_loss,
        )
        self.record("TEST-P4-COMPAT-001-pipeline-drift", self._pipeline_drift)
        self.record("TEST-P4-FW-001-overlay-order-default", self._overlay_and_defaults)
        self.record("TEST-P4-FW-001-fragment-malformed", self._fragment_and_malformed)
        self.record("TEST-TRAFFIC-001-four-modes", self._traffic_modes)
        self.record("TEST-P4-OBS-001-counter-readback", self._counter_readback)
        self.record("TEST-TEL-INF-001-bounded-snapshot", self._telemetry_snapshot)
        self.record("TEST-TEL-INF-001-best-effort-hints", self._supplemental_hints)
        self.record("TEST-TEL-INF-001-hint-loss", self._supplemental_hint_loss)
        self.record(
            "TEST-P4-FW-001-host-filter-contamination",
            self._host_filter_contamination,
        )
        self.record("TEST-P4-FAULT-001-link-recovery", self._link_recovery)
        self.record("TEST-P4-FAULT-003-netem-recovery", self._netem_recovery)
        self.write_phase_evidence(self.phase_result())

    def _startup(self) -> dict[str, object]:
        assert self.client is not None
        self.assertTrue(self.client.is_primary)
        self.configure_pipeline()
        self.install_infrastructure(with_hints=True)
        self.expect_forward(self.ipv4_tcp(src="192.0.2.1", sport=31001, dport=80))
        return {
            "p4runtime": "mTLS public boundary",
            "device_id": DEVICE_ID,
            "primary_election_id": PRIMARY_ELECTION_ID,
            "pipeline": "VERIFY_AND_COMMIT",
        }

    def _security(self) -> dict[str, object]:
        assert self.client is not None
        ca = (self.cert_dir / "ca.crt").read_bytes()
        no_client_channel = grpc.secure_channel(
            self.address,
            grpc.ssl_channel_credentials(root_certificates=ca),
            options=(("grpc.ssl_target_name_override", "masi-switch"),),
        )
        with self.assertRaises(grpc.FutureTimeoutError):
            grpc.channel_ready_future(no_client_channel).result(timeout=1.5)
        no_client_channel.close()

        wrong_name_channel = grpc.secure_channel(
            self.address,
            grpc.ssl_channel_credentials(
                root_certificates=ca,
                private_key=(self.cert_dir / "client.key").read_bytes(),
                certificate_chain=(self.cert_dir / "client.crt").read_bytes(),
            ),
            options=(("grpc.ssl_target_name_override", "wrong-switch-name"),),
        )
        with self.assertRaises(grpc.FutureTimeoutError):
            grpc.channel_ready_future(wrong_name_channel).result(timeout=1.5)
        wrong_name_channel.close()

        backup = self.connect(election_id=PRIMARY_ELECTION_ID - 1)
        try:
            self.assertFalse(backup.is_primary)
            with self.assertRaises(grpc.RpcError) as caught:
                backup.write(
                    p4runtime_pb2.Update.MODIFY,
                    [
                        selector_entity(
                            self.index, "policy_selector", "select_policy_bank_1"
                        )
                    ],
                )
            self.assertNotEqual(caught.exception.code(), grpc.StatusCode.OK)
        finally:
            backup.close()

        # The primary remains authoritative after the rejected lower election.
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [selector_entity(self.index, "policy_selector", "select_policy_bank_0")],
        )
        return {
            "client_certificate_required": True,
            "server_identity_required": True,
            "lower_election_write_rejected": True,
        }

    def _make_rules(self, bank: int, count: int, iteration: int):
        if count == 0:
            return [], None, None
        probe_src = f"192.0.2.{20 + iteration}"
        probe_sport = 32000 + iteration
        probe_dport = 22000 + iteration
        rules = [
            self.index.baseline_entry(
                bank,
                src_ipv4=int.from_bytes(socket.inet_aton(probe_src), "big"),
                action="drop",
                priority=2_000_000,
                ingress_port=1,
                dst_ipv4=int.from_bytes(socket.inet_aton(DST_IPV4), "big"),
                protocol=6,
                l4_present=1,
                src_port=probe_sport,
                dst_port=probe_dport,
                fragment_class=0,
            )
        ]
        for offset in range(count - 1):
            rules.append(
                self.index.baseline_entry(
                    bank,
                    src_ipv4=0x0A000001 + offset,
                    action="permit",
                    priority=1_000_000 - offset,
                )
            )
        return (
            rules,
            self.ipv4_tcp(src=probe_src, sport=probe_sport, dport=probe_dport),
            rules[0],
        )

    def _capacity_matrix(self) -> dict[str, object]:
        assert self.client is not None
        measurements: list[dict[str, object]] = []
        active_bank = 0
        active_rules = []
        for iteration, count in enumerate((0, 128, 1024, 4096)):
            bank = 0 if iteration == 0 else 1 - active_bank
            rules, probe, probe_entry = self._make_rules(bank, count, iteration)

            if probe is not None and iteration > 0:
                self.expect_forward(probe)

            write_started = time.perf_counter()
            self.client.write_batched(
                p4runtime_pb2.Update.INSERT,
                [table_entity(rule) for rule in rules],
                timeout=30,
            )
            write_ms = (time.perf_counter() - write_started) * 1000

            read_started = time.perf_counter()
            if rules:
                received = self.client.read_batched(
                    readback_selectors(rules), timeout=30
                )
                actual = {
                    self.index.readback_key(entity.table_entry)
                    for entity in received
                    if entity.HasField("table_entry")
                }
                expected = {self.index.readback_key(rule) for rule in rules}
                self.assertEqual(actual, expected)
            else:
                wildcard = p4runtime_pb2.Entity()
                wildcard.table_entry.table_id = self.index.table(
                    f"baseline_bank_{bank}"
                ).preamble.id
                self.assertEqual(self.client.read([wildcard]), [])
            readback_ms = (time.perf_counter() - read_started) * 1000

            flip_started = time.perf_counter()
            self.client.write(
                p4runtime_pb2.Update.MODIFY,
                [
                    selector_entity(
                        self.index,
                        "policy_selector",
                        f"select_policy_bank_{bank}",
                    )
                ],
            )
            selector_flip_ms = (time.perf_counter() - flip_started) * 1000

            if probe is not None:
                self.expect_drop(probe)
                counter = self.client.read([direct_counter_entity(probe_entry)])
                self.assertEqual(len(counter), 1)
                self.assertGreaterEqual(
                    counter[0].direct_counter_entry.data.packet_count, 1
                )

            permit_probe = self.ipv4_tcp(
                src=f"203.0.113.{20 + iteration}",
                sport=33000 + iteration,
                dport=8080,
            )
            batch_started = time.perf_counter()
            for _ in range(32):
                testutils.send_packet(self, 1, permit_probe)
            for _ in range(32):
                testutils.verify_packet(self, permit_probe, 2, timeout=2)
            packet_elapsed = time.perf_counter() - batch_started

            if active_rules and bank != active_bank:
                self.client.write_batched(
                    p4runtime_pb2.Update.DELETE,
                    [table_key_entity(rule) for rule in active_rules],
                    timeout=30,
                )

            measurements.append(
                {
                    "rule_count": count,
                    "write_ms": round(write_ms, 3),
                    "readback_ms": round(readback_ms, 3),
                    "selector_flip_ms": round(selector_flip_ms, 3),
                    "packet_oracle_pps": round(32 / packet_elapsed, 3),
                    "result": "OBSERVATION_ONLY",
                    "qualification": "NOT_QUALIFIED",
                    "qualification_source": "separate five-repeat absolute performance phase",
                }
            )
            active_bank = bank
            active_rules = rules

        self.active_bank = active_bank
        self.active_rules = active_rules
        self.performance = measurements
        return {
            "matrix": measurements,
            "writes_bounded_to": 256,
            "reads_bounded_to": 256,
            "inactive_full_readback_before_flip": True,
            "packet_oracle_after_flip": True,
        }

    def _overlay_and_defaults(self) -> dict[str, object]:
        assert self.client is not None
        src = "192.0.2.200"
        sport = 40000
        dport = 443
        frame = self.ipv4_tcp(src=src, sport=sport, dport=dport)
        overlay = self.index.overlay_entry(
            src_ipv4=src,
            dst_ipv4=DST_IPV4,
            protocol=6,
            src_port=sport,
            dst_port=dport,
            action="drop",
        )
        self.client.write(p4runtime_pb2.Update.INSERT, [table_entity(overlay)])
        self.expect_drop(frame)
        overlay_count = self.client.read([direct_counter_entity(overlay)])[0]
        self.assertEqual(overlay_count.direct_counter_entry.data.packet_count, 1)

        baseline = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=int.from_bytes(socket.inet_aton(src), "big"),
            dst_ipv4=int.from_bytes(socket.inet_aton(DST_IPV4), "big"),
            action="drop",
            priority=2_100_000,
            ingress_port=1,
            protocol=6,
            l4_present=1,
            src_port=sport,
            dst_port=dport,
            fragment_class=0,
        )
        self.client.write(p4runtime_pb2.Update.INSERT, [table_entity(baseline)])
        permitted_overlay = self.index.overlay_entry(
            src_ipv4=src,
            dst_ipv4=DST_IPV4,
            protocol=6,
            src_port=sport,
            dst_port=dport,
            action="permit",
        )
        self.client.write(
            p4runtime_pb2.Update.MODIFY, [table_entity(permitted_overlay)]
        )
        self.expect_drop(frame)
        baseline_count = self.client.read([direct_counter_entity(baseline)])[0]
        self.assertEqual(baseline_count.direct_counter_entry.data.packet_count, 1)

        self.client.write(
            p4runtime_pb2.Update.DELETE,
            [table_key_entity(baseline), table_key_entity(permitted_overlay)],
        )
        self.expect_forward(frame)

        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [default_action_entity(self.index, self.active_bank, "drop")],
        )
        default_probe = self.ipv4_tcp(src="203.0.113.201", sport=40201, dport=8443)
        self.expect_drop(default_probe)
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [default_action_entity(self.index, self.active_bank, "permit")],
        )
        self.expect_forward(default_probe)

        ttl_overlay = self.index.overlay_entry(
            src_ipv4="192.0.2.201",
            dst_ipv4=DST_IPV4,
            protocol=6,
            src_port=40202,
            dst_port=443,
            action="drop",
        )
        ttl_frame = self.ipv4_tcp(src="192.0.2.201", sport=40202, dport=443)
        self.client.write(p4runtime_pb2.Update.INSERT, [table_entity(ttl_overlay)])
        self.expect_drop(ttl_frame)
        # The contract-bound controller owns expiry; P4 stores no wall clock.
        self.client.write(p4runtime_pb2.Update.DELETE, [table_key_entity(ttl_overlay)])
        self.expect_forward(ttl_frame)
        return {
            "overlay_drop_bypasses_baseline": True,
            "overlay_permit_continues_to_baseline": True,
            "explicit_default_drop_and_permit": True,
            "ttl_removed_by_test_controller_not_dataplane_timer": True,
        }

    def _fragment_and_malformed(self) -> dict[str, object]:
        assert self.client is not None
        first_fragment_rule = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.210"), "big"),
            action="drop",
            priority=2_200_000,
            ingress_port=1,
            fragment_class=1,
        )
        non_initial_rule = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.211"), "big"),
            action="drop",
            priority=2_200_001,
            ingress_port=1,
            fragment_class=2,
            l4_present=0,
        )
        self.client.write(
            p4runtime_pb2.Update.INSERT,
            [table_entity(first_fragment_rule), table_entity(non_initial_rule)],
        )
        first_fragment = (
            packet.Ether(src=SRC_MAC, dst=DST_MAC)
            / packet.IP(src="192.0.2.210", dst=DST_IPV4, flags="MF", frag=0)
            / packet.UDP(sport=41000, dport=53)
            / b"fragment-one"
        )
        non_initial = (
            packet.Ether(src=SRC_MAC, dst=DST_MAC)
            / packet.IP(src="192.0.2.211", dst=DST_IPV4, frag=1, proto=17)
            / b"fragment-two"
        )
        self.expect_drop(first_fragment)
        self.expect_drop(non_initial)
        self.client.write(
            p4runtime_pb2.Update.DELETE,
            [table_key_entity(first_fragment_rule), table_key_entity(non_initial_rule)],
        )

        bad_checksum = self.ipv4_tcp(src="192.0.2.212", sport=41212, dport=80)
        bad_checksum[packet.IP].chksum = 0x1234
        self.expect_drop(bad_checksum)
        ipv4_options = (
            packet.Ether(src=SRC_MAC, dst=DST_MAC)
            / packet.IP(
                src="192.0.2.213", dst=DST_IPV4, ihl=6, options=b"\x01\x01\x01\x01"
            )
            / packet.TCP(sport=41213, dport=80)
        )
        self.expect_drop(ipv4_options)
        truncated = (
            bytes(packet.Ether(src=SRC_MAC, dst=DST_MAC, type=0x0800)) + b"\x45\x00\x00"
        )
        self.dataplane.flush()
        testutils.send_packet(self, 1, truncated)
        testutils.verify_no_packet(self, truncated, 2, timeout=0.25)

        ipv6 = (
            packet.Ether(src=SRC_MAC, dst=DST_MAC)
            / packet.IPv6(src="2001:db8::1", dst="2001:db8::2")
            / packet.TCP(sport=41214, dport=80)
        )
        self.expect_forward(ipv6)
        return {
            "first_fragment_match": "drop",
            "non_initial_fragment_without_ports": "drop",
            "bad_checksum": "fail-closed",
            "ipv4_options": "fail-closed",
            "truncated_header": "fail-closed",
            "ipv6_effect": "unsupported/bypass",
        }

    def _traffic_modes(self) -> dict[str, object]:
        assert self.client is not None
        generated = self.ipv4_tcp(src="203.0.113.61", sport=46061, dport=80)
        self.expect_forward(generated)

        synthetic = self.ipv4_tcp(src="203.0.113.62", sport=46062, dport=8080)
        eligible_id = self.index.counter_id("baseline_eligible_counter")
        eligible_before = self.client.read(
            [counter_entity(eligible_id, self.active_bank)]
        )[0].counter_entry.data.packet_count
        self.dataplane.flush()
        sender_started = time.perf_counter()
        for _ in range(64):
            testutils.send_packet(self, 1, synthetic)
            time.sleep(0.001)
        sender_elapsed = time.perf_counter() - sender_started
        for _ in range(64):
            testutils.verify_packet(self, synthetic, 2, timeout=2)
        eligible_after = self.client.read(
            [counter_entity(eligible_id, self.active_bank)]
        )[0].counter_entry.data.packet_count
        self.assertEqual(eligible_after - eligible_before, 64)

        curated = []
        for offset in range(4):
            frame = (
                packet.Ether(src=SRC_MAC, dst=DST_MAC)
                / packet.IP(src=f"203.0.113.{70 + offset}", dst=DST_IPV4)
                / packet.UDP(sport=47000 + offset, dport=5300 + offset)
                / (b"masi-curated-pcap-" + bytes([offset]) * 32)
            )
            frame.time = 1_700_000_000 + offset
            curated.append(frame)
        pcap_path = Path("/tmp/masi-curated-pcap-l2-v1.pcap")
        wrpcap(str(pcap_path), curated)
        pcap_digest = hashlib.sha256(pcap_path.read_bytes()).hexdigest()
        self.dataplane.flush()
        replay = subprocess.run(
            [
                "tcpreplay",
                "--preload-pcap",
                "--topspeed",
                "--intf1=masi-p1",
                str(pcap_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        for frame in curated:
            testutils.verify_packet(self, frame, 2, timeout=2)

        client_isn = 1000
        server_isn = 9000
        syn = (
            packet.Ether(src=MAC_PORT_1, dst=MAC_PORT_2)
            / packet.IP(src="192.0.2.61", dst="198.51.100.61")
            / packet.TCP(sport=48061, dport=8443, flags="S", seq=client_isn)
        )
        syn_ack = (
            packet.Ether(src=MAC_PORT_2, dst=MAC_PORT_1)
            / packet.IP(src="198.51.100.61", dst="192.0.2.61")
            / packet.TCP(
                sport=8443,
                dport=48061,
                flags="SA",
                seq=server_isn,
                ack=client_isn + 1,
            )
        )
        ack = (
            packet.Ether(src=MAC_PORT_1, dst=MAC_PORT_2)
            / packet.IP(src="192.0.2.61", dst="198.51.100.61")
            / packet.TCP(
                sport=48061,
                dport=8443,
                flags="A",
                seq=client_isn + 1,
                ack=server_isn + 1,
            )
        )
        data = ack.copy()
        data[packet.TCP].flags = "PA"
        data = data / b"live-session-payload"
        fin = ack.copy()
        fin[packet.TCP].flags = "FA"
        self.expect_forward_on(syn, 1, 2)
        self.expect_forward_on(syn_ack, 2, 1)
        self.expect_forward_on(ack, 1, 2)
        self.expect_forward_on(data, 1, 2)
        self.expect_forward_on(fin, 1, 2)

        return {
            "generated_packet": {"packets": 1, "oracle": "forwarded"},
            "synthetic_flow": {
                "requested_packets": 64,
                "sender_completed_packets": 64,
                "sender_pps": round(64 / sender_elapsed, 3),
                "dut_eligible_counter_delta": eligible_after - eligible_before,
                "packet_action_outcome_packets": 64,
                "oracle": "all forwarded",
            },
            "curated_pcap_l2": {
                "packets": 4,
                "sha256": pcap_digest,
                "license": "Apache-2.0 repository-generated fixture",
                "privacy": "documentation-only addresses; no personal data",
                "ground_truth": "four exact generated Ethernet frames",
                "tcpreplay": replay.stderr.strip() or replay.stdout.strip(),
            },
            "live_session": {
                "kind": "bounded user-space TCP handshake/data/FIN exchange",
                "directions": "bidirectional",
                "frames": 5,
            },
            "sender_dut_counter_packet_oracles_separated": True,
        }

    def _counter_readback(self) -> dict[str, object]:
        assert self.client is not None
        rule = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.220"), "big"),
            action="drop",
            priority=2_300_000,
            ingress_port=1,
            protocol=6,
            l4_present=1,
            dst_port=22,
            fragment_class=0,
        )
        self.client.write(p4runtime_pb2.Update.INSERT, [table_entity(rule)])
        frame = self.ipv4_tcp(src="192.0.2.220", sport=42220, dport=22)
        self.expect_drop(frame)
        direct = self.client.read([direct_counter_entity(rule)])[
            0
        ].direct_counter_entry.data
        eligible_id = self.index.counter_id("baseline_eligible_counter")
        eligible = self.client.read([counter_entity(eligible_id, self.active_bank)])[
            0
        ].counter_entry.data
        readback = self.client.read([table_key_entity(rule)])[0].table_entry
        self.assertEqual(
            self.index.readback_key(readback), self.index.readback_key(rule)
        )
        self.assertGreaterEqual(direct.packet_count, 1)
        self.assertGreater(eligible.packet_count, direct.packet_count)

        max_counter = (1 << 64) - 1
        # CounterData uses an int64 protobuf field even though the target
        # counter is 64-bit. -2 carries the exact two's-complement bit pattern
        # 0xffff_ffff_ffff_fffe across this public boundary.
        near_max_wire = -2
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                direct_counter_entity(
                    rule,
                    packets=near_max_wire,
                    bytes_=0,
                )
            ],
        )
        seeded = self.client.read([direct_counter_entity(rule)])[0]
        self.assertEqual(seeded.direct_counter_entry.data.packet_count, near_max_wire)
        self.expect_drop(frame)
        self.expect_drop(frame)
        wrapped = self.client.read([direct_counter_entity(rule)])[0]
        self.assertEqual(wrapped.direct_counter_entry.data.packet_count, 0)

        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [direct_counter_entity(rule, packets=0, bytes_=0)],
        )
        reset = self.client.read([direct_counter_entity(rule)])[0]
        self.assertEqual(reset.direct_counter_entry.data.packet_count, 0)
        self.assertEqual(reset.direct_counter_entry.data.byte_count, 0)
        self.expect_drop(frame)
        after_reset = self.client.read([direct_counter_entity(rule)])[0]
        self.assertEqual(after_reset.direct_counter_entry.data.packet_count, 1)
        self.client.write(p4runtime_pb2.Update.DELETE, [table_key_entity(rule)])
        return {
            "installation_readback": "exact",
            "direct_counter_packets": direct.packet_count,
            "eligible_counter_packets": eligible.packet_count,
            "packet_action_outcome": "independently observed drop",
            "counter_width_bits": 64,
            "counter_mode": "wrap",
            "near_max_seed_unsigned_decimal": str(max_counter - 1),
            "near_max_seed_hex": "0xfffffffffffffffe",
            "near_max_seed_int64_wire": near_max_wire,
            "after_two_packets": wrapped.direct_counter_entry.data.packet_count,
            "explicit_reset_readback": {
                "packets": reset.direct_counter_entry.data.packet_count,
                "bytes": reset.direct_counter_entry.data.byte_count,
            },
            "post_reset_increment": after_reset.direct_counter_entry.data.packet_count,
            "polling_did_not_clear_counter": True,
        }

    def _read_bank_sequence(self, index: int) -> int:
        assert self.client is not None
        entity = counter_entity(self.index.counter_id("telemetry_bank_counter"), index)
        response = self.client.read([entity])
        self.assertEqual(len(response), 1)
        return response[0].counter_entry.data.packet_count

    def _telemetry_snapshot(self) -> dict[str, object]:
        assert self.client is not None
        # Start a fresh application generation so the per-bank sample sequence
        # has an exact origin. Counters are cleared per snapshot epoch, while
        # the supplemental sample sequence remains monotonic for this pipeline.
        self.configure_pipeline()
        self.install_infrastructure(with_hints=True)
        self.active_bank = 0
        self.active_rules = []
        cell_id = self.index.counter_id("telemetry_cell_counter")
        bank_id = self.index.counter_id("telemetry_bank_counter")
        class_id = self.index.counter_id("telemetry_class_counter")
        reset = [counter_entity(bank_id, 1, packets=0, bytes_=0)]
        reset.extend(
            counter_entity(class_id, 5 + offset, packets=0, bytes_=0)
            for offset in range(5)
        )
        reset.extend(
            counter_entity(cell_id, 256 + offset, packets=0, bytes_=0)
            for offset in range(256)
        )
        self.client.write_batched(p4runtime_pb2.Update.MODIFY, reset)
        cleared_before = self.client.read_batched(reset)
        self.assertEqual(len(cleared_before), len(reset))
        self.assertTrue(
            all(
                (
                    entity.counter_entry.data.packet_count == 0
                    and entity.counter_entry.data.byte_count == 0
                )
                for entity in cleared_before
            )
        )
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                selector_entity(
                    self.index, "telemetry_selector", "select_telemetry_bank_1", epoch=2
                )
            ],
        )
        self.expect_forward(self.ipv4_tcp(src="203.0.113.230", sport=43000, dport=80))
        fragment = (
            packet.Ether(src=SRC_MAC, dst=DST_MAC)
            / packet.IP(src="203.0.113.231", dst=DST_IPV4, flags="MF", frag=0)
            / packet.UDP(sport=43001, dport=53)
            / b"telemetry-fragment"
        )
        self.expect_forward(fragment)
        ipv6 = (
            packet.Ether(src=SRC_MAC, dst=DST_MAC)
            / packet.IPv6(src="2001:db8::230", dst="2001:db8::10")
            / packet.UDP(sport=43002, dport=53)
            / b"telemetry-ipv6"
        )
        self.expect_forward(ipv6)
        non_ip = (
            packet.Ether(src=SRC_MAC, dst=DST_MAC, type=0x88B5) / b"telemetry-non-ip"
        )
        self.expect_forward(non_ip)
        invalid = self.ipv4_tcp(src="203.0.113.234", sport=43004, dport=80)
        invalid[packet.IP].chksum = 0x1234
        self.expect_drop(invalid)
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                selector_entity(
                    self.index, "telemetry_selector", "select_telemetry_bank_0", epoch=3
                )
            ],
        )
        sequence_before = self._read_bank_sequence(1)
        aggregate = self.client.read([counter_entity(bank_id, 1)])[0].counter_entry.data
        cells = self.client.read_batched(
            [counter_entity(cell_id, 256 + offset) for offset in range(256)]
        )
        classes = self.client.read_batched(
            [counter_entity(class_id, 5 + offset) for offset in range(5)]
        )
        sequence_after = self._read_bank_sequence(1)
        cell_packets = sum(entity.counter_entry.data.packet_count for entity in cells)
        cell_bytes = sum(entity.counter_entry.data.byte_count for entity in cells)
        self.assertEqual(sequence_before, sequence_after)
        self.assertEqual(sequence_before, 5)
        self.assertEqual(aggregate.packet_count, sequence_before)
        self.assertEqual(cell_packets, 3)
        self.assertLessEqual(cell_bytes, aggregate.byte_count)
        class_packets = [entity.counter_entry.data.packet_count for entity in classes]
        self.assertEqual(class_packets, [2, 1, 1, 1, 1])

        self.client.write_batched(p4runtime_pb2.Update.MODIFY, reset)
        cleared_after = self.client.read_batched(reset)
        self.assertEqual(len(cleared_after), len(reset))
        non_contract_class_bytes = [
            {
                "index": entity.counter_entry.index.index,
                "bytes": entity.counter_entry.data.byte_count,
            }
            for entity in cleared_after
            if entity.counter_entry.counter_id == class_id
            and entity.counter_entry.data.byte_count != 0
        ]
        contract_residuals = [
            {
                "counter_id": entity.counter_entry.counter_id,
                "index": entity.counter_entry.index.index,
                "packets": entity.counter_entry.data.packet_count,
                "bytes": entity.counter_entry.data.byte_count,
            }
            for entity in cleared_after
            if entity.counter_entry.data.packet_count != 0
            or (
                entity.counter_entry.counter_id != class_id
                and entity.counter_entry.data.byte_count != 0
            )
        ]
        self.assertEqual(
            [],
            contract_residuals,
            f"frozen telemetry bank did not clear: {contract_residuals}",
        )
        return {
            "bank": 1,
            "epoch": 2,
            "snapshot_consistency_mechanism": "selector-frozen-bank",
            "snapshot_sequence_source": "telemetry_bank_counter.packet_count",
            "sample_sequence_register_readback": "unsupported-by-exact-bmv2-pi",
            "sequence_before": sequence_before,
            "sequence_after": sequence_after,
            "aggregate_packets": aggregate.packet_count,
            "aggregate_bytes": aggregate.byte_count,
            "cell_packet_sum": cell_packets,
            "class_packets": {
                "ipv4_total": class_packets[0],
                "ipv6": class_packets[1],
                "non_ip": class_packets[2],
                "fragment": class_packets[3],
                "invalid": class_packets[4],
            },
            "quality": "valid/approximate-hash",
            "cell_count": 256,
            "expected_entries": 256,
            "observed_entries": len(cells),
            "clear_before_readback_zero": True,
            "frozen_bank_clear_after_snapshot_readback_zero": True,
            "packet_only_class_byte_field_non_authoritative": non_contract_class_bytes,
            "active_bank_after_flip": 0,
        }

    def _supplemental_hints(self) -> dict[str, object]:
        assert self.client is not None
        sequence_before = self._read_bank_sequence(1)
        self.assertEqual(sequence_before, 0)
        prior_application_sequence = 5
        packets_to_boundary = 1024 - prior_application_sequence
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                selector_entity(
                    self.index, "telemetry_selector", "select_telemetry_bank_1", epoch=4
                )
            ],
        )
        frame = self.ipv4_tcp(src="203.0.113.240", sport=44240, dport=80)
        self.dataplane.flush()
        for _ in range(packets_to_boundary):
            testutils.send_packet(self, 1, frame)
            time.sleep(0.0005)
        digest = self.client.wait_for("digest", timeout=10)
        packet_in = self.client.wait_for("packet", timeout=10)
        self.assertEqual(
            digest.digest.digest_id, self.index.digest_id("telemetry_hint_t")
        )
        self.assertGreaterEqual(len(digest.digest.data), 1)
        self.client.send_digest_ack(digest.digest.digest_id, digest.digest.list_id)
        metadata = {
            item.metadata_id: int.from_bytes(item.value, "big")
            for item in packet_in.packet.metadata
        }
        packet_in_info = next(
            item
            for item in self.index.p4info.controller_packet_metadata
            if item.preamble.alias == "packet_in"
        )
        metadata_by_name = {
            item.name: metadata[item.id]
            for item in packet_in_info.metadata
            if item.id in metadata
        }
        self.assertEqual(metadata_by_name.get("reason"), 1)
        self.assertEqual(metadata_by_name.get("telemetry_bank"), 1)
        self.assertEqual(metadata_by_name.get("telemetry_epoch"), 4)
        self.assertEqual(metadata_by_name.get("telemetry_sequence"), 1024)
        self.assertEqual(self._read_bank_sequence(1), packets_to_boundary)
        self.dataplane.flush()
        return {
            "digest_received": True,
            "digest_acked": True,
            "packet_in_received": True,
            "sample_period_packets": 1024,
            "application_sequence_before_epoch": prior_application_sequence,
            "packets_sent_to_next_boundary": packets_to_boundary,
            "expected_hints": 1,
            "observed_digest_hints": 1,
            "observed_packetin_hints": 1,
            "coverage": 1.0,
            "semantics": "best-effort supplemental hint, not canonical flow source",
        }

    def _supplemental_hint_loss(self) -> dict[str, object]:
        assert self.client is not None
        bank_id = self.index.counter_id("telemetry_bank_counter")
        cell_id = self.index.counter_id("telemetry_cell_counter")
        class_id = self.index.counter_id("telemetry_class_counter")
        reset_bank_0 = [counter_entity(bank_id, 0, packets=0, bytes_=0)]
        reset_bank_0.extend(
            counter_entity(class_id, offset, packets=0, bytes_=0) for offset in range(5)
        )
        reset_bank_0.extend(
            counter_entity(cell_id, offset, packets=0, bytes_=0)
            for offset in range(256)
        )
        self.client.write_batched(p4runtime_pb2.Update.MODIFY, reset_bank_0)

        digest_key = p4runtime_pb2.Entity()
        digest_key.digest_entry.digest_id = self.index.digest_id("telemetry_hint_t")
        clone_key = p4runtime_pb2.Entity()
        clone_key.packet_replication_engine_entry.clone_session_entry.session_id = (
            CLONE_SESSION
        )
        self.client.write(p4runtime_pb2.Update.DELETE, [digest_key, clone_key])
        deletion_readback = {}
        for name, key in (("digest", digest_key), ("clone_session", clone_key)):
            with self.assertRaises(grpc.RpcError) as caught:
                self.client.read([key])
            self.assertEqual(caught.exception.code(), grpc.StatusCode.NOT_FOUND)
            deletion_readback[name] = caught.exception.code().name
        stale_stream_messages = self.client.drain_stream(quiet_seconds=0.1)
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                selector_entity(
                    self.index, "telemetry_selector", "select_telemetry_bank_0", epoch=5
                )
            ],
        )
        frame = self.ipv4_tcp(src="203.0.113.241", sport=44241, dport=80)
        self.dataplane.flush()
        for _ in range(1024):
            testutils.send_packet(self, 1, frame)
            time.sleep(0.0005)
        with self.assertRaises(TimeoutError):
            self.client.wait_for("digest", timeout=0.25)
        with self.assertRaises(TimeoutError):
            self.client.wait_for("packet", timeout=0.25)
        aggregate_during_loss = self._read_bank_sequence(0)
        self.assertEqual(aggregate_during_loss, 1024)
        self.dataplane.flush()

        self.client.write_batched(p4runtime_pb2.Update.MODIFY, reset_bank_0)
        self.client.write(
            p4runtime_pb2.Update.INSERT,
            [
                clone_session_entity(CLONE_SESSION, CPU_PORT),
                digest_entity(self.index.digest_id("telemetry_hint_t")),
            ],
        )
        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                selector_entity(
                    self.index, "telemetry_selector", "select_telemetry_bank_0", epoch=6
                )
            ],
        )
        for _ in range(1024):
            testutils.send_packet(self, 1, frame)
            time.sleep(0.0005)
        recovered_digest = self.client.wait_for("digest", timeout=10)
        recovered_packet = self.client.wait_for("packet", timeout=10)
        self.client.send_digest_ack(
            recovered_digest.digest.digest_id,
            recovered_digest.digest.list_id,
        )
        self.assertTrue(recovered_packet.HasField("packet"))
        self.assertEqual(self._read_bank_sequence(0), 1024)
        self.dataplane.flush()
        return {
            "loss_injection": "digest config and clone session removed for one exact epoch",
            "deletion_readback": deletion_readback,
            "stale_stream_messages_drained_before_epoch": stale_stream_messages,
            "lost_epoch": 5,
            "eligible_population": 1024,
            "expected_digest_hints": 1,
            "observed_digest_hints": 0,
            "expected_packetin_hints": 1,
            "observed_packetin_hints": 0,
            "coverage": 0.0,
            "quality": "not-covered",
            "reason_bits": ["DIGEST_DROP", "PACKETIN_DROP"],
            "canonical_aggregate_packets_preserved": aggregate_during_loss,
            "recovery_epoch": 6,
            "recovery_hint_observed": True,
            "reliable_packet_fallback": False,
        }

    def _host_filter_contamination(self) -> dict[str, object]:
        table_name = "masi_p4_negative"
        subprocess.run(
            ["nft", "delete", "table", "inet", table_name],
            check=False,
            capture_output=True,
            text=True,
        )
        ruleset = (
            f"add table inet {table_name}\n"
            f"add chain inet {table_name} forward "
            "{ type filter hook forward priority 0; policy accept; }\n"
            f"add rule inet {table_name} forward "
            'iifname "masi-p1" oifname "masi-p2" counter drop\n'
        )
        subprocess.run(
            ["nft", "-f", "-"],
            input=ruleset,
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            before = subprocess.run(
                ["nft", "list", "chain", "inet", table_name, "forward"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn('iifname "masi-p1" oifname "masi-p2"', before)
            self.assertRegex(before, r"counter packets 0 bytes 0 drop")
            frame = self.ipv4_tcp(src="203.0.113.242", sport=44242, dport=80)
            self.expect_forward(frame)
            after = subprocess.run(
                ["nft", "list", "chain", "inet", table_name, "forward"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            match = re.search(r"counter packets (\d+) bytes (\d+) drop", after)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(int(match.group(1)), 0)
        finally:
            subprocess.run(
                ["nft", "delete", "table", "inet", table_name],
                check=True,
                capture_output=True,
                text=True,
            )
        return {
            "contaminating_host_rule": (
                'inet forward iifname "masi-p1" oifname "masi-p2" counter drop'
            ),
            "host_rule_readback": True,
            "host_rule_packets": 0,
            "p4_packet_outcome": "forwarded",
            "interpretation": "raw BMv2 dataplane did not traverse host L3 forward hook",
            "cleanup": "exact nft table deleted",
        }

    def _resource_bounds(self) -> dict[str, object]:
        assert self.client is not None
        with self.assertRaises(ValueError):
            self.client.write(
                p4runtime_pb2.Update.INSERT,
                [table_entity(self.active_rules[0])] * 257,
            )
        overflow = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=0x0B000001,
            action="permit",
            priority=900_000,
        )
        with self.assertRaises(grpc.RpcError):
            self.client.write(p4runtime_pb2.Update.INSERT, [table_entity(overflow)])
        self.assertEqual(self.client.read([table_key_entity(overflow)]), [])

        unknown = p4runtime_pb2.Entity()
        unknown.table_entry.table_id = 0x02FFFFFE
        unknown.table_entry.action.action.action_id = self.index.action_info(
            "baseline_drop"
        ).preamble.id
        with self.assertRaises(grpc.RpcError):
            self.client.write(p4runtime_pb2.Update.INSERT, [unknown])
        self.client.write_batched(
            p4runtime_pb2.Update.DELETE,
            [table_key_entity(rule) for rule in self.active_rules],
            timeout=30,
        )
        self.active_rules = []
        return {
            "max_updates_per_write": 256,
            "max_entities_per_read": 256,
            "baseline_capacity": 4096,
            "entry_4097_rejected": True,
            "unknown_table_rejected": True,
        }

    def _read_policy_bank(self) -> int:
        assert self.client is not None
        selector = selector_entity(
            self.index,
            "policy_selector",
            f"select_policy_bank_{self.active_bank}",
        )
        response = self.client.read([table_key_entity(selector.table_entry)])
        self.assertEqual(len(response), 1)
        action_id = response[0].table_entry.action.action.action_id
        if action_id == self.index.action_info("select_policy_bank_0").preamble.id:
            return 0
        if action_id == self.index.action_info("select_policy_bank_1").preamble.id:
            return 1
        self.fail(f"unexpected selector action id {action_id}")
        raise AssertionError(f"unexpected selector action id {action_id}")

    def _priority_conflict_shadow(self) -> dict[str, object]:
        assert self.client is not None
        frame = self.ipv4_tcp(src="192.0.2.180", sport=42180, dport=9443)
        lower_permit = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.180"), "big"),
            action="permit",
            priority=2_400_000,
            ingress_port=1,
            protocol=6,
            l4_present=1,
            src_port=42180,
            dst_port=9443,
            fragment_class=0,
        )
        higher_drop = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.180"), "big"),
            action="drop",
            priority=2_400_001,
            ingress_port=1,
            protocol=6,
            l4_present=1,
            src_port=42180,
            dst_port=9443,
            fragment_class=0,
        )
        equal_priority_drop = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.180"), "big"),
            action="drop",
            priority=2_400_000,
            ingress_port=1,
            protocol=6,
            l4_present=1,
            src_port=42180,
            dst_port=9443,
            fragment_class=0,
        )
        self.client.write(
            p4runtime_pb2.Update.INSERT,
            [table_entity(lower_permit), table_entity(higher_drop)],
        )
        self.expect_drop(frame)
        self.client.write(p4runtime_pb2.Update.DELETE, [table_key_entity(higher_drop)])
        self.expect_forward(frame)
        with self.assertRaisesRegex(ValueError, "equal-priority"):
            reject_equal_priority_conflicts([lower_permit, equal_priority_drop])
        readback = self.client.read([table_key_entity(lower_permit)])
        self.assertEqual(len(readback), 1)
        self.assertEqual(
            self.index.readback_key(readback[0].table_entry),
            self.index.readback_key(lower_permit),
        )
        self.client.write(p4runtime_pb2.Update.DELETE, [table_key_entity(lower_permit)])
        return {
            "higher_priority_drop_wins": True,
            "lower_priority_permit_observed_after_unshadow": True,
            "equal_priority_conflict_rejected_before_write": True,
            "stable_reason": "EQUAL_PRIORITY_CONFLICT",
            "target_readback_unchanged_after_preflight_rejection": True,
        }

    def _partial_bank_and_selector_response_loss(self) -> dict[str, object]:
        assert self.client is not None
        inactive_bank = 1 - self.active_bank
        first = self.index.baseline_entry(
            inactive_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.181"), "big"),
            action="drop",
            priority=2_410_000,
        )
        second = self.index.baseline_entry(
            inactive_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.182"), "big"),
            action="drop",
            priority=2_410_001,
        )
        self.client.write(p4runtime_pb2.Update.INSERT, [table_entity(first)])
        received = self.client.read_batched(readback_selectors([first, second]))
        expected = {
            self.index.readback_key(first),
            self.index.readback_key(second),
        }
        actual = {
            self.index.readback_key(entity.table_entry)
            for entity in received
            if entity.HasField("table_entry")
        }
        self.assertNotEqual(expected, actual)
        self.assertEqual(self._read_policy_bank(), self.active_bank)
        inactive_probe = self.ipv4_tcp(src="192.0.2.181", sport=42181, dport=9443)
        self.expect_forward(inactive_probe)

        desired_bank = inactive_bank
        unobserved = self.client.write_without_observing_response(
            p4runtime_pb2.Update.MODIFY,
            [
                selector_entity(
                    self.index,
                    "policy_selector",
                    f"select_policy_bank_{desired_bank}",
                )
            ],
        )
        observed_bank = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            observed_bank = self._read_policy_bank()
            if observed_bank == desired_bank:
                break
            time.sleep(0.01)
        self.assertEqual(observed_bank, desired_bank)
        response_was_buffered_but_not_consumed = unobserved.done()
        unobserved.cancel()

        self.client.write(
            p4runtime_pb2.Update.MODIFY,
            [
                selector_entity(
                    self.index,
                    "policy_selector",
                    f"select_policy_bank_{self.active_bank}",
                )
            ],
        )
        self.assertEqual(self._read_policy_bank(), self.active_bank)
        self.client.write(p4runtime_pb2.Update.DELETE, [table_key_entity(first)])
        return {
            "partial_expected_entries": 2,
            "partial_observed_entries": len(actual),
            "selector_held_old_bank_on_partial_readback": True,
            "inactive_partial_entry_did_not_change_active_packet_behavior": True,
            "selector_response_loss_injection": "fixture discarded real unary Write response",
            "response_buffered_not_consumed": response_was_buffered_but_not_consumed,
            "read_only_reconcile_observed_bank": observed_bank,
            "cleanup_restored_bank": self.active_bank,
        }

    def _pipeline_drift(self) -> dict[str, object]:
        assert self.client is not None
        assert self.pipeline_identity is not None
        observed = self.client.verify_pipeline_identity(self.pipeline_identity)
        wrong = replace(
            self.pipeline_identity,
            p4info_digest="sha256:" + ("0" * 64),
        )
        candidate = self.index.baseline_entry(
            self.active_bank,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.183"), "big"),
            action="drop",
            priority=2_420_000,
        )
        with self.assertRaises(PipelineIdentityMismatch):
            self.client.write_if_pipeline_matches(
                wrong,
                p4runtime_pb2.Update.INSERT,
                [table_entity(candidate)],
            )
        self.assertEqual(self.client.read([table_key_entity(candidate)]), [])
        self.assertEqual(
            self.client.verify_pipeline_identity(self.pipeline_identity), observed
        )
        return {
            "get_pipeline_config_readback": True,
            "p4info_proto_digest": observed.p4info_digest,
            "p4info_artifact_digest": "sha256:"
            + hashlib.sha256(
                (self.artifacts / "masi_switch.p4info.txtpb").read_bytes()
            ).hexdigest(),
            "device_config_digest": observed.device_config_digest,
            "cookie_decimal": str(observed.cookie),
            "drift_write_rejected_before_target_mutation": True,
            "stable_reason": PipelineIdentityMismatch.reason_code,
            "target_mode_after_drift": "read-only/HOLD until exact identity restored",
        }

    def _link_recovery(self) -> dict[str, object]:
        frame = self.ipv4_tcp(src="203.0.113.250", sport=45250, dport=80)
        recovery_started = time.perf_counter()
        self.expect_forward(frame)
        subprocess.run(
            ["tc", "qdisc", "add", "dev", "masi-s2", "root", "netem", "loss", "100%"],
            check=True,
        )
        applied = subprocess.run(
            ["tc", "qdisc", "show", "dev", "masi-s2"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertIn("netem", applied)
        self.assertIn("loss 100%", applied)
        try:
            self.expect_drop(frame, timeout=0.2)
        finally:
            subprocess.run(["tc", "qdisc", "del", "dev", "masi-s2", "root"], check=True)

        deadline = time.perf_counter() + 3.0
        attempts = 0
        last_error = ""
        while time.perf_counter() < deadline:
            attempts += 1
            try:
                self.expect_forward(frame, timeout=0.25)
                break
            except AssertionError as exc:
                last_error = str(exc)
                time.sleep(0.05)
        else:
            self.fail(
                "forwarding did not recover within the bounded 3s window; "
                f"last_error={last_error}"
            )
        cleaned = subprocess.run(
            ["tc", "qdisc", "show", "dev", "masi-s2"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotIn("netem", cleaned)
        return {
            "fault": "DUT egress link loss 100%",
            "fault_injection": "tc netem loss 100% on masi-s2",
            "fault_readback": applied,
            "recovery": "forwarding restored after exact qdisc cleanup",
            "recovery_attempts": attempts,
            "recovery_elapsed_ms": round(
                (time.perf_counter() - recovery_started) * 1000, 3
            ),
            "cleanup_readback": cleaned,
        }

    def _netem_recovery(self) -> dict[str, object]:
        frame = self.ipv4_tcp(src="203.0.113.251", sport=45251, dport=8080)
        subprocess.run(
            ["tc", "qdisc", "add", "dev", "masi-s2", "root", "netem", "delay", "10ms"],
            check=True,
        )
        started = time.perf_counter()
        try:
            self.expect_forward(frame, timeout=2.0)
        finally:
            subprocess.run(["tc", "qdisc", "del", "dev", "masi-s2", "root"], check=True)
        delayed_ms = (time.perf_counter() - started) * 1000
        self.assertGreaterEqual(delayed_ms, 5)
        self.expect_forward(frame)
        return {
            "impairment": "netem 10ms on DUT egress",
            "observed_roundtrip_ms": round(delayed_ms, 3),
            "cleanup": "qdisc removed",
            "recovery": "unimpaired packet forwarded",
        }


class MasiSwitchCrashRecovery(_EvidenceTest):
    """Run after the orchestrator SIGKILLs and restarts BMv2."""

    phase_name = "crash-recovery"

    def runTest(self) -> None:
        self.client = self.connect(election_id=PRIMARY_ELECTION_ID + 1)
        self.record("TEST-P4-FAULT-002-process-crash-recovery", self._recover)
        self.write_phase_evidence("PASS")

    def _recover(self) -> dict[str, object]:
        assert self.client is not None
        self.configure_pipeline()
        self.install_infrastructure(with_hints=False)
        rule = self.index.baseline_entry(
            0,
            src_ipv4=int.from_bytes(socket.inet_aton("192.0.2.251"), "big"),
            action="drop",
            priority=100,
            ingress_port=1,
            protocol=6,
            l4_present=1,
            dst_port=22,
            fragment_class=0,
        )
        self.client.write(p4runtime_pb2.Update.INSERT, [table_entity(rule)])
        readback = self.client.read([table_key_entity(rule)])
        self.assertEqual(len(readback), 1)
        self.assertEqual(
            self.index.readback_key(readback[0].table_entry),
            self.index.readback_key(rule),
        )
        self.expect_drop(self.ipv4_tcp(src="192.0.2.251", sport=45251, dport=22))
        self.expect_forward(self.ipv4_tcp(src="192.0.2.252", sport=45252, dport=80))
        return {
            "process_fault": "SIGKILL",
            "state_after_restart": "empty/fail-closed until exact pipeline replay",
            "pipeline_replayed": True,
            "rule_readback": "exact",
            "packet_oracle": "drop and permit verified",
        }


if __name__ == "__main__":
    unittest.main()
