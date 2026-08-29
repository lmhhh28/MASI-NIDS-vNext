from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[3]


def load(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class ContractGoldenTests(unittest.TestCase):
    def assert_valid(self, schema_path: str, document_path: str) -> None:
        schema = load(schema_path)
        document = load(document_path)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(
            validator.iter_errors(document), key=lambda item: list(item.path)
        )
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def test_firewall_policy_golden(self) -> None:
        self.assert_valid(
            "contracts/p4/firewall-policy/v1/schema.json",
            "contracts/golden/p4/firewall-policy-v1.json",
        )

    def test_rule_observation_golden(self) -> None:
        self.assert_valid(
            "contracts/p4/rule-observation/v1/schema.json",
            "contracts/golden/p4/rule-observation-v1.json",
        )

    def test_telemetry_golden(self) -> None:
        self.assert_valid(
            "contracts/telemetry/v1/schema.json",
            "contracts/golden/telemetry/snapshot-v1.json",
        )

    def test_soak_golden(self) -> None:
        self.assert_valid(
            "contracts/evidence/soak/v1/schema.json",
            "contracts/golden/evidence/soak-v1.json",
        )

    def test_performance_environment_profile(self) -> None:
        self.assert_valid(
            "contracts/performance-environment/v1/schema.json",
            "contracts/profiles/v1/performance-environment-p4-bmv2-wsl2.json",
        )

    def test_soak_pass_rejects_short_elapsed_time(self) -> None:
        schema = load("contracts/evidence/soak/v1/schema.json")
        evidence = load("contracts/golden/evidence/soak-v1.json")
        evidence.update(
            {
                "level": "MODULE",
                "result": "PASS",
                "qualification": "QUALIFIED",
                "interruption": "NONE",
            }
        )
        errors = list(Draft202012Validator(schema).iter_errors(evidence))
        self.assertTrue(any("3600000" in error.message for error in errors))

    def test_soak_pass_rejects_cleanup_residual(self) -> None:
        schema = load("contracts/evidence/soak/v1/schema.json")
        evidence = load("contracts/golden/evidence/soak-v1.json")
        evidence.update(
            {
                "level": "MODULE",
                "result": "PASS",
                "qualification": "QUALIFIED",
                "qualified_elapsed_ms": 3_600_000,
                "interruption": "NONE",
            }
        )
        evidence["cleanup"]["remaining_resources"] = ["masi-p1"]
        errors = list(Draft202012Validator(schema).iter_errors(evidence))
        self.assertTrue(errors)

    def test_all_four_traffic_modes(self) -> None:
        schema = "contracts/testkit/traffic-replay/v1/schema.json"
        documents = sorted((ROOT / "testkit/fixtures/traffic").glob("*.json"))
        runner_profile = load("contracts/profiles/v1/e2e-runner-compose.json")
        traffic_profile = load(
            "contracts/profiles/v1/p4-traffic-replay-bmv2-compose.json"
        )
        self.assertEqual(
            runner_profile["runner_environment"], traffic_profile["runner_environment"]
        )
        modes = set()
        for document in documents:
            relative = document.relative_to(ROOT).as_posix()
            self.assert_valid(schema, relative)
            fixture = load(relative)
            self.assertEqual(
                runner_profile["runner_image_digest"],
                fixture["execution"]["runner_image_digest"],
            )
            self.assertEqual(
                runner_profile["runner_environment"],
                fixture["impairment_environment"]["runner_environment"],
            )
            modes.add(fixture["fixture_class"])
            expected_manifest_digest = fixture.pop("manifest_digest")
            canonical = json.dumps(
                fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            self.assertEqual(
                expected_manifest_digest,
                "sha256:" + hashlib.sha256(canonical).hexdigest(),
            )
            self.assertEqual(
                [
                    "prepare",
                    "preflight-readback",
                    "send-or-session",
                    "observe-oracle",
                    "cleanup",
                ],
                fixture["phases"],
            )
            self.assertNotEqual(
                "sha256:" + ("9" * 64), fixture["execution"]["runner_image_digest"]
            )
        self.assertEqual(
            {"generated-packet", "synthetic-flow", "curated-pcap", "live-session"},
            modes,
        )

    def test_traffic_schema_rejects_stale_runner_version(self) -> None:
        schema = load("contracts/testkit/traffic-replay/v1/schema.json")
        fixture = load("testkit/fixtures/traffic/generated-ipv4-tcp-v1.json")
        fixture["impairment_environment"]["runner_environment"]["iproute2"][
            "tool_version"
        ] = "6.15.0"
        errors = list(Draft202012Validator(schema).iter_errors(fixture))
        self.assertTrue(errors)

    def test_unknown_major_is_rejected(self) -> None:
        schema = load("contracts/p4/firewall-policy/v1/schema.json")
        golden = load("contracts/golden/p4/firewall-policy-v1.json")
        golden["schema_version"] = "p4-firewall-policy/v2"
        errors = list(Draft202012Validator(schema).iter_errors(golden))
        self.assertTrue(errors)

    def test_rule_capacity_is_bounded(self) -> None:
        schema = load("contracts/p4/firewall-policy/v1/schema.json")
        golden = load("contracts/golden/p4/firewall-policy-v1.json")
        rule = golden["rules"][0]
        golden["rules"] = [copy.deepcopy(rule) for _ in range(4097)]
        errors = list(Draft202012Validator(schema).iter_errors(golden))
        self.assertTrue(any("too long" in error.message for error in errors))

    def test_firewall_golden_has_compiled_plan_and_canonical_default(self) -> None:
        golden = load("contracts/golden/p4/firewall-policy-v1.json")
        self.assertEqual("permit-and-continue", golden["default_action"])
        self.assertEqual(1, golden["compiled_plan"]["inactive_bank"])
        self.assertEqual(
            golden["bank"],
            golden["compiled_plan"]["selector"]["expected_bank"],
        )

    def test_profile_has_frozen_resource_bounds(self) -> None:
        profile = load("contracts/profiles/v1/p4-stateless-firewall-bmv2.json")
        self.assertEqual("p4-stateless-firewall/v1", profile["profile_id"])
        self.assertEqual(4096, profile["capacity"]["baseline_entries_per_bank"])
        self.assertEqual(2, profile["capacity"]["baseline_banks"])
        self.assertEqual(256, profile["atomicity_readback"]["max_updates_per_write"])
        self.assertEqual("mutual-tls", profile["security"]["p4runtime_transport"])
        self.assertEqual(
            "OWNER_FROZEN", profile["performance_gate"]["absolute_threshold_status"]
        )
        self.assertEqual(3600, profile["performance_gate"]["soak_duration_seconds"])
        runner_profile = load("contracts/profiles/v1/e2e-runner-compose.json")
        self.assertEqual(
            32 * 1024 * 1024 * 1024, runner_profile["supply_chain_minimum_free_bytes"]
        )
        self.assertEqual(
            33554689, profile["pipeline"]["tables"]["response_overlay"]["id"]
        )
        self.assertEqual(
            369099032, profile["pipeline"]["registers"]["telemetry_bank_sequence"]["id"]
        )
        self.assertEqual("1.4.1", profile["target"]["p4runtime_capabilities_version"])

        performance = load("contracts/profiles/v1/p4-bmv2-functional-reference.json")
        workload_bounds = performance["workload_resource_bounds"]
        self.assertEqual(4_000_000, workload_bounds["maximum_packets_per_run"])
        self.assertEqual(400, workload_bounds["maximum_samples_per_run"])
        self.assertEqual(65_536, workload_bounds["latency_samples_per_phase"])
        self.assertEqual(
            "deterministic-reservoir-splitmix64/v1",
            workload_bounds["latency_sampling_method"],
        )
        self.assertTrue(workload_bounds["latency_exact_max"])
        packet_oracle = performance["packet_oracle_runtime"]
        self.assertEqual("multiprocessing-spawn/v1", packet_oracle["sender_execution"])
        self.assertEqual(64, packet_oracle["sender_progress_publish_packets"])
        self.assertEqual(
            16 * 1024 * 1024,
            packet_oracle["capture_receive_buffer_requested_bytes"],
        )
        self.assertTrue(packet_oracle["capture_packet_statistics_required"])
        self.assertEqual(2000, packet_oracle["capture_drain_timeout_ms"])

    def test_p4runtime_compatibility_requires_exact_1_4_1(self) -> None:
        compatibility = load("contracts/profiles/v1/p4runtime-edge-compatibility.json")
        self.assertEqual(["1.4.1"], compatibility["accepted_capabilities_api_versions"])
        self.assertEqual(
            "P4Runtime-1.4.1",
            compatibility["compatibility_basis"]["required_common_wire_surface"],
        )
        decisions = compatibility["version_decision"]
        self.assertEqual("ACCEPT_FOR_PINNED_BMV2_PROFILE", decisions["1.4.1"])
        for rejected in ("1.3.0", "1.4.0", "unknown"):
            self.assertTrue(decisions[rejected].startswith("REJECT"))

    def test_older_p4runtime_capabilities_are_rejected_by_contracts(self) -> None:
        target_schema = load("contracts/target/v1/schema.json")
        assignment = load("contracts/golden/target/assignment-v1.json")
        assignment["expected_pipeline"]["p4runtime_api_version"] = "1.3.0"
        self.assertTrue(
            list(Draft202012Validator(target_schema).iter_errors(assignment))
        )

        firewall_schema = load("contracts/p4/firewall-policy/v1/schema.json")
        firewall = load("contracts/golden/p4/firewall-policy-v1.json")
        firewall["pipeline_identity"]["p4runtime_version"] = "1.3.0"
        self.assertTrue(
            list(Draft202012Validator(firewall_schema).iter_errors(firewall))
        )

    def test_evidence_schema_requires_orthogonal_test_status(self) -> None:
        schema = load("contracts/evidence/v1/schema.json")
        self.assertTrue(
            {
                "findings",
                "completion",
                "overall_module_complete",
                "source_revision",
                "source_tree_digest",
                "working_tree_dirty",
                "working_tree_status_digest",
                "phase_bindings",
            }.issubset(schema["required"])
        )
        self.assertEqual(12, schema["properties"]["phase_bindings"]["minItems"])
        required = set(schema["properties"]["tests"]["items"]["required"])
        self.assertTrue(
            {
                "requirement_ids",
                "level",
                "applicability",
                "result",
                "qualification",
            }.issubset(required)
        )

    def test_p4_module_findings_registry_is_public_and_closed(self) -> None:
        self.assert_valid(
            "contracts/evidence/module-findings/v1/schema.json",
            "p4/module-findings.json",
        )
        registry = load("p4/module-findings.json")
        self.assertEqual("MOD-SW-001", registry["module_id"])
        self.assertFalse(
            any(
                finding["status"] == "OPEN" and finding["severity"] == "P0"
                for finding in registry["findings"]
            )
        )

    def test_formal_source_inputs_are_durable_and_supply_snapshot_excludes_builds(
        self,
    ) -> None:
        runner = (ROOT / "testkit/p4_switch/run-module-e2e.sh").read_text(
            encoding="utf-8"
        )
        supply = (ROOT / "testkit/p4_switch/scripts/run-supply-chain.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("source-inputs", runner)
        self.assertIn(".source_archive.filename", runner)
        self.assertIn(".source_archive.sha256", runner)
        self.assertIn(".source_archive.size_bytes", runner)
        for excluded in ("**/build", "**/target", "**/dist"):
            self.assertIn(f"--exclude='{excluded}'", supply)

    def test_mininet_profile_pins_real_external_target_topology(self) -> None:
        profile = load("contracts/profiles/v1/p4-mininet-bmv2.json")
        self.assertEqual("p4-mininet-bmv2/v1", profile["profile_id"])
        self.assertEqual("2.3.0-1ubuntu1", profile["mininet"]["package_version"])
        self.assertEqual(1000, profile["mininet"]["max_queue_packets"])
        self.assertEqual("mutual-tls", profile["target"]["p4runtime_transport"])
        self.assertEqual(
            "independent tcpdump capture in h2 namespace",
            profile["oracles"]["outcome"],
        )

    def test_rule_observation_distinguishes_target_and_client_versions(self) -> None:
        profile = load("contracts/profiles/v1/p4-rule-observation-bmv2.json")
        self.assertEqual("1.4.1", profile["p4runtime_capabilities_version"])
        self.assertEqual("1.4.1", profile["test_client_proto_package_version"])
        self.assertNotIn("p4runtime_version", profile)

    def test_supply_registry_has_no_unclassified_component(self) -> None:
        registry = load("contracts/supply-chain/v1/p4-switch-components.json")
        required = {
            "name",
            "decision",
            "license",
            "authority",
            "failure",
            "rollback",
            "exit",
        }
        self.assertGreaterEqual(len(registry["components"]), 6)
        for component in registry["components"]:
            self.assertTrue(required.issubset(component), component["name"])
            self.assertIn(component["decision"], {"ADOPT", "CONDITIONAL", "REJECT"})

        tcpreplay = next(
            item for item in registry["components"] if item["name"] == "Tcpreplay"
        )
        self.assertEqual("CONDITIONAL", tcpreplay["decision"])
        self.assertEqual("4.5.2", tcpreplay["version"])
        self.assertEqual("4.5.2-r1", tcpreplay["package_version"])
        iproute2 = next(
            item for item in registry["components"] if item["name"] == "iproute2"
        )
        self.assertEqual("7.0.0", iproute2["version"])
        self.assertEqual("7.0.0-r0", iproute2["package_version"])

        runner_profile = load("contracts/profiles/v1/e2e-runner-compose.json")
        self.assertEqual(
            "python@sha256:42825e7ec3437b3bce923c237484eb23d32128476e18307d2f48951bf86f1db2",
            runner_profile["runner_base_image"],
        )
        self.assertEqual(
            runner_profile["runner_environment"],
            load("contracts/profiles/v1/p4-traffic-replay-bmv2-compose.json")[
                "runner_environment"
            ],
        )
        dockerfile = (ROOT / "deploy/p4-switch/Dockerfile.runner").read_text(
            encoding="utf-8"
        )
        for expected in (
            'io.masi-nids.runner-os="Alpine Linux 3.24.1"',
            'io.masi-nids.iproute2.tool-version="7.0.0"',
            'io.masi-nids.iproute2.package-version="7.0.0-r0"',
            'io.masi-nids.tcpreplay.tool-version="4.5.2"',
            'io.masi-nids.tcpreplay.package-version="4.5.2-r1"',
        ):
            self.assertIn(expected, dockerfile)

    def test_bmv2_source_patch_is_exactly_bound_across_profiles(self) -> None:
        source_lock = load("deploy/p4-switch/bmv2/source.lock.json")
        profile = load("contracts/profiles/v1/p4-stateless-firewall-bmv2.json")
        registry = load("contracts/supply-chain/v1/p4-switch-components.json")
        patch = source_lock["patch"]
        patch_digest = (
            "sha256:" + hashlib.sha256((ROOT / patch["path"]).read_bytes()).hexdigest()
        )
        bmv2 = next(
            item
            for item in registry["components"]
            if item["name"] == "BMv2 behavioral-model"
        )

        self.assertEqual(patch_digest, "sha256:" + patch["sha256"])
        self.assertEqual(
            patch_digest, profile["target"]["qualified_source_patch_sha256"]
        )
        self.assertEqual(patch_digest, bmv2["patch_sha256"])
        self.assertEqual(
            patch["scopes"], profile["target"]["qualified_source_patch_scopes"]
        )
        self.assertEqual(patch["scopes"], bmv2["patch_scopes"])
        dockerfile = (ROOT / "deploy/p4-switch/bmv2/Dockerfile.runtime").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'io.masi-nids.patch.sha256="{patch["sha256"]}"', dockerfile)

    def test_pi_p4runtime_1_4_1_source_is_exactly_bound_and_rebuildable(self) -> None:
        source_lock = load("deploy/p4-switch/pi/source.lock.json")
        profile = load("contracts/profiles/v1/p4-stateless-firewall-bmv2.json")
        registry = load("contracts/supply-chain/v1/p4-switch-components.json")
        patch = source_lock["patch"]
        patch_digest = (
            "sha256:" + hashlib.sha256((ROOT / patch["path"]).read_bytes()).hexdigest()
        )
        pi = next(
            item
            for item in registry["components"]
            if item["name"] == "PI P4Runtime server"
        )
        p4runtime = source_lock["submodules"]["proto/p4runtime"]

        self.assertEqual("v1.4.1", p4runtime["release"])
        self.assertEqual("1.4.1", profile["target"]["p4runtime_capabilities_version"])
        self.assertEqual(
            p4runtime["commit"], profile["target"]["p4runtime_source_commit"]
        )
        self.assertEqual(p4runtime["commit"], pi["p4runtime_source_commit"])
        self.assertEqual(
            "sha256:" + source_lock["source_archive"]["sha256"],
            profile["target"]["pi_source_archive_sha256"],
        )
        self.assertEqual(patch_digest, "sha256:" + patch["sha256"])
        self.assertEqual(patch_digest, profile["target"]["qualified_pi_patch_sha256"])
        self.assertEqual(patch_digest, pi["patch_sha256"])
        self.assertEqual(patch["scopes"], pi["patch_scopes"])
        self.assertEqual(
            profile["target"]["runtime_image"].split("@", 1)[1],
            pi["runtime_image_digest"],
        )

        dockerfile = (ROOT / "deploy/p4-switch/bmv2/Dockerfile.runtime").read_text(
            encoding="utf-8"
        )
        build_script = (ROOT / "deploy/p4-switch/bmv2/build-runtime.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('io.masi-nids.p4runtime.version="1.4.1"', dockerfile)
        self.assertIn(f'io.masi-nids.pi.patch.sha256="{patch["sha256"]}"', dockerfile)
        self.assertIn("ldconfig && rm -rf /var/cache/ldconfig", dockerfile)
        self.assertIn("PI_SOURCE_ARCHIVE", build_script)
        self.assertIn(".masi-pi-source", build_script)
        self.assertIn('p4runtime_api_version[] = "1.4.1"', build_script)


if __name__ == "__main__":
    unittest.main()
