//! Deterministic public-boundary fakes and ephemeral PKI for Edge black-box tests.

use std::{
    collections::BTreeMap,
    error::Error,
    fs,
    net::SocketAddr,
    os::unix::fs::PermissionsExt as _,
    path::{Path, PathBuf},
    pin::Pin,
    sync::Arc,
    time::Duration,
};

use futures::Stream;
use masi_edge::{
    contract::{
        edge::{
            BindingReadback, CanonicalAck, CanonicalAckBatch, CanonicalCommitStatus, DataQuality,
            GetBindingRequest, InferenceDecision, InferenceExecutionStatus, InferenceInputBatch,
            InferenceResultBatch, InferenceResultRecord, InferenceRoute, InferenceWorkerIdentity,
            PublishAck, PublishStatus, RuleObservationBatch, TargetStatusBatch,
            control_sink_server::{ControlSink, ControlSinkServer},
        },
        inference::central_inference_server::{CentralInference, CentralInferenceServer},
        p4::{
            CapabilitiesRequest, CapabilitiesResponse, CounterData, CounterEntry, DigestList,
            DigestListAck, Entity, ForwardingPipelineConfig, GetForwardingPipelineConfigRequest,
            GetForwardingPipelineConfigResponse, Index, MasterArbitrationUpdate, PacketIn,
            ReadRequest, ReadResponse, SetForwardingPipelineConfigRequest,
            SetForwardingPipelineConfigResponse, StreamMessageRequest, StreamMessageResponse,
            TableAction, TableEntry, WriteRequest, WriteResponse, action, entity,
            forwarding_pipeline_config,
            p4_runtime_server::{P4Runtime, P4RuntimeServer},
            stream_message_request, stream_message_response, table_action, update,
        },
    },
    digest,
    firewall::{action_id, counter_id, table_id},
    google::rpc::Status as GoogleStatus,
    inference::{
        canonical_ack_batch_digest, canonical_output_record_digest, canonical_result_batch_digest,
    },
};
use parking_lot::Mutex;
use prost::Message as _;
use rcgen::{
    BasicConstraints, CertificateParams, CertifiedIssuer, DnType, ExtendedKeyUsagePurpose, IsCa,
    KeyPair, KeyUsagePurpose, date_time_ymd,
};
use tokio::{
    net::TcpListener,
    sync::{broadcast, mpsc, oneshot},
    task::JoinHandle,
};
use tokio_stream::wrappers::{ReceiverStream, TcpListenerStream};
use tonic::{
    Request, Response, Status,
    transport::{Certificate, Identity, Server, ServerTlsConfig},
};

type RpcStream<T> = Pin<Box<dyn Stream<Item = Result<T, Status>> + Send + 'static>>;

/// One leaf certificate and its shared trust anchor.
#[derive(Clone, Debug)]
pub struct LeafIdentity {
    pub ca_pem: String,
    pub certificate_pem: String,
    pub certificate_der: Vec<u8>,
    pub private_key_pem: String,
}

/// All distinct identities used by one module-test topology.
#[derive(Clone, Debug)]
pub struct TestPki {
    pub edge_server: LeafIdentity,
    pub edge_client: LeafIdentity,
    pub p4_server: LeafIdentity,
    pub p4_client: LeafIdentity,
    pub inference_server: LeafIdentity,
    pub inference_client: LeafIdentity,
    pub control_server: LeafIdentity,
    pub control_client: LeafIdentity,
}

/// Generate a single ephemeral CA and distinct mTLS leaves.
pub fn generate_test_pki() -> Result<TestPki, Box<dyn Error>> {
    let mut ca_params = CertificateParams::new(Vec::<String>::new())?;
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    ca_params.not_before = date_time_ymd(2025, 1, 1);
    ca_params.not_after = date_time_ymd(2035, 1, 1);
    ca_params
        .distinguished_name
        .push(DnType::CommonName, "MASI Edge module-test CA");
    ca_params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
    ];
    let ca = CertifiedIssuer::self_signed(ca_params, KeyPair::generate()?)?;
    let ca_pem = ca.pem();
    let leaf = |name: &str| -> Result<LeafIdentity, Box<dyn Error>> {
        let mut params = CertificateParams::new(vec![name.into()])?;
        params.not_before = date_time_ymd(2025, 1, 1);
        params.not_after = date_time_ymd(2035, 1, 1);
        params.distinguished_name.push(DnType::CommonName, name);
        params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        params.extended_key_usages = vec![
            ExtendedKeyUsagePurpose::ServerAuth,
            ExtendedKeyUsagePurpose::ClientAuth,
        ];
        let key = KeyPair::generate()?;
        let certificate = params.signed_by(&key, &ca)?;
        Ok(LeafIdentity {
            ca_pem: ca_pem.clone(),
            certificate_pem: certificate.pem(),
            certificate_der: certificate.der().to_vec(),
            private_key_pem: key.serialize_pem(),
        })
    };
    Ok(TestPki {
        edge_server: leaf("edge.test")?,
        edge_client: leaf("go-edge-client.test")?,
        p4_server: leaf("p4.test")?,
        p4_client: leaf("edge-p4-client.test")?,
        inference_server: leaf("inference.test")?,
        inference_client: leaf("edge-inference-client.test")?,
        control_server: leaf("control.test")?,
        control_client: leaf("edge-control-client.test")?,
    })
}

/// Paths for one identity as consumed by strict Edge configuration.
#[derive(Clone, Debug)]
pub struct IdentityPaths {
    pub ca: PathBuf,
    pub certificate: PathBuf,
    pub private_key: PathBuf,
}

/// Write one leaf identity with a mode-0600 private key.
pub fn write_identity(
    root: &Path,
    prefix: &str,
    identity: &LeafIdentity,
) -> Result<IdentityPaths, Box<dyn Error>> {
    let ca = root.join(format!("{}-ca.pem", prefix));
    let certificate = root.join(format!("{}-certificate.pem", prefix));
    let private_key = root.join(format!("{}-private-key.pem", prefix));
    fs::write(&ca, &identity.ca_pem)?;
    fs::write(&certificate, &identity.certificate_pem)?;
    fs::write(&private_key, &identity.private_key_pem)?;
    fs::set_permissions(&private_key, fs::Permissions::from_mode(0o600))?;
    Ok(IdentityPaths {
        ca,
        certificate,
        private_key,
    })
}

/// Deterministic P4 target state shared with assertions/fault injection.
#[derive(Clone, Debug)]
pub struct FakeP4 {
    inner: Arc<Mutex<P4Inner>>,
    stream_resets: broadcast::Sender<u64>,
    stream_events: broadcast::Sender<(u64, StreamMessageResponse)>,
}

#[derive(Debug)]
struct P4Inner {
    p4info: Vec<u8>,
    device_config: Vec<u8>,
    cookie: u64,
    devices: BTreeMap<u64, DeviceState>,
    stream_opens: BTreeMap<u64, u64>,
    active_streams: BTreeMap<u64, u64>,
    max_active_streams: BTreeMap<u64, u64>,
    digest_acks: Vec<(u64, DigestListAck)>,
    next_read_entity_amplification: usize,
    pipeline_read_attempts: u64,
    pipeline_read_delay_remaining: u32,
    pipeline_read_delay_ms: u64,
    fail_effect_write_response_once: bool,
    effect_write_attempts: u64,
}

#[derive(Debug, Default)]
struct DeviceState {
    tables: BTreeMap<Vec<u8>, Entity>,
    counters: BTreeMap<(u32, i64), CounterData>,
    direct_counters: BTreeMap<Vec<u8>, CounterData>,
}

impl DeviceState {
    fn initialized() -> Self {
        let mut state = Self::default();
        state.insert_table(selector_entry(
            table_id::POLICY_SELECTOR,
            action_id::SELECT_POLICY_0,
            None,
        ));
        state.insert_table(selector_entry(
            table_id::TELEMETRY_SELECTOR,
            action_id::SELECT_TELEMETRY_0,
            Some(0),
        ));
        state.seed_telemetry_bank(0);
        state
    }

    fn insert_table(&mut self, entity: Entity) {
        if let Some(entity::Entity::TableEntry(entry)) = entity.entity.as_ref() {
            self.tables.insert(table_key(entry), entity);
        }
    }

    fn delete_table(&mut self, entry: &TableEntry) {
        self.tables.remove(&table_key(entry));
    }

    fn seed_telemetry_bank(&mut self, bank: u32) {
        let bank_index = i64::from(bank);
        self.counters.insert(
            (counter_id::TELEMETRY_BANK, bank_index),
            CounterData {
                byte_count: 640,
                packet_count: 10,
            },
        );
        self.counters.insert(
            (counter_id::TELEMETRY_CELL, bank_index * 256 + 3),
            CounterData {
                byte_count: 640,
                packet_count: 10,
            },
        );
    }
}

impl FakeP4 {
    pub fn new(p4info: Vec<u8>, device_config: Vec<u8>, cookie: u64) -> Self {
        let (stream_resets, _) = broadcast::channel(32);
        let (stream_events, _) = broadcast::channel(4096);
        Self {
            inner: Arc::new(Mutex::new(P4Inner {
                p4info,
                device_config,
                cookie,
                devices: BTreeMap::new(),
                stream_opens: BTreeMap::new(),
                active_streams: BTreeMap::new(),
                max_active_streams: BTreeMap::new(),
                digest_acks: Vec::new(),
                next_read_entity_amplification: 0,
                pipeline_read_attempts: 0,
                pipeline_read_delay_remaining: 0,
                pipeline_read_delay_ms: 0,
                fail_effect_write_response_once: false,
                effect_write_attempts: 0,
            })),
            stream_resets,
            stream_events,
        }
    }

    pub fn fail_next_effect_write_response(&self) {
        self.inner.lock().fail_effect_write_response_once = true;
    }

    pub fn stream_opens(&self, device_id: u64) -> u64 {
        self.inner
            .lock()
            .stream_opens
            .get(&device_id)
            .copied()
            .unwrap_or_default()
    }

    pub fn effect_write_attempts(&self) -> u64 {
        self.inner.lock().effect_write_attempts
    }

    pub fn max_active_streams(&self, device_id: u64) -> u64 {
        self.inner
            .lock()
            .max_active_streams
            .get(&device_id)
            .copied()
            .unwrap_or_default()
    }

    pub fn active_streams(&self, device_id: u64) -> u64 {
        self.inner
            .lock()
            .active_streams
            .get(&device_id)
            .copied()
            .unwrap_or_default()
    }

    pub fn close_stream(&self, device_id: u64) {
        let _ignored = self.stream_resets.send(device_id);
    }

    /// Inject one target-scoped supplemental digest on the active StreamChannel.
    pub fn inject_digest(&self, device_id: u64, digest: DigestList) -> bool {
        self.stream_events
            .send((
                device_id,
                StreamMessageResponse {
                    update: Some(stream_message_response::Update::Digest(digest)),
                },
            ))
            .is_ok()
    }

    /// Inject one target-scoped supplemental PacketIn on the active StreamChannel.
    pub fn inject_packet(&self, device_id: u64, packet: PacketIn) -> bool {
        self.stream_events
            .send((
                device_id,
                StreamMessageResponse {
                    update: Some(stream_message_response::Update::Packet(packet)),
                },
            ))
            .is_ok()
    }

    /// Return all digest acknowledgements observed on a device StreamChannel.
    pub fn digest_acks(&self, device_id: u64) -> Vec<DigestListAck> {
        self.inner
            .lock()
            .digest_acks
            .iter()
            .filter(|(observed_device, _)| *observed_device == device_id)
            .map(|(_, acknowledgement)| *acknowledgement)
            .collect()
    }

    /// Seed one aggregate counter for a deterministic observation assertion.
    pub fn seed_counter(&self, device_id: u64, counter_id: u32, index: i64, data: CounterData) {
        self.inner
            .lock()
            .devices
            .entry(device_id)
            .or_insert_with(DeviceState::initialized)
            .counters
            .insert((counter_id, index), data);
    }

    /// Seed a direct counter bound to an exact table key.
    pub fn seed_direct_counter(&self, device_id: u64, entity: &Entity, data: CounterData) -> bool {
        let Some(entity::Entity::TableEntry(entry)) = entity.entity.as_ref() else {
            return false;
        };
        self.inner
            .lock()
            .devices
            .entry(device_id)
            .or_insert_with(DeviceState::initialized)
            .direct_counters
            .insert(table_key(entry), data);
        true
    }

    /// Replace the next Read response with a bounded malicious entity count.
    pub fn amplify_next_read_response(&self, entity_count: usize) {
        self.inner.lock().next_read_entity_amplification = entity_count;
    }

    /// Delay a bounded number of pipeline readbacks to exercise client RPC deadlines.
    pub fn delay_pipeline_reads(&self, requests: u32, delay_ms: u64) {
        let mut inner = self.inner.lock();
        inner.pipeline_read_delay_remaining = requests;
        inner.pipeline_read_delay_ms = delay_ms;
    }

    pub fn pipeline_read_attempts(&self) -> u64 {
        self.inner.lock().pipeline_read_attempts
    }
}

struct StreamGuard {
    state: FakeP4,
    device_id: u64,
}

impl Drop for StreamGuard {
    fn drop(&mut self) {
        let mut inner = self.state.inner.lock();
        let active = inner.active_streams.entry(self.device_id).or_default();
        *active = active.saturating_sub(1);
    }
}

#[tonic::async_trait]
impl P4Runtime for FakeP4 {
    async fn write(
        &self,
        request: Request<WriteRequest>,
    ) -> Result<Response<WriteResponse>, Status> {
        let request = request.into_inner();
        let mut inner = self.inner.lock();
        let mut effect_write = false;
        let device = inner
            .devices
            .entry(request.device_id)
            .or_insert_with(DeviceState::initialized);
        for item in request.updates {
            let entity = item
                .entity
                .ok_or_else(|| Status::invalid_argument("update entity missing"))?;
            match entity.entity.as_ref() {
                Some(entity::Entity::TableEntry(entry)) => {
                    if entry.table_id != table_id::TELEMETRY_SELECTOR {
                        effect_write = true;
                    }
                    if item.r#type == update::Type::Delete as i32 {
                        device.delete_table(entry);
                    } else {
                        device.insert_table(entity.clone());
                    }
                    if entry.table_id == table_id::TELEMETRY_SELECTOR
                        && let Some(bank) = telemetry_bank(entry)
                    {
                        device.seed_telemetry_bank(bank);
                    }
                }
                Some(entity::Entity::CounterEntry(entry)) => {
                    let index = entry.index.as_ref().map_or(0, |value| value.index);
                    device
                        .counters
                        .insert((entry.counter_id, index), entry.data.unwrap_or_default());
                }
                Some(entity::Entity::DirectCounterEntry(_))
                | Some(entity::Entity::DigestEntry(_))
                | Some(entity::Entity::PacketReplicationEngineEntry(_))
                | None => {}
            }
        }
        if effect_write {
            inner.effect_write_attempts = inner.effect_write_attempts.saturating_add(1);
        }
        if effect_write && inner.fail_effect_write_response_once {
            inner.fail_effect_write_response_once = false;
            return Err(Status::unavailable(
                "injected response loss after deterministic apply",
            ));
        }
        Ok(Response::new(WriteResponse {}))
    }

    type ReadStream = RpcStream<ReadResponse>;

    async fn read(
        &self,
        request: Request<ReadRequest>,
    ) -> Result<Response<Self::ReadStream>, Status> {
        let request = request.into_inner();
        let mut inner = self.inner.lock();
        let device = inner
            .devices
            .entry(request.device_id)
            .or_insert_with(DeviceState::initialized);
        let mut entities = Vec::new();
        for query in request.entities {
            match query.entity {
                Some(entity::Entity::TableEntry(entry)) => {
                    if let Some(found) = device.tables.get(&table_key(&entry)) {
                        entities.push(found.clone());
                    }
                }
                Some(entity::Entity::CounterEntry(entry)) => {
                    let index = entry.index.as_ref().map_or(0, |value| value.index);
                    let data = device
                        .counters
                        .get(&(entry.counter_id, index))
                        .copied()
                        .unwrap_or_default();
                    entities.push(Entity {
                        entity: Some(entity::Entity::CounterEntry(CounterEntry {
                            counter_id: entry.counter_id,
                            index: Some(Index { index }),
                            data: Some(data),
                        })),
                    });
                }
                Some(entity::Entity::DirectCounterEntry(mut entry)) => {
                    let data = entry
                        .table_entry
                        .as_ref()
                        .and_then(|table| device.direct_counters.get(&table_key(table)))
                        .copied()
                        .unwrap_or_default();
                    entry.data = Some(data);
                    entities.push(Entity {
                        entity: Some(entity::Entity::DirectCounterEntry(entry)),
                    });
                }
                Some(other) => entities.push(Entity {
                    entity: Some(other),
                }),
                None => {}
            }
        }
        let amplification = std::mem::take(&mut inner.next_read_entity_amplification);
        if amplification > 0 {
            entities = vec![Entity::default(); amplification];
        }
        let stream = tokio_stream::iter(vec![Ok(ReadResponse { entities })]);
        Ok(Response::new(Box::pin(stream)))
    }

    async fn set_forwarding_pipeline_config(
        &self,
        _request: Request<SetForwardingPipelineConfigRequest>,
    ) -> Result<Response<SetForwardingPipelineConfigResponse>, Status> {
        Err(Status::permission_denied(
            "module fake never permits pipeline mutation",
        ))
    }

    async fn get_forwarding_pipeline_config(
        &self,
        _request: Request<GetForwardingPipelineConfigRequest>,
    ) -> Result<Response<GetForwardingPipelineConfigResponse>, Status> {
        let delay_ms = {
            let mut inner = self.inner.lock();
            inner.pipeline_read_attempts = inner.pipeline_read_attempts.saturating_add(1);
            if inner.pipeline_read_delay_remaining == 0 {
                0
            } else {
                inner.pipeline_read_delay_remaining =
                    inner.pipeline_read_delay_remaining.saturating_sub(1);
                inner.pipeline_read_delay_ms
            }
        };
        if delay_ms > 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }
        let inner = self.inner.lock();
        Ok(Response::new(GetForwardingPipelineConfigResponse {
            config: Some(ForwardingPipelineConfig {
                p4info: inner.p4info.clone(),
                p4_device_config: inner.device_config.clone(),
                cookie: Some(forwarding_pipeline_config::Cookie {
                    cookie: inner.cookie,
                }),
            }),
        }))
    }

    type StreamChannelStream = RpcStream<StreamMessageResponse>;

    async fn stream_channel(
        &self,
        request: Request<tonic::Streaming<StreamMessageRequest>>,
    ) -> Result<Response<Self::StreamChannelStream>, Status> {
        let mut inbound = request.into_inner();
        let state = self.clone();
        let mut stream_resets = self.stream_resets.subscribe();
        let mut stream_events = self.stream_events.subscribe();
        let (sender, receiver) = mpsc::channel(16);
        tokio::spawn(async move {
            let arbitration = loop {
                let message = match inbound.message().await {
                    Ok(Some(message)) => message,
                    Ok(None) | Err(_) => return,
                };
                let Some(stream_message_request::Update::Arbitration(arbitration)) = message.update
                else {
                    continue;
                };
                break arbitration;
            };
            {
                let mut inner = state.inner.lock();
                let count = inner.stream_opens.entry(arbitration.device_id).or_default();
                *count = count.saturating_add(1);
                let current = {
                    let active = inner
                        .active_streams
                        .entry(arbitration.device_id)
                        .or_default();
                    *active = active.saturating_add(1);
                    *active
                };
                let maximum = inner
                    .max_active_streams
                    .entry(arbitration.device_id)
                    .or_default();
                *maximum = (*maximum).max(current);
            }
            let _guard = StreamGuard {
                state: state.clone(),
                device_id: arbitration.device_id,
            };
            let response = StreamMessageResponse {
                update: Some(stream_message_response::Update::Arbitration(
                    MasterArbitrationUpdate {
                        device_id: arbitration.device_id,
                        role: arbitration.role,
                        election_id: arbitration.election_id,
                        status: Some(GoogleStatus {
                            code: 0,
                            message: "primary".into(),
                            details: Vec::new(),
                        }),
                    },
                )),
            };
            if sender.send(Ok(response)).await.is_err() {
                return;
            }
            // Keep the active-session guard for the entire inbound stream.
            stream_until_closed_or_reset(
                &mut inbound,
                &sender,
                &mut stream_resets,
                &mut stream_events,
                &state,
                arbitration.device_id,
            )
            .await;
        });
        Ok(Response::new(Box::pin(ReceiverStream::new(receiver))))
    }

    async fn capabilities(
        &self,
        _request: Request<CapabilitiesRequest>,
    ) -> Result<Response<CapabilitiesResponse>, Status> {
        Ok(Response::new(CapabilitiesResponse {
            p4runtime_api_version: "1.3.0".into(),
        }))
    }
}

async fn stream_until_closed_or_reset(
    inbound: &mut tonic::Streaming<StreamMessageRequest>,
    sender: &mpsc::Sender<Result<StreamMessageResponse, Status>>,
    stream_resets: &mut broadcast::Receiver<u64>,
    stream_events: &mut broadcast::Receiver<(u64, StreamMessageResponse)>,
    state: &FakeP4,
    device_id: u64,
) {
    loop {
        tokio::select! {
            message = inbound.message() => match message {
                Ok(Some(message)) => {
                    if let Some(stream_message_request::Update::DigestAck(acknowledgement)) = message.update {
                        state.inner.lock().digest_acks.push((device_id, acknowledgement));
                    }
                }
                Ok(None) | Err(_) => return,
            },
            reset = stream_resets.recv() => match reset {
                Ok(reset_device) if reset_device == device_id => return,
                Ok(_) => {}
                Err(broadcast::error::RecvError::Lagged(_)) => return,
                Err(broadcast::error::RecvError::Closed) => return,
            },
            event = stream_events.recv() => match event {
                Ok((event_device, response)) if event_device == device_id => {
                    if sender.send(Ok(response)).await.is_err() {
                        return;
                    }
                }
                Ok(_) => {}
                Err(broadcast::error::RecvError::Lagged(_)) => return,
                Err(broadcast::error::RecvError::Closed) => return,
            }
        }
    }
}

fn table_key(entry: &TableEntry) -> Vec<u8> {
    let mut key = entry.clone();
    key.action = None;
    key.controller_metadata = 0;
    key.counter_data = None;
    key.idle_timeout_ns = 0;
    key.metadata.clear();
    key.encode_to_vec()
}

fn selector_entry(table: u32, action_identifier: u32, epoch: Option<u32>) -> Entity {
    Entity {
        entity: Some(entity::Entity::TableEntry(TableEntry {
            table_id: table,
            r#match: vec![masi_edge::contract::p4::FieldMatch {
                field_id: 1,
                field_match_type: Some(
                    masi_edge::contract::p4::field_match::FieldMatchType::Exact(
                        masi_edge::contract::p4::field_match::Exact { value: vec![0] },
                    ),
                ),
            }],
            action: Some(TableAction {
                r#type: Some(table_action::Type::Action(
                    masi_edge::contract::p4::Action {
                        action_id: action_identifier,
                        params: epoch.map_or_else(Vec::new, |value| {
                            vec![action::Param {
                                param_id: 1,
                                value: value.to_be_bytes().to_vec(),
                            }]
                        }),
                    },
                )),
            }),
            ..TableEntry::default()
        })),
    }
}

fn telemetry_bank(entry: &TableEntry) -> Option<u32> {
    let identifier = match entry
        .action
        .as_ref()
        .and_then(|value| value.r#type.as_ref())
    {
        Some(table_action::Type::Action(action)) => action.action_id,
        None => return None,
    };
    match identifier {
        action_id::SELECT_TELEMETRY_0 => Some(0),
        action_id::SELECT_TELEMETRY_1 => Some(1),
        _ => None,
    }
}

/// Central inference fake with exact binding and retry observations.
#[derive(Clone, Debug)]
pub struct FakeInference {
    inner: Arc<Mutex<InferenceInner>>,
}

#[derive(Debug)]
struct InferenceInner {
    route: InferenceRoute,
    worker_id: String,
    worker_digest: String,
    alternate_worker_id: String,
    alternate_worker_digest: String,
    fail_infer_remaining: u32,
    delay_infer_remaining: u32,
    infer_delay_ms: u64,
    duplicate_result_remaining: u32,
    reverse_result_remaining: u32,
    invalid_value_remaining: u32,
    result_fence_drift_remaining: u32,
    result_reorders_applied: u64,
    calls: Vec<(String, String, u32)>,
}

impl FakeInference {
    pub fn new(route: InferenceRoute, fail_infer_remaining: u32) -> Self {
        Self {
            inner: Arc::new(Mutex::new(InferenceInner {
                route,
                worker_id: "worker-module-0001".into(),
                worker_digest: digest::sha256(b"worker-module-0001"),
                alternate_worker_id: "worker-module-0002".into(),
                alternate_worker_digest: digest::sha256(b"worker-module-0002"),
                fail_infer_remaining,
                delay_infer_remaining: 0,
                infer_delay_ms: 0,
                duplicate_result_remaining: 0,
                reverse_result_remaining: 0,
                invalid_value_remaining: 0,
                result_fence_drift_remaining: 0,
                result_reorders_applied: 0,
                calls: Vec::new(),
            })),
        }
    }

    pub fn calls(&self) -> Vec<(String, String, u32)> {
        self.inner.lock().calls.clone()
    }

    pub fn set_failures(&self, failures: u32) {
        self.inner.lock().fail_infer_remaining = failures;
    }

    pub fn delay_infer_calls(&self, calls: u32, delay_ms: u64) {
        let mut inner = self.inner.lock();
        inner.delay_infer_remaining = calls;
        inner.infer_delay_ms = delay_ms;
    }

    pub fn duplicate_next_result(&self) {
        self.inner.lock().duplicate_result_remaining = 1;
    }

    pub fn reverse_next_result(&self) {
        self.inner.lock().reverse_result_remaining = 1;
    }

    pub fn invalidate_next_result_value(&self) {
        self.inner.lock().invalid_value_remaining = 1;
    }

    pub fn drift_next_result_fence(&self) {
        self.inner.lock().result_fence_drift_remaining = 1;
    }

    pub fn result_reorders_applied(&self) -> u64 {
        self.inner.lock().result_reorders_applied
    }
}

#[tonic::async_trait]
impl CentralInference for FakeInference {
    async fn get_binding(
        &self,
        request: Request<GetBindingRequest>,
    ) -> Result<Response<BindingReadback>, Status> {
        let request = request.into_inner();
        let inner = self.inner.lock();
        if request.logical_pool_id != inner.route.logical_pool_id
            || request.pool_generation != inner.route.pool_generation
            || request.binding_generation != inner.route.binding_generation
            || request.schema_version != "inference-binding-readback/v1"
            || request.model_control_incarnation_id != inner.route.model_control_incarnation_id
            || request.operation_id != inner.route.operation_id
            || request.startup_envelope_digest != inner.route.startup_envelope_digest
            || request.model_revision_digest != inner.route.model_revision_digest
            || request.model_bundle_digest != inner.route.model_bundle_digest
            || request.feature_contract_digest != inner.route.feature_contract_digest
            || request.label_contract_digest != inner.route.label_contract_digest
            || request.output_adapter_digest != inner.route.output_adapter_digest
            || request.wire_profile != inner.route.wire_profile
            || request.wire_profile_digest != inner.route.wire_profile_digest
            || request.runtime_profile != inner.route.runtime_profile
            || request.runtime_profile_digest != inner.route.runtime_profile_digest
            || request.optimization_profile_digest != inner.route.optimization_profile_digest
            || request.pool_observation_digest != inner.route.pool_observation_digest
            || request.binding_digest != inner.route.binding_digest
        {
            return Err(Status::failed_precondition("binding identity mismatch"));
        }
        Ok(Response::new(BindingReadback {
            logical_pool_id: inner.route.logical_pool_id.clone(),
            pool_generation: inner.route.pool_generation,
            binding_generation: inner.route.binding_generation,
            model_revision_digest: inner.route.model_revision_digest.clone(),
            feature_contract_digest: inner.route.feature_contract_digest.clone(),
            label_contract_digest: inner.route.label_contract_digest.clone(),
            output_adapter_digest: inner.route.output_adapter_digest.clone(),
            runtime_profile: inner.route.runtime_profile.clone(),
            worker_id: inner.worker_id.clone(),
            worker_digest: inner.worker_digest.clone(),
            schema_version: "inference-binding-readback/v1".into(),
            model_control_incarnation_id: inner.route.model_control_incarnation_id.clone(),
            operation_id: inner.route.operation_id.clone(),
            startup_envelope_digest: inner.route.startup_envelope_digest.clone(),
            model_bundle_digest: inner.route.model_bundle_digest.clone(),
            wire_profile: inner.route.wire_profile.clone(),
            wire_profile_digest: inner.route.wire_profile_digest.clone(),
            runtime_profile_digest: inner.route.runtime_profile_digest.clone(),
            optimization_profile_digest: inner.route.optimization_profile_digest.clone(),
            readback_attempt_id: "readback-attempt-module-0001".into(),
            observed_at_unix_ms: 1_893_456_000_000,
            eligible_workers: vec![
                InferenceWorkerIdentity {
                    worker_id: inner.worker_id.clone(),
                    worker_digest: inner.worker_digest.clone(),
                },
                InferenceWorkerIdentity {
                    worker_id: inner.alternate_worker_id.clone(),
                    worker_digest: inner.alternate_worker_digest.clone(),
                },
            ],
            pool_observation_digest: inner.route.pool_observation_digest.clone(),
            binding_digest: inner.route.binding_digest.clone(),
        }))
    }

    async fn infer(
        &self,
        request: Request<InferenceInputBatch>,
    ) -> Result<Response<InferenceResultBatch>, Status> {
        let input = request.into_inner();
        let (
            worker_id,
            worker_digest,
            should_fail,
            delay_ms,
            duplicate_result,
            reverse_result,
            invalid_value,
            result_fence_drift,
        ) = {
            let mut inner = self.inner.lock();
            inner.calls.push((
                input.request_id.clone(),
                input.batch_digest.clone(),
                input.attempt,
            ));
            let should_fail = inner.fail_infer_remaining > 0;
            inner.fail_infer_remaining = inner.fail_infer_remaining.saturating_sub(1);
            let delay_ms = if inner.delay_infer_remaining == 0 {
                0
            } else {
                inner.delay_infer_remaining = inner.delay_infer_remaining.saturating_sub(1);
                inner.infer_delay_ms
            };
            let duplicate_result = !should_fail && inner.duplicate_result_remaining > 0;
            if duplicate_result {
                inner.duplicate_result_remaining =
                    inner.duplicate_result_remaining.saturating_sub(1);
            }
            let reverse_result =
                !should_fail && input.records.len() > 1 && inner.reverse_result_remaining > 0;
            if reverse_result {
                inner.reverse_result_remaining = inner.reverse_result_remaining.saturating_sub(1);
            }
            let invalid_value = !should_fail && inner.invalid_value_remaining > 0;
            if invalid_value {
                inner.invalid_value_remaining = inner.invalid_value_remaining.saturating_sub(1);
            }
            let result_fence_drift = !should_fail && inner.result_fence_drift_remaining > 0;
            if result_fence_drift {
                inner.result_fence_drift_remaining =
                    inner.result_fence_drift_remaining.saturating_sub(1);
            }
            let (worker_id, worker_digest) = if input.attempt > 1 {
                (
                    inner.alternate_worker_id.clone(),
                    inner.alternate_worker_digest.clone(),
                )
            } else {
                (inner.worker_id.clone(), inner.worker_digest.clone())
            };
            (
                worker_id,
                worker_digest,
                should_fail,
                delay_ms,
                duplicate_result,
                reverse_result,
                invalid_value,
                result_fence_drift,
            )
        };
        if delay_ms > 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }
        if should_fail {
            return Err(Status::unavailable("injected same-generation transient"));
        }
        let mut records = Vec::with_capacity(input.records.len());
        for source in &input.records {
            let mut record = InferenceResultRecord {
                input_id: source.input_id.clone(),
                event_idempotency_key: source.event_idempotency_key.clone(),
                input_digest: source.input_digest.clone(),
                output_digest: String::new(),
                scores: vec![0.1, 0.9],
                predicted_label: 1,
                decision: "alert".into(),
                out_of_distribution: false,
                abstain: false,
                quality: "valid".into(),
                status: "ok".into(),
                error_code: "NONE".into(),
                worker_id: worker_id.clone(),
                worker_digest: worker_digest.clone(),
                source_wal_sequence: source.source_wal_sequence,
                input_wal_sequence: source.input_wal_sequence,
                result_wal_sequence: 0,
                decision_code: InferenceDecision::Alert as i32,
                quality_code: DataQuality::Valid as i32,
                execution_status: InferenceExecutionStatus::Ok as i32,
                window_id: source.window_id.clone(),
                model_control_incarnation_id: source.model_control_incarnation_id.clone(),
                logical_pool_id: source.logical_pool_id.clone(),
                pool_generation: source.pool_generation,
                binding_generation: source.binding_generation,
                route_epoch: source.route_epoch,
                model_revision_digest: source.model_revision_digest.clone(),
                feature_contract_digest: source.feature_contract_digest.clone(),
                label_contract_digest: source.label_contract_digest.clone(),
                output_adapter_digest: source.output_adapter_digest.clone(),
                wire_profile: source.wire_profile.clone(),
                runtime_profile: source.runtime_profile.clone(),
                source_runtime_epoch: source.source_runtime_epoch.clone(),
                source_sequence_start: source.source_sequence_start,
                source_sequence_end: source.source_sequence_end,
                window_start_unix_ms: source.window_start_unix_ms,
                window_end_unix_ms: source.window_end_unix_ms,
                finalized_at_unix_ms: source.finalized_at_unix_ms,
                target_id: source.target_id.clone(),
                sampling_coverage_ppm: source.sampling_coverage_ppm,
                operation_id: source.operation_id.clone(),
                scope: source.scope.clone(),
                expected_binding_generation: source.expected_binding_generation,
                proposed_binding_generation: source.proposed_binding_generation,
                current_binding_generation: source.current_binding_generation,
                startup_envelope_digest: source.startup_envelope_digest.clone(),
                pool_observation_digest: source.pool_observation_digest.clone(),
                binding_digest: source.binding_digest.clone(),
                model_bundle_digest: source.model_bundle_digest.clone(),
                wire_profile_digest: source.wire_profile_digest.clone(),
                runtime_profile_digest: source.runtime_profile_digest.clone(),
                optimization_profile_digest: source.optimization_profile_digest.clone(),
                worker_attempt_id: format!("worker-attempt-{}-{}", input.attempt, worker_id),
                inference_started_at_unix_ms: source.finalized_at_unix_ms.saturating_add(1),
                inference_completed_at_unix_ms: source.finalized_at_unix_ms.saturating_add(2),
                trace_id: input.trace_id.clone(),
            };
            if result_fence_drift {
                record.binding_digest = digest::sha256(b"wrong-result-binding");
                record.worker_attempt_id = "worker-attempt-drifted-0001".into();
            }
            record.output_digest = canonical_output_record_digest(&record);
            records.push(record);
        }
        if invalid_value && let Some(record) = records.first_mut() {
            record.scores = vec![f32::NAN];
            record.output_digest = canonical_output_record_digest(record);
        }
        if reverse_result {
            records.reverse();
            let mut inner = self.inner.lock();
            inner.result_reorders_applied = inner.result_reorders_applied.saturating_add(1);
        }
        if duplicate_result && let Some(record) = records.first().cloned() {
            records.push(record);
        }
        let mut result = InferenceResultBatch {
            schema_version: "inference-central-grpc-batch/v1".into(),
            request_id: input.request_id,
            route: input.route,
            records,
            batch_digest: String::new(),
            trace_id: input.trace_id,
        };
        result.batch_digest = canonical_result_batch_digest(&result);
        Ok(Response::new(result))
    }
}

/// Deterministic Go/PostgreSQL commit-boundary fake.
#[derive(Clone, Debug)]
pub struct FakeControl {
    inner: Arc<Mutex<ControlInner>>,
}

#[derive(Debug)]
struct ControlInner {
    fail_commit_remaining: u32,
    delay_commit_remaining: u32,
    commit_delay_ms: u64,
    duplicate_ack_remaining: u32,
    reverse_ack_remaining: u32,
    ack_reorders_applied: u64,
    commit_attempts: Vec<String>,
    committed_batches: Vec<InferenceResultBatch>,
    rule_observation_batches: Vec<RuleObservationBatch>,
}

impl FakeControl {
    pub fn new(fail_commit_remaining: u32) -> Self {
        Self {
            inner: Arc::new(Mutex::new(ControlInner {
                fail_commit_remaining,
                delay_commit_remaining: 0,
                commit_delay_ms: 0,
                duplicate_ack_remaining: 0,
                reverse_ack_remaining: 0,
                ack_reorders_applied: 0,
                commit_attempts: Vec::new(),
                committed_batches: Vec::new(),
                rule_observation_batches: Vec::new(),
            })),
        }
    }

    pub fn commit_attempts(&self) -> Vec<String> {
        self.inner.lock().commit_attempts.clone()
    }

    pub fn committed_batches(&self) -> Vec<InferenceResultBatch> {
        self.inner.lock().committed_batches.clone()
    }

    pub fn rule_observation_batches(&self) -> Vec<RuleObservationBatch> {
        self.inner.lock().rule_observation_batches.clone()
    }

    pub fn delay_commit_calls(&self, calls: u32, delay_ms: u64) {
        let mut inner = self.inner.lock();
        inner.delay_commit_remaining = calls;
        inner.commit_delay_ms = delay_ms;
    }

    pub fn duplicate_next_ack(&self) {
        self.inner.lock().duplicate_ack_remaining = 1;
    }

    pub fn reverse_next_ack(&self) {
        self.inner.lock().reverse_ack_remaining = 1;
    }

    pub fn ack_reorders_applied(&self) -> u64 {
        self.inner.lock().ack_reorders_applied
    }
}

#[tonic::async_trait]
impl ControlSink for FakeControl {
    async fn commit_results(
        &self,
        request: Request<InferenceResultBatch>,
    ) -> Result<Response<CanonicalAckBatch>, Status> {
        let batch = request.into_inner();
        let (delay_ms, duplicate_ack, reverse_ack, already_committed) = {
            let mut inner = self.inner.lock();
            inner.commit_attempts.push(batch.batch_digest.clone());
            if inner.fail_commit_remaining > 0 {
                inner.fail_commit_remaining = inner.fail_commit_remaining.saturating_sub(1);
                return Err(Status::unavailable("injected PostgreSQL boundary outage"));
            }
            let delay_ms = if inner.delay_commit_remaining == 0 {
                0
            } else {
                inner.delay_commit_remaining = inner.delay_commit_remaining.saturating_sub(1);
                inner.commit_delay_ms
            };
            let duplicate_ack = inner.duplicate_ack_remaining > 0;
            inner.duplicate_ack_remaining = inner.duplicate_ack_remaining.saturating_sub(1);
            let reverse_ack = batch.records.len() > 1 && inner.reverse_ack_remaining > 0;
            if reverse_ack {
                inner.reverse_ack_remaining = inner.reverse_ack_remaining.saturating_sub(1);
                inner.ack_reorders_applied = inner.ack_reorders_applied.saturating_add(1);
            }
            let already_committed = inner
                .committed_batches
                .iter()
                .any(|committed| committed.batch_digest == batch.batch_digest);
            if !already_committed {
                inner.committed_batches.push(batch.clone());
            }
            (delay_ms, duplicate_ack, reverse_ack, already_committed)
        };
        if delay_ms > 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }
        let mut acknowledgements: Vec<CanonicalAck> = batch
            .records
            .iter()
            .map(|record| CanonicalAck {
                event_idempotency_key: record.event_idempotency_key.clone(),
                input_digest: record.input_digest.clone(),
                output_digest: record.output_digest.clone(),
                canonical_event_id: format!("canonical-{}", record.input_id),
                committed_at_unix_ms: 1_893_456_020_000,
                status: if already_committed {
                    "idempotent"
                } else {
                    "committed"
                }
                .into(),
                reason_code: "POSTGRESQL_EVENT_COMMITTED".into(),
                commit_status: if already_committed {
                    CanonicalCommitStatus::Idempotent
                } else {
                    CanonicalCommitStatus::Committed
                } as i32,
            })
            .collect();
        if reverse_ack {
            acknowledgements.reverse();
        }
        if duplicate_ack && let Some(acknowledgement) = acknowledgements.first().cloned() {
            acknowledgements.push(acknowledgement);
        }
        let mut ack = CanonicalAckBatch {
            schema_version: "canonical-event-ack/v1".into(),
            acknowledgements,
            trace_id: batch.trace_id,
            result_batch_digest: batch.batch_digest,
            ack_batch_digest: String::new(),
        };
        ack.ack_batch_digest = canonical_ack_batch_digest(&ack);
        Ok(Response::new(ack))
    }

    async fn publish_rule_observations(
        &self,
        request: Request<RuleObservationBatch>,
    ) -> Result<Response<PublishAck>, Status> {
        let batch = request.into_inner();
        self.inner
            .lock()
            .rule_observation_batches
            .push(batch.clone());
        Ok(Response::new(PublishAck {
            status: "accepted".into(),
            identity: batch.batch_id,
            digest: batch.batch_digest,
            reason_code: "OBSERVATIONS_ACCEPTED".into(),
            status_code: PublishStatus::Accepted as i32,
        }))
    }

    async fn publish_target_status(
        &self,
        request: Request<TargetStatusBatch>,
    ) -> Result<Response<PublishAck>, Status> {
        let batch = request.into_inner();
        Ok(Response::new(PublishAck {
            status: "accepted".into(),
            identity: batch.trace_id.clone(),
            digest: digest::message_sha256(&batch),
            reason_code: "TARGET_STATUS_ACCEPTED".into(),
            status_code: PublishStatus::Accepted as i32,
        }))
    }
}

/// Running fake server with bounded explicit shutdown.
#[derive(Debug)]
pub struct FakeServerHandle {
    shutdown: Option<oneshot::Sender<()>>,
    join: Option<JoinHandle<Result<(), tonic::transport::Error>>>,
    pub address: SocketAddr,
}

impl FakeServerHandle {
    pub async fn shutdown(mut self) -> Result<(), Box<dyn Error>> {
        if let Some(sender) = self.shutdown.take() {
            let _ignored = sender.send(());
        }
        if let Some(join) = self.join.take() {
            join.await??;
        }
        Ok(())
    }
}

impl Drop for FakeServerHandle {
    fn drop(&mut self) {
        if let Some(sender) = self.shutdown.take() {
            let _ignored = sender.send(());
        }
    }
}

fn server_tls(identity: &LeafIdentity) -> Result<ServerTlsConfig, Box<dyn Error>> {
    Ok(ServerTlsConfig::new()
        .identity(Identity::from_pem(
            identity.certificate_pem.clone(),
            identity.private_key_pem.clone(),
        ))
        .client_ca_root(Certificate::from_pem(identity.ca_pem.clone())))
}

/// Start a P4Runtime fake on an ephemeral loopback socket.
pub async fn spawn_p4(
    service: FakeP4,
    identity: &LeafIdentity,
) -> Result<FakeServerHandle, Box<dyn Error>> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let incoming = TcpListenerStream::new(listener);
    let (shutdown, receiver) = oneshot::channel();
    let tls = server_tls(identity)?;
    let join = tokio::spawn(async move {
        Server::builder()
            .tls_config(tls)?
            .add_service(P4RuntimeServer::new(service))
            .serve_with_incoming_shutdown(incoming, async {
                let _ignored = receiver.await;
            })
            .await
    });
    Ok(FakeServerHandle {
        shutdown: Some(shutdown),
        join: Some(join),
        address,
    })
}

/// Start a Central Inference fake on an ephemeral loopback socket.
pub async fn spawn_inference(
    service: FakeInference,
    identity: &LeafIdentity,
) -> Result<FakeServerHandle, Box<dyn Error>> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let incoming = TcpListenerStream::new(listener);
    let (shutdown, receiver) = oneshot::channel();
    let tls = server_tls(identity)?;
    let join = tokio::spawn(async move {
        Server::builder()
            .tls_config(tls)?
            .add_service(CentralInferenceServer::new(service))
            .serve_with_incoming_shutdown(incoming, async {
                let _ignored = receiver.await;
            })
            .await
    });
    Ok(FakeServerHandle {
        shutdown: Some(shutdown),
        join: Some(join),
        address,
    })
}

/// Start a Go Control commit-sink fake on an ephemeral loopback socket.
pub async fn spawn_control(
    service: FakeControl,
    identity: &LeafIdentity,
) -> Result<FakeServerHandle, Box<dyn Error>> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let incoming = TcpListenerStream::new(listener);
    let (shutdown, receiver) = oneshot::channel();
    let tls = server_tls(identity)?;
    let join = tokio::spawn(async move {
        Server::builder()
            .tls_config(tls)?
            .add_service(ControlSinkServer::new(service))
            .serve_with_incoming_shutdown(incoming, async {
                let _ignored = receiver.await;
            })
            .await
    });
    Ok(FakeServerHandle {
        shutdown: Some(shutdown),
        join: Some(join),
        address,
    })
}

pub fn p4info_bytes() -> Vec<u8> {
    b"masi-p4info-module-v1".to_vec()
}

pub fn device_config_bytes() -> Vec<u8> {
    b"masi-device-config-module-v1".to_vec()
}

pub const PIPELINE_COOKIE: u64 = 0x4d41_5349;
