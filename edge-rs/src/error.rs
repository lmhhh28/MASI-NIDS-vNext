//! Stable fail-closed error semantics.

use tonic::{Code, Status};

/// Edge operation result.
pub type EdgeResult<T> = Result<T, EdgeError>;

/// Complete frozen set returned by [`EdgeError::reason_code`].
///
/// Category prefixes and gRPC codes are deliberately defined separately in
/// `contracts/edge/v1/error-semantics.json`.
pub const STABLE_REASON_CODES: &[&str] = &[
    "ACTOR_QUEUE_FULL",
    "ACTOR_UNAVAILABLE",
    "ASSIGNMENT_CONFLICT",
    "ASSIGNMENT_LEDGER_CORRUPT",
    "ASSIGNMENT_LEDGER_UNAVAILABLE",
    "ASSIGNMENT_STALE",
    "ATOMICITY_UNSUPPORTED",
    "BINDING_READBACK_MISMATCH",
    "BOUNDED_CAPTURE_PROFILE_UNSUPPORTED",
    "CANONICAL_ACK_MISMATCH",
    "CAPABILITY_DRIFT",
    "CAPABILITY_READ_FAILED",
    "CAPACITY_EXCEEDED",
    "CHECKPOINT_OUT_OF_RANGE",
    "CLOCK_INVALID",
    "CONFIG_TOO_LARGE",
    "CONTROL_MESSAGE_TOO_LARGE",
    "CONTROL_UNAVAILABLE",
    "COUNTER_READBACK_INVALID",
    "COUNTER_READBACK_MISSING",
    "DEADLINE_EXCEEDED",
    "DIGEST_ACK_QUEUE_FULL",
    "DIGEST_ACK_RECOVERY_LIMIT",
    "DIGEST_INTERNAL",
    "EDGE_DATA_PATH_INVALID",
    "EDGE_PROCESS_CONFLICT",
    "EDGE_SERVER_FAILED",
    "EFFECT_ACK_PENDING",
    "EFFECT_DIGEST_CONFLICT",
    "EFFECT_RECONCILE_REQUIRED",
    "EFFECT_RECOVERY_ABSENT",
    "ELECTION_OUT_OF_RANGE",
    "ENDPOINT_NOT_ALLOWED",
    "EQUAL_PRIORITY_CONFLICT",
    "FEATURE_CONTRACT_MISMATCH",
    "INFERENCE_MESSAGE_TOO_LARGE",
    "INPUT_CONTRACT_MISMATCH",
    "INPUT_DIGEST_CONFLICT",
    "INPUT_IDENTITY_CONFLICT",
    "INPUT_IDENTITY_LIMIT_EXCEEDED",
    "INPUT_RECOVERY_LIMIT",
    "INVALID_ARGUMENT",
    "JOURNAL_RECOVERY_LIMIT",
    "LEASE_EXPIRED",
    "LEASE_ID_MISMATCH",
    "LOCAL_IO",
    "NOT_PRIMARY",
    "OBSERVATION_BATCH_TOO_LARGE",
    "OBSERVATION_CONFIG_DIGEST_CONFLICT",
    "OBSERVATION_COUNTER_MISMATCH",
    "OBSERVATION_ENTRY_DIGEST_CONFLICT",
    "OBSERVATION_EPOCH_MISMATCH",
    "OBSERVATION_LIMIT_EXCEEDED",
    "OBSERVATION_QUEUE_FULL",
    "OBSERVATION_REPLACE_ALL_REQUIRED",
    "OBSERVATION_TABLE_UNSUPPORTED",
    "OVERLAY_EXPIRED",
    "OVERLAY_TTL_EXCEEDED",
    "P4INFO_DRIFT",
    "P4_READ_FAILED",
    "P4_READ_RESPONSE_TOO_LARGE",
    "P4_READ_RESPONSE_TOO_MANY_ENTITIES",
    "P4_STREAM_CLOSED",
    "P4_STREAM_FAILED",
    "P4_STREAM_OPEN_FAILED",
    "P4_STREAM_REQUEST_QUEUE_FULL",
    "P4_WRITE_FAILED",
    "PACKET_IN_TOO_LARGE",
    "PIPELINE_DRIFT",
    "PIPELINE_READ_FAILED",
    "POOL_UNAVAILABLE",
    "PREFLIGHT_EXPIRED",
    "PREFLIGHT_LIMIT_EXCEEDED",
    "PREFLIGHT_REPLAY",
    "PUBLISH_ACK_MISMATCH",
    "READBACK_MISMATCH",
    "RESULT_DIGEST_CONFLICT",
    "RESULT_IDENTITY_MISMATCH",
    "RESULT_VALUE_INVALID",
    "ROLE_ELECTION_MISMATCH",
    "ROUTE_DRAINING",
    "ROUTE_DRAIN_REQUIRED",
    "ROUTE_FENCE_MISMATCH",
    "ROUTE_NOT_READY",
    "ROUTE_RECOVERY_LIMIT",
    "ROUTE_RECOVERY_MISSING",
    "ROUTE_RECOVERY_PENDING",
    "RUNTIME_PROFILE_UNSUPPORTED",
    "SELECTOR_MISMATCH",
    "SNAPSHOT_INCONSISTENT",
    "SNAPSHOT_QUALITY_MISMATCH",
    "SOURCE_ACK_PENDING",
    "SOURCE_IDENTITY_DRIFT",
    "SOURCE_PROFILE_MISMATCH",
    "SOURCE_RECOVERY_ABSENT",
    "SOURCE_RECOVERY_LIMIT",
    "TARGET_ALREADY_ASSIGNED",
    "TARGET_DATA_PATH_INVALID",
    "TARGET_FENCE_MISMATCH",
    "TARGET_LIMIT_EXCEEDED",
    "TARGET_NOT_ASSIGNED",
    "TELEMETRY_SELECTOR_DRIFT",
    "TELEMETRY_SELECTOR_MISMATCH",
    "TLS_IDENTITY_MISMATCH",
    "TLS_IDENTITY_REUSE",
    "TLS_IDENTITY_UNKNOWN",
    "UNKNOWN_MAJOR",
    "UNSUPPORTED_FRAGMENT",
    "UNSUPPORTED_L4_UNAVAILABLE",
    "WAL_CLOCK_UNPROVABLE",
    "WAL_CORRUPT",
    "WAL_FULL",
    "WAL_PATH_INVALID",
    "WAL_RECORD_TOO_LARGE",
    "WAL_RETENTION_EXPIRED",
    "WAL_SEQUENCE_CONFLICT",
    "WAL_WRITER_CONFLICT",
    "WINDOW_CONFIG_INVALID",
    "WINDOW_LIMIT_EXCEEDED",
    "WINDOW_PROFILE_MISMATCH",
    "WINDOW_STATE_LOST",
    "WRITE_OUTCOME_UNCONFIRMED",
];

/// Non-error reason codes emitted in typed status/result records.
pub const STABLE_STATUS_REASON_CODES: &[&str] = &[
    "BANK_FREEZE_STARTED",
    "BANK_FROZEN",
    "BANK_FROZEN_RECOVERED",
    "BASELINE_ACTIVATION",
    "BINDING_READBACK_EXACT",
    "CANONICAL_OBSERVATION_SET_REPLACED",
    "CLEAR_COMPLETED",
    "CONNECTING",
    "CONTROL_UNAVAILABLE",
    "DIGEST_HINT_ACKED",
    "DIGEST_HINT_DURABLE",
    "EXACT_READBACK",
    "GO_CANONICAL_BINDING_COMMITTED",
    "LEASE_EXPIRED",
    "NONE",
    "NOT_PRIMARY",
    "OK",
    "P4_STREAM_CLOSED",
    "P4_STREAM_ERROR",
    "PACKET_HINT_DURABLE",
    "POOL_UNAVAILABLE",
    "POSTGRESQL_CAS_CONFIRMED",
    "POSTGRESQL_EVENT_COMMIT_ACK_DURABLE",
    "PREFLIGHT_ACCEPTED",
    "PRIMARY",
    "RECOVERED_EXACT_READBACK",
    "RESULT_BATCH_DURABLE",
    "ROUTE_ACTIVE",
    "ROUTE_DRAINING",
    "ROUTE_HOLD",
    "ROUTE_PREPARED",
    "ROUTE_READY",
    "ROUTE_WITHDRAWN",
    "RULE_EXPIRED_AWAITING_DURABLE_DELETE_INTENT",
    "SEQUENCE_GAP",
    "SNAPSHOT_DURABLE",
    "SNAPSHOT_FINALIZED",
    "SOURCE_START_UNKNOWN",
    "SUPPLEMENTAL_HINT_DROPPED",
    "WRITE_STARTED",
];

/// Errors exposed by Edge public boundaries.
#[derive(Debug, thiserror::Error)]
pub enum EdgeError {
    /// Contract argument is invalid.
    #[error("INVALID_ARGUMENT:{field}:{message}")]
    InvalidArgument {
        /// Stable field name.
        field: &'static str,
        /// Safe diagnostic without secret material.
        message: String,
    },
    /// An unknown contract major was supplied.
    #[error("UNKNOWN_MAJOR:{0}")]
    UnknownMajor(String),
    /// An endpoint is outside the registry allowlist.
    #[error("ENDPOINT_NOT_ALLOWED:{0}")]
    EndpointNotAllowed(String),
    /// TLS configuration or identity verification failed.
    #[error("TLS_IDENTITY_MISMATCH:{0}")]
    TlsIdentityMismatch(String),
    /// A bounded resource is exhausted.
    #[error("RESOURCE_EXHAUSTED:{code}:{message}")]
    ResourceExhausted {
        /// More precise stable reason.
        code: &'static str,
        /// Safe diagnostic.
        message: String,
    },
    /// A fence or lifecycle precondition is false.
    #[error("PRECONDITION_FAILED:{code}:{message}")]
    Precondition {
        /// Stable reason.
        code: &'static str,
        /// Safe diagnostic.
        message: String,
    },
    /// An operation exceeded its explicit deadline.
    #[error("DEADLINE_EXCEEDED:{0}")]
    Deadline(String),
    /// Durable local state is corrupt.
    #[error("WAL_CORRUPT:{0}")]
    WalCorrupt(String),
    /// Local filesystem operation failed.
    #[error("LOCAL_IO:{context}:{source}")]
    Io {
        /// Static operation context.
        context: &'static str,
        /// Underlying error.
        #[source]
        source: std::io::Error,
    },
    /// A remote boundary returned a stable failure.
    #[error("REMOTE_FAILURE:{code}:{message}")]
    Remote {
        /// Stable remote reason.
        code: &'static str,
        /// Safe diagnostic.
        message: String,
    },
    /// An external effect was attempted but exact outcome cannot be confirmed.
    #[error("WRITE_OUTCOME_UNCONFIRMED:{0}")]
    UnknownOutcome(String),
    /// Internal actor channel was closed during shutdown or failure.
    #[error("ACTOR_UNAVAILABLE:{0}")]
    ActorUnavailable(String),
}

impl EdgeError {
    /// Construct an invalid-argument error.
    #[must_use]
    pub fn invalid(field: &'static str, message: impl Into<String>) -> Self {
        Self::InvalidArgument {
            field,
            message: message.into(),
        }
    }

    /// Construct an explicit resource exhaustion error.
    #[must_use]
    pub fn exhausted(code: &'static str, message: impl Into<String>) -> Self {
        Self::ResourceExhausted {
            code,
            message: message.into(),
        }
    }

    /// Construct a fence / lifecycle precondition error.
    #[must_use]
    pub fn precondition(code: &'static str, message: impl Into<String>) -> Self {
        Self::Precondition {
            code,
            message: message.into(),
        }
    }

    /// Construct a local I/O error with a non-secret context.
    #[must_use]
    pub fn io(context: &'static str, source: std::io::Error) -> Self {
        Self::Io { context, source }
    }

    /// Return the stable reason code.
    #[must_use]
    pub fn reason_code(&self) -> &'static str {
        match self {
            Self::InvalidArgument { .. } => "INVALID_ARGUMENT",
            Self::UnknownMajor(_) => "UNKNOWN_MAJOR",
            Self::EndpointNotAllowed(_) => "ENDPOINT_NOT_ALLOWED",
            Self::TlsIdentityMismatch(_) => "TLS_IDENTITY_MISMATCH",
            Self::ResourceExhausted { code, .. }
            | Self::Precondition { code, .. }
            | Self::Remote { code, .. } => code,
            Self::Deadline(_) => "DEADLINE_EXCEEDED",
            Self::WalCorrupt(_) => "WAL_CORRUPT",
            Self::Io { .. } => "LOCAL_IO",
            Self::UnknownOutcome(_) => "WRITE_OUTCOME_UNCONFIRMED",
            Self::ActorUnavailable(_) => "ACTOR_UNAVAILABLE",
        }
    }
}

impl From<EdgeError> for Status {
    fn from(error: EdgeError) -> Self {
        let code = match error {
            EdgeError::InvalidArgument { .. } | EdgeError::UnknownMajor(_) => Code::InvalidArgument,
            EdgeError::EndpointNotAllowed(_) => Code::PermissionDenied,
            EdgeError::TlsIdentityMismatch(_) => Code::Unauthenticated,
            EdgeError::ResourceExhausted { .. } => Code::ResourceExhausted,
            EdgeError::Precondition { .. } | EdgeError::WalCorrupt(_) => Code::FailedPrecondition,
            EdgeError::Deadline(_) => Code::DeadlineExceeded,
            EdgeError::Io { .. } | EdgeError::Remote { .. } | EdgeError::ActorUnavailable(_) => {
                Code::Unavailable
            }
            EdgeError::UnknownOutcome(_) => Code::Aborted,
        };
        Status::new(code, error.to_string())
    }
}
