//! Stable public error mapping for Host admission, lifecycle and execution.

use tonic::{Code, Status};

/// Stable reason codes returned at the public boundary.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReasonCode {
    /// The request passed all checks.
    Ok,
    /// A schema or major version is unsupported.
    UnknownVersion,
    /// The plugin kind is not in the closed first-release set.
    UnknownKind,
    /// The runtime profile is not supported by this Host.
    UnknownRuntimeProfile,
    /// A digest does not bind the observed bytes or identity.
    DigestMismatch,
    /// The publisher or offline verification bundle is untrusted.
    PublisherUntrusted,
    /// The artifact or binding is revoked.
    Revoked,
    /// The trust/revocation observation is too old.
    TrustStale,
    /// The binding or invocation is fenced by generation/epoch/result identity.
    Fenced,
    /// The requested capability was not granted.
    CapabilityDenied,
    /// An input, output, queue, fuel, memory or other resource bound was exceeded.
    ResourceExhausted,
    /// Wasm deterministic fuel was exhausted.
    WasmFuelExhausted,
    /// Wasm execution was interrupted by the epoch guard.
    WasmEpochInterrupted,
    /// The total deadline expired.
    DeadlineExceeded,
    /// The caller or lifecycle controller cancelled the invocation.
    Cancelled,
    /// The selected binding is draining and accepts no new work.
    Draining,
    /// A bounded circuit is open or the binding is quarantined.
    CircuitOpen,
    /// The exact Host-managed service identity or handshake did not match.
    ServiceIdentityMismatch,
    /// The plugin trapped or returned a deterministic plugin error.
    PluginTrap,
    /// The request conflicts with an existing idempotency identity.
    IdempotencyConflict,
    /// The requested exact binding is unavailable.
    Unavailable,
    /// Configuration, manifest or payload validation failed.
    InvalidArgument,
    /// An internal Host invariant failed without exposing sensitive detail.
    Internal,
}

impl ReasonCode {
    /// Return the wire-stable uppercase representation.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "OK",
            Self::UnknownVersion => "UNKNOWN_VERSION",
            Self::UnknownKind => "UNKNOWN_KIND",
            Self::UnknownRuntimeProfile => "UNKNOWN_RUNTIME_PROFILE",
            Self::DigestMismatch => "DIGEST_MISMATCH",
            Self::PublisherUntrusted => "PUBLISHER_UNTRUSTED",
            Self::Revoked => "REVOKED",
            Self::TrustStale => "TRUST_STALE",
            Self::Fenced => "FENCED",
            Self::CapabilityDenied => "CAPABILITY_DENIED",
            Self::ResourceExhausted => "RESOURCE_EXHAUSTED",
            Self::WasmFuelExhausted => "WASM_FUEL_EXHAUSTED",
            Self::WasmEpochInterrupted => "WASM_EPOCH_INTERRUPTED",
            Self::DeadlineExceeded => "DEADLINE_EXCEEDED",
            Self::Cancelled => "CANCELLED",
            Self::Draining => "DRAINING",
            Self::CircuitOpen => "CIRCUIT_OPEN",
            Self::ServiceIdentityMismatch => "SERVICE_IDENTITY_MISMATCH",
            Self::PluginTrap => "PLUGIN_TRAP",
            Self::IdempotencyConflict => "IDEMPOTENCY_CONFLICT",
            Self::Unavailable => "UNAVAILABLE",
            Self::InvalidArgument => "INVALID_ARGUMENT",
            Self::Internal => "INTERNAL",
        }
    }
}

impl std::fmt::Display for ReasonCode {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Typed Host error with stable public reason code.
#[derive(Debug, thiserror::Error)]
#[error("{reason}: {message}")]
pub struct HostError {
    /// Stable error reason.
    pub reason: ReasonCode,
    /// Bounded redacted diagnostic.
    pub message: String,
}

impl HostError {
    /// Construct a bounded error.
    #[must_use]
    pub fn new(reason: ReasonCode, message: impl Into<String>) -> Self {
        let mut message = message.into();
        if message.len() > 512 {
            let mut boundary = 512;
            while !message.is_char_boundary(boundary) {
                boundary = boundary.saturating_sub(1);
            }
            message.truncate(boundary);
        }
        Self { reason, message }
    }

    /// Convert to a gRPC status without leaking payload or credential content.
    #[must_use]
    pub fn into_status(self) -> Status {
        let code = match self.reason {
            ReasonCode::UnknownVersion
            | ReasonCode::UnknownKind
            | ReasonCode::UnknownRuntimeProfile
            | ReasonCode::DigestMismatch
            | ReasonCode::InvalidArgument => Code::InvalidArgument,
            ReasonCode::PublisherUntrusted | ReasonCode::CapabilityDenied => Code::PermissionDenied,
            ReasonCode::Revoked | ReasonCode::Fenced | ReasonCode::Draining => {
                Code::FailedPrecondition
            }
            ReasonCode::ResourceExhausted | ReasonCode::WasmFuelExhausted => {
                Code::ResourceExhausted
            }
            ReasonCode::DeadlineExceeded => Code::DeadlineExceeded,
            ReasonCode::Cancelled => Code::Cancelled,
            ReasonCode::IdempotencyConflict => Code::AlreadyExists,
            ReasonCode::Unavailable
            | ReasonCode::TrustStale
            | ReasonCode::CircuitOpen
            | ReasonCode::ServiceIdentityMismatch => Code::Unavailable,
            ReasonCode::PluginTrap
            | ReasonCode::WasmEpochInterrupted
            | ReasonCode::Internal
            | ReasonCode::Ok => Code::Internal,
        };
        Status::new(code, format!("{}: {}", self.reason.as_str(), self.message))
    }
}

/// Convenient Host result alias.
pub type HostResult<T> = Result<T, HostError>;
