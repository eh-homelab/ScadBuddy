//! What the activity's tests and the worker's (`bins/scadbuddy-slicer/tests/worker.rs`,
//! which includes this file by path) share: a transfer server, the inputs that point at
//! it, the fake CLI, and the check that a killed CLI left no process behind.
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration as StdDuration, Instant};

use axum::{
    Router,
    body::Bytes,
    extract::{Path as UrlPath, State},
    http::StatusCode,
    routing::{get, put},
};
use chrono::{Duration, Utc};
use sb_slicer::activity::SlicerActivities;
use sb_slicer::models::{Arrange, SlicePlateInput, TransferUrl};
use sb_temporal::Stages;
use sb_transfer::Transfer;

#[derive(Default)]
pub struct Seen {
    pub gets: Vec<String>,
    pub puts: BTreeMap<String, Vec<u8>>,
    /// How long `/stall/...` and `/stall-sink/...` wait before they answer.
    pub stall: StdDuration,
}

pub type Shared = Arc<Mutex<Seen>>;

async fn blob(State(s): State<Shared>, UrlPath(name): UrlPath<String>) -> String {
    s.lock().unwrap().gets.push(name.clone());
    format!("contents of {name}")
}

async fn sink(State(s): State<Shared>, UrlPath(name): UrlPath<String>, body: Bytes) -> StatusCode {
    s.lock().unwrap().puts.insert(name, body.to_vec());
    StatusCode::OK
}

async fn stalled_blob(state: State<Shared>, name: UrlPath<String>) -> String {
    let stall = state.lock().unwrap().stall;
    tokio::time::sleep(stall).await;
    blob(state, name).await
}

async fn stalled_sink(state: State<Shared>, name: UrlPath<String>, body: Bytes) -> StatusCode {
    let stall = state.lock().unwrap().stall;
    tokio::time::sleep(stall).await;
    sink(state, name, body).await
}

/// `GET /blob/{name}` answers `contents of {name}`; `PUT /sink/{name}` keeps the body;
/// `/stall/{name}` and `/stall-sink/{name}` do the same after `Seen::stall`.
pub async fn serve() -> (String, Shared) {
    let state: Shared = Arc::default();
    let app = Router::new()
        .route("/blob/{name}", get(blob))
        .route("/sink/{name}", put(sink))
        .route("/stall/{name}", get(stalled_blob))
        .route("/stall-sink/{name}", put(stalled_sink))
        .with_state(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (base, state)
}

pub fn url(base: &str, path: &str) -> TransferUrl {
    TransferUrl {
        url: format!("{base}{path}"),
        headers: BTreeMap::new(),
        expires_at: Utc::now() + Duration::hours(1),
    }
}

pub fn input(base: &str) -> SlicePlateInput {
    SlicePlateInput {
        input_3mf: url(base, "/blob/input"),
        plate: 1,
        machine_preset: url(base, "/blob/machine"),
        process_preset: url(base, "/blob/process"),
        filament_presets: vec![url(base, "/blob/f1"), url(base, "/blob/f2")],
        sliced_3mf: url(base, "/sink/sliced"),
        slicedata: None,
        arrange: Arrange::No,
        allow_newer_file: true,
        debug: 1,
    }
}

/// The fake CLI (`""`) or one of its `fake/<mode>.sh` wrappers. Both crates that include
/// this file sit two levels below the workspace, so the path holds for either.
pub fn fake(mode: &str) -> PathBuf {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../crates/sb-slicer/tests");
    match mode {
        "" => dir.join("fake-bambu-studio.sh"),
        mode => dir.join("fake").join(format!("{mode}.sh")),
    }
}

pub fn activities(mode: &str) -> SlicerActivities {
    SlicerActivities {
        cli: fake(mode),
        transfer: Transfer::new(),
        stages: Stages::register(&prometheus::Registry::new()),
        keepalive: sb_slicer::runner::KEEPALIVE,
    }
}

/// Waits until no process is left in `pgid`; the killed `sleep` is reaped by init a
/// moment after the kill.
pub async fn assert_group_gone(pgid: i32) {
    let deadline = Instant::now() + StdDuration::from_secs(2);
    loop {
        let out = std::process::Command::new("pgrep")
            .args(["-g", &pgid.to_string()])
            .output()
            .unwrap();
        if out.stdout.is_empty() {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "group {pgid} still has {}",
            String::from_utf8_lossy(&out.stdout)
        );
        tokio::time::sleep(StdDuration::from_millis(20)).await;
    }
}
