//! Strict mTLS liveness/readiness probe for the Edge public boundary.

use std::{
    io::Write as _,
    path::PathBuf,
    process::ExitCode,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context as _, bail};
use clap::Parser as _;
use masi_edge::{
    config::{read_pem, validate_server_name},
    contract::edge::{GetStatusRequest, edge_control_client::EdgeControlClient},
};
use serde_json::{Value, json};
use tonic::{
    Request,
    transport::{Certificate, ClientTlsConfig, Endpoint, Identity},
};

#[derive(Debug, clap::Parser)]
#[command(
    name = "masi-edge-probe",
    version,
    about = "mTLS EdgeControl GetStatus probe"
)]
struct Arguments {
    /// Exact HTTPS EdgeControl endpoint.
    #[arg(long)]
    endpoint: String,
    /// Exact expected DNS SAN.
    #[arg(long)]
    server_name: String,
    /// Trust-anchor PEM.
    #[arg(long)]
    ca: PathBuf,
    /// Probe client certificate PEM.
    #[arg(long)]
    certificate: PathBuf,
    /// Probe client private-key PEM.
    #[arg(long)]
    private_key: PathBuf,
    /// Optional target whose actor must be present.
    #[arg(long, default_value = "")]
    target_id: String,
    /// Expected immutable Edge configuration digest.
    #[arg(long)]
    expected_config_digest: Option<String>,
    /// Expected Edge process identity.
    #[arg(long)]
    expected_edge_instance_id: Option<String>,
    /// Entire connect and RPC deadline.
    #[arg(long, default_value_t = 2_000)]
    deadline_ms: u64,
}

#[tokio::main]
async fn main() -> ExitCode {
    let arguments = Arguments::parse();
    let trace_id = format!("edge-probe-{}", uuid::Uuid::new_v4());
    let observed_at_unix_ms = unix_ms();
    match run_probe(&arguments, &trace_id, observed_at_unix_ms).await {
        Ok(evidence) => {
            emit(&evidence);
            ExitCode::SUCCESS
        }
        Err(error) => {
            emit(&json!({
                "schema_version": "edge-status-probe-evidence/v1",
                "observed_at_unix_ms": observed_at_unix_ms,
                "trace_id": trace_id,
                "rpc_method": "masi.edge.v1.EdgeControl/GetStatus",
                "target_id": arguments.target_id,
                "tls_server_name": arguments.server_name,
                "deadline_ms": arguments.deadline_ms,
                "result": "FAIL",
                "failure": error.to_string()
            }));
            ExitCode::from(1)
        }
    }
}

async fn run_probe(
    arguments: &Arguments,
    trace_id: &str,
    observed_at_unix_ms: i64,
) -> anyhow::Result<Value> {
    if arguments.deadline_ms == 0 || arguments.deadline_ms > 10_000 {
        bail!("deadline_ms must be in [1,10000]");
    }
    validate_server_name(&arguments.server_name).context("invalid TLS server name")?;
    if !arguments.endpoint.starts_with("https://") {
        bail!("probe endpoint must use https");
    }
    let tls = ClientTlsConfig::new()
        .domain_name(arguments.server_name.clone())
        .ca_certificate(Certificate::from_pem(read_pem(&arguments.ca, false)?))
        .identity(Identity::from_pem(
            read_pem(&arguments.certificate, false)?,
            read_pem(&arguments.private_key, true)?,
        ));
    let deadline = Duration::from_millis(arguments.deadline_ms);
    let endpoint = Endpoint::from_shared(arguments.endpoint.clone())
        .context("invalid Edge endpoint")?
        .connect_timeout(deadline)
        .timeout(deadline)
        .tls_config(tls)
        .context("invalid mTLS configuration")?;
    let channel = endpoint
        .connect()
        .await
        .context("Edge mTLS connect failed")?;
    let mut client = EdgeControlClient::new(channel);
    let mut request = Request::new(GetStatusRequest {
        target_id: arguments.target_id.clone(),
        trace_id: trace_id.into(),
    });
    request.set_timeout(deadline);
    let status = client
        .get_status(request)
        .await
        .context("GetStatus failed")?
        .into_inner();
    if status.schema_version != "edge-status/v1" {
        bail!("unexpected Edge status schema");
    }
    if !status.process_live {
        bail!("Edge reports process_live=false");
    }
    if status.trace_id != trace_id {
        bail!("Edge status trace identity mismatch");
    }
    if let Some(expected) = &arguments.expected_config_digest
        && &status.config_digest != expected
    {
        bail!("Edge configuration digest mismatch");
    }
    if let Some(expected) = &arguments.expected_edge_instance_id
        && &status.edge_instance_id != expected
    {
        bail!("Edge process identity mismatch");
    }
    if !arguments.target_id.is_empty() && status.targets.len() != 1 {
        bail!("requested target actor is absent or ambiguous");
    }
    let target = status.targets.first();
    Ok(json!({
        "schema_version": "edge-status-probe-evidence/v1",
        "observed_at_unix_ms": observed_at_unix_ms,
        "trace_id": trace_id,
        "rpc_method": "masi.edge.v1.EdgeControl/GetStatus",
        "target_id": arguments.target_id,
        "tls_server_name": arguments.server_name,
        "deadline_ms": arguments.deadline_ms,
        "result": "PASS",
        "edge_status_schema_version": status.schema_version,
        "edge_instance_id": status.edge_instance_id,
        "build_version": status.build_version,
        "config_digest": status.config_digest,
        "process_live": status.process_live,
        "accepting_assignments": status.accepting_assignments,
        "target_limit": status.target_limit,
        "observed_target_count": status.targets.len(),
        "target_actor_state": target.map(|value| value.actor_state),
        "target_reason_code": target.map(|value| value.reason_code.clone()),
        "semantics": if arguments.target_id.is_empty() {
            "process-liveness-and-public-boundary-readiness"
        } else {
            "target-actor-presence-readiness"
        }
    }))
}

fn emit(value: &Value) {
    let stdout = std::io::stdout();
    let mut lock = stdout.lock();
    let _ignored = serde_json::to_writer(&mut lock, value);
    let _ignored = writeln!(lock);
}

fn unix_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|duration| i64::try_from(duration.as_millis()).ok())
        .unwrap_or_default()
}
