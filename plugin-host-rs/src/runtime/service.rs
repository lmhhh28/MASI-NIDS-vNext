//! UDS identity and typed invocation for an already-deployed Host-managed service.

use std::fs::FileType;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::sync::Arc;
use std::time::Duration;

use hyper_util::rt::TokioIo;
use tokio::net::UnixStream;
use tonic::transport::{Channel, Endpoint, Uri};
use tower::service_fn;

use crate::config::{ServiceEndpoint, validate_no_symlink_path};
use crate::contract::host::{BindingEnvelope, ExecuteRequest};
use crate::contract::service::host_managed_plugin_client::HostManagedPluginClient;
use crate::contract::service::{
    HandshakeRequest, HealthRequest, ServiceDisableRequest, ServiceDrainRequest,
    ServiceExecuteRequest,
};
use crate::error::{HostError, HostResult, ReasonCode};
use crate::runtime::{InterruptReason, InvocationControl};

/// Connected exact Host-managed service runtime.
pub struct ServiceRuntime {
    binding: BindingEnvelope,
    client: HostManagedPluginClient<Channel>,
}

impl ServiceRuntime {
    /// Connect over an allowlisted UDS and complete the exact handshake.
    pub async fn connect(
        endpoint: &ServiceEndpoint,
        binding: &BindingEnvelope,
    ) -> HostResult<Self> {
        validate_uds_endpoint(endpoint)?;
        let path = endpoint.uds_path.clone();
        let expected_uid = endpoint.expected_uid;
        let expected_gid = endpoint.expected_gid;
        let channel = Endpoint::try_from("http://[::]:50051")
            .map_err(|error| {
                HostError::new(
                    ReasonCode::Internal,
                    format!("UDS endpoint builder: {error}"),
                )
            })?
            .connect_timeout(Duration::from_secs(2))
            .timeout(Duration::from_secs(10))
            .connect_with_connector(service_fn(move |_: Uri| {
                let path = path.clone();
                async move {
                    let stream = UnixStream::connect(path).await?;
                    let credentials = stream.peer_cred()?;
                    if credentials.uid() != expected_uid || credentials.gid() != expected_gid {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::PermissionDenied,
                            "SO_PEERCRED mismatch",
                        ));
                    }
                    Ok::<_, std::io::Error>(TokioIo::new(stream))
                }
            }))
            .await
            .map_err(|error| {
                HostError::new(
                    ReasonCode::ServiceIdentityMismatch,
                    format!("service UDS connect: {error}"),
                )
            })?;
        let mut client = HostManagedPluginClient::new(channel)
            .max_decoding_message_size(2_097_152)
            .max_encoding_message_size(4_194_304);
        let challenge = uuid::Uuid::new_v4().simple().to_string();
        let response = tokio::time::timeout(
            Duration::from_secs(2),
            client.handshake(HandshakeRequest {
                schema_version: "plugin-service-handshake/v1".to_owned(),
                plugin_id: binding.plugin_id.clone(),
                plugin_revision: binding.plugin_revision.clone(),
                artifact_digest: binding.artifact_digest.clone(),
                config_digest: binding.config_digest.clone(),
                capability_digest: binding.capability_digest.clone(),
                binding_generation: binding.binding_generation,
                binding_epoch: binding.binding_epoch.clone(),
                service_proto_digest: binding.service_proto_digest.clone(),
                challenge_nonce: challenge.clone(),
                trace_id: binding.trace_id.clone(),
            }),
        )
        .await
        .map_err(|_| HostError::new(ReasonCode::DeadlineExceeded, "service handshake deadline"))?
        .map_err(|error| {
            HostError::new(
                ReasonCode::ServiceIdentityMismatch,
                format!("service handshake RPC: {error}"),
            )
        })?
        .into_inner();
        if response.schema_version != "plugin-service-handshake-result/v1"
            || response.plugin_id != binding.plugin_id
            || response.plugin_revision != binding.plugin_revision
            || response.artifact_digest != binding.artifact_digest
            || response.config_digest != binding.config_digest
            || response.capability_digest != binding.capability_digest
            || response.binding_generation != binding.binding_generation
            || response.binding_epoch != binding.binding_epoch
            || response.service_proto_digest != binding.service_proto_digest
            || response.challenge_nonce != challenge
            || response.status != "ready"
            || response.workload_identity != endpoint.expected_workload_identity
        {
            return Err(HostError::new(
                ReasonCode::ServiceIdentityMismatch,
                "service handshake readback mismatch",
            ));
        }
        Ok(Self {
            binding: binding.clone(),
            client,
        })
    }

    /// Execute one typed service call with bounded cancellation/deadline.
    pub async fn execute(
        &self,
        request: &ExecuteRequest,
        control: Arc<InvocationControl>,
    ) -> HostResult<Vec<u8>> {
        let rpc = async {
            let mut client = self.client.clone();
            client
                .execute(ServiceExecuteRequest {
                    schema_version: "plugin-service-execute/v1".to_owned(),
                    invocation_id: request.invocation_id.clone(),
                    plugin_id: request.plugin_id.clone(),
                    binding_generation: request.binding_generation,
                    binding_epoch: request.binding_epoch.clone(),
                    capability_id: request.capability_id.clone(),
                    input_digest: request.input_digest.clone(),
                    input: request.input.clone(),
                    deadline_ms: request.deadline_ms,
                    result_fence: request.result_fence.clone(),
                    trace_id: request.trace_id.clone(),
                    execution_mode: request.execution_mode.clone(),
                })
                .await
        };
        let response = tokio::select! {
            _ = control.cancelled() => {
                return Err(match control.reason() {
                    InterruptReason::Deadline => HostError::new(ReasonCode::DeadlineExceeded, "service invocation deadline exceeded"),
                    _ => HostError::new(ReasonCode::Cancelled, "service invocation cancelled"),
                });
            }
            result = tokio::time::timeout(Duration::from_millis(u64::from(request.deadline_ms)), rpc) => {
                result.map_err(|_| HostError::new(ReasonCode::DeadlineExceeded, "service invocation deadline exceeded"))?
                    .map_err(map_service_status)?
                    .into_inner()
            }
        };
        if response.schema_version != "plugin-service-execute-result/v1"
            || response.invocation_id != request.invocation_id
            || response.plugin_id != request.plugin_id
            || response.binding_generation != request.binding_generation
            || response.binding_epoch != request.binding_epoch
            || response.input_digest != request.input_digest
            || response.result_fence != request.result_fence
            || response.execution_mode != request.execution_mode
            || response.status != "succeeded"
        {
            return Err(HostError::new(
                ReasonCode::Fenced,
                "service execution result identity mismatch",
            ));
        }
        if crate::admission::sha256_bytes(&response.output) != response.output_digest {
            return Err(HostError::new(
                ReasonCode::DigestMismatch,
                "service output digest mismatch",
            ));
        }
        Ok(response.output)
    }

    /// Read exact service health.
    pub async fn health(&self, trace_id: &str) -> HostResult<()> {
        let mut client = self.client.clone();
        let response = client
            .health(HealthRequest {
                schema_version: "plugin-service-health/v1".to_owned(),
                plugin_id: self.binding.plugin_id.clone(),
                binding_generation: self.binding.binding_generation,
                trace_id: trace_id.to_owned(),
            })
            .await
            .map_err(|error| {
                HostError::new(
                    ReasonCode::Unavailable,
                    format!("service health RPC: {error}"),
                )
            })?
            .into_inner();
        if response.schema_version != "plugin-service-health-result/v1"
            || response.plugin_id != self.binding.plugin_id
            || response.binding_generation != self.binding.binding_generation
            || response.trace_id != trace_id
            || !response.ready
            || !response.live
            || response.status != "ready"
        {
            return Err(HostError::new(
                ReasonCode::Unavailable,
                "service health is not ready/live",
            ));
        }
        Ok(())
    }

    /// Ask the service to stop new work and drain.
    pub async fn drain(&self, deadline_ms: u32, trace_id: &str) -> HostResult<()> {
        let mut client = self.client.clone();
        let response = tokio::time::timeout(
            Duration::from_millis(u64::from(deadline_ms).max(1)),
            client.drain(ServiceDrainRequest {
                schema_version: "plugin-service-drain/v1".to_owned(),
                plugin_id: self.binding.plugin_id.clone(),
                binding_generation: self.binding.binding_generation,
                deadline_ms,
                trace_id: trace_id.to_owned(),
            }),
        )
        .await
        .map_err(|_| HostError::new(ReasonCode::DeadlineExceeded, "service drain deadline"))?
        .map_err(|error| {
            HostError::new(
                ReasonCode::Unavailable,
                format!("service drain RPC: {error}"),
            )
        })?
        .into_inner();
        if response.schema_version != "plugin-service-drain-result/v1"
            || response.plugin_id != self.binding.plugin_id
            || response.binding_generation != self.binding.binding_generation
            || response.trace_id != trace_id
            || !response.drained
        {
            return Err(HostError::new(
                ReasonCode::DeadlineExceeded,
                "service did not drain",
            ));
        }
        Ok(())
    }

    /// Disable the exact service generation after revoke.
    pub async fn disable(
        &self,
        revocation_digest: &str,
        deadline_ms: u32,
        trace_id: &str,
    ) -> HostResult<()> {
        let mut client = self.client.clone();
        let response = tokio::time::timeout(
            Duration::from_millis(u64::from(deadline_ms).max(1)),
            client.disable(ServiceDisableRequest {
                schema_version: "plugin-service-disable/v1".to_owned(),
                plugin_id: self.binding.plugin_id.clone(),
                binding_generation: self.binding.binding_generation,
                revocation_digest: revocation_digest.to_owned(),
                trace_id: trace_id.to_owned(),
            }),
        )
        .await
        .map_err(|_| HostError::new(ReasonCode::DeadlineExceeded, "service disable deadline"))?
        .map_err(|error| {
            HostError::new(
                ReasonCode::Unavailable,
                format!("service disable RPC: {error}"),
            )
        })?
        .into_inner();
        if response.schema_version != "plugin-service-disable-result/v1"
            || response.plugin_id != self.binding.plugin_id
            || response.binding_generation != self.binding.binding_generation
            || response.trace_id != trace_id
            || !response.disabled
        {
            return Err(HostError::new(
                ReasonCode::Unavailable,
                "service disable not confirmed",
            ));
        }
        Ok(())
    }
}

fn map_service_status(error: tonic::Status) -> HostError {
    let reason = match error.code() {
        tonic::Code::Cancelled => ReasonCode::Cancelled,
        tonic::Code::DeadlineExceeded => ReasonCode::DeadlineExceeded,
        tonic::Code::ResourceExhausted => ReasonCode::ResourceExhausted,
        tonic::Code::FailedPrecondition | tonic::Code::AlreadyExists => ReasonCode::Fenced,
        tonic::Code::Internal if error.message().contains("PLUGIN_TRAP") => ReasonCode::PluginTrap,
        _ => ReasonCode::Unavailable,
    };
    HostError::new(reason, format!("service execute RPC: {error}"))
}

fn validate_uds_endpoint(endpoint: &ServiceEndpoint) -> HostResult<()> {
    validate_no_symlink_path(&endpoint.uds_path, true)?;
    let parent = endpoint.uds_path.parent().ok_or_else(|| {
        HostError::new(
            ReasonCode::ServiceIdentityMismatch,
            "UDS has no parent directory",
        )
    })?;
    let parent_metadata = std::fs::metadata(parent).map_err(|error| {
        HostError::new(
            ReasonCode::ServiceIdentityMismatch,
            format!("UDS parent metadata: {error}"),
        )
    })?;
    if !parent_metadata.is_dir()
        || parent_metadata.uid() != endpoint.expected_uid
        || parent_metadata.gid() != endpoint.expected_gid
        || parent_metadata.permissions().mode() & 0o777 != 0o750
    {
        return Err(HostError::new(
            ReasonCode::ServiceIdentityMismatch,
            "UDS directory owner/group/mode mismatch",
        ));
    }
    let socket_metadata = std::fs::symlink_metadata(&endpoint.uds_path).map_err(|error| {
        HostError::new(
            ReasonCode::ServiceIdentityMismatch,
            format!("UDS socket metadata: {error}"),
        )
    })?;
    let file_type: FileType = socket_metadata.file_type();
    if !file_type.is_socket()
        || socket_metadata.uid() != endpoint.expected_uid
        || socket_metadata.gid() != endpoint.expected_gid
        || socket_metadata.permissions().mode() & 0o777 != 0o660
    {
        return Err(HostError::new(
            ReasonCode::ServiceIdentityMismatch,
            "UDS socket owner/group/mode mismatch",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn regular_file_is_not_a_socket() -> Result<(), Box<dyn std::error::Error>> {
        let temp = tempfile::tempdir()?;
        std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o750))?;
        let path = temp.path().join("service.sock");
        std::fs::write(&path, b"not a socket")?;
        let metadata = std::fs::metadata(temp.path())?;
        let endpoint = ServiceEndpoint {
            endpoint_ref: "fixture".to_owned(),
            transport: "uds".to_owned(),
            uds_path: path,
            expected_uid: metadata.uid(),
            expected_gid: metadata.gid(),
            expected_workload_identity: "spiffe://masi.test/plugin/fixture".to_owned(),
            service_proto_digest: crate::admission::qualified_service_proto_digest().to_owned(),
            artifact_digest: format!("sha256:{}", "a".repeat(64)),
        };
        assert!(validate_uds_endpoint(&endpoint).is_err());
        Ok(())
    }
}
