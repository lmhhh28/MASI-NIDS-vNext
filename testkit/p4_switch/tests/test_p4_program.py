from __future__ import annotations

# P4Info objects in the pinned protobuf package are descriptor-backed and ship
# without static attribute stubs; scope the exception to those member lookups.
# pyright: reportAttributeAccessIssue=false

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from testkit.p4_switch.lib.p4runtime_client import P4InfoIndex, bmv2_device_config


ROOT = Path(__file__).resolve().parents[3]
PROGRAM = ROOT / "p4/src/masi_switch.p4"


class P4ProgramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="masi-p4-unit-")
        cls.output = Path(cls.temporary.name)
        cls.p4info_path = cls.output / "masi_switch.p4info.txtpb"
        cls.json_path = cls.output / "masi_switch.json"
        subprocess.run(
            [
                str(ROOT / "p4/scripts/compile.sh"),
                str(cls.output),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        cls.index = P4InfoIndex(cls.p4info_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_compiles_with_pinned_container(self) -> None:
        parsed = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertIn("pipelines", parsed)

    def test_p4info_capacity_and_architecture(self) -> None:
        self.assertEqual("v1model", self.index.p4info.pkg_info.arch)
        self.assertEqual(1024, self.index.table("response_overlay").size)
        self.assertEqual(4096, self.index.table("baseline_bank_0").size)
        self.assertEqual(4096, self.index.table("baseline_bank_1").size)
        self.assertEqual(1, self.index.table("policy_selector").size)
        self.assertEqual(1, self.index.table("telemetry_selector").size)

    def test_p4info_contains_required_observation_resources(self) -> None:
        aliases = {counter.preamble.alias for counter in self.index.p4info.counters}
        direct_aliases = {
            counter.preamble.alias for counter in self.index.p4info.direct_counters
        }
        self.assertTrue(
            {
                "response_overlay_eligible_counter",
                "baseline_eligible_counter",
                "telemetry_cell_counter",
                "telemetry_bank_counter",
                "telemetry_class_counter",
            }.issubset(aliases)
        )
        self.assertEqual(
            {
                "response_overlay_direct_counter",
                "baseline_bank_0_direct_counter",
                "baseline_bank_1_direct_counter",
            },
            direct_aliases,
        )
        self.assertEqual(1, len(self.index.p4info.digests))
        self.assertEqual(
            "telemetry_hint_t", self.index.p4info.digests[0].preamble.alias
        )

    def test_profile_entity_ids_and_sizes_match_p4info(self) -> None:
        profile = json.loads(
            (ROOT / "contracts/profiles/v1/p4-stateless-firewall-bmv2.json").read_text(
                encoding="utf-8"
            )
        )["pipeline"]
        for alias, expected in profile["tables"].items():
            actual = self.index.table(alias)
            self.assertEqual(expected["id"], actual.preamble.id, alias)
            self.assertEqual(expected["size"], actual.size, alias)
            if "direct_counter_id" in expected:
                self.assertIn(expected["direct_counter_id"], actual.direct_resource_ids)

        actions = {
            item.preamble.alias: item.preamble.id for item in self.index.p4info.actions
        }
        self.assertEqual(
            profile["actions"],
            {name: actions[name] for name in profile["actions"]},
        )

        counters = {
            item.preamble.alias: (item.preamble.id, item.size)
            for item in self.index.p4info.counters
        }
        for alias, expected in profile["counters"].items():
            self.assertEqual((expected["id"], expected["size"]), counters[alias])

        registers = {
            item.preamble.alias: (item.preamble.id, item.size)
            for item in self.index.p4info.registers
        }
        for alias, expected in profile["registers"].items():
            self.assertEqual((expected["id"], expected["size"]), registers[alias])

        digest = next(
            item
            for item in self.index.p4info.digests
            if item.preamble.alias == "telemetry_hint_t"
        )
        self.assertEqual(
            profile["digest"]["telemetry_hint_t"]["id"], digest.preamble.id
        )
        packet_in = next(
            item
            for item in self.index.p4info.controller_packet_metadata
            if item.preamble.alias == "packet_in"
        )
        self.assertEqual(profile["packet_in"]["metadata_id"], packet_in.preamble.id)

    def test_baseline_wildcards_are_omitted_on_wire(self) -> None:
        entry = self.index.baseline_entry(
            0,
            src_ipv4=0x0A000001,
            action="drop",
            priority=100,
            protocol=6,
        )
        self.assertEqual(2, len(entry.match))
        self.assertEqual(100, entry.priority)

    def test_overlay_requires_supported_ipv4_and_l4(self) -> None:
        entry = self.index.overlay_entry(
            src_ipv4="192.0.2.1",
            dst_ipv4="198.51.100.1",
            protocol=6,
            src_port=40000,
            dst_port=443,
            action="drop",
        )
        self.assertEqual(7, len(entry.match))

    def test_bmv2_device_config_wire_wrapper(self) -> None:
        payload = b'{"program":"masi"}'
        encoded = bmv2_device_config(payload)
        self.assertEqual(0x1A, encoded[0])
        self.assertTrue(encoded.endswith(payload))

    def test_source_does_not_reference_host_firewall_backend(self) -> None:
        source = PROGRAM.read_text(encoding="utf-8").lower()
        for forbidden in ("iptables", "nftables", "ufw"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
