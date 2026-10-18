use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::Router;
use axum::extract::State;
use axum::http::{StatusCode, header};
use axum::response::IntoResponse;
use axum::routing::get;
use prometheus::{Encoder, TextEncoder};

#[derive(Clone)]
struct Health {
    ready: Arc<AtomicBool>,
    registry: prometheus::Registry,
}

/// `GET /healthz` (`ok` once `ready`, else 503) and `GET /metrics` (the registry, in
/// Prometheus's text format) on `0.0.0.0:port`. A port that cannot be bound is logged
/// and ends the task.
pub fn serve_health(
    port: u16,
    ready: Arc<AtomicBool>,
    registry: prometheus::Registry,
) -> tokio::task::JoinHandle<()> {
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics))
        .with_state(Health { ready, registry });
    tokio::spawn(async move {
        let listener = match tokio::net::TcpListener::bind(("0.0.0.0", port)).await {
            Ok(l) => l,
            Err(e) => {
                tracing::error!(port, error = %e, "could not serve /healthz and /metrics");
                return;
            }
        };
        if let Err(e) = axum::serve(listener, app).await {
            tracing::error!(error = %e, "/healthz and /metrics stopped");
        }
    })
}

async fn healthz(State(h): State<Health>) -> impl IntoResponse {
    if h.ready.load(Ordering::SeqCst) {
        (StatusCode::OK, "ok")
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "starting")
    }
}

async fn metrics(State(h): State<Health>) -> impl IntoResponse {
    let encoder = TextEncoder::new();
    let mut body = Vec::new();
    match encoder.encode(&h.registry.gather(), &mut body) {
        Ok(()) => (
            StatusCode::OK,
            [(header::CONTENT_TYPE, encoder.format_type().to_owned())],
            body,
        ),
        Err(e) => {
            tracing::error!(error = %e, "could not encode /metrics");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                [(header::CONTENT_TYPE, "text/plain".to_owned())],
                Vec::new(),
            )
        }
    }
}
