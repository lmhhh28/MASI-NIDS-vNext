//! Sole per-target P4Runtime session, arbitration, bounded I/O, and readback.

use std::{collections::BTreeMap, sync::Arc, time::Duration};

use prost::Message as _;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{
    Request,
    transport::{Certificate, Channel, ClientTlsConfig as TonicClientTlsConfig, Identity},
};

use crate::{
    EdgeError, EdgeResult,
    config::{ClientTlsConfig, DeploymentTier, EndpointPolicyConfig, RuntimeLimits, read_pem},
    contract::{
        edge::{PipelineIdentity, TargetAssignment, TlsClientIdentity},
        p4::{
            CapabilitiesRequest, DigestList, DigestListAck, Entity,
            GetForwardingPipelineConfigRequest, MasterArbitrationUpdate, PacketIn, ReadRequest,
            Role, StreamMessageRequest, Uint128, Update, WriteRequest,
            get_forwarding_pipeline_config_request::ResponseType,
            p4_runtime_client::P4RuntimeClient, stream_message_request, stream_message_response,
            write_request::Atomicity,
        },
        p4info::P4Info,
    },
    digest,
    endpoint::resolve_endpoint,
};

/// Asynchronous events from the single StreamChannel.
#[derive(Debug)]
pub enum P4StreamEvent {
    /// Mastership update, including loss of primary.
    Arbitration(MasterArbitrationUpdate),
    /// Best-effort digest hint; ACK only after source WAL durability.
    Digest(DigestList),
    /// Best-effort PacketIn hint.
    Packet(PacketIn),
    /// Target-originated StreamChannel error.
    Error(String),
    /// Supplemental hint was deliberately dropped under bounded backpressure.
    HintDropped {
        /// Stable hint kind (`digest` or `packet-in`).
        kind: &'static str,
    },
    /// Transport closed or failed.
    Closed(String),
}

/// One target's sole production P4Runtime client and StreamChannel.
#[derive(Debug)]
pub struct P4Session {
    target_id: String,
    device_id: u64,
    role: String,
    election_id: Uint128,
    client: P4RuntimeClient<Channel>,
    stream_requests: mpsc::Sender<StreamMessageRequest>,
    high_stream_events: mpsc::Receiver<P4StreamEvent>,
    hint_stream_events: mpsc::Receiver<P4StreamEvent>,
    stream_task: tokio::task::JoinHandle<()>,
    primary: Arc<std::sync::atomic::AtomicBool>,
    pipeline_exact: bool,
    observed_pipeline: PipelineIdentity,
    limits: RuntimeLimits,
}

impl P4Session {
    /// Resolve, establish mTLS, check capabilities, and win exact arbitration.
    pub async fn connect(
        assignment: &TargetAssignment,
        tier: DeploymentTier,
        endpoint_policy: &EndpointPolicyConfig,
        identities: &BTreeMap<String, ClientTlsConfig>,
        limits: &RuntimeLimits,
    ) -> EdgeResult<Self> {
        let tls = assignment
            .p4runtime_tls
            .as_ref()
            .ok_or_else(|| EdgeError::invalid("p4runtime_tls", "mTLS identity is required"))?;
        let client_tls = contract_tls(tls, identities)?;
        let resolved = resolve_endpoint(
            &assignment.p4runtime_endpoint,
            &client_tls.server_name,
            tier,
            endpoint_policy,
            Duration::from_millis(limits.p4_connect_deadline_ms),
        )
        .await?;
        let identity = Identity::from_pem(
            read_pem(&client_tls.certificate_path, false)?,
            read_pem(&client_tls.private_key_path, true)?,
        );
        let ca = Certificate::from_pem(read_pem(&client_tls.ca_path, false)?);
        let tls_config = TonicClientTlsConfig::new()
            .ca_certificate(ca)
            .identity(identity)
            .domain_name(client_tls.server_name.clone());
        let endpoint = resolved
            .endpoint
            .tls_config(tls_config)
            .map_err(|error| EdgeError::TlsIdentityMismatch(error.to_string()))?;
        let channel = tokio::time::timeout(
            Duration::from_millis(limits.p4_connect_deadline_ms),
            endpoint.connect(),
        )
        .await
        .map_err(|_| EdgeError::Deadline("P4 mTLS connect".into()))?
        .map_err(|error| EdgeError::TlsIdentityMismatch(error.to_string()))?;
        let mut client = P4RuntimeClient::new(channel)
            .max_decoding_message_size(limits.p4_read_response_bytes)
            .max_encoding_message_size(limits.p4_read_response_bytes);

        let mut capability_request = Request::new(CapabilitiesRequest {});
        capability_request.set_timeout(Duration::from_millis(limits.p4_rpc_deadline_ms));
        let capability = tokio::time::timeout(
            Duration::from_millis(limits.p4_rpc_deadline_ms),
            client.capabilities(capability_request),
        )
        .await
        .map_err(|_| EdgeError::Deadline("P4 capabilities readback".into()))?
        .map_err(|error| EdgeError::Remote {
            code: "CAPABILITY_READ_FAILED",
            message: error.to_string(),
        })?
        .into_inner();
        let expected_pipeline = assignment.expected_pipeline.as_ref().ok_or_else(|| {
            EdgeError::invalid("expected_pipeline", "pipeline identity is required")
        })?;
        if capability.p4runtime_api_version != expected_pipeline.p4runtime_api_version {
            return Err(EdgeError::precondition(
                "CAPABILITY_DRIFT",
                format!(
                    "expected {}, observed {}",
                    expected_pipeline.p4runtime_api_version, capability.p4runtime_api_version
                ),
            ));
        }

        let election_id = Uint128 {
            high: assignment
                .fence
                .as_ref()
                .map_or(0, |fence| fence.election_id_high),
            low: assignment
                .fence
                .as_ref()
                .map_or(0, |fence| fence.election_id_low),
        };
        let (request_tx, request_rx) = mpsc::channel(limits.p4_stream_request_queue);
        let arbitration = MasterArbitrationUpdate {
            device_id: assignment.device_id,
            role: (assignment.role != "default").then(|| Role {
                id: 0,
                config: Vec::new(),
                name: assignment.role.clone(),
            }),
            election_id: Some(election_id),
            status: None,
        };
        request_tx
            .send(StreamMessageRequest {
                update: Some(stream_message_request::Update::Arbitration(arbitration)),
            })
            .await
            .map_err(|_| EdgeError::ActorUnavailable("P4 request queue closed".into()))?;
        let response = tokio::time::timeout(
            Duration::from_millis(limits.p4_arbitration_deadline_ms),
            client.stream_channel(ReceiverStream::new(request_rx)),
        )
        .await
        .map_err(|_| EdgeError::Deadline("P4 StreamChannel open".into()))?
        .map_err(|error| EdgeError::Remote {
            code: "P4_STREAM_OPEN_FAILED",
            message: error.to_string(),
        })?;
        let mut response_stream = response.into_inner();
        let first = tokio::time::timeout(
            Duration::from_millis(limits.p4_arbitration_deadline_ms),
            response_stream.message(),
        )
        .await
        .map_err(|_| EdgeError::Deadline("P4 arbitration".into()))?
        .map_err(|error| EdgeError::Remote {
            code: "P4_STREAM_FAILED",
            message: error.to_string(),
        })?
        .ok_or_else(|| EdgeError::Remote {
            code: "P4_STREAM_CLOSED",
            message: "stream closed before arbitration".into(),
        })?;
        let observed_arbitration = match first.update {
            Some(stream_message_response::Update::Arbitration(update)) => update,
            _ => {
                return Err(EdgeError::precondition(
                    "ROLE_ELECTION_MISMATCH",
                    "first P4 stream response was not arbitration",
                ));
            }
        };
        validate_arbitration(
            &observed_arbitration,
            assignment.device_id,
            &assignment.role,
            &election_id,
        )?;
        let primary = Arc::new(std::sync::atomic::AtomicBool::new(true));
        let (high_event_tx, high_event_rx) = mpsc::channel(limits.actor_high_queue);
        let (hint_event_tx, hint_event_rx) = mpsc::channel(limits.p4_stream_response_queue);
        let task_primary = Arc::clone(&primary);
        let stream_task = tokio::spawn(async move {
            loop {
                match response_stream.message().await {
                    Ok(Some(message)) => {
                        let event = match message.update {
                            Some(stream_message_response::Update::Arbitration(update)) => {
                                let is_primary = update
                                    .status
                                    .as_ref()
                                    .is_some_and(|status| status.code == 0);
                                task_primary
                                    .store(is_primary, std::sync::atomic::Ordering::Release);
                                P4StreamEvent::Arbitration(update)
                            }
                            Some(stream_message_response::Update::Digest(digest)) => {
                                P4StreamEvent::Digest(digest)
                            }
                            Some(stream_message_response::Update::Packet(packet)) => {
                                P4StreamEvent::Packet(packet)
                            }
                            Some(stream_message_response::Update::Error(error)) => {
                                P4StreamEvent::Error(format!(
                                    "{}:{}:{}",
                                    error.canonical_code, error.space, error.code
                                ))
                            }
                            None => P4StreamEvent::Error("empty stream update".into()),
                        };
                        match event {
                            P4StreamEvent::Digest(_) | P4StreamEvent::Packet(_) => {
                                let kind = if matches!(event, P4StreamEvent::Digest(_)) {
                                    "digest"
                                } else {
                                    "packet-in"
                                };
                                if hint_event_tx.try_send(event).is_err()
                                    && high_event_tx
                                        .send(P4StreamEvent::HintDropped { kind })
                                        .await
                                        .is_err()
                                {
                                    return;
                                }
                            }
                            event => {
                                if high_event_tx.send(event).await.is_err() {
                                    return;
                                }
                            }
                        }
                    }
                    Ok(None) => {
                        task_primary.store(false, std::sync::atomic::Ordering::Release);
                        let _ = high_event_tx
                            .send(P4StreamEvent::Closed("stream closed".into()))
                            .await;
                        return;
                    }
                    Err(error) => {
                        task_primary.store(false, std::sync::atomic::Ordering::Release);
                        let _ = high_event_tx
                            .send(P4StreamEvent::Closed(error.to_string()))
                            .await;
                        return;
                    }
                }
            }
        });

        let observed_pipeline = match read_pipeline_identity(
            &mut client,
            assignment.device_id,
            limits.p4_rpc_deadline_ms,
            &expected_pipeline.p4runtime_api_version,
            &expected_pipeline.profile_digest,
        )
        .await
        {
            Ok(observed) => observed,
            Err(error) => {
                stream_task.abort();
                let _cancelled = stream_task.await;
                return Err(error);
            }
        };
        if let Err(error) = compare_pipeline(expected_pipeline, &observed_pipeline) {
            stream_task.abort();
            let _cancelled = stream_task.await;
            return Err(error);
        }
        Ok(Self {
            target_id: assignment.target_id.clone(),
            device_id: assignment.device_id,
            role: assignment.role.clone(),
            election_id,
            client,
            stream_requests: request_tx,
            high_stream_events: high_event_rx,
            hint_stream_events: hint_event_rx,
            stream_task,
            primary,
            pipeline_exact: true,
            observed_pipeline,
            limits: limits.clone(),
        })
    }

    /// Stable target identity for diagnostics.
    #[must_use]
    pub fn target_id(&self) -> &str {
        &self.target_id
    }

    /// Whether the latest arbitration update still grants primary.
    #[must_use]
    pub fn is_primary(&self) -> bool {
        self.primary.load(std::sync::atomic::Ordering::Acquire)
    }

    /// Whether exact pipeline identity was most recently verified.
    #[must_use]
    pub fn pipeline_exact(&self) -> bool {
        self.pipeline_exact
    }

    /// Exact observed pipeline identity.
    #[must_use]
    pub fn observed_pipeline(&self) -> &PipelineIdentity {
        &self.observed_pipeline
    }

    /// Receive one bounded StreamChannel event.
    pub async fn next_stream_event(&mut self) -> Option<P4StreamEvent> {
        tokio::select! {
            biased;
            event = self.high_stream_events.recv() => event,
            event = self.hint_stream_events.recv() => event,
        }
    }

    /// Non-blocking bounded drain used by the actor's priority scheduler.
    pub fn try_stream_event(&mut self) -> Option<P4StreamEvent> {
        self.high_stream_events
            .try_recv()
            .ok()
            .or_else(|| self.hint_stream_events.try_recv().ok())
    }

    /// ACK a digest only after its source record has been fsynced.
    pub async fn acknowledge_digest(&self, digest_id: u32, list_id: u64) -> EdgeResult<()> {
        self.stream_requests
            .try_send(StreamMessageRequest {
                update: Some(stream_message_request::Update::DigestAck(DigestListAck {
                    digest_id,
                    list_id,
                })),
            })
            .map_err(|error| match error {
                tokio::sync::mpsc::error::TrySendError::Full(_) => EdgeError::exhausted(
                    "P4_STREAM_REQUEST_QUEUE_FULL",
                    "P4 StreamChannel request queue reached its configured bound",
                ),
                tokio::sync::mpsc::error::TrySendError::Closed(_) => {
                    EdgeError::ActorUnavailable("P4 stream request queue closed".into())
                }
            })
    }

    /// Send bounded updates with `CONTINUE_ON_ERROR` only.
    pub async fn write(&mut self, updates: Vec<Update>) -> EdgeResult<()> {
        if !self.is_primary() {
            return Err(EdgeError::precondition(
                "NOT_PRIMARY",
                "P4 write forbidden without current primary arbitration",
            ));
        }
        if !self.pipeline_exact {
            return Err(EdgeError::precondition(
                "PIPELINE_DRIFT",
                "P4 write forbidden after pipeline drift",
            ));
        }
        if updates.is_empty() || updates.len() > self.limits.p4_updates_per_write {
            return Err(EdgeError::invalid(
                "p4_updates",
                "write update count outside configured bounds",
            ));
        }
        let mut request = Request::new(WriteRequest {
            device_id: self.device_id,
            role_id: 0,
            role: self.role.clone(),
            election_id: Some(self.election_id),
            updates,
            atomicity: Atomicity::ContinueOnError as i32,
        });
        request.set_timeout(Duration::from_millis(self.limits.p4_rpc_deadline_ms));
        tokio::time::timeout(
            Duration::from_millis(self.limits.p4_rpc_deadline_ms),
            self.client.write(request),
        )
        .await
        .map_err(|_| EdgeError::Deadline("P4 Write".into()))?
        .map_err(|error| EdgeError::Remote {
            code: "P4_WRITE_FAILED",
            message: error.to_string(),
        })?;
        Ok(())
    }

    /// Write an arbitrary number of updates as bounded target-local requests.
    pub async fn write_batched(&mut self, updates: &[Update]) -> EdgeResult<()> {
        if updates.is_empty() {
            return Err(EdgeError::invalid("p4_updates", "empty write is forbidden"));
        }
        for batch in updates.chunks(self.limits.p4_updates_per_write) {
            self.write(batch.to_vec()).await?;
        }
        Ok(())
    }

    /// Read bounded entities and enforce aggregate response bytes/count.
    pub async fn read(&mut self, entities: Vec<Entity>) -> EdgeResult<Vec<Entity>> {
        if entities.is_empty() || entities.len() > self.limits.p4_entities_per_read {
            return Err(EdgeError::invalid(
                "p4_entities",
                "read entity count outside configured bounds",
            ));
        }
        let mut request = Request::new(ReadRequest {
            device_id: self.device_id,
            role: self.role.clone(),
            entities,
        });
        request.set_timeout(Duration::from_millis(self.limits.p4_rpc_deadline_ms));
        let deadline =
            tokio::time::Instant::now() + Duration::from_millis(self.limits.p4_rpc_deadline_ms);
        let mut stream = tokio::time::timeout_at(deadline, self.client.read(request))
            .await
            .map_err(|_| EdgeError::Deadline("P4 Read response open".into()))?
            .map_err(|error| EdgeError::Remote {
                code: "P4_READ_FAILED",
                message: error.to_string(),
            })?
            .into_inner();
        let mut result = Vec::new();
        let mut bytes = 0_usize;
        let mut response_entities = 0_usize;
        while let Some(response) = tokio::time::timeout_at(deadline, stream.message())
            .await
            .map_err(|_| EdgeError::Deadline("P4 Read response stream".into()))?
            .map_err(|error| EdgeError::Remote {
                code: "P4_READ_FAILED",
                message: error.to_string(),
            })?
        {
            bytes = bytes.saturating_add(response.encoded_len());
            if bytes > self.limits.p4_read_response_bytes {
                return Err(EdgeError::exhausted(
                    "P4_READ_RESPONSE_TOO_LARGE",
                    "P4 Read response exceeded the configured byte limit",
                ));
            }
            response_entities = response_entities.saturating_add(response.entities.len());
            if response_entities > self.limits.p4_read_response_entities {
                return Err(EdgeError::exhausted(
                    "P4_READ_RESPONSE_TOO_MANY_ENTITIES",
                    "P4 Read response exceeded the configured entity limit",
                ));
            }
            result.extend(response.entities);
        }
        Ok(result)
    }

    /// Re-read and compare the exact forwarding pipeline before a mutation.
    pub async fn reverify_pipeline(&mut self, expected: &PipelineIdentity) -> EdgeResult<()> {
        let observed = read_pipeline_identity(
            &mut self.client,
            self.device_id,
            self.limits.p4_rpc_deadline_ms,
            &expected.p4runtime_api_version,
            &expected.profile_digest,
        )
        .await?;
        match compare_pipeline(expected, &observed) {
            Ok(()) => {
                self.pipeline_exact = true;
                self.observed_pipeline = observed;
                Ok(())
            }
            Err(error) => {
                self.pipeline_exact = false;
                Err(error)
            }
        }
    }
}

impl Drop for P4Session {
    fn drop(&mut self) {
        self.primary
            .store(false, std::sync::atomic::Ordering::Release);
        self.stream_task.abort();
    }
}

fn contract_tls(
    value: &TlsClientIdentity,
    identities: &BTreeMap<String, ClientTlsConfig>,
) -> EdgeResult<ClientTlsConfig> {
    if value.identity_ref.is_empty() {
        return Err(EdgeError::invalid(
            "tls.identity_ref",
            "stable credential reference is required",
        ));
    }
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

fn validate_arbitration(
    update: &MasterArbitrationUpdate,
    device_id: u64,
    role: &str,
    election: &Uint128,
) -> EdgeResult<()> {
    let status_ok = update
        .status
        .as_ref()
        .is_some_and(|status| status.code == 0);
    let role_ok = if role == "default" {
        update
            .role
            .as_ref()
            .is_none_or(|value| value.id == 0 && value.name.is_empty() && value.config.is_empty())
    } else {
        update.role.as_ref().is_some_and(|value| value.name == role)
    };
    let election_ok = update.election_id.as_ref() == Some(election);
    if update.device_id != device_id || !status_ok || !role_ok || !election_ok {
        return Err(EdgeError::precondition(
            "ROLE_ELECTION_MISMATCH",
            "arbitration response did not grant the exact device/role/election identity",
        ));
    }
    Ok(())
}

async fn read_pipeline_identity(
    client: &mut P4RuntimeClient<Channel>,
    device_id: u64,
    deadline_ms: u64,
    p4runtime_api_version: &str,
    profile_digest: &str,
) -> EdgeResult<PipelineIdentity> {
    let mut request = Request::new(GetForwardingPipelineConfigRequest {
        device_id,
        response_type: ResponseType::All as i32,
    });
    request.set_timeout(Duration::from_millis(deadline_ms));
    let response = tokio::time::timeout(
        Duration::from_millis(deadline_ms),
        client.get_forwarding_pipeline_config(request),
    )
    .await
    .map_err(|_| EdgeError::Remote {
        code: "PIPELINE_READ_FAILED",
        message: "local monotonic P4 pipeline read deadline exceeded".into(),
    })?
    .map_err(|error| EdgeError::Remote {
        code: "PIPELINE_READ_FAILED",
        message: error.to_string(),
    })?
    .into_inner();
    let config = response.config.ok_or_else(|| {
        EdgeError::precondition("PIPELINE_DRIFT", "target omitted pipeline readback")
    })?;
    let cookie = config.cookie.ok_or_else(|| {
        EdgeError::precondition("PIPELINE_DRIFT", "target omitted pipeline cookie")
    })?;
    Ok(PipelineIdentity {
        p4runtime_api_version: p4runtime_api_version.to_owned(),
        p4info_digest: canonical_p4info_digest(&config.p4info)?,
        device_config_digest: digest::sha256(&config.p4_device_config),
        profile_digest: profile_digest.to_owned(),
        cookie: cookie.cookie,
        supported_write_atomicity: vec![
            crate::contract::edge::P4WriteAtomicity::ContinueOnError as i32,
        ],
    })
}

fn compare_pipeline(expected: &PipelineIdentity, observed: &PipelineIdentity) -> EdgeResult<()> {
    for (field, left, right, code) in [
        (
            "p4info_digest",
            expected.p4info_digest.as_str(),
            observed.p4info_digest.as_str(),
            "P4INFO_DRIFT",
        ),
        (
            "device_config_digest",
            expected.device_config_digest.as_str(),
            observed.device_config_digest.as_str(),
            "PIPELINE_DRIFT",
        ),
        (
            "profile_digest",
            expected.profile_digest.as_str(),
            observed.profile_digest.as_str(),
            "CAPABILITY_DRIFT",
        ),
    ] {
        digest::validate_sha256(left, field)?;
        if left != right {
            return Err(EdgeError::precondition(
                code,
                format!("{field} exact readback mismatch: expected {left}, observed {right}"),
            ));
        }
    }
    if expected.cookie == 0 || expected.cookie != observed.cookie {
        return Err(EdgeError::precondition(
            "PIPELINE_DRIFT",
            "pipeline cookie exact readback mismatch",
        ));
    }
    if expected.supported_write_atomicity != observed.supported_write_atomicity {
        return Err(EdgeError::precondition(
            "CAPABILITY_DRIFT",
            "qualified P4 Write atomicity profile mismatch",
        ));
    }
    Ok(())
}

fn canonical_p4info_digest(payload: &[u8]) -> EdgeResult<String> {
    let p4info = P4Info::decode(payload).map_err(|error| {
        EdgeError::precondition(
            "P4INFO_DRIFT",
            format!("target P4Info cannot be decoded with the pinned 1.4.1 schema: {error}"),
        )
    })?;
    Ok(digest::sha256(&p4info.encode_to_vec()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_role_is_encoded_as_absent_and_validated_exactly() {
        let election = Uint128 { high: 0, low: 7 };
        let update = MasterArbitrationUpdate {
            device_id: 1,
            role: None,
            election_id: Some(election),
            status: Some(crate::google::rpc::Status {
                code: 0,
                ..crate::google::rpc::Status::default()
            }),
        };
        assert!(validate_arbitration(&update, 1, "default", &election).is_ok());
        assert!(validate_arbitration(&update, 1, "primary", &election).is_err());
    }
}
