//! GET a `TransferUrl` to a file and PUT a file to one, with sha256 (spec 2026-10-16 §5).
//! The worker holds no store credentials: whatever a URL needs (a presigned query, or the
//! internal route's `X-ScadBuddy-Transfer`) is in its `headers`, so this crate knows neither.

mod sha;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::{DateTime, SecondsFormat, Utc};
use futures_util::StreamExt;
use reqwest::header::{CONTENT_LENGTH, HeaderMap, HeaderName, HeaderValue};
use reqwest::{Body, StatusCode};
use serde::{Deserialize, Serialize, Serializer};
use tokio::io::AsyncWriteExt;
use tokio_util::io::ReaderStream;

use sha::Hasher;

const CHUNK: usize = 1024 * 1024;

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransferUrl {
    pub url: String,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    /// Dumped as pydantic dumps a UTC datetime: `2026-10-16T12:00:00Z`.
    #[serde(serialize_with = "utc_rfc3339")]
    pub expires_at: DateTime<Utc>,
}

/// Never prints the path, the query (a presigned signature) or a header value (the
/// internal route's token).
impl std::fmt::Debug for TransferUrl {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let origin = match reqwest::Url::parse(&self.url) {
            Ok(u) => format!("{}://{}", u.scheme(), u.host_str().unwrap_or("?")),
            Err(_) => "<redacted>".to_owned(),
        };
        let headers: BTreeMap<&str, &str> = self
            .headers
            .keys()
            .map(|k| (k.as_str(), "<redacted>"))
            .collect();
        f.debug_struct("TransferUrl")
            .field("origin", &origin)
            .field("headers", &headers)
            .field("expires_at", &self.expires_at)
            .finish()
    }
}

fn utc_rfc3339<S: Serializer>(value: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&value.to_rfc3339_opts(SecondsFormat::AutoSi, true))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Transferred {
    pub sha256: String,
    pub size: u64,
}

#[derive(Debug)]
pub enum TransferError {
    Expired,
    Missing,
    Refused(u16),
    /// Any other non-2xx (a 3xx is not followed): 5xx and 429 are worth a retry.
    Status(u16),
    Digest {
        expected: String,
        actual: String,
    },
    Io(std::io::Error),
    Http(String),
}

impl std::fmt::Display for TransferError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Expired => write!(f, "transfer url expired"),
            Self::Missing => write!(f, "object not found (404)"),
            Self::Refused(code) => write!(f, "transfer refused ({code})"),
            Self::Status(code) => write!(f, "unexpected status {code}"),
            Self::Digest { expected, actual } => {
                write!(f, "sha256 mismatch: expected {expected}, got {actual}")
            }
            Self::Io(e) => write!(f, "io: {e}"),
            Self::Http(e) => write!(f, "http: {e}"),
        }
    }
}

impl std::error::Error for TransferError {}

impl From<std::io::Error> for TransferError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}

impl From<reqwest::Error> for TransferError {
    fn from(e: reqwest::Error) -> Self {
        // Without the URL: a presigned one carries its signature.
        Self::Http(e.without_url().to_string())
    }
}

pub struct Transfer {
    client: reqwest::Client,
}

impl Default for Transfer {
    fn default() -> Self {
        Self::new()
    }
}

impl Transfer {
    /// rustls (webpki and native roots), no proxy from the environment, no redirects (they
    /// would carry the headers to another origin), a 30 s connect timeout, a 60 s idle read
    /// timeout and no overall timeout (bodies are large).
    pub fn new() -> Self {
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .read_timeout(Duration::from_secs(60))
            .connect_timeout(Duration::from_secs(30))
            .build()
            .expect("reqwest client builds");
        Self { client }
    }

    /// Streams the GET body to `dest` (through `dest.part`, renamed on success), sending
    /// `url.headers`. 404 is `Missing`, 401/403 `Refused`.
    pub async fn get(&self, url: &TransferUrl, dest: &Path) -> Result<Transferred, TransferError> {
        check_live(url)?;
        let resp = self
            .client
            .get(&url.url)
            .headers(header_map(url)?)
            .send()
            .await?;
        check_status(resp.status())?;

        let guard = PartGuard(Some(part_path(dest)));
        let done = write_body(resp, guard.path()).await?;
        tokio::fs::rename(guard.path(), dest).await?;
        guard.disarm();
        Ok(done)
    }

    /// Streams `src` as the PUT body with its `Content-Length`, sending `url.headers`.
    pub async fn put(&self, url: &TransferUrl, src: &Path) -> Result<Transferred, TransferError> {
        check_live(url)?;
        let file = tokio::fs::File::open(src).await?;
        let length = file.metadata().await?.len();
        let hasher = Arc::new(Mutex::new(Hasher::default()));
        let tee = hasher.clone();
        let stream = ReaderStream::with_capacity(file, CHUNK).map(move |chunk| {
            if let Ok(bytes) = &chunk {
                tee.lock().expect("hasher lock").update(bytes);
            }
            chunk
        });
        let resp = self
            .client
            .put(&url.url)
            .headers(header_map(url)?)
            .header(CONTENT_LENGTH, length)
            .body(Body::wrap_stream(stream))
            .send()
            .await?;
        check_status(resp.status())?;
        let hasher = std::mem::take(&mut *hasher.lock().expect("hasher lock"));
        let (sha256, size) = hasher.finish();
        Ok(Transferred { sha256, size })
    }
}

/// Removes the `.part` file unless disarmed: on an error, a failed rename, or the future
/// being dropped (an activity's cancellation).
struct PartGuard(Option<PathBuf>);

impl PartGuard {
    fn path(&self) -> &Path {
        self.0.as_deref().expect("armed")
    }

    fn disarm(mut self) {
        self.0 = None;
    }
}

impl Drop for PartGuard {
    fn drop(&mut self) {
        if let Some(path) = &self.0 {
            let _ = std::fs::remove_file(path);
        }
    }
}

async fn write_body(resp: reqwest::Response, part: &Path) -> Result<Transferred, TransferError> {
    let mut file = tokio::fs::File::create(part).await?;
    let mut hasher = Hasher::default();
    let mut stream = resp.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        hasher.update(&chunk);
        file.write_all(&chunk).await?;
    }
    file.flush().await?;
    let (sha256, size) = hasher.finish();
    Ok(Transferred { sha256, size })
}

fn part_path(dest: &Path) -> PathBuf {
    let mut name = dest.file_name().unwrap_or_default().to_owned();
    name.push(".part");
    dest.with_file_name(name)
}

fn check_live(url: &TransferUrl) -> Result<(), TransferError> {
    if url.expires_at <= Utc::now() {
        return Err(TransferError::Expired);
    }
    Ok(())
}

fn check_status(status: StatusCode) -> Result<(), TransferError> {
    match status {
        s if s.is_success() => Ok(()),
        StatusCode::NOT_FOUND => Err(TransferError::Missing),
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => {
            Err(TransferError::Refused(status.as_u16()))
        }
        s => Err(TransferError::Status(s.as_u16())),
    }
}

fn header_map(url: &TransferUrl) -> Result<HeaderMap, TransferError> {
    let mut map = HeaderMap::new();
    for (k, v) in &url.headers {
        let name = HeaderName::from_bytes(k.as_bytes())
            .map_err(|_| TransferError::Http(format!("invalid header name {k:?}")))?;
        let value = HeaderValue::from_str(v)
            .map_err(|_| TransferError::Http(format!("invalid value for header {k:?}")))?;
        map.insert(name, value);
    }
    Ok(map)
}
