//! Single-route central-inference client and commit-before-canonical-ACK sink.

use std::{collections::BTreeMap, time::Duration};

use prost::Message as _;
use tonic::Request;

use crate::{
    EdgeError, EdgeResult,
    config::{
        ClientTlsConfig, ControlSinkConfig, DeploymentTier, EndpointPolicyConfig, RuntimeLimits,
    },
    contract::edge::{
        BindingReadback, CanonicalAckBatch, CanonicalCommitStatus, DataQuality, InferenceDecision,
        InferenceExecutionStatus, InferenceInputBatch, InferenceRecord, InferenceResultBatch,
        InferenceResultRecord, InferenceRoute, PublishAck, PublishStatus, ResumeRouteRequest,
        RuleObservationBatch, TargetStatusBatch, TlsClientIdentity,
        control_sink_client::ControlSinkClient,
    },
    contract::inference::central_inference_client::CentralInferenceClient,
    digest,
    endpoint::connect_mtls,
    window::{FEATURE_DTYPE, FEATURE_WIDTH, canonical_input_record_digest},
};

/// Per-shard route state; only Active admits a new canonical window.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RouteState {
    /// No committed route.
    Withdrawn,
    /// New admission stopped while old in-flight/WAL state drains.
    Draining,
    /// Exact new binding read back, awaiting Go commit/resume.
    Ready,
    /// Single committed route accepts new windows.
    Active,
    /// Route failed closed due to drift or unavailable pool.
    Hold,
}

impl RouteState {
    /// Stable wire/display value.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Withdrawn => "withdrawn",
            Self::Draining => "draining",
            Self::Ready => "ready",
            Self::Active => "active",
            Self::Hold => "hold",
        }
    }
}

/// A shard's sole central inference route. No fallback client exists.
#[derive(Debug)]
pub struct InferenceRouter {
    tier: DeploymentTier,
    endpoint_policy: EndpointPolicyConfig,
    limits: RuntimeLimits,
    identities: BTreeMap<String, ClientTlsConfig>,
    route: Option<InferenceRoute>,
    binding: Option<BindingReadback>,
    client: Option<CentralInferenceClient<tonic::transport::Channel>>,
    state: RouteState,
    in_flight: usize,
}

impl InferenceRouter {
    /// Create a withdrawn route.
    #[must_use]
    pub fn new(
        tier: DeploymentTier,
        endpoint_policy: EndpointPolicyConfig,
        limits: RuntimeLimits,
        identities: BTreeMap<String, ClientTlsConfig>,
    ) -> Self {
        Self {
            tier,
            endpoint_policy,
            limits,
            identities,
            route: None,
            binding: None,
            client: None,
            state: RouteState::Withdrawn,
            in_flight: 0,
        }
    }

    /// Stop new window admission before drain/CAS/commit.
    pub fn prepare(
        &mut self,
        current_incarnation: &str,
        current_route_epoch: u64,
        proposed_route_epoch: u64,
    ) -> EdgeResult<()> {
        if proposed_route_epoch <= current_route_epoch {
            return Err(EdgeError::precondition(
                "ROUTE_FENCE_MISMATCH",
                "proposed route epoch must strictly increase",
            ));
        }
        if let Some(route) = &self.route {
            if route.model_control_incarnation_id != current_incarnation
                || route.route_epoch != current_route_epoch
            {
                return Err(EdgeError::precondition(
                    "ROUTE_FENCE_MISMATCH",
                    "prepare does not match current route incarnation/epoch",
                ));
            }
        } else if current_route_epoch != 0 {
            return Err(EdgeError::precondition(
                "ROUTE_FENCE_MISMATCH",
                "non-zero current epoch without a route",
            ));
        }
        self.state = RouteState::Draining;
        Ok(())
    }

    /// True only after all active RPCs are complete. WAL drain is checked by
    /// the owning actor before it invokes commit.
    #[must_use]
    pub fn rpc_drained(&self) -> bool {
        self.in_flight == 0
    }

    /// Connect to exactly one new generation and verify immutable readback.
    pub async fn commit(&mut self, route: InferenceRoute) -> EdgeResult<()> {
        if self.state == RouteState::Ready
            && self.route.as_ref().is_some_and(|current| current == &route)
            && self.binding.is_some()
            && self.client.is_some()
        {
            return Ok(());
        }
        if self.state != RouteState::Draining {
            return Err(EdgeError::precondition(
                "ROUTE_DRAIN_REQUIRED",
                "route must be prepared and drained before commit",
            ));
        }
        validate_route(&route)?;
        if let Some(previous) = &self.route
            && route.route_epoch <= previous.route_epoch
        {
            return Err(EdgeError::precondition(
                "ROUTE_FENCE_MISMATCH",
                "route epoch did not advance",
            ));
        }
        let tls = route
            .tls
            .as_ref()
            .ok_or_else(|| EdgeError::invalid("route.tls", "mTLS is required"))?;
        let client_tls = contract_tls(tls, &self.identities)?;
        let binding_request = crate::contract::edge::GetBindingRequest {
            logical_pool_id: route.logical_pool_id.clone(),
            pool_generation: route.pool_generation,
            binding_generation: route.binding_generation,
            trace_id: format!("binding-readback:{}", route.shard_id),
            schema_version: "inference-committed-binding/v1".into(),
            model_control_incarnation_id: route.model_control_incarnation_id.clone(),
            operation_id: route.operation_id.clone(),
            startup_envelope_digest: route.startup_envelope_digest.clone(),
            model_revision_digest: route.model_revision_digest.clone(),
            model_bundle_digest: route.model_bundle_digest.clone(),
            feature_contract_digest: route.feature_contract_digest.clone(),
            label_contract_digest: route.label_contract_digest.clone(),
            output_adapter_digest: route.output_adapter_digest.clone(),
            wire_profile: route.wire_profile.clone(),
            wire_profile_digest: route.wire_profile_digest.clone(),
            runtime_profile: route.runtime_profile.clone(),
            runtime_profile_digest: route.runtime_profile_digest.clone(),
            optimization_profile_digest: route.optimization_profile_digest.clone(),
            pool_observation_digest: route.pool_observation_digest.clone(),
            binding_digest: route.binding_digest.clone(),
        };
        if binding_request.encoded_len() > self.limits.inference_message_bytes {
            return Err(EdgeError::exhausted(
                "INFERENCE_MESSAGE_TOO_LARGE",
                "encoded binding readback request exceeds configured bound",
            ));
        }
        let channel = connect_mtls(
            &route.endpoint,
            &client_tls,
            self.tier,
            &self.endpoint_policy,
            Duration::from_millis(self.limits.inference_deadline_ms),
        )
        .await?;
        let mut client = CentralInferenceClient::new(channel)
            .max_decoding_message_size(self.limits.inference_message_bytes)
            .max_encoding_message_size(self.limits.inference_message_bytes);
        let mut request = Request::new(binding_request);
        request.set_timeout(Duration::from_millis(self.limits.inference_deadline_ms));
        let readback = tokio::time::timeout(
            Duration::from_millis(self.limits.inference_deadline_ms),
            client.get_binding(request),
        )
        .await
        .map_err(|_| EdgeError::Deadline("central binding readback".into()))?
        .map_err(|error| EdgeError::Remote {
            code: "POOL_UNAVAILABLE",
            message: error.to_string(),
        })?
        .into_inner();
        validate_binding(&route, &readback)?;
        self.route = Some(route);
        self.binding = Some(readback);
        self.client = Some(client);
        self.state = RouteState::Ready;
        Ok(())
    }

    /// Resume the exact route after Go commits the new canonical binding.
    pub fn resume(&mut self, request: &ResumeRouteRequest) -> EdgeResult<()> {
        if self.state == RouteState::Active {
            return self.validate_active_request(request);
        }
        self.validate_resume(request)?;
        self.state = RouteState::Active;
        Ok(())
    }

    /// Validate an exact Go-committed resume before durable route admission.
    pub(crate) fn validate_resume(&self, request: &ResumeRouteRequest) -> EdgeResult<()> {
        if self.state != RouteState::Ready {
            return Err(EdgeError::precondition(
                "ROUTE_NOT_READY",
                "route cannot resume before exact binding readback",
            ));
        }
        let route = self
            .route
            .as_ref()
            .ok_or_else(|| EdgeError::precondition("ROUTE_NOT_READY", "route missing"))?;
        validate_committed_binding_request(route, request)
    }

    fn validate_active_request(&self, request: &ResumeRouteRequest) -> EdgeResult<()> {
        let route = self
            .route
            .as_ref()
            .ok_or_else(|| EdgeError::precondition("ROUTE_NOT_READY", "route missing"))?;
        validate_committed_binding_request(route, request)
    }

    /// Reset volatile transport state before replaying the durable route journal.
    pub(crate) fn reset_for_recovery(&mut self) -> EdgeResult<()> {
        if self.in_flight != 0 {
            return Err(EdgeError::precondition(
                "ROUTE_DRAINING",
                "cannot reset route transport with an in-flight request",
            ));
        }
        self.route = None;
        self.binding = None;
        self.client = None;
        self.state = RouteState::Withdrawn;
        Ok(())
    }

    /// Current route state.
    #[must_use]
    pub fn state(&self) -> RouteState {
        self.state
    }

    /// Current exact route, if any.
    #[must_use]
    pub fn route(&self) -> Option<&InferenceRoute> {
        self.route.as_ref()
    }

    /// Run one no-delay, bounded batched-unary request. Retry never changes
    /// request identity, input digest, generation, backend, or endpoint.
    pub async fn infer(
        &mut self,
        records: Vec<InferenceRecord>,
        now_ms: i64,
        trace_id: String,
    ) -> EdgeResult<InferenceResultBatch> {
        if !matches!(self.state, RouteState::Active | RouteState::Draining) {
            return Err(EdgeError::precondition(
                "ROUTE_DRAINING",
                "no committed route is available for pending durable input",
            ));
        }
        if records.is_empty() || records.len() > self.limits.inference_batch_records {
            return Err(EdgeError::invalid(
                "inference_records",
                "batch record count outside configured bounds",
            ));
        }
        let route = self
            .route
            .clone()
            .ok_or_else(|| EdgeError::precondition("POOL_UNAVAILABLE", "route missing"))?;
        validate_inference_records(&route, &records)?;
        let request_id = request_identity(&route, &records);
        let deadline_ms = now_ms.saturating_add(
            i64::try_from(self.limits.inference_deadline_ms)
                .map_err(|_| EdgeError::invalid("inference_deadline_ms", "exceeds i64"))?,
        );
        let mut batch = InferenceInputBatch {
            schema_version: "inference-central-grpc-batch/v1".into(),
            request_id,
            route: Some(route),
            records,
            deadline_unix_ms: deadline_ms,
            attempt: 0,
            batch_digest: String::new(),
            trace_id,
        };
        batch.batch_digest = input_batch_digest(&batch);
        if batch.encoded_len() > self.limits.inference_message_bytes {
            return Err(EdgeError::exhausted(
                "INFERENCE_MESSAGE_TOO_LARGE",
                "encoded batch exceeds configured bound",
            ));
        }
        let max_attempts = self.limits.inference_max_attempts;
        let overall_deadline =
            tokio::time::Instant::now() + Duration::from_millis(self.limits.inference_deadline_ms);
        self.in_flight = self.in_flight.saturating_add(1);
        let outcome = async {
            for attempt in 1..=max_attempts {
                let remaining =
                    overall_deadline.saturating_duration_since(tokio::time::Instant::now());
                if remaining.is_zero() {
                    return Err(EdgeError::Deadline(
                        "central inference request exhausted its global monotonic deadline".into(),
                    ));
                }
                batch.attempt = attempt;
                let mut request = Request::new(batch.clone());
                request.set_timeout(remaining);
                let result = tokio::time::timeout_at(
                    overall_deadline,
                    self.client
                        .as_mut()
                        .ok_or_else(|| {
                            EdgeError::precondition("POOL_UNAVAILABLE", "central client missing")
                        })?
                        .infer(request),
                )
                .await;
                match result {
                    Ok(Ok(response)) => {
                        let response = response.into_inner();
                        validate_result_batch(&batch, &response, self.binding.as_ref())?;
                        return Ok(response);
                    }
                    Ok(Err(error)) if attempt < max_attempts && retryable(error.code()) => {
                        sleep_retry_backoff(attempt, overall_deadline).await?;
                    }
                    Ok(Err(error)) => {
                        return Err(EdgeError::Remote {
                            code: if error.code() == tonic::Code::DeadlineExceeded {
                                "DEADLINE_EXCEEDED"
                            } else {
                                "POOL_UNAVAILABLE"
                            },
                            message: error.to_string(),
                        });
                    }
                    Err(_) => {
                        return Err(EdgeError::Deadline(
                            "central inference request exhausted its global monotonic deadline"
                                .into(),
                        ));
                    }
                }
            }
            Err(EdgeError::Remote {
                code: "POOL_UNAVAILABLE",
                message: "retry budget exhausted".into(),
            })
        }
        .await;
        self.in_flight = self.in_flight.saturating_sub(1);
        // Transient exhaustion keeps the exact committed route active so the
        // durable input can be retried later with the same binding. Validation
        // drift returns from `validate_result_batch` and permanently holds the
        // route because accepting more results would cross a model fence.
        if matches!(
            outcome,
            Err(EdgeError::Precondition {
                code: "RESULT_IDENTITY_MISMATCH"
                    | "RESULT_DIGEST_CONFLICT"
                    | "RESULT_VALUE_INVALID"
                    | "BINDING_READBACK_MISMATCH",
                ..
            })
        ) {
            self.state = RouteState::Hold;
        }
        outcome
    }
}

/// Reconnecting, bounded Go Control sink.
#[derive(Debug)]
pub struct ControlSink {
    config: ControlSinkConfig,
    tier: DeploymentTier,
    endpoint_policy: EndpointPolicyConfig,
    limits: RuntimeLimits,
    client: Option<ControlSinkClient<tonic::transport::Channel>>,
}

impl ControlSink {
    /// Construct a disconnected sink; downstream outage does not kill liveness.
    #[must_use]
    pub fn new(
        config: ControlSinkConfig,
        tier: DeploymentTier,
        endpoint_policy: EndpointPolicyConfig,
        limits: RuntimeLimits,
    ) -> Self {
        Self {
            config,
            tier,
            endpoint_policy,
            limits,
            client: None,
        }
    }

    /// Commit results and require matching canonical Event ACKs.
    pub async fn commit_results(
        &mut self,
        batch: InferenceResultBatch,
    ) -> EdgeResult<CanonicalAckBatch> {
        if batch.records.is_empty() || batch.records.len() > self.limits.control_batch_records {
            return Err(EdgeError::invalid(
                "result_records",
                "control batch count outside configured bounds",
            ));
        }
        if batch.encoded_len() > self.limits.control_message_bytes {
            return Err(EdgeError::exhausted(
                "CONTROL_MESSAGE_TOO_LARGE",
                "encoded result batch exceeds configured bound",
            ));
        }
        self.ensure_connected().await?;
        let mut request = Request::new(batch.clone());
        request.set_timeout(Duration::from_millis(self.limits.control_deadline_ms));
        let outcome = tokio::time::timeout(
            Duration::from_millis(self.limits.control_deadline_ms),
            self.client
                .as_mut()
                .ok_or_else(|| EdgeError::Remote {
                    code: "CONTROL_UNAVAILABLE",
                    message: "control client missing".into(),
                })?
                .commit_results(request),
        )
        .await;
        let ack = match outcome {
            Ok(Ok(response)) => response.into_inner(),
            Ok(Err(error)) => {
                self.client = None;
                return Err(EdgeError::Remote {
                    code: "CONTROL_UNAVAILABLE",
                    message: error.to_string(),
                });
            }
            Err(_) => {
                self.client = None;
                return Err(EdgeError::Deadline(
                    "control result commit exceeded local monotonic deadline".into(),
                ));
            }
        };
        validate_canonical_acks(&batch, &ack)?;
        Ok(ack)
    }

    /// Publish cumulative rule observations.
    pub async fn publish_observations(
        &mut self,
        batch: RuleObservationBatch,
    ) -> EdgeResult<PublishAck> {
        if batch.observations.len() > 256 || batch.encoded_len() > self.limits.control_message_bytes
        {
            return Err(EdgeError::exhausted(
                "OBSERVATION_BATCH_TOO_LARGE",
                "observation batch exceeds configured bounds",
            ));
        }
        self.ensure_connected().await?;
        let identity = batch.batch_id.clone();
        let digest = batch.batch_digest.clone();
        let mut request = Request::new(batch);
        request.set_timeout(Duration::from_millis(self.limits.control_deadline_ms));
        let result = tokio::time::timeout(
            Duration::from_millis(self.limits.control_deadline_ms),
            self.client
                .as_mut()
                .ok_or_else(|| EdgeError::Remote {
                    code: "CONTROL_UNAVAILABLE",
                    message: "control client missing".into(),
                })?
                .publish_rule_observations(request),
        )
        .await;
        match result {
            Ok(Ok(response)) => {
                let ack = response.into_inner();
                validate_publish_ack(&ack, Some((&identity, &digest)))?;
                Ok(ack)
            }
            Ok(Err(error)) => {
                self.client = None;
                Err(EdgeError::Remote {
                    code: "CONTROL_UNAVAILABLE",
                    message: error.to_string(),
                })
            }
            Err(_) => {
                self.client = None;
                Err(EdgeError::Deadline(
                    "control observation publish exceeded local monotonic deadline".into(),
                ))
            }
        }
    }

    /// Publish bounded per-target runtime status.
    pub async fn publish_status(&mut self, batch: TargetStatusBatch) -> EdgeResult<PublishAck> {
        self.ensure_connected().await?;
        let mut request = Request::new(batch);
        request.set_timeout(Duration::from_millis(self.limits.control_deadline_ms));
        let result = tokio::time::timeout(
            Duration::from_millis(self.limits.control_deadline_ms),
            self.client
                .as_mut()
                .ok_or_else(|| EdgeError::Remote {
                    code: "CONTROL_UNAVAILABLE",
                    message: "control client missing".into(),
                })?
                .publish_target_status(request),
        )
        .await;
        match result {
            Ok(Ok(response)) => {
                let ack = response.into_inner();
                validate_publish_ack(&ack, None)?;
                Ok(ack)
            }
            Ok(Err(error)) => {
                self.client = None;
                Err(EdgeError::Remote {
                    code: "CONTROL_UNAVAILABLE",
                    message: error.to_string(),
                })
            }
            Err(_) => {
                self.client = None;
                Err(EdgeError::Deadline(
                    "control status publish exceeded local monotonic deadline".into(),
                ))
            }
        }
    }

    async fn ensure_connected(&mut self) -> EdgeResult<()> {
        if self.client.is_some() {
            return Ok(());
        }
        let channel = connect_mtls(
            &self.config.endpoint,
            &self.config.tls,
            self.tier,
            &self.endpoint_policy,
            Duration::from_millis(self.limits.control_deadline_ms),
        )
        .await?;
        self.client = Some(
            ControlSinkClient::new(channel)
                .max_decoding_message_size(self.limits.control_message_bytes)
                .max_encoding_message_size(self.limits.control_message_bytes),
        );
        Ok(())
    }
}

fn validate_route(route: &InferenceRoute) -> EdgeResult<()> {
    if route.schema_version != "inference-route/v1"
        || route.wire_profile != "inference-central-grpc-batch/v1"
    {
        return Err(EdgeError::UnknownMajor(format!(
            "{}/{}",
            route.schema_version, route.wire_profile
        )));
    }
    for (field, value) in [
        ("shard_id", route.shard_id.as_str()),
        (
            "model_control_incarnation_id",
            route.model_control_incarnation_id.as_str(),
        ),
        ("logical_pool_id", route.logical_pool_id.as_str()),
        ("operation_id", route.operation_id.as_str()),
        ("scope", route.scope.as_str()),
    ] {
        digest::validate_identity(value, field)?;
    }
    if route.pool_generation == 0
        || route.binding_generation == 0
        || route.route_epoch == 0
        || route.proposed_binding_generation == 0
        || route.current_binding_generation == 0
    {
        return Err(EdgeError::invalid(
            "route_generation",
            "pool, binding, proposed/current binding, and route generations must be positive",
        ));
    }
    if route.binding_generation != route.proposed_binding_generation
        || route.binding_generation != route.current_binding_generation
        || route.expected_binding_generation >= route.proposed_binding_generation
    {
        return Err(EdgeError::precondition(
            "ROUTE_FENCE_MISMATCH",
            "binding generation must equal proposed/current and strictly exceed expected",
        ));
    }
    for (field, value) in [
        (
            "model_revision_digest",
            route.model_revision_digest.as_str(),
        ),
        (
            "feature_contract_digest",
            route.feature_contract_digest.as_str(),
        ),
        (
            "label_contract_digest",
            route.label_contract_digest.as_str(),
        ),
        (
            "output_adapter_digest",
            route.output_adapter_digest.as_str(),
        ),
        (
            "startup_envelope_digest",
            route.startup_envelope_digest.as_str(),
        ),
        (
            "pool_observation_digest",
            route.pool_observation_digest.as_str(),
        ),
        ("binding_digest", route.binding_digest.as_str()),
        ("model_bundle_digest", route.model_bundle_digest.as_str()),
        ("wire_profile_digest", route.wire_profile_digest.as_str()),
        (
            "runtime_profile_digest",
            route.runtime_profile_digest.as_str(),
        ),
        (
            "optimization_profile_digest",
            route.optimization_profile_digest.as_str(),
        ),
    ] {
        digest::validate_sha256(value, field)?;
    }
    if !matches!(
        route.runtime_profile.as_str(),
        "model-runtime-central-cpu/v1" | "model-runtime-central-cuda/v1"
    ) {
        return Err(EdgeError::precondition(
            "RUNTIME_PROFILE_UNSUPPORTED",
            "runtime profile must be an explicitly selected central CPU or CUDA profile",
        ));
    }
    Ok(())
}

fn validate_binding(route: &InferenceRoute, binding: &BindingReadback) -> EdgeResult<()> {
    let exact = binding.schema_version == "inference-committed-binding/v1"
        && route.model_control_incarnation_id == binding.model_control_incarnation_id
        && route.operation_id == binding.operation_id
        && route.logical_pool_id == binding.logical_pool_id
        && route.pool_generation == binding.pool_generation
        && route.binding_generation == binding.binding_generation
        && route.startup_envelope_digest == binding.startup_envelope_digest
        && route.model_revision_digest == binding.model_revision_digest
        && route.model_bundle_digest == binding.model_bundle_digest
        && route.feature_contract_digest == binding.feature_contract_digest
        && route.label_contract_digest == binding.label_contract_digest
        && route.output_adapter_digest == binding.output_adapter_digest
        && route.wire_profile == binding.wire_profile
        && route.wire_profile_digest == binding.wire_profile_digest
        && route.runtime_profile == binding.runtime_profile
        && route.runtime_profile_digest == binding.runtime_profile_digest
        && route.optimization_profile_digest == binding.optimization_profile_digest
        && route.pool_observation_digest == binding.pool_observation_digest
        && route.binding_digest == binding.binding_digest;
    if !exact {
        return Err(EdgeError::precondition(
            "BINDING_READBACK_MISMATCH",
            "central binding readback differs from the immutable route envelope",
        ));
    }
    digest::validate_identity(&binding.worker_id, "worker_id")?;
    digest::validate_sha256(&binding.worker_digest, "worker_digest")?;
    digest::validate_identity(&binding.readback_attempt_id, "readback_attempt_id")?;
    if binding.observed_at_unix_ms <= 0 {
        return Err(EdgeError::precondition(
            "BINDING_READBACK_MISMATCH",
            "central binding readback observed time is invalid",
        ));
    }
    if binding.eligible_workers.is_empty() || binding.eligible_workers.len() > 64 {
        return Err(EdgeError::precondition(
            "BINDING_READBACK_MISMATCH",
            "central binding readback eligible worker set is empty or exceeds 64",
        ));
    }
    let mut workers = BTreeMap::new();
    for worker in &binding.eligible_workers {
        digest::validate_identity(&worker.worker_id, "eligible_worker.worker_id")?;
        digest::validate_sha256(&worker.worker_digest, "eligible_worker.worker_digest")?;
        if workers
            .insert(worker.worker_id.as_str(), worker.worker_digest.as_str())
            .is_some()
        {
            return Err(EdgeError::precondition(
                "BINDING_READBACK_MISMATCH",
                "central binding readback contains duplicate worker identities",
            ));
        }
    }
    if workers.get(binding.worker_id.as_str()) != Some(&binding.worker_digest.as_str()) {
        return Err(EdgeError::precondition(
            "BINDING_READBACK_MISMATCH",
            "central primary worker is absent from the eligible worker set",
        ));
    }
    Ok(())
}

fn validate_committed_binding_request(
    route: &InferenceRoute,
    request: &ResumeRouteRequest,
) -> EdgeResult<()> {
    if request.schema_version != "inference-committed-binding/v1" {
        return Err(EdgeError::UnknownMajor(request.schema_version.clone()));
    }
    let exact = request.shard_id == route.shard_id
        && request.model_control_incarnation_id == route.model_control_incarnation_id
        && request.operation_id == route.operation_id
        && request.scope == route.scope
        && request.route_epoch == route.route_epoch
        && request.logical_pool_id == route.logical_pool_id
        && request.pool_generation == route.pool_generation
        && request.expected_binding_generation == route.expected_binding_generation
        && request.proposed_binding_generation == route.proposed_binding_generation
        && request.current_binding_generation == route.current_binding_generation
        && request.startup_envelope_digest == route.startup_envelope_digest
        && request.pool_observation_digest == route.pool_observation_digest
        && request.binding_digest == route.binding_digest;
    if !exact {
        return Err(EdgeError::precondition(
            "ROUTE_FENCE_MISMATCH",
            "committed binding handshake does not exactly match the prepared route",
        ));
    }
    Ok(())
}

/// Bind a durable, route-neutral final window to one exact committed route.
/// The tensor identity/digest remain stable while Event identity acquires the
/// model-control fence. A bound record can only be replayed on that route.
pub(crate) fn bind_inference_record(
    record: &mut InferenceRecord,
    route: &InferenceRoute,
) -> EdgeResult<()> {
    validate_route(route)?;
    if record.target_id != route.shard_id
        || record.feature_contract_digest != route.feature_contract_digest
    {
        return Err(EdgeError::precondition(
            "INPUT_CONTRACT_MISMATCH",
            "route-neutral window does not match route shard/feature contract",
        ));
    }
    if inference_record_is_bound(record) {
        if record_matches_route(record, route)
            && canonical_input_record_digest(record) == record.input_digest
        {
            return Ok(());
        }
        return Err(EdgeError::precondition(
            "ROUTE_FENCE_MISMATCH",
            "durable input is already bound to another route identity",
        ));
    }
    record.model_control_incarnation_id = route.model_control_incarnation_id.clone();
    record.logical_pool_id = route.logical_pool_id.clone();
    record.pool_generation = route.pool_generation;
    record.binding_generation = route.binding_generation;
    record.route_epoch = route.route_epoch;
    record.model_revision_digest = route.model_revision_digest.clone();
    record.label_contract_digest = route.label_contract_digest.clone();
    record.output_adapter_digest = route.output_adapter_digest.clone();
    record.wire_profile = route.wire_profile.clone();
    record.runtime_profile = route.runtime_profile.clone();
    record.operation_id = route.operation_id.clone();
    record.scope = route.scope.clone();
    record.expected_binding_generation = route.expected_binding_generation;
    record.proposed_binding_generation = route.proposed_binding_generation;
    record.current_binding_generation = route.current_binding_generation;
    record.startup_envelope_digest = route.startup_envelope_digest.clone();
    record.pool_observation_digest = route.pool_observation_digest.clone();
    record.binding_digest = route.binding_digest.clone();
    record.model_bundle_digest = route.model_bundle_digest.clone();
    record.wire_profile_digest = route.wire_profile_digest.clone();
    record.runtime_profile_digest = route.runtime_profile_digest.clone();
    record.optimization_profile_digest = route.optimization_profile_digest.clone();
    let event_material = [
        record.target_id.clone(),
        record.window_id.clone(),
        record.model_control_incarnation_id.clone(),
        record.logical_pool_id.clone(),
        record.pool_generation.to_string(),
        record.binding_generation.to_string(),
        record.route_epoch.to_string(),
        record.model_revision_digest.clone(),
    ]
    .join("\0");
    record.event_idempotency_key = format!(
        "event:{}",
        digest::sha256(event_material.as_bytes()).trim_start_matches("sha256:"),
    );
    record.input_digest = canonical_input_record_digest(record);
    Ok(())
}

/// Whether an input WAL record has acquired a complete route fence.
#[must_use]
pub(crate) fn inference_record_is_bound(record: &InferenceRecord) -> bool {
    record.route_epoch != 0
}

fn record_matches_route(record: &InferenceRecord, route: &InferenceRoute) -> bool {
    record.model_control_incarnation_id == route.model_control_incarnation_id
        && record.logical_pool_id == route.logical_pool_id
        && record.pool_generation == route.pool_generation
        && record.binding_generation == route.binding_generation
        && record.route_epoch == route.route_epoch
        && record.model_revision_digest == route.model_revision_digest
        && record.feature_contract_digest == route.feature_contract_digest
        && record.label_contract_digest == route.label_contract_digest
        && record.output_adapter_digest == route.output_adapter_digest
        && record.wire_profile == route.wire_profile
        && record.runtime_profile == route.runtime_profile
        && record.operation_id == route.operation_id
        && record.scope == route.scope
        && record.expected_binding_generation == route.expected_binding_generation
        && record.proposed_binding_generation == route.proposed_binding_generation
        && record.current_binding_generation == route.current_binding_generation
        && record.startup_envelope_digest == route.startup_envelope_digest
        && record.pool_observation_digest == route.pool_observation_digest
        && record.binding_digest == route.binding_digest
        && record.model_bundle_digest == route.model_bundle_digest
        && record.wire_profile_digest == route.wire_profile_digest
        && record.runtime_profile_digest == route.runtime_profile_digest
        && record.optimization_profile_digest == route.optimization_profile_digest
}

fn request_identity(route: &InferenceRoute, records: &[InferenceRecord]) -> String {
    let mut material = format!(
        "{}\0{}\0{}\0{}\0{}",
        route.model_control_incarnation_id,
        route.logical_pool_id,
        route.pool_generation,
        route.binding_generation,
        route.route_epoch
    )
    .into_bytes();
    for record in records {
        material.extend_from_slice(record.input_id.as_bytes());
        material.push(0);
        material.extend_from_slice(record.input_digest.as_bytes());
        material.push(0);
    }
    let hash = digest::sha256(&material);
    format!("request:{}", hash.trim_start_matches("sha256:"))
}

fn input_batch_digest(batch: &InferenceInputBatch) -> String {
    let mut canonical = batch.clone();
    canonical.batch_digest.clear();
    canonical.attempt = 0;
    canonical.deadline_unix_ms = 0;
    canonical.trace_id.clear();
    digest::message_sha256(&canonical)
}

/// Compute a retry-stable canonical input-batch digest.
#[must_use]
pub fn canonical_input_batch_digest(batch: &InferenceInputBatch) -> String {
    input_batch_digest(batch)
}

fn output_record_digest(record: &InferenceResultRecord) -> String {
    let mut canonical = Vec::with_capacity(record.scores.len() * std::mem::size_of::<f32>());
    for score in &record.scores {
        canonical.extend_from_slice(&score.to_le_bytes());
    }
    digest::sha256(&canonical)
}

/// Compute the canonical digest of one central inference output record.
#[must_use]
pub fn canonical_output_record_digest(record: &InferenceResultRecord) -> String {
    output_record_digest(record)
}

/// Compute the canonical output digest used by deterministic providers.
#[must_use]
pub fn canonical_output_digest(record: &InferenceResultRecord) -> String {
    output_record_digest(record)
}

fn result_batch_digest(batch: &InferenceResultBatch) -> String {
    let mut canonical = Vec::with_capacity(batch.records.len() * 72);
    for record in &batch.records {
        canonical.extend_from_slice(record.output_digest.as_bytes());
        canonical.push(b'\n');
    }
    digest::sha256(&canonical)
}

/// Compute the canonical result-batch digest.
#[must_use]
pub fn canonical_result_batch_digest(batch: &InferenceResultBatch) -> String {
    result_batch_digest(batch)
}

fn validate_result_batch(
    input: &InferenceInputBatch,
    result: &InferenceResultBatch,
    binding: Option<&BindingReadback>,
) -> EdgeResult<()> {
    if result.schema_version != "inference-central-grpc-batch/v1"
        || result.request_id != input.request_id
        || result.route != input.route
        || result.records.len() != input.records.len()
    {
        return Err(EdgeError::precondition(
            "RESULT_IDENTITY_MISMATCH",
            "result batch identity/route/cardinality differs from input",
        ));
    }
    digest::validate_sha256(&result.batch_digest, "result.batch_digest")?;
    if result_batch_digest(result) != result.batch_digest {
        return Err(EdgeError::precondition(
            "RESULT_DIGEST_CONFLICT",
            "result batch digest mismatch",
        ));
    }
    let inputs: BTreeMap<&str, &InferenceRecord> = input
        .records
        .iter()
        .map(|record| (record.input_id.as_str(), record))
        .collect();
    let binding = binding
        .ok_or_else(|| EdgeError::precondition("BINDING_READBACK_MISMATCH", "binding missing"))?;
    let mut seen = BTreeMap::new();
    for record in &result.records {
        let source = inputs.get(record.input_id.as_str()).ok_or_else(|| {
            EdgeError::precondition("RESULT_IDENTITY_MISMATCH", "unknown result input ID")
        })?;
        if seen.insert(record.input_id.as_str(), ()).is_some()
            || record.event_idempotency_key != source.event_idempotency_key
            || record.input_digest != source.input_digest
            || record.source_wal_sequence != source.source_wal_sequence
            || record.input_wal_sequence != source.input_wal_sequence
            || record.result_wal_sequence != 0
            || !binding.eligible_workers.iter().any(|worker| {
                worker.worker_id == record.worker_id && worker.worker_digest == record.worker_digest
            })
            || record.window_id != source.window_id
            || record.model_control_incarnation_id != source.model_control_incarnation_id
            || record.logical_pool_id != source.logical_pool_id
            || record.pool_generation != source.pool_generation
            || record.binding_generation != source.binding_generation
            || record.route_epoch != source.route_epoch
            || record.model_revision_digest != source.model_revision_digest
            || record.feature_contract_digest != source.feature_contract_digest
            || record.label_contract_digest != source.label_contract_digest
            || record.output_adapter_digest != source.output_adapter_digest
            || record.wire_profile != source.wire_profile
            || record.runtime_profile != source.runtime_profile
            || record.source_runtime_epoch != source.source_runtime_epoch
            || record.source_sequence_start != source.source_sequence_start
            || record.source_sequence_end != source.source_sequence_end
            || record.window_start_unix_ms != source.window_start_unix_ms
            || record.window_end_unix_ms != source.window_end_unix_ms
            || record.finalized_at_unix_ms != source.finalized_at_unix_ms
            || record.target_id != source.target_id
            || record.sampling_coverage_ppm != source.sampling_coverage_ppm
            || record.operation_id != source.operation_id
            || record.scope != source.scope
            || record.expected_binding_generation != source.expected_binding_generation
            || record.proposed_binding_generation != source.proposed_binding_generation
            || record.current_binding_generation != source.current_binding_generation
            || record.startup_envelope_digest != source.startup_envelope_digest
            || record.pool_observation_digest != source.pool_observation_digest
            || record.binding_digest != source.binding_digest
            || record.model_bundle_digest != source.model_bundle_digest
            || record.wire_profile_digest != source.wire_profile_digest
            || record.runtime_profile_digest != source.runtime_profile_digest
            || record.optimization_profile_digest != source.optimization_profile_digest
            || record.inference_started_at_unix_ms <= 0
            || record.inference_completed_at_unix_ms < record.inference_started_at_unix_ms
            || record.trace_id != input.trace_id
        {
            return Err(EdgeError::precondition(
                "RESULT_IDENTITY_MISMATCH",
                "record identity, WAL fence, or worker differs from input/readback",
            ));
        }
        digest::validate_identity(&record.worker_attempt_id, "worker_attempt_id")?;
        let decision = InferenceDecision::try_from(record.decision_code).map_err(|_| {
            EdgeError::precondition("RESULT_VALUE_INVALID", "unknown typed inference decision")
        })?;
        let decision_exact = matches!(
            (record.decision.as_str(), decision),
            ("benign", InferenceDecision::Benign)
                | ("alert", InferenceDecision::Alert)
                | ("abstain", InferenceDecision::Abstain)
        );
        if record.scores.is_empty()
            || record.scores.len() > 1024
            || record.scores.iter().any(|score| !score.is_finite())
            || record.predicted_label as usize >= record.scores.len()
            || record.status != "OK"
            || record.quality != "valid"
            || record.execution_status != InferenceExecutionStatus::Ok as i32
            || record.quality_code != DataQuality::Valid as i32
            || !decision_exact
            || record.abstain != (decision == InferenceDecision::Abstain)
            || (record.out_of_distribution && !record.abstain)
            || record.error_code != "NONE"
        {
            return Err(EdgeError::precondition(
                "RESULT_VALUE_INVALID",
                "central result value/status is invalid or out of bounds",
            ));
        }
        digest::validate_sha256(&record.output_digest, "output_digest")?;
        for (field, value) in [
            (
                "startup_envelope_digest",
                record.startup_envelope_digest.as_str(),
            ),
            (
                "pool_observation_digest",
                record.pool_observation_digest.as_str(),
            ),
            ("binding_digest", record.binding_digest.as_str()),
            ("model_bundle_digest", record.model_bundle_digest.as_str()),
            ("wire_profile_digest", record.wire_profile_digest.as_str()),
            (
                "runtime_profile_digest",
                record.runtime_profile_digest.as_str(),
            ),
            (
                "optimization_profile_digest",
                record.optimization_profile_digest.as_str(),
            ),
        ] {
            digest::validate_sha256(value, field)?;
        }
        if output_record_digest(record) != record.output_digest {
            return Err(EdgeError::precondition(
                "RESULT_DIGEST_CONFLICT",
                "record output digest mismatch",
            ));
        }
    }
    Ok(())
}

fn validate_inference_records(
    route: &InferenceRoute,
    records: &[InferenceRecord],
) -> EdgeResult<()> {
    let mut input_ids = BTreeMap::new();
    let mut event_keys = BTreeMap::new();
    for record in records {
        digest::validate_identity(&record.input_id, "input_id")?;
        digest::validate_identity(&record.event_idempotency_key, "event_idempotency_key")?;
        digest::validate_identity(&record.window_id, "window_id")?;
        if input_ids.insert(record.input_id.as_str(), ()).is_some()
            || event_keys
                .insert(record.event_idempotency_key.as_str(), ())
                .is_some()
        {
            return Err(EdgeError::precondition(
                "INPUT_IDENTITY_CONFLICT",
                "inference batch contains duplicate input/Event identity",
            ));
        }
        if record.schema_version != "edge-inference-record/v1"
            || record.target_id != route.shard_id
            || record.feature_contract_digest != route.feature_contract_digest
            || !record.final_window
            || record.quality != "valid"
            || record.quality_code != DataQuality::Valid as i32
            || record.dtype != FEATURE_DTYPE
            || record.shape.as_slice() != [1, FEATURE_WIDTH as u32]
            || record.feature_tensor.len() != FEATURE_WIDTH * std::mem::size_of::<u64>()
            || record.source_sequence_start > record.source_sequence_end
            || record.window_start_unix_ms >= record.window_end_unix_ms
            || record.watermark_unix_ms < record.window_end_unix_ms
            || record.finalized_at_unix_ms < record.window_end_unix_ms
            || record.source_wal_sequence == 0
            || record.input_wal_sequence == 0
            || !record_matches_route(record, route)
            || record.quality_reasons.as_slice() != ["NONE"]
            || record.sampling_coverage_ppm > 1_000_000
            || record.enqueued_at_unix_ms < record.finalized_at_unix_ms
        {
            return Err(EdgeError::precondition(
                "INPUT_CONTRACT_MISMATCH",
                "inference record schema/feature/finality/window/WAL fence mismatch",
            ));
        }
        digest::validate_sha256(&record.feature_contract_digest, "feature_contract_digest")?;
        digest::validate_sha256(&record.model_revision_digest, "model_revision_digest")?;
        digest::validate_sha256(&record.label_contract_digest, "label_contract_digest")?;
        digest::validate_sha256(&record.output_adapter_digest, "output_adapter_digest")?;
        digest::validate_sha256(&record.startup_envelope_digest, "startup_envelope_digest")?;
        digest::validate_sha256(&record.pool_observation_digest, "pool_observation_digest")?;
        digest::validate_sha256(&record.binding_digest, "binding_digest")?;
        digest::validate_sha256(&record.model_bundle_digest, "model_bundle_digest")?;
        digest::validate_sha256(&record.wire_profile_digest, "wire_profile_digest")?;
        digest::validate_sha256(&record.runtime_profile_digest, "runtime_profile_digest")?;
        digest::validate_sha256(
            &record.optimization_profile_digest,
            "optimization_profile_digest",
        )?;
        digest::validate_sha256(&record.input_digest, "input_digest")?;
        if canonical_input_record_digest(record) != record.input_digest {
            return Err(EdgeError::precondition(
                "INPUT_DIGEST_CONFLICT",
                "inference record digest mismatch",
            ));
        }
    }
    Ok(())
}

pub(crate) fn validate_canonical_acks(
    batch: &InferenceResultBatch,
    ack: &CanonicalAckBatch,
) -> EdgeResult<()> {
    if ack.schema_version != "canonical-event-ack/v1"
        || ack.acknowledgements.len() != batch.records.len()
        || ack.result_batch_digest != batch.batch_digest
    {
        return Err(EdgeError::precondition(
            "CANONICAL_ACK_MISMATCH",
            "ACK version/cardinality differs from result batch",
        ));
    }
    digest::validate_sha256(&ack.ack_batch_digest, "ack.ack_batch_digest")?;
    if canonical_ack_batch_digest(ack) != ack.ack_batch_digest {
        return Err(EdgeError::precondition(
            "CANONICAL_ACK_MISMATCH",
            "canonical ACK batch digest mismatch",
        ));
    }
    let expected: BTreeMap<&str, (&str, &str)> = batch
        .records
        .iter()
        .map(|record| {
            (
                record.event_idempotency_key.as_str(),
                (record.input_digest.as_str(), record.output_digest.as_str()),
            )
        })
        .collect();
    let mut seen = BTreeMap::new();
    for item in &ack.acknowledgements {
        let Some((input_digest, output_digest)) = expected.get(item.event_idempotency_key.as_str())
        else {
            return Err(EdgeError::precondition(
                "CANONICAL_ACK_MISMATCH",
                "ACK contains an unknown Event idempotency key",
            ));
        };
        if seen
            .insert(item.event_idempotency_key.as_str(), ())
            .is_some()
            || item.input_digest != *input_digest
            || item.output_digest != *output_digest
            || !matches!(
                (
                    item.status.as_str(),
                    CanonicalCommitStatus::try_from(item.commit_status)
                ),
                ("committed", Ok(CanonicalCommitStatus::Committed))
                    | ("idempotent", Ok(CanonicalCommitStatus::Idempotent))
            )
            || item.canonical_event_id.is_empty()
            || item.committed_at_unix_ms <= 0
        {
            return Err(EdgeError::precondition(
                "CANONICAL_ACK_MISMATCH",
                format!(
                    "ACK digest, status, identity, or commit time mismatch: status={}, reason={}",
                    item.status, item.reason_code
                ),
            ));
        }
    }
    Ok(())
}

fn validate_publish_ack(
    ack: &PublishAck,
    expected_identity_digest: Option<(&str, &str)>,
) -> EdgeResult<()> {
    let status = PublishStatus::try_from(ack.status_code).map_err(|_| {
        EdgeError::precondition("PUBLISH_ACK_MISMATCH", "unknown typed publish status")
    })?;
    let status_exact = matches!(
        (ack.status.as_str(), status),
        ("accepted", PublishStatus::Accepted)
            | ("checkpointed", PublishStatus::Checkpointed)
            | ("idempotent", PublishStatus::Idempotent)
    );
    if !status_exact {
        return Err(EdgeError::precondition(
            "PUBLISH_ACK_MISMATCH",
            "typed/display publish status conflict",
        ));
    }
    if let Some((identity, expected_digest)) = expected_identity_digest
        && (ack.identity != identity || ack.digest != expected_digest)
    {
        return Err(EdgeError::precondition(
            "PUBLISH_ACK_MISMATCH",
            "publish ACK identity or digest mismatch",
        ));
    }
    Ok(())
}

/// Canonical digest of a PostgreSQL Event commit ACK batch.
#[must_use]
pub fn canonical_ack_batch_digest(batch: &CanonicalAckBatch) -> String {
    let mut canonical = batch.clone();
    canonical.ack_batch_digest.clear();
    digest::message_sha256(&canonical)
}

fn retryable(code: tonic::Code) -> bool {
    matches!(
        code,
        tonic::Code::Unavailable | tonic::Code::ResourceExhausted | tonic::Code::Aborted
    )
}

async fn sleep_retry_backoff(
    completed_attempt: u32,
    overall_deadline: tokio::time::Instant,
) -> EdgeResult<()> {
    let backoff = Duration::from_millis(if completed_attempt == 1 { 25 } else { 100 });
    let remaining = overall_deadline.saturating_duration_since(tokio::time::Instant::now());
    if remaining <= backoff {
        return Err(EdgeError::Deadline(
            "central inference retry backoff would exceed the global monotonic deadline".into(),
        ));
    }
    tokio::time::sleep(backoff).await;
    Ok(())
}

fn contract_tls(
    value: &TlsClientIdentity,
    identities: &BTreeMap<String, ClientTlsConfig>,
) -> EdgeResult<ClientTlsConfig> {
    digest::validate_identity(&value.identity_ref, "tls.identity_ref")?;
    let identity = identities.get(&value.identity_ref).ok_or_else(|| {
        EdgeError::precondition(
            "TLS_IDENTITY_UNKNOWN",
            "credential identity_ref is absent from the local registry",
        )
    })?;
    if identity.server_name != value.server_name {
        return Err(EdgeError::TlsIdentityMismatch(
            "wire server_name differs from the registered credential SAN".into(),
        ));
    }
    Ok(identity.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn batch_digest_does_not_change_across_retry_attempt() {
        let mut batch = InferenceInputBatch {
            schema_version: "inference-central-grpc-batch/v1".into(),
            request_id: "request-1".into(),
            attempt: 1,
            ..InferenceInputBatch::default()
        };
        let first = input_batch_digest(&batch);
        batch.attempt = 3;
        batch.deadline_unix_ms = 1_893_456_000_000;
        batch.trace_id = "different-rpc-trace".into();
        assert_eq!(first, input_batch_digest(&batch));
    }

    #[test]
    fn output_and_result_batch_digests_match_the_cross_language_profile() {
        let mut record = InferenceResultRecord {
            scores: vec![2.148_195_2e-25_f32, 1.0],
            ..InferenceResultRecord::default()
        };
        let expected_output =
            "sha256:3fdaaa5605f835ec748c0795734317d2f9c4e22c2f1bc23461ee1b892c5ca0d9";
        assert_eq!(expected_output, output_record_digest(&record));
        record.worker_attempt_id = "a-different-attempt".into();
        record.inference_completed_at_unix_ms = 123;
        assert_eq!(expected_output, output_record_digest(&record));

        record.output_digest = expected_output.into();
        let batch = InferenceResultBatch {
            records: vec![record],
            ..InferenceResultBatch::default()
        };
        assert_eq!(
            "sha256:9a78403e49fd21d593546e9058457a04d94657a98ad2eadf378be19c5a992a97",
            result_batch_digest(&batch)
        );
    }

    #[test]
    fn retryable_codes_exactly_match_the_frozen_profile() {
        for code in [
            tonic::Code::Unavailable,
            tonic::Code::ResourceExhausted,
            tonic::Code::Aborted,
        ] {
            assert!(retryable(code), "{code:?} must remain retryable");
        }
        for code in [
            tonic::Code::Ok,
            tonic::Code::Cancelled,
            tonic::Code::Unknown,
            tonic::Code::InvalidArgument,
            tonic::Code::DeadlineExceeded,
            tonic::Code::NotFound,
            tonic::Code::AlreadyExists,
            tonic::Code::PermissionDenied,
            tonic::Code::FailedPrecondition,
            tonic::Code::OutOfRange,
            tonic::Code::Unimplemented,
            tonic::Code::Internal,
            tonic::Code::DataLoss,
            tonic::Code::Unauthenticated,
        ] {
            assert!(!retryable(code), "{code:?} must fail without retry");
        }
    }

    #[tokio::test]
    async fn retry_backoff_refuses_to_cross_the_global_deadline()
    -> Result<(), Box<dyn std::error::Error>> {
        let started = tokio::time::Instant::now();
        let error = sleep_retry_backoff(1, started + Duration::from_millis(10))
            .await
            .err()
            .ok_or("25ms backoff was admitted into a 10ms remaining budget")?;
        assert_eq!("DEADLINE_EXCEEDED", error.reason_code());
        Ok(())
    }

    #[test]
    fn automatic_backend_fallback_is_not_a_route_value() {
        let mut route = InferenceRoute {
            schema_version: "inference-route/v1".into(),
            wire_profile: "inference-central-grpc-batch/v1".into(),
            shard_id: "shard-1".into(),
            model_control_incarnation_id: "incarnation-1".into(),
            logical_pool_id: "pool-1".into(),
            pool_generation: 1,
            binding_generation: 1,
            route_epoch: 1,
            model_revision_digest: format!("sha256:{}", "a".repeat(64)),
            feature_contract_digest: format!("sha256:{}", "b".repeat(64)),
            label_contract_digest: format!("sha256:{}", "c".repeat(64)),
            output_adapter_digest: format!("sha256:{}", "d".repeat(64)),
            runtime_profile: "automatic".into(),
            operation_id: "operation-1".into(),
            scope: "scope-1".into(),
            proposed_binding_generation: 1,
            current_binding_generation: 1,
            startup_envelope_digest: format!("sha256:{}", "e".repeat(64)),
            pool_observation_digest: format!("sha256:{}", "f".repeat(64)),
            binding_digest: format!("sha256:{}", "1".repeat(64)),
            model_bundle_digest: format!("sha256:{}", "2".repeat(64)),
            wire_profile_digest: format!("sha256:{}", "3".repeat(64)),
            runtime_profile_digest: format!("sha256:{}", "4".repeat(64)),
            optimization_profile_digest: format!("sha256:{}", "5".repeat(64)),
            ..InferenceRoute::default()
        };
        assert!(validate_route(&route).is_err());
        route.runtime_profile = "model-runtime-central-cpu/v1".into();
        assert!(validate_route(&route).is_ok());
    }
}
