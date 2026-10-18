//! What the activity's tests and the worker's (`bins/scadbuddy-slicer/tests/worker.rs`,
//! which includes this file by path) share: a transfer server, the inputs that point at
//! it, and the fake CLI.
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

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

/// `GET /blob/{name}` answers `contents of {name}`; `PUT /sink/{name}` keeps the body.
pub async fn serve() -> (String, Shared) {
    let state: Shared = Arc::default();
    let app = Router::new()
        .route("/blob/{name}", get(blob))
        .route("/sink/{name}", put(sink))
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
    }
}
