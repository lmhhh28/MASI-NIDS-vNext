#![allow(missing_docs)]

use masi_plugin_host::admission::{compute_capability_digest, sha256_bytes};
use masi_plugin_host::config::validate_digest;
use masi_plugin_host::{HostError, ReasonCode};
use proptest::prelude::*;

proptest! {
    #[test]
    fn capability_digest_is_permutation_stable(mut values in proptest::collection::vec("[a-z][a-z0-9._-]{0,15}", 0..32)) {
        values.sort();
        values.dedup();
        let expected = compute_capability_digest(&values);
        values.reverse();
        prop_assert_eq!(compute_capability_digest(&values), expected);
    }

    #[test]
    fn sha256_text_is_always_canonical(bytes in proptest::collection::vec(any::<u8>(), 0..4096)) {
        let digest = sha256_bytes(&bytes);
        prop_assert!(validate_digest(&digest).is_ok());
        prop_assert_eq!(digest.len(), 71);
    }

    #[test]
    fn error_messages_are_bounded(message in ".{0,4096}") {
        let error = HostError::new(ReasonCode::InvalidArgument, message);
        prop_assert!(error.message.len() <= 512);
    }
}

#[test]
fn stable_reason_codes_are_unique_and_closed() {
    let values = [
        ReasonCode::Ok,
        ReasonCode::UnknownVersion,
        ReasonCode::UnknownKind,
        ReasonCode::UnknownRuntimeProfile,
        ReasonCode::DigestMismatch,
        ReasonCode::PublisherUntrusted,
        ReasonCode::Revoked,
        ReasonCode::TrustStale,
        ReasonCode::Fenced,
        ReasonCode::CapabilityDenied,
        ReasonCode::ResourceExhausted,
        ReasonCode::WasmFuelExhausted,
        ReasonCode::WasmEpochInterrupted,
        ReasonCode::DeadlineExceeded,
        ReasonCode::Cancelled,
        ReasonCode::Draining,
        ReasonCode::CircuitOpen,
        ReasonCode::ServiceIdentityMismatch,
        ReasonCode::PluginTrap,
        ReasonCode::IdempotencyConflict,
        ReasonCode::Unavailable,
        ReasonCode::InvalidArgument,
        ReasonCode::Internal,
    ];
    let unique: std::collections::BTreeSet<_> = values.iter().map(|value| value.as_str()).collect();
    assert_eq!(unique.len(), values.len());
}
