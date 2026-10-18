use std::sync::{Arc, Mutex};

use axum::{
    Router,
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
    routing::{get, put},
};
use chrono::{Duration, Utc};
use sb_transfer::{Transfer, TransferError, TransferUrl};
use sha2::{Digest, Sha256};

#[derive(Default)]
struct Seen {
    requests: usize,
    body: Vec<u8>,
    length: Option<String>,
    header: Option<String>,
}

type Shared = Arc<Mutex<Seen>>;

async fn blob(State(s): State<Shared>) -> &'static str {
    s.lock().unwrap().requests += 1;
    "hello"
}

async fn gone(State(s): State<Shared>) -> StatusCode {
    s.lock().unwrap().requests += 1;
    StatusCode::NOT_FOUND
}

async fn locked(State(s): State<Shared>, h: HeaderMap) -> (StatusCode, &'static str) {
    s.lock().unwrap().requests += 1;
    if h.get("x-scadbuddy-transfer").map(|v| v.as_bytes()) == Some(b"tok") {
        (StatusCode::OK, "hello")
    } else {
        (StatusCode::FORBIDDEN, "")
    }
}

async fn sink(State(s): State<Shared>, h: HeaderMap, body: Bytes) -> StatusCode {
    let mut seen = s.lock().unwrap();
    seen.requests += 1;
    seen.length = h
        .get("content-length")
        .map(|v| v.to_str().unwrap().to_owned());
    seen.header = h
        .get("x-scadbuddy-transfer")
        .map(|v| v.to_str().unwrap().to_owned());
    seen.body = body.to_vec();
    StatusCode::OK
}

async fn serve() -> (String, Shared) {
    let state: Shared = Arc::default();
    let app = Router::new()
        .route("/blob", get(blob))
        .route("/boom", get(|| async { StatusCode::INTERNAL_SERVER_ERROR }))
        .route("/full", put(|| async { StatusCode::INSUFFICIENT_STORAGE }))
        .route(
            "/hop",
            get(|| async { (StatusCode::FOUND, [("location", "/blob")]) }),
        )
        .route("/gone", get(gone))
        .route("/locked", get(locked))
        .route(
            "/sink",
            put(sink).layer(axum::extract::DefaultBodyLimit::disable()),
        )
        .with_state(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (base, state)
}

fn url(base: &str, path: &str, headers: &[(&str, &str)]) -> TransferUrl {
    TransferUrl {
        url: format!("{base}{path}"),
        headers: headers
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect(),
        expires_at: Utc::now() + Duration::minutes(5),
    }
}

#[tokio::test]
async fn get_streams_and_digests() {
    let (base, _state) = serve().await;
    let dir = tempfile::tempdir().unwrap();
    let got = Transfer::new()
        .get(&url(&base, "/blob", &[]), &dir.path().join("in"))
        .await
        .unwrap();
    assert_eq!(
        got.sha256,
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    );
    assert_eq!(got.size, 5);
    assert_eq!(std::fs::read(dir.path().join("in")).unwrap(), b"hello");
    assert!(!dir.path().join("in.part").exists());
}

#[tokio::test]
async fn get_404_is_missing_and_403_is_refused() {
    let (base, _state) = serve().await;
    let dir = tempfile::tempdir().unwrap();
    let dest = dir.path().join("out");
    let t = Transfer::new();
    assert!(matches!(
        t.get(&url(&base, "/gone", &[]), &dest).await,
        Err(TransferError::Missing)
    ));
    assert!(matches!(
        t.get(&url(&base, "/locked", &[]), &dest).await,
        Err(TransferError::Refused(403))
    ));
    assert!(!dest.exists());
    let ok = t
        .get(
            &url(&base, "/locked", &[("X-ScadBuddy-Transfer", "tok")]),
            &dest,
        )
        .await
        .unwrap();
    assert_eq!(ok.size, 5);
}

#[tokio::test]
async fn expired_url_is_refused_before_any_request() {
    let (base, state) = serve().await;
    let dir = tempfile::tempdir().unwrap();
    let mut u = url(&base, "/blob", &[]);
    u.expires_at = Utc::now() - Duration::seconds(1);
    let t = Transfer::new();
    assert!(matches!(
        t.get(&u, &dir.path().join("x")).await,
        Err(TransferError::Expired)
    ));
    std::fs::write(dir.path().join("src"), b"x").unwrap();
    assert!(matches!(
        t.put(&u, &dir.path().join("src")).await,
        Err(TransferError::Expired)
    ));
    assert_eq!(state.lock().unwrap().requests, 0);
}

#[tokio::test]
async fn an_invalid_header_is_refused_before_any_request_without_its_value() {
    let (base, state) = serve().await;
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("src"), b"x").unwrap();
    let t = Transfer::new();
    for (name, value) in [("bad name", "tok"), ("x-scadbuddy-transfer", "s3cret\n")] {
        let u = url(&base, "/blob", &[(name, value)]);
        for result in [
            t.get(&u, &dir.path().join("x")).await,
            t.put(&u, &dir.path().join("src")).await,
        ] {
            let Err(e @ TransferError::InvalidHeader(_)) = result else {
                panic!("expected InvalidHeader, got {result:?}");
            };
            assert!(!e.to_string().contains("s3cret"), "{e}");
        }
    }
    assert_eq!(state.lock().unwrap().requests, 0);
}

#[tokio::test]
async fn put_streams_the_file_and_reports_its_digest() {
    let (base, state) = serve().await;
    let dir = tempfile::tempdir().unwrap();
    let data: Vec<u8> = (0u32..3 * 1024 * 1024)
        .map(|i| (i.wrapping_mul(2654435761) >> 13) as u8)
        .collect();
    let src = dir.path().join("src");
    std::fs::write(&src, &data).unwrap();
    let got = Transfer::new()
        .put(
            &url(&base, "/sink", &[("X-ScadBuddy-Transfer", "tok")]),
            &src,
        )
        .await
        .unwrap();
    assert_eq!(got.size, data.len() as u64);
    assert_eq!(got.sha256, format!("{:x}", Sha256::digest(&data)));
    let seen = state.lock().unwrap();
    assert!(seen.body == data);
    assert_eq!(
        seen.length.as_deref(),
        Some(data.len().to_string().as_str())
    );
    assert_eq!(seen.header.as_deref(), Some("tok"));
}

#[tokio::test]
async fn other_statuses_carry_their_code_and_redirects_are_not_followed() {
    let (base, _state) = serve().await;
    let dir = tempfile::tempdir().unwrap();
    let t = Transfer::new();
    assert!(matches!(
        t.get(&url(&base, "/boom", &[]), &dir.path().join("a"))
            .await,
        Err(TransferError::Status(500))
    ));
    std::fs::write(dir.path().join("src"), b"x").unwrap();
    assert!(matches!(
        t.put(&url(&base, "/full", &[]), &dir.path().join("src"))
            .await,
        Err(TransferError::Status(507))
    ));
    assert!(matches!(
        t.get(&url(&base, "/hop", &[]), &dir.path().join("b")).await,
        Err(TransferError::Status(302))
    ));
}

#[tokio::test]
async fn a_body_cut_short_leaves_no_part_and_no_dest() {
    use tokio::io::AsyncWriteExt;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move {
        let (mut sock, _) = listener.accept().await.unwrap();
        let mut buf = [0u8; 4096];
        let _ = tokio::io::AsyncReadExt::read(&mut sock, &mut buf).await;
        sock.write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n0123456789")
            .await
            .unwrap();
    });
    let dir = tempfile::tempdir().unwrap();
    let dest = dir.path().join("out");
    let got = Transfer::new().get(&url(&base, "/x", &[]), &dest).await;
    assert!(got.is_err());
    assert!(!dest.exists());
    assert!(!dir.path().join("out.part").exists());
}

#[test]
fn debug_never_prints_the_signature_or_a_header_value() {
    let u = url(
        "https://store.example:9000",
        "/bucket/key?X-Amz-Signature=sekret",
        &[("X-ScadBuddy-Transfer", "tok-value")],
    );
    let shown = format!("{u:?}");
    assert!(shown.contains("store.example"));
    assert!(shown.contains("X-ScadBuddy-Transfer"));
    for leaked in ["sekret", "X-Amz-Signature", "bucket/key", "tok-value"] {
        assert!(!shown.contains(leaked), "{leaked} leaked: {shown}");
    }
}
