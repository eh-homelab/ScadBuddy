use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use axum::{
    Router,
    body::Bytes,
    extract::{Path as UrlPath, State},
    http::StatusCode,
    routing::{get, put},
};
use chrono::{Duration, Utc};
use sb_slicer::activity::{SLICER_ERROR, SlicerActivities};
use sb_slicer::models::{Arrange, SlicePlateInput, SliceProgress, TransferUrl};
use sb_temporal::Stages;
use sb_transfer::Transfer;
use sha2::{Digest, Sha256};
use temporalio_sdk::activities::ActivityError;
use temporalio_sdk::testing::{ActivityEnvironment, ActivityEnvironmentError};

#[derive(Default)]
struct Seen {
    gets: Vec<String>,
    puts: BTreeMap<String, Vec<u8>>,
}

type Shared = Arc<Mutex<Seen>>;

async fn blob(State(s): State<Shared>, UrlPath(name): UrlPath<String>) -> String {
    s.lock().unwrap().gets.push(name.clone());
    format!("contents of {name}")
}

async fn sink(State(s): State<Shared>, UrlPath(name): UrlPath<String>, body: Bytes) -> StatusCode {
    s.lock().unwrap().puts.insert(name, body.to_vec());
    StatusCode::OK
}

async fn serve() -> (String, Shared) {
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

fn url(base: &str, path: &str) -> TransferUrl {
    TransferUrl {
        url: format!("{base}{path}"),
        headers: BTreeMap::new(),
        expires_at: Utc::now() + Duration::hours(1),
    }
}

fn input(base: &str) -> SlicePlateInput {
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

fn fake(mode: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests");
    match mode {
        "" => dir.join("fake-bambu-studio.sh"),
        mode => dir.join("fake").join(format!("{mode}.sh")),
    }
}

fn activities(mode: &str) -> SlicerActivities {
    SlicerActivities {
        cli: fake(mode),
        transfer: Transfer::new(),
        stages: Stages::register(&prometheus::Registry::new()),
    }
}

fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn application(err: ActivityEnvironmentError) -> (Option<String>, bool) {
    match err {
        ActivityEnvironmentError::Activity(ActivityError::Application(f)) => {
            (f.type_name().map(str::to_owned), f.is_non_retryable())
        }
        other => panic!("expected an application failure, got {other:?}"),
    }
}

#[tokio::test]
async fn a_slice_puts_its_output_and_heartbeats_progress() {
    let (base, seen) = serve().await;
    let beats: Arc<Mutex<Vec<SliceProgress>>> = Arc::default();
    let env = ActivityEnvironment::builder()
        .register_activities(activities(""))
        .on_heartbeat({
            let beats = beats.clone();
            move |details| {
                let p = details.downcast::<SliceProgress>().expect("SliceProgress");
                beats.lock().unwrap().push(*p);
            }
        })
        .build();
    let out = env
        .run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap();

    let seen = seen.lock().unwrap();
    let body = &seen.puts["sliced"];
    assert_eq!(out.sliced_sha256, sha(body));
    assert_eq!(out.sliced_size, body.len() as u64);
    assert_eq!(body.as_slice(), b"PK fake 3mf");
    assert_eq!(out.exit_code, 0);
    assert_eq!(out.warnings, vec!["thin wall".to_owned()]);
    assert!(out.stderr_tail.contains("fake stderr line"));
    assert_eq!(out.slicedata_sha256, None);
    assert_eq!(seen.gets, vec!["input", "machine", "process", "f1", "f2"]);
    assert_eq!(beats.lock().unwrap().last().unwrap().total_percent, 100);
}

#[tokio::test]
async fn slicedata_is_put_as_a_zip_when_asked() {
    let (base, seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities(""))
        .build();
    let mut req = input(&base);
    req.slicedata = Some(url(&base, "/sink/slicedata"));
    let out = env.run(SlicerActivities::slice_plate, req).await.unwrap();

    let seen = seen.lock().unwrap();
    let zip = &seen.puts["slicedata"];
    assert_eq!(out.slicedata_sha256.as_deref(), Some(sha(zip).as_str()));
    assert_eq!(out.slicedata_size, Some(zip.len() as u64));
    assert!(zip.starts_with(b"PK"));
}

#[tokio::test]
async fn a_validation_failure_is_not_retried() {
    let (base, seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities("exit-192"))
        .build();
    let err = env
        .run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap_err();
    assert_eq!(application(err), (Some(SLICER_ERROR.to_owned()), true));
    assert!(seen.lock().unwrap().puts.is_empty());
}

#[tokio::test]
async fn any_other_failure_is_retried() {
    let (base, _seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities("exit-1"))
        .build();
    let err = env
        .run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap_err();
    assert_eq!(application(err), (Some(SLICER_ERROR.to_owned()), false));
}

#[tokio::test]
async fn an_expired_input_is_refused_without_a_request() {
    let (base, seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities(""))
        .build();
    let mut req = input(&base);
    req.input_3mf.expires_at = Utc::now() - Duration::seconds(1);
    let err = env
        .run(SlicerActivities::slice_plate, req)
        .await
        .unwrap_err();
    assert_eq!(application(err), (Some("TransferRefused".to_owned()), true));
    assert!(seen.lock().unwrap().gets.is_empty());
}

#[tokio::test]
async fn a_cancelled_activity_is_cancelled() {
    let (base, _seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities("hang"))
        .build();
    let env = Arc::new(env);
    let canceller = env.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        canceller.cancel();
    });
    let err = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        env.run(SlicerActivities::slice_plate, input(&base)),
    )
    .await
    .expect("returns within 5 s")
    .unwrap_err();
    assert!(matches!(
        err,
        ActivityEnvironmentError::Activity(ActivityError::Cancelled { .. })
    ));
}
