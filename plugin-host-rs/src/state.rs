//! Bounded binding registry, lifecycle, admission, fencing, idempotency and circuits.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::Arc;
use std::sync::Mutex as StdMutex;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tokio::sync::{Mutex, OwnedSemaphorePermit, RwLock, Semaphore};

use crate::admission::{ValidatedBinding, sha256_bytes, validate_binding};
use crate::config::HostConfig;
use crate::contract::control_adapter::{StatisticsExecutionReply, StatisticsExecutionRequest};
use crate::contract::host::{
    BindingEnvelope, BindingObservation, ExecuteReply, ExecuteRequest, StatisticsDefinitionBinding,
};
use crate::error::{HostError, HostResult, ReasonCode};
use crate::metrics::HostMetrics;
use crate::runtime::service::ServiceRuntime;
use crate::runtime::wasm::WasmRuntime;
use crate::runtime::{InterruptReason, InvocationControl, RuntimeInstance};
use crate::statistics::{InputBundleValidationContext, finalize_artifact, validate_input_bundle};

const OBSERVATION_SCHEMA: &str = "plugin-host-binding-observation/v1";

type BindingKey = (String, u64);

/// Observed Host runtime state, distinct from Manager canonical lifecycle facts.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ObservedPhase {
    /// Artifact/runtime is validated but receives no work.
    Staged,
    /// Runtime accepts isolated shadow calls only through the explicit shadow path.
    Shadow,
    /// Runtime is the one active generation for new calls.
    Active,
    /// Runtime accepts no new calls and waits boundedly for in-flight work.
    Draining,
    /// Runtime is stopped/disabled.
    Stopped,
    /// Runtime is permanently fenced by revocation.
    Revoked,
    /// Runtime is isolated after bounded failure budget exhaustion.
    Quarantined,
}

impl ObservedPhase {
    fn as_str(self) -> &'static str {
        match self {
            Self::Staged => "staged",
            Self::Shadow => "shadow",
            Self::Active => "active",
            Self::Draining => "draining",
            Self::Stopped => "stopped",
            Self::Revoked => "revoked",
            Self::Quarantined => "quarantined",
        }
    }
}

#[derive(Clone)]
struct CachedReply {
    request_digest: String,
    reply: ExecuteReply,
}

#[derive(Clone)]
struct CachedStatisticsReply {
    request_digest: String,
    reply: StatisticsExecutionReply,
}

struct StatisticsInFlight {
    request_digest: String,
    completed: AtomicBool,
    notify: tokio::sync::Notify,
}

impl StatisticsInFlight {
    async fn wait_completed(&self) {
        let notified = self.notify.notified();
        tokio::pin!(notified);
        loop {
            notified.as_mut().enable();
            if self.completed.load(Ordering::Acquire) {
                return;
            }
            notified.as_mut().await;
            notified.set(self.notify.notified());
        }
    }
}

struct CircuitState {
    consecutive_failures: u32,
    open_until: Option<Instant>,
}

struct RestartBudget {
    events: VecDeque<Instant>,
    retry_after: Option<Instant>,
    quarantine_until: Option<Instant>,
}

impl RestartBudget {
    fn new() -> Self {
        Self {
            events: VecDeque::new(),
            retry_after: None,
            quarantine_until: None,
        }
    }
}

enum RestartDecision {
    Backoff(Duration),
    Quarantine(Instant),
}

impl CircuitState {
    fn new() -> Self {
        Self {
            consecutive_failures: 0,
            open_until: None,
        }
    }
}

struct BindingRuntime {
    validated: Arc<ValidatedBinding>,
    control: RwLock<BindingControl>,
    runtime: RuntimeInstance,
    phase: RwLock<ObservedPhase>,
    execution_permits: Arc<Semaphore>,
    queued: AtomicU32,
    in_flight: AtomicU32,
    circuit: Mutex<CircuitState>,
    invocations: StdMutex<HashMap<String, Arc<InvocationControl>>>,
    dedupe: Mutex<BTreeMap<String, CachedReply>>,
    dedupe_order: Mutex<VecDeque<String>>,
    statistics_dedupe: Mutex<BTreeMap<String, CachedStatisticsReply>>,
    statistics_dedupe_order: Mutex<VecDeque<String>>,
    statistics_in_flight: StdMutex<HashMap<String, Arc<StatisticsInFlight>>>,
}

struct BindingControl {
    envelope_digest: String,
    revocation_checked_at_unix_ms: i64,
    expires_at_unix_ms: i64,
    trace_id: String,
}

impl BindingRuntime {
    async fn phase(&self) -> ObservedPhase {
        *self.phase.read().await
    }
}

/// Entire in-memory execution observation state. It is intentionally not durable.
pub struct HostState {
    config: Arc<HostConfig>,
    bindings: RwLock<BTreeMap<BindingKey, Arc<BindingRuntime>>>,
    active: RwLock<HashMap<String, u64>>,
    definitions: RwLock<HashMap<String, (BindingKey, String, String)>>,
    lifecycle: Mutex<()>,
    restart_budgets: Mutex<HashMap<BindingKey, RestartBudget>>,
    global_admission: Arc<Semaphore>,
    shutting_down: AtomicBool,
    control_ready: AtomicBool,
    /// Low-cardinality runtime counters.
    pub metrics: Arc<HostMetrics>,
}

impl HostState {
    /// Create a deny-all Host with no active binding.
    #[must_use]
    pub fn new(config: HostConfig) -> Arc<Self> {
        let global_depth = config.limits.global_queue_depth;
        Arc::new(Self {
            config: Arc::new(config),
            bindings: RwLock::new(BTreeMap::new()),
            active: RwLock::new(HashMap::new()),
            definitions: RwLock::new(HashMap::new()),
            lifecycle: Mutex::new(()),
            restart_budgets: Mutex::new(HashMap::new()),
            global_admission: Arc::new(Semaphore::new(global_depth)),
            shutting_down: AtomicBool::new(false),
            control_ready: AtomicBool::new(false),
            metrics: Arc::new(HostMetrics::new()),
        })
    }

    /// Return immutable Host configuration.
    #[must_use]
    pub fn config(&self) -> &HostConfig {
        &self.config
    }

    /// Validate, instantiate and observe one exact binding generation.
    pub async fn apply_binding(&self, binding: BindingEnvelope) -> HostResult<BindingObservation> {
        let _lifecycle = self.lifecycle.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return Err(HostError::new(
                ReasonCode::Unavailable,
                "Host is shutting down",
            ));
        }
        let desired_phase = parse_desired_state(&binding.desired_state)?;
        if !matches!(
            desired_phase,
            ObservedPhase::Staged | ObservedPhase::Shadow | ObservedPhase::Active
        ) {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "ApplyBinding accepts only staged, shadow or active; use DrainBinding/RevokeBinding for terminal transitions",
            ));
        }
        let key = (binding.plugin_id.clone(), binding.binding_generation);

        if let Some(existing) = self.bindings.read().await.get(&key).cloned() {
            let updated = validate_binding(&self.config, binding)?;
            if binding_subject_digest(&existing.validated.envelope)
                != binding_subject_digest(&updated.envelope)
            {
                return Err(HostError::new(
                    ReasonCode::IdempotencyConflict,
                    "same binding generation changed immutable admission subject",
                ));
            }
            if desired_phase == ObservedPhase::Active {
                self.ensure_activation_eligible(&key, &existing, false)
                    .await?;
            }
            {
                let mut control = existing.control.write().await;
                if updated.envelope.revocation_checked_at_unix_ms
                    < control.revocation_checked_at_unix_ms
                {
                    return Err(HostError::new(
                        ReasonCode::Fenced,
                        "revocation freshness observation regressed",
                    ));
                }
                control.envelope_digest = updated.envelope.envelope_digest;
                control.revocation_checked_at_unix_ms =
                    updated.envelope.revocation_checked_at_unix_ms;
                control.expires_at_unix_ms = updated.envelope.expires_at_unix_ms;
                control.trace_id = updated.envelope.trace_id;
            }
            if desired_phase == ObservedPhase::Active {
                self.activate_pointer(&key, existing.clone(), false).await?;
            } else {
                transition_phase(existing.clone(), desired_phase).await?;
            }
            return self.observation(&key, ReasonCode::Ok).await;
        }

        if self.bindings.read().await.len() >= self.config.limits.max_bindings {
            return Err(HostError::new(
                ReasonCode::ResourceExhausted,
                "Host binding registry is full",
            ));
        }
        if let Some(highest) = self
            .bindings
            .read()
            .await
            .keys()
            .filter(|(plugin_id, _)| plugin_id == &key.0)
            .map(|(_, generation)| *generation)
            .max()
            && key.1 < highest
        {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "binding generation regressed",
            ));
        }

        self.check_restart_budget(&key).await?;
        let validated = Arc::new(validate_binding(&self.config, binding)?);
        validate_definitions(&validated.envelope.statistics_definitions)?;
        let runtime = match instantiate_runtime(&self.config, &validated).await {
            Ok(runtime) => runtime,
            Err(error) => {
                let decision = self.record_restart_event(&key).await;
                self.metrics.record_activation_failure(error.reason);
                return Err(match decision {
                    RestartDecision::Backoff(duration) => HostError::new(
                        error.reason,
                        format!(
                            "{}; retry backoff {} ms",
                            error.message,
                            duration.as_millis()
                        ),
                    ),
                    RestartDecision::Quarantine(_) => HostError::new(
                        ReasonCode::CircuitOpen,
                        "binding generation exceeded restart budget and is quarantined",
                    ),
                });
            }
        };
        self.restart_budgets.lock().await.remove(&key);
        let observed = Arc::new(BindingRuntime {
            control: RwLock::new(BindingControl {
                envelope_digest: validated.envelope.envelope_digest.clone(),
                revocation_checked_at_unix_ms: validated.envelope.revocation_checked_at_unix_ms,
                expires_at_unix_ms: validated.envelope.expires_at_unix_ms,
                trace_id: validated.envelope.trace_id.clone(),
            }),
            validated,
            runtime,
            phase: RwLock::new(if desired_phase == ObservedPhase::Active {
                ObservedPhase::Staged
            } else {
                desired_phase
            }),
            execution_permits: Arc::new(Semaphore::new(self.config.limits.per_binding_in_flight)),
            queued: AtomicU32::new(0),
            in_flight: AtomicU32::new(0),
            circuit: Mutex::new(CircuitState::new()),
            invocations: StdMutex::new(HashMap::new()),
            dedupe: Mutex::new(BTreeMap::new()),
            dedupe_order: Mutex::new(VecDeque::new()),
            statistics_dedupe: Mutex::new(BTreeMap::new()),
            statistics_dedupe_order: Mutex::new(VecDeque::new()),
            statistics_in_flight: StdMutex::new(HashMap::new()),
        });
        self.bindings
            .write()
            .await
            .insert(key.clone(), observed.clone());
        self.metrics
            .binding_added(observed.validated.envelope.runtime_profile.as_str());
        if desired_phase == ObservedPhase::Active {
            self.activate_pointer(&key, observed, false).await?;
        }
        self.observation(&key, ReasonCode::Ok).await
    }

    /// Apply a separately authorized rollback to an already observed lower generation.
    ///
    /// Ordinary ApplyBinding is generation-monotonic. A rollback therefore has an
    /// explicit request shape, requires the exact current generation as a CAS fence,
    /// and can only reactivate a previously active generation left in draining state.
    #[allow(clippy::too_many_arguments)]
    pub async fn rollback_binding(
        &self,
        operation_id: &str,
        from_generation: u64,
        target_binding: BindingEnvelope,
        authorization_digest: &str,
        trace_id: &str,
        actor_ref: &str,
        reason_code: &str,
    ) -> HostResult<BindingObservation> {
        let _lifecycle = self.lifecycle.lock().await;
        if self.shutting_down.load(Ordering::Acquire) {
            return Err(HostError::new(
                ReasonCode::Unavailable,
                "Host is shutting down",
            ));
        }
        validate_runtime_identity(operation_id, "operation_id")?;
        validate_runtime_identity(trace_id, "trace_id")?;
        validate_runtime_identity(actor_ref, "actor_ref")?;
        validate_runtime_identity(reason_code, "reason_code")?;
        crate::config::validate_digest(authorization_digest)?;
        if target_binding.operation_id != operation_id
            || target_binding.trace_id != trace_id
            || target_binding.actor_ref != actor_ref
        {
            return Err(HostError::new(
                ReasonCode::IdempotencyConflict,
                "rollback request and target envelope operation identity differ",
            ));
        }
        if parse_desired_state(&target_binding.desired_state)? != ObservedPhase::Active {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "rollback target must request active state",
            ));
        }
        let key = (
            target_binding.plugin_id.clone(),
            target_binding.binding_generation,
        );
        if key.1 >= from_generation {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "rollback target generation must be lower than current generation",
            ));
        }
        if self.active.read().await.get(&key.0).copied() != Some(from_generation) {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "rollback source generation is not the exact active generation",
            ));
        }
        let source = self
            .bindings
            .read()
            .await
            .get(&(key.0.clone(), from_generation))
            .cloned()
            .ok_or_else(|| {
                HostError::new(
                    ReasonCode::Fenced,
                    "rollback source generation is not observed",
                )
            })?;
        if source.phase().await != ObservedPhase::Active {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "rollback source generation is not active",
            ));
        }
        let existing = self.binding(&key).await?;
        if !source
            .validated
            .manifest
            .compatibility
            .rollback_compatible_revisions
            .iter()
            .any(|revision| revision == &existing.validated.envelope.plugin_revision)
        {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "rollback target revision is absent from the active manifest compatibility set",
            ));
        }
        let updated = validate_binding(&self.config, target_binding)?;
        if binding_subject_digest(&existing.validated.envelope)
            != binding_subject_digest(&updated.envelope)
        {
            return Err(HostError::new(
                ReasonCode::IdempotencyConflict,
                "rollback changed immutable target admission subject",
            ));
        }
        self.ensure_activation_eligible(&key, &existing, true)
            .await?;
        {
            let mut control = existing.control.write().await;
            if updated.envelope.revocation_checked_at_unix_ms
                < control.revocation_checked_at_unix_ms
            {
                return Err(HostError::new(
                    ReasonCode::Fenced,
                    "rollback revocation freshness observation regressed",
                ));
            }
            control.envelope_digest = updated.envelope.envelope_digest;
            control.revocation_checked_at_unix_ms = updated.envelope.revocation_checked_at_unix_ms;
            control.expires_at_unix_ms = updated.envelope.expires_at_unix_ms;
            control.trace_id = updated.envelope.trace_id;
        }
        self.activate_pointer(&key, existing, true).await?;
        self.observation(&key, ReasonCode::Ok).await
    }

    async fn release_definitions(
        &self,
        key: &BindingKey,
        definitions: &[StatisticsDefinitionBinding],
    ) {
        let mut index = self.definitions.write().await;
        for definition in definitions {
            if index
                .get(&definition.definition_id)
                .is_some_and(|(bound_key, revision, digest)| {
                    bound_key == key
                        && revision == &definition.definition_revision
                        && digest == &definition.definition_digest
                })
            {
                index.remove(&definition.definition_id);
            }
        }
    }

    async fn activate_pointer(
        &self,
        key: &BindingKey,
        binding: Arc<BindingRuntime>,
        allow_rollback: bool,
    ) -> HostResult<()> {
        self.ensure_activation_eligible(key, &binding, allow_rollback)
            .await?;
        let new_definitions = &binding.validated.envelope.statistics_definitions;
        let mut definitions = self.definitions.write().await;
        for definition in new_definitions {
            if let Some((existing_key, existing_revision, existing_digest)) =
                definitions.get(&definition.definition_id)
                && (existing_key.0 != key.0
                    || existing_revision != &definition.definition_revision
                    || existing_digest != &definition.definition_digest)
            {
                return Err(HostError::new(
                    ReasonCode::IdempotencyConflict,
                    "statistics definition identity is active for another plugin",
                ));
            }
        }
        let previous = self.active.write().await.insert(key.0.clone(), key.1);
        if let Some(previous_generation) = previous
            && previous_generation != key.1
            && let Some(previous_binding) = self
                .bindings
                .read()
                .await
                .get(&(key.0.clone(), previous_generation))
                .cloned()
        {
            for definition in &previous_binding.validated.envelope.statistics_definitions {
                if definitions.get(&definition.definition_id).is_some_and(
                    |(bound_key, revision, digest)| {
                        bound_key == &(key.0.clone(), previous_generation)
                            && revision == &definition.definition_revision
                            && digest == &definition.definition_digest
                    },
                ) {
                    definitions.remove(&definition.definition_id);
                }
            }
            let mut phase = previous_binding.phase.write().await;
            if *phase == ObservedPhase::Active {
                *phase = ObservedPhase::Draining;
            }
        }
        for definition in new_definitions {
            definitions.insert(
                definition.definition_id.clone(),
                (
                    key.clone(),
                    definition.definition_revision.clone(),
                    definition.definition_digest.clone(),
                ),
            );
        }
        *binding.phase.write().await = ObservedPhase::Active;
        Ok(())
    }

    async fn ensure_activation_eligible(
        &self,
        key: &BindingKey,
        binding: &BindingRuntime,
        allow_rollback: bool,
    ) -> HostResult<()> {
        let phase = binding.phase().await;
        let current_generation = self.active.read().await.get(&key.0).copied();
        let eligible = if allow_rollback {
            phase == ObservedPhase::Draining
        } else {
            matches!(
                phase,
                ObservedPhase::Staged | ObservedPhase::Shadow | ObservedPhase::Active
            ) || (phase == ObservedPhase::Draining
                && current_generation.is_some_and(|current| current < key.1))
        };
        if !eligible {
            return Err(HostError::new(
                ReasonCode::Fenced,
                if allow_rollback {
                    "rollback target is not a previously active draining generation"
                } else {
                    "binding phase is not eligible for ordinary activation"
                },
            ));
        }
        if !allow_rollback
            && let Some(current_generation) = current_generation
            && current_generation > key.1
        {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "ordinary activation cannot regress the active generation",
            ));
        }
        Ok(())
    }

    /// Execute a generic exact-binding invocation.
    pub async fn execute(&self, request: ExecuteRequest) -> HostResult<ExecuteReply> {
        self.execute_inner(request, true).await
    }

    async fn execute_inner(
        &self,
        request: ExecuteRequest,
        raw_digest: bool,
    ) -> HostResult<ExecuteReply> {
        let request_started = Instant::now();
        if request.schema_version != "plugin-host-execute/v1" {
            return Err(HostError::new(
                ReasonCode::UnknownVersion,
                "execute schema version rejected",
            ));
        }
        if self.shutting_down.load(Ordering::Acquire) {
            return Err(HostError::new(
                ReasonCode::Unavailable,
                "Host is shutting down",
            ));
        }
        if request.input.is_empty() || request.input.len() > self.config.limits.max_input_bytes {
            return Err(HostError::new(
                ReasonCode::ResourceExhausted,
                "invocation input exceeds profile",
            ));
        }
        if raw_digest && sha256_bytes(&request.input) != request.input_digest {
            return Err(HostError::new(
                ReasonCode::DigestMismatch,
                "invocation input digest mismatch",
            ));
        }
        if request.deadline_ms == 0
            || u64::from(request.deadline_ms) > self.config.limits.max_deadline_ms
        {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "invocation deadline exceeds profile",
            ));
        }
        validate_execute_request(&request)?;
        let key = (request.plugin_id.clone(), request.binding_generation);
        let binding = self
            .bindings
            .read()
            .await
            .get(&key)
            .cloned()
            .ok_or_else(|| HostError::new(ReasonCode::Unavailable, "exact binding not observed"))?;
        if binding.validated.envelope.binding_epoch != request.binding_epoch {
            return Err(HostError::new(ReasonCode::Fenced, "binding epoch mismatch"));
        }
        let now = unix_ms();
        let control = binding.control.read().await;
        if now > control.expires_at_unix_ms {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "binding envelope expired",
            ));
        }
        if now.saturating_sub(control.revocation_checked_at_unix_ms)
            > self.config.trust.revocation_max_age_ms
        {
            return Err(HostError::new(
                ReasonCode::TrustStale,
                "revocation observation is stale; new work is stopped",
            ));
        }
        drop(control);
        let phase = binding.phase().await;
        match request.execution_mode.as_str() {
            "active"
                if self.active.read().await.get(&request.plugin_id).copied()
                    == Some(request.binding_generation)
                    && phase == ObservedPhase::Active => {}
            "shadow" if phase == ObservedPhase::Shadow => {}
            "active" | "shadow" => {
                return Err(HostError::new(
                    ReasonCode::Fenced,
                    "binding generation is not eligible for the requested execution mode",
                ));
            }
            _ => {
                return Err(HostError::new(
                    ReasonCode::InvalidArgument,
                    "execution_mode must be active or shadow",
                ));
            }
        }
        if !binding
            .validated
            .envelope
            .granted_capabilities
            .iter()
            .any(|value| value == &request.capability_id)
        {
            return Err(HostError::new(
                ReasonCode::CapabilityDenied,
                "invocation capability is not granted",
            ));
        }

        let request_digest = sha256_bytes(&prost::Message::encode_to_vec(&request));
        if let Some(cached) = binding
            .dedupe
            .lock()
            .await
            .get(&request.invocation_id)
            .cloned()
        {
            if cached.request_digest != request_digest {
                return Err(HostError::new(
                    ReasonCode::IdempotencyConflict,
                    "same invocation id has a different input digest",
                ));
            }
            self.metrics.record_deduplicated();
            return Ok(cached.reply);
        }
        let control = InvocationControl::new();
        reserve_invocation(
            &binding.invocations,
            &request.invocation_id,
            control.clone(),
        )?;
        let _invocation_reservation = InvocationReservation {
            binding: binding.clone(),
            invocation_id: request.invocation_id.clone(),
        };
        self.check_circuit(&binding).await?;

        let global_permit = self
            .global_admission
            .clone()
            .try_acquire_owned()
            .map_err(|_| {
                HostError::new(
                    ReasonCode::ResourceExhausted,
                    "global Host admission queue is full",
                )
            })?;
        let queue_slot = increment_bounded(
            &binding.queued,
            binding.validated.manifest.resource_limits.queue_depth,
        )?;
        let total_deadline = Duration::from_millis(u64::from(request.deadline_ms));
        let remaining_before_queue = total_deadline
            .checked_sub(request_started.elapsed())
            .ok_or_else(|| {
                HostError::new(
                    ReasonCode::DeadlineExceeded,
                    "deadline exhausted before queue admission",
                )
            })?;
        let execution_permit = acquire_execution_permit(
            binding.execution_permits.clone(),
            self.config
                .limits
                .queue_wait_ms
                .min(u64::try_from(remaining_before_queue.as_millis()).unwrap_or(u64::MAX)),
        )
        .await;
        drop(queue_slot);
        let execution_permit = execution_permit?;
        let remaining = total_deadline
            .checked_sub(request_started.elapsed())
            .ok_or_else(|| {
                HostError::new(
                    ReasonCode::DeadlineExceeded,
                    "deadline exhausted in admission queue",
                )
            })?;
        let mut runtime_request = request.clone();
        runtime_request.deadline_ms =
            u32::try_from(remaining.as_millis().max(1)).unwrap_or(u32::MAX);
        let _in_flight_slot = InFlightSlot::new(binding.clone());
        let started = Instant::now();
        let result = binding.runtime.execute(&runtime_request, control).await;
        drop(execution_permit);
        drop(global_permit);

        match result {
            Ok(output) => {
                if output.len() > self.config.limits.max_output_bytes
                    || output.len() as u64 > binding.validated.manifest.resource_limits.output_bytes
                {
                    self.record_failure(&binding, ReasonCode::ResourceExhausted)
                        .await;
                    return Err(HostError::new(
                        ReasonCode::ResourceExhausted,
                        "invocation output exceeds exact binding",
                    ));
                }
                self.record_success(&binding).await;
                let reply = ExecuteReply {
                    schema_version: "plugin-host-execute-result/v1".to_owned(),
                    invocation_id: request.invocation_id.clone(),
                    plugin_id: request.plugin_id.clone(),
                    binding_generation: request.binding_generation,
                    binding_epoch: request.binding_epoch.clone(),
                    input_digest: request.input_digest.clone(),
                    output_digest: sha256_bytes(&output),
                    output,
                    result_fence: request.result_fence.clone(),
                    status: "succeeded".to_owned(),
                    reason_code: ReasonCode::Ok.as_str().to_owned(),
                    trace_id: request.trace_id.clone(),
                    execution_mode: request.execution_mode.clone(),
                };
                self.cache_reply(
                    &binding,
                    request.invocation_id,
                    request_digest,
                    reply.clone(),
                )
                .await;
                self.metrics.record_invocation(
                    binding.validated.envelope.runtime_profile.as_str(),
                    "succeeded",
                    ReasonCode::Ok,
                    started.elapsed(),
                );
                Ok(reply)
            }
            Err(error) => {
                self.record_failure(&binding, error.reason).await;
                self.metrics.record_invocation(
                    binding.validated.envelope.runtime_profile.as_str(),
                    "failed",
                    error.reason,
                    started.elapsed(),
                );
                Err(error)
            }
        }
    }

    async fn cache_reply(
        &self,
        binding: &BindingRuntime,
        invocation_id: String,
        request_digest: String,
        reply: ExecuteReply,
    ) {
        let max = self.config.limits.per_binding_queue_depth;
        let mut dedupe = binding.dedupe.lock().await;
        let mut order = binding.dedupe_order.lock().await;
        if dedupe
            .insert(
                invocation_id.clone(),
                CachedReply {
                    request_digest,
                    reply,
                },
            )
            .is_none()
        {
            order.push_back(invocation_id);
        }
        while order.len() > max {
            if let Some(oldest) = order.pop_front() {
                dedupe.remove(&oldest);
            }
        }
    }

    async fn check_circuit(&self, binding: &BindingRuntime) -> HostResult<()> {
        if binding.phase().await == ObservedPhase::Quarantined {
            return Err(HostError::new(
                ReasonCode::CircuitOpen,
                "binding generation is quarantined",
            ));
        }
        let mut circuit = binding.circuit.lock().await;
        if let Some(until) = circuit.open_until {
            if Instant::now() < until {
                return Err(HostError::new(
                    ReasonCode::CircuitOpen,
                    "binding circuit is open",
                ));
            }
            circuit.open_until = None;
            circuit.consecutive_failures = 0;
        }
        Ok(())
    }

    async fn record_success(&self, binding: &BindingRuntime) {
        let mut circuit = binding.circuit.lock().await;
        circuit.consecutive_failures = 0;
        circuit.open_until = None;
    }

    async fn record_failure(&self, binding: &BindingRuntime, reason: ReasonCode) {
        let threshold_reached = {
            let mut circuit = binding.circuit.lock().await;
            circuit.consecutive_failures = circuit.consecutive_failures.saturating_add(1);
            circuit.consecutive_failures >= self.config.limits.failure_threshold
                && circuit.open_until.is_none()
        };
        if threshold_reached {
            let key = (
                binding.validated.envelope.plugin_id.clone(),
                binding.validated.envelope.binding_generation,
            );
            match self.record_restart_event(&key).await {
                RestartDecision::Backoff(backoff) => {
                    binding.circuit.lock().await.open_until = Some(Instant::now() + backoff);
                    self.metrics.record_circuit_open();
                }
                RestartDecision::Quarantine(until) => {
                    let _lifecycle = self.lifecycle.lock().await;
                    binding.circuit.lock().await.open_until = Some(until);
                    if matches!(
                        binding.phase().await,
                        ObservedPhase::Active | ObservedPhase::Shadow | ObservedPhase::Staged
                    ) {
                        *binding.phase.write().await = ObservedPhase::Quarantined;
                        if self.active.read().await.get(&key.0).copied() == Some(key.1) {
                            self.active.write().await.remove(&key.0);
                        }
                        self.release_definitions(
                            &key,
                            &binding.validated.envelope.statistics_definitions,
                        )
                        .await;
                    }
                    self.metrics.record_circuit_open();
                }
            }
        }
        if matches!(reason, ReasonCode::Revoked | ReasonCode::Fenced) {
            *binding.phase.write().await = ObservedPhase::Revoked;
        }
    }

    async fn check_restart_budget(&self, key: &BindingKey) -> HostResult<()> {
        let now = Instant::now();
        let mut budgets = self.restart_budgets.lock().await;
        let Some(budget) = budgets.get_mut(key) else {
            return Ok(());
        };
        if let Some(until) = budget.quarantine_until {
            if now < until {
                return Err(HostError::new(
                    ReasonCode::CircuitOpen,
                    "binding generation is within restart quarantine",
                ));
            }
            budgets.remove(key);
            return Ok(());
        }
        if budget
            .retry_after
            .is_some_and(|retry_after| now < retry_after)
        {
            return Err(HostError::new(
                ReasonCode::CircuitOpen,
                "binding generation is within bounded restart backoff",
            ));
        }
        budget.retry_after = None;
        prune_restart_events(budget, now, self.config.limits.restart_window_ms);
        if budget.events.is_empty() {
            budgets.remove(key);
        }
        Ok(())
    }

    async fn record_restart_event(&self, key: &BindingKey) -> RestartDecision {
        let now = Instant::now();
        let mut budgets = self.restart_budgets.lock().await;
        let budget = budgets
            .entry(key.clone())
            .or_insert_with(RestartBudget::new);
        prune_restart_events(budget, now, self.config.limits.restart_window_ms);
        budget.events.push_back(now);
        if budget.events.len() > self.config.limits.max_restarts_in_window {
            let until = now + Duration::from_millis(self.config.limits.quarantine_ms);
            budget.retry_after = None;
            budget.quarantine_until = Some(until);
            return RestartDecision::Quarantine(until);
        }
        let exponent = u32::try_from(budget.events.len().saturating_sub(1))
            .unwrap_or(31)
            .min(16);
        let multiplier = 1u64.checked_shl(exponent).unwrap_or(u64::MAX);
        let backoff = Duration::from_millis(
            self.config
                .limits
                .circuit_open_ms
                .saturating_mul(multiplier)
                .min(60_000),
        );
        budget.retry_after = Some(now + backoff);
        RestartDecision::Backoff(backoff)
    }

    /// Cancel one in-flight invocation; no new durable identity is created.
    pub async fn cancel_invocation(
        &self,
        plugin_id: &str,
        generation: u64,
        invocation_id: &str,
        epoch_fault: bool,
    ) -> HostResult<bool> {
        let key = (plugin_id.to_owned(), generation);
        let binding = self
            .bindings
            .read()
            .await
            .get(&key)
            .cloned()
            .ok_or_else(|| HostError::new(ReasonCode::Unavailable, "binding not found"))?;
        let control = lock_invocations(&binding.invocations)
            .get(invocation_id)
            .cloned();
        if let Some(control) = control {
            control.interrupt(if epoch_fault {
                InterruptReason::Epoch
            } else {
                InterruptReason::Cancelled
            });
            binding.runtime.interrupt_epoch();
            self.metrics.record_cancellation();
            Ok(true)
        } else {
            Ok(false)
        }
    }

    /// Drain an exact binding and reject new work immediately.
    pub async fn drain_binding(
        &self,
        plugin_id: &str,
        generation: u64,
        deadline_ms: u32,
        trace_id: &str,
    ) -> HostResult<BindingObservation> {
        let key = (plugin_id.to_owned(), generation);
        let binding = self.binding(&key).await?;
        let expected_envelope_digest = binding.control.read().await.envelope_digest.clone();
        self.drain_binding_authorized(
            "internal-drain",
            plugin_id,
            generation,
            deadline_ms,
            trace_id,
            "plugin-host-internal",
            "INTERNAL_DRAIN",
            &expected_envelope_digest,
            &sha256_bytes(b"plugin-host-internal-drain"),
        )
        .await
    }

    /// Drain an exact binding through an authenticated Manager lifecycle action.
    #[allow(clippy::too_many_arguments)]
    pub async fn drain_binding_authorized(
        &self,
        operation_id: &str,
        plugin_id: &str,
        generation: u64,
        deadline_ms: u32,
        trace_id: &str,
        actor_ref: &str,
        reason_code: &str,
        expected_envelope_digest: &str,
        authorization_digest: &str,
    ) -> HostResult<BindingObservation> {
        let _lifecycle = self.lifecycle.lock().await;
        let key = (plugin_id.to_owned(), generation);
        let binding = self.binding(&key).await?;
        validate_lifecycle_action(
            operation_id,
            trace_id,
            actor_ref,
            reason_code,
            expected_envelope_digest,
            authorization_digest,
        )?;
        if binding.control.read().await.envelope_digest != expected_envelope_digest {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "drain expected envelope digest does not match observed binding",
            ));
        }
        {
            let mut phase = binding.phase.write().await;
            if *phase == ObservedPhase::Revoked {
                return Err(HostError::new(
                    ReasonCode::Revoked,
                    "revoked binding cannot drain as active",
                ));
            }
            *phase = ObservedPhase::Draining;
        }
        if self.active.read().await.get(plugin_id).copied() == Some(generation) {
            self.active.write().await.remove(plugin_id);
            self.release_definitions(&key, &binding.validated.envelope.statistics_definitions)
                .await;
        }
        let deadline = Duration::from_millis(
            u64::from(deadline_ms)
                .min(self.config.limits.drain_deadline_ms)
                .max(1),
        );
        let started = Instant::now();
        while binding.in_flight.load(Ordering::Acquire) > 0 && started.elapsed() < deadline {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        if binding.in_flight.load(Ordering::Acquire) > 0 {
            let controls: Vec<_> = lock_invocations(&binding.invocations)
                .values()
                .cloned()
                .collect();
            for control in controls {
                control.interrupt(InterruptReason::Cancelled);
            }
            binding.runtime.interrupt_epoch();
            return Err(HostError::new(
                ReasonCode::DeadlineExceeded,
                "binding drain deadline exceeded",
            ));
        }
        binding.runtime.drain(deadline_ms, trace_id).await?;
        *binding.phase.write().await = ObservedPhase::Stopped;
        self.observation(&key, ReasonCode::Ok).await
    }

    /// Revoke an exact generation, fence all late results, and disable the runtime.
    #[allow(clippy::too_many_arguments)]
    pub async fn revoke_binding(
        &self,
        operation_id: &str,
        plugin_id: &str,
        generation: u64,
        revocation_digest: &str,
        deadline_ms: u32,
        trace_id: &str,
        actor_ref: &str,
        reason_code: &str,
        expected_envelope_digest: &str,
        authorization_digest: &str,
    ) -> HostResult<BindingObservation> {
        let _lifecycle = self.lifecycle.lock().await;
        crate::config::validate_digest(revocation_digest)?;
        let key = (plugin_id.to_owned(), generation);
        let binding = self.binding(&key).await?;
        validate_lifecycle_action(
            operation_id,
            trace_id,
            actor_ref,
            reason_code,
            expected_envelope_digest,
            authorization_digest,
        )?;
        if binding.control.read().await.envelope_digest != expected_envelope_digest {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "revoke expected envelope digest does not match observed binding",
            ));
        }
        *binding.phase.write().await = ObservedPhase::Revoked;
        if self.active.read().await.get(plugin_id).copied() == Some(generation) {
            self.active.write().await.remove(plugin_id);
        }
        let controls: Vec<_> = lock_invocations(&binding.invocations)
            .values()
            .cloned()
            .collect();
        for control in controls {
            control.interrupt(InterruptReason::Cancelled);
        }
        binding.runtime.interrupt_epoch();
        let disable_result = binding
            .runtime
            .disable(revocation_digest, deadline_ms, trace_id)
            .await;
        self.release_definitions(&key, &binding.validated.envelope.statistics_definitions)
            .await;
        self.metrics.record_revocation();
        disable_result?;
        self.observation(&key, ReasonCode::Revoked).await
    }

    /// Look up and observe one exact binding.
    pub async fn get_binding(
        &self,
        plugin_id: &str,
        generation: u64,
    ) -> HostResult<BindingObservation> {
        self.observation(&(plugin_id.to_owned(), generation), ReasonCode::Ok)
            .await
    }

    /// Return a bounded page of observed bindings, sorted by plugin/generation.
    pub async fn list_bindings(
        &self,
        page_size: u32,
        page_token: &str,
    ) -> HostResult<(Vec<BindingObservation>, String)> {
        let size = usize::try_from(page_size.clamp(1, 200)).unwrap_or(200);
        let start = if page_token.is_empty() {
            0
        } else {
            page_token
                .parse::<usize>()
                .map_err(|_| HostError::new(ReasonCode::InvalidArgument, "page token malformed"))?
        };
        let keys: Vec<_> = self.bindings.read().await.keys().cloned().collect();
        if start > keys.len() {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "page token outside result",
            ));
        }
        let end = (start + size).min(keys.len());
        let mut results = Vec::with_capacity(end.saturating_sub(start));
        for key in &keys[start..end] {
            results.push(self.observation(key, ReasonCode::Ok).await?);
        }
        let next = if end < keys.len() {
            end.to_string()
        } else {
            String::new()
        };
        Ok((results, next))
    }

    /// Execute the existing Go statistics adapter through the active exact definition binding.
    pub async fn execute_statistics(
        &self,
        request: StatisticsExecutionRequest,
    ) -> HostResult<StatisticsExecutionReply> {
        if request.schema_version != "control-plugin-statistics-execution/v1" {
            return Err(HostError::new(
                ReasonCode::UnknownVersion,
                "statistics execution version rejected",
            ));
        }
        if request.input_bundle_json.len() > self.config.limits.max_input_bytes {
            return Err(HostError::new(
                ReasonCode::ResourceExhausted,
                "statistics input exceeds Host bound",
            ));
        }
        if request.claim_generation < 1
            || request.deadline_ms == 0
            || u64::from(request.deadline_ms) > self.config.limits.max_deadline_ms
            || request.expires_at_unix_ms < unix_ms()
        {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics lease/generation/deadline envelope is expired or malformed",
            ));
        }
        for (value, field) in [
            (&request.run_id, "run_id"),
            (&request.lease_id, "lease_id"),
            (&request.result_fence, "result_fence"),
            (&request.definition_id, "definition_id"),
            (&request.scope, "scope"),
            (&request.trace_id, "trace_id"),
        ] {
            validate_runtime_identity(value, field)?;
        }
        crate::config::validate_digest(&request.definition_digest)?;
        crate::config::validate_digest(&request.frozen_input_digest)?;
        let (key, bound_definition_revision, bound_definition_digest) = self
            .definitions
            .read()
            .await
            .get(&request.definition_id)
            .cloned()
            .ok_or_else(|| {
                HostError::new(
                    ReasonCode::Unavailable,
                    "statistics definition is not bound",
                )
            })?;
        if bound_definition_digest != request.definition_digest {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics definition digest differs from exact active binding",
            ));
        }
        if key.1 != request.binding_generation {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics binding generation mismatch",
            ));
        }
        let binding = self.binding(&key).await?;
        if binding.validated.envelope.kind != "pure-transform" {
            return Err(HostError::new(
                ReasonCode::CapabilityDenied,
                "Host statistics scope requires pure-transform",
            ));
        }
        if binding.validated.envelope.scope != request.scope {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics scope does not match exact binding",
            ));
        }
        let statistics_request_digest = sha256_bytes(&prost::Message::encode_to_vec(&request));
        let _statistics_reservation = loop {
            // A cached success is still subject to the current lease and active
            // binding. Serialize this read with lifecycle mutations so revoke,
            // drain, rollback or trust reconciliation cannot race the reply.
            let wait_for = {
                let _lifecycle = self.lifecycle.lock().await;
                self.ensure_statistics_execution_eligible(
                    &key,
                    &binding,
                    &request,
                    &bound_definition_revision,
                )
                .await?;
                if let Some(cached) = binding
                    .statistics_dedupe
                    .lock()
                    .await
                    .get(&request.run_id)
                    .cloned()
                {
                    if cached.request_digest != statistics_request_digest {
                        return Err(HostError::new(
                            ReasonCode::IdempotencyConflict,
                            "same statistics run id has a different request identity",
                        ));
                    }
                    self.metrics.record_deduplicated();
                    return Ok(cached.reply);
                }
                match reserve_statistics_run(&binding, &request.run_id, &statistics_request_digest)?
                {
                    StatisticsRunAdmission::Owner(reservation) => break reservation,
                    StatisticsRunAdmission::Wait(in_flight) => Some(in_flight),
                }
            };
            if let Some(in_flight) = wait_for {
                in_flight.wait_completed().await;
            }
        };
        let frozen = validate_input_bundle(
            &request.input_bundle_json,
            &InputBundleValidationContext {
                definition_id: &request.definition_id,
                definition_digest: &request.definition_digest,
                frozen_digest: &request.frozen_input_digest,
                run_id: &request.run_id,
                scope: &request.scope,
                binding: &binding.validated,
                deadline_ms: request.deadline_ms,
            },
        )?;
        if frozen.definition_revision != bound_definition_revision {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics definition revision differs from exact active binding",
            ));
        }
        if !frozen.external_source_capability_refs.is_empty() {
            return Err(HostError::new(
                ReasonCode::CapabilityDenied,
                "pure-transform statistics input cannot carry an external-source capability",
            ));
        }
        let execute_request = ExecuteRequest {
            schema_version: "plugin-host-execute/v1".to_owned(),
            invocation_id: request.run_id.clone(),
            plugin_id: key.0.clone(),
            binding_generation: key.1,
            binding_epoch: binding.validated.envelope.binding_epoch.clone(),
            capability_id: "plugin.statistics.execute".to_owned(),
            input_digest: request.frozen_input_digest.clone(),
            input: request.input_bundle_json.clone(),
            deadline_ms: request.deadline_ms,
            result_fence: request.result_fence.clone(),
            trace_id: request.trace_id.clone(),
            execution_mode: "active".to_owned(),
        };
        let raw_reply = self.execute_inner(execute_request, false).await?;
        // Runtime success is not a statistics success until the lease and
        // exact active binding are revalidated. Lifecycle serialization closes
        // the result-publication race with revoke/drain/rollback/reconcile.
        let _lifecycle = self.lifecycle.lock().await;
        if let Err(error) = self
            .ensure_statistics_execution_eligible(
                &key,
                &binding,
                &request,
                &bound_definition_revision,
            )
            .await
        {
            self.evict_execute_reply(&binding, &request.run_id).await;
            return Err(error);
        }
        let artifact = finalize_artifact(
            &raw_reply.output,
            &binding.validated,
            &request.run_id,
            &request.definition_id,
            &request.definition_digest,
            &request.trace_id,
            &self.config.host_id,
        )?;
        let artifact_digest = serde_json::from_slice::<serde_json::Value>(&artifact)
            .ok()
            .and_then(|value| {
                value
                    .get("artifact_digest")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_owned)
            })
            .ok_or_else(|| {
                HostError::new(ReasonCode::Internal, "final artifact digest unavailable")
            })?;
        let reply = StatisticsExecutionReply {
            schema_version: "control-plugin-statistics-result/v1".to_owned(),
            run_id: request.run_id.clone(),
            result_fence: request.result_fence,
            artifact_json: artifact,
            artifact_digest,
            status: "succeeded".to_owned(),
            reason_code: ReasonCode::Ok.as_str().to_owned(),
            trace_id: request.trace_id,
        };
        self.cache_statistics_reply(
            &binding,
            request.run_id,
            statistics_request_digest,
            reply.clone(),
        )
        .await;
        Ok(reply)
    }

    async fn ensure_statistics_execution_eligible(
        &self,
        key: &BindingKey,
        binding: &BindingRuntime,
        request: &StatisticsExecutionRequest,
        definition_revision: &str,
    ) -> HostResult<()> {
        let now = unix_ms();
        if self.shutting_down.load(Ordering::Acquire) || request.expires_at_unix_ms < now {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics result lease expired before publication",
            ));
        }
        if key.1 != request.binding_generation
            || binding.validated.envelope.binding_generation != request.binding_generation
            || binding.validated.envelope.scope != request.scope
        {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics result binding identity changed before publication",
            ));
        }
        let control = binding.control.read().await;
        if now > control.expires_at_unix_ms
            || now.saturating_sub(control.revocation_checked_at_unix_ms)
                > self.config.trust.revocation_max_age_ms
        {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics result trust lease is no longer current",
            ));
        }
        drop(control);
        if binding.phase().await != ObservedPhase::Active
            || self.active.read().await.get(&key.0).copied() != Some(key.1)
        {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics result binding is no longer active",
            ));
        }
        let expected = (
            key.clone(),
            definition_revision.to_owned(),
            request.definition_digest.clone(),
        );
        if self.definitions.read().await.get(&request.definition_id) != Some(&expected) {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "statistics definition binding changed before publication",
            ));
        }
        Ok(())
    }

    async fn evict_execute_reply(&self, binding: &BindingRuntime, invocation_id: &str) {
        binding.dedupe.lock().await.remove(invocation_id);
        binding
            .dedupe_order
            .lock()
            .await
            .retain(|value| value != invocation_id);
    }

    async fn cache_statistics_reply(
        &self,
        binding: &BindingRuntime,
        run_id: String,
        request_digest: String,
        reply: StatisticsExecutionReply,
    ) {
        let max = self.config.limits.per_binding_queue_depth;
        let mut dedupe = binding.statistics_dedupe.lock().await;
        let mut order = binding.statistics_dedupe_order.lock().await;
        if dedupe
            .insert(
                run_id.clone(),
                CachedStatisticsReply {
                    request_digest,
                    reply,
                },
            )
            .is_none()
        {
            order.push_back(run_id);
        }
        while order.len() > max {
            if let Some(oldest) = order.pop_front() {
                dedupe.remove(&oldest);
            }
        }
    }

    /// Fail closed every observed generation whose binding or revocation freshness expired.
    ///
    /// The Manager remains the revocation fact owner and must refresh/push the exact
    /// envelope. This local reconciliation loop only enforces the bounded freshness
    /// lease; it never invents a replacement generation or fetches trust material.
    pub async fn reconcile_trust(&self) -> usize {
        let now = unix_ms();
        let mut fenced = Vec::new();
        {
            let _lifecycle = self.lifecycle.lock().await;
            let bindings: Vec<_> = self
                .bindings
                .read()
                .await
                .iter()
                .map(|(key, binding)| (key.clone(), binding.clone()))
                .collect();
            for (key, binding) in bindings {
                let control = binding.control.read().await;
                let expired = now > control.expires_at_unix_ms
                    || now.saturating_sub(control.revocation_checked_at_unix_ms)
                        > self.config.trust.revocation_max_age_ms;
                drop(control);
                if !expired
                    || !matches!(
                        binding.phase().await,
                        ObservedPhase::Staged | ObservedPhase::Shadow | ObservedPhase::Active
                    )
                {
                    continue;
                }
                *binding.phase.write().await = ObservedPhase::Stopped;
                if self.active.read().await.get(&key.0).copied() == Some(key.1) {
                    self.active.write().await.remove(&key.0);
                }
                self.release_definitions(&key, &binding.validated.envelope.statistics_definitions)
                    .await;
                let controls: Vec<_> = lock_invocations(&binding.invocations)
                    .values()
                    .cloned()
                    .collect();
                for control in controls {
                    control.interrupt(InterruptReason::Cancelled);
                }
                binding.runtime.interrupt_epoch();
                fenced.push(binding);
            }
        }
        let fenced_count = fenced.len();
        let mut drains = tokio::task::JoinSet::new();
        for binding in fenced {
            let deadline = u32::try_from(self.config.limits.drain_deadline_ms).unwrap_or(u32::MAX);
            drains.spawn(async move {
                let _ = binding
                    .runtime
                    .drain(deadline, "trust-freshness-expired")
                    .await;
            });
        }
        while drains.join_next().await.is_some() {}
        fenced_count
    }

    /// Boundedly drain every active binding during Host shutdown.
    pub async fn shutdown(&self) {
        self.shutting_down.store(true, Ordering::Release);
        let keys: Vec<_> = self.bindings.read().await.keys().cloned().collect();
        for key in keys {
            let _ = self
                .drain_binding(
                    &key.0,
                    key.1,
                    u32::try_from(self.config.limits.shutdown_deadline_ms).unwrap_or(60_000),
                    "host-shutdown",
                )
                .await;
        }
    }

    /// Whether the Host scheduler is accepting control traffic.
    #[must_use]
    pub fn ready(&self) -> bool {
        self.control_ready.load(Ordering::Acquire) && !self.shutting_down.load(Ordering::Acquire)
    }

    /// Publish whether the Manager-facing listener is bound and serving.
    pub fn set_control_ready(&self, ready: bool) {
        self.control_ready.store(ready, Ordering::Release);
    }

    /// Current bounded observed-binding count for low-cardinality metrics.
    pub async fn binding_count(&self) -> usize {
        self.bindings.read().await.len()
    }

    /// Aggregate low-cardinality queue/in-flight gauges across observed bindings.
    pub async fn runtime_gauges(&self) -> (u64, u64) {
        let bindings: Vec<_> = self.bindings.read().await.values().cloned().collect();
        bindings
            .iter()
            .fold((0u64, 0u64), |(queued, in_flight), binding| {
                (
                    queued.saturating_add(u64::from(binding.queued.load(Ordering::Acquire))),
                    in_flight.saturating_add(u64::from(binding.in_flight.load(Ordering::Acquire))),
                )
            })
    }

    async fn binding(&self, key: &BindingKey) -> HostResult<Arc<BindingRuntime>> {
        self.bindings
            .read()
            .await
            .get(key)
            .cloned()
            .ok_or_else(|| HostError::new(ReasonCode::Unavailable, "binding not found"))
    }

    async fn observation(
        &self,
        key: &BindingKey,
        reason: ReasonCode,
    ) -> HostResult<BindingObservation> {
        let binding = self.binding(key).await?;
        let phase = binding.phase().await;
        let circuit = binding.circuit.lock().await;
        let control = binding.control.read().await;
        let open_ms = circuit
            .open_until
            .map(|instant| {
                unix_ms().saturating_add(
                    i64::try_from(
                        instant
                            .saturating_duration_since(Instant::now())
                            .as_millis(),
                    )
                    .unwrap_or(i64::MAX),
                )
            })
            .unwrap_or(0);
        let observed_at = unix_ms();
        let mut observation = BindingObservation {
            schema_version: OBSERVATION_SCHEMA.to_owned(),
            plugin_id: key.0.clone(),
            plugin_revision: binding.validated.envelope.plugin_revision.clone(),
            binding_generation: key.1,
            binding_epoch: binding.validated.envelope.binding_epoch.clone(),
            envelope_digest: control.envelope_digest.clone(),
            artifact_digest: binding.validated.envelope.artifact_digest.clone(),
            config_digest: binding.validated.envelope.config_digest.clone(),
            capability_digest: binding.validated.envelope.capability_digest.clone(),
            resource_profile_digest: binding.validated.envelope.resource_profile_digest.clone(),
            runtime_profile: binding.validated.envelope.runtime_profile.clone(),
            observed_state: phase.as_str().to_owned(),
            ready: phase == ObservedPhase::Active && circuit.open_until.is_none(),
            in_flight: binding.in_flight.load(Ordering::Acquire),
            queued: binding.queued.load(Ordering::Acquire),
            consecutive_failures: circuit.consecutive_failures,
            circuit_open_until_unix_ms: open_ms,
            observed_at_unix_ms: observed_at,
            observation_digest: String::new(),
            status: if matches!(phase, ObservedPhase::Revoked) {
                "revoked"
            } else {
                "observed"
            }
            .to_owned(),
            reason_code: reason.as_str().to_owned(),
            trace_id: control.trace_id.clone(),
        };
        observation.observation_digest = observation_digest(&observation);
        Ok(observation)
    }
}

async fn instantiate_runtime(
    config: &HostConfig,
    validated: &ValidatedBinding,
) -> HostResult<RuntimeInstance> {
    match validated.envelope.runtime_profile.as_str() {
        "wasm-component/v1" => {
            let bytes = validated.component_bytes.clone().ok_or_else(|| {
                HostError::new(
                    ReasonCode::InvalidArgument,
                    "validated Wasm component is absent",
                )
            })?;
            let limits = config.limits.clone();
            let runtime =
                tokio::task::spawn_blocking(move || WasmRuntime::compile(&bytes, &limits))
                    .await
                    .map_err(|error| {
                        HostError::new(
                            ReasonCode::Internal,
                            format!("component compiler join: {error}"),
                        )
                    })??;
            Ok(RuntimeInstance::Wasm(Arc::new(runtime)))
        }
        "grpc-service/v1" => {
            let endpoint = validated.service_endpoint.as_ref().ok_or_else(|| {
                HostError::new(
                    ReasonCode::InvalidArgument,
                    "validated service endpoint is absent",
                )
            })?;
            let runtime = ServiceRuntime::connect(endpoint, &validated.envelope).await?;
            runtime.health(&validated.envelope.trace_id).await?;
            Ok(RuntimeInstance::Service(Arc::new(runtime)))
        }
        _ => Err(HostError::new(
            ReasonCode::UnknownRuntimeProfile,
            "runtime registry is closed",
        )),
    }
}

async fn transition_phase(binding: Arc<BindingRuntime>, desired: ObservedPhase) -> HostResult<()> {
    let mut phase = binding.phase.write().await;
    let allowed = matches!(
        (*phase, desired),
        (
            ObservedPhase::Staged,
            ObservedPhase::Staged
                | ObservedPhase::Shadow
                | ObservedPhase::Active
                | ObservedPhase::Stopped
                | ObservedPhase::Revoked
        ) | (
            ObservedPhase::Shadow,
            ObservedPhase::Shadow
                | ObservedPhase::Active
                | ObservedPhase::Draining
                | ObservedPhase::Stopped
                | ObservedPhase::Revoked
        ) | (
            ObservedPhase::Active,
            ObservedPhase::Active | ObservedPhase::Draining | ObservedPhase::Revoked
        ) | (
            ObservedPhase::Draining,
            ObservedPhase::Draining | ObservedPhase::Stopped | ObservedPhase::Revoked
        ) | (
            ObservedPhase::Stopped,
            ObservedPhase::Stopped | ObservedPhase::Revoked
        ) | (ObservedPhase::Revoked, ObservedPhase::Revoked)
            | (
                ObservedPhase::Quarantined,
                ObservedPhase::Quarantined | ObservedPhase::Revoked
            )
    );
    if !allowed {
        return Err(HostError::new(
            ReasonCode::Fenced,
            "illegal observed lifecycle transition",
        ));
    }
    *phase = desired;
    Ok(())
}

fn parse_desired_state(value: &str) -> HostResult<ObservedPhase> {
    match value {
        "staged" => Ok(ObservedPhase::Staged),
        "shadow" => Ok(ObservedPhase::Shadow),
        "active" => Ok(ObservedPhase::Active),
        "draining" => Ok(ObservedPhase::Draining),
        "disabled" => Ok(ObservedPhase::Stopped),
        "revoked" => Ok(ObservedPhase::Revoked),
        _ => Err(HostError::new(
            ReasonCode::InvalidArgument,
            "desired binding state is unknown",
        )),
    }
}

fn validate_definitions(definitions: &[StatisticsDefinitionBinding]) -> HostResult<()> {
    if definitions.len() > 32 {
        return Err(HostError::new(
            ReasonCode::ResourceExhausted,
            "statistics definitions exceed 32",
        ));
    }
    let mut seen = std::collections::BTreeSet::new();
    for definition in definitions {
        validate_runtime_identity(&definition.definition_id, "statistics definition")?;
        validate_runtime_identity(
            &definition.definition_revision,
            "statistics definition revision",
        )?;
        crate::config::validate_digest(&definition.definition_digest)?;
        if !seen.insert((
            &definition.definition_id,
            &definition.definition_revision,
            &definition.definition_digest,
        )) {
            return Err(HostError::new(
                ReasonCode::InvalidArgument,
                "statistics definition identity is duplicated",
            ));
        }
    }
    Ok(())
}

fn validate_execute_request(request: &ExecuteRequest) -> HostResult<()> {
    for (value, field) in [
        (&request.invocation_id, "invocation_id"),
        (&request.plugin_id, "plugin_id"),
        (&request.binding_epoch, "binding_epoch"),
        (&request.capability_id, "capability_id"),
        (&request.result_fence, "result_fence"),
        (&request.trace_id, "trace_id"),
    ] {
        validate_runtime_identity(value, field)?;
    }
    crate::config::validate_digest(&request.input_digest)?;
    if request.binding_generation < 1 {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "binding generation must be positive",
        ));
    }
    Ok(())
}

fn validate_runtime_identity(value: &str, field: &str) -> HostResult<()> {
    if value.is_empty()
        || value.len() > 256
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-/".contains(&byte))
    {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            format!("{field} is malformed"),
        ));
    }
    Ok(())
}

fn validate_lifecycle_action(
    operation_id: &str,
    trace_id: &str,
    actor_ref: &str,
    reason_code: &str,
    expected_envelope_digest: &str,
    authorization_digest: &str,
) -> HostResult<()> {
    for (value, field) in [
        (operation_id, "operation_id"),
        (trace_id, "trace_id"),
        (actor_ref, "actor_ref"),
        (reason_code, "reason_code"),
    ] {
        validate_runtime_identity(value, field)?;
    }
    crate::config::validate_digest(expected_envelope_digest)?;
    crate::config::validate_digest(authorization_digest)?;
    Ok(())
}

fn binding_subject_digest(binding: &BindingEnvelope) -> String {
    let mut canonical = binding.clone();
    canonical.operation_id.clear();
    canonical.desired_state.clear();
    canonical.trace_id.clear();
    canonical.revocation_snapshot_digest.clear();
    canonical.revocation_checked_at_unix_ms = 0;
    canonical.issued_at_unix_ms = 0;
    canonical.expires_at_unix_ms = 0;
    canonical.envelope_digest.clear();
    sha256_bytes(&prost::Message::encode_to_vec(&canonical))
}

fn prune_restart_events(budget: &mut RestartBudget, now: Instant, window_ms: u64) {
    let window = Duration::from_millis(window_ms);
    while budget
        .events
        .front()
        .is_some_and(|event| now.saturating_duration_since(*event) > window)
    {
        budget.events.pop_front();
    }
}

async fn acquire_execution_permit(
    semaphore: Arc<Semaphore>,
    wait_ms: u64,
) -> HostResult<OwnedSemaphorePermit> {
    tokio::time::timeout(Duration::from_millis(wait_ms), semaphore.acquire_owned())
        .await
        .map_err(|_| {
            HostError::new(
                ReasonCode::ResourceExhausted,
                "binding execution queue wait exceeded",
            )
        })?
        .map_err(|_| {
            HostError::new(
                ReasonCode::Unavailable,
                "binding execution semaphore closed",
            )
        })
}

struct QueueSlot<'a> {
    counter: &'a AtomicU32,
}

impl Drop for QueueSlot<'_> {
    fn drop(&mut self) {
        self.counter.fetch_sub(1, Ordering::AcqRel);
    }
}

fn increment_bounded(counter: &AtomicU32, limit: u32) -> HostResult<QueueSlot<'_>> {
    let result = counter.fetch_update(Ordering::AcqRel, Ordering::Acquire, |current| {
        (current < limit).then_some(current + 1)
    });
    result
        .map(|_| QueueSlot { counter })
        .map_err(|_| HostError::new(ReasonCode::ResourceExhausted, "per-binding queue is full"))
}

type InvocationMap = HashMap<String, Arc<InvocationControl>>;

fn lock_invocations(
    invocations: &StdMutex<InvocationMap>,
) -> std::sync::MutexGuard<'_, InvocationMap> {
    invocations
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn reserve_invocation(
    invocations: &StdMutex<InvocationMap>,
    invocation_id: &str,
    control: Arc<InvocationControl>,
) -> HostResult<()> {
    // Generic execute deliberately rejects an in-flight duplicate. One owner
    // executes, concurrent duplicates receive IDEMPOTENCY_CONFLICT, and only
    // a later request may replay the completed bounded success cache. Durable
    // statistics runs use their stronger owner/waiter policy below.
    match lock_invocations(invocations).entry(invocation_id.to_owned()) {
        std::collections::hash_map::Entry::Occupied(_) => Err(HostError::new(
            ReasonCode::IdempotencyConflict,
            "same invocation is already in flight",
        )),
        std::collections::hash_map::Entry::Vacant(entry) => {
            entry.insert(control);
            Ok(())
        }
    }
}

struct InvocationReservation {
    binding: Arc<BindingRuntime>,
    invocation_id: String,
}

enum StatisticsRunAdmission {
    Owner(StatisticsRunReservation),
    Wait(Arc<StatisticsInFlight>),
}

struct StatisticsRunReservation {
    binding: Arc<BindingRuntime>,
    run_id: String,
    in_flight: Arc<StatisticsInFlight>,
}

struct InFlightSlot {
    binding: Arc<BindingRuntime>,
}

impl InFlightSlot {
    fn new(binding: Arc<BindingRuntime>) -> Self {
        binding.in_flight.fetch_add(1, Ordering::AcqRel);
        Self { binding }
    }
}

impl Drop for InFlightSlot {
    fn drop(&mut self) {
        self.binding.in_flight.fetch_sub(1, Ordering::AcqRel);
    }
}

impl Drop for InvocationReservation {
    fn drop(&mut self) {
        lock_invocations(&self.binding.invocations).remove(&self.invocation_id);
    }
}

fn reserve_statistics_run(
    binding: &Arc<BindingRuntime>,
    run_id: &str,
    request_digest: &str,
) -> HostResult<StatisticsRunAdmission> {
    let mut in_flight = binding
        .statistics_in_flight
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    match in_flight.entry(run_id.to_owned()) {
        std::collections::hash_map::Entry::Occupied(entry) => {
            if entry.get().request_digest != request_digest {
                return Err(HostError::new(
                    ReasonCode::IdempotencyConflict,
                    "same statistics run id is in flight with a different request identity",
                ));
            }
            Ok(StatisticsRunAdmission::Wait(entry.get().clone()))
        }
        std::collections::hash_map::Entry::Vacant(entry) => {
            let state = Arc::new(StatisticsInFlight {
                request_digest: request_digest.to_owned(),
                completed: AtomicBool::new(false),
                notify: tokio::sync::Notify::new(),
            });
            entry.insert(state.clone());
            Ok(StatisticsRunAdmission::Owner(StatisticsRunReservation {
                binding: binding.clone(),
                run_id: run_id.to_owned(),
                in_flight: state,
            }))
        }
    }
}

impl Drop for StatisticsRunReservation {
    fn drop(&mut self) {
        self.in_flight.completed.store(true, Ordering::Release);
        let mut in_flight = self
            .binding
            .statistics_in_flight
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if in_flight
            .get(&self.run_id)
            .is_some_and(|value| Arc::ptr_eq(value, &self.in_flight))
        {
            in_flight.remove(&self.run_id);
        }
        drop(in_flight);
        self.in_flight.notify.notify_waiters();
    }
}

fn observation_digest(observation: &BindingObservation) -> String {
    let mut canonical = observation.clone();
    canonical.observation_digest.clear();
    crate::admission::sha256_bytes(&prost::Message::encode_to_vec(&canonical))
}

fn unix_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|duration| i64::try_from(duration.as_millis()).ok())
        .unwrap_or(1)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lifecycle_is_monotonic() {
        assert!(parse_desired_state("active").is_ok());
        assert!(parse_desired_state("registered").is_err());
    }

    #[test]
    fn queue_counter_is_bounded() {
        let counter = AtomicU32::new(0);
        let first = increment_bounded(&counter, 1);
        assert!(first.is_ok());
        assert!(increment_bounded(&counter, 1).is_err());
        drop(first);
        assert!(increment_bounded(&counter, 1).is_ok());
    }

    #[test]
    fn invocation_identity_reservation_is_atomic() {
        let invocations = Arc::new(StdMutex::new(HashMap::new()));
        let barrier = Arc::new(std::sync::Barrier::new(32));
        let successes = Arc::new(AtomicU32::new(0));
        let conflicts = Arc::new(AtomicU32::new(0));
        let unexpected = Arc::new(AtomicU32::new(0));
        let threads: Vec<_> = (0..32)
            .map(|_| {
                let invocations = invocations.clone();
                let barrier = barrier.clone();
                let successes = successes.clone();
                let conflicts = conflicts.clone();
                let unexpected = unexpected.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    match reserve_invocation(
                        &invocations,
                        "same-invocation",
                        InvocationControl::new(),
                    ) {
                        Ok(()) => {
                            successes.fetch_add(1, Ordering::AcqRel);
                        }
                        Err(error) if error.reason == ReasonCode::IdempotencyConflict => {
                            conflicts.fetch_add(1, Ordering::AcqRel);
                        }
                        Err(_) => {
                            unexpected.fetch_add(1, Ordering::AcqRel);
                        }
                    }
                })
            })
            .collect();
        for thread in threads {
            assert!(thread.join().is_ok(), "reservation worker panicked");
        }
        assert_eq!(1, successes.load(Ordering::Acquire));
        assert_eq!(31, conflicts.load(Ordering::Acquire));
        assert_eq!(0, unexpected.load(Ordering::Acquire));
        assert_eq!(1, lock_invocations(&invocations).len());
    }
}
