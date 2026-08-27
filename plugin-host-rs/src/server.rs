//! Manager-facing mTLS gRPC server implementing lifecycle and statistics adapters.

use std::collections::BTreeSet;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use rustls::pki_types::pem::PemObject as _;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio::sync::OwnedSemaphorePermit;
use tokio_rustls::server::TlsStream;
use tokio_stream::StreamExt as _;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::service::interceptor::InterceptedService;
use tonic::transport::Server;
use tonic::transport::server::Connected;
use tonic::{Request, Response, Status};
use tonic_health::ServingStatus;

use crate::admission::sha256_bytes;
use crate::config::read_secure_file;
use crate::contract::control_adapter::plugin_statistics_executor_server::{
    PluginStatisticsExecutor, PluginStatisticsExecutorServer,
};
use crate::contract::control_adapter::{StatisticsExecutionReply, StatisticsExecutionRequest};
use crate::contract::host::plugin_host_control_server::{
    PluginHostControl, PluginHostControlServer,
};
use crate::contract::host::{
    ApplyBindingRequest, BindingObservation, CancelInvocationReply, CancelInvocationRequest,
    DrainBindingRequest, ExecuteReply, ExecuteRequest, GetBindingRequest, ListBindingsReply,
    ListBindingsRequest, RevokeBindingRequest, RollbackBindingRequest,
};
use crate::error::{HostError, HostResult, ReasonCode};
use crate::state::HostState;

/// Serve all Manager-facing public services until shutdown.
pub async fn serve(
    state: Arc<HostState>,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) -> HostResult<()> {
    let address = state.config().listen_socket()?;
    let certificate = read_secure_file(
        &state.config().server_tls.certificate_path,
        1024 * 1024,
        false,
    )?;
    let private_key = read_secure_file(
        &state.config().server_tls.private_key_path,
        1024 * 1024,
        true,
    )?;
    let client_ca = read_secure_file(
        &state.config().server_tls.client_ca_path,
        1024 * 1024,
        false,
    )?;
    let tls = tls13_server_config(&certificate, &private_key, &client_ca)?;
    let allowed: BTreeSet<_> = state
        .config()
        .server_tls
        .allowed_client_certificate_sha256
        .iter()
        .cloned()
        .collect();
    let max = state.config().limits.max_control_message_bytes;
    let control = PluginHostControlServer::new(ControlService {
        state: state.clone(),
    })
    .max_decoding_message_size(max)
    .max_encoding_message_size(max);
    let control_allowed = allowed.clone();
    let health_allowed = allowed.clone();
    let control = InterceptedService::new(control, move |request: Request<()>| {
        authorize_peer(request, &control_allowed)
    });
    let statistics = PluginStatisticsExecutorServer::new(StatisticsService {
        state: state.clone(),
    })
    .max_decoding_message_size(max)
    .max_encoding_message_size(max);
    let statistics = InterceptedService::new(statistics, move |request: Request<()>| {
        authorize_peer(request, &allowed)
    });
    let (health_reporter, health) = tonic_health::server::health_reporter();
    health_reporter
        .set_service_status("", ServingStatus::Serving)
        .await;
    health_reporter
        .set_service_status(
            "masi.control.adapter.v1.PluginStatisticsExecutor",
            ServingStatus::Serving,
        )
        .await;
    let health = InterceptedService::new(health, move |request: Request<()>| {
        authorize_peer(request, &health_allowed)
    });
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .map_err(|error| {
            HostError::new(
                ReasonCode::Unavailable,
                format!("control listener: {error}"),
            )
        })?;
    state.set_control_ready(true);
    let connection_permits = Arc::new(tokio::sync::Semaphore::new(
        state.config().limits.global_queue_depth,
    ));
    let acceptor = tokio_rustls::TlsAcceptor::from(tls);
    let metrics = state.metrics.clone();
    let incoming = TcpListenerStream::new(listener).then(move |accepted| {
        let acceptor = acceptor.clone();
        let connection_permits = connection_permits.clone();
        let metrics = metrics.clone();
        async move {
            let stream = accepted?;
            let permit = connection_permits
                .try_acquire_owned()
                .map_err(|_| std::io::Error::other("manager connection bound exhausted"))?;
            let stream = tokio::time::timeout(Duration::from_secs(2), acceptor.accept(stream))
                .await
                .map_err(|_| {
                    std::io::Error::new(std::io::ErrorKind::TimedOut, "TLS handshake timeout")
                })?
                .map_err(std::io::Error::other)?;
            if stream.get_ref().1.protocol_version() != Some(rustls::ProtocolVersion::TLSv1_3) {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "TLS version is not 1.3",
                ));
            }
            metrics.connection_opened();
            Ok(BoundedTlsStream {
                inner: Box::pin(stream),
                _permit: permit,
                metrics,
            })
        }
    });
    let max_streams = u32::try_from(state.config().limits.global_queue_depth).unwrap_or(64);
    let result = Server::builder()
        .concurrency_limit_per_connection(state.config().limits.global_queue_depth)
        .max_concurrent_streams(max_streams)
        .load_shed(true)
        .timeout(Duration::from_millis(state.config().limits.max_deadline_ms))
        .add_service(health)
        .add_service(control)
        .add_service(statistics)
        .serve_with_incoming_shutdown(incoming, async move {
            while !*shutdown.borrow() {
                if shutdown.changed().await.is_err() {
                    break;
                }
            }
        })
        .await
        .map_err(|error| {
            HostError::new(ReasonCode::Unavailable, format!("control server: {error}"))
        });
    state.set_control_ready(false);
    result
}

struct BoundedTlsStream {
    inner: Pin<Box<TlsStream<TcpStream>>>,
    _permit: OwnedSemaphorePermit,
    metrics: Arc<crate::metrics::HostMetrics>,
}

impl Drop for BoundedTlsStream {
    fn drop(&mut self) {
        self.metrics.connection_closed();
    }
}

impl AsyncRead for BoundedTlsStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        self.inner.as_mut().poll_read(context, buffer)
    }
}

impl AsyncWrite for BoundedTlsStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<Result<usize, std::io::Error>> {
        self.inner.as_mut().poll_write(context, buffer)
    }

    fn poll_flush(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        self.inner.as_mut().poll_flush(context)
    }

    fn poll_shutdown(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        self.inner.as_mut().poll_shutdown(context)
    }
}

impl Connected for BoundedTlsStream {
    type ConnectInfo = <TlsStream<TcpStream> as Connected>::ConnectInfo;

    fn connect_info(&self) -> Self::ConnectInfo {
        Connected::connect_info(self.inner.as_ref().get_ref())
    }
}

fn tls13_server_config(
    certificate_pem: &[u8],
    private_key_pem: &[u8],
    client_ca_pem: &[u8],
) -> HostResult<Arc<rustls::ServerConfig>> {
    let certificates = CertificateDer::pem_slice_iter(certificate_pem)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("server certificate PEM: {error}"),
            )
        })?;
    if certificates.is_empty() {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "server certificate chain is empty",
        ));
    }
    let private_key = PrivateKeyDer::from_pem_slice(private_key_pem).map_err(|error| {
        HostError::new(
            ReasonCode::InvalidArgument,
            format!("server private key PEM: {error}"),
        )
    })?;
    let client_authorities = CertificateDer::pem_slice_iter(client_ca_pem)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("client CA PEM: {error}"),
            )
        })?;
    let mut roots = rustls::RootCertStore::empty();
    let parsed = roots.add_parsable_certificates(client_authorities);
    if parsed.0 == 0 || parsed.1 != 0 {
        return Err(HostError::new(
            ReasonCode::InvalidArgument,
            "client CA PEM is empty or malformed",
        ));
    }
    let verifier = rustls::server::WebPkiClientVerifier::builder(Arc::new(roots))
        .build()
        .map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("client verifier: {error}"),
            )
        })?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut config = rustls::ServerConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(|error| HostError::new(ReasonCode::Internal, format!("TLS1.3 profile: {error}")))?
        .with_client_cert_verifier(verifier)
        .with_single_cert(certificates, private_key)
        .map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("server TLS identity: {error}"),
            )
        })?;
    config.alpn_protocols = vec![b"h2".to_vec()];
    Ok(Arc::new(config))
}

#[derive(Clone)]
struct ControlService {
    state: Arc<HostState>,
}

#[tonic::async_trait]
impl PluginHostControl for ControlService {
    async fn apply_binding(
        &self,
        request: Request<ApplyBindingRequest>,
    ) -> Result<Response<BindingObservation>, Status> {
        let request = request.into_inner();
        if request.schema_version != "plugin-host-control/v1" {
            return Err(HostError::new(
                ReasonCode::UnknownVersion,
                "apply binding request version rejected",
            )
            .into_status());
        }
        let binding = request.binding.ok_or_else(|| {
            HostError::new(ReasonCode::InvalidArgument, "binding envelope missing").into_status()
        })?;
        self.state
            .apply_binding(binding)
            .await
            .map(Response::new)
            .map_err(HostError::into_status)
    }

    async fn get_binding(
        &self,
        request: Request<GetBindingRequest>,
    ) -> Result<Response<BindingObservation>, Status> {
        let request = request.into_inner();
        require_version(&request.schema_version, "plugin-host-control/v1")?;
        self.state
            .get_binding(&request.plugin_id, request.binding_generation)
            .await
            .map(Response::new)
            .map_err(HostError::into_status)
    }

    async fn list_bindings(
        &self,
        request: Request<ListBindingsRequest>,
    ) -> Result<Response<ListBindingsReply>, Status> {
        let request = request.into_inner();
        require_version(&request.schema_version, "plugin-host-control/v1")?;
        let (bindings, next_page_token) = self
            .state
            .list_bindings(request.page_size, &request.page_token)
            .await
            .map_err(HostError::into_status)?;
        Ok(Response::new(ListBindingsReply {
            schema_version: "plugin-host-binding-list/v1".to_owned(),
            bindings,
            next_page_token,
            trace_id: request.trace_id,
        }))
    }

    async fn execute(
        &self,
        request: Request<ExecuteRequest>,
    ) -> Result<Response<ExecuteReply>, Status> {
        self.state
            .execute(request.into_inner())
            .await
            .map(Response::new)
            .map_err(HostError::into_status)
    }

    async fn cancel_invocation(
        &self,
        request: Request<CancelInvocationRequest>,
    ) -> Result<Response<CancelInvocationReply>, Status> {
        let request = request.into_inner();
        require_version(&request.schema_version, "plugin-host-control/v1")?;
        let requested = self
            .state
            .cancel_invocation(
                &request.plugin_id,
                request.binding_generation,
                &request.invocation_id,
                request.reason_code == "EPOCH_INTERRUPT_TEST",
            )
            .await
            .map_err(HostError::into_status)?;
        Ok(Response::new(CancelInvocationReply {
            schema_version: "plugin-host-cancel-result/v1".to_owned(),
            invocation_id: request.invocation_id,
            cancellation_requested: requested,
            status: if requested { "cancelled" } else { "not_found" }.to_owned(),
            reason_code: if requested {
                ReasonCode::Cancelled.as_str()
            } else {
                ReasonCode::Unavailable.as_str()
            }
            .to_owned(),
            trace_id: request.trace_id,
        }))
    }

    async fn drain_binding(
        &self,
        request: Request<DrainBindingRequest>,
    ) -> Result<Response<BindingObservation>, Status> {
        let request = request.into_inner();
        require_version(&request.schema_version, "plugin-host-control/v1")?;
        self.state
            .drain_binding_authorized(
                &request.operation_id,
                &request.plugin_id,
                request.binding_generation,
                request.deadline_ms,
                &request.trace_id,
                &request.actor_ref,
                &request.reason_code,
                &request.expected_envelope_digest,
                &request.authorization_digest,
            )
            .await
            .map(Response::new)
            .map_err(HostError::into_status)
    }

    async fn revoke_binding(
        &self,
        request: Request<RevokeBindingRequest>,
    ) -> Result<Response<BindingObservation>, Status> {
        let request = request.into_inner();
        require_version(&request.schema_version, "plugin-host-control/v1")?;
        self.state
            .revoke_binding(
                &request.operation_id,
                &request.plugin_id,
                request.binding_generation,
                &request.revocation_digest,
                request.deadline_ms,
                &request.trace_id,
                &request.actor_ref,
                &request.reason_code,
                &request.expected_envelope_digest,
                &request.authorization_digest,
            )
            .await
            .map(Response::new)
            .map_err(HostError::into_status)
    }

    async fn rollback_binding(
        &self,
        request: Request<RollbackBindingRequest>,
    ) -> Result<Response<BindingObservation>, Status> {
        let request = request.into_inner();
        require_version(&request.schema_version, "plugin-host-control/v1")?;
        let target_binding = request.target_binding.ok_or_else(|| {
            HostError::new(
                ReasonCode::InvalidArgument,
                "rollback target binding envelope missing",
            )
            .into_status()
        })?;
        self.state
            .rollback_binding(
                &request.operation_id,
                request.from_generation,
                target_binding,
                &request.authorization_digest,
                &request.trace_id,
                &request.actor_ref,
                &request.reason_code,
            )
            .await
            .map(Response::new)
            .map_err(HostError::into_status)
    }
}

#[derive(Clone)]
struct StatisticsService {
    state: Arc<HostState>,
}

#[tonic::async_trait]
impl PluginStatisticsExecutor for StatisticsService {
    async fn execute_statistics(
        &self,
        request: Request<StatisticsExecutionRequest>,
    ) -> Result<Response<StatisticsExecutionReply>, Status> {
        self.state
            .execute_statistics(request.into_inner())
            .await
            .map(Response::new)
            .map_err(HostError::into_status)
    }
}

fn authorize_peer(request: Request<()>, allowed: &BTreeSet<String>) -> Result<Request<()>, Status> {
    let certificates = request.peer_certs().ok_or_else(|| {
        HostError::new(
            ReasonCode::PublisherUntrusted,
            "manager peer certificate missing",
        )
        .into_status()
    })?;
    let leaf = certificates.first().ok_or_else(|| {
        HostError::new(
            ReasonCode::PublisherUntrusted,
            "manager peer certificate chain empty",
        )
        .into_status()
    })?;
    let digest = sha256_bytes(leaf.as_ref());
    if !allowed.contains(&digest) {
        return Err(HostError::new(
            ReasonCode::PublisherUntrusted,
            "manager leaf certificate is not allowlisted",
        )
        .into_status());
    }
    Ok(request)
}

fn require_version(actual: &str, expected: &str) -> Result<(), Status> {
    if actual != expected {
        return Err(HostError::new(
            ReasonCode::UnknownVersion,
            "control request version rejected",
        )
        .into_status());
    }
    Ok(())
}
