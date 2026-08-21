//! Startup/readiness/liveness and low-cardinality metrics HTTP surface.

use std::sync::Arc;

use axum::extract::State;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;
use tokio::sync::watch;

use crate::error::{HostError, HostResult, ReasonCode};
use crate::state::HostState;

/// Serve health and metrics until shutdown is requested.
pub async fn serve(state: Arc<HostState>, mut shutdown: watch::Receiver<bool>) -> HostResult<()> {
    let address = state.config().health_socket()?;
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .map_err(|error| {
            HostError::new(ReasonCode::Unavailable, format!("health listener: {error}"))
        })?;
    let router = Router::new()
        .route("/startupz", get(startup))
        .route("/readyz", get(ready))
        .route("/livez", get(live))
        .route("/metrics", get(metrics))
        .with_state(state);
    axum::serve(listener, router)
        .with_graceful_shutdown(async move {
            while !*shutdown.borrow() {
                if shutdown.changed().await.is_err() {
                    break;
                }
            }
        })
        .await
        .map_err(|error| HostError::new(ReasonCode::Unavailable, format!("health server: {error}")))
}

async fn startup(State(state): State<Arc<HostState>>) -> impl IntoResponse {
    Json(json!({
        "schema_version": "plugin-host-health/v1",
        "status_namespace": "startup",
        "status": "started",
        "host_id": state.config().host_id,
        "profile_id": state.config().profile_id
    }))
}

async fn ready(State(state): State<Arc<HostState>>) -> Response {
    let ready = state.ready();
    let status = if ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (
        status,
        Json(json!({
            "schema_version": "plugin-host-health/v1",
            "status_namespace": "readiness",
            "status": if ready { "ready" } else { "draining" },
            "deny_all_without_binding": true
        })),
    )
        .into_response()
}

async fn live(State(_state): State<Arc<HostState>>) -> impl IntoResponse {
    Json(json!({
        "schema_version": "plugin-host-health/v1",
        "status_namespace": "liveness",
        "status": "live"
    }))
}

async fn metrics(State(state): State<Arc<HostState>>) -> Response {
    let count = state.binding_count().await;
    let (queued, in_flight) = state.runtime_gauges().await;
    let body = state
        .metrics
        .render(count, queued, in_flight, state.ready());
    let mut response = body.into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; version=0.0.4; charset=utf-8"),
    );
    response
}
