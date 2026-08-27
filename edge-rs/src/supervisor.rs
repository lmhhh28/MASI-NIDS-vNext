//! TargetSupervisor: stable assignment fences and isolated actor lifecycle.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{File, OpenOptions},
    io::Write as _,
    path::Path,
    sync::Arc,
};

use fs2::FileExt as _;
use serde::{Deserialize, Serialize};
use tokio::sync::{Mutex, RwLock};

use crate::{
    EdgeError, EdgeResult, actor,
    actor::ActorHandle,
    config::EdgeConfig,
    contract::edge::{
        AcknowledgeEffectRequest, ActorState, CommitRouteRequest, ConfigureRuleObservationsRequest,
        EdgeStatus, EffectIntent, EffectResult, PreflightEffectReply, PrepareRouteRequest,
        PublishAck, RenewTargetRequest, ResumeRouteRequest, RevokeTargetRequest, RouteReply,
        TargetAssignment, TargetReply, TargetStatus,
    },
    digest,
};

const LEDGER_SCHEMA: &str = "edge-assignment-ledger/v1";

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TargetFenceLedger {
    current_incarnation: String,
    retired_incarnations: BTreeSet<String>,
    max_assignment_generation: u64,
    max_election_ceiling: u64,
    #[serde(default)]
    last_election_floor: u64,
    #[serde(default)]
    last_lease_id: String,
    #[serde(default)]
    last_assignment_digest: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct AssignmentLedger {
    schema_version: String,
    targets: BTreeMap<String, TargetFenceLedger>,
}

impl Default for AssignmentLedger {
    fn default() -> Self {
        Self {
            schema_version: LEDGER_SCHEMA.into(),
            targets: BTreeMap::new(),
        }
    }
}

#[derive(Debug)]
struct SupervisorState {
    actors: BTreeMap<String, ActorHandle>,
    assignments: BTreeMap<String, TargetAssignment>,
    endpoint_owners: BTreeMap<(String, u64), String>,
    pending_targets: BTreeSet<String>,
    ledger: AssignmentLedger,
    accepting_assignments: bool,
}

/// Cloneable coordinator for the single Edge process.
#[derive(Clone, Debug)]
pub struct TargetSupervisor {
    config: Arc<EdgeConfig>,
    config_digest: Arc<String>,
    state: Arc<RwLock<SupervisorState>>,
    ledger_gate: Arc<Mutex<()>>,
    _process_lock: Arc<File>,
}

impl TargetSupervisor {
    /// Open the non-reusable assignment ledger and acquire the process writer lock.
    pub fn open(config: EdgeConfig, config_digest: String) -> EdgeResult<Self> {
        ensure_data_dir(&config.data_dir)?;
        let lock_path = config.data_dir.join("edge-process.lock");
        let process_lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(lock_path)
            .map_err(|source| EdgeError::io("open Edge process lock", source))?;
        process_lock.try_lock_exclusive().map_err(|error| {
            EdgeError::precondition(
                "EDGE_PROCESS_CONFLICT",
                format!("data root already has an Edge writer: {error}"),
            )
        })?;
        let ledger = read_ledger(&config.data_dir)?;
        Ok(Self {
            config: Arc::new(config),
            config_digest: Arc::new(config_digest),
            state: Arc::new(RwLock::new(SupervisorState {
                actors: BTreeMap::new(),
                assignments: BTreeMap::new(),
                endpoint_owners: BTreeMap::new(),
                pending_targets: BTreeSet::new(),
                ledger,
                accepting_assignments: true,
            })),
            ledger_gate: Arc::new(Mutex::new(())),
            _process_lock: Arc::new(process_lock),
        })
    }

    /// Validate, durably consume a generation/election range, then start one actor.
    pub async fn assign(&self, mut assignment: TargetAssignment) -> EdgeResult<TargetReply> {
        validate_assignment(&assignment, &self.config)?;
        let target_id = assignment.target_id.clone();
        let endpoint_key = (assignment.p4runtime_endpoint.clone(), assignment.device_id);
        {
            let mut state = self.state.write().await;
            if !state.accepting_assignments {
                return Err(EdgeError::precondition(
                    "ASSIGNMENT_LEDGER_UNAVAILABLE",
                    "supervisor stopped assignments after a durable-ledger failure",
                ));
            }
            if state.pending_targets.contains(&target_id) {
                return Err(EdgeError::precondition(
                    "TARGET_ALREADY_ASSIGNED",
                    "target assignment is already in progress",
                ));
            }
            if let Some(existing) = state.assignments.get(&target_id) {
                let existing_generation = existing
                    .fence
                    .as_ref()
                    .map_or(0, |fence| fence.target_assignment_generation);
                let incoming_generation = assignment
                    .fence
                    .as_ref()
                    .map_or(0, |fence| fence.target_assignment_generation);
                if incoming_generation == existing_generation
                    && existing.lease_id == assignment.lease_id
                {
                    return state
                        .actors
                        .get(&target_id)
                        .map(|actor| reply_from_status(actor.status(), &assignment.trace_id))
                        .ok_or_else(|| {
                            EdgeError::precondition(
                                "TARGET_ALREADY_ASSIGNED",
                                "idempotent assignment has no live actor",
                            )
                        });
                }
                let existing_state = state
                    .actors
                    .get(&target_id)
                    .and_then(|actor| ActorState::try_from(actor.status().actor_state).ok());
                if !matches!(
                    existing_state,
                    Some(ActorState::Revoked | ActorState::ReadOnly)
                ) {
                    return Err(EdgeError::precondition(
                        "TARGET_ALREADY_ASSIGNED",
                        "old actor must be revoked or lease-expired before handoff",
                    ));
                }
            }
            if state.actors.len() + state.pending_targets.len() >= self.config.limits.max_targets
                && !state.actors.contains_key(&target_id)
            {
                return Err(EdgeError::exhausted(
                    "TARGET_LIMIT_EXCEEDED",
                    "active plus pending targets reached the implementation bound",
                ));
            }
            if let Some(owner) = state.endpoint_owners.get(&endpoint_key)
                && owner != &target_id
            {
                return Err(EdgeError::precondition(
                    "ASSIGNMENT_CONFLICT",
                    "endpoint/device tuple already belongs to another target",
                ));
            }
            state.pending_targets.insert(target_id.clone());
            state
                .endpoint_owners
                .insert(endpoint_key.clone(), target_id.clone());
        }

        let actor_epoch = format!("actor:{}", uuid::Uuid::new_v4());
        assignment
            .fence
            .as_mut()
            .ok_or_else(|| EdgeError::invalid("assignment.fence", "fence required"))?
            .actor_runtime_epoch = actor_epoch;

        let ledger_result = self.consume_assignment_fence(&assignment).await;
        if let Err(error) = ledger_result {
            let mut state = self.state.write().await;
            state.pending_targets.remove(&target_id);
            state.endpoint_owners.remove(&endpoint_key);
            return Err(error);
        }

        // A handoff never overlaps two StreamChannels for the same target.
        // The old actor was already verified read-only/revoked above; close it
        // before the new higher-election actor starts. The durable ledger has
        // already consumed the new fence, so a subsequent failure remains
        // fail-closed and requires a strictly newer assignment.
        let prior_actor = {
            let state = self.state.read().await;
            state.actors.get(&target_id).cloned()
        };
        if let Some(prior) = prior_actor {
            prior.shutdown().await;
        }

        let actor = actor::spawn(assignment.clone(), (*self.config).clone()).await;
        let actor = match actor {
            Ok(actor) => actor,
            Err(error) => {
                let mut state = self.state.write().await;
                state.pending_targets.remove(&target_id);
                state.endpoint_owners.remove(&endpoint_key);
                return Err(error);
            }
        };
        let old_actor = {
            let mut state = self.state.write().await;
            state.pending_targets.remove(&target_id);
            let old = state.actors.insert(target_id.clone(), actor.clone());
            state.assignments.insert(target_id, assignment.clone());
            old
        };
        if let Some(old) = old_actor {
            old.shutdown().await;
        }
        Ok(reply_from_status(actor.status(), &assignment.trace_id))
    }

    /// Renew a target lease through its isolated actor.
    pub async fn renew(&self, request: RenewTargetRequest) -> EdgeResult<TargetReply> {
        self.actor(&request.target_id).await?.renew(request).await
    }

    /// Revoke target authority. The actor remains as read-only status until handoff.
    pub async fn revoke(&self, request: RevokeTargetRequest) -> EdgeResult<TargetReply> {
        self.actor(&request.target_id).await?.revoke(request).await
    }

    /// Run read-only effect preflight.
    pub async fn preflight(&self, intent: EffectIntent) -> EdgeResult<PreflightEffectReply> {
        self.actor(&intent.target_id).await?.preflight(intent).await
    }

    /// Execute a target-local effect.
    pub async fn execute(&self, intent: EffectIntent, token: String) -> EdgeResult<EffectResult> {
        self.actor(&intent.target_id)
            .await?
            .execute(intent, token)
            .await
    }

    /// Checkpoint an effect journal only after PostgreSQL CAS confirmation.
    pub async fn acknowledge_effect(
        &self,
        request: AcknowledgeEffectRequest,
    ) -> EdgeResult<PublishAck> {
        self.actor(&request.target_id)
            .await?
            .acknowledge_effect(request)
            .await
    }

    /// Withdraw/drain a target shard route.
    pub async fn prepare_route(&self, request: PrepareRouteRequest) -> EdgeResult<RouteReply> {
        self.actor(&request.shard_id)
            .await?
            .prepare_route(request)
            .await
    }

    /// Install/read back an exact route.
    pub async fn commit_route(&self, request: CommitRouteRequest) -> EdgeResult<RouteReply> {
        let shard = request
            .route
            .as_ref()
            .map(|route| route.shard_id.as_str())
            .ok_or_else(|| EdgeError::invalid("route", "route required"))?;
        self.actor(shard).await?.commit_route(request).await
    }

    /// Resume a Go-committed route.
    pub async fn resume_route(&self, request: ResumeRouteRequest) -> EdgeResult<RouteReply> {
        self.actor(&request.shard_id)
            .await?
            .resume_route(request)
            .await
    }

    /// Replace one target's complete Go-owned canonical observation set.
    pub async fn configure_rule_observations(
        &self,
        request: ConfigureRuleObservationsRequest,
    ) -> EdgeResult<PublishAck> {
        self.actor(&request.target_id)
            .await?
            .configure_rule_observations(request)
            .await
    }

    /// Process and per-target status are deliberately separate.
    pub async fn status(&self, target_id: &str, trace_id: &str) -> EdgeStatus {
        let state = self.state.read().await;
        let targets = if target_id.is_empty() {
            state.actors.values().map(ActorHandle::status).collect()
        } else {
            state
                .actors
                .get(target_id)
                .map(|actor| vec![actor.status()])
                .unwrap_or_default()
        };
        EdgeStatus {
            schema_version: "edge-status/v1".into(),
            edge_instance_id: self.config.edge_instance_id.clone(),
            process_live: true,
            accepting_assignments: state.accepting_assignments,
            targets,
            target_limit: self.config.limits.max_targets as u64,
            build_version: env!("CARGO_PKG_VERSION").into(),
            config_digest: (*self.config_digest).clone(),
            trace_id: trace_id.into(),
        }
    }

    /// Gracefully stop all target tasks.
    pub async fn shutdown(&self) {
        let actors: Vec<ActorHandle> = {
            let mut state = self.state.write().await;
            state.accepting_assignments = false;
            state.actors.values().cloned().collect()
        };
        for actor in actors {
            actor.shutdown().await;
        }
    }

    async fn actor(&self, target_id: &str) -> EdgeResult<ActorHandle> {
        self.state
            .read()
            .await
            .actors
            .get(target_id)
            .cloned()
            .ok_or_else(|| EdgeError::precondition("TARGET_NOT_ASSIGNED", "target actor absent"))
    }

    async fn consume_assignment_fence(&self, assignment: &TargetAssignment) -> EdgeResult<()> {
        let _gate = self.ledger_gate.lock().await;
        let fence = assignment
            .fence
            .as_ref()
            .ok_or_else(|| EdgeError::invalid("assignment.fence", "fence required"))?;
        let assignment_digest = assignment_recovery_digest(assignment);
        let ledger_snapshot = {
            let state = self.state.read().await;
            let mut candidate = state.ledger.clone();
            let target = candidate
                .targets
                .entry(assignment.target_id.clone())
                .or_default();
            if target
                .retired_incarnations
                .contains(&fence.target_control_incarnation_id)
            {
                return Err(EdgeError::precondition(
                    "ASSIGNMENT_STALE",
                    "retired target-control incarnation cannot reappear",
                ));
            }
            let same_incarnation = target.current_incarnation.is_empty()
                || target.current_incarnation == fence.target_control_incarnation_id;
            let exact_recovery = same_incarnation
                && fence.target_assignment_generation == target.max_assignment_generation
                && assignment.election_floor == target.last_election_floor
                && assignment.election_ceiling == target.max_election_ceiling
                && assignment.lease_id == target.last_lease_id
                && assignment_digest == target.last_assignment_digest;
            if exact_recovery {
                candidate
            } else {
                if !target.current_incarnation.is_empty()
                    && target.current_incarnation != fence.target_control_incarnation_id
                {
                    if fence.target_assignment_generation <= target.max_assignment_generation {
                        return Err(EdgeError::precondition(
                            "ASSIGNMENT_STALE",
                            "new target-control incarnation requires a higher assignment generation",
                        ));
                    }
                    target
                        .retired_incarnations
                        .insert(target.current_incarnation.clone());
                    target.current_incarnation = fence.target_control_incarnation_id.clone();
                } else if target.current_incarnation.is_empty() {
                    target.current_incarnation = fence.target_control_incarnation_id.clone();
                }
                if fence.target_assignment_generation <= target.max_assignment_generation {
                    return Err(EdgeError::precondition(
                        "ASSIGNMENT_STALE",
                        "assignment generation is not strictly increasing and is not exact recovery",
                    ));
                }
                if assignment.election_floor <= target.max_election_ceiling {
                    return Err(EdgeError::precondition(
                        "ELECTION_OUT_OF_RANGE",
                        "new election floor is not above the entire previous range",
                    ));
                }
                target.max_assignment_generation = fence.target_assignment_generation;
                target.last_election_floor = assignment.election_floor;
                target.max_election_ceiling = assignment.election_ceiling;
                target.last_lease_id = assignment.lease_id.clone();
                target.last_assignment_digest = assignment_digest;
                candidate
            }
        };
        if let Err(error) = write_ledger(&self.config.data_dir, &ledger_snapshot) {
            self.state.write().await.accepting_assignments = false;
            return Err(error);
        }
        self.state.write().await.ledger = ledger_snapshot;
        Ok(())
    }
}

fn assignment_recovery_digest(assignment: &TargetAssignment) -> String {
    let mut canonical = assignment.clone();
    canonical.issued_at_unix_ms = 0;
    canonical.expires_at_unix_ms = 0;
    canonical.trace_id.clear();
    if let Some(fence) = &mut canonical.fence {
        fence.actor_runtime_epoch.clear();
    }
    digest::message_sha256(&canonical)
}

fn validate_assignment(assignment: &TargetAssignment, config: &EdgeConfig) -> EdgeResult<()> {
    if assignment.schema_version != "target-assignment/v1" {
        return Err(EdgeError::UnknownMajor(assignment.schema_version.clone()));
    }
    for (field, value) in [
        ("target_id", assignment.target_id.as_str()),
        ("role", assignment.role.as_str()),
        ("lease_id", assignment.lease_id.as_str()),
        ("trace_id", assignment.trace_id.as_str()),
    ] {
        digest::validate_identity(value, field)?;
    }
    if assignment.role.len() > 64 {
        return Err(EdgeError::invalid("role", "role exceeds 64 bytes"));
    }
    let fence = assignment
        .fence
        .as_ref()
        .ok_or_else(|| EdgeError::invalid("assignment.fence", "fence is required"))?;
    digest::validate_identity(
        &fence.target_control_incarnation_id,
        "target_control_incarnation_id",
    )?;
    if fence.target_assignment_generation == 0
        || fence.application_generation == 0
        || fence.election_id_high != 0
    {
        return Err(EdgeError::invalid(
            "assignment.fence",
            "generation must be positive and first-release election high must be zero",
        ));
    }
    if assignment.election_floor == 0
        || assignment.election_ceiling < assignment.election_floor
        || assignment.election_ceiling - assignment.election_floor > 1_000_000
        || !(assignment.election_floor..=assignment.election_ceiling)
            .contains(&fence.election_id_low)
    {
        return Err(EdgeError::precondition(
            "ELECTION_OUT_OF_RANGE",
            "election ID/range is invalid or wider than 1,000,001 IDs",
        ));
    }
    let pipeline = assignment
        .expected_pipeline
        .as_ref()
        .ok_or_else(|| EdgeError::invalid("expected_pipeline", "pipeline identity is required"))?;
    if pipeline.p4runtime_api_version != "1.4.1"
        || pipeline.cookie == 0
        || pipeline.supported_write_atomicity.as_slice()
            != [crate::contract::edge::P4WriteAtomicity::ContinueOnError as i32]
    {
        return Err(EdgeError::precondition(
            "CAPABILITY_DRIFT",
            "target capability version/cookie/atomicity differs from first-release profile",
        ));
    }
    for (field, value) in [
        ("p4info_digest", pipeline.p4info_digest.as_str()),
        (
            "device_config_digest",
            pipeline.device_config_digest.as_str(),
        ),
        ("profile_digest", pipeline.profile_digest.as_str()),
    ] {
        digest::validate_sha256(value, field)?;
    }
    if assignment.observation_epoch == 0 || assignment.reset_epoch == 0 {
        return Err(EdgeError::invalid(
            "observation_epoch",
            "observation and reset epochs must be positive",
        ));
    }
    let tls = assignment
        .p4runtime_tls
        .as_ref()
        .ok_or_else(|| EdgeError::invalid("p4runtime_tls", "mTLS identity required"))?;
    digest::validate_identity(&tls.identity_ref, "p4runtime_tls.identity_ref")?;
    let registered_tls = config
        .client_identities
        .get(&tls.identity_ref)
        .ok_or_else(|| {
            EdgeError::precondition(
                "TLS_IDENTITY_UNKNOWN",
                "P4 credential identity_ref is absent from the local registry",
            )
        })?;
    if registered_tls.server_name != tls.server_name {
        return Err(EdgeError::TlsIdentityMismatch(
            "P4 server_name differs from registered credential SAN".into(),
        ));
    }
    if tls.identity_ref == config.control_sink.tls.identity_ref
        || tls.identity_ref == config.server_tls.identity_ref
    {
        return Err(EdgeError::precondition(
            "TLS_IDENTITY_REUSE",
            "P4 identity must be distinct from Edge server and Go sink",
        ));
    }
    if !assignment.p4runtime_endpoint.starts_with("https://") {
        return Err(EdgeError::precondition(
            "ENDPOINT_NOT_ALLOWED",
            "P4Runtime endpoint must use https",
        ));
    }
    // Full timestamp-to-monotonic validation is repeated by actor construction.
    if assignment.expires_at_unix_ms <= assignment.issued_at_unix_ms
        || assignment.expires_at_unix_ms - assignment.issued_at_unix_ms > 300_000
    {
        return Err(EdgeError::invalid(
            "assignment.lease",
            "lease interval must be positive and no greater than 300 seconds",
        ));
    }
    Ok(())
}

fn reply_from_status(status: TargetStatus, trace_id: &str) -> TargetReply {
    TargetReply {
        target_id: status.target_id,
        state: status.actor_state,
        actor_runtime_epoch: status
            .fence
            .as_ref()
            .map_or_else(String::new, |fence| fence.actor_runtime_epoch.clone()),
        reason_code: status.reason_code,
        trace_id: trace_id.into(),
    }
}

fn ensure_data_dir(path: &Path) -> EdgeResult<()> {
    if path.exists() {
        let metadata = std::fs::symlink_metadata(path)
            .map_err(|source| EdgeError::io("stat Edge data directory", source))?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(EdgeError::precondition(
                "EDGE_DATA_PATH_INVALID",
                "data root must be a non-symlink directory",
            ));
        }
        return Ok(());
    }
    std::fs::create_dir_all(path)
        .map_err(|source| EdgeError::io("create Edge data directory", source))
}

fn ledger_path(data_dir: &Path) -> std::path::PathBuf {
    data_dir.join("assignment-ledger.json")
}

fn read_ledger(data_dir: &Path) -> EdgeResult<AssignmentLedger> {
    let path = ledger_path(data_dir);
    if !path.exists() {
        return Ok(AssignmentLedger::default());
    }
    let metadata = std::fs::symlink_metadata(&path)
        .map_err(|source| EdgeError::io("stat assignment ledger", source))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() > 1_048_576 {
        return Err(EdgeError::precondition(
            "ASSIGNMENT_LEDGER_CORRUPT",
            "ledger must be a bounded regular non-symlink file",
        ));
    }
    let payload =
        std::fs::read(path).map_err(|source| EdgeError::io("read assignment ledger", source))?;
    let ledger: AssignmentLedger = serde_json::from_slice(&payload)
        .map_err(|error| EdgeError::WalCorrupt(format!("decode assignment ledger: {error}")))?;
    if ledger.schema_version != LEDGER_SCHEMA {
        return Err(EdgeError::UnknownMajor(ledger.schema_version));
    }
    Ok(ledger)
}

fn write_ledger(data_dir: &Path, ledger: &AssignmentLedger) -> EdgeResult<()> {
    let payload = serde_json::to_vec(ledger)
        .map_err(|error| EdgeError::WalCorrupt(format!("encode assignment ledger: {error}")))?;
    let temporary = data_dir.join(format!("assignment-ledger-{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .map_err(|source| EdgeError::io("create assignment ledger temporary", source))?;
    file.write_all(&payload)
        .and_then(|()| file.sync_all())
        .map_err(|source| EdgeError::io("write and fsync assignment ledger", source))?;
    std::fs::rename(&temporary, ledger_path(data_dir))
        .map_err(|source| EdgeError::io("atomically replace assignment ledger", source))?;
    File::open(data_dir)
        .and_then(|directory| directory.sync_all())
        .map_err(|source| EdgeError::io("fsync assignment ledger directory", source))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ledger_round_trip_is_strict() -> Result<(), Box<dyn std::error::Error>> {
        let temp = tempfile::tempdir()?;
        let mut ledger = AssignmentLedger::default();
        ledger.targets.insert(
            "target-1".into(),
            TargetFenceLedger {
                current_incarnation: "incarnation-1".into(),
                max_assignment_generation: 2,
                max_election_ceiling: 200,
                ..TargetFenceLedger::default()
            },
        );
        write_ledger(temp.path(), &ledger)?;
        let observed = read_ledger(temp.path())?;
        assert_eq!(
            2,
            observed
                .targets
                .get("target-1")
                .map_or(0, |target| target.max_assignment_generation)
        );
        Ok(())
    }
}
