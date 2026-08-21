//! MASI Plugin Runtime Host process entrypoint.

use std::path::PathBuf;

use clap::{Parser, ValueEnum};
use masi_plugin_host::{HostConfig, HostError, HostState};

#[derive(Debug, Parser)]
#[command(
    name = "masi-plugin-host",
    about = "Deny-by-default MASI Plugin Runtime Host"
)]
struct Args {
    /// Strict versioned config file.
    #[arg(long)]
    config: PathBuf,
    /// Execute one semantic in-container health probe and exit.
    #[arg(long, value_enum)]
    probe: Option<Probe>,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum Probe {
    Startup,
    Ready,
    Live,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .with_current_span(false)
        .with_span_list(false)
        .init();
    let args = Args::parse();
    let config = HostConfig::load(&args.config)?;
    if let Some(probe) = args.probe {
        return run_probe(&config, probe).await;
    }
    let state = HostState::new(config);
    let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);
    let health_state = state.clone();
    let health_shutdown = shutdown_rx.clone();
    let mut health = tokio::spawn(async move {
        masi_plugin_host::health::serve(health_state, health_shutdown).await
    });
    let control_state = state.clone();
    let reconcile_state = state.clone();
    let mut reconcile_shutdown = shutdown_rx.clone();
    let mut control =
        tokio::spawn(
            async move { masi_plugin_host::server::serve(control_state, shutdown_rx).await },
        );
    let mut reconcile = tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(30));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                changed = reconcile_shutdown.changed() => {
                    if changed.is_err() || *reconcile_shutdown.borrow() {
                        return Ok::<(), HostError>(());
                    }
                }
                _ = interval.tick() => {
                    let fenced = reconcile_state.reconcile_trust().await;
                    if fenced > 0 {
                        tracing::warn!(reason_code = "TRUST_FRESHNESS_FENCED", fenced_bindings = fenced);
                    }
                }
            }
        }
    });

    let mut fatal_error = None;
    let completed = tokio::select! {
        signal = wait_for_shutdown_signal() => {
            if let Err(error) = signal {
                tracing::error!(reason_code = "SIGNAL_HANDLER_FAILED", error = %error);
            }
            CompletedTask::Signal
        }
        result = &mut control => {
            fatal_error = Some(match result {
                Ok(Ok(())) => HostError::new(masi_plugin_host::ReasonCode::Unavailable, "control server stopped unexpectedly"),
                Ok(Err(error)) => error,
                Err(error) => HostError::new(masi_plugin_host::ReasonCode::Internal, format!("control task join: {error}")),
            });
            CompletedTask::Control
        }
        result = &mut health => {
            fatal_error = Some(match result {
                Ok(Ok(())) => HostError::new(masi_plugin_host::ReasonCode::Unavailable, "health server stopped unexpectedly"),
                Ok(Err(error)) => error,
                Err(error) => HostError::new(masi_plugin_host::ReasonCode::Internal, format!("health task join: {error}")),
            });
            CompletedTask::Health
        }
        result = &mut reconcile => {
            fatal_error = Some(match result {
                Ok(Ok(())) => HostError::new(masi_plugin_host::ReasonCode::Unavailable, "trust reconciler stopped unexpectedly"),
                Ok(Err(error)) => error,
                Err(error) => HostError::new(masi_plugin_host::ReasonCode::Internal, format!("trust reconciler join: {error}")),
            });
            CompletedTask::Reconcile
        }
    };
    state.shutdown().await;
    let _ = shutdown_tx.send(true);
    let join_remaining = async {
        match completed {
            CompletedTask::Signal => {
                let _ = (&mut control).await;
                let _ = (&mut health).await;
                let _ = (&mut reconcile).await;
            }
            CompletedTask::Control => {
                let _ = (&mut health).await;
                let _ = (&mut reconcile).await;
            }
            CompletedTask::Health => {
                let _ = (&mut control).await;
                let _ = (&mut reconcile).await;
            }
            CompletedTask::Reconcile => {
                let _ = (&mut control).await;
                let _ = (&mut health).await;
            }
        }
    };
    if tokio::time::timeout(
        std::time::Duration::from_millis(state.config().limits.shutdown_deadline_ms),
        join_remaining,
    )
    .await
    .is_err()
        && fatal_error.is_none()
    {
        fatal_error = Some(HostError::new(
            masi_plugin_host::ReasonCode::DeadlineExceeded,
            "Host server shutdown deadline exceeded",
        ));
    }
    if let Some(error) = fatal_error {
        return Err(error.into());
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum CompletedTask {
    Signal,
    Control,
    Health,
    Reconcile,
}

async fn run_probe(config: &HostConfig, probe: Probe) -> anyhow::Result<()> {
    use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};

    let mut address = config.health_socket()?;
    if address.ip().is_unspecified() {
        address.set_ip(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST));
    }
    let path = match probe {
        Probe::Startup => "/startupz",
        Probe::Ready => "/readyz",
        Probe::Live => "/livez",
    };
    let operation = async {
        let mut stream = tokio::net::TcpStream::connect(address).await?;
        stream
            .write_all(
                format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    .as_bytes(),
            )
            .await?;
        let mut response = Vec::with_capacity(1024);
        stream.take(4096).read_to_end(&mut response).await?;
        if !response.starts_with(b"HTTP/1.1 200") {
            return Err(std::io::Error::other("health probe returned non-200"));
        }
        Ok::<(), std::io::Error>(())
    };
    tokio::time::timeout(std::time::Duration::from_secs(2), operation)
        .await
        .map_err(|_| anyhow::anyhow!("health probe timed out"))??;
    Ok(())
}

async fn wait_for_shutdown_signal() -> std::io::Result<()> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => result,
            _ = terminate.recv() => Ok(()),
        }
    }
    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c().await
    }
}
