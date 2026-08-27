//! Per-target actor: sole session, queues, journals, source, route, and sweep.

use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    path::Path,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use prost::Message as _;
use tokio::sync::{mpsc, oneshot, watch};

use crate::{
    EdgeError, EdgeResult,
    config::EdgeConfig,
    contract::{
        edge::{
            AcknowledgeEffectRequest, ActorState, AdmissionResult, AppliedRuleReadback,
            ClearAdvanceCondition, CommitRouteRequest, CompiledEffectPlan,
            ConfigureRuleObservationsRequest, CounterValue, DataQuality, DropEvidence,
            EffectIntent, EffectJournalRecord, EffectResult, EffectStatus, Fence, FreshnessStatus,
            InferenceRecord, InferenceRouteLifecycle, InstallationReadbackStatus, JournalStage,
            PreflightEffectReply, PrepareRouteRequest, PublishAck, PublishStatus,
            RenewTargetRequest, ResultWalRecord, ResultWalStage, ResumeRouteRequest,
            RevokeTargetRequest, RouteReply, RouteResumeWatermark, RouteWalRecord, RouteWalStage,
            RuleObservation, RuleObservationBatch, SamplingEvidence, SnapshotConsistency,
            SourceGap, SourceWalRecord, SourceWalStage, SupplementalHint, TargetAssignment,
            TargetReply, TargetStatus, TargetStatusBatch, TelemetryCell, TelemetryFlowDirection,
            TelemetryFlowIdentityProfile, TelemetryIpVersion, TelemetrySnapshot,
        },
        p4::{Entity, Update, entity, update},
    },
    digest,
    firewall::{self, CompiledPlan},
    inference::{
        ControlSink, InferenceRouter, RouteState, bind_inference_record,
        canonical_ack_batch_digest, canonical_result_batch_digest, inference_record_is_bound,
        validate_canonical_acks,
    },
    p4runtime::{P4Session, P4StreamEvent},
    wal::{DurableWal, WalKind, WalLimits},
    window::WindowEngine,
};

/// Handle used by the supervisor; all mutable target state remains in the task.
#[derive(Clone, Debug)]
pub struct ActorHandle {
    target_id: String,
    commands: mpsc::Sender<ActorCommand>,
    status: watch::Receiver<TargetStatus>,
}

impl ActorHandle {
    /// Stable target identity.
    #[must_use]
    pub fn target_id(&self) -> &str {
        &self.target_id
    }

    /// Latest non-blocking runtime status.
    #[must_use]
    pub fn status(&self) -> TargetStatus {
        self.status.borrow().clone()
    }

    /// Renew the exact non-reusable lease.
    pub async fn renew(&self, request: RenewTargetRequest) -> EdgeResult<TargetReply> {
        self.call(|reply| ActorCommand::Renew(request, reply)).await
    }

    /// Revoke the actor's write authority.
    pub async fn revoke(&self, request: RevokeTargetRequest) -> EdgeResult<TargetReply> {
        self.call(|reply| ActorCommand::Revoke(request, reply))
            .await
    }

    /// Read-only compile/capability/capacity preflight.
    pub async fn preflight(&self, intent: EffectIntent) -> EdgeResult<PreflightEffectReply> {
        self.call(|reply| ActorCommand::Preflight(intent, reply))
            .await
    }

    /// Execute a preflight-frozen effect.
    pub async fn execute(&self, intent: EffectIntent, token: String) -> EdgeResult<EffectResult> {
        self.call(|reply| ActorCommand::Execute(intent, token, reply))
            .await
    }

    /// Confirm Go/PostgreSQL CAS before journal checkpoint.
    pub async fn acknowledge_effect(
        &self,
        request: AcknowledgeEffectRequest,
    ) -> EdgeResult<PublishAck> {
        self.call(|reply| ActorCommand::AcknowledgeEffect(request, reply))
            .await
    }

    /// Withdraw and drain a route.
    pub async fn prepare_route(&self, request: PrepareRouteRequest) -> EdgeResult<RouteReply> {
        self.call(|reply| ActorCommand::PrepareRoute(request, reply))
            .await
    }

    /// Install and read back an exact central route.
    pub async fn commit_route(&self, request: CommitRouteRequest) -> EdgeResult<RouteReply> {
        self.call(|reply| ActorCommand::CommitRoute(request, reply))
            .await
    }

    /// Resume a Go-committed exact route.
    pub async fn resume_route(&self, request: ResumeRouteRequest) -> EdgeResult<RouteReply> {
        self.call(|reply| ActorCommand::ResumeRoute(request, reply))
            .await
    }

    /// Replace the complete Go-owned canonical rule observation set.
    pub async fn configure_rule_observations(
        &self,
        request: ConfigureRuleObservationsRequest,
    ) -> EdgeResult<PublishAck> {
        self.call(|reply| ActorCommand::ConfigureRuleObservations(request, reply))
            .await
    }

    /// Stop this actor task.
    pub async fn shutdown(&self) {
        let (reply, receive) = oneshot::channel();
        if self
            .commands
            .send(ActorCommand::Shutdown(reply))
            .await
            .is_ok()
        {
            let _ = receive.await;
        }
    }

    async fn call<T>(
        &self,
        build: impl FnOnce(oneshot::Sender<EdgeResult<T>>) -> ActorCommand,
    ) -> EdgeResult<T> {
        let (reply, receive) = oneshot::channel();
        match self.commands.try_send(build(reply)) {
            Ok(()) => {}
            Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                return Err(EdgeError::exhausted(
                    "ACTOR_QUEUE_FULL",
                    "target actor command queue reached its configured admission bound",
                ));
            }
            Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                return Err(EdgeError::ActorUnavailable(self.target_id.clone()));
            }
        }
        receive
            .await
            .map_err(|_| EdgeError::ActorUnavailable(self.target_id.clone()))?
    }
}

#[derive(Debug)]
enum ActorCommand {
    Renew(RenewTargetRequest, oneshot::Sender<EdgeResult<TargetReply>>),
    Revoke(
        RevokeTargetRequest,
        oneshot::Sender<EdgeResult<TargetReply>>,
    ),
    Preflight(
        EffectIntent,
        oneshot::Sender<EdgeResult<PreflightEffectReply>>,
    ),
    Execute(
        EffectIntent,
        String,
        oneshot::Sender<EdgeResult<EffectResult>>,
    ),
    AcknowledgeEffect(
        AcknowledgeEffectRequest,
        oneshot::Sender<EdgeResult<PublishAck>>,
    ),
    PrepareRoute(PrepareRouteRequest, oneshot::Sender<EdgeResult<RouteReply>>),
    CommitRoute(CommitRouteRequest, oneshot::Sender<EdgeResult<RouteReply>>),
    ResumeRoute(ResumeRouteRequest, oneshot::Sender<EdgeResult<RouteReply>>),
    ConfigureRuleObservations(
        ConfigureRuleObservationsRequest,
        oneshot::Sender<EdgeResult<PublishAck>>,
    ),
    Shutdown(oneshot::Sender<()>),
}

#[derive(Clone, Debug)]
struct PreflightRecord {
    effect_digest: String,
    plan: CompiledEffectPlan,
    expires: Instant,
}

#[derive(Clone, Debug)]
struct PendingEffectAck {
    operation_id: String,
    effect_intent_id: String,
    result_digest: String,
    journal_sequence: u64,
    result: EffectResult,
}

#[derive(Clone, Debug)]
struct RecoveredEffect {
    intent: EffectIntent,
    plan: CompiledPlan,
    last_status: Option<EffectStatus>,
}

#[derive(Clone, Debug)]
struct SourceRecovery {
    operation_wal_sequence: u64,
    frozen_bank: u32,
    new_active_bank: u32,
    previous_epoch: u64,
    new_epoch: u64,
    source_sequence: u64,
    counter_packets_before: u64,
    counter_bytes_before: u64,
    source_runtime_epoch: String,
    new_active_started_at_unix_ms: i64,
    frozen: bool,
    snapshot: Option<TelemetrySnapshot>,
    snapshot_wal_sequence: u64,
    cleared: bool,
}

#[derive(Clone, Debug)]
struct ObservedRule {
    entity: Entity,
    target_id: String,
    fence: Fence,
    effect_intent_id: String,
    operation_id: String,
    rule_id: String,
    canonical_entry_digest: String,
    entity_id: String,
    match_priority_action_digest: String,
    table_id: u32,
    direct_counter_id: u32,
    bank: u32,
    expires_at_unix_ms: Option<i64>,
}

fn applied_readback_entries(plan: &CompiledPlan) -> Vec<AppliedRuleReadback> {
    let mut entries = plan
        .contract
        .entries
        .iter()
        .map(|entry| AppliedRuleReadback {
            entity_id: entry.entity_id.clone(),
            rule_id: entry.logical_rule_id.clone(),
            canonical_entry_digest: entry.canonical_entry_digest.clone(),
            match_priority_action_digest: entry.match_priority_action_digest.clone(),
            table_id: entry.table_id,
            direct_counter_id: entry.direct_counter_id,
            bank: entry.bank,
        })
        .collect::<Vec<_>>();
    entries.sort_by(|left, right| {
        left.entity_id
            .cmp(&right.entity_id)
            .then(left.rule_id.cmp(&right.rule_id))
            .then(
                left.canonical_entry_digest
                    .cmp(&right.canonical_entry_digest),
            )
    });
    entries
}

fn readback_manifest_digest(plan: &CompiledPlan) -> String {
    let entries = applied_readback_entries(plan);
    #[derive(serde::Serialize)]
    struct ManifestEntry<'a> {
        entity_id: &'a str,
        rule_id: &'a str,
        canonical_entry_digest: &'a str,
        match_priority_action_digest: &'a str,
        table_id: u32,
        direct_counter_id: u32,
        bank: u32,
    }
    let manifest = entries
        .iter()
        .map(|entry| ManifestEntry {
            entity_id: &entry.entity_id,
            rule_id: &entry.rule_id,
            canonical_entry_digest: &entry.canonical_entry_digest,
            match_priority_action_digest: &entry.match_priority_action_digest,
            table_id: entry.table_id,
            direct_counter_id: entry.direct_counter_id,
            bank: entry.bank,
        })
        .collect::<Vec<_>>();
    let canonical = serde_json::to_vec(&manifest).unwrap_or_default();
    digest::sha256(&canonical)
}

#[derive(Debug)]
struct TargetActor {
    config: EdgeConfig,
    assignment: TargetAssignment,
    lease_deadline: Instant,
    state: ActorState,
    reason_code: String,
    session: Option<P4Session>,
    reconnect_after: Instant,
    source_wal: DurableWal,
    input_wal: DurableWal,
    result_wal: DurableWal,
    journal: DurableWal,
    route_journal: DurableWal,
    windows: WindowEngine,
    router: InferenceRouter,
    sink: ControlSink,
    commands: mpsc::Receiver<ActorCommand>,
    status: watch::Sender<TargetStatus>,
    preflights: BTreeMap<String, PreflightRecord>,
    pending_effect_ack: Option<PendingEffectAck>,
    recovered_effect: Option<RecoveredEffect>,
    source_recovery: Option<SourceRecovery>,
    known_input_ids: BTreeSet<String>,
    observed_rules: Vec<ObservedRule>,
    expired_overlay_rules: BTreeSet<String>,
    observation_backlog: VecDeque<RuleObservationBatch>,
    source_sequence: u64,
    active_bank_started_ms: Option<i64>,
    telemetry_due: Instant,
    observation_due: Instant,
    status_due: Instant,
    last_successful_read_ms: i64,
    sample_sequence: u64,
    pending_digest_acks: VecDeque<(u64, SupplementalHint)>,
    route_recovery: Option<RouteWalRecord>,
    committed_resume: Option<ResumeRouteRequest>,
    route_reconnect_after: Instant,
    inference_retry_after: Instant,
    control_retry_after: Instant,
    wal_retention_hold: bool,
    digest_hints_since_snapshot: u64,
    packet_hints_since_snapshot: u64,
    hint_drops_since_snapshot: u64,
}

/// Start a target actor and attempt its initial sole P4Runtime session.
pub async fn spawn(assignment: TargetAssignment, config: EdgeConfig) -> EdgeResult<ActorHandle> {
    let target_id = assignment.target_id.clone();
    let target_dir = config.data_dir.join("targets").join(&target_id);
    ensure_target_dir(&target_dir)?;
    let limits = &config.limits;
    let source_wal = DurableWal::open(
        &target_dir.join("source"),
        WalKind::Source,
        WalLimits {
            max_bytes: limits.source_wal_bytes,
            max_records: limits.source_wal_records,
            segment_bytes: limits.wal_segment_bytes,
            max_record_bytes: limits.wal_record_bytes,
            max_age_seconds: limits.wal_max_age_seconds,
        },
    )?;
    let input_wal = DurableWal::open(
        &target_dir.join("input"),
        WalKind::InferenceInput,
        WalLimits {
            max_bytes: limits.input_wal_bytes,
            max_records: limits.input_wal_records,
            segment_bytes: limits.wal_segment_bytes,
            max_record_bytes: limits.wal_record_bytes,
            max_age_seconds: limits.wal_max_age_seconds,
        },
    )?;
    let result_wal = DurableWal::open(
        &target_dir.join("result"),
        WalKind::InferenceResult,
        WalLimits {
            max_bytes: limits.result_wal_bytes,
            max_records: limits.result_wal_records,
            segment_bytes: limits.wal_segment_bytes,
            max_record_bytes: limits.wal_record_bytes,
            max_age_seconds: limits.wal_max_age_seconds,
        },
    )?;
    let journal = DurableWal::open(
        &target_dir.join("effect-journal"),
        WalKind::EffectJournal,
        WalLimits {
            max_bytes: limits.journal_bytes,
            max_records: limits.journal_records,
            segment_bytes: limits.wal_segment_bytes,
            max_record_bytes: limits.wal_record_bytes,
            max_age_seconds: limits.wal_max_age_seconds,
        },
    )?;
    let route_journal = DurableWal::open(
        &target_dir.join("route-journal"),
        WalKind::RouteJournal,
        WalLimits {
            max_bytes: limits.route_journal_bytes,
            max_records: limits.route_journal_records,
            segment_bytes: limits.wal_segment_bytes,
            max_record_bytes: limits.wal_record_bytes,
            max_age_seconds: limits.wal_max_age_seconds,
        },
    )?;
    let wal_retention_reason = [
        &source_wal,
        &input_wal,
        &result_wal,
        &journal,
        &route_journal,
    ]
    .into_iter()
    .find_map(|wal| {
        wal.check_retention()
            .err()
            .map(|error| error.reason_code().to_owned())
    });
    let wal_retention_hold = wal_retention_reason.is_some();
    let (commands_tx, commands_rx) = mpsc::channel(limits.actor_high_queue);
    let initial_status = empty_status(&assignment, ActorState::Connecting, "CONNECTING");
    let (status_tx, status_rx) = watch::channel(initial_status);
    let lease_deadline = lease_deadline(&assignment)?;
    let (session, state, reason_code) = if let Some(reason) = wal_retention_reason {
        (None, ActorState::Hold, reason)
    } else {
        match P4Session::connect(
            &assignment,
            config.deployment_tier,
            &config.endpoint_policy,
            &config.client_identities,
            limits,
        )
        .await
        {
            Ok(session) => (Some(session), ActorState::Primary, "PRIMARY".to_owned()),
            Err(error) => {
                tracing::warn!(
                    target_id = %assignment.target_id,
                    error = %error,
                    "initial P4 session connect failed"
                );
                (
                    None,
                    ActorState::Hold,
                    format!("{}:initial-connect", error.reason_code()),
                )
            }
        }
    };
    let now = Instant::now();
    let mut actor = TargetActor {
        windows: WindowEngine::new(
            limits.window_duration_ms,
            limits.allowed_lateness_ms,
            limits.idle_source_timeout_ms,
            limits.max_open_windows,
            config.feature_profile_digest.clone(),
        )?,
        router: InferenceRouter::new(
            config.deployment_tier,
            config.endpoint_policy.clone(),
            limits.clone(),
            config.client_identities.clone(),
        ),
        sink: ControlSink::new(
            config.control_sink.clone(),
            config.deployment_tier,
            config.endpoint_policy.clone(),
            limits.clone(),
        ),
        config,
        assignment,
        lease_deadline,
        state,
        reason_code,
        session,
        reconnect_after: now + Duration::from_secs(1),
        source_wal,
        input_wal,
        result_wal,
        journal,
        route_journal,
        commands: commands_rx,
        status: status_tx,
        preflights: BTreeMap::new(),
        pending_effect_ack: None,
        recovered_effect: None,
        source_recovery: None,
        known_input_ids: BTreeSet::new(),
        observed_rules: Vec::new(),
        expired_overlay_rules: BTreeSet::new(),
        observation_backlog: VecDeque::new(),
        source_sequence: 0,
        active_bank_started_ms: None,
        telemetry_due: now,
        observation_due: now + Duration::from_secs(1),
        status_due: now,
        last_successful_read_ms: 0,
        sample_sequence: 0,
        pending_digest_acks: VecDeque::new(),
        route_recovery: None,
        committed_resume: None,
        route_reconnect_after: now,
        inference_retry_after: now,
        control_retry_after: now,
        wal_retention_hold,
        digest_hints_since_snapshot: 0,
        packet_hints_since_snapshot: 0,
        hint_drops_since_snapshot: 0,
    };
    if !actor.wal_retention_hold {
        actor.recover_effect_journal()?;
        actor.recover_source_state()?;
        actor.recover_route_journal()?;
    }
    if actor.session.is_some()
        && actor.recovered_effect.is_some()
        && let Err(error) = actor.reconcile_recovered_effect().await
    {
        actor.state = ActorState::Hold;
        actor.reason_code = format!("{}:effect-recovery", error.reason_code());
    }
    if actor.session.is_some()
        && actor.source_recovery.is_some()
        && let Err(error) = actor.finish_source_recovery().await
    {
        actor.state = ActorState::Hold;
        actor.reason_code = format!("{}:source-recovery", error.reason_code());
    }
    if actor.route_recovery.is_some()
        && let Err(error) = actor.finish_route_recovery().await
    {
        actor.reason_code = format!("{}:route-recovery", error.reason_code());
        actor.route_reconnect_after = Instant::now() + Duration::from_secs(1);
    }
    // Publish the fully constructed/recovered state before Supervisor returns
    // AssignTarget; otherwise the public reply can race with the actor task and
    // incorrectly expose the channel's provisional CONNECTING value.
    actor.update_status();
    let handle = ActorHandle {
        target_id,
        commands: commands_tx,
        status: status_rx,
    };
    tokio::spawn(actor.run());
    Ok(handle)
}

impl TargetActor {
    async fn run(mut self) {
        let mut maintenance = tokio::time::interval(Duration::from_millis(20));
        maintenance.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        self.update_status();
        loop {
            tokio::select! {
                biased;
                command = self.commands.recv() => {
                    let Some(command) = command else { return };
                    if self.handle_command(command).await {
                        return;
                    }
                }
                _ = maintenance.tick() => {
                    self.maintain().await;
                }
            }
        }
    }

    async fn handle_command(&mut self, command: ActorCommand) -> bool {
        match command {
            ActorCommand::Renew(request, reply) => {
                let _ = reply.send(self.renew(request));
            }
            ActorCommand::Revoke(request, reply) => {
                let _ = reply.send(self.revoke(request));
            }
            ActorCommand::Preflight(intent, reply) => {
                let result = self.preflight(intent).await;
                let _ = reply.send(result);
            }
            ActorCommand::Execute(intent, token, reply) => {
                let result = self.execute(intent, &token).await;
                let _ = reply.send(result);
            }
            ActorCommand::AcknowledgeEffect(request, reply) => {
                let result = self.acknowledge_effect(request);
                let _ = reply.send(result);
            }
            ActorCommand::PrepareRoute(request, reply) => {
                let result = self.prepare_route(request);
                let _ = reply.send(result);
            }
            ActorCommand::CommitRoute(request, reply) => {
                let result = self.commit_route(request).await;
                let _ = reply.send(result);
            }
            ActorCommand::ResumeRoute(request, reply) => {
                let result = self.resume_route(request);
                let _ = reply.send(result);
            }
            ActorCommand::ConfigureRuleObservations(request, reply) => {
                let result = self.configure_rule_observations(request);
                let _ = reply.send(result);
            }
            ActorCommand::Shutdown(reply) => {
                self.state = ActorState::Revoked;
                self.session = None;
                self.update_status();
                let _ = reply.send(());
                return true;
            }
        }
        self.update_status();
        false
    }

    async fn maintain(&mut self) {
        let now = Instant::now();
        if !self.wal_retention_hold
            && let Err(error) = self.verify_wal_retention()
        {
            self.wal_retention_hold = true;
            self.state = ActorState::Hold;
            self.reason_code = error.reason_code().into();
            self.preflights.clear();
            self.session = None;
        }
        if self.wal_retention_hold {
            if now >= self.status_due {
                self.status_due = now + Duration::from_secs(1);
                self.update_status();
                let _ = self.publish_status().await;
            }
            return;
        }
        if now >= self.lease_deadline && self.state != ActorState::Revoked {
            self.state = ActorState::ReadOnly;
            self.reason_code = "LEASE_EXPIRED".into();
            self.preflights.clear();
        }
        self.drain_stream_events().await;
        if self.session.is_none()
            && now >= self.reconnect_after
            && self.lease_valid()
            && self.state != ActorState::Revoked
        {
            self.reconnect_after = now + Duration::from_secs(1);
            match P4Session::connect(
                &self.assignment,
                self.config.deployment_tier,
                &self.config.endpoint_policy,
                &self.config.client_identities,
                &self.config.limits,
            )
            .await
            {
                Ok(session) => {
                    self.session = Some(session);
                    self.state = ActorState::Primary;
                    self.reason_code = "PRIMARY".into();
                }
                Err(error) => {
                    self.state = ActorState::Hold;
                    self.reason_code = format!("{}:reconnect", error.reason_code());
                }
            }
        }
        if self.session.is_some()
            && self.recovered_effect.is_some()
            && let Err(error) = self.reconcile_recovered_effect().await
        {
            self.state = ActorState::Hold;
            self.reason_code = format!("{}:effect-recovery", error.reason_code());
        }
        if self.session.is_some() && self.source_recovery.is_some() {
            match self.finish_source_recovery().await {
                Ok(()) => {
                    if self.state == ActorState::Hold
                        && self.reason_code.ends_with(":source-recovery")
                    {
                        self.state = ActorState::Primary;
                        self.reason_code = "PRIMARY".into();
                    }
                }
                Err(error) => {
                    tracing::warn!(
                        target_id = %self.assignment.target_id,
                        error = %error,
                        "durable telemetry source recovery failed"
                    );
                    self.state = ActorState::Hold;
                    self.reason_code = format!("{}:source-recovery", error.reason_code());
                }
            }
        }
        if self.route_recovery.is_some() && now >= self.route_reconnect_after {
            self.route_reconnect_after = now + Duration::from_secs(1);
            if let Err(error) = self.finish_route_recovery().await {
                tracing::warn!(
                    target_id = %self.assignment.target_id,
                    error = %error,
                    "durable route recovery failed"
                );
                self.reason_code = format!("{}:route-recovery", error.reason_code());
            }
        }
        self.preflights
            .retain(|_, record| record.expires > Instant::now());
        if let Err(error) = self.flush_digest_acks().await {
            self.reason_code = error.reason_code().into();
        }
        let mut result_delivery_blocked = false;
        if now >= self.control_retry_after
            && let Err(error) = self.process_pending_results().await
        {
            tracing::warn!(
                target_id = %self.assignment.target_id,
                error = %error,
                "canonical result delivery failed"
            );
            // Result remains durable and retryable. A whole failed RPC budget
            // opens a bounded outer cooldown instead of retrying every 20 ms.
            self.reason_code = error.reason_code().into();
            self.control_retry_after =
                now + Duration::from_millis(self.config.limits.control_deadline_ms);
            result_delivery_blocked = true;
        }
        if !result_delivery_blocked
            && now >= self.inference_retry_after
            && let Err(error) = self.process_pending_inputs().await
        {
            // Same-generation attempts are bounded inside InferenceRouter;
            // durable re-admission is no faster than one complete RPC budget.
            self.reason_code = error.reason_code().into();
            self.inference_retry_after =
                now + Duration::from_millis(self.config.limits.inference_deadline_ms);
        }
        let idle_advance = unix_ms()
            .and_then(|now_ms| self.windows.tick(now_ms))
            .and_then(|advance| self.admit_window_advance(advance));
        if let Err(error) = idle_advance {
            self.reason_code = error.reason_code().into();
        }
        if now >= self.telemetry_due {
            self.telemetry_due =
                now + Duration::from_millis(self.config.limits.telemetry_poll_interval_ms);
            if let Err(error) = self.poll_telemetry().await {
                tracing::warn!(
                    target_id = %self.assignment.target_id,
                    error = %error,
                    "telemetry poll failed"
                );
                self.reason_code = error.reason_code().into();
            }
        }
        if now >= self.observation_due {
            self.observation_due =
                now + Duration::from_millis(self.config.limits.observation_interval_ms);
            if let Err(error) = self.sweep_rule_observations().await {
                self.reason_code = error.reason_code().into();
            }
        }
        self.flush_observation_backlog().await;
        if now >= self.status_due {
            self.status_due = now + Duration::from_secs(1);
            self.update_status();
            let _ = self.publish_status().await;
        }
    }

    async fn drain_stream_events(&mut self) {
        for _ in 0..256 {
            let event = self.session.as_mut().and_then(P4Session::try_stream_event);
            let Some(event) = event else { break };
            match event {
                P4StreamEvent::Arbitration(update) => {
                    if !update
                        .status
                        .as_ref()
                        .is_some_and(|status| status.code == 0)
                    {
                        self.session = None;
                        self.state = ActorState::ReadOnly;
                        self.reason_code = "NOT_PRIMARY".into();
                        self.reconnect_after = Instant::now() + Duration::from_secs(1);
                        break;
                    }
                }
                P4StreamEvent::Digest(hint) => {
                    if let Err(error) = self.persist_digest_hint(hint).await {
                        self.reason_code = error.reason_code().into();
                        break;
                    }
                }
                P4StreamEvent::Packet(packet) => {
                    if let Err(error) = self.persist_packet_hint(&packet) {
                        self.reason_code = error.reason_code().into();
                        break;
                    }
                }
                P4StreamEvent::Error(_) => {
                    self.session = None;
                    self.state = ActorState::Hold;
                    self.reason_code = "P4_STREAM_ERROR".into();
                    self.reconnect_after = Instant::now() + Duration::from_secs(1);
                    break;
                }
                P4StreamEvent::HintDropped { kind } => {
                    if let Err(error) = self.persist_hint_gap(kind) {
                        self.reason_code = error.reason_code().into();
                        break;
                    }
                    self.reason_code = "SUPPLEMENTAL_HINT_DROPPED".into();
                }
                P4StreamEvent::Closed(_) => {
                    self.session = None;
                    self.state = ActorState::Hold;
                    self.reason_code = "P4_STREAM_CLOSED".into();
                    self.reconnect_after = Instant::now() + Duration::from_secs(1);
                    break;
                }
            }
        }
    }

    fn renew(&mut self, request: RenewTargetRequest) -> EdgeResult<TargetReply> {
        if request.schema_version != "target-lease-renew/v1" {
            return Err(EdgeError::UnknownMajor(request.schema_version));
        }
        if self.wal_retention_hold {
            return Err(EdgeError::precondition(
                "WAL_RETENTION_EXPIRED",
                "target remains held until stale WAL is explicitly recovered",
            ));
        }
        self.verify_target_fence(
            &request.target_id,
            request.fence.as_ref(),
            Some(&request.lease_id),
        )?;
        let now_ms = unix_ms()?;
        if request.expires_at_unix_ms <= now_ms {
            return Err(EdgeError::precondition(
                "LEASE_EXPIRED",
                "renewal expiry is not in the future",
            ));
        }
        let remaining = u64::try_from(request.expires_at_unix_ms - now_ms)
            .map_err(|_| EdgeError::invalid("expires_at_unix_ms", "negative duration"))?;
        if remaining > 300_000 {
            return Err(EdgeError::invalid(
                "expires_at_unix_ms",
                "renewal exceeds 300 second lease bound",
            ));
        }
        self.assignment.expires_at_unix_ms = request.expires_at_unix_ms;
        self.lease_deadline = Instant::now() + Duration::from_millis(remaining);
        if self.session.as_ref().is_some_and(P4Session::is_primary) {
            self.state = ActorState::Primary;
            self.reason_code = "PRIMARY".into();
        }
        Ok(self.target_reply(&request.trace_id))
    }

    fn revoke(&mut self, request: RevokeTargetRequest) -> EdgeResult<TargetReply> {
        if request.schema_version != "target-revoke/v1" {
            return Err(EdgeError::UnknownMajor(request.schema_version));
        }
        self.verify_target_fence(
            &request.target_id,
            request.fence.as_ref(),
            Some(&request.lease_id),
        )?;
        self.lease_deadline = Instant::now();
        self.state = ActorState::Revoked;
        self.reason_code = request.reason_code;
        self.preflights.clear();
        // Closing StreamChannel surrenders mastership and is stricter than
        // retaining read-only connectivity after revocation.
        self.session = None;
        Ok(self.target_reply(&request.trace_id))
    }

    async fn preflight(&mut self, intent: EffectIntent) -> EdgeResult<PreflightEffectReply> {
        self.require_write_ready()?;
        self.verify_intent_fence(&intent)?;
        if self.pending_effect_ack.is_some() {
            return Err(EdgeError::precondition(
                "EFFECT_ACK_PENDING",
                "prior effect awaits PostgreSQL CAS acknowledgement",
            ));
        }
        let expected = self
            .assignment
            .expected_pipeline
            .as_ref()
            .ok_or_else(|| EdgeError::precondition("PIPELINE_DRIFT", "expected pipeline missing"))?
            .clone();
        let session = self
            .session
            .as_mut()
            .ok_or_else(|| EdgeError::precondition("NOT_PRIMARY", "P4 session missing"))?;
        session.reverify_pipeline(&expected).await?;
        let selector = session
            .read(vec![firewall::policy_selector_query()])
            .await?;
        self.last_successful_read_ms = unix_ms()?;
        let active_bank = firewall::active_policy_bank(&selector)?;
        let plan = firewall::compile(
            &intent,
            active_bank,
            self.config.limits.baseline_rules,
            self.config.limits.overlay_rules,
        )?;
        if self.preflights.len() >= 128 {
            return Err(EdgeError::exhausted(
                "PREFLIGHT_LIMIT_EXCEEDED",
                "target preflight token limit reached",
            ));
        }
        let token = uuid::Uuid::new_v4().to_string();
        let validity = Duration::from_millis(self.config.limits.preflight_validity_ms);
        let expires_at = unix_ms()?.saturating_add(
            i64::try_from(self.config.limits.preflight_validity_ms)
                .map_err(|_| EdgeError::invalid("preflight_validity_ms", "exceeds i64"))?,
        );
        self.preflights.insert(
            token.clone(),
            PreflightRecord {
                effect_digest: intent.effect_digest.clone(),
                plan: plan.contract.clone(),
                expires: Instant::now() + validity,
            },
        );
        Ok(PreflightEffectReply {
            target_id: intent.target_id,
            effect_intent_id: intent.effect_intent_id,
            preflight_token: token,
            plan_digest: plan.contract.plan_digest,
            physical_entries: u32::try_from(plan.entries.len()).map_err(|_| {
                EdgeError::exhausted("CAPACITY_EXCEEDED", "physical entry count exceeds u32")
            })?,
            expires_at_unix_ms: expires_at,
            result: "accepted".into(),
            reason_code: "PREFLIGHT_ACCEPTED".into(),
            trace_id: intent.trace_id,
            admission: AdmissionResult::Accepted as i32,
        })
    }

    async fn execute(&mut self, intent: EffectIntent, token: &str) -> EdgeResult<EffectResult> {
        self.require_write_ready()?;
        self.verify_intent_fence(&intent)?;
        if let Some(pending) = &self.pending_effect_ack {
            if pending.operation_id == intent.operation_id
                && pending.effect_intent_id == intent.effect_intent_id
            {
                return Ok(pending.result.clone());
            }
            return Err(EdgeError::precondition(
                "EFFECT_ACK_PENDING",
                "prior effect awaits PostgreSQL CAS acknowledgement",
            ));
        }
        let preflight = self.preflights.remove(token).ok_or_else(|| {
            EdgeError::precondition("PREFLIGHT_REPLAY", "token absent or consumed")
        })?;
        if preflight.expires <= Instant::now() {
            return Err(EdgeError::precondition(
                "PREFLIGHT_EXPIRED",
                "preflight token expired",
            ));
        }
        if preflight.effect_digest != intent.effect_digest {
            return Err(EdgeError::precondition(
                "EFFECT_DIGEST_CONFLICT",
                "preflight token belongs to a different effect digest",
            ));
        }
        let plan = CompiledPlan::from_contract(preflight.plan)?;
        let started = EffectJournalRecord {
            schema_version: "edge-effect-journal/v1".into(),
            stage: JournalStage::WriteStarted as i32,
            intent: Some(intent.clone()),
            plan: Some(plan.contract.clone()),
            result: None,
            recorded_at_unix_ms: unix_ms()?,
            reason_code: "WRITE_STARTED".into(),
        };
        self.journal.append_message(&started)?;
        let outcome = self.apply_plan(&intent, &plan).await;
        let result = match outcome {
            Ok((readback_digest, observed_entries)) => EffectResult {
                schema_version: "edge-effect-result/v1".into(),
                target_id: intent.target_id.clone(),
                effect_intent_id: intent.effect_intent_id.clone(),
                operation_id: intent.operation_id.clone(),
                fence: intent.fence.clone(),
                status: EffectStatus::Applied as i32,
                plan_digest: plan.contract.plan_digest.clone(),
                readback_digest,
                expected_entries: plan.contract.entries.len() as u32,
                observed_entries,
                mismatched_entries: 0,
                active_bank: if intent.kind
                    == crate::contract::edge::EffectKind::BaselineActivate as i32
                {
                    plan.contract.inactive_bank
                } else {
                    plan.contract.active_bank
                },
                reason_code: "EXACT_READBACK".into(),
                trace_id: intent.trace_id.clone(),
                applied_entries: applied_readback_entries(&plan),
                readback_manifest_digest: readback_manifest_digest(&plan),
                bounded_capture: None,
            },
            Err(EdgeError::UnknownOutcome(message)) => EffectResult {
                schema_version: "edge-effect-result/v1".into(),
                target_id: intent.target_id.clone(),
                effect_intent_id: intent.effect_intent_id.clone(),
                operation_id: intent.operation_id.clone(),
                fence: intent.fence.clone(),
                status: EffectStatus::Unknown as i32,
                plan_digest: plan.contract.plan_digest.clone(),
                reason_code: format!("WRITE_OUTCOME_UNCONFIRMED:{message}"),
                trace_id: intent.trace_id.clone(),
                ..EffectResult::default()
            },
            Err(error) => EffectResult {
                schema_version: "edge-effect-result/v1".into(),
                target_id: intent.target_id.clone(),
                effect_intent_id: intent.effect_intent_id.clone(),
                operation_id: intent.operation_id.clone(),
                fence: intent.fence.clone(),
                status: EffectStatus::Hold as i32,
                plan_digest: plan.contract.plan_digest.clone(),
                reason_code: error.reason_code().into(),
                trace_id: intent.trace_id.clone(),
                ..EffectResult::default()
            },
        };
        let stage = match EffectStatus::try_from(result.status) {
            Ok(EffectStatus::Applied) => JournalStage::SelectorExact,
            Ok(EffectStatus::Unknown) => JournalStage::Unknown,
            _ => JournalStage::Hold,
        };
        let durable_result = EffectJournalRecord {
            schema_version: "edge-effect-journal/v1".into(),
            stage: stage as i32,
            intent: Some(intent.clone()),
            plan: Some(plan.contract.clone()),
            result: Some(result.clone()),
            recorded_at_unix_ms: unix_ms()?,
            reason_code: result.reason_code.clone(),
        };
        let journal_sequence = self.journal.append_message(&durable_result)?;
        let result_digest = digest::message_sha256(&result);
        self.pending_effect_ack = Some(PendingEffectAck {
            operation_id: intent.operation_id,
            effect_intent_id: intent.effect_intent_id,
            result_digest,
            journal_sequence,
            result: result.clone(),
        });
        Ok(result)
    }

    fn acknowledge_effect(&mut self, request: AcknowledgeEffectRequest) -> EdgeResult<PublishAck> {
        if request.schema_version != "effect-canonical-ack/v1" {
            return Err(EdgeError::UnknownMajor(request.schema_version));
        }
        if request.target_id != self.assignment.target_id {
            return Err(EdgeError::precondition(
                "TARGET_FENCE_MISMATCH",
                "effect ACK target differs from actor target",
            ));
        }
        digest::validate_sha256(&request.result_digest, "result_digest")?;
        digest::validate_identity(
            &request.canonical_effect_reference,
            "canonical_effect_reference",
        )?;
        if request.committed_at_unix_ms <= 0 {
            return Err(EdgeError::invalid(
                "committed_at_unix_ms",
                "must be positive",
            ));
        }
        let pending = self.pending_effect_ack.as_ref().ok_or_else(|| {
            EdgeError::precondition("CANONICAL_ACK_MISMATCH", "no pending effect result")
        })?;
        if pending.operation_id != request.operation_id
            || pending.effect_intent_id != request.effect_intent_id
            || pending.result_digest != request.result_digest
            || pending.result.fence.as_ref() != request.fence.as_ref()
        {
            return Err(EdgeError::precondition(
                "CANONICAL_ACK_MISMATCH",
                "effect ACK identity or result digest mismatch",
            ));
        }
        if pending.result.status == EffectStatus::Unknown as i32 || self.recovered_effect.is_some()
        {
            return Err(EdgeError::precondition(
                "EFFECT_RECONCILE_REQUIRED",
                "unknown effect remains journaled until exact readback converges",
            ));
        }
        let journal_sequence = pending.journal_sequence;
        self.journal.checkpoint(journal_sequence)?;
        self.pending_effect_ack = None;
        Ok(PublishAck {
            status: "checkpointed".into(),
            identity: request.operation_id,
            digest: request.result_digest,
            reason_code: "POSTGRESQL_CAS_CONFIRMED".into(),
            status_code: PublishStatus::Checkpointed as i32,
        })
    }

    fn prepare_route(&mut self, request: PrepareRouteRequest) -> EdgeResult<RouteReply> {
        if request.schema_version != "inference-route-prepare/v1" {
            return Err(EdgeError::UnknownMajor(request.schema_version));
        }
        if request.shard_id != self.assignment.target_id {
            return Err(EdgeError::precondition(
                "ROUTE_FENCE_MISMATCH",
                "route shard must equal target-scoped actor",
            ));
        }
        if self.route_recovery.is_some() {
            return Err(EdgeError::precondition(
                "ROUTE_RECOVERY_PENDING",
                "durable route recovery must converge before a new route operation",
            ));
        }
        let current_route = self.router.route().cloned();
        self.router.prepare(
            &request.current_model_control_incarnation_id,
            request.current_route_epoch,
            request.proposed_route_epoch,
        )?;
        self.append_route_stage(
            RouteWalStage::Prepared,
            current_route,
            request.proposed_route_epoch,
            "ROUTE_PREPARED",
            &request.trace_id,
            self.committed_resume.clone(),
        )?;
        self.route_reply(
            &request.shard_id,
            request.proposed_route_epoch,
            &request.trace_id,
        )
    }

    async fn commit_route(&mut self, request: CommitRouteRequest) -> EdgeResult<RouteReply> {
        let route = request
            .route
            .ok_or_else(|| EdgeError::invalid("route", "route is required"))?;
        if route.shard_id != self.assignment.target_id {
            return Err(EdgeError::precondition(
                "ROUTE_FENCE_MISMATCH",
                "route shard must equal target-scoped actor",
            ));
        }
        if self.route_recovery.is_some() {
            return Err(EdgeError::precondition(
                "ROUTE_RECOVERY_PENDING",
                "durable route recovery must converge before route commit",
            ));
        }
        if !self.router.rpc_drained()
            || self.result_wal.stats().last_sequence > self.result_wal.stats().checkpoint_sequence
            || self.has_route_bound_pending_inputs()?
        {
            return Err(EdgeError::precondition(
                "ROUTE_DRAINING",
                "in-flight RPC, route-bound input WAL, or result WAL is not drained",
            ));
        }
        if let (Some(p4_tls), Some(route_tls)) =
            (self.assignment.p4runtime_tls.as_ref(), route.tls.as_ref())
            && (p4_tls.identity_ref == route_tls.identity_ref
                || route_tls.identity_ref == self.config.control_sink.tls.identity_ref)
        {
            return Err(EdgeError::precondition(
                "TLS_IDENTITY_REUSE",
                "P4, inference, and control identities must be distinct",
            ));
        }
        let shard = route.shard_id.clone();
        let epoch = route.route_epoch;
        if route.feature_contract_digest != self.config.feature_profile_digest {
            return Err(EdgeError::precondition(
                "FEATURE_CONTRACT_MISMATCH",
                "route feature digest differs from the Edge frozen feature profile",
            ));
        }
        self.router.commit(route.clone()).await?;
        self.append_route_stage(
            RouteWalStage::BindingReady,
            Some(route),
            epoch,
            "BINDING_READBACK_EXACT",
            &request.trace_id,
            None,
        )?;
        self.committed_resume = None;
        self.route_reply(&shard, epoch, &request.trace_id)
    }

    fn resume_route(&mut self, request: ResumeRouteRequest) -> EdgeResult<RouteReply> {
        if request.schema_version != "inference-committed-binding/v1" {
            return Err(EdgeError::UnknownMajor(request.schema_version));
        }
        if request.shard_id != self.assignment.target_id {
            return Err(EdgeError::precondition(
                "ROUTE_FENCE_MISMATCH",
                "route shard must equal target-scoped actor",
            ));
        }
        if self.route_recovery.is_some() {
            return Err(EdgeError::precondition(
                "ROUTE_RECOVERY_PENDING",
                "durable route recovery must converge before route resume",
            ));
        }
        if self.router.state() == RouteState::Active {
            let committed = self.committed_resume.as_ref().ok_or_else(|| {
                EdgeError::precondition(
                    "ROUTE_RECOVERY_MISSING",
                    "active route lacks its durable committed-binding handshake",
                )
            })?;
            if !same_committed_binding_request(committed, &request) {
                return Err(EdgeError::precondition(
                    "ROUTE_FENCE_MISMATCH",
                    "duplicate committed-binding operation changed its durable request tuple",
                ));
            }
            self.router.resume(&request)?;
            return self.route_reply(&request.shard_id, request.route_epoch, &request.trace_id);
        }
        self.router.validate_resume(&request)?;
        if request.deadline_unix_ms <= unix_ms()? {
            return Err(EdgeError::Deadline(
                "committed binding handshake expired before durable admission".into(),
            ));
        }
        let route = self.router.route().cloned().ok_or_else(|| {
            EdgeError::precondition("ROUTE_NOT_READY", "route missing before resume")
        })?;
        self.append_route_stage(
            RouteWalStage::CanonicalActive,
            Some(route),
            request.route_epoch,
            "GO_CANONICAL_BINDING_COMMITTED",
            &request.trace_id,
            Some(request.clone()),
        )?;
        self.router.resume(&request)?;
        self.committed_resume = Some(request.clone());
        self.route_reply(&request.shard_id, request.route_epoch, &request.trace_id)
    }

    fn route_reply(&self, shard: &str, epoch: u64, trace_id: &str) -> EdgeResult<RouteReply> {
        let source = self.source_wal.stats();
        let input = self.input_wal.stats();
        let result = self.result_wal.stats();
        let runtime_epoch = self
            .assignment
            .fence
            .as_ref()
            .map_or_else(String::new, |fence| fence.actor_runtime_epoch.clone());
        Ok(RouteReply {
            shard_id: shard.into(),
            state: self.router.state().as_str().into(),
            route_epoch: epoch,
            pending_inputs: input
                .last_sequence
                .saturating_sub(input.checkpoint_sequence),
            pending_results: result
                .last_sequence
                .saturating_sub(result.checkpoint_sequence),
            reason_code: match self.router.state() {
                RouteState::Active => "ROUTE_ACTIVE",
                RouteState::Ready => "ROUTE_READY",
                RouteState::Draining => "ROUTE_DRAINING",
                RouteState::Withdrawn => "ROUTE_WITHDRAWN",
                RouteState::Hold => "ROUTE_HOLD",
            }
            .into(),
            trace_id: trace_id.into(),
            lifecycle: match self.router.state() {
                RouteState::Active => InferenceRouteLifecycle::Active,
                RouteState::Ready => InferenceRouteLifecycle::Ready,
                RouteState::Draining => InferenceRouteLifecycle::Draining,
                RouteState::Withdrawn => InferenceRouteLifecycle::Withdrawn,
                RouteState::Hold => InferenceRouteLifecycle::Hold,
            } as i32,
            actor_runtime_epoch: runtime_epoch.clone(),
            source_runtime_epoch: runtime_epoch,
            binding_digest: self
                .router
                .route()
                .map_or_else(String::new, |route| route.binding_digest.clone()),
            resume_watermark: Some(RouteResumeWatermark {
                schema_version: "edge-route-resume-watermark/v1".into(),
                source_wal_checkpoint_sequence: source.checkpoint_sequence,
                source_wal_last_sequence: source.last_sequence,
                input_wal_checkpoint_sequence: input.checkpoint_sequence,
                input_wal_last_sequence: input.last_sequence,
                result_wal_checkpoint_sequence: result.checkpoint_sequence,
                result_wal_last_sequence: result.last_sequence,
                canonical_source_sequence: self.source_sequence,
                pending_source_gap_records: self.hint_drops_since_snapshot,
            }),
        })
    }

    fn append_route_stage(
        &mut self,
        stage: RouteWalStage,
        route: Option<crate::contract::edge::InferenceRoute>,
        proposed_route_epoch: u64,
        reason_code: &str,
        trace_id: &str,
        committed_binding_handshake: Option<ResumeRouteRequest>,
    ) -> EdgeResult<()> {
        if proposed_route_epoch == 0 {
            return Err(EdgeError::invalid(
                "proposed_route_epoch",
                "route journal epoch must be positive",
            ));
        }
        let previous = self.route_journal.stats().last_sequence;
        let record = RouteWalRecord {
            schema_version: "edge-route-wal/v1".into(),
            stage: stage as i32,
            route,
            proposed_route_epoch,
            recorded_at_unix_ms: unix_ms()?,
            reason_code: reason_code.into(),
            trace_id: trace_id.into(),
            committed_binding_handshake,
        };
        self.route_journal.append_message(&record)?;
        if previous > self.route_journal.stats().checkpoint_sequence {
            self.route_journal.checkpoint(previous)?;
        }
        Ok(())
    }

    fn recover_route_journal(&mut self) -> EdgeResult<()> {
        let max_records =
            usize::try_from(self.config.limits.route_journal_records).map_err(|_| {
                EdgeError::exhausted(
                    "ROUTE_RECOVERY_LIMIT",
                    "route journal record bound exceeds usize",
                )
            })?;
        let max_bytes = usize::try_from(self.config.limits.route_journal_bytes).map_err(|_| {
            EdgeError::exhausted(
                "ROUTE_RECOVERY_LIMIT",
                "route journal byte bound exceeds usize",
            )
        })?;
        let replay = self.route_journal.replay(max_records, max_bytes)?;
        let Some(item) = replay.last() else {
            return Ok(());
        };
        let record = RouteWalRecord::decode(item.payload.as_slice())
            .map_err(|error| EdgeError::WalCorrupt(format!("decode route journal: {error}")))?;
        validate_route_wal_record(&record)?;
        self.route_recovery = Some(record);
        Ok(())
    }

    async fn finish_route_recovery(&mut self) -> EdgeResult<()> {
        let record = self.route_recovery.clone().ok_or_else(|| {
            EdgeError::precondition("ROUTE_RECOVERY_MISSING", "route record missing")
        })?;
        let stage = RouteWalStage::try_from(record.stage)
            .map_err(|_| EdgeError::WalCorrupt("unknown route WAL stage".into()))?;
        self.router.reset_for_recovery()?;
        self.committed_resume = None;
        match stage {
            RouteWalStage::Prepared => {
                if let Some(previous) = record.route {
                    self.restore_route_binding(
                        previous.clone(),
                        record.committed_binding_handshake,
                    )
                    .await?;
                    self.router.prepare(
                        &previous.model_control_incarnation_id,
                        previous.route_epoch,
                        record.proposed_route_epoch,
                    )?;
                } else {
                    self.router.prepare("", 0, record.proposed_route_epoch)?;
                }
            }
            RouteWalStage::BindingReady => {
                let route = record.route.ok_or_else(|| {
                    EdgeError::WalCorrupt("binding-ready route journal lacks route".into())
                })?;
                self.restore_route_binding(route, None).await?;
            }
            RouteWalStage::CanonicalActive => {
                let route = record.route.ok_or_else(|| {
                    EdgeError::WalCorrupt("canonical-active route journal lacks route".into())
                })?;
                let handshake = record.committed_binding_handshake.ok_or_else(|| {
                    EdgeError::WalCorrupt(
                        "canonical-active route journal lacks committed-binding handshake".into(),
                    )
                })?;
                self.restore_route_binding(route, Some(handshake)).await?;
            }
            RouteWalStage::Unspecified => {
                return Err(EdgeError::WalCorrupt("unspecified route WAL stage".into()));
            }
        }
        self.route_recovery = None;
        Ok(())
    }

    async fn restore_route_binding(
        &mut self,
        route: crate::contract::edge::InferenceRoute,
        committed_binding_handshake: Option<ResumeRouteRequest>,
    ) -> EdgeResult<()> {
        self.router.prepare("", 0, route.route_epoch)?;
        self.router.commit(route.clone()).await?;
        if let Some(handshake) = committed_binding_handshake {
            self.router.resume(&handshake)?;
            self.committed_resume = Some(handshake);
        }
        Ok(())
    }

    async fn apply_plan(
        &mut self,
        intent: &EffectIntent,
        plan: &CompiledPlan,
    ) -> EdgeResult<(String, u32)> {
        let kind = crate::contract::edge::EffectKind::try_from(intent.kind)
            .map_err(|_| EdgeError::invalid("effect_kind", "unknown effect kind"))?;
        match kind {
            crate::contract::edge::EffectKind::BaselineActivate => self.apply_baseline(plan).await,
            crate::contract::edge::EffectKind::OverlayUpsert => {
                self.apply_overlay_upsert(plan).await
            }
            crate::contract::edge::EffectKind::OverlayDelete => {
                self.apply_overlay_delete(plan).await
            }
            crate::contract::edge::EffectKind::Unspecified => Err(EdgeError::invalid(
                "effect_kind",
                "unspecified effect cannot execute",
            )),
            crate::contract::edge::EffectKind::BoundedCaptureStart => Err(EdgeError::precondition(
                "BOUNDED_CAPTURE_PROFILE_UNSUPPORTED",
                "bounded capture is not qualified in this Edge target profile",
            )),
        }
    }

    fn recover_effect_journal(&mut self) -> EdgeResult<()> {
        let max_records = usize::try_from(self.config.limits.journal_records).map_err(|_| {
            EdgeError::exhausted(
                "JOURNAL_RECOVERY_LIMIT",
                "journal record bound exceeds usize",
            )
        })?;
        let max_bytes = usize::try_from(self.config.limits.journal_bytes).map_err(|_| {
            EdgeError::exhausted("JOURNAL_RECOVERY_LIMIT", "journal byte bound exceeds usize")
        })?;
        let replay = self.journal.replay(max_records, max_bytes)?;
        if replay.is_empty() {
            return Ok(());
        }
        let mut operation_key: Option<(String, String, String)> = None;
        let mut recovered: Option<RecoveredEffect> = None;
        let mut pending: Option<PendingEffectAck> = None;
        for item in replay {
            let record = EffectJournalRecord::decode(item.payload.as_slice()).map_err(|error| {
                EdgeError::WalCorrupt(format!("decode effect journal record: {error}"))
            })?;
            if record.schema_version != "edge-effect-journal/v1" {
                return Err(EdgeError::WalCorrupt(
                    "unknown effect journal schema major".into(),
                ));
            }
            let intent = record.intent.ok_or_else(|| {
                EdgeError::WalCorrupt("effect journal record is missing intent".into())
            })?;
            let plan_contract = record.plan.ok_or_else(|| {
                EdgeError::WalCorrupt("effect journal record is missing compiled plan".into())
            })?;
            if intent.target_id != self.assignment.target_id {
                return Err(EdgeError::WalCorrupt(
                    "effect journal target differs from target directory".into(),
                ));
            }
            let key = (
                intent.operation_id.clone(),
                intent.effect_intent_id.clone(),
                intent.effect_digest.clone(),
            );
            if operation_key
                .as_ref()
                .is_some_and(|existing| existing != &key)
            {
                return Err(EdgeError::WalCorrupt(
                    "multiple unacknowledged effect operations share one target journal".into(),
                ));
            }
            operation_key = Some(key);
            let plan = CompiledPlan::from_contract(plan_contract)?;
            let status = record
                .result
                .as_ref()
                .map(|result| {
                    EffectStatus::try_from(result.status).map_err(|_| {
                        EdgeError::WalCorrupt("unknown effect result status in journal".into())
                    })
                })
                .transpose()?;
            recovered = Some(RecoveredEffect {
                intent: intent.clone(),
                plan: plan.clone(),
                last_status: status,
            });
            if let Some(result) = record.result {
                pending = Some(PendingEffectAck {
                    operation_id: intent.operation_id,
                    effect_intent_id: intent.effect_intent_id,
                    result_digest: digest::message_sha256(&result),
                    journal_sequence: item.sequence,
                    result,
                });
            }
        }
        let recovered = recovered.ok_or_else(|| {
            EdgeError::WalCorrupt("effect journal replay contained no usable record".into())
        })?;
        match recovered.last_status {
            Some(EffectStatus::Applied) | Some(EffectStatus::Hold) => {
                self.pending_effect_ack = pending;
            }
            Some(EffectStatus::Unknown)
            | Some(EffectStatus::Reconciling)
            | Some(EffectStatus::Unspecified)
            | None => {
                self.pending_effect_ack = pending;
                self.recovered_effect = Some(recovered);
            }
        }
        Ok(())
    }

    async fn reconcile_recovered_effect(&mut self) -> EdgeResult<()> {
        let recovered = self.recovered_effect.clone().ok_or_else(|| {
            EdgeError::precondition("EFFECT_RECOVERY_ABSENT", "no recovered effect")
        })?;
        let readback = self.readback_plan(&recovered.intent, &recovered.plan).await;
        let (status, readback_digest, observed_entries, mismatched_entries, reason_code) =
            match readback {
                Ok((readback_digest, observed_entries)) => (
                    EffectStatus::Applied,
                    readback_digest,
                    observed_entries,
                    0,
                    "RECOVERED_EXACT_READBACK".to_owned(),
                ),
                Err(EdgeError::Precondition { code, message })
                    if matches!(code, "READBACK_MISMATCH" | "SELECTOR_MISMATCH") =>
                {
                    (
                        EffectStatus::Unknown,
                        String::new(),
                        0,
                        u32::try_from(recovered.plan.entries.len()).unwrap_or(u32::MAX),
                        format!("{code}:{message}"),
                    )
                }
                Err(error) => return Err(error),
            };
        if status == EffectStatus::Unknown && recovered.last_status == Some(EffectStatus::Unknown) {
            return Ok(());
        }
        let result = EffectResult {
            schema_version: "edge-effect-result/v1".into(),
            target_id: recovered.intent.target_id.clone(),
            effect_intent_id: recovered.intent.effect_intent_id.clone(),
            operation_id: recovered.intent.operation_id.clone(),
            fence: recovered.intent.fence.clone(),
            status: status as i32,
            plan_digest: recovered.plan.contract.plan_digest.clone(),
            readback_digest,
            expected_entries: u32::try_from(recovered.plan.entries.len()).map_err(|_| {
                EdgeError::exhausted("CAPACITY_EXCEEDED", "entry count exceeds u32")
            })?,
            observed_entries,
            mismatched_entries,
            active_bank: if recovered.intent.kind
                == crate::contract::edge::EffectKind::BaselineActivate as i32
            {
                recovered.plan.contract.inactive_bank
            } else {
                recovered.plan.contract.active_bank
            },
            reason_code,
            trace_id: recovered.intent.trace_id.clone(),
            applied_entries: applied_readback_entries(&recovered.plan),
            readback_manifest_digest: readback_manifest_digest(&recovered.plan),
            bounded_capture: None,
        };
        let durable = EffectJournalRecord {
            schema_version: "edge-effect-journal/v1".into(),
            stage: if status == EffectStatus::Applied {
                JournalStage::ReadbackExact as i32
            } else {
                JournalStage::Unknown as i32
            },
            intent: Some(recovered.intent.clone()),
            plan: Some(recovered.plan.contract.clone()),
            result: Some(result.clone()),
            recorded_at_unix_ms: unix_ms()?,
            reason_code: result.reason_code.clone(),
        };
        let journal_sequence = self.journal.append_message(&durable)?;
        self.pending_effect_ack = Some(PendingEffectAck {
            operation_id: recovered.intent.operation_id.clone(),
            effect_intent_id: recovered.intent.effect_intent_id.clone(),
            result_digest: digest::message_sha256(&result),
            journal_sequence,
            result,
        });
        if status == EffectStatus::Applied {
            self.recovered_effect = None;
        } else {
            self.recovered_effect = Some(RecoveredEffect {
                last_status: Some(EffectStatus::Unknown),
                ..recovered
            });
        }
        Ok(())
    }

    async fn readback_plan(
        &mut self,
        intent: &EffectIntent,
        plan: &CompiledPlan,
    ) -> EdgeResult<(String, u32)> {
        let kind = crate::contract::edge::EffectKind::try_from(intent.kind)
            .map_err(|_| EdgeError::WalCorrupt("unknown effect kind in journal".into()))?;
        match kind {
            crate::contract::edge::EffectKind::BaselineActivate => {
                let mut expected = plan.entries.clone();
                let default = plan.default_entry.clone().ok_or_else(|| {
                    EdgeError::WalCorrupt("recovered baseline lacks default entry".into())
                })?;
                let selector = plan.selector_entry.clone().ok_or_else(|| {
                    EdgeError::WalCorrupt("recovered baseline lacks selector entry".into())
                })?;
                let mut queries = plan.key_queries()?;
                queries.push(table_key(&default)?);
                let observed = self.read_batched(queries).await?;
                expected.push(default);
                let digest = firewall::exact_readback(&expected, &observed)?;
                let observed_selector = self
                    .session_mut()?
                    .read(vec![firewall::policy_selector_query()])
                    .await?;
                firewall::exact_readback(&[selector], &observed_selector).map_err(|_| {
                    EdgeError::precondition(
                        "SELECTOR_MISMATCH",
                        "recovered selector does not match compiled plan",
                    )
                })?;
                Ok((
                    digest,
                    u32::try_from(plan.entries.len()).unwrap_or(u32::MAX),
                ))
            }
            crate::contract::edge::EffectKind::OverlayUpsert => {
                let observed = self.read_batched(plan.key_queries()?).await?;
                firewall::exact_readback(&plan.entries, &observed)
                    .map(|digest| (digest, u32::try_from(observed.len()).unwrap_or(u32::MAX)))
            }
            crate::contract::edge::EffectKind::OverlayDelete => {
                let mut observed = Vec::new();
                for query in plan.key_queries()? {
                    observed.extend(self.session_mut()?.read(vec![query]).await?);
                }
                if observed.is_empty() {
                    Ok((digest::sha256(b"empty-overlay-readback"), 0))
                } else {
                    Err(EdgeError::precondition(
                        "READBACK_MISMATCH",
                        "recovered overlay delete still has matching entries",
                    ))
                }
            }
            crate::contract::edge::EffectKind::Unspecified => Err(EdgeError::WalCorrupt(
                "unspecified effect kind in journal".into(),
            )),
            crate::contract::edge::EffectKind::BoundedCaptureStart => Err(EdgeError::WalCorrupt(
                "bounded capture journal is unsupported by this target profile".into(),
            )),
        }
    }

    async fn apply_baseline(&mut self, plan: &CompiledPlan) -> EdgeResult<(String, u32)> {
        let table = if plan.contract.inactive_bank == 0 {
            firewall::table_id::BASELINE_0
        } else {
            firewall::table_id::BASELINE_1
        };
        let existing = self
            .session_mut()?
            .read(vec![firewall::table_query(table)])
            .await?;
        if !existing.is_empty() {
            let deletes = firewall::delete_updates(&existing)?;
            let write = self.session_mut()?.write_batched(&deletes).await;
            let after = self
                .session_mut()?
                .read(vec![firewall::table_query(table)])
                .await
                .map_err(|error| {
                    EdgeError::UnknownOutcome(format!(
                        "inactive bank clear readback unavailable:{}",
                        error.reason_code()
                    ))
                })?;
            if !after.is_empty() {
                return Err(EdgeError::UnknownOutcome(if write.is_err() {
                    "inactive bank clear response failed and entries remain".into()
                } else {
                    "inactive bank clear response returned but entries remain".into()
                }));
            }
        }
        let default_entry = plan.default_entry.clone().ok_or_else(|| {
            EdgeError::precondition("READBACK_MISMATCH", "baseline default entry missing")
        })?;
        let default_write = self
            .session_mut()?
            .write(vec![firewall::modify_update(default_entry.clone())])
            .await;
        let default_observed = self
            .session_mut()?
            .read(vec![table_key(&default_entry)?])
            .await
            .map_err(|error| {
                EdgeError::UnknownOutcome(format!(
                    "default action readback unavailable:{}",
                    error.reason_code()
                ))
            })?;
        if firewall::exact_readback(std::slice::from_ref(&default_entry), &default_observed)
            .is_err()
        {
            return Err(EdgeError::UnknownOutcome(if default_write.is_err() {
                "default action response failed and readback differs".into()
            } else {
                "default action response returned but readback differs".into()
            }));
        }
        if !plan.entries.is_empty() {
            let write = self
                .session_mut()?
                .write_batched(&plan.insert_updates())
                .await;
            let observed = self
                .read_batched(plan.key_queries()?)
                .await
                .map_err(|error| {
                    EdgeError::UnknownOutcome(format!(
                        "baseline entry readback unavailable:{}",
                        error.reason_code()
                    ))
                })?;
            if firewall::exact_readback(&plan.entries, &observed).is_err() {
                return Err(EdgeError::UnknownOutcome(if write.is_err() {
                    "baseline write response failed and exact readback differs".into()
                } else {
                    "baseline write response returned but exact readback differs".into()
                }));
            }
        }
        let selector = plan.selector_entry.clone().ok_or_else(|| {
            EdgeError::precondition("SELECTOR_MISMATCH", "selector entry missing")
        })?;
        let selector_write = self
            .session_mut()?
            .write(vec![firewall::modify_update(selector.clone())])
            .await;
        let observed_selector = self
            .session_mut()?
            .read(vec![firewall::policy_selector_query()])
            .await
            .map_err(|error| {
                EdgeError::UnknownOutcome(format!(
                    "policy selector readback unavailable:{}",
                    error.reason_code()
                ))
            })?;
        if firewall::exact_readback(&[selector], &observed_selector).is_err() {
            return Err(EdgeError::UnknownOutcome(if selector_write.is_err() {
                "selector response failed and readback differs".into()
            } else {
                "selector response returned but readback differs".into()
            }));
        }
        self.last_successful_read_ms = unix_ms()?;
        let mut expected = plan.entries.clone();
        expected.push(default_entry);
        let readback_digest = firewall::exact_readback(&expected, &expected)?;
        Ok((readback_digest, plan.entries.len() as u32))
    }

    async fn apply_overlay_upsert(&mut self, plan: &CompiledPlan) -> EdgeResult<(String, u32)> {
        let mut updates = Vec::with_capacity(plan.entries.len());
        for entry in &plan.entries {
            let observed = self.session_mut()?.read(vec![table_key(entry)?]).await?;
            updates.push(if observed.is_empty() {
                firewall::insert_update(entry.clone())
            } else {
                firewall::modify_update(entry.clone())
            });
        }
        let write = self.session_mut()?.write_batched(&updates).await;
        let observed = self
            .read_batched(plan.key_queries()?)
            .await
            .map_err(|error| {
                EdgeError::UnknownOutcome(format!(
                    "overlay readback unavailable:{}",
                    error.reason_code()
                ))
            })?;
        let readback = firewall::exact_readback(&plan.entries, &observed);
        match readback {
            Ok(digest) => Ok((digest, observed.len() as u32)),
            Err(_) => Err(EdgeError::UnknownOutcome(if write.is_err() {
                "overlay write response failed and readback differs".into()
            } else {
                "overlay write response returned but readback differs".into()
            })),
        }
    }

    async fn apply_overlay_delete(&mut self, plan: &CompiledPlan) -> EdgeResult<(String, u32)> {
        let updates: Vec<Update> = plan
            .entries
            .iter()
            .map(firewall::delete_update)
            .collect::<EdgeResult<_>>()?;
        let write = self.session_mut()?.write_batched(&updates).await;
        let mut remaining = Vec::new();
        for query in plan.key_queries()? {
            remaining.extend(
                self.session_mut()?
                    .read(vec![query])
                    .await
                    .map_err(|error| {
                        EdgeError::UnknownOutcome(format!(
                            "overlay delete readback unavailable:{}",
                            error.reason_code()
                        ))
                    })?,
            );
        }
        if remaining.is_empty() {
            Ok((digest::sha256(b"empty-overlay-readback"), 0))
        } else {
            Err(EdgeError::UnknownOutcome(if write.is_err() {
                "overlay delete response failed and entries remain".into()
            } else {
                "overlay delete response returned but entries remain".into()
            }))
        }
    }

    async fn poll_telemetry(&mut self) -> EdgeResult<()> {
        if !self.lease_valid()
            || self.state != ActorState::Primary
            || self.session.is_none()
            || self.source_recovery.is_some()
            || self.source_wal.at_watermark(95, 100)
        {
            return Ok(());
        }
        let selector = self
            .session_mut()?
            .read(vec![firewall::telemetry_selector_query()])
            .await?;
        let (active_bank, epoch) = firewall::active_telemetry_bank(&selector)?;
        let sequence_before = self
            .session_mut()?
            .read(vec![firewall::counter_query(
                firewall::counter_id::TELEMETRY_BANK,
                i64::from(active_bank),
            )])
            .await?;
        let (packets_before, bytes_before) = single_counter(&sequence_before)?;
        self.source_sequence = self.source_sequence.saturating_add(1);
        let source_runtime_epoch = format!(
            "source:{}",
            self.assignment
                .fence
                .as_ref()
                .map_or("missing", |fence| fence.actor_runtime_epoch.as_str())
        );
        let started_at_ms = unix_ms()?;
        let started = SourceWalRecord {
            schema_version: "edge-source-wal/v1".into(),
            stage: SourceWalStage::BankFreezeStarted as i32,
            recorded_at_unix_ms: started_at_ms,
            frozen_bank: active_bank,
            new_active_bank: 1 - active_bank,
            previous_epoch: u64::from(epoch),
            new_epoch: u64::from(epoch.saturating_add(1)),
            operation_wal_sequence: self.source_wal.next_sequence(),
            source_sequence: self.source_sequence,
            counter_packets_before: packets_before,
            counter_bytes_before: bytes_before,
            reason_code: "BANK_FREEZE_STARTED".into(),
            source_runtime_epoch: source_runtime_epoch.clone(),
            new_active_started_at_unix_ms: started_at_ms,
            ..SourceWalRecord::default()
        };
        let operation_wal_sequence = self.source_wal.append_message(&started)?;
        if operation_wal_sequence != started.operation_wal_sequence {
            return Err(EdgeError::precondition(
                "WAL_SEQUENCE_CONFLICT",
                "predicted source operation sequence changed",
            ));
        }
        self.source_recovery = Some(SourceRecovery {
            operation_wal_sequence,
            frozen_bank: active_bank,
            new_active_bank: 1 - active_bank,
            previous_epoch: u64::from(epoch),
            new_epoch: u64::from(epoch.saturating_add(1)),
            source_sequence: self.source_sequence,
            counter_packets_before: packets_before,
            counter_bytes_before: bytes_before,
            source_runtime_epoch,
            new_active_started_at_unix_ms: started_at_ms,
            frozen: false,
            snapshot: None,
            snapshot_wal_sequence: 0,
            cleared: false,
        });
        self.finish_source_recovery().await
    }

    async fn finish_source_recovery(&mut self) -> EdgeResult<()> {
        let mut recovery = self.source_recovery.clone().ok_or_else(|| {
            EdgeError::precondition("SOURCE_RECOVERY_ABSENT", "no source operation to recover")
        })?;
        let selector = self
            .session_mut()?
            .read(vec![firewall::telemetry_selector_query()])
            .await?;
        let (observed_bank, observed_epoch) = firewall::active_telemetry_bank(&selector)?;
        let mut freeze_reason = None;
        if observed_bank == recovery.frozen_bank
            && u64::from(observed_epoch) == recovery.previous_epoch
            && !recovery.frozen
        {
            let new_epoch = u32::try_from(recovery.new_epoch).map_err(|_| {
                EdgeError::WalCorrupt("telemetry epoch exceeds first-release u32".into())
            })?;
            let new_selector = firewall::telemetry_selector(recovery.new_active_bank, new_epoch)?;
            let write = self
                .session_mut()?
                .write(vec![firewall::modify_update(new_selector.clone())])
                .await;
            let selector_readback = self
                .session_mut()?
                .read(vec![firewall::telemetry_selector_query()])
                .await
                .map_err(|error| {
                    EdgeError::UnknownOutcome(format!(
                        "telemetry selector readback unavailable:{}",
                        error.reason_code()
                    ))
                })?;
            if let Err(readback_error) =
                firewall::exact_readback(&[new_selector], &selector_readback)
            {
                return Err(if write.is_err() {
                    EdgeError::UnknownOutcome(
                        "telemetry selector response lost and readback differs".into(),
                    )
                } else {
                    EdgeError::precondition(
                        "TELEMETRY_SELECTOR_MISMATCH",
                        format!("telemetry selector exact readback failed: {readback_error}"),
                    )
                });
            }
            freeze_reason = Some("BANK_FROZEN");
        } else if observed_bank == recovery.new_active_bank
            && u64::from(observed_epoch) == recovery.new_epoch
        {
            if !recovery.frozen {
                freeze_reason = Some("BANK_FROZEN_RECOVERED");
            }
        } else {
            return Err(EdgeError::precondition(
                "TELEMETRY_SELECTOR_DRIFT",
                "selector is neither the durable pre-state nor frozen post-state",
            ));
        }
        if let Some(reason) = freeze_reason {
            let frozen_aggregate = self
                .session_mut()?
                .read(vec![firewall::counter_query(
                    firewall::counter_id::TELEMETRY_BANK,
                    i64::from(recovery.frozen_bank),
                )])
                .await?;
            let (packets, bytes) = single_counter(&frozen_aggregate)?;
            recovery.counter_packets_before = packets;
            recovery.counter_bytes_before = bytes;
            recovery.frozen = true;
            self.source_wal.append_message(&source_stage_record(
                SourceWalStage::BankFrozen,
                &recovery,
                reason,
            )?)?;
            self.source_recovery = Some(recovery.clone());
        }

        if recovery.snapshot.is_none() {
            let base = i64::from(recovery.frozen_bank) * 256;
            let queries: Vec<Entity> = (0_i64..256)
                .map(|index| {
                    firewall::counter_query(firewall::counter_id::TELEMETRY_CELL, base + index)
                })
                .collect();
            let cells_readback = self.read_batched(queries).await?;
            if cells_readback.len() != 256 {
                return Err(EdgeError::precondition(
                    "SNAPSHOT_INCONSISTENT",
                    "telemetry snapshot did not return all 256 cells",
                ));
            }
            let aggregate = self
                .session_mut()?
                .read(vec![firewall::counter_query(
                    firewall::counter_id::TELEMETRY_BANK,
                    i64::from(recovery.frozen_bank),
                )])
                .await?;
            let (packets_after, bytes_after) = single_counter(&aggregate)?;
            let mut cells = Vec::new();
            for (index, entity) in cells_readback.iter().enumerate() {
                let (packets, bytes) = firewall::counter_value(entity)?;
                if packets != 0 || bytes != 0 {
                    cells.push(TelemetryCell {
                        index: index as u32,
                        packets,
                        bytes,
                        selector_digest: digest::sha256(
                            format!(
                                "{}\0{}\0{}\0{}",
                                self.assignment.target_id,
                                self.config.telemetry_source_profile_digest,
                                recovery.previous_epoch,
                                index
                            )
                            .as_bytes(),
                        ),
                    });
                }
            }
            let reported_nonzero_cells = cells.len() as u64;
            let now_ms = unix_ms()?;
            let stable_counter = recovery.counter_packets_before == packets_after
                && recovery.counter_bytes_before == bytes_after;
            if !stable_counter {
                return Err(EdgeError::precondition(
                    "SNAPSHOT_INCONSISTENT",
                    "frozen-bank aggregate changed during readback; retaining bank for retry",
                ));
            }
            let aggregation_start = self.active_bank_started_ms.unwrap_or_default();
            let known_start = aggregation_start > 0;
            let quality_valid = known_start;
            let mut quality_reasons = Vec::new();
            let mut gaps = Vec::new();
            if !known_start {
                quality_reasons.push("SOURCE_START_UNKNOWN".into());
                gaps.push(SourceGap {
                    source_sequence_start: recovery.source_sequence,
                    source_sequence_end: recovery.source_sequence,
                    reason_code: "SOURCE_START_UNKNOWN".into(),
                });
            }
            if quality_reasons.is_empty() {
                quality_reasons.push("NONE".into());
            }
            let snapshot_sequence = self.source_wal.next_sequence();
            let expected_hints = packets_after.saturating_add(1023) / 1024;
            let sampling_coverage_ppm =
                coverage_ppm(self.digest_hints_since_snapshot, expected_hints);
            let allowed_lateness_ms = i64::try_from(self.config.limits.allowed_lateness_ms)
                .map_err(|_| {
                    EdgeError::invalid("allowed_lateness_ms", "exceeds signed timestamp range")
                })?;
            let fence = self.assignment.fence.clone();
            let application_generation = fence
                .as_ref()
                .map_or(0, |value| value.application_generation);
            let window_material = format!(
                "{}\0{}\0{}\0{}\0{}",
                self.assignment.target_id,
                recovery.source_runtime_epoch,
                recovery.source_sequence,
                aggregation_start,
                now_ms,
            );
            let window_id = format!(
                "window:{}",
                digest::sha256(window_material.as_bytes())
                    .strip_prefix("sha256:")
                    .ok_or_else(|| {
                        EdgeError::precondition("DIGEST_INTERNAL", "sha256 prefix missing")
                    })?,
            );
            let snapshot = TelemetrySnapshot {
                schema_version: "telemetry-p4-window/v1".into(),
                source_profile: "p4-bounded-aggregate-dual-bank/v1".into(),
                source_profile_digest: self.config.telemetry_source_profile_digest.clone(),
                telemetry_source_id: format!("p4-source:{}", self.assignment.target_id),
                target_id: self.assignment.target_id.clone(),
                device_id: self.assignment.device_id,
                fence,
                pipeline: self.assignment.expected_pipeline.clone(),
                source_runtime_epoch: recovery.source_runtime_epoch.clone(),
                frozen_bank: recovery.frozen_bank,
                epoch: recovery.previous_epoch,
                source_sequence_start: recovery.source_sequence,
                source_sequence_end: recovery.source_sequence,
                export_time_unix_ms: now_ms,
                ingest_time_unix_ms: now_ms,
                cells,
                aggregate_packets: packets_after,
                aggregate_bytes: bytes_after,
                quality: if quality_valid {
                    "valid".into()
                } else {
                    "partial".into()
                },
                quality_reasons,
                source_wal_sequence: snapshot_sequence,
                quality_code: if quality_valid {
                    DataQuality::Valid
                } else {
                    DataQuality::Partial
                } as i32,
                aggregation_start_unix_ms: aggregation_start,
                aggregation_end_unix_ms: now_ms,
                watermark_unix_ms: now_ms.saturating_sub(allowed_lateness_ms),
                final_snapshot: true,
                gaps,
                observation_epoch: self.assignment.observation_epoch,
                reset_epoch: self.assignment.reset_epoch,
                role: self.assignment.role.clone(),
                observation_domain: format!("target:{}", self.assignment.target_id),
                observation_point: "p4-ingress-pre-firewall".into(),
                application_generation,
                window_id,
                event_time_basis: "p4-aggregate-export-time".into(),
                allowed_lateness_ms: self.config.limits.allowed_lateness_ms,
                finalized_at_unix_ms: now_ms,
                produced_at_unix_ms: now_ms,
                expected_sequence_start: recovery.source_sequence,
                expected_sequence_end: recovery.source_sequence,
                observed_sequence_start: recovery.source_sequence,
                observed_sequence_end: recovery.source_sequence,
                sampling: Some(SamplingEvidence {
                    algorithm: "sequence-modulo".into(),
                    period_packets: 1024,
                    eligible_population: packets_after,
                    expected_hints,
                    observed_hints: self.digest_hints_since_snapshot,
                    coverage_ppm: sampling_coverage_ppm,
                    selector: "source-sequence-modulo-period".into(),
                    seed_digest: self.config.telemetry_source_profile_digest.clone(),
                    weighting: "none-hint-only".into(),
                    estimation_error_ppm: 1_000_000,
                    complete_population_claim: false,
                }),
                drops: Some(DropEvidence {
                    p4_source_drops: 0,
                    p4_server_drops: 0,
                    grpc_channel_drops: 0,
                    edge_client_drops: self.hint_drops_since_snapshot,
                    digest_hints_observed: self.digest_hints_since_snapshot,
                    packet_in_hints_observed: self.packet_hints_since_snapshot,
                    p4_source_drop_measurable: false,
                    p4_server_drop_measurable: false,
                    grpc_channel_drop_measurable: false,
                    edge_client_drop_measurable: true,
                }),
                cell_count: 256,
                reported_nonzero_cells,
                snapshot_strategy: "freeze-flip-read-readback-clear".into(),
                active_bank_after_flip: recovery.new_active_bank,
                sequence_before: recovery.counter_packets_before,
                sequence_after: packets_after,
                expected_entries: 256,
                observed_entries: 256,
                snapshot_consistency: SnapshotConsistency::Stable as i32,
                clear_advance_condition: ClearAdvanceCondition::ReadbackStable as i32,
                shard_id: self.assignment.target_id.clone(),
                capture_adapter_id: "p4runtime-bounded-aggregate".into(),
                endpoint_identity: format!("p4runtime-device:{}", self.assignment.device_id),
                flow_identity_profile: Some(TelemetryFlowIdentityProfile {
                    schema_version: "telemetry-flow-identity-profile/v1".into(),
                    shard_id: self.assignment.target_id.clone(),
                    capture_adapter_id: "p4runtime-bounded-aggregate".into(),
                    endpoint_identity: format!("p4runtime-device:{}", self.assignment.device_id),
                    direction: TelemetryFlowDirection::Ingress as i32,
                    supported_ip_versions: vec![TelemetryIpVersion::Ipv4 as i32],
                    endpoint_ordering: "unidirectional-observation-order".into(),
                    vlan_id_included: false,
                    tunnel_id_included: false,
                    fragment_semantics: "fragment-class-preserved".into(),
                    l4_unavailable_semantics: "port-unavailable-with-absent-ports".into(),
                    selector_algorithm: "p4-qualified-cell-selector/v1".into(),
                    selector_seed_digest: self.config.telemetry_source_profile_digest.clone(),
                    exposure: "aggregate-cell-selector-digest-only".into(),
                }),
                event_time_min_unix_ms: aggregation_start,
                event_time_max_unix_ms: now_ms,
                packet_time_observed: false,
                packet_time_semantics: "not-observed-by-p4-bounded-aggregate".into(),
                monotonic_queue_age_ms: 0,
                monotonic_deadline_budget_ms: self.config.limits.p4_rpc_deadline_ms,
            };
            let snapshot_record = SourceWalRecord {
                schema_version: "edge-source-wal/v1".into(),
                stage: SourceWalStage::SnapshotDurable as i32,
                snapshot: Some(snapshot.clone()),
                snapshot_wal_sequence: snapshot_sequence,
                recorded_at_unix_ms: now_ms,
                frozen_bank: recovery.frozen_bank,
                new_active_bank: recovery.new_active_bank,
                previous_epoch: recovery.previous_epoch,
                new_epoch: recovery.new_epoch,
                operation_wal_sequence: recovery.operation_wal_sequence,
                source_sequence: recovery.source_sequence,
                counter_packets_before: recovery.counter_packets_before,
                counter_bytes_before: recovery.counter_bytes_before,
                reason_code: "SNAPSHOT_DURABLE".into(),
                source_runtime_epoch: recovery.source_runtime_epoch.clone(),
                new_active_started_at_unix_ms: recovery.new_active_started_at_unix_ms,
                ..SourceWalRecord::default()
            };
            let appended = self.source_wal.append_message(&snapshot_record)?;
            if appended != snapshot_sequence {
                return Err(EdgeError::precondition(
                    "WAL_SEQUENCE_CONFLICT",
                    "predicted snapshot sequence changed",
                ));
            }
            recovery.snapshot = Some(snapshot);
            recovery.snapshot_wal_sequence = snapshot_sequence;
            self.source_recovery = Some(recovery.clone());
        }

        if !recovery.cleared {
            self.clear_telemetry_bank(recovery.frozen_bank).await?;
            recovery.cleared = true;
            self.source_wal.append_message(&source_stage_record(
                SourceWalStage::ClearCompleted,
                &recovery,
                "CLEAR_COMPLETED",
            )?)?;
            self.source_recovery = Some(recovery.clone());
        }

        let mut snapshot = recovery.snapshot.clone().ok_or_else(|| {
            EdgeError::WalCorrupt("source recovery cleared without a durable snapshot".into())
        })?;
        snapshot.source_wal_sequence = self.source_wal.next_sequence();
        let finalized = SourceWalRecord {
            schema_version: "edge-source-wal/v1".into(),
            stage: SourceWalStage::SnapshotFinalized as i32,
            snapshot: Some(snapshot.clone()),
            snapshot_wal_sequence: recovery.snapshot_wal_sequence,
            recorded_at_unix_ms: unix_ms()?,
            frozen_bank: recovery.frozen_bank,
            new_active_bank: recovery.new_active_bank,
            previous_epoch: recovery.previous_epoch,
            new_epoch: recovery.new_epoch,
            operation_wal_sequence: recovery.operation_wal_sequence,
            source_sequence: recovery.source_sequence,
            counter_packets_before: recovery.counter_packets_before,
            counter_bytes_before: recovery.counter_bytes_before,
            reason_code: "SNAPSHOT_FINALIZED".into(),
            source_runtime_epoch: recovery.source_runtime_epoch,
            new_active_started_at_unix_ms: recovery.new_active_started_at_unix_ms,
            ..SourceWalRecord::default()
        };
        let finalized_sequence = self.source_wal.append_message(&finalized)?;
        if finalized_sequence != snapshot.source_wal_sequence {
            return Err(EdgeError::precondition(
                "WAL_SEQUENCE_CONFLICT",
                "predicted finalized source sequence changed",
            ));
        }
        self.active_bank_started_ms = Some(recovery.new_active_started_at_unix_ms);
        self.digest_hints_since_snapshot = 0;
        self.packet_hints_since_snapshot = 0;
        self.hint_drops_since_snapshot = 0;
        self.source_recovery = None;
        self.last_successful_read_ms = snapshot.ingest_time_unix_ms;
        self.admit_snapshot(&snapshot)
    }

    fn recover_source_state(&mut self) -> EdgeResult<()> {
        let input_record_limit =
            usize::try_from(self.config.limits.input_wal_records).map_err(|_| {
                EdgeError::exhausted("INPUT_RECOVERY_LIMIT", "input record bound exceeds usize")
            })?;
        let input_byte_limit =
            usize::try_from(self.config.limits.input_wal_bytes).map_err(|_| {
                EdgeError::exhausted("INPUT_RECOVERY_LIMIT", "input byte bound exceeds usize")
            })?;
        for item in self
            .input_wal
            .replay(input_record_limit, input_byte_limit)?
        {
            let record = InferenceRecord::decode(item.payload.as_slice()).map_err(|error| {
                EdgeError::WalCorrupt(format!("decode input WAL during recovery: {error}"))
            })?;
            if record.input_wal_sequence != item.sequence {
                return Err(EdgeError::WalCorrupt(
                    "input WAL sequence fence mismatch during recovery".into(),
                ));
            }
            if !self.known_input_ids.contains(&record.input_id)
                && self.known_input_ids.len() == self.config.limits.pending_input_identities
            {
                return Err(EdgeError::exhausted(
                    "INPUT_IDENTITY_LIMIT_EXCEEDED",
                    "recovered pending input identities exceed the memory bound",
                ));
            }
            self.known_input_ids.insert(record.input_id);
        }

        let source_record_limit =
            usize::try_from(self.config.limits.source_wal_records).map_err(|_| {
                EdgeError::exhausted("SOURCE_RECOVERY_LIMIT", "source record bound exceeds usize")
            })?;
        let source_byte_limit =
            usize::try_from(self.config.limits.source_wal_bytes).map_err(|_| {
                EdgeError::exhausted("SOURCE_RECOVERY_LIMIT", "source byte bound exceeds usize")
            })?;
        let replay = self
            .source_wal
            .replay(source_record_limit, source_byte_limit)?;
        let mut finalized_snapshots = Vec::new();
        let mut recovery: Option<SourceRecovery> = None;
        for item in replay {
            let record = SourceWalRecord::decode(item.payload.as_slice()).map_err(|error| {
                EdgeError::WalCorrupt(format!("decode source WAL record: {error}"))
            })?;
            if record.schema_version != "edge-source-wal/v1" {
                return Err(EdgeError::WalCorrupt(
                    "unknown source WAL schema major".into(),
                ));
            }
            let stage = SourceWalStage::try_from(record.stage)
                .map_err(|_| EdgeError::WalCorrupt("unknown source WAL stage".into()))?;
            self.source_sequence = self.source_sequence.max(record.source_sequence);
            match stage {
                SourceWalStage::BankFreezeStarted => {
                    if recovery.is_some() {
                        return Err(EdgeError::WalCorrupt(
                            "overlapping telemetry freeze operations".into(),
                        ));
                    }
                    if record.operation_wal_sequence != item.sequence
                        || record.new_active_bank == record.frozen_bank
                        || record.new_epoch <= record.previous_epoch
                        || record.source_sequence == 0
                    {
                        return Err(EdgeError::WalCorrupt(
                            "invalid telemetry freeze fence".into(),
                        ));
                    }
                    recovery = Some(SourceRecovery {
                        operation_wal_sequence: item.sequence,
                        frozen_bank: record.frozen_bank,
                        new_active_bank: record.new_active_bank,
                        previous_epoch: record.previous_epoch,
                        new_epoch: record.new_epoch,
                        source_sequence: record.source_sequence,
                        counter_packets_before: record.counter_packets_before,
                        counter_bytes_before: record.counter_bytes_before,
                        source_runtime_epoch: record.source_runtime_epoch,
                        new_active_started_at_unix_ms: record.new_active_started_at_unix_ms,
                        frozen: false,
                        snapshot: None,
                        snapshot_wal_sequence: 0,
                        cleared: false,
                    });
                }
                SourceWalStage::BankFrozen => {
                    source_recovery_for_record(&mut recovery, &record)?.frozen = true;
                }
                SourceWalStage::SnapshotDurable => {
                    let current = source_recovery_for_record(&mut recovery, &record)?;
                    let snapshot = record.snapshot.ok_or_else(|| {
                        EdgeError::WalCorrupt("snapshot stage lacks snapshot".into())
                    })?;
                    if record.snapshot_wal_sequence != item.sequence
                        || snapshot.source_wal_sequence != item.sequence
                    {
                        return Err(EdgeError::WalCorrupt(
                            "snapshot WAL sequence fence mismatch".into(),
                        ));
                    }
                    current.snapshot = Some(snapshot);
                    current.snapshot_wal_sequence = item.sequence;
                }
                SourceWalStage::ClearCompleted => {
                    source_recovery_for_record(&mut recovery, &record)?.cleared = true;
                }
                SourceWalStage::SnapshotFinalized => {
                    let current = source_recovery_for_record(&mut recovery, &record)?;
                    if !current.cleared {
                        return Err(EdgeError::WalCorrupt(
                            "snapshot finalized before durable clear".into(),
                        ));
                    }
                    let snapshot = record.snapshot.ok_or_else(|| {
                        EdgeError::WalCorrupt("finalized source stage lacks snapshot".into())
                    })?;
                    if snapshot.source_wal_sequence != item.sequence
                        || record.snapshot_wal_sequence != current.snapshot_wal_sequence
                    {
                        return Err(EdgeError::WalCorrupt(
                            "finalized source cursor mismatch".into(),
                        ));
                    }
                    finalized_snapshots.push(snapshot);
                    self.active_bank_started_ms = Some(record.new_active_started_at_unix_ms);
                    self.digest_hints_since_snapshot = 0;
                    self.packet_hints_since_snapshot = 0;
                    self.hint_drops_since_snapshot = 0;
                    recovery = None;
                }
                SourceWalStage::DigestHintDurable => {
                    let hint = record.hint.ok_or_else(|| {
                        EdgeError::WalCorrupt("durable digest stage lacks hint".into())
                    })?;
                    self.digest_hints_since_snapshot =
                        self.digest_hints_since_snapshot.saturating_add(1);
                    if self.pending_digest_acks.len() == self.config.limits.pending_digest_ack_queue
                    {
                        return Err(EdgeError::exhausted(
                            "DIGEST_ACK_RECOVERY_LIMIT",
                            "unacknowledged durable digest hints exceed the recovery queue bound",
                        ));
                    }
                    self.pending_digest_acks.push_back((item.sequence, hint));
                }
                SourceWalStage::DigestHintAcked => {
                    let durable_sequence = record.operation_wal_sequence;
                    let Some(position) = self
                        .pending_digest_acks
                        .iter()
                        .position(|(sequence, _)| *sequence == durable_sequence)
                    else {
                        return Err(EdgeError::WalCorrupt(
                            "digest ACK stage has no durable hint".into(),
                        ));
                    };
                    self.pending_digest_acks.remove(position);
                }
                SourceWalStage::PacketHintDurable => {
                    self.packet_hints_since_snapshot =
                        self.packet_hints_since_snapshot.saturating_add(1);
                }
                SourceWalStage::SourceGapDurable => {
                    if record.reason_code.starts_with("SUPPLEMENTAL_HINT_DROPPED:") {
                        self.hint_drops_since_snapshot =
                            self.hint_drops_since_snapshot.saturating_add(1);
                    }
                }
                SourceWalStage::Unspecified => {
                    return Err(EdgeError::WalCorrupt("unspecified source WAL stage".into()));
                }
            }
        }
        self.source_recovery = recovery;
        for snapshot in finalized_snapshots {
            self.admit_snapshot(&snapshot)?;
        }
        Ok(())
    }

    fn admit_snapshot(&mut self, snapshot: &TelemetrySnapshot) -> EdgeResult<()> {
        let advance = self.windows.push(snapshot, unix_ms()?)?;
        self.admit_window_advance(advance)
    }

    fn admit_window_advance(&mut self, advance: crate::window::WindowAdvance) -> EdgeResult<()> {
        for mut record in advance.inference_records {
            if self.known_input_ids.contains(&record.input_id) {
                continue;
            }
            if self.input_wal.at_watermark(95, 100) || self.result_wal.at_watermark(95, 100) {
                return Err(EdgeError::exhausted(
                    "WAL_FULL",
                    "input/result WAL reached critical watermark",
                ));
            }
            if self.known_input_ids.len() == self.config.limits.pending_input_identities {
                return Err(EdgeError::exhausted(
                    "INPUT_IDENTITY_LIMIT_EXCEEDED",
                    "pending canonical input identity bound reached",
                ));
            }
            if self.router.state() == RouteState::Active {
                let route = self.router.route().cloned().ok_or_else(|| {
                    EdgeError::precondition("POOL_UNAVAILABLE", "active route is missing")
                })?;
                bind_inference_record(&mut record, &route)?;
            }
            record.input_wal_sequence = self.input_wal.next_sequence();
            let sequence = self.input_wal.append_message(&record)?;
            if sequence != record.input_wal_sequence {
                return Err(EdgeError::precondition(
                    "WAL_SEQUENCE_CONFLICT",
                    "predicted input WAL sequence changed",
                ));
            }
            self.known_input_ids.insert(record.input_id);
        }
        Ok(())
    }

    async fn clear_telemetry_bank(&mut self, bank: u32) -> EdgeResult<()> {
        let base = i64::from(bank) * 256;
        let mut updates: Vec<Update> = (0_i64..256)
            .map(|index| Update {
                r#type: update::Type::Modify as i32,
                entity: Some(firewall::counter_zero(
                    firewall::counter_id::TELEMETRY_CELL,
                    base + index,
                )),
            })
            .collect();
        updates.push(Update {
            r#type: update::Type::Modify as i32,
            entity: Some(firewall::counter_zero(
                firewall::counter_id::TELEMETRY_BANK,
                i64::from(bank),
            )),
        });
        let class_base = i64::from(bank) * 5;
        for index in 0_i64..5 {
            updates.push(Update {
                r#type: update::Type::Modify as i32,
                entity: Some(firewall::counter_zero(
                    firewall::counter_id::TELEMETRY_CLASS,
                    class_base + index,
                )),
            });
        }
        let write = self.session_mut()?.write_batched(&updates).await;
        let aggregate = self
            .session_mut()?
            .read(vec![firewall::counter_query(
                firewall::counter_id::TELEMETRY_BANK,
                i64::from(bank),
            )])
            .await
            .map_err(|error| {
                EdgeError::UnknownOutcome(format!(
                    "telemetry clear readback unavailable:{}",
                    error.reason_code()
                ))
            })?;
        let (packets, bytes) = single_counter(&aggregate)?;
        if packets == 0 && bytes == 0 {
            Ok(())
        } else {
            Err(EdgeError::UnknownOutcome(if write.is_err() {
                "telemetry clear response failed and readback nonzero".into()
            } else {
                "telemetry clear response returned but readback nonzero".into()
            }))
        }
    }

    async fn process_pending_inputs(&mut self) -> EdgeResult<()> {
        if !matches!(
            self.router.state(),
            RouteState::Active | RouteState::Draining
        ) || self.result_wal.stats().last_sequence > self.result_wal.stats().checkpoint_sequence
        {
            return Ok(());
        }
        let replay = self.input_wal.replay(
            self.config.limits.inference_batch_records,
            self.config.limits.inference_message_bytes,
        )?;
        if replay.is_empty() {
            return Ok(());
        }
        let mut decoded = Vec::with_capacity(replay.len());
        for item in &replay {
            let record = InferenceRecord::decode(item.payload.as_slice()).map_err(|error| {
                EdgeError::WalCorrupt(format!("decode inference input WAL: {error}"))
            })?;
            if record.input_wal_sequence != item.sequence {
                return Err(EdgeError::WalCorrupt(
                    "input WAL sequence fence mismatch".into(),
                ));
            }
            decoded.push(record);
        }
        if !inference_record_is_bound(&decoded[0]) {
            if self.router.state() != RouteState::Active {
                return Ok(());
            }
            let route = self.router.route().cloned().ok_or_else(|| {
                EdgeError::precondition("POOL_UNAVAILABLE", "active route is missing")
            })?;
            let first = &decoded[0];
            let already_durable = decoded.iter().skip(1).any(|candidate| {
                candidate.input_id == first.input_id
                    && inference_record_is_bound(candidate)
                    && candidate.model_control_incarnation_id == route.model_control_incarnation_id
                    && candidate.route_epoch == route.route_epoch
            });
            if !already_durable {
                let mut bound = first.clone();
                bind_inference_record(&mut bound, &route)?;
                bound.input_wal_sequence = self.input_wal.next_sequence();
                let sequence = self.input_wal.append_message(&bound)?;
                if sequence != bound.input_wal_sequence {
                    return Err(EdgeError::precondition(
                        "WAL_SEQUENCE_CONFLICT",
                        "route-bound input WAL sequence changed",
                    ));
                }
            }
            self.input_wal.checkpoint(replay[0].sequence)?;
            return Ok(());
        }
        let first_route_epoch = decoded[0].route_epoch;
        let records: Vec<InferenceRecord> = decoded
            .into_iter()
            .take_while(|record| {
                inference_record_is_bound(record) && record.route_epoch == first_route_epoch
            })
            .collect();
        let mut result = self
            .router
            .infer(
                records,
                unix_ms()?,
                format!("infer:{}", self.assignment.target_id),
            )
            .await?;
        let result_sequence = self.result_wal.next_sequence();
        for record in &mut result.records {
            record.result_wal_sequence = result_sequence;
        }
        result.batch_digest = canonical_result_batch_digest(&result);
        let durable = ResultWalRecord {
            schema_version: "edge-result-wal/v1".into(),
            stage: ResultWalStage::BatchDurable as i32,
            result_batch: Some(result),
            canonical_ack: None,
            result_batch_wal_sequence: result_sequence,
            recorded_at_unix_ms: unix_ms()?,
            reason_code: "RESULT_BATCH_DURABLE".into(),
        };
        let sequence = self.result_wal.append_message(&durable)?;
        if sequence != result_sequence {
            return Err(EdgeError::precondition(
                "WAL_SEQUENCE_CONFLICT",
                "predicted result WAL sequence changed",
            ));
        }
        Ok(())
    }

    fn has_route_bound_pending_inputs(&self) -> EdgeResult<bool> {
        let max_records = usize::try_from(self.config.limits.input_wal_records).map_err(|_| {
            EdgeError::exhausted("INPUT_RECOVERY_LIMIT", "input record bound exceeds usize")
        })?;
        let max_bytes = usize::try_from(self.config.limits.input_wal_bytes).map_err(|_| {
            EdgeError::exhausted("INPUT_RECOVERY_LIMIT", "input byte bound exceeds usize")
        })?;
        for item in self.input_wal.replay(max_records, max_bytes)? {
            let record = InferenceRecord::decode(item.payload.as_slice()).map_err(|error| {
                EdgeError::WalCorrupt(format!("decode inference input WAL: {error}"))
            })?;
            if record.input_wal_sequence != item.sequence {
                return Err(EdgeError::WalCorrupt(
                    "input WAL sequence fence mismatch".into(),
                ));
            }
            if inference_record_is_bound(&record) {
                return Ok(true);
            }
        }
        Ok(false)
    }

    async fn process_pending_results(&mut self) -> EdgeResult<()> {
        let replay = self.result_wal.replay(
            2,
            self.config.limits.control_message_bytes.saturating_mul(2),
        )?;
        let Some(batch_item) = replay.first() else {
            return Ok(());
        };
        let durable_batch = ResultWalRecord::decode(batch_item.payload.as_slice())
            .map_err(|error| EdgeError::WalCorrupt(format!("decode result WAL record: {error}")))?;
        if durable_batch.schema_version != "edge-result-wal/v1"
            || durable_batch.stage != ResultWalStage::BatchDurable as i32
            || durable_batch.result_batch_wal_sequence != batch_item.sequence
        {
            return Err(EdgeError::WalCorrupt(
                "result WAL does not start with a valid durable batch".into(),
            ));
        }
        let batch = durable_batch.result_batch.ok_or_else(|| {
            EdgeError::WalCorrupt("durable result stage lacks result batch".into())
        })?;
        if batch
            .records
            .iter()
            .any(|record| record.result_wal_sequence != batch_item.sequence)
        {
            return Err(EdgeError::WalCorrupt(
                "result WAL sequence fence mismatch".into(),
            ));
        }
        if canonical_result_batch_digest(&batch) != batch.batch_digest {
            return Err(EdgeError::WalCorrupt(
                "durable result batch digest mismatch".into(),
            ));
        }
        let (ack, result_checkpoint) = if let Some(ack_item) = replay.get(1) {
            let durable_ack =
                ResultWalRecord::decode(ack_item.payload.as_slice()).map_err(|error| {
                    EdgeError::WalCorrupt(format!("decode canonical ACK WAL record: {error}"))
                })?;
            if durable_ack.schema_version != "edge-result-wal/v1"
                || durable_ack.stage != ResultWalStage::CanonicalAckDurable as i32
                || durable_ack.result_batch_wal_sequence != batch_item.sequence
                || durable_ack.result_batch.is_some()
            {
                return Err(EdgeError::WalCorrupt(
                    "canonical ACK WAL fence mismatch".into(),
                ));
            }
            let ack = durable_ack.canonical_ack.ok_or_else(|| {
                EdgeError::WalCorrupt("canonical ACK stage lacks ACK batch".into())
            })?;
            validate_canonical_acks(&batch, &ack)?;
            (ack, ack_item.sequence)
        } else {
            let ack = self.sink.commit_results(batch.clone()).await?;
            let ack_sequence = self.result_wal.next_sequence();
            let durable_ack = ResultWalRecord {
                schema_version: "edge-result-wal/v1".into(),
                stage: ResultWalStage::CanonicalAckDurable as i32,
                result_batch: None,
                canonical_ack: Some(ack.clone()),
                result_batch_wal_sequence: batch_item.sequence,
                recorded_at_unix_ms: unix_ms()?,
                reason_code: "POSTGRESQL_EVENT_COMMIT_ACK_DURABLE".into(),
            };
            let appended = self.result_wal.append_message(&durable_ack)?;
            if appended != ack_sequence {
                return Err(EdgeError::precondition(
                    "WAL_SEQUENCE_CONFLICT",
                    "predicted canonical ACK WAL sequence changed",
                ));
            }
            (ack, ack_sequence)
        };
        if canonical_ack_batch_digest(&ack) != ack.ack_batch_digest {
            return Err(EdgeError::WalCorrupt(
                "durable canonical ACK digest mismatch".into(),
            ));
        }
        let source_checkpoint = batch
            .records
            .iter()
            .map(|record| record.source_wal_sequence)
            .max()
            .unwrap_or_default();
        if self
            .pending_digest_acks
            .front()
            .is_some_and(|(sequence, _)| *sequence <= source_checkpoint)
        {
            return Err(EdgeError::precondition(
                "SOURCE_ACK_PENDING",
                "digest ACK must finish before source checkpoint crosses its record",
            ));
        }
        let input_checkpoint = batch
            .records
            .iter()
            .map(|record| record.input_wal_sequence)
            .max()
            .unwrap_or_default();
        // Canonical Event commit ACK was validated by ControlSink before any
        // source/input/result checkpoint can advance.
        self.source_wal.checkpoint(source_checkpoint)?;
        self.input_wal.checkpoint(input_checkpoint)?;
        self.result_wal.checkpoint(result_checkpoint)?;
        for record in &batch.records {
            self.known_input_ids.remove(&record.input_id);
        }
        Ok(())
    }

    async fn persist_digest_hint(
        &mut self,
        hint: crate::contract::p4::DigestList,
    ) -> EdgeResult<()> {
        if self.pending_digest_acks.len() == self.config.limits.pending_digest_ack_queue {
            self.persist_hint_gap("digest-ack-queue-full")?;
            return Err(EdgeError::exhausted(
                "DIGEST_ACK_QUEUE_FULL",
                "durable digest acknowledgement queue is full",
            ));
        }
        let encoded = hint.encode_to_vec();
        let now_ms = unix_ms()?;
        let supplemental = SupplementalHint {
            target_id: self.assignment.target_id.clone(),
            source_runtime_epoch: self
                .assignment
                .fence
                .as_ref()
                .map_or_else(String::new, |fence| fence.actor_runtime_epoch.clone()),
            kind: 1,
            digest_id: hint.digest_id,
            list_id: hint.list_id,
            payload_digest: digest::sha256(&encoded),
            payload_bytes: encoded.len() as u64,
            received_at_unix_ms: now_ms,
        };
        let record = SourceWalRecord {
            schema_version: "edge-source-wal/v1".into(),
            stage: SourceWalStage::DigestHintDurable as i32,
            hint: Some(supplemental.clone()),
            recorded_at_unix_ms: now_ms,
            reason_code: "DIGEST_HINT_DURABLE".into(),
            source_runtime_epoch: supplemental.source_runtime_epoch.clone(),
            ..SourceWalRecord::default()
        };
        let sequence = self.source_wal.append_message(&record)?;
        self.digest_hints_since_snapshot = self.digest_hints_since_snapshot.saturating_add(1);
        self.pending_digest_acks.push_back((sequence, supplemental));
        self.flush_digest_acks().await
    }

    async fn flush_digest_acks(&mut self) -> EdgeResult<()> {
        let Some((durable_sequence, hint)) = self.pending_digest_acks.front().cloned() else {
            return Ok(());
        };
        self.session
            .as_ref()
            .ok_or_else(|| EdgeError::ActorUnavailable("P4 session missing".into()))?
            .acknowledge_digest(hint.digest_id, hint.list_id)
            .await?;
        let record = SourceWalRecord {
            schema_version: "edge-source-wal/v1".into(),
            stage: SourceWalStage::DigestHintAcked as i32,
            hint: Some(hint.clone()),
            recorded_at_unix_ms: unix_ms()?,
            operation_wal_sequence: durable_sequence,
            reason_code: "DIGEST_HINT_ACKED".into(),
            source_runtime_epoch: hint.source_runtime_epoch,
            ..SourceWalRecord::default()
        };
        self.source_wal.append_message(&record)?;
        self.pending_digest_acks.pop_front();
        Ok(())
    }

    fn persist_packet_hint(&mut self, packet: &crate::contract::p4::PacketIn) -> EdgeResult<()> {
        if packet.payload.len() > 2048 {
            return Err(EdgeError::exhausted(
                "PACKET_IN_TOO_LARGE",
                "supplemental PacketIn payload exceeds profile",
            ));
        }
        let encoded = packet.encode_to_vec();
        let now_ms = unix_ms()?;
        let record = SourceWalRecord {
            schema_version: "edge-source-wal/v1".into(),
            stage: SourceWalStage::PacketHintDurable as i32,
            hint: Some(SupplementalHint {
                target_id: self.assignment.target_id.clone(),
                source_runtime_epoch: self
                    .assignment
                    .fence
                    .as_ref()
                    .map_or_else(String::new, |fence| fence.actor_runtime_epoch.clone()),
                kind: 2,
                digest_id: 0,
                list_id: 0,
                payload_digest: digest::sha256(&encoded),
                payload_bytes: encoded.len() as u64,
                received_at_unix_ms: now_ms,
            }),
            recorded_at_unix_ms: now_ms,
            reason_code: "PACKET_HINT_DURABLE".into(),
            ..SourceWalRecord::default()
        };
        self.source_wal.append_message(&record)?;
        self.packet_hints_since_snapshot = self.packet_hints_since_snapshot.saturating_add(1);
        Ok(())
    }

    fn persist_hint_gap(&mut self, kind: &str) -> EdgeResult<()> {
        let record = SourceWalRecord {
            schema_version: "edge-source-wal/v1".into(),
            stage: SourceWalStage::SourceGapDurable as i32,
            recorded_at_unix_ms: unix_ms()?,
            reason_code: format!("SUPPLEMENTAL_HINT_DROPPED:{kind}"),
            source_runtime_epoch: self
                .assignment
                .fence
                .as_ref()
                .map_or_else(String::new, |fence| fence.actor_runtime_epoch.clone()),
            ..SourceWalRecord::default()
        };
        self.source_wal.append_message(&record)?;
        self.hint_drops_since_snapshot = self.hint_drops_since_snapshot.saturating_add(1);
        Ok(())
    }

    fn configure_rule_observations(
        &mut self,
        request: ConfigureRuleObservationsRequest,
    ) -> EdgeResult<PublishAck> {
        if request.schema_version != "p4-rule-observation-config/v1" {
            return Err(EdgeError::UnknownMajor(request.schema_version));
        }
        self.verify_target_fence(&request.target_id, request.fence.as_ref(), None)?;
        if !request.replace_all {
            return Err(EdgeError::precondition(
                "OBSERVATION_REPLACE_ALL_REQUIRED",
                "first release accepts only a complete canonical replacement",
            ));
        }
        if request.observation_epoch != self.assignment.observation_epoch
            || request.reset_epoch != self.assignment.reset_epoch
        {
            return Err(EdgeError::precondition(
                "OBSERVATION_EPOCH_MISMATCH",
                "Go-owned observation/reset epoch differs from assignment",
            ));
        }
        if request.rules.len()
            > self.config.limits.baseline_rules + self.config.limits.overlay_rules
        {
            return Err(EdgeError::exhausted(
                "OBSERVATION_LIMIT_EXCEEDED",
                "canonical observation set exceeds profile",
            ));
        }
        digest::validate_sha256(&request.configuration_digest, "configuration_digest")?;
        let mut canonical = request.clone();
        canonical.configuration_digest.clear();
        canonical.trace_id.clear();
        if digest::message_sha256(&canonical) != request.configuration_digest {
            return Err(EdgeError::precondition(
                "OBSERVATION_CONFIG_DIGEST_CONFLICT",
                "observation configuration digest mismatch",
            ));
        }
        let fence = request
            .fence
            .clone()
            .ok_or_else(|| EdgeError::invalid("fence", "observation fence is required"))?;
        let mut previous_rule_id: Option<&str> = None;
        let mut replacement = Vec::with_capacity(request.rules.len());
        for rule in &request.rules {
            for (field, value) in [
                ("effect_intent_id", rule.effect_intent_id.as_str()),
                ("operation_id", rule.operation_id.as_str()),
                ("rule_id", rule.rule_id.as_str()),
            ] {
                digest::validate_identity(value, field)?;
            }
            if previous_rule_id.is_some_and(|previous| previous >= rule.rule_id.as_str()) {
                return Err(EdgeError::invalid(
                    "rules",
                    "canonical observation rules must be strictly sorted by rule_id",
                ));
            }
            previous_rule_id = Some(rule.rule_id.as_str());
            digest::validate_sha256(&rule.canonical_entry_digest, "canonical_entry_digest")?;
            digest::validate_identity(&rule.entity_id, "entity_id")?;
            digest::validate_sha256(
                &rule.match_priority_action_digest,
                "match_priority_action_digest",
            )?;
            let entity = Entity::decode(rule.canonical_entity.as_slice()).map_err(|error| {
                EdgeError::invalid(
                    "canonical_entity",
                    format!("protobuf decode failed: {error}"),
                )
            })?;
            if firewall::canonical_entity_digest(&entity)? != rule.canonical_entry_digest {
                return Err(EdgeError::precondition(
                    "OBSERVATION_ENTRY_DIGEST_CONFLICT",
                    "canonical observation entity digest mismatch",
                ));
            }
            let entry = match entity.entity.as_ref() {
                Some(entity::Entity::TableEntry(entry)) => entry,
                _ => {
                    return Err(EdgeError::invalid(
                        "canonical_entity",
                        "observation entity must be a table entry",
                    ));
                }
            };
            let expected_counter = match rule.table_id {
                firewall::table_id::RESPONSE_OVERLAY => {
                    firewall::direct_counter_id::RESPONSE_OVERLAY
                }
                firewall::table_id::BASELINE_0 if rule.bank == 0 => {
                    firewall::direct_counter_id::BASELINE_0
                }
                firewall::table_id::BASELINE_1 if rule.bank == 1 => {
                    firewall::direct_counter_id::BASELINE_1
                }
                _ => {
                    return Err(EdgeError::precondition(
                        "OBSERVATION_TABLE_UNSUPPORTED",
                        "table/bank is outside the qualified rule-observation profile",
                    ));
                }
            };
            if entry.table_id != rule.table_id || rule.direct_counter_id != expected_counter {
                return Err(EdgeError::precondition(
                    "OBSERVATION_COUNTER_MISMATCH",
                    "entity table or direct-counter identity differs from profile",
                ));
            }
            let expires_at_unix_ms = if rule.table_id == firewall::table_id::RESPONSE_OVERLAY {
                if rule.expires_at_unix_ms <= 0 {
                    return Err(EdgeError::invalid(
                        "expires_at_unix_ms",
                        "overlay observation requires durable positive expiry",
                    ));
                }
                Some(rule.expires_at_unix_ms)
            } else {
                if rule.expires_at_unix_ms != 0 {
                    return Err(EdgeError::invalid(
                        "expires_at_unix_ms",
                        "baseline observation must not carry incident TTL",
                    ));
                }
                None
            };
            replacement.push(ObservedRule {
                entity,
                target_id: request.target_id.clone(),
                fence: fence.clone(),
                effect_intent_id: rule.effect_intent_id.clone(),
                operation_id: rule.operation_id.clone(),
                rule_id: rule.rule_id.clone(),
                canonical_entry_digest: rule.canonical_entry_digest.clone(),
                entity_id: rule.entity_id.clone(),
                match_priority_action_digest: rule.match_priority_action_digest.clone(),
                table_id: rule.table_id,
                direct_counter_id: rule.direct_counter_id,
                bank: rule.bank,
                expires_at_unix_ms,
            });
        }
        self.observed_rules = replacement;
        self.expired_overlay_rules.retain(|rule_id| {
            request.rules.iter().any(|rule| {
                rule.rule_id == *rule_id && rule.table_id == firewall::table_id::RESPONSE_OVERLAY
            })
        });
        Ok(PublishAck {
            status: "accepted".into(),
            identity: request.target_id,
            digest: request.configuration_digest,
            reason_code: "CANONICAL_OBSERVATION_SET_REPLACED".into(),
            status_code: PublishStatus::Accepted as i32,
        })
    }

    async fn sweep_rule_observations(&mut self) -> EdgeResult<()> {
        if self.observed_rules.is_empty() || self.session.is_none() {
            return Ok(());
        }
        let rules = self.observed_rules.clone();
        let queries: Vec<Entity> = rules
            .iter()
            .map(|rule| firewall::direct_counter_query(&rule.entity))
            .collect::<EdgeResult<_>>()?;
        let observed = self.read_batched(queries).await?;
        if observed.len() != rules.len() {
            return Err(EdgeError::precondition(
                "COUNTER_READBACK_MISSING",
                "direct counter response cardinality mismatch",
            ));
        }
        self.sample_sequence = self.sample_sequence.saturating_add(1);
        let read_started = unix_ms()?;
        let mut observations = Vec::with_capacity(rules.len());
        let mut expired_rule_ids = BTreeSet::new();
        for (rule, value) in rules.iter().zip(&observed) {
            let (packets, bytes) = firewall::counter_value(value)?;
            let (eligible_counter, eligible_index) =
                if rule.table_id == firewall::table_id::RESPONSE_OVERLAY {
                    (firewall::counter_id::RESPONSE_ELIGIBLE, 0)
                } else {
                    (
                        firewall::counter_id::BASELINE_ELIGIBLE,
                        i64::from(rule.bank),
                    )
                };
            let eligible = self
                .session_mut()?
                .read(vec![firewall::counter_query(
                    eligible_counter,
                    eligible_index,
                )])
                .await?;
            let (eligible_packets, eligible_bytes) = single_counter(&eligible)?;
            let expired = rule
                .expires_at_unix_ms
                .is_some_and(|expiry| expiry <= read_started);
            if expired {
                expired_rule_ids.insert(rule.rule_id.clone());
            }
            observations.push(RuleObservation {
                target_id: rule.target_id.clone(),
                fence: Some(rule.fence.clone()),
                effect_intent_id: rule.effect_intent_id.clone(),
                operation_id: rule.operation_id.clone(),
                rule_id: rule.rule_id.clone(),
                canonical_entry_digest: rule.canonical_entry_digest.clone(),
                table_id: rule.table_id,
                direct_counter_id: rule.direct_counter_id,
                observation_epoch: self.assignment.observation_epoch,
                reset_epoch: self.assignment.reset_epoch,
                sample_sequence: self.sample_sequence,
                read_started_at_unix_ms: read_started,
                read_completed_at_unix_ms: unix_ms()?,
                cumulative: Some(CounterValue { packets, bytes }),
                eligible_cumulative: Some(CounterValue {
                    packets: eligible_packets,
                    bytes: eligible_bytes,
                }),
                installation_readback: "exact".into(),
                quality: if expired { "stale" } else { "valid" }.into(),
                quality_reasons: if expired {
                    vec!["RULE_EXPIRED_AWAITING_DURABLE_DELETE_INTENT".into()]
                } else {
                    vec!["NONE".into()]
                },
                trace_id: format!(
                    "rule:{}:{}",
                    self.assignment.target_id, self.sample_sequence
                ),
                installation_status: InstallationReadbackStatus::Exact as i32,
                quality_code: if expired {
                    DataQuality::Stale
                } else {
                    DataQuality::Valid
                } as i32,
                expires_at_unix_ms: rule.expires_at_unix_ms.unwrap_or_default(),
                entity_id: rule.entity_id.clone(),
                sampling_coverage_ppm: 1_000_000,
                match_priority_action_digest: rule.match_priority_action_digest.clone(),
            });
        }
        let mut batch = RuleObservationBatch {
            schema_version: "p4-rule-observation-batch/v1".into(),
            batch_id: format!(
                "observation:{}:{}",
                self.assignment.target_id, self.sample_sequence
            ),
            observations,
            batch_digest: String::new(),
        };
        batch.batch_digest = observation_batch_digest(&batch);
        match self.sink.publish_observations(batch.clone()).await {
            Ok(_) => {
                self.expired_overlay_rules
                    .extend(expired_rule_ids.iter().cloned());
                self.observed_rules
                    .retain(|rule| !expired_rule_ids.contains(&rule.rule_id));
                Ok(())
            }
            Err(error) => {
                if self.observation_backlog.len() == self.config.limits.observation_batches {
                    return Err(EdgeError::exhausted(
                        "OBSERVATION_QUEUE_FULL",
                        "bounded observation backlog is full",
                    ));
                }
                self.observation_backlog.push_back(batch);
                self.expired_overlay_rules
                    .extend(expired_rule_ids.iter().cloned());
                self.observed_rules
                    .retain(|rule| !expired_rule_ids.contains(&rule.rule_id));
                Err(error)
            }
        }
    }

    async fn flush_observation_backlog(&mut self) {
        let Some(batch) = self.observation_backlog.front().cloned() else {
            return;
        };
        if self.sink.publish_observations(batch).await.is_ok() {
            self.observation_backlog.pop_front();
        }
    }

    async fn publish_status(&mut self) -> EdgeResult<()> {
        let mut batch = TargetStatusBatch {
            schema_version: "edge-target-status-batch/v1".into(),
            targets: vec![self.status.borrow().clone()],
            trace_id: format!("status:{}", self.assignment.target_id),
            batch_id: format!("status:{}:{}", self.assignment.target_id, unix_ms()?),
            batch_digest: String::new(),
        };
        batch.batch_digest = digest::message_sha256(&batch);
        self.sink.publish_status(batch).await.map(|_| ())
    }

    fn require_write_ready(&self) -> EdgeResult<()> {
        if self.wal_retention_hold {
            return Err(EdgeError::precondition(
                "WAL_RETENTION_EXPIRED",
                "target is fenced by an expired uncheckpointed WAL record",
            ));
        }
        if self.recovered_effect.is_some() {
            return Err(EdgeError::precondition(
                "EFFECT_RECONCILE_REQUIRED",
                "a prior write-started operation requires exact readback",
            ));
        }
        if !self.lease_valid() {
            return Err(EdgeError::precondition(
                "LEASE_EXPIRED",
                "target lease expired",
            ));
        }
        let session = self
            .session
            .as_ref()
            .ok_or_else(|| EdgeError::precondition("NOT_PRIMARY", "P4 session missing"))?;
        if self.state != ActorState::Primary || !session.is_primary() {
            return Err(EdgeError::precondition(
                "NOT_PRIMARY",
                "target actor is not current primary",
            ));
        }
        if !session.pipeline_exact() {
            return Err(EdgeError::precondition(
                "PIPELINE_DRIFT",
                "pipeline identity is not exact",
            ));
        }
        Ok(())
    }

    fn verify_intent_fence(&self, intent: &EffectIntent) -> EdgeResult<()> {
        self.verify_target_fence(&intent.target_id, intent.fence.as_ref(), None)?;
        let now_ms = unix_ms()?;
        if intent.deadline_unix_ms <= now_ms {
            return Err(EdgeError::Deadline("effect deadline elapsed".into()));
        }
        if intent.kind == crate::contract::edge::EffectKind::OverlayUpsert as i32 {
            for rule in &intent.overlay_rules {
                if rule.expires_at_unix_ms <= now_ms {
                    return Err(EdgeError::precondition(
                        "OVERLAY_EXPIRED",
                        "response overlay expiry is not in the future",
                    ));
                }
                if rule.expires_at_unix_ms.saturating_sub(now_ms) > 1_800_000 {
                    return Err(EdgeError::precondition(
                        "OVERLAY_TTL_EXCEEDED",
                        "first-release response overlay TTL exceeds 1800 seconds",
                    ));
                }
            }
        }
        firewall::validate_intent(intent)
    }

    fn verify_target_fence(
        &self,
        target_id: &str,
        fence: Option<&Fence>,
        lease_id: Option<&str>,
    ) -> EdgeResult<()> {
        if target_id != self.assignment.target_id {
            return Err(EdgeError::precondition(
                "TARGET_FENCE_MISMATCH",
                "request target differs from actor target",
            ));
        }
        if fence != self.assignment.fence.as_ref() {
            return Err(EdgeError::precondition(
                "ASSIGNMENT_STALE",
                "request fence differs from actor assignment",
            ));
        }
        if let Some(lease_id) = lease_id
            && lease_id != self.assignment.lease_id
        {
            return Err(EdgeError::precondition(
                "LEASE_ID_MISMATCH",
                "lease identity differs from assignment",
            ));
        }
        Ok(())
    }

    fn lease_valid(&self) -> bool {
        Instant::now() < self.lease_deadline && self.state != ActorState::Revoked
    }

    fn session_mut(&mut self) -> EdgeResult<&mut P4Session> {
        self.session
            .as_mut()
            .ok_or_else(|| EdgeError::ActorUnavailable("P4 session missing".into()))
    }

    fn verify_wal_retention(&self) -> EdgeResult<()> {
        for wal in [
            &self.source_wal,
            &self.input_wal,
            &self.result_wal,
            &self.journal,
            &self.route_journal,
        ] {
            wal.check_retention()?;
        }
        Ok(())
    }

    async fn read_batched(&mut self, queries: Vec<Entity>) -> EdgeResult<Vec<Entity>> {
        let mut result = Vec::new();
        for batch in queries.chunks(self.config.limits.p4_entities_per_read) {
            result.extend(self.session_mut()?.read(batch.to_vec()).await?);
        }
        Ok(result)
    }

    fn target_reply(&self, trace_id: &str) -> TargetReply {
        TargetReply {
            target_id: self.assignment.target_id.clone(),
            state: self.state as i32,
            actor_runtime_epoch: self
                .assignment
                .fence
                .as_ref()
                .map_or_else(String::new, |fence| fence.actor_runtime_epoch.clone()),
            reason_code: self.reason_code.clone(),
            trace_id: trace_id.into(),
        }
    }

    fn update_status(&self) {
        let session = self.session.as_ref();
        let (freshness, freshness_code) = self.freshness();
        let observed_at = unix_ms().unwrap_or_default();
        let capacity_raw = serde_json::to_vec(&self.config.limits).unwrap_or_default();
        let expected_pipeline = self.assignment.expected_pipeline.as_ref();
        let mut status = TargetStatus {
            target_id: self.assignment.target_id.clone(),
            actor_state: self.state as i32,
            fence: self.assignment.fence.clone(),
            lease_valid: self.lease_valid(),
            p4_connected: session.is_some(),
            primary: session.is_some_and(P4Session::is_primary),
            pipeline_exact: session.is_some_and(P4Session::pipeline_exact),
            pipeline_digest: session.map_or_else(String::new, |value| {
                value.observed_pipeline().device_config_digest.clone()
            }),
            high_priority_queue_depth: self.commands.len() as u64,
            telemetry_queue_depth: self.pending_digest_acks.len() as u64,
            observation_queue_depth: self.observation_backlog.len() as u64,
            source_wal_bytes: self.source_wal.stats().bytes,
            input_wal_bytes: self.input_wal.stats().bytes,
            result_wal_bytes: self.result_wal.stats().bytes,
            last_successful_read_unix_ms: self.last_successful_read_ms,
            freshness: freshness.into(),
            reason_code: self.reason_code.clone(),
            expired_overlay_observations: self.expired_overlay_rules.len() as u64,
            freshness_code: freshness_code as i32,
            p4info_digest: expected_pipeline
                .map_or_else(String::new, |value| value.p4info_digest.clone()),
            capacity_digest: digest::sha256(&capacity_raw),
            capacity_available: self.commands.len() < self.config.limits.actor_high_queue,
            observed_at_unix_ms: observed_at,
            expires_at_unix_ms: observed_at.saturating_add(
                i64::try_from(
                    self.config
                        .limits
                        .telemetry_poll_interval_ms
                        .saturating_mul(3),
                )
                .unwrap_or(i64::MAX),
            ),
            profile_digest: expected_pipeline
                .map_or_else(String::new, |value| value.profile_digest.clone()),
            observation_id: format!("status:{}:{}", self.assignment.target_id, observed_at),
            observation_digest: String::new(),
        };
        status.observation_digest = digest::message_sha256(&status);
        let _ = self.status.send(status);
    }

    fn freshness(&self) -> (&'static str, FreshnessStatus) {
        if self.last_successful_read_ms == 0 {
            return ("not_observed", FreshnessStatus::NotObserved);
        }
        let now_ms = match unix_ms() {
            Ok(value) => value,
            Err(_) => return ("gap", FreshnessStatus::Gap),
        };
        let stale_after = self
            .config
            .limits
            .telemetry_poll_interval_ms
            .saturating_mul(3);
        let stale_after = i64::try_from(stale_after).unwrap_or(i64::MAX);
        if now_ms.saturating_sub(self.last_successful_read_ms) <= stale_after {
            ("fresh", FreshnessStatus::Fresh)
        } else {
            ("stale", FreshnessStatus::Stale)
        }
    }
}

fn ensure_target_dir(path: &Path) -> EdgeResult<()> {
    if path.exists() {
        let metadata = std::fs::symlink_metadata(path)
            .map_err(|source| EdgeError::io("stat target data directory", source))?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(EdgeError::precondition(
                "TARGET_DATA_PATH_INVALID",
                "target data path must be a non-symlink directory",
            ));
        }
        return Ok(());
    }
    std::fs::create_dir_all(path)
        .map_err(|source| EdgeError::io("create target data directory", source))
}

fn lease_deadline(assignment: &TargetAssignment) -> EdgeResult<Instant> {
    let now_ms = unix_ms()?;
    if assignment.issued_at_unix_ms > now_ms.saturating_add(5_000)
        || assignment.expires_at_unix_ms <= now_ms
    {
        return Err(EdgeError::precondition(
            "LEASE_EXPIRED",
            "assignment lease is not currently valid",
        ));
    }
    let duration = u64::try_from(assignment.expires_at_unix_ms - now_ms)
        .map_err(|_| EdgeError::invalid("assignment.expires_at", "negative duration"))?;
    if !(1_000..=300_000).contains(&duration) {
        return Err(EdgeError::invalid(
            "assignment.expires_at",
            "remaining lease must be in [1000,300000] ms",
        ));
    }
    Ok(Instant::now() + Duration::from_millis(duration))
}

fn coverage_ppm(observed: u64, expected: u64) -> u32 {
    if expected == 0 {
        return 1_000_000;
    }
    let scaled = u128::from(observed)
        .saturating_mul(1_000_000)
        .checked_div(u128::from(expected))
        .unwrap_or_default()
        .min(1_000_000);
    u32::try_from(scaled).unwrap_or(1_000_000)
}

fn same_committed_binding_request(left: &ResumeRouteRequest, right: &ResumeRouteRequest) -> bool {
    let mut left = left.clone();
    let mut right = right.clone();
    left.trace_id.clear();
    right.trace_id.clear();
    left == right
}

fn committed_binding_matches_route(
    request: &ResumeRouteRequest,
    route: &crate::contract::edge::InferenceRoute,
) -> bool {
    request.schema_version == "inference-committed-binding/v1"
        && request.shard_id == route.shard_id
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
        && request.binding_digest == route.binding_digest
        && request.deadline_unix_ms > 0
}

fn validate_route_wal_record(record: &RouteWalRecord) -> EdgeResult<()> {
    if record.schema_version != "edge-route-wal/v1" {
        return Err(EdgeError::WalCorrupt(
            "unknown route journal schema major".into(),
        ));
    }
    let stage = RouteWalStage::try_from(record.stage)
        .map_err(|_| EdgeError::WalCorrupt("unknown route WAL stage".into()))?;
    if stage == RouteWalStage::Unspecified
        || record.proposed_route_epoch == 0
        || record.recorded_at_unix_ms <= 0
        || record.reason_code.is_empty()
        || record.trace_id.is_empty()
    {
        return Err(EdgeError::WalCorrupt(
            "route journal has an invalid stage, epoch, timestamp, or identity".into(),
        ));
    }
    match (
        stage,
        record.route.as_ref(),
        record.committed_binding_handshake.as_ref(),
    ) {
        (RouteWalStage::Prepared, Some(route), Some(handshake))
            if record.proposed_route_epoch > route.route_epoch
                && committed_binding_matches_route(handshake, route) => {}
        (RouteWalStage::Prepared, Some(route), None)
            if record.proposed_route_epoch > route.route_epoch => {}
        (RouteWalStage::Prepared, None, None) => {}
        (RouteWalStage::BindingReady, Some(route), None)
            if record.proposed_route_epoch == route.route_epoch => {}
        (RouteWalStage::CanonicalActive, Some(route), Some(handshake))
            if record.proposed_route_epoch == route.route_epoch
                && committed_binding_matches_route(handshake, route) => {}
        _ => {
            return Err(EdgeError::WalCorrupt(
                "route journal stage, route fence, or committed handshake conflict".into(),
            ));
        }
    }
    Ok(())
}

fn unix_ms() -> EdgeResult<i64> {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| EdgeError::precondition("CLOCK_INVALID", "system clock before Unix epoch"))?;
    i64::try_from(duration.as_millis())
        .map_err(|_| EdgeError::precondition("CLOCK_INVALID", "Unix milliseconds exceed i64"))
}

fn empty_status(assignment: &TargetAssignment, state: ActorState, reason: &str) -> TargetStatus {
    TargetStatus {
        target_id: assignment.target_id.clone(),
        actor_state: state as i32,
        fence: assignment.fence.clone(),
        reason_code: reason.into(),
        freshness: "not_observed".into(),
        freshness_code: FreshnessStatus::NotObserved as i32,
        ..TargetStatus::default()
    }
}

fn single_counter(entities: &[Entity]) -> EdgeResult<(u64, u64)> {
    if entities.len() != 1 {
        return Err(EdgeError::precondition(
            "COUNTER_READBACK_MISSING",
            "expected exactly one counter value",
        ));
    }
    firewall::counter_value(&entities[0])
}

fn source_stage_record(
    stage: SourceWalStage,
    recovery: &SourceRecovery,
    reason_code: &str,
) -> EdgeResult<SourceWalRecord> {
    Ok(SourceWalRecord {
        schema_version: "edge-source-wal/v1".into(),
        stage: stage as i32,
        snapshot_wal_sequence: recovery.snapshot_wal_sequence,
        recorded_at_unix_ms: unix_ms()?,
        frozen_bank: recovery.frozen_bank,
        new_active_bank: recovery.new_active_bank,
        previous_epoch: recovery.previous_epoch,
        new_epoch: recovery.new_epoch,
        operation_wal_sequence: recovery.operation_wal_sequence,
        source_sequence: recovery.source_sequence,
        counter_packets_before: recovery.counter_packets_before,
        counter_bytes_before: recovery.counter_bytes_before,
        reason_code: reason_code.into(),
        source_runtime_epoch: recovery.source_runtime_epoch.clone(),
        new_active_started_at_unix_ms: recovery.new_active_started_at_unix_ms,
        ..SourceWalRecord::default()
    })
}

fn source_recovery_for_record<'a>(
    recovery: &'a mut Option<SourceRecovery>,
    record: &SourceWalRecord,
) -> EdgeResult<&'a mut SourceRecovery> {
    let current = recovery.as_mut().ok_or_else(|| {
        EdgeError::WalCorrupt("source stage appears without a freeze operation".into())
    })?;
    if record.operation_wal_sequence != current.operation_wal_sequence
        || record.frozen_bank != current.frozen_bank
        || record.new_active_bank != current.new_active_bank
        || record.previous_epoch != current.previous_epoch
        || record.new_epoch != current.new_epoch
        || record.source_sequence != current.source_sequence
        || record.source_runtime_epoch != current.source_runtime_epoch
        || record.new_active_started_at_unix_ms != current.new_active_started_at_unix_ms
    {
        return Err(EdgeError::WalCorrupt(
            "source recovery operation fence mismatch".into(),
        ));
    }
    Ok(current)
}

fn table_key(entity: &Entity) -> EdgeResult<Entity> {
    let entry = match entity.entity.as_ref() {
        Some(entity::Entity::TableEntry(entry)) => entry,
        _ => return Err(EdgeError::invalid("entity", "expected table entry")),
    };
    Ok(Entity {
        entity: Some(entity::Entity::TableEntry(
            crate::contract::p4::TableEntry {
                table_id: entry.table_id,
                r#match: entry.r#match.clone(),
                priority: entry.priority,
                is_default_action: entry.is_default_action,
                ..crate::contract::p4::TableEntry::default()
            },
        )),
    })
}

fn observation_batch_digest(batch: &RuleObservationBatch) -> String {
    let mut canonical = batch.clone();
    canonical.batch_digest.clear();
    digest::message_sha256(&canonical)
}
