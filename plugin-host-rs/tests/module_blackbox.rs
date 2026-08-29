#![allow(missing_docs)]

mod support;

use std::io::{Read as _, Write as _};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use masi_plugin_host::HostState;
use masi_plugin_host::ReasonCode;
use masi_plugin_host::admission::{compute_envelope_digest, sha256_bytes};
use masi_plugin_host::contract::control_adapter::StatisticsExecutionRequest;
use masi_plugin_host::contract::control_adapter::plugin_statistics_executor_client::PluginStatisticsExecutorClient;
use masi_plugin_host::contract::host::plugin_host_control_client::PluginHostControlClient;
use masi_plugin_host::contract::host::{
    ApplyBindingRequest, DrainBindingRequest, ExecuteRequest, GetBindingRequest,
    RevokeBindingRequest, RollbackBindingRequest,
};
use masi_plugin_host::statistics::{
    FrozenInputBundle, StatisticsArtifact, compute_frozen_input_digest, compute_input_bundle_digest,
};
use rustls::pki_types::pem::PemObject as _;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName};
use support::{
    PLUGIN_SCOPE, SERVICE_PLUGIN_ID, STATISTICS_DEFINITION_ID, TestEnvironment, WASM_PLUGIN_ID,
    WASM_STATISTICS_PLUGIN_ID, digest, refresh_binding, unix_ms,
};
use tonic::Code;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity};

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn real_process_mtls_lifecycle_service_statistics_and_isolation()
-> Result<(), Box<dyn std::error::Error>> {
    let environment = TestEnvironment::new(true)?;
    let mut host = ProcessGuard::spawn(
        &host_binary(),
        &["--config", path_text(&environment.config_path)?],
        &environment.temporary.path().join("host.log"),
    )?;
    wait_for_http_ok(environment.health_address, "/readyz").await?;
    for probe in ["startup", "ready", "live"] {
        let status = Command::new(host_binary())
            .args([
                "--config",
                path_text(&environment.config_path)?,
                "--probe",
                probe,
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()?;
        assert!(status.success());
    }
    assert_plaintext_rejected(environment.listen_address)?;
    assert_tls12_rejected(&environment)?;

    let channel = manager_channel(
        &environment,
        &environment.manager_certificate_pem,
        &environment.manager_private_key_pem,
    )
    .await?;
    let mut control = PluginHostControlClient::new(channel.clone());

    let staged = environment.wasm_binding(1, "staged")?;
    let staged_observation = control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(staged.clone()),
        })
        .await?
        .into_inner();
    assert_eq!(staged_observation.observed_state, "staged");
    let denied = control
        .execute(execute_request(&staged, "active", "staged-denied"))
        .await;
    assert_eq!(
        denied.err().map(|status| status.code()),
        Some(Code::FailedPrecondition)
    );

    let shadow = refresh_binding(&staged, "shadow")?;
    let shadow_observation = control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(shadow.clone()),
        })
        .await?
        .into_inner();
    assert_eq!(shadow_observation.observed_state, "shadow");
    let shadow_reply = control
        .execute(execute_request(&shadow, "shadow", "shadow-ok"))
        .await?
        .into_inner();
    assert_eq!(shadow_reply.execution_mode, "shadow");

    let active = refresh_binding(&shadow, "active")?;
    control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(active.clone()),
        })
        .await?;
    let active_reply = control
        .execute(execute_request(&active, "active", "active-1"))
        .await?
        .into_inner();
    assert_eq!(
        active_reply.output_digest,
        sha256_bytes(&active_reply.output)
    );

    let generation_two_staged = environment.wasm_binding(2, "staged")?;
    control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(generation_two_staged.clone()),
        })
        .await?;
    let generation_two_active = refresh_binding(&generation_two_staged, "active")?;
    control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(generation_two_active.clone()),
        })
        .await?;
    let old_observation = control
        .get_binding(GetBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            plugin_id: WASM_PLUGIN_ID.to_owned(),
            binding_generation: 1,
            trace_id: "observe-old-generation".to_owned(),
        })
        .await?
        .into_inner();
    assert_eq!(old_observation.observed_state, "draining");
    let late_old = control
        .execute(execute_request(&active, "active", "late-old-generation"))
        .await;
    assert_eq!(
        late_old.err().map(|status| status.code()),
        Some(Code::FailedPrecondition)
    );
    control
        .execute(execute_request(
            &generation_two_active,
            "active",
            "generation-two-active",
        ))
        .await?;

    let stale_generation_one_refresh = refresh_binding(&active, "active")?;
    let stale_refresh = control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(stale_generation_one_refresh),
        })
        .await;
    assert_eq!(
        stale_refresh.err().map(|status| status.code()),
        Some(Code::FailedPrecondition),
        "a delayed ordinary refresh must not roll the active pointer backward"
    );

    let mut rollback_target = refresh_binding(&active, "active")?;
    rollback_target.operation_id = "rollback-wasm-2-to-1".to_owned();
    rollback_target.trace_id = "rollback-wasm-2-to-1-trace".to_owned();
    rollback_target.envelope_digest.clear();
    rollback_target.envelope_digest = compute_envelope_digest(&rollback_target);

    let missing_target = control
        .rollback_binding(RollbackBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: rollback_target.operation_id.clone(),
            from_generation: 2,
            target_binding: None,
            authorization_digest: digest("rollback-authorization-2-to-1"),
            trace_id: rollback_target.trace_id.clone(),
            actor_ref: rollback_target.actor_ref.clone(),
            reason_code: "ROLLBACK_AUTHORIZED".to_owned(),
        })
        .await;
    assert_eq!(
        missing_target.err().map(|status| status.code()),
        Some(Code::InvalidArgument)
    );
    let mismatched_operation = control
        .rollback_binding(RollbackBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: "rollback-mismatched-operation".to_owned(),
            from_generation: 2,
            target_binding: Some(rollback_target.clone()),
            authorization_digest: digest("rollback-authorization-2-to-1"),
            trace_id: rollback_target.trace_id.clone(),
            actor_ref: rollback_target.actor_ref.clone(),
            reason_code: "ROLLBACK_AUTHORIZED".to_owned(),
        })
        .await;
    assert_eq!(
        mismatched_operation.err().map(|status| status.code()),
        Some(Code::AlreadyExists)
    );
    let malformed_authorization = control
        .rollback_binding(RollbackBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: rollback_target.operation_id.clone(),
            from_generation: 2,
            target_binding: Some(rollback_target.clone()),
            authorization_digest: "not-a-digest".to_owned(),
            trace_id: rollback_target.trace_id.clone(),
            actor_ref: rollback_target.actor_ref.clone(),
            reason_code: "ROLLBACK_AUTHORIZED".to_owned(),
        })
        .await;
    assert_eq!(
        malformed_authorization.err().map(|status| status.code()),
        Some(Code::InvalidArgument)
    );
    let wrong_source_generation = control
        .rollback_binding(RollbackBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: rollback_target.operation_id.clone(),
            from_generation: 3,
            target_binding: Some(rollback_target.clone()),
            authorization_digest: digest("rollback-authorization-2-to-1"),
            trace_id: rollback_target.trace_id.clone(),
            actor_ref: rollback_target.actor_ref.clone(),
            reason_code: "ROLLBACK_AUTHORIZED".to_owned(),
        })
        .await;
    assert_eq!(
        wrong_source_generation.err().map(|status| status.code()),
        Some(Code::FailedPrecondition)
    );
    let mut non_active_target = rollback_target.clone();
    non_active_target.desired_state = "shadow".to_owned();
    non_active_target.envelope_digest.clear();
    non_active_target.envelope_digest = compute_envelope_digest(&non_active_target);
    let non_active_target = control
        .rollback_binding(RollbackBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: non_active_target.operation_id.clone(),
            from_generation: 2,
            target_binding: Some(non_active_target.clone()),
            authorization_digest: digest("rollback-authorization-2-to-1"),
            trace_id: non_active_target.trace_id.clone(),
            actor_ref: non_active_target.actor_ref.clone(),
            reason_code: "ROLLBACK_AUTHORIZED".to_owned(),
        })
        .await;
    assert_eq!(
        non_active_target.err().map(|status| status.code()),
        Some(Code::InvalidArgument)
    );
    let rollback = control
        .rollback_binding(RollbackBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: rollback_target.operation_id.clone(),
            from_generation: 2,
            target_binding: Some(rollback_target.clone()),
            authorization_digest: digest("rollback-authorization-2-to-1"),
            trace_id: rollback_target.trace_id.clone(),
            actor_ref: rollback_target.actor_ref.clone(),
            reason_code: "ROLLBACK_AUTHORIZED".to_owned(),
        })
        .await?
        .into_inner();
    assert_eq!(rollback.binding_generation, 1);
    assert_eq!(rollback.observed_state, "active");
    control
        .execute(execute_request(
            &rollback_target,
            "active",
            "generation-one-explicit-rollback",
        ))
        .await?;
    let rolled_back_generation_two = control
        .execute(execute_request(
            &generation_two_active,
            "active",
            "generation-two-after-rollback",
        ))
        .await;
    assert_eq!(
        rolled_back_generation_two.err().map(|status| status.code()),
        Some(Code::FailedPrecondition)
    );

    let generation_two_reactivated = refresh_binding(&generation_two_active, "active")?;
    let roll_forward = control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(generation_two_reactivated.clone()),
        })
        .await?
        .into_inner();
    assert_eq!(roll_forward.binding_generation, 2);
    assert_eq!(roll_forward.observed_state, "active");
    control
        .execute(execute_request(
            &generation_two_reactivated,
            "active",
            "generation-two-roll-forward",
        ))
        .await?;

    let service_binding = environment.service_binding("active")?;
    let service_config = environment.write_service_fixture_config(&service_binding, "echo")?;
    let mut service = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment.temporary.path().join("service.log"),
    )?;
    wait_for_socket(&environment.service_socket).await?;
    control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(service_binding.clone()),
        })
        .await?;
    let service_reply = control
        .execute(execute_request(
            &service_binding,
            "active",
            "typed-service-execution",
        ))
        .await?
        .into_inner();
    assert_eq!(
        service_reply.output_digest,
        sha256_bytes(&service_reply.output)
    );

    let statistics_binding = environment.wasm_statistics_binding(1, "active")?;
    control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(statistics_binding.clone()),
        })
        .await?;

    let (statistics_request, frozen) = build_statistics_request(
        &statistics_binding,
        "statistics-run-1",
        unix_ms()?.saturating_add(30_000),
    )?;
    let mut statistics = PluginStatisticsExecutorClient::new(channel.clone());
    let mut unbound_definition = statistics_request.clone();
    unbound_definition.run_id = "statistics-run-unbound-digest".to_owned();
    unbound_definition.definition_digest = digest("unqualified-definition-body");
    let unbound_definition = statistics.execute_statistics(unbound_definition).await;
    assert_eq!(
        unbound_definition.err().map(|status| status.code()),
        Some(Code::FailedPrecondition)
    );
    let mut wrong_revision_bundle = frozen.clone();
    wrong_revision_bundle.record_id = "bundle-run-wrong-revision".to_owned();
    wrong_revision_bundle.run_id = "statistics-run-wrong-revision".to_owned();
    wrong_revision_bundle.definition_revision = "fixture-row-count-v2".to_owned();
    wrong_revision_bundle.bundle_digest.clear();
    wrong_revision_bundle.frozen_input_digest =
        compute_frozen_input_digest(&wrong_revision_bundle)?;
    wrong_revision_bundle.bundle_digest = compute_input_bundle_digest(&wrong_revision_bundle)?;
    let mut wrong_revision = statistics_request.clone();
    wrong_revision.run_id = wrong_revision_bundle.run_id.clone();
    wrong_revision.frozen_input_digest = wrong_revision_bundle.frozen_input_digest.clone();
    wrong_revision.input_bundle_json = serde_json::to_vec(&wrong_revision_bundle)?;
    let wrong_revision = statistics.execute_statistics(wrong_revision).await;
    assert_eq!(
        wrong_revision.err().map(|status| status.code()),
        Some(Code::FailedPrecondition)
    );
    let mut concurrent_statistics = PluginStatisticsExecutorClient::new(channel.clone());
    let (first, repeated) = tokio::join!(
        statistics.execute_statistics(statistics_request.clone()),
        concurrent_statistics.execute_statistics(statistics_request.clone()),
    );
    let first = first?.into_inner();
    let repeated = repeated?.into_inner();
    assert_eq!(first.artifact_digest, repeated.artifact_digest);
    assert_eq!(first.artifact_json, repeated.artifact_json);
    let artifact: StatisticsArtifact = serde_json::from_slice(&first.artifact_json)?;
    assert_eq!(artifact.provenance.plugin_id, WASM_STATISTICS_PLUGIN_ID);
    assert_eq!(artifact.provenance.binding_generation, 1);
    assert_eq!(artifact.metrics[0].value, 2.0);
    assert_eq!(artifact.bytes, first.artifact_json.len());

    let mut conflicting = statistics_request.clone();
    conflicting.trace_id = "statistics-trace-conflict".to_owned();
    let conflict = statistics.execute_statistics(conflicting).await;
    assert_eq!(
        conflict.err().map(|status| status.code()),
        Some(Code::AlreadyExists)
    );

    control
        .revoke_binding(RevokeBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: "revoke-statistics-1".to_owned(),
            plugin_id: WASM_STATISTICS_PLUGIN_ID.to_owned(),
            binding_generation: 1,
            revocation_digest: digest("statistics-revocation"),
            deadline_ms: 5000,
            trace_id: "revoke-statistics-trace".to_owned(),
            actor_ref: "platform-admin:test".to_owned(),
            reason_code: "REVOKE_AUTHORIZED".to_owned(),
            expected_envelope_digest: statistics_binding.envelope_digest.clone(),
            authorization_digest: digest("statistics-revoke-authorization"),
        })
        .await?;
    let revoked_statistics = statistics.execute_statistics(statistics_request).await;
    assert_eq!(
        revoked_statistics.err().map(|status| status.code()),
        Some(Code::Unavailable)
    );

    control
        .revoke_binding(RevokeBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: "revoke-service-1".to_owned(),
            plugin_id: SERVICE_PLUGIN_ID.to_owned(),
            binding_generation: 1,
            revocation_digest: digest("service-revocation"),
            deadline_ms: 5000,
            trace_id: "revoke-service-trace".to_owned(),
            actor_ref: "platform-admin:test".to_owned(),
            reason_code: "REVOKE_AUTHORIZED".to_owned(),
            expected_envelope_digest: service_binding.envelope_digest.clone(),
            authorization_digest: digest("service-revoke-authorization"),
        })
        .await?;
    let revoked = control
        .execute(execute_request(
            &service_binding,
            "active",
            "revoked-service-call",
        ))
        .await;
    assert_eq!(
        revoked.err().map(|status| status.code()),
        Some(Code::FailedPrecondition)
    );

    let unauthorized = manager_channel(
        &environment,
        &environment.other_certificate_pem,
        &environment.other_private_key_pem,
    )
    .await?;
    let unauthorized = PluginHostControlClient::new(unauthorized)
        .get_binding(GetBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            plugin_id: WASM_PLUGIN_ID.to_owned(),
            binding_generation: 2,
            trace_id: "unauthorized-manager".to_owned(),
        })
        .await;
    assert_eq!(
        unauthorized.err().map(|status| status.code()),
        Some(Code::PermissionDenied)
    );

    let drained = control
        .drain_binding(DrainBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            operation_id: "drain-wasm-generation-2".to_owned(),
            plugin_id: WASM_PLUGIN_ID.to_owned(),
            binding_generation: 2,
            deadline_ms: 5000,
            trace_id: "drain-wasm-generation-2-trace".to_owned(),
            actor_ref: "platform-admin:test".to_owned(),
            reason_code: "DRAIN_AUTHORIZED".to_owned(),
            expected_envelope_digest: generation_two_reactivated.envelope_digest.clone(),
            authorization_digest: digest("wasm-drain-authorization"),
        })
        .await?
        .into_inner();
    assert_eq!(drained.observed_state, "stopped");
    assert!(read_http(environment.health_address, "/metrics")?.contains("masi_plugin_host"));

    service.terminate()?;
    host.crash()?;
    let mut restarted = ProcessGuard::spawn(
        &host_binary(),
        &["--config", path_text(&environment.config_path)?],
        &environment.temporary.path().join("host-restarted.log"),
    )?;
    wait_for_http_ok(environment.health_address, "/readyz").await?;
    let restarted_channel = manager_channel(
        &environment,
        &environment.manager_certificate_pem,
        &environment.manager_private_key_pem,
    )
    .await?;
    let mut restarted_control = PluginHostControlClient::new(restarted_channel);
    let absent_after_restart = restarted_control
        .get_binding(GetBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            plugin_id: WASM_PLUGIN_ID.to_owned(),
            binding_generation: 2,
            trace_id: "post-restart-absent".to_owned(),
        })
        .await;
    assert_eq!(
        absent_after_restart.err().map(|status| status.code()),
        Some(Code::Unavailable)
    );
    restarted_control
        .apply_binding(ApplyBindingRequest {
            schema_version: "plugin-host-control/v1".to_owned(),
            binding: Some(generation_two_active.clone()),
        })
        .await?;
    restarted_control
        .execute(execute_request(
            &generation_two_active,
            "active",
            "post-restart-rebuilt",
        ))
        .await?;
    restarted.terminate()?;
    Ok(())
}

#[tokio::test]
async fn in_process_generation_monotonicity_and_explicit_rollback()
-> Result<(), Box<dyn std::error::Error>> {
    let environment = TestEnvironment::new(false)?;
    let state = HostState::new(environment.config.clone());
    let generation_one = environment.wasm_binding(1, "active")?;
    let generation_two = environment.wasm_binding(2, "active")?;
    state.apply_binding(generation_one.clone()).await?;
    state.apply_binding(generation_two.clone()).await?;

    let delayed_refresh = refresh_binding(&generation_one, "active")?;
    let delayed_error = state
        .apply_binding(delayed_refresh)
        .await
        .err()
        .ok_or("delayed lower-generation refresh unexpectedly activated")?;
    assert_eq!(delayed_error.reason, ReasonCode::Fenced);

    let mut rollback_target = refresh_binding(&generation_one, "active")?;
    rollback_target.operation_id = "direct-rollback-2-to-1".to_owned();
    rollback_target.trace_id = "direct-rollback-2-to-1-trace".to_owned();
    rollback_target.envelope_digest.clear();
    rollback_target.envelope_digest = compute_envelope_digest(&rollback_target);
    let mismatch = state
        .rollback_binding(
            "different-operation",
            2,
            rollback_target.clone(),
            &digest("direct-rollback-authorization"),
            &rollback_target.trace_id,
            &rollback_target.actor_ref,
            "ROLLBACK_AUTHORIZED",
        )
        .await
        .err()
        .ok_or("mismatched rollback operation unexpectedly accepted")?;
    assert_eq!(mismatch.reason, ReasonCode::IdempotencyConflict);
    let wrong_source = state
        .rollback_binding(
            &rollback_target.operation_id,
            3,
            rollback_target.clone(),
            &digest("direct-rollback-authorization"),
            &rollback_target.trace_id,
            &rollback_target.actor_ref,
            "ROLLBACK_AUTHORIZED",
        )
        .await
        .err()
        .ok_or("wrong rollback source unexpectedly accepted")?;
    assert_eq!(wrong_source.reason, ReasonCode::Fenced);

    let rollback_operation = rollback_target.operation_id.clone();
    let observation = state
        .rollback_binding(
            &rollback_operation,
            2,
            rollback_target,
            &digest("direct-rollback-authorization"),
            "direct-rollback-2-to-1-trace",
            "platform-admin:test",
            "ROLLBACK_AUTHORIZED",
        )
        .await?;
    assert_eq!(observation.binding_generation, 1);
    assert_eq!(observation.observed_state, "active");

    let roll_forward = refresh_binding(&generation_two, "active")?;
    let observation = state.apply_binding(roll_forward).await?;
    assert_eq!(observation.binding_generation, 2);
    assert_eq!(observation.observed_state, "active");
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn statistics_result_rechecks_lease_and_lifecycle_after_runtime()
-> Result<(), Box<dyn std::error::Error>> {
    {
        let environment = TestEnvironment::new(true)?;
        let binding = environment.service_statistics_binding("active")?;
        let fixture_config =
            environment.write_service_fixture_config(&binding, "statistics-slow")?;
        let mut service = ProcessGuard::spawn(
            &service_binary(),
            &["--config", path_text(&fixture_config)?],
            &environment.temporary.path().join("statistics-lease.log"),
        )?;
        wait_for_socket(&environment.service_socket).await?;
        let state = HostState::new(environment.config.clone());
        state.apply_binding(binding.clone()).await?;
        let (request, _) = build_statistics_request(
            &binding,
            "statistics-expiring-run",
            unix_ms()?.saturating_add(75),
        )?;
        let error = state
            .execute_statistics(request)
            .await
            .err()
            .ok_or("statistics result published after its lease expired")?;
        assert_eq!(error.reason, ReasonCode::Fenced);
        service.terminate()?;
    }

    {
        let environment = TestEnvironment::new(true)?;
        let binding = environment.service_statistics_binding("active")?;
        let fixture_config =
            environment.write_service_fixture_config(&binding, "statistics-slow")?;
        let mut service = ProcessGuard::spawn(
            &service_binary(),
            &["--config", path_text(&fixture_config)?],
            &environment.temporary.path().join("statistics-drain.log"),
        )?;
        wait_for_socket(&environment.service_socket).await?;
        let state = HostState::new(environment.config.clone());
        state.apply_binding(binding.clone()).await?;
        let (request, _) = build_statistics_request(
            &binding,
            "statistics-drained-run",
            unix_ms()?.saturating_add(30_000),
        )?;
        let task = {
            let state = state.clone();
            let request = request.clone();
            tokio::spawn(async move { state.execute_statistics(request).await })
        };
        let mut entered_runtime = false;
        for _ in 0..100 {
            if state.runtime_gauges().await.1 > 0 {
                entered_runtime = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
        assert!(entered_runtime, "statistics fixture did not enter runtime");
        state
            .drain_binding_authorized(
                "statistics-post-execution-drain",
                &binding.plugin_id,
                binding.binding_generation,
                1000,
                "statistics-post-execution-drain-trace",
                "platform-admin:test",
                "DRAIN_AUTHORIZED",
                &binding.envelope_digest,
                &digest("statistics-post-execution-drain-authorization"),
            )
            .await?;
        let error = task
            .await?
            .err()
            .ok_or("statistics result published after binding drain")?;
        assert_eq!(error.reason, ReasonCode::Fenced);
        let retry = state.execute_statistics(request).await;
        assert_eq!(
            retry.err().map(|error| error.reason),
            Some(ReasonCode::Unavailable)
        );
        service.terminate()?;
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn service_faults_cancellation_quarantine_and_trust_reconcile_are_bounded()
-> Result<(), Box<dyn std::error::Error>> {
    let mut fault_cases = Vec::new();
    for (behavior, deadline_ms, expected) in [
        ("hang", 25, ReasonCode::DeadlineExceeded),
        ("trap", 1000, ReasonCode::PluginTrap),
        ("wrong-digest", 1000, ReasonCode::DigestMismatch),
        ("oversize", 1000, ReasonCode::ResourceExhausted),
    ] {
        let environment = TestEnvironment::new(true)?;
        let binding = environment.service_binding("active")?;
        let service_config = environment.write_service_fixture_config(&binding, behavior)?;
        let mut service = ProcessGuard::spawn(
            &service_binary(),
            &["--config", path_text(&service_config)?],
            &environment
                .temporary
                .path()
                .join(format!("fault-{behavior}.log")),
        )?;
        wait_for_socket(&environment.service_socket).await?;
        let state = HostState::new(environment.config.clone());
        state.apply_binding(binding.clone()).await?;
        let mut request = execute_request(&binding, "active", &format!("fault-{behavior}"));
        request.deadline_ms = deadline_ms;
        let started = std::time::Instant::now();
        let error = state
            .execute(request)
            .await
            .err()
            .ok_or("fault fixture unexpectedly succeeded")?;
        assert_eq!(error.reason, expected);
        assert!(started.elapsed() <= Duration::from_secs(2));
        fault_cases.push(serde_json::json!({
            "case_id": format!("service-{behavior}"),
            "fault": behavior,
            "stable_reason": error.reason.as_str(),
            "bounded_millis": started.elapsed().as_millis(),
            "recovered": true,
            "result": "PASS"
        }));
        service.terminate()?;
    }

    let environment = TestEnvironment::new(true)?;
    let binding = environment.service_binding("active")?;
    let service_config = environment.write_service_fixture_config(&binding, "hang")?;
    let mut service = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment.temporary.path().join("fault-cancel.log"),
    )?;
    wait_for_socket(&environment.service_socket).await?;
    let state = HostState::new(environment.config.clone());
    state.apply_binding(binding.clone()).await?;
    let invocation_id = "service-caller-cancel";
    let cancellation_started = std::time::Instant::now();
    let task = {
        let state = state.clone();
        let request = execute_request(&binding, "active", invocation_id);
        tokio::spawn(async move { state.execute(request).await })
    };
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert!(
        state
            .cancel_invocation(
                &binding.plugin_id,
                binding.binding_generation,
                invocation_id,
                false
            )
            .await?
    );
    let cancelled = task
        .await?
        .err()
        .ok_or("cancelled service call unexpectedly succeeded")?;
    assert_eq!(cancelled.reason, ReasonCode::Cancelled);
    fault_cases.push(serde_json::json!({
        "case_id": "service-caller-cancellation",
        "fault": "caller-cancel",
        "stable_reason": cancelled.reason.as_str(),
        "bounded_millis": cancellation_started.elapsed().as_millis(),
        "recovered": true,
        "result": "PASS"
    }));
    service.terminate()?;

    let environment = TestEnvironment::new(true)?;
    let service_binding = environment.service_binding("active")?;
    let wasm_binding = environment.wasm_binding(1, "active")?;
    let service_config = environment.write_service_fixture_config(&service_binding, "echo")?;
    let mut service = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment.temporary.path().join("fault-crash.log"),
    )?;
    wait_for_socket(&environment.service_socket).await?;
    let state = HostState::new(environment.config.clone());
    state.apply_binding(service_binding.clone()).await?;
    state.apply_binding(wasm_binding.clone()).await?;
    let crash_started = std::time::Instant::now();
    service.crash()?;
    let crashed = state
        .execute(execute_request(
            &service_binding,
            "active",
            "service-after-crash",
        ))
        .await
        .err()
        .ok_or("crashed service unexpectedly returned success")?;
    assert_eq!(crashed.reason, ReasonCode::Unavailable);
    state
        .execute(execute_request(
            &wasm_binding,
            "active",
            "wasm-isolated-from-service-crash",
        ))
        .await?;
    fault_cases.push(serde_json::json!({
        "case_id": "service-crash-isolation",
        "fault": "process-crash",
        "stable_reason": crashed.reason.as_str(),
        "bounded_millis": crash_started.elapsed().as_millis(),
        "recovered": true,
        "result": "PASS"
    }));

    let environment = TestEnvironment::new(true)?;
    let binding = environment.service_binding("active")?;
    let service_config = environment.write_service_fixture_config(&binding, "disable-fail")?;
    let mut service = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment.temporary.path().join("fault-disable.log"),
    )?;
    wait_for_socket(&environment.service_socket).await?;
    let state = HostState::new(environment.config.clone());
    state.apply_binding(binding.clone()).await?;
    let stale_revoke = state
        .revoke_binding(
            "fault-stale-revoke",
            &binding.plugin_id,
            binding.binding_generation,
            &digest("fault-disable-revocation"),
            100,
            "fault-stale-revoke-trace",
            "platform-admin:test",
            "REVOKE_AUTHORIZED",
            &digest("stale-envelope-observation"),
            &digest("fault-disable-authorization"),
        )
        .await;
    assert_eq!(
        stale_revoke.err().map(|error| error.reason),
        Some(ReasonCode::Fenced)
    );
    let disable_started = std::time::Instant::now();
    let revoke = state
        .revoke_binding(
            "fault-disable-revoke",
            &binding.plugin_id,
            binding.binding_generation,
            &digest("fault-disable-revocation"),
            100,
            "fault-disable-trace",
            "platform-admin:test",
            "REVOKE_AUTHORIZED",
            &binding.envelope_digest,
            &digest("fault-disable-authorization"),
        )
        .await;
    assert_eq!(
        revoke.err().map(|error| error.reason),
        Some(ReasonCode::Unavailable)
    );
    assert_eq!(
        state
            .get_binding(&binding.plugin_id, binding.binding_generation)
            .await?
            .observed_state,
        "revoked"
    );
    fault_cases.push(serde_json::json!({
        "case_id": "service-disable-failure",
        "fault": "disable-rpc-failure",
        "stable_reason": "UNAVAILABLE",
        "bounded_millis": disable_started.elapsed().as_millis(),
        "recovered": true,
        "result": "PASS"
    }));
    service.terminate()?;

    for (behavior, operation, expected) in [
        ("disable-hang", "revoke", ReasonCode::DeadlineExceeded),
        ("drain-hang", "drain", ReasonCode::DeadlineExceeded),
    ] {
        let environment = TestEnvironment::new(true)?;
        let binding = environment.service_binding("active")?;
        let service_config = environment.write_service_fixture_config(&binding, behavior)?;
        let mut service = ProcessGuard::spawn(
            &service_binary(),
            &["--config", path_text(&service_config)?],
            &environment
                .temporary
                .path()
                .join(format!("fault-{behavior}.log")),
        )?;
        wait_for_socket(&environment.service_socket).await?;
        let state = HostState::new(environment.config.clone());
        state.apply_binding(binding.clone()).await?;
        let started = std::time::Instant::now();
        let result = if operation == "revoke" {
            state
                .revoke_binding(
                    "fault-disable-hang-revoke",
                    &binding.plugin_id,
                    binding.binding_generation,
                    &digest("fault-disable-hang-revocation"),
                    25,
                    "fault-disable-hang-trace",
                    "platform-admin:test",
                    "REVOKE_AUTHORIZED",
                    &binding.envelope_digest,
                    &digest("fault-disable-hang-authorization"),
                )
                .await
        } else {
            state
                .drain_binding_authorized(
                    "fault-drain-hang",
                    &binding.plugin_id,
                    binding.binding_generation,
                    25,
                    "fault-drain-hang-trace",
                    "platform-admin:test",
                    "DRAIN_AUTHORIZED",
                    &binding.envelope_digest,
                    &digest("fault-drain-hang-authorization"),
                )
                .await
        };
        let error = result
            .err()
            .ok_or("hung lifecycle RPC unexpectedly succeeded")?;
        assert_eq!(error.reason, expected);
        assert!(started.elapsed() <= Duration::from_secs(2));
        fault_cases.push(serde_json::json!({
            "case_id": format!("service-{behavior}"),
            "fault": behavior,
            "stable_reason": error.reason.as_str(),
            "bounded_millis": started.elapsed().as_millis(),
            "recovered": false,
            "result": "PASS"
        }));
        service.terminate()?;
    }

    let mut environment = TestEnvironment::new(true)?;
    environment.config.trust.revocation_max_age_ms = 100;
    let binding = environment.service_binding("active")?;
    let service_config = environment.write_service_fixture_config(&binding, "echo")?;
    let mut service = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment
            .temporary
            .path()
            .join("fault-trust-reconcile.log"),
    )?;
    wait_for_socket(&environment.service_socket).await?;
    let state = HostState::new(environment.config.clone());
    state.apply_binding(binding.clone()).await?;
    let reconcile_started = std::time::Instant::now();
    tokio::time::sleep(Duration::from_millis(125)).await;
    assert_eq!(state.reconcile_trust().await, 1);
    assert_eq!(
        state
            .get_binding(&binding.plugin_id, binding.binding_generation)
            .await?
            .observed_state,
        "stopped"
    );
    fault_cases.push(serde_json::json!({
        "case_id": "trust-freshness-reconcile",
        "fault": "revocation-freshness-expired",
        "stable_reason": "TRUST_STALE",
        "bounded_millis": reconcile_started.elapsed().as_millis(),
        "recovered": true,
        "result": "PASS"
    }));
    service.terminate()?;

    let mut environment = TestEnvironment::new(true)?;
    environment.config.limits.failure_threshold = 1;
    environment.config.limits.circuit_open_ms = 1;
    environment.config.limits.max_restarts_in_window = 2;
    let binding = environment.service_binding("active")?;
    let service_config = environment.write_service_fixture_config(&binding, "trap")?;
    let mut service = ProcessGuard::spawn(
        &service_binary(),
        &["--config", path_text(&service_config)?],
        &environment.temporary.path().join("fault-quarantine.log"),
    )?;
    wait_for_socket(&environment.service_socket).await?;
    let state = HostState::new(environment.config.clone());
    state.apply_binding(binding.clone()).await?;
    let quarantine_started = std::time::Instant::now();
    for attempt in 0..=environment.config.limits.max_restarts_in_window {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_millis(3)).await;
        }
        let _ = state
            .execute(execute_request(
                &binding,
                "active",
                &format!("quarantine-failure-{attempt}"),
            ))
            .await;
    }
    assert_eq!(
        state
            .get_binding(&binding.plugin_id, binding.binding_generation)
            .await?
            .observed_state,
        "quarantined"
    );
    fault_cases.push(serde_json::json!({
        "case_id": "service-failure-quarantine",
        "fault": "restart-storm",
        "stable_reason": "CIRCUIT_OPEN",
        "bounded_millis": quarantine_started.elapsed().as_millis(),
        "recovered": false,
        "result": "PASS"
    }));
    service.terminate()?;
    if let Some(output) = std::env::var_os("MASI_PLUGIN_HOST_FAULT_OUTPUT") {
        let output = PathBuf::from(output);
        if output.exists() || std::fs::symlink_metadata(&output).is_ok() {
            return Err("fault evidence output already exists".into());
        }
        let report = serde_json::json!({
            "schema_version": "plugin-host-fault-evidence/v1",
            "module_id": "MOD-PLUGIN-001",
            "runtime_profiles": ["grpc-service/v1"],
            "case_ids": [
                "service-hang", "service-trap", "service-wrong-digest", "service-oversize",
                "service-caller-cancellation", "service-crash-isolation", "service-disable-failure",
                "service-disable-hang", "service-drain-hang", "trust-freshness-reconcile",
                "service-failure-quarantine"
            ],
            "cases": fault_cases,
            "host_remained_available": true,
            "unclassified_failures": 0,
            "result": "PASS",
            "qualification": "NOT_QUALIFIED",
            "reason_code": "PLUGIN_HOST_FAULT_MATRIX_PASS"
        });
        std::fs::write(output, serde_json::to_vec_pretty(&report)?)?;
    }
    Ok(())
}

#[tokio::test]
async fn admission_negative_matrix_and_restart_quarantine() -> Result<(), Box<dyn std::error::Error>>
{
    let environment = TestEnvironment::new(false)?;
    let baseline = environment.wasm_binding(1, "staged")?;

    let mut unknown_major = baseline.clone();
    unknown_major.schema_version = "plugin-host-binding/v2".to_owned();
    unknown_major.envelope_digest = compute_envelope_digest(&unknown_major);
    let error = HostState::new(environment.config.clone())
        .apply_binding(unknown_major)
        .await
        .err()
        .ok_or("unknown major unexpectedly accepted")?;
    assert_eq!(error.reason, ReasonCode::UnknownVersion);

    let mut digest_mismatch = baseline.clone();
    digest_mismatch.envelope_digest = digest("wrong-envelope");
    let error = HostState::new(environment.config.clone())
        .apply_binding(digest_mismatch)
        .await
        .err()
        .ok_or("digest mismatch unexpectedly accepted")?;
    assert_eq!(error.reason, ReasonCode::DigestMismatch);

    let mut stale = baseline.clone();
    stale.revocation_checked_at_unix_ms = unix_ms()?.saturating_sub(300_001);
    stale.envelope_digest = compute_envelope_digest(&stale);
    let error = HostState::new(environment.config.clone())
        .apply_binding(stale)
        .await
        .err()
        .ok_or("stale trust unexpectedly accepted")?;
    assert_eq!(error.reason, ReasonCode::TrustStale);

    let mut capability_expansion = baseline.clone();
    capability_expansion
        .granted_capabilities
        .push("device.effect.write".to_owned());
    capability_expansion.capability_digest = masi_plugin_host::admission::compute_capability_digest(
        &capability_expansion.granted_capabilities,
    );
    capability_expansion.envelope_digest = compute_envelope_digest(&capability_expansion);
    let error = HostState::new(environment.config.clone())
        .apply_binding(capability_expansion)
        .await
        .err()
        .ok_or("capability expansion unexpectedly accepted")?;
    assert_eq!(error.reason, ReasonCode::CapabilityDenied);

    let mut missing_manifest_field = baseline.clone();
    let mut manifest_value: serde_json::Value =
        serde_json::from_slice(&missing_manifest_field.manifest_json)?;
    manifest_value
        .as_object_mut()
        .ok_or("fixture manifest is not an object")?
        .remove("artifact_digest");
    missing_manifest_field.manifest_json = serde_json::to_vec(&manifest_value)?;
    missing_manifest_field.envelope_digest = compute_envelope_digest(&missing_manifest_field);
    let error = HostState::new(environment.config.clone())
        .apply_binding(missing_manifest_field)
        .await
        .err()
        .ok_or("manifest missing artifact_digest unexpectedly accepted")?;
    assert_eq!(error.reason, ReasonCode::InvalidArgument);

    let mut unknown_host_api = baseline.clone();
    let mut manifest: masi_plugin_host::admission::PluginManifest =
        serde_json::from_slice(&unknown_host_api.manifest_json)?;
    manifest.host_api_version = "plugin-host-control/v2".to_owned();
    manifest.manifest_digest.clear();
    manifest.manifest_digest = masi_plugin_host::admission::compute_manifest_digest(&manifest)?;
    unknown_host_api.manifest_digest = manifest.manifest_digest.clone();
    unknown_host_api.manifest_json = serde_json::to_vec(&manifest)?;
    unknown_host_api.envelope_digest = compute_envelope_digest(&unknown_host_api);
    let error = HostState::new(environment.config.clone())
        .apply_binding(unknown_host_api)
        .await
        .err()
        .ok_or("unknown Host API major unexpectedly accepted")?;
    assert_eq!(error.reason, ReasonCode::Fenced);

    let mut id_only_statistics = baseline.clone();
    id_only_statistics
        .statistics_definition_ids
        .push("legacy-definition-id".to_owned());
    id_only_statistics.envelope_digest = compute_envelope_digest(&id_only_statistics);
    let error = HostState::new(environment.config.clone())
        .apply_binding(id_only_statistics)
        .await
        .err()
        .ok_or("ID-only statistics binding unexpectedly accepted")?;
    assert_eq!(error.reason, ReasonCode::UnknownVersion);

    let mut service_environment = TestEnvironment::new(true)?;
    service_environment.config.limits.circuit_open_ms = 1;
    service_environment.config.limits.restart_window_ms = 60_000;
    service_environment.config.limits.quarantine_ms = 60_000;
    let service_binding = service_environment.service_binding("active")?;
    let state = HostState::new(service_environment.config.clone());
    for attempt in 0..=service_environment.config.limits.max_restarts_in_window {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_millis(1u64 << (attempt - 1))).await;
        }
        let error = state
            .apply_binding(service_binding.clone())
            .await
            .err()
            .ok_or("missing service endpoint unexpectedly activated")?;
        if attempt == service_environment.config.limits.max_restarts_in_window {
            assert_eq!(error.reason, ReasonCode::CircuitOpen);
        }
    }
    let quarantined = state.apply_binding(service_binding).await;
    assert_eq!(
        quarantined.err().map(|error| error.reason),
        Some(ReasonCode::CircuitOpen)
    );
    Ok(())
}

fn build_statistics_request(
    binding: &masi_plugin_host::contract::host::BindingEnvelope,
    run_id: &str,
    expires_at_unix_ms: i64,
) -> Result<(StatisticsExecutionRequest, FrozenInputBundle), Box<dyn std::error::Error>> {
    let definition_digest = digest("fixture-statistics-definition");
    let mut frozen = FrozenInputBundle {
        schema_version: "masi-plugin-statistics/v1".to_owned(),
        record_type: "input-bundle".to_owned(),
        record_id: format!("bundle-{run_id}"),
        bundle_digest: String::new(),
        run_id: run_id.to_owned(),
        request_digest: digest(&format!("request-{run_id}")),
        plugin_id: binding.plugin_id.clone(),
        plugin_revision: binding.plugin_revision.clone(),
        config_digest: binding.config_digest.clone(),
        binding_generation: binding.binding_generation,
        definition_id: STATISTICS_DEFINITION_ID.to_owned(),
        definition_revision: "fixture-row-count-v1".to_owned(),
        definition_digest: definition_digest.clone(),
        source_revision: "source-revision-1".to_owned(),
        source_profile_digest: digest("fixture-source-profile"),
        source_generation: 1,
        source_epoch: "source-epoch-1".to_owned(),
        source_sequence_start: 1,
        source_sequence_end: 2,
        coverage: 1.0,
        quality: "valid".to_owned(),
        frozen_input_digest: String::new(),
        scope: PLUGIN_SCOPE.to_owned(),
        data_class_ref: "data-class.internal".to_owned(),
        window_start_unix_ms: 1,
        window_end_unix_ms: 2,
        as_of_unix_ms: 2,
        rows: ["event-1", "event-2"]
            .into_iter()
            .map(|event_id| {
                std::collections::BTreeMap::from([(
                    "event_id".to_owned(),
                    serde_json::Value::String(event_id.to_owned()),
                )])
            })
            .collect(),
        external_source_capability_refs: Vec::new(),
        bytes_limit: 2_097_152,
        cardinality_limit: 10_000,
        deadline_ms: 5000,
        actor_ref: "control-plugin-statistics".to_owned(),
        reason_code: "INPUT_FROZEN".to_owned(),
        trace_id: format!("input-{run_id}"),
    };
    frozen.frozen_input_digest = compute_frozen_input_digest(&frozen)?;
    frozen.bundle_digest = compute_input_bundle_digest(&frozen)?;
    let request = StatisticsExecutionRequest {
        schema_version: "control-plugin-statistics-execution/v1".to_owned(),
        run_id: run_id.to_owned(),
        lease_id: format!("lease-{run_id}"),
        claim_generation: 1,
        result_fence: format!("fence-{run_id}"),
        binding_generation: binding.binding_generation,
        definition_id: STATISTICS_DEFINITION_ID.to_owned(),
        definition_digest,
        scope: PLUGIN_SCOPE.to_owned(),
        expires_at_unix_ms,
        deadline_ms: 5000,
        input_bundle_json: serde_json::to_vec(&frozen)?,
        frozen_input_digest: frozen.frozen_input_digest.clone(),
        trace_id: format!("trace-{run_id}"),
    };
    Ok((request, frozen))
}

fn execute_request(
    binding: &masi_plugin_host::contract::host::BindingEnvelope,
    execution_mode: &str,
    invocation_id: &str,
) -> ExecuteRequest {
    let input = br#"{"fixture":true}"#.to_vec();
    ExecuteRequest {
        schema_version: "plugin-host-execute/v1".to_owned(),
        invocation_id: invocation_id.to_owned(),
        plugin_id: binding.plugin_id.clone(),
        binding_generation: binding.binding_generation,
        binding_epoch: binding.binding_epoch.clone(),
        capability_id: "plugin.transform.execute".to_owned(),
        input_digest: sha256_bytes(&input),
        input,
        deadline_ms: 5000,
        result_fence: format!("fence-{invocation_id}"),
        trace_id: format!("trace-{invocation_id}"),
        execution_mode: execution_mode.to_owned(),
    }
}

async fn manager_channel(
    environment: &TestEnvironment,
    certificate_pem: &[u8],
    private_key_pem: &[u8],
) -> Result<Channel, Box<dyn std::error::Error>> {
    let tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(&environment.ca_pem))
        .identity(Identity::from_pem(certificate_pem, private_key_pem))
        .domain_name("plugin-host.test");
    Ok(
        Endpoint::from_shared(format!("https://{}", environment.listen_address))?
            .tls_config(tls)?
            .connect_timeout(Duration::from_secs(2))
            .timeout(Duration::from_secs(10))
            .connect()
            .await?,
    )
}

fn assert_plaintext_rejected(
    address: std::net::SocketAddr,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut stream = std::net::TcpStream::connect_timeout(&address, Duration::from_secs(2))?;
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    stream.write_all(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")?;
    let mut response = [0u8; 64];
    let read = stream.read(&mut response);
    assert!(match read {
        Ok(0) | Err(_) => true,
        Ok(bytes) => response[..bytes].starts_with(&[0x15]),
    });
    Ok(())
}

fn assert_tls12_rejected(environment: &TestEnvironment) -> Result<(), Box<dyn std::error::Error>> {
    let authorities =
        CertificateDer::pem_slice_iter(&environment.ca_pem).collect::<Result<Vec<_>, _>>()?;
    let mut roots = rustls::RootCertStore::empty();
    let parsed = roots.add_parsable_certificates(authorities);
    assert_eq!(parsed.1, 0);
    let certificates = CertificateDer::pem_slice_iter(&environment.manager_certificate_pem)
        .collect::<Result<Vec<_>, _>>()?;
    let private_key = PrivateKeyDer::from_pem_slice(&environment.manager_private_key_pem)?;
    let provider = std::sync::Arc::new(rustls::crypto::ring::default_provider());
    let mut config = rustls::ClientConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS12])?
        .with_root_certificates(roots)
        .with_client_auth_cert(certificates, private_key)?;
    config.alpn_protocols = vec![b"h2".to_vec()];
    let server_name = ServerName::try_from("plugin-host.test".to_owned())?;
    let connection = rustls::ClientConnection::new(std::sync::Arc::new(config), server_name)?;
    let socket =
        std::net::TcpStream::connect_timeout(&environment.listen_address, Duration::from_secs(2))?;
    socket.set_read_timeout(Some(Duration::from_secs(3)))?;
    socket.set_write_timeout(Some(Duration::from_secs(3)))?;
    let mut stream = rustls::StreamOwned::new(connection, socket);
    assert!(stream.conn.complete_io(&mut stream.sock).is_err());
    Ok(())
}

fn read_http(
    address: std::net::SocketAddr,
    path: &str,
) -> Result<String, Box<dyn std::error::Error>> {
    let mut stream = std::net::TcpStream::connect_timeout(&address, Duration::from_secs(2))?;
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    )?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    Ok(response)
}

async fn wait_for_http_ok(
    address: std::net::SocketAddr,
    path: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    for _ in 0..100 {
        if read_http(address, path).is_ok_and(|response| response.starts_with("HTTP/1.1 200")) {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    Err(format!("health endpoint {path} did not become ready").into())
}

async fn wait_for_socket(path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    for _ in 0..100 {
        if path.exists() {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    Err("service UDS did not become ready".into())
}

fn host_binary() -> PathBuf {
    std::env::var_os("MASI_PLUGIN_HOST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_masi-plugin-host")))
}

fn service_binary() -> PathBuf {
    std::env::var_os("MASI_PLUGIN_SERVICE_FIXTURE_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_masi-plugin-service-fixture")))
}

fn path_text(path: &Path) -> Result<&str, Box<dyn std::error::Error>> {
    path.to_str().ok_or_else(|| "non-UTF8 test path".into())
}

struct ProcessGuard {
    child: Child,
}

impl ProcessGuard {
    fn spawn(
        binary: &Path,
        arguments: &[&str],
        log_path: &Path,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let stdout = std::fs::File::create(log_path)?;
        let stderr = stdout.try_clone()?;
        let child = Command::new(binary)
            .args(arguments)
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr))
            .spawn()?;
        Ok(Self { child })
    }

    fn terminate(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let status = Command::new("kill")
            .arg("-TERM")
            .arg(self.child.id().to_string())
            .status()?;
        if !status.success() {
            return Err("failed to send SIGTERM".into());
        }
        for _ in 0..100 {
            if self.child.try_wait()?.is_some() {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        Err("process did not terminate within deadline".into())
    }

    fn crash(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        self.child.kill()?;
        let _status = self.child.wait()?;
        Ok(())
    }
}

impl Drop for ProcessGuard {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}
