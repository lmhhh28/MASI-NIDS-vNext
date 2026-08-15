//! mTLS EdgeControl public server and graceful process lifecycle.

use std::{collections::BTreeSet, time::Duration};

use tonic::{
    Request, Response, Status,
    service::interceptor::InterceptedService,
    transport::{Certificate, Identity, Server, ServerTlsConfig},
};

use crate::{
    EdgeError, EdgeResult,
    config::{EdgeConfig, read_pem},
    contract::edge::{
        AcknowledgeEffectRequest, AssignTargetRequest, CommitRouteRequest,
        ConfigureRuleObservationsRequest, EdgeStatus, ExecuteEffectRequest, GetStatusRequest,
        PreflightEffectReply, PreflightEffectRequest, PrepareRouteRequest, PublishAck,
        RenewTargetRequest, ResumeRouteRequest, RevokeTargetRequest, RouteReply, TargetReply,
        edge_control_server::{EdgeControl, EdgeControlServer},
    },
    digest,
    supervisor::TargetSupervisor,
};

/// gRPC adapter; all ownership and state stay in TargetSupervisor/TargetActor.
#[derive(Clone, Debug)]
pub struct EdgeControlService {
    supervisor: TargetSupervisor,
}

impl EdgeControlService {
    /// Create the thin public-boundary adapter.
    #[must_use]
    pub fn new(supervisor: TargetSupervisor) -> Self {
        Self { supervisor }
    }
}

#[tonic::async_trait]
impl EdgeControl for EdgeControlService {
    async fn assign_target(
        &self,
        request: Request<AssignTargetRequest>,
    ) -> Result<Response<TargetReply>, Status> {
        let assignment = request
            .into_inner()
            .assignment
            .ok_or_else(|| Status::invalid_argument("assignment is required"))?;
        self.supervisor
            .assign(assignment)
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn renew_target(
        &self,
        request: Request<RenewTargetRequest>,
    ) -> Result<Response<TargetReply>, Status> {
        self.supervisor
            .renew(request.into_inner())
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn revoke_target(
        &self,
        request: Request<RevokeTargetRequest>,
    ) -> Result<Response<TargetReply>, Status> {
        self.supervisor
            .revoke(request.into_inner())
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn preflight_effect(
        &self,
        request: Request<PreflightEffectRequest>,
    ) -> Result<Response<PreflightEffectReply>, Status> {
        let intent = request
            .into_inner()
            .intent
            .ok_or_else(|| Status::invalid_argument("intent is required"))?;
        self.supervisor
            .preflight(intent)
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn execute_effect(
        &self,
        request: Request<ExecuteEffectRequest>,
    ) -> Result<Response<crate::contract::edge::EffectResult>, Status> {
        let request = request.into_inner();
        let intent = request
            .intent
            .ok_or_else(|| Status::invalid_argument("intent is required"))?;
        self.supervisor
            .execute(intent, request.preflight_token)
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn acknowledge_effect(
        &self,
        request: Request<AcknowledgeEffectRequest>,
    ) -> Result<Response<PublishAck>, Status> {
        self.supervisor
            .acknowledge_effect(request.into_inner())
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn prepare_route(
        &self,
        request: Request<PrepareRouteRequest>,
    ) -> Result<Response<RouteReply>, Status> {
        self.supervisor
            .prepare_route(request.into_inner())
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn commit_route(
        &self,
        request: Request<CommitRouteRequest>,
    ) -> Result<Response<RouteReply>, Status> {
        self.supervisor
            .commit_route(request.into_inner())
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn resume_route(
        &self,
        request: Request<ResumeRouteRequest>,
    ) -> Result<Response<RouteReply>, Status> {
        self.supervisor
            .resume_route(request.into_inner())
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn configure_rule_observations(
        &self,
        request: Request<ConfigureRuleObservationsRequest>,
    ) -> Result<Response<PublishAck>, Status> {
        self.supervisor
            .configure_rule_observations(request.into_inner())
            .await
            .map(Response::new)
            .map_err(Into::into)
    }

    async fn get_status(
        &self,
        request: Request<GetStatusRequest>,
    ) -> Result<Response<EdgeStatus>, Status> {
        let request = request.into_inner();
        Ok(Response::new(
            self.supervisor
                .status(&request.target_id, &request.trace_id)
                .await,
        ))
    }
}

/// Start the real Edge binary public boundary until SIGINT/SIGTERM.
pub async fn run(config: EdgeConfig, config_digest: String) -> EdgeResult<()> {
    let address = config.listen_address;
    let identity = Identity::from_pem(
        read_pem(&config.server_tls.certificate_path, false)?,
        read_pem(&config.server_tls.private_key_path, true)?,
    );
    let client_ca = Certificate::from_pem(read_pem(&config.server_tls.client_ca_path, false)?);
    let tls = ServerTlsConfig::new()
        .identity(identity)
        .client_ca_root(client_ca);
    let message_limit = config
        .limits
        .control_message_bytes
        .max(config.limits.inference_message_bytes);
    let allowed_clients: BTreeSet<String> = config
        .server_tls
        .allowed_client_certificate_sha256
        .iter()
        .cloned()
        .collect();
    let supervisor = TargetSupervisor::open(config, config_digest)?;
    let service = EdgeControlServer::new(EdgeControlService::new(supervisor.clone()))
        .max_decoding_message_size(message_limit)
        .max_encoding_message_size(message_limit);
    let service = InterceptedService::new(service, move |request: Request<()>| {
        authorize_control_peer(request, &allowed_clients)
    });
    let shutdown_supervisor = supervisor.clone();
    let shutdown = async move {
        wait_for_shutdown_signal().await;
        shutdown_supervisor.shutdown().await;
    };
    Server::builder()
        .tls_config(tls)
        .map_err(|error| crate::EdgeError::TlsIdentityMismatch(error.to_string()))?
        .add_service(service)
        .serve_with_shutdown(address, shutdown)
        .await
        .map_err(|error| crate::EdgeError::Remote {
            code: "EDGE_SERVER_FAILED",
            message: error.to_string(),
        })?;
    Ok(())
}

fn authorize_control_peer(
    request: Request<()>,
    allowed_clients: &BTreeSet<String>,
) -> Result<Request<()>, Status> {
    let certificates = request.peer_certs().ok_or_else(|| {
        Status::unauthenticated(
            EdgeError::TlsIdentityMismatch("EdgeControl peer certificate missing".into())
                .to_string(),
        )
    })?;
    let leaf = certificates.first().ok_or_else(|| {
        Status::unauthenticated(
            EdgeError::TlsIdentityMismatch("EdgeControl peer certificate chain empty".into())
                .to_string(),
        )
    })?;
    let observed = digest::sha256(leaf.as_ref());
    if !allowed_clients.contains(&observed) {
        return Err(Status::unauthenticated(
            EdgeError::TlsIdentityMismatch(
                "EdgeControl peer certificate is not in the exact identity allowlist".into(),
            )
            .to_string(),
        ));
    }
    Ok(request)
}

async fn wait_for_shutdown_signal() {
    #[cfg(unix)]
    {
        let terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate());
        match terminate {
            Ok(mut terminate) => {
                tokio::select! {
                    result = tokio::signal::ctrl_c() => {
                        if let Err(error) = result {
                            tracing::error!(reason = %error, "failed to install SIGINT handler");
                        }
                    }
                    _ = terminate.recv() => {}
                }
            }
            Err(error) => {
                tracing::error!(reason = %error, "failed to install SIGTERM handler");
                let _ = tokio::signal::ctrl_c().await;
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
    // Bound in-flight public RPC grace without claiming downstream completion.
    tokio::time::sleep(Duration::from_millis(10)).await;
}
