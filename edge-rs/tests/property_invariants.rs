//! Property checks for deterministic compilation and bounded WAL replay.

use masi_edge::{
    contract::edge::{
        BaselineRule, EffectIntent, EffectKind, FirewallAction, Ipv4Prefix, OptionalUint32,
        P4WriteAtomicity,
    },
    digest, firewall,
    wal::{DurableWal, WalKind, WalLimits},
};
use proptest::prelude::*;

fn optional(value: Option<u32>) -> OptionalUint32 {
    OptionalUint32 {
        present: value.is_some(),
        value: value.unwrap_or_default(),
    }
}

fn effect(seed: u32, count: usize) -> EffectIntent {
    let rules = (0..count)
        .map(|index| {
            let identity = format!("property-rule-{}-{}", seed, index);
            BaselineRule {
                rule_id: identity.clone(),
                rule_revision: 1,
                canonical_rule_digest: digest::sha256(identity.as_bytes()),
                priority: 20_000 - index as i32,
                ingress_port: Some(optional(Some((index % 8 + 1) as u32))),
                source: Some(Ipv4Prefix {
                    address: seed.wrapping_add(index as u32),
                    prefix_length: 32,
                }),
                destination: Some(Ipv4Prefix {
                    address: 0xc633_640a,
                    prefix_length: 32,
                }),
                protocol: Some(optional(Some(6))),
                l4_present: Some(optional(Some(1))),
                source_port: Some(optional(None)),
                destination_port: Some(optional(Some(443))),
                fragment_class: Some(optional(Some(0))),
                action: FirewallAction::Drop as i32,
            }
        })
        .collect();
    let mut intent = EffectIntent {
        schema_version: "effect-intent-edge/v1".into(),
        effect_intent_id: format!("property-effect-{}-{}", seed, count),
        operation_id: format!("property-operation-{}-{}", seed, count),
        target_id: "property-target".into(),
        effect_digest: String::new(),
        authorization_digest: digest::sha256(b"property-authorization"),
        kind: EffectKind::BaselineActivate as i32,
        policy_revision_digest: digest::sha256(format!("{}:{}", seed, count).as_bytes()),
        default_action: FirewallAction::PermitAndContinue as i32,
        baseline_rules: rules,
        deadline_unix_ms: 1_893_456_000_000,
        actor_ref: "property-operator".into(),
        reason_code: "BASELINE_ACTIVATION".into(),
        trace_id: "property-trace".into(),
        required_write_atomicity: P4WriteAtomicity::ContinueOnError as i32,
        ..EffectIntent::default()
    };
    let mut canonical = intent.clone();
    canonical.effect_digest.clear();
    intent.effect_digest = digest::message_sha256(&canonical);
    intent
}

proptest! {
    #[test]
    fn firewall_compilation_is_deterministic(
        seed in any::<u32>(),
        count in 0_usize..64,
        active_bank in 0_u32..2,
    ) {
        let intent = effect(seed, count);
        let first = firewall::compile(&intent, active_bank, 4096, 1024)
            .map_err(|error| TestCaseError::fail(error.to_string()))?;
        let second = firewall::compile(&intent, active_bank, 4096, 1024)
            .map_err(|error| TestCaseError::fail(error.to_string()))?;
        prop_assert_eq!(first.contract, second.contract);
        prop_assert_eq!(first.entries, second.entries);
    }

    #[test]
    fn wal_replay_preserves_arbitrary_bounded_payloads(
        payloads in prop::collection::vec(prop::collection::vec(any::<u8>(), 0..512), 0..32),
    ) {
        let directory = tempfile::tempdir()
            .map_err(|error| TestCaseError::fail(error.to_string()))?;
        let limits = WalLimits {
            max_bytes: 1_048_576,
            max_records: 128,
            segment_bytes: 4_096,
            max_record_bytes: 1_024,
            max_age_seconds: 86_400,
        };
        let mut wal = DurableWal::open(directory.path(), WalKind::Source, limits)
            .map_err(|error| TestCaseError::fail(error.to_string()))?;
        for payload in &payloads {
            wal.append(payload)
                .map_err(|error| TestCaseError::fail(error.to_string()))?;
        }
        let replay = wal.replay(128, 1_048_576)
            .map_err(|error| TestCaseError::fail(error.to_string()))?;
        let observed: Vec<Vec<u8>> = replay.into_iter().map(|record| record.payload).collect();
        prop_assert_eq!(payloads, observed);
    }
}
