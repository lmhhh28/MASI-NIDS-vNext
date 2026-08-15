//! MASI Edge Agent process entrypoint.

use std::{fmt::Display, io::Write as _, path::PathBuf, process::ExitCode};

use clap::Parser as _;
use tracing_subscriber::{EnvFilter, layer::SubscriberExt as _, util::SubscriberInitExt as _};

use masi_edge::config::EdgeConfig;

#[derive(Debug, clap::Parser)]
#[command(name = "masi-edge", version, about = "MASI-NIDS-vNext Rust Edge Agent")]
struct Arguments {
    /// Strict edge-config/v1 JSON file.
    #[arg(long)]
    config: PathBuf,
}

#[tokio::main]
async fn main() -> ExitCode {
    let arguments = Arguments::parse();
    let (config, config_digest) = match EdgeConfig::load(&arguments.config) {
        Ok(value) => value,
        Err(error) => {
            startup_error(format_args!("edge startup rejected: {error}"));
            return ExitCode::from(78);
        }
    };
    let filter = match EnvFilter::try_new(&config.log_filter) {
        Ok(filter) => filter,
        Err(error) => {
            startup_error(format_args!("edge log filter rejected: {error}"));
            return ExitCode::from(78);
        }
    };
    if tracing_subscriber::registry()
        .with(filter)
        .with(tracing_subscriber::fmt::layer().json())
        .try_init()
        .is_err()
    {
        startup_error("edge logging initialization failed");
        return ExitCode::from(70);
    }
    tracing::info!(
        edge_instance_id = %config.edge_instance_id,
        config_digest = %config_digest,
        "starting Edge public boundary"
    );
    match masi_edge::server::run(config, config_digest).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            tracing::error!(
                reason_code = error.reason_code(),
                detail = %error,
                "Edge process stopped"
            );
            ExitCode::from(1)
        }
    }
}

fn startup_error(message: impl Display) {
    let stderr = std::io::stderr();
    let mut lock = stderr.lock();
    let _ignored = writeln!(lock, "{message}");
}
