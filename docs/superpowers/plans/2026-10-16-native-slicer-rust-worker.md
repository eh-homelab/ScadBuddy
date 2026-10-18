# Native Slicer on a Rust Temporal Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A slice is a ScadBuddy activity: a Rust worker runs the Bambu Studio CLI with ScadBuddy's own preset files, reports progress as heartbeats, and the print dialog and `PrintRunWorkflow` use it when `slicer_backend` is `native`, with Bambuddy's slicer still selectable.

**Architecture:** A new Rust workspace `workers/rust/` holds the first Rust worker, `scadbuddy-slicer`, on task queue `slicer`. It serves one activity, `slice_plate`, whose input carries transfer URLs the Python caller mints (presigned S3 URLs, or the API's internal blob routes), never bytes, store keys or credentials. The Python side resolves presets into files (`backend/scadbuddy/slicing/`), mints the URLs and records the result in the store (`slicing/transfer.py`), and wraps the activity in a one-activity `SliceWorkflow` until Temporal 1.31's standalone activities. Payload shapes are pinned across the two languages by a JSON vector file, as the agent and agent-durable pin theirs.

**Tech Stack:** Rust stable (pinned by `rust-toolchain.toml`), `temporalio-sdk` / `temporalio-client` / `temporalio-common` / `temporalio-macros` 1.1.0, `tokio` 1, `serde` / `serde_json`, `reqwest` 0.12 (streaming bodies, rustls), `sha2`, `nix` 0.31 (`mkfifo`), `zip`, `tracing` + `tracing-opentelemetry` 0.34 + `opentelemetry-otlp` 0.33, `prometheus` for `/metrics`, `axum` 0.8 for `/healthz` and `/metrics` and as the test servers; Python 3.12 backend (FastAPI, `temporalio` ≥1.33, pydantic 2); Bambu Studio `v02.08.02.61` Ubuntu 24.04 AppImage; React 19 frontend.

**Spec:** `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §5, §7.1, §7.2, §7.4 (§2 for the measured CLI facts; §3 for the tiers' independence).

## Global Constraints

- Rust Temporal crates pinned exactly at `1.1.0`, all one version, like the agent's `@temporalio/*` (spec §7.1).
- Bambu Studio pinned by version `v02.08.02.61` and the AppImage's sha256 in the Dockerfile, bumped deliberately, as `OPENSCAD_VERSION` is. The newer slicer reads a 3MF stamped `BambuStudio-02.07.00.00` (`render/bambu3mf.py` `BAMBU_APPLICATION`), so `BAMBU_APPLICATION` does not move. My call on the spec's open decision (§9): a newer slicer reading an older file is the common case, and the reverse is what `--allow-newer-file` exists for.
- The Rust worker's whole configuration is its environment (spec §5.2): `SCADBUDDY_TEMPORAL_ADDRESS`, `SCADBUDDY_TEMPORAL_NAMESPACE`, `SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER` (default `slicer`), `SCADBUDDY_SLICER_CONCURRENCY` (default `max(1, cpus / 2)`) and the standard `OTEL_*`. It holds no Postgres connection, no store credentials, no Bambuddy client, no settings view and no database role.
- A `TransferUrl` in a `slice_plate` payload may carry a presigned signature or a signed token, so it does enter the history. Each expires two hours after minting, and the `slicer` queue's payloads are not sealed (only flows' are, `payload_codec.py`). A history is as sensitive as a two-hour URL, which is what Temporal's own data-converter guidance accepts for short-lived links; the design records it in §5.2.
- Every setting a person changes lives in Postgres (`core/settings.py`, `APPLIES`), seeded by `SCADBUDDY_<FIELD>`; `slicer_backend` applies `live`.
- Schema changes are new files `backend/scadbuddy/migrations/<yyyymmdd>T<hhmm>Z_<slug>.sql`; never edit a merged one.
- No trace attribute, log line or metric label carries a parameter value, a file name a person typed, a transfer URL or the CLI's stderr (`core/trace_scrub.py`'s rule, applied in Rust by hand).
- No image or 3MF as base64 in JSON or text (`lint-base64-images.sh`): bytes move through URLs.
- `scadbuddy_render_stage_seconds` is the one histogram the dashboards read for stages; the Rust activity emits it with `stage="slice"` and the same buckets as `core/metrics.py` `RENDER_BUCKETS`.
- `workflow.patched("native-slicer")` guards every change to `PrintRunWorkflow`'s history shape; existing print-run history fixtures must still replay.
- No task queue is renamed. The new queue is `slicer`; `library`, `bambuddy`, `projects` and the render queue keep their names.
- Commits: conventional titles; never amend; `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` as the last line.

## Review Focus

1. **A progress line split across two fifo reads.** The CLI writes in 512-byte chunks (spec §2), so one JSON line can arrive in two reads and two lines in one. The reader must buffer to `\n` and parse each complete line; a half line parsed as JSON must not fail the slice. Test in Task 6.
2. **The CLI exits before it ever opens the pipe.** A validation failure (exit 192) can happen before the first progress write. A blocking `open` of the fifo for reading would hang until the activity's start-to-close timeout. The reader opens the fifo `O_RDWR | O_NONBLOCK` (so there is always a writer) and ends when the child exits. Test in Task 6.
3. **A cancelled slice leaves a process behind.** The CLI forks worker threads; killing only the parent pid can leave them. The activity spawns the child in its own process group and sends `SIGKILL` to the group on cancellation and on its own error path; the temp dir goes with it. Test in Task 6.
4. **A preset whose `inherits` chain loops or names a missing parent.** Bambu Studio's profiles are hand-maintained; flattening must refuse a loop and a missing parent with the preset's name in the error, not recurse forever. Test in Task 8.
5. **A transfer token for one blob opening another, or outliving its two hours.** The internal blob routes take a per-call token bound to the key, the method and the expiry; a token minted for a GET of key A must not PUT, must not read key B, and must be refused after `expires_at`. Test in Task 10.

---

### Task 1: Payload models and the cross-language vector file

**Files:**
- Create: `backend/scadbuddy/slicing/__init__.py` (empty)
- Create: `backend/scadbuddy/slicing/models.py`
- Modify: `backend/scadbuddy/store/content_models.py:12` (`BlobKind`), `:35` (`SWEPT_KINDS`)
- Create: `workers/rust/vectors/slice_plate.json` (written by the test)
- Test: `backend/tests/slicing/test_models.py`, `backend/tests/slicing/__init__.py`

**Interfaces:**
- Produces:
  ```python
  BlobKind = Literal["piece", "snapshot", "asset", "font", "slice"]   # content_models.py
  SWEPT_KINDS = ("piece", "snapshot", "slice")

  class TransferUrl(BaseModel):              # slicing/models.py (spec §5.2)
      url: str
      headers: dict[str, str] = Field(default_factory=dict)
      expires_at: datetime                   # aware UTC; dumped as RFC 3339 with "Z"? No: pydantic dumps "+00:00"; Rust parses either (chrono)

  class SlicePlateInput(BaseModel):
      input_3mf: TransferUrl                 # GET
      plate: int = Field(ge=0)               # 0 = every plate
      machine_preset: TransferUrl            # GET
      process_preset: TransferUrl            # GET
      filament_presets: list[TransferUrl]    # GET, slot order
      sliced_3mf: TransferUrl                # PUT
      slicedata: TransferUrl | None = None   # PUT, when wanted
      arrange: Literal["no", "yes", "auto"] = "no"
      allow_newer_file: bool = True
      debug: int = Field(default=1, ge=0, le=5)

  class SliceProgress(BaseModel):            # one heartbeat's details
      plate_index: int
      plate_count: int
      plate_percent: int
      total_percent: int
      message: str = ""

  class SlicePlateOutput(BaseModel):
      sliced_sha256: str
      sliced_size: int
      slicedata_sha256: str | None = None
      slicedata_size: int | None = None
      warnings: list[str] = Field(default_factory=list)
      exit_code: int
      stderr_tail: str = ""
      elapsed_seconds: float

  SLICER_ERROR = "SlicerError"               # the ApplicationError type
  VALIDATION_EXIT = 192
  ```
- The vector file is `{"input": [...], "progress": [...], "output": [...]}`, each entry a `model_dump(mode="json")`, written only with `UPDATE_SLICE_VECTORS=1`, else compared after a round trip. `expires_at` in the vectors is a fixed `datetime(2026, 10, 16, 12, 0, tzinfo=UTC)`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/slicing/test_models.py
import json, os
from datetime import UTC, datetime
from pathlib import Path
from scadbuddy.slicing.models import SlicePlateInput, SlicePlateOutput, SliceProgress, TransferUrl
from scadbuddy.store.content_models import SWEPT_KINDS

VECTORS = Path(__file__).parents[3] / "workers/rust/vectors/slice_plate.json"
AT = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

def _get(name): return TransferUrl(url=f"http://api/api/v1/internal/blobs/slice/{name}", headers={"X-ScadBuddy-Transfer": "tok"}, expires_at=AT)
def _put(name): return TransferUrl(url=f"http://garage/scadbuddy/prod/slice/{name}?X-Amz-Signature=sig", expires_at=AT)

def test_slice_is_a_swept_kind():
    assert "slice" in SWEPT_KINDS

def test_vectors_round_trip():
    cases = {
        "input": [SlicePlateInput(input_3mf=_get("in"), plate=1, machine_preset=_get("m"), process_preset=_get("p"),
                                  filament_presets=[_get("f1"), _get("f2")], sliced_3mf=_put("out")),
                  SlicePlateInput(input_3mf=_get("in"), plate=0, machine_preset=_get("m"), process_preset=_get("p"),
                                  filament_presets=[_get("f1")], sliced_3mf=_put("out"), slicedata=_put("data"),
                                  arrange="auto", allow_newer_file=False, debug=3)],
        "progress": [SliceProgress(plate_index=0, plate_count=2, plate_percent=50, total_percent=25, message="Slicing")],
        "output": [SlicePlateOutput(sliced_sha256="a" * 64, sliced_size=1234, warnings=["w"], exit_code=0, elapsed_seconds=12.5),
                   SlicePlateOutput(sliced_sha256="b" * 64, sliced_size=1, slicedata_sha256="c" * 64, slicedata_size=9,
                                    exit_code=0, stderr_tail="tail", elapsed_seconds=0.5)],
    }
    dumped = {k: [m.model_dump(mode="json") for m in v] for k, v in cases.items()}
    if os.environ.get("UPDATE_SLICE_VECTORS"):
        VECTORS.write_text(json.dumps(dumped, indent=2, sort_keys=True) + "\n")
    assert json.loads(VECTORS.read_text()) == dumped
    for cls, key in ((SlicePlateInput, "input"), (SliceProgress, "progress"), (SlicePlateOutput, "output")):
        for raw in dumped[key]:
            assert cls.model_validate(raw).model_dump(mode="json") == raw
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/slicing/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: scadbuddy.slicing`

- [ ] **Step 3: Add the models and the kind**

Add `"slice"` to `BlobKind` and `SWEPT_KINDS`; write `slicing/models.py` with the classes above. `TransferUrl` gets a `field_validator("expires_at")` refusing a naive datetime.

- [ ] **Step 4: Write the vectors, then run the tests**

Run: `cd backend && UPDATE_SLICE_VECTORS=1 uv run --frozen pytest tests/slicing/test_models.py -v && uv run --frozen pytest tests/slicing/test_models.py -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS both times; the file exists with three keys.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/slicing backend/scadbuddy/store/content_models.py backend/tests/slicing workers/rust/vectors/slice_plate.json
git commit -m "feat(slicing): slice_plate payload models, the slice blob kind and their vectors"
```

---

### Task 2: Rust workspace, toolchain and CI job

**Files:**
- Create: `workers/rust/Cargo.toml`, `workers/rust/rust-toolchain.toml`, `workers/rust/.gitignore` (`target/`), `workers/rust/rustfmt.toml` (`max_width = 100`)
- Create: `workers/rust/crates/sb-slicer/Cargo.toml`, `src/lib.rs`, `src/models.rs`
- Create: `workers/rust/crates/sb-slicer/tests/vectors.rs`
- Modify: `.github/workflows/ci.yml` (a `rust` job; `summary`'s `needs`)
- Modify: `CLAUDE.md` "Commands" (a Rust block) and "Layout" (a `workers/rust/` bullet)

**Interfaces:**
- Produces (`sb_slicer::models`), `serde` structs mirroring Task 1 field for field, no `deny_unknown_fields` (Python may add fields), `#[serde(default)]` on every field Python defaults, no `skip_serializing_if` (Python dumps every field):
  ```rust
  pub struct TransferUrl { pub url: String, pub headers: BTreeMap<String, String>, pub expires_at: chrono::DateTime<chrono::Utc> }
  pub struct SlicePlateInput { pub input_3mf: TransferUrl, pub plate: u32, pub machine_preset: TransferUrl, pub process_preset: TransferUrl,
      pub filament_presets: Vec<TransferUrl>, pub sliced_3mf: TransferUrl, pub slicedata: Option<TransferUrl>,
      pub arrange: Arrange, pub allow_newer_file: bool, pub debug: u8 }
  pub enum Arrange { No, Yes, Auto }                       // serde rename_all = "lowercase"
  pub struct SliceProgress { pub plate_index: u32, pub plate_count: u32, pub plate_percent: u32, pub total_percent: u32, pub message: String }
  pub struct SlicePlateOutput { pub sliced_sha256: String, pub sliced_size: u64, pub slicedata_sha256: Option<String>, pub slicedata_size: Option<u64>,
      pub warnings: Vec<String>, pub exit_code: i32, pub stderr_tail: String, pub elapsed_seconds: f64 }
  ```
  `expires_at` round-trips through `chrono`'s RFC 3339 with `SecondsFormat::AutoSi` and `+00:00` so the vector's string is reproduced exactly (`serialize_with` a small helper; test pins it).
- Workspace layout (spec §7.1), with the members this plan builds marked: `crates/sb-transfer` (Task 3), `crates/sb-temporal` (Task 4), `crates/sb-slicer` (Tasks 2, 6), `bins/scadbuddy-slicer` (Tasks 5, 7). `crates/sb-render` and `bins/scadbuddy-render-tools` are the render-tools plan's (spec §7.3); they are named in the workspace README only, not created here. `sb-temporal` is a dependency of `scadbuddy-slicer` alone: `scadbuddy-render-tools` is a CLI the Python render worker runs, not a Temporal worker.
- `[workspace.dependencies]` pins: `temporalio-sdk = { version = "=1.1.0", features = ["testing"] }`, `temporalio-client = "=1.1.0"`, `temporalio-common = "=1.1.0"`, `temporalio-macros = "=1.1.0"`, `tokio = { version = "1", features = ["full"] }`, `serde = { version = "1", features = ["derive"] }`, `serde_json = "1"`, `chrono = { version = "0.4", features = ["serde"] }`, `anyhow = "1"`, `thiserror = "2"`, `tracing = "0.1"`.
- `rust-toolchain.toml`: `[toolchain] channel = "<current stable>"`, `components = ["rustfmt", "clippy"]`; the implementer runs `rustup check` and pins the stable of the day.

- [ ] **Step 1: Write the failing vector test**

```rust
// workers/rust/crates/sb-slicer/tests/vectors.rs
use sb_slicer::models::{SlicePlateInput, SlicePlateOutput, SliceProgress};
use serde_json::Value;

const VECTORS: &str = include_str!("../../../vectors/slice_plate.json");

fn round_trip<T: serde::de::DeserializeOwned + serde::Serialize>(raw: &Value) {
    let parsed: T = serde_json::from_value(raw.clone()).expect("parses");
    assert_eq!(serde_json::to_value(&parsed).unwrap(), *raw, "a field was lost, renamed or reformatted");
}

#[test]
fn every_vector_round_trips() {
    let doc: Value = serde_json::from_str(VECTORS).unwrap();
    for raw in doc["input"].as_array().unwrap() { round_trip::<SlicePlateInput>(raw); }
    for raw in doc["progress"].as_array().unwrap() { round_trip::<SliceProgress>(raw); }
    for raw in doc["output"].as_array().unwrap() { round_trip::<SlicePlateOutput>(raw); }
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd workers/rust && cargo test -p sb-slicer`
Expected: FAIL to compile (`models` missing)

- [ ] **Step 3: Write the workspace files, `models.rs` and `workers/rust/README.md`** (the layout above, the commands below, the `protoc` note).

- [ ] **Step 4: Run the tests, fmt and clippy**

Run: `cd workers/rust && cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`
Expected: PASS

- [ ] **Step 5: Add the CI job**

In `ci.yml`, a job `rust` on `ubuntu-latest`: `dtolnay/rust-toolchain` with the toolchain read from `workers/rust/rust-toolchain.toml`, `Swatinem/rust-cache@v2` with `workspaces: workers/rust`, `apt-get install -y protobuf-compiler` (the Core SDK's build needs `protoc`, per the SDK README), then the three commands of Step 4. It runs on every PR (no path filter: `summary` needs it, and a skipped required job fails the gate). Add `rust` to `summary`'s `needs` and to its result check the way `go` appears there.

- [ ] **Step 6: Document and commit**

`CLAUDE.md` Commands: a "Rust workers (`workers/rust/`)" block with the Step 4 commands and the `protoc` note. Layout: `workers/rust/` — Rust Temporal workers and tools (spec 2026-10-16 §7): `crates/sb-slicer` the `slice_plate` activity, `crates/sb-transfer` GET/PUT of `TransferUrl`s with sha256, `crates/sb-temporal` the worker bootstrap (slicer only), `bins/scadbuddy-slicer` the `slicer` queue's worker; `vectors/` pins payloads against the pydantic models (`UPDATE_SLICE_VECTORS=1` in `backend/tests/slicing/test_models.py`).

```bash
git add workers/rust .github/workflows/ci.yml CLAUDE.md
git commit -m "feat(rust): workers/rust workspace with the slice_plate models pinned to the Python vectors"
```

---

### Task 3: `sb-transfer`: GET a URL to a file, PUT a file to a URL, with sha256

**Files:**
- Create: `workers/rust/crates/sb-transfer/Cargo.toml`, `src/lib.rs`, `src/sha.rs`
- Test: `workers/rust/crates/sb-transfer/tests/transfer.rs` (an `axum` test server in the test; no S3, no network)

**Interfaces:**
- Consumes: `TransferUrl` (move it to `sb_transfer::TransferUrl`; `sb-slicer` re-exports it so `sb-slicer::models` stays the one place the vectors test imports from).
- Produces:
  ```rust
  pub struct Transferred { pub sha256: String, pub size: u64 }
  pub enum TransferError { Expired, Missing, Refused(u16), Digest { expected: String, actual: String }, Io(std::io::Error), Http(String) }
  pub struct Transfer { client: reqwest::Client }
  impl Transfer {
      pub fn new() -> Self;      // rustls, no proxy from env, 30 s connect timeout, no overall timeout (bodies are large)
      /// Streams the GET body to `dest`, sending `url.headers`; Err(Expired) before sending if `expires_at` is past;
      /// 404 → Missing, 401/403 → Refused; returns the sha256 and size written.
      pub async fn get(&self, url: &TransferUrl, dest: &Path) -> Result<Transferred, TransferError>;
      /// Streams `src` as the PUT body with Content-Length, sending `url.headers`; same error mapping;
      /// returns the sha256 and size read (computed while streaming).
      pub async fn put(&self, url: &TransferUrl, src: &Path) -> Result<Transferred, TransferError>;
  }
  ```
  A presigned S3 PUT needs no extra headers; the internal route needs `X-ScadBuddy-Transfer`. Both come from `url.headers`, so `sb-transfer` knows neither.

- [ ] **Step 1: Write the failing test**

```rust
// tests/transfer.rs: an axum router with GET /blob (bytes "hello"), GET /gone (404), GET /locked (403 unless header
// X-ScadBuddy-Transfer: tok), PUT /sink (stores body in an Arc<Mutex<Vec<u8>>>, asserts Content-Length).
#[tokio::test]
async fn get_streams_and_digests() {
    let (base, _state) = serve().await;
    let dir = tempfile::tempdir().unwrap();
    let got = Transfer::new().get(&url(&base, "/blob", &[]), &dir.path().join("in")).await.unwrap();
    assert_eq!(got.sha256, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
    assert_eq!(got.size, 5);
    assert_eq!(std::fs::read(dir.path().join("in")).unwrap(), b"hello");
}
#[tokio::test] async fn get_404_is_missing_and_403_is_refused() { /* /gone → Missing; /locked without header → Refused(403); with header → Ok */ }
#[tokio::test] async fn expired_url_is_refused_before_any_request() { /* expires_at = now - 1s → Err(Expired); server saw no request */ }
#[tokio::test] async fn put_streams_the_file_and_reports_its_digest() { /* 3 MiB random file → PUT /sink → server body == file, Transferred.size == len, sha matches sha2 of the file */ }
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd workers/rust && cargo test -p sb-transfer`
Expected: FAIL to compile

- [ ] **Step 3: Implement** — `get`: `reqwest::Response::bytes_stream()` into a `tokio::fs::File` through `sha.rs`'s `Hasher` (1 MiB chunks), the file written to `dest.with_extension("part")` and renamed on success; `put`: `reqwest::Body::wrap_stream` over `tokio_util::io::ReaderStream` of the file, with a `sha2` tee; `Content-Length` from the file's metadata.

- [ ] **Step 4: Run and commit**

Run: `cd workers/rust && cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`
Expected: PASS

```bash
git add workers/rust
git commit -m "feat(rust): sb-transfer streams TransferUrls to and from files with sha256"
```

---

### Task 4: `sb-temporal`: worker bootstrap, health, metrics, tracing

**Files:**
- Create: `workers/rust/crates/sb-temporal/Cargo.toml`, `src/lib.rs`, `src/health.rs`, `src/metrics.rs`, `src/telemetry.rs`
- Test: `workers/rust/crates/sb-temporal/tests/health.rs`

**Interfaces:**
- Produces:
  ```rust
  pub struct WorkerEnv { pub address: String, pub namespace: String, pub task_queue: String, pub concurrency: usize, pub metrics_port: u16 /* 9090 */ }
  impl WorkerEnv { pub fn from_env(queue_var: &str, queue_default: &str, concurrency_var: &str) -> anyhow::Result<Self> }
      // SCADBUDDY_TEMPORAL_ADDRESS (required), SCADBUDDY_TEMPORAL_NAMESPACE (default "default"), the two named variables
  /// Connects (plain gRPC to `address`, as the Python workers do in-cluster); installs the OTel plugin when
  /// OTEL_EXPORTER_OTLP_ENDPOINT is set and OTEL_SDK_DISABLED is not "true".
  pub async fn connect(env: &WorkerEnv, runtime: &temporalio_sdk::Runtime) -> anyhow::Result<temporalio_client::Client>;
  /// GET /healthz ("ok" once `ready`, else 503) and GET /metrics (the prometheus registry) on 0.0.0.0:port.
  pub fn serve_health(port: u16, ready: Arc<AtomicBool>, registry: prometheus::Registry) -> tokio::task::JoinHandle<()>;
  pub struct Stages { pub stage_seconds: prometheus::HistogramVec /* scadbuddy_render_stage_seconds{stage} */ }
  impl Stages { pub fn register(registry: &Registry) -> Self; pub fn observe(&self, stage: &str, seconds: f64) }
  pub const RENDER_BUCKETS: &[f64] = &[…];   // copied from backend/scadbuddy/core/metrics.py RENDER_BUCKETS, source line in a comment
  pub fn init_tracing() -> anyhow::Result<Option<opentelemetry_sdk::trace::SdkTracerProvider>>;
  ```
- `telemetry.rs`: `tracing_subscriber` with a JSON `fmt` layer (level from `RUST_LOG`, default `info`) and, when exporting, a `tracing_opentelemetry` layer on an OTLP exporter from `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_PROTOCOL` (grpc default) / `OTEL_SERVICE_NAME` (default `scadbuddy-slicer`), and the SDK's `OpenTelemetryPlugin` with `WorkflowIdGenerator` on the provider, as the SDK README's "OpenTelemetry tracing" section shows (features `experimental` + `opentelemetry`).

- [ ] **Step 1: Write the failing health test**

```rust
#[tokio::test]
async fn healthz_turns_ok_when_ready_and_metrics_lists_the_stage_histogram() {
    let registry = prometheus::Registry::new();
    let stages = sb_temporal::Stages::register(&registry);
    stages.observe("slice", 1.5);
    let ready = Arc::new(AtomicBool::new(false));
    let port = free_port();
    let _server = sb_temporal::serve_health(port, ready.clone(), registry);
    wait_for_listen(port).await;
    assert_eq!(reqwest::get(format!("http://127.0.0.1:{port}/healthz")).await.unwrap().status(), 503);
    ready.store(true, Ordering::SeqCst);
    assert_eq!(reqwest::get(format!("http://127.0.0.1:{port}/healthz")).await.unwrap().status(), 200);
    let body = reqwest::get(format!("http://127.0.0.1:{port}/metrics")).await.unwrap().text().await.unwrap();
    assert!(body.contains("scadbuddy_render_stage_seconds_bucket{stage=\"slice\""));
}
#[test] fn from_env_defaults_the_queue_and_concurrency() { /* with only the address set: queue "slicer", concurrency ≥ 1 */ }
```

- [ ] **Step 2: Run it to verify it fails** — `cd workers/rust && cargo test -p sb-temporal` → FAIL to compile.

- [ ] **Step 3: Implement the four modules.** `connect` uses `temporalio_client::Connection::connect` and `ClientOptions::new(namespace)`; the exact builder names are in the `temporalio-client` 1.1.0 docs on docs.rs, which the implementer reads rather than guesses.

- [ ] **Step 4: Run and commit**

Run: `cd workers/rust && cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`
Expected: PASS

```bash
git add workers/rust
git commit -m "feat(rust): sb-temporal worker bootstrap with /healthz, /metrics and OTel"
```

---

### Task 5: The slicer image: Dockerfile `slicer` stage and its publish

**Files:**
- Modify: `Dockerfile` (two new stages: `slicer-build`, `slicer`), `.hadolint.yaml` only if a new rule must be ignored (say which and why in the Dockerfile comment)
- Modify: `.github/workflows/build-image.yml` (publish target `slicer` as `ghcr.io/eh-homelab/scadbuddy-slicer`, buildx `type=gha` cache `scope=slicer`, `mode=min`), `.github/workflows/ci.yml` `image` job (build the `slicer` target too and run its smoke check)
- Create: `workers/rust/bins/scadbuddy-slicer/Cargo.toml`, `src/main.rs` (`--version` and `--check-cli` only in this task; the worker loop comes in Task 7)
- Modify: `README.md` "Deploying" (the slicer image, its variables)

**Interfaces:**
- Produces: image `slicer` with `/usr/local/bin/scadbuddy-slicer`, `/opt/bambu-studio/` (the extracted AppImage; `/opt/bambu-studio/AppRun` is the CLI), `/opt/bambu-studio/profiles/BBL/` (the profiles the Python side reads, Task 8), `ENV BAMBU_STUDIO_VERSION=02.08.02.61`, `CMD ["scadbuddy-slicer"]`, `EXPOSE 9090`.
- `scadbuddy-slicer --check-cli` runs `AppRun --help` headless and exits 0 only if the output names `--slice` and `--export-3mf` and the version string matches `BAMBU_STUDIO_VERSION`.

- [ ] **Step 1: Pin the AppImage**

Run: `curl -sL https://github.com/bambulab/BambuStudio/releases/download/v02.08.02.61/BambuStudio_ubuntu24.04-v02.08.02.61-20260820225108.AppImage | sha256sum`
Record it as `ARG BAMBU_STUDIO_SHA256_AMD64`. There is no arm64 AppImage: the stage fails with a message for `TARGETARCH != amd64`, as the `openscad-lsp` stage does (`Dockerfile:361-390`).

- [ ] **Step 2: Write the stages**

`slicer-build`: `FROM rust:<toolchain>-bookworm AS slicer-build`, `apt-get install -y --no-install-recommends protobuf-compiler`, `COPY workers/rust /src`, `cargo build --release --locked -p scadbuddy-slicer`.
`slicer`: `FROM debian:bookworm-slim AS slicer`; `apt-get install` `ca-certificates`, `curl`, and the AppImage's runtime libraries: start from `libgl1 libglu1-mesa libgtk-3-0 libwebkit2gtk-4.1-0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libegl1 libosmesa6` and prune to what leaves `ldd /opt/bambu-studio/usr/bin/bambu-studio | grep 'not found'` empty (record the final list and how it was measured in the comment); download by the pinned URL, `sha256sum --check --strict` through a file (DL4006), `chmod +x`, `--appimage-extract` to `/opt/bambu-studio` (extraction needs no FUSE), `rm` the AppImage; `COPY --from=slicer-build /src/target/release/scadbuddy-slicer /usr/local/bin/`; `RUN scadbuddy-slicer --check-cli`; a non-root `USER slicer` with a writable `/tmp`.

- [ ] **Step 3: Build and check locally**

Run: `docker build --target slicer -t scadbuddy-slicer:dev . && docker run --rm scadbuddy-slicer:dev scadbuddy-slicer --check-cli && docker run --rm scadbuddy-slicer:dev /opt/bambu-studio/AppRun --help 2>&1 | grep -c -- '--slice'`
Expected: both succeed; the grep prints ≥ 1. If `AppRun --help` wants a display, set the headless `ENV` the stage needs (the CLI path of `BambuStudio.cpp` starts no GUI, but `AppRun` may export GTK variables; `xvfb` is a last resort, recorded if needed).

- [ ] **Step 4: Lint and wire CI**

Run: `hadolint --config .hadolint.yaml Dockerfile && actionlint` → clean. Add the `slicer` target to `ci.yml`'s `image` job (build, then the two `docker run`s) and to `build-image.yml` with cache `scope=slicer`, `mode=min` (the Actions cache is full: CLAUDE.md "CI and caching rules").

- [ ] **Step 5: Document and commit**

README "Deploying": the `scadbuddy-slicer` image, its five variables (Global Constraints), that it mounts no volume and holds no credentials, and that it must reach the API's internal URL and, on `s3`, the bucket endpoint.

```bash
git add Dockerfile .github/workflows/build-image.yml .github/workflows/ci.yml README.md workers/rust/bins
git commit -m "build(slicer): the scadbuddy-slicer image with Bambu Studio v02.08.02.61 pinned by sha256"
```

---

### Task 6: `slice_plate`: the CLI runner with fifo progress

**Files:**
- Create: `workers/rust/crates/sb-slicer/src/runner.rs`, `src/progress.rs`, `src/activity.rs`, `src/error.rs`
- Create: `workers/rust/crates/sb-slicer/tests/fake-bambu-studio.sh` (executable), `tests/runner.rs`, `tests/progress.rs`, `tests/activity.rs`

**Interfaces:**
- Consumes: `sb_transfer::{Transfer, TransferUrl, Transferred}`, `sb_temporal::Stages`, the models.
- Produces:
  ```rust
  // progress.rs
  pub enum Line { Progress(SliceProgress), Warning(String), Other }
  pub struct LineBuffer { … }   // push(&[u8]) -> Vec<Line>: splits on '\n', keeps a partial tail, parses each whole line
  // runner.rs
  pub struct RunRequest<'a> { pub cli: &'a Path, pub input_3mf: &'a Path, pub machine: &'a Path, pub process: &'a Path, pub filaments: &'a [PathBuf],
      pub out_3mf: &'a Path, pub slicedata_dir: Option<&'a Path>, pub plate: u32, pub arrange: Arrange, pub allow_newer_file: bool, pub debug: u8 }
  pub struct RunOutcome { pub exit_code: i32, pub warnings: Vec<String>, pub stderr_tail: String, pub elapsed: Duration }
  /// Runs the CLI in its own process group; calls `on_progress` for each progress line (coalesced to ≤ 1/s, the last
  /// one always delivered); kills the group when `cancel` fires or on any error; never blocks on the fifo.
  pub async fn run(req: RunRequest<'_>, on_progress: impl FnMut(SliceProgress), cancel: CancellationToken) -> Result<RunOutcome, RunError>;
  pub fn command_line(req: &RunRequest<'_>, fifo: &Path) -> Vec<OsString>;   // pure: the argv spec §5.2 fixes
  // activity.rs
  pub struct SlicerActivities { pub cli: PathBuf, pub transfer: Transfer, pub stages: Stages }
  #[activities] impl SlicerActivities { #[activity] pub async fn slice_plate(self: Arc<Self>, ctx: ActivityContext, input: SlicePlateInput) -> Result<SlicePlateOutput, ActivityError>; }
  pub const SLICER_ERROR: &str = "SlicerError"; pub const VALIDATION_EXIT: i32 = 192;
  ```
- `command_line` yields, in order: `--pipe <fifo>`, `--debug <n>`, `--load-settings <machine>;<process>`, `--load-filaments <f1;…>` (joined with `;`, as `BambuStudio.cpp` splits them), `--allow-newer-file` when set, `--arrange 0|1|auto` (`No`→`0`, `Yes`→`1`), `--slice <plate>`, `--export-slicedata <dir>` when set, `--export-3mf <out>`, then the input path last.
- `slice_plate`: a tempdir per attempt; `transfer.get` of `input_3mf`, `machine_preset`, `process_preset`, each `filament_presets[i]` (to `filament_<i+1>.json`); `run`, heartbeating each progress as the one detail; on exit 0, `transfer.put(sliced_3mf, out)` and, when asked, zip `slicedata_dir` and `transfer.put(slicedata, zip)`; `stages.observe("slice", elapsed)`; return the output with the two `Transferred`s' sha256 and size. Error mapping: exit `192` → `ActivityError::Application` of type `SlicerError`, `non_retryable: true`, details `{"exit_code":192,"stderr_tail":…}`; other non-zero → same type, retryable; `TransferError::Missing | Expired | Refused` → type `TransferRefused`, non-retryable (the caller mints anew on the next start); a PUT's transport error → retryable (the slice is redone: the activity is one unit); cancellation → `ActivityError::Cancelled`. Span: `tracing::info_span!("slice.plate", plate, exit_code = Empty)` and nothing else.

- [ ] **Step 1: Write the fake CLI**

```sh
#!/bin/sh
# tests/fake-bambu-studio.sh: speaks the CLI's protocol. FAKE_EXIT (default 0), FAKE_SPLIT=1 writes one
# progress line in two chunks with a pause, FAKE_SILENT=1 writes nothing to the pipe, FAKE_HANG=1 sleeps 300 s.
pipe=""; out=""
while [ $# -gt 0 ]; do case "$1" in --pipe) pipe="$2"; shift;; --export-3mf) out="$2"; shift;; esac; shift; done
[ -n "$FAKE_HANG" ] && sleep 300
if [ -z "$FAKE_SILENT" ]; then
  if [ -n "$FAKE_SPLIT" ]; then
    printf '{"plate_index":0,"plate_count":1,"plate_pe' > "$pipe"; sleep 0.2
    printf 'rcent":40,"total_percent":40,"message":"Slicing"}\n' > "$pipe"
  else
    printf '{"plate_index":0,"plate_count":1,"plate_percent":40,"total_percent":40,"message":"Slicing"}\n' > "$pipe"
  fi
  printf '{"warning":"thin wall"}\n{"plate_index":0,"plate_count":1,"plate_percent":100,"total_percent":100,"message":"Done"}\n' > "$pipe"
fi
[ "${FAKE_EXIT:-0}" = 0 ] && printf 'PK fake 3mf' > "$out"
echo "fake stderr line" >&2
exit "${FAKE_EXIT:-0}"
```

- [ ] **Step 2: Write the failing tests**

`tests/progress.rs`: `LineBuffer` given `b'{"plate_index":0,"plate_count":1,"plate_pe'` yields nothing; then the rest yields one `Progress` with `total_percent == 40`; two lines in one push yield two; `{"warning":"x"}` yields `Warning("x")`; `not json\n` yields `Other`.
`tests/runner.rs` (the fake as `RunRequest.cli`):
- exit 0: `exit_code == 0`, `warnings == ["thin wall"]`, the callback's last `total_percent == 100`, `out_3mf` exists, `stderr_tail` contains `fake stderr line`.
- `FAKE_SPLIT=1`: the same progress values (Review Focus 1).
- `FAKE_SILENT=1 FAKE_EXIT=192`: returns within 5 s with `exit_code == 192` (Review Focus 2).
- `FAKE_HANG=1`, `cancel.cancel()` after 200 ms: `Err(RunError::Cancelled)` within 2 s, and `pgrep -g <pgid>` finds nothing (Review Focus 3; expose the pgid through `RunError::Cancelled { pgid }` or a `run_with_pgid` for the test).
- `command_line` equals the exact argv above for a two-filament request.
`tests/activity.rs`: an `axum` server (as Task 3's) serving the fake inputs on GET and a sink on PUT; `ActivityEnvironment` (feature `testing`) with `SlicerActivities { cli: fake, … }`; `env.run(SlicerActivities::slice_plate, input)` returns an output whose `sliced_sha256` equals the sink body's sha and `sliced_size` its length; with `FAKE_EXIT=192` the error is `Application` with type `SlicerError` and `non_retryable`; with `FAKE_EXIT=1` it is retryable; with an expired `input_3mf` the error type is `TransferRefused` and the server saw no GET.

- [ ] **Step 3: Run them to verify they fail** — `cd workers/rust && cargo test -p sb-slicer` → FAIL to compile.

- [ ] **Step 4: Implement `progress.rs`, `runner.rs`, `activity.rs`**

`runner.rs`: `tempfile::tempdir()` per run; `nix::unistd::mkfifo(path, Mode::S_IRWXU)`; open the fifo `OpenOptions::new().read(true).write(true).custom_flags(libc::O_NONBLOCK)` wrapped in `tokio::fs::File` (the runner's own write side keeps `read` from reporting EOF before the child opens; reading stops when the child has exited and the buffer drains); `tokio::process::Command` with `.process_group(0)`, stdout and stderr piped, stderr kept as a ring of the last 8 KiB; on cancel or error `nix::sys::signal::killpg(pgid, SIGKILL)`. Coalesce progress with a 1 s `tokio::time::Interval`, always emitting the last line seen before exit.

- [ ] **Step 5: Run and commit**

Run: `cd workers/rust && cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`
Expected: PASS

```bash
git add workers/rust
git commit -m "feat(slicer): slice_plate runs the Bambu Studio CLI with fifo progress as heartbeats"
```

---

### Task 7: The `scadbuddy-slicer` worker binary and its Temporal test

**Files:**
- Modify: `workers/rust/bins/scadbuddy-slicer/src/main.rs`; create `src/lib.rs` (`run_worker`)
- Test: `workers/rust/bins/scadbuddy-slicer/tests/worker.rs` (skips unless `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` names a Temporal CLI or `temporal` is on `PATH`, as the agent's Temporal tests do)

**Interfaces:**
- Consumes: `sb_temporal::{WorkerEnv, connect, serve_health, Stages, init_tracing}`, `sb_slicer::activity::SlicerActivities`.
- Produces: `pub async fn run_worker(runtime: &Runtime, client: Client, queue: &str, concurrency: usize, activities: SlicerActivities, ready: Arc<AtomicBool>) -> anyhow::Result<()>`. `main`: `init_tracing`, `WorkerEnv::from_env("SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER", "slicer", "SCADBUDDY_SLICER_CONCURRENCY")`, `BAMBU_STUDIO_CLI` (default `/opt/bambu-studio/AppRun`), `serve_health(9090, …)`, `WorkerOptions::new(queue).register_activities(...)` with the concurrency option (name per docs.rs), `Worker::new(&runtime, client, options)?.run()`, ready true once running, SIGTERM → graceful shutdown. No workflows. Unversioned (spec §7.2: versioning comes with the first Rust workflow).

- [ ] **Step 1: Write the failing worker test** — `WorkflowEnvironment::start_local` (feature `testing`); `run_worker` with a fake-CLI `SlicerActivities` and Task 6's axum server; a tiny Rust test workflow defined in the test executes `slice_plate` and returns its output; assert `exit_code == 0`; a `192` run surfaces as an `ActivityError` whose failure type is `SlicerError`.

- [ ] **Step 2: Run it to verify it fails** — `cd workers/rust && cargo test -p scadbuddy-slicer` → FAIL to compile.

- [ ] **Step 3: Implement `lib.rs` and `main.rs`.**

- [ ] **Step 4: Run, build the image, commit**

Run: `cd workers/rust && cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace && cd ../.. && docker build --target slicer -t scadbuddy-slicer:dev .`
Expected: PASS; `docker run --rm -e SCADBUDDY_TEMPORAL_ADDRESS=nowhere:7233 scadbuddy-slicer:dev` logs a connection error and exits non-zero (no hang).

```bash
git add workers/rust
git commit -m "feat(slicer): the scadbuddy-slicer worker serves slice_plate on the slicer queue"
```

---

### Task 8: Presets as files (`slicing/presets.py`)

**Files:**
- Create: `backend/scadbuddy/slicing/presets.py`, `backend/scadbuddy/slicing/profiles.py`
- Modify: `Dockerfile` `app` stage (`COPY --from=slicer /opt/bambu-studio/profiles/BBL /opt/slicer-profiles/BBL`), `backend/scadbuddy/core/settings.py` (`slicer_profiles_dir: Path = Path("/opt/slicer-profiles")`, an infrastructure field in `BOOTSTRAP_FIELDS`, not env-seeded into Postgres)
- Test: `backend/tests/slicing/test_presets.py`, fixtures `backend/tests/slicing/profiles/BBL/` (a three-level `inherits` chain, a loop, a missing parent; small JSON files)

**Interfaces:**
- Consumes: `SlicePlan` (`bambuddy/dispatch.py:61`), `PresetRef` (`bambuddy/models.py:34`), the override semantics of `bambuddy/filament_profiles.py` and the 2026-10-13 filament-profiles spec, `BambuddyClient.preset_values` (`client.py:482`) for a `local:` preset's values when a Bambuddy is configured.
- Produces:
  ```python
  class Profiles:                       # profiles.py: Bambu Studio's resources/profiles/BBL as shipped in the image
      def __init__(self, root: Path) -> None
      def machine(self, name: str) -> dict[str, str | list[str]]      # flattened
      def process(self, name: str) -> dict[str, str | list[str]]
      def filament(self, name: str) -> dict[str, str | list[str]]
      @property
      def version(self) -> str          # BBL.json's "version"
  class ProfileError(ValueError): ...   # a missing preset, a missing parent, an inherits loop; the message names the preset

  def flatten(name: str, load: Callable[[str], dict[str, Any]]) -> dict[str, str | list[str]]
      # follows "inherits" to the root, child over parent; keeps every key but "inherits"; refuses a loop and a missing parent

  class PresetFiles(BaseModel): machine: bytes; process: bytes; filaments: list[bytes]      # the JSON documents
  async def build_presets(plan: SlicePlan, profiles: Profiles, client: BambuddyClient | None) -> PresetFiles
      # process = flatten(process) with apply_overrides(plan.process_overrides); a `cloud:`/`standard:` ref resolves by name;
      # a `local:` ref needs `client` (its preset_values), else ProfileError
  def apply_overrides(process: dict[str, str | list[str]], overrides: Mapping[str, ProcessValue]) -> dict[str, str | list[str]]
      # a str over a str; a list over a list position by position (per-extruder variants, #717); a str over a list sets every position
  def native_preset_key(plan: SlicePlan, plate: int, version: str) -> str
      # f"native@{version}/" + slice_request(plan, plate).preset_key
  ```

- [ ] **Step 1: Write the failing tests**

```python
def test_flatten_child_wins_and_drops_inherits(profiles):            # chain: leaf -> mid -> root
    out = profiles.process("leaf")
    assert out["layer_height"] == "0.16" and out["wall_loops"] == "3" and "inherits" not in out

def test_flatten_refuses_a_loop(profiles):
    with pytest.raises(ProfileError, match="loop.*loop_a"): profiles.process("loop_a")

def test_flatten_refuses_a_missing_parent(profiles):
    with pytest.raises(ProfileError, match="orphan.*no such"): profiles.process("orphan")

def test_apply_overrides_per_extruder():
    assert apply_overrides({"outer_wall_speed": ["200", "200"]}, {"outer_wall_speed": ["150", "200"]}) == {"outer_wall_speed": ["150", "200"]}
    assert apply_overrides({"outer_wall_speed": ["200", "200"]}, {"outer_wall_speed": "150"}) == {"outer_wall_speed": ["150", "150"]}

async def test_build_presets_gives_one_file_per_slot(profiles, plan):   # plan: cloud refs resolving in the fixture profiles
    files = await build_presets(plan, profiles, None)
    assert len(files.filaments) == 2 and json.loads(files.process)["layer_height"] == "0.16"

def test_native_preset_key_is_distinct_from_bambuddys(plan):
    assert native_preset_key(plan, 1, "02.08.02.61") == "native@02.08.02.61/" + slice_request(plan, 1).preset_key
```

- [ ] **Step 2: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_presets.py -v` → import errors.

- [ ] **Step 3: Implement `profiles.py` and `presets.py`**

`Profiles` indexes `BBL/machine/*.json`, `BBL/process/*.json`, `BBL/filament/*.json` by each file's `"name"` once, lazily. A `PresetRef.id` for `cloud`/`standard` is the preset *name* on the Bambuddy side (`resolver.py:56-80` picks by name); confirm against `bambuddy/catalogue.py:105-143` and record the mapping in the docstring. One test marked `requires_slicer_profiles` skips unless `/opt/slicer-profiles/BBL/BBL.json` exists (so it runs in the test image): it flattens `"Bambu Lab H2C 0.4 nozzle"` and asserts `printable_height` is present.

- [ ] **Step 4: Run and commit**

Run: `cd backend && uv run --frozen pytest tests/slicing -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS (the image-only test skipped locally)

```bash
git add backend/scadbuddy/slicing backend/tests/slicing backend/scadbuddy/core/settings.py Dockerfile
git commit -m "feat(slicing): presets as flattened Bambu Studio profile files with ScadBuddy's overrides"
```

---

### Task 9: Reading a sliced 3MF (`slicing/slice_info.py`)

**Files:**
- Create: `backend/scadbuddy/slicing/slice_info.py`
- Modify: `backend/scadbuddy/bambuddy/preview.py:57,341-383` (`_filament_changes`'s G-code reader becomes the pure `filament_changes(sliced: bytes, plate_id: int) -> int | None` in `slice_info.py`, which `preview.py` imports; `_LOAD` and the `MAX_*` caps move with it)
- Test: `backend/tests/slicing/test_slice_info.py`, with a small 3MF zip (`Metadata/slice_info.config`, `Metadata/plate_1.gcode`) the test writes

**Interfaces:**
- Consumes: `SLICE_INFO_NAME` and the parser in `bambuddy/filaments3mf.py:53,147,241` (`used_g` per filament per plate); extend it, do not duplicate it.
- Produces:
  ```python
  class SlicedPlate(BaseModel):
      plate_id: int
      print_time_seconds: float | None     # <metadata key="prediction" value="…"/>
      slots: list[PreviewSlot]             # bambuddy/preview.py:67; side=None (the native slicer gives no side)
      filament_used_g: float | None        # the slots' sum
      filament_changes: int | None
  def read_sliced(sliced: bytes, plate_id: int) -> SlicedPlate   # ApiError 404 for a plate the file lacks
  def filament_changes(sliced: bytes, plate_id: int) -> int | None
  ```

- [ ] **Step 1: Write the failing tests** — one plate (`index="1"`, `prediction="3600"`, filaments `used_g="12.5"`/`"3.0"`) and a `plate_1.gcode` with `M620 S0A`, `M620 S1A`, `M620 S0A` → `print_time_seconds == 3600`, `filament_used_g == 15.5`, two slots, `filament_changes == 2`; a missing plate → 404.
- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_slice_info.py -v` → import error.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the slicing tests and the preview tests** — `cd backend && uv run --frozen pytest tests/slicing $(grep -rl read_preview tests/api | tr '\n' ' ') -v` → PASS.
- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/slicing/slice_info.py backend/scadbuddy/bambuddy/preview.py backend/tests/slicing/test_slice_info.py
git commit -m "feat(slicing): read time, grams and filament changes from a sliced 3MF"
```

---

### Task 10: Transfer URLs and the internal blob routes (`slicing/transfer.py`)

**Files:**
- Create: `backend/scadbuddy/slicing/transfer.py`, `backend/scadbuddy/slicing/transfer_token.py`
- Modify: `backend/scadbuddy/api/internal.py` (routes `GET` and `PUT /api/v1/internal/blobs/{key}`; the router's prefix today is `INTERNAL_OUTPUTS`-derived, so add a second router `blobs_router` with prefix `/api/v1/internal/blobs`, `include_in_schema=False`, registered where `internal.router` is), `backend/scadbuddy/core/settings.py` (`api_internal_url` exists for the print worker; reuse it as the base of minted internal URLs)
- Test: `backend/tests/slicing/test_transfer_token.py`, `backend/tests/api/test_internal_blobs.py` (requires_postgres; `local` and `bambuddy`-shaped stores through the api `client` fixture), `backend/tests/slicing/test_transfer.py`

**Interfaces:**
- Consumes: `ContentStore.put/get/stat` (`store/content.py:175,299,408`), `BlobRef`, `BlobScope`, the KEK file `SCADBUDDY_SECRET_KEY_FILE` (already required by flows; `payload_codec.py` reads it), and, from the S3 plan (`docs/superpowers/plans/2026-10-16-s3-store-backend.md`), `S3ContentBackend.presign_get(backend_id, expires: timedelta) -> str` and `S3ContentBackend.presign_put(backend_id, expires: timedelta) -> str`; this plan does not define them. Until that plan lands, `mint_*` on a backend without `presign_*` falls back to the internal routes (`hasattr` check), so the native slicer works on `s3` either way.
- Produces:
  ```python
  TRANSFER_TTL = timedelta(hours=2)
  TRANSFER_HEADER = "X-ScadBuddy-Transfer"

  # transfer_token.py: HMAC-SHA256 under a key derived from the KEK (HKDF info b"scadbuddy/transfer/v1"), urlsafe base64 of
  # f"{method}\n{key}\n{expires_unix}" + "." + mac; verify refuses a wrong method, another key, a tampered mac, or a past expiry.
  class TransferToken: method: Literal["GET", "PUT"]; key: str; expires_at: datetime
  def sign(kek: bytes, token: TransferToken) -> str
  def verify(kek: bytes, value: str, *, method: str, key: str, now: datetime) -> TransferToken   # raises TokenError

  # transfer.py
  class Transfers:
      def __init__(self, content: ContentStore, *, internal_base_url: str, kek: bytes, now: Callable[[], datetime] = utcnow)
      async def mint_get(self, ref: BlobRef) -> TransferUrl
          # s3 with presign: TransferUrl(url=presign_get(ref.backend_id, TRANSFER_TTL), headers={}, expires_at=now+TTL)
          # else: f"{internal_base_url}/api/v1/internal/blobs/{key}" with {TRANSFER_HEADER: sign(GET, key)}
      async def mint_put(self, kind: BlobKind, name: str) -> PendingPut
          # PendingPut(url: TransferUrl, kind, name, pending_key: str)   pending_key = f"pending/{kind}/{uuid4().hex}"
          # s3 with presign: a presigned PUT of backend_id f"{prefix}/{pending_key}"; else the internal PUT route of pending_key
      async def finish_put(self, put: PendingPut, *, sha256: str, size: int, scope: BlobScope) -> BlobRef
          # s3: ContentStore.adopt_object(kind, pending backend_id, sha256, size, name, scope) → the index row, the object
          #   copied/renamed to its content key by the backend (S3 CopyObject) and the pending one removed; refuses a size or
          #   sha the object does not have (HEAD/ETag check in the backend, or a streamed re-hash under 64 MiB)
          # internal: the PUT route already stored it under its content key through ContentStore.put, recording
          #   `transfer_puts(pending_key → key, sha256, size)`; finish_put reads that row, checks sha and size, returns the ref
  ```
  Internal routes (`api/internal.py`): `GET /api/v1/internal/blobs/{key:path}` verifies the header token for `GET` and that key, then streams `ContentStore.get(key)` with `Content-Length` from `stat`; `PUT /api/v1/internal/blobs/{key:path}` verifies for `PUT`, spools the request body to a temp file under `cache/blob-spool` (never into memory), `ContentStore.put(kind, file, name=…, scope=BlobScope(folder="work"))`, writes a `transfer_puts` row (`pending_key`, `key`, `sha256`, `size`, `created_at`; migration in this task; rows older than a day pruned by the Task 12 sweep) and answers 204. A missing or bad token is 403 (not 401: nothing to challenge), an expired one 403, an unknown key 404 after the token check (so a token cannot probe keys it was not minted for).

- [ ] **Step 1: Write the failing token tests**

```python
def test_token_round_trips():
    t = TransferToken(method="GET", key="slice/abc", expires_at=AT)
    assert verify(KEK, sign(KEK, t), method="GET", key="slice/abc", now=AT - timedelta(minutes=1)) == t

@pytest.mark.parametrize("method,key,now", [("PUT", "slice/abc", AT - 1min), ("GET", "slice/other", AT - 1min), ("GET", "slice/abc", AT + 1s)])
def test_token_refuses_other_method_key_or_expiry(method, key, now):
    with pytest.raises(TokenError): verify(KEK, sign(KEK, TransferToken(method="GET", key="slice/abc", expires_at=AT)), method=method, key=key, now=now)

def test_tampered_mac_is_refused(): ...   # flip one char of the mac
```

- [ ] **Step 2: Write the failing route and `Transfers` tests** — with the api `client` on a `local`-shaped `ContentStore` (the test store the api fixtures already build, or `LocalContentBackend`): `mint_get` of a put blob gives a URL the test GETs through `client` with the minted headers → the bytes; the same URL with the header stripped → 403; a GET token used for PUT → 403; `mint_put` then PUT 5 bytes → 204, `finish_put(sha256 of the bytes, 5)` → a `BlobRef` the store can `read`; `finish_put` with a wrong sha → `TransferError`; a URL minted with `now` two hours and a second later → 403 (Review Focus 5).

- [ ] **Step 3: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_transfer_token.py tests/slicing/test_transfer.py tests/api/test_internal_blobs.py -v` → import errors.

- [ ] **Step 4: Implement** — the migration `transfer_puts`, `transfer_token.py` (`hmac`, `hashlib`, HKDF via `cryptography`, already a dependency of the backend? check `pyproject.toml`; if not, `hashlib.pbkdf2_hmac` with a fixed salt is enough for a key derivation from 32 random bytes and adds nothing), `transfer.py`, the two routes, `ContentStore.adopt_object` (s3 only, under the S3 plan's backend; here it raises `NotImplementedError` for a backend without `presign_*`, since such a backend never mints a presigned PUT).

- [ ] **Step 5: Run, lint, commit**

Run: `cd backend && uv run --frozen pytest tests/slicing tests/api/test_internal_blobs.py -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS

```bash
git add backend/scadbuddy/slicing backend/scadbuddy/api/internal.py backend/scadbuddy/migrations backend/scadbuddy/store/content.py backend/tests
git commit -m "feat(slicing): transfer URLs for workers: signed internal blob routes, presigned S3 when the backend can"
```

---

### Task 11: `slicer_backend`, the `native_slices` table and `SliceWorkflow`

**Files:**
- Create: `backend/scadbuddy/migrations/<now>_native_slices.sql`, `backend/scadbuddy/slicing/store.py`, `backend/scadbuddy/slicing/component.py`, `backend/scadbuddy/workflows/slicing.py`
- Modify: `backend/scadbuddy/core/config.py:91` (`Slicer = Literal["bambuddy", "native"]`), `core/settings.py` (`slicer_backend: Slicer = "bambuddy"`, `temporal_task_queue_slicer: str = "slicer"`; `APPLIES["slicer_backend"] = "live"`), `library/settings_store.py` (the stored field, as `store_backend` is stored), `main.py:519-525` (register `SliceWorkflow`, `slice_prepare` and `slice_record` on the `library` queue's in-process worker), `workflows/client.py` (the `library` worker's workflow list)
- Test: `backend/tests/slicing/test_store.py` (requires_postgres), `backend/tests/api/test_slice_workflow.py` (requires_temporal + pg), `backend/tests/support/fake_slicer.py`

**Interfaces:**
- Consumes: `Transfers` (Task 10), `build_presets`, `native_preset_key` (Task 8), `read_sliced` (Task 9), the models (Task 1).
- Produces:
  ```sql
  CREATE TABLE native_slices (
    id bigserial PRIMARY KEY,
    workflow_id text NOT NULL UNIQUE,            -- slice-<sha256(input sha256, preset_key)[:32]>
    subject text NOT NULL,                       -- the run subject, as print_preview_slices.subject
    input_sha256 text NOT NULL,
    preset_key text NOT NULL,                    -- native_preset_key(...)
    plate_id integer NOT NULL,
    status text NOT NULL DEFAULT 'running',      -- running | completed | failed | cancelled
    failure text,
    sliced_blob_key text,                        -- store_blobs key of the sliced 3MF
    warnings jsonb NOT NULL DEFAULT '[]',
    printed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
  );
  CREATE INDEX native_slices_reuse ON native_slices (input_sha256, preset_key) WHERE status = 'completed';
  ```
  ```python
  class NativeSlice(BaseModel): id, workflow_id, subject, input_sha256, preset_key, plate_id, status, failure, sliced_blob_key, warnings, printed, created_at, finished_at
  class NativeSliceStore:                                     # slicing/store.py, over the pool
      async def start(self, row: NativeSlice) -> NativeSlice   # insert, or the existing row of the same workflow_id
      async def finish(self, workflow_id: str, *, status: str, sliced_blob_key: str | None, failure: str | None, warnings: list[str]) -> None
      async def get(self, slice_id: int, subject: str) -> NativeSlice | None
      async def reusable(self, input_sha256: str, preset_key: str) -> NativeSlice | None
      async def claim(self, slice_id: int) -> bool              # printed = true once
      async def sweep(self, older_than: timedelta) -> list[str] # finished rows past grace: their blob keys, rows deleted
  SLICING = Component(...)                                     # slicing/component.py: Slicing(store, transfers, profiles, content, client, settings)

  class SliceWorkflowInput(BaseModel): slice_id: int; workflow_id: str; input_3mf: BlobRef; presets: PresetFiles; plate: int;
                                        arrange: Literal["no","yes","auto"]; slicer_queue: str; timeout: timedelta
  class SliceWorkflowResult(BaseModel): status: Literal["completed", "failed"]; sliced_blob_key: str | None; failure: str | None; warnings: list[str]
  class SlicePrepared(BaseModel): plate_input: SlicePlateInput; put: PendingPut
  class SliceWorkflow:                                         # workflows/slicing.py
      # 1. execute_activity("slice_prepare", input, result_type=SlicePrepared, SHORT): puts the preset files as `slice` blobs
      #    (ContentStore.put, deduped by sha so a repeated preset costs one upload), mints GET URLs for the 3MF and each preset
      #    and a PendingPut for the output; returns SlicePlateInput. Minted inside the workflow's first activity so the two-hour
      #    clock starts at the slice, not at the preview request.
      # 2. execute_activity("slice_plate", prepared.plate_input, task_queue=input.slicer_queue, result_type=SlicePlateOutput,
      #    start_to_close_timeout=input.timeout, heartbeat_timeout=timedelta(seconds=60), retry_policy=RetryPolicy(maximum_attempts=3))
      # 3. execute_activity("slice_record", SliceRecordInput(workflow_id, put, output | failure), SHORT, RECORD_RETRY):
      #    finish_put(...) → the BlobRef → NativeSliceStore.finish(status="completed", sliced_blob_key=key, warnings)
      #    or finish(status="failed", failure=message + stderr_tail)
      # A TransferRefused failure (an expired URL after a long queue wait) runs step 1 again once, then fails.
  async def slice_progress(client: Client, workflow_id: str) -> SliceProgress | None
      # describe() → pending_activities[0].heartbeat_details → payload converter → SliceProgress; None when none
  ```

- [ ] **Step 1: Write the failing store test** — `start` twice with one `workflow_id` returns the same row; `reusable` finds a completed row and not a failed one; `claim` is true once; `sweep(timedelta(0))` returns the blob key and deletes the row.

- [ ] **Step 2: Write the failing workflow test** — register `fake_slicer.slice_plate` (a Python `@activity.defn(name="slice_plate")` that GETs each `TransferUrl` with httpx and the headers, heartbeats one `SliceProgress`, PUTs Task 9's fixture zip to `sliced_3mf`, returns a `SlicePlateOutput` with its sha and size; with `plate == 99` raises `ApplicationError("validation failed", type="SlicerError", non_retryable=True, details=[{"exit_code": 192, "stderr_tail": "…"}])`) on the test's slicer queue; start `SliceWorkflow`; assert the `native_slices` row is `completed` with a `sliced_blob_key` the `ContentStore` can `read` to the fixture bytes, and that plate 99 ends `failed` with `failure` containing the stderr tail after one attempt.

- [ ] **Step 3: Run both to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_store.py tests/api/test_slice_workflow.py -v` → import errors.

- [ ] **Step 4: Implement** — the migration, `store.py`, `component.py`, `workflows/slicing.py` and its two activities, the settings fields (the `render_settings` view needs nothing: the render worker never slices), registration in `main.py` beside the housekeeping activities.

- [ ] **Step 5: Run, lint, commit**

Run: `cd backend && uv run --frozen pytest tests/slicing tests/api/test_slice_workflow.py -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS

```bash
git add backend/scadbuddy/migrations backend/scadbuddy/slicing backend/scadbuddy/workflows/slicing.py backend/scadbuddy/core backend/scadbuddy/library/settings_store.py backend/scadbuddy/main.py backend/scadbuddy/workflows/client.py backend/tests
git commit -m "feat(slicing): slicer_backend setting, native_slices and the SliceWorkflow around slice_plate"
```

---

### Task 12: The print dialog's preview on the native slicer

**Files:**
- Create: `backend/scadbuddy/slicing/preview.py`
- Modify: `backend/scadbuddy/bambuddy/preview.py:89-146` (`start_preview`), `:220-246` (`read_preview`), `:60-86` (`PreviewStarted.slicer`, `SlicePreview.progress: int | None`, `.warnings: list[str]`, `.slicer: Literal["bambuddy","native"] = "bambuddy"`), `bambuddy/operations.py:300-334` (`preview_run` passes the slicing component), the two `preview-slices/{job_id}` routes (`api/library_print.py:1124-1136`, `api/printing.py:981`)
- Modify: `backend/scadbuddy/workflows/housekeeping.py:89-129` (`housekeeping_sweep_native_slices` beside `PREVIEWS_SWEEP`: `NativeSliceStore.sweep(PREVIEW_GRACE)` then `content.delete` per key, and the `transfer_puts` prune from Task 10; behind `workflow.patched("housekeeping-sweep-native-slices")`, following the sweeps' switch)
- Test: `backend/tests/api/test_print_preview_native.py` (requires_temporal + pg; uses `fake_slicer`)

**Interfaces:**
- Consumes: `prepare_run`, `plan_run` (`print_run.py`), `_laid_out_for` (`send.py:265-306`) for the laid-out 3MF bytes, `build_presets`, `native_preset_key`, `NativeSliceStore`, `SliceWorkflow`, `slice_progress`, `read_sliced`.
- Produces:
  ```python
  async def start_native_preview(client: BambuddyClient | None, source: PrintSource, settings: StoredSettings,
                                 slicing: Slicing, request: PrintRunRequest, *, subject: str) -> PreviewStarted
      # prepare + plan as start_preview does (a Bambuddy client only when one is configured: with none, `local:` presets are refused);
      # laid-out bytes → content.put(kind="slice") → BlobRef; presets = build_presets; key = native_preset_key(plan, plate, profiles.version);
      # a reusable row → PreviewStarted(job_id=row.id, slicer="native"); else NativeSliceStore.start +
      # client.start_workflow(SliceWorkflow, id=workflow_id, id_conflict_policy=USE_EXISTING, task_queue=settings.temporal_task_queue_library)
  async def read_native_preview(slicing: Slicing, slice_id: int, subject: str) -> SlicePreview
      # running → status="running", progress=slice_progress(...).total_percent; completed → read_sliced(content.read(key), plate_id);
      # failed → status, failure
  ```
  `start_preview`/`read_preview` branch on `settings.slicer_backend` at their top; the two slicers' job ids are different id spaces, and the row's `subject` check refuses a cross-read.

- [ ] **Step 1: Write the failing test** — with `slicer_backend="native"` and the fake slicer: `POST …/preview-slice` returns a `job_id` and `slicer == "native"`; polling `GET …/preview-slices/{id}` reaches `completed` with `print_time_seconds == 3600`, `filament_used_g == 15.5`, `filament_changes == 2`; a second POST with the same choices returns the same `job_id`; the refused plate reaches `failed` with the stderr tail in `failure`; `GET` under another subject is 404; a row older than `PREVIEW_GRACE` is swept and its blob gone.

- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run --frozen pytest tests/api/test_print_preview_native.py -v` → fails.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the preview tests, both slicers** — `cd backend && uv run --frozen pytest tests/api/test_print_preview_native.py $(grep -rl read_preview tests/api | tr '\n' ' ') -v` → PASS (the Bambuddy path unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/slicing/preview.py backend/scadbuddy/bambuddy/preview.py backend/scadbuddy/bambuddy/operations.py backend/scadbuddy/api backend/scadbuddy/workflows/housekeeping.py backend/tests
git commit -m "feat(slicing): the print dialog's background slice runs on the native slicer when selected"
```

---

### Task 13: `PrintRunWorkflow` on the native slicer

**Files:**
- Create: `backend/scadbuddy/migrations/<now>_output_bambuddy_slices_blob.sql` (`ALTER TABLE output_bambuddy_slices ADD COLUMN sliced_blob_key text;`)
- Modify: `backend/scadbuddy/bambuddy/print_run.py:1371-1393` (`PlannedRun.slicer: Literal["bambuddy","native"]` and `.slicer_queue: str`, read from the live settings in `plan_run`), `workflows/printing.py:323-370` (`_slice` branches), `workflows/print_activities.py` (new activities `print_native_slice_prepare`, `print_sliced_upload`), `workflows/print_models.py` (`NativeSliceInput`, `NativeSlicePrepared`, `SlicedUploadInput`), `bambuddy/dispatch.py:179` (`QueueOutcome.sliced_blob_key: str | None = None`), `bambuddy/uploads.py:570,703-747` (`SlicedCopy.sliced_blob_key`, written by `record`)
- Test: `backend/tests/api/test_print_run_native.py` (requires_temporal + pg); a history fixture of the Bambuddy path exported before the change and replayed after (`tests/fixtures/print_run_histories/`, created if absent)

**Interfaces:**
- Produces:
  ```python
  class NativeSliceInput(BaseModel): planned: PlannedRun; plate: PlatePlan; run_id: str
  class NativeSlicePrepared(BaseModel): slice_id: int; workflow_id: str; preset_key: str; reused_blob_key: str | None;
                                        workflow_input: SliceWorkflowInput | None     # None when reused
  # activity print_native_slice_prepare (bambuddy queue): the plate's laid-out bytes (as print_plan laid them out) → content.put(kind="slice");
  #   build_presets; native_preset_key; NativeSliceStore.reusable → reused_blob_key, else NativeSliceStore.start + the SliceWorkflowInput
  class SlicedUploadInput(BaseModel): planned: PlannedRun; plate: PlatePlan; sliced_blob_key: str
  # activity print_sliced_upload (bambuddy queue): content.read(key) → upload_into(client, f"<copy name>.sliced.3mf", bytes, folder_id=inbox)
  #   → the library file id the Bambuddy queue takes; recorded by print_record through QueueOutcome.sliced_library_file_id
  ```
  In `_slice`, after `if plate.plan is None`: `if planned.slicer == "native" and workflow.patched("native-slicer"):` → `print_native_slice_prepare` (SHORT, READ_RETRY) → unless reused, `execute_child_workflow(SliceWorkflow.run, prepared.workflow_input, id=prepared.workflow_id, task_queue=<library queue>, parent_close_policy=ABANDON)` (a child, so the preview's reuse row and the run's slice are one execution; `USE_EXISTING` on the child id when the dialog started it first) → `print_sliced_upload` (SHORT, READ_RETRY) → return `(SliceStarted(job_id=prepared.slice_id, preset_key=prepared.preset_key), uploaded_library_file_id)`, and set `QueueOutcome.sliced_blob_key` where the outcome is built (`printing.py:432-438`). A `SlicerError` ends the run as `FAILED` with the result's `failure`, never `SLICE_LOST`.

- [ ] **Step 1: Write the failing test** — a run with `slicer_backend="native"`, the fake slicer and the test's fake Bambuddy for the queue succeeds; `output_bambuddy_slices.sliced_blob_key` is set and `sliced_library_file_id` is the fake Bambuddy's upload id; the history shows no `print_slice_start`; with the refused plate the run fails with the stderr tail in its error.

- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run --frozen pytest tests/api/test_print_run_native.py -v` → fails.

- [ ] **Step 3: Implement**, with the replay fixture check.

- [ ] **Step 4: Run the print suite** — `cd backend && uv run --frozen pytest tests/api/test_print_run_native.py $(ls tests/api/test_print_run*.py | tr '\n' ' ') tests/workflows -v` → PASS; the Bambuddy-path history replays.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat(printing): PrintRunWorkflow slices on the native slicer behind the native-slicer patch"
```

---

### Task 14: Progress in the print dialog

**Files:**
- Modify: `frontend/src/lib/usePreviewSlice.ts` (`PreviewSliceState.progress: number | null`, from `read.progress` while running), `frontend/src/components/print/PreviewPane.tsx:214-222` (the `slice-status` span shows a `<progress max={100}>` bar, `data-testid="slice-progress"`, when `progress != null`, with the text `Slicing… 40%`), the msw mock of `preview-slices/{id}` (`grep -rl preview-slices frontend/src/mocks`) answering `progress` on a running native slice
- Test: `frontend/src/components/print/PreviewPane.test.tsx` (a running read with `progress: 40` renders the bar at 40; a Bambuddy read without `progress` renders the text only), `frontend/src/lib/usePreviewSlice.test.ts` (progress updates between polls)

**Interfaces:**
- Consumes: `SlicePreview.progress` and `.slicer` from the regenerated `schema.d.ts` (`pnpm gen:api` runs in `typecheck`).

- [ ] **Step 1: Write the failing tests.**
- [ ] **Step 2: Run to verify they fail** — `cd frontend && pnpm test -- PreviewPane usePreviewSlice` → FAIL.
- [ ] **Step 3: Implement** — a native `<progress>` styled with the pane's tokens; no new library.
- [ ] **Step 4: Run** — `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build` → PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): the print dialog shows the native slicer's progress"
```

> **My call, departing from spec §5.4:** no `slice:<job>` realtime topic in this plan. `realtime.ts` documents that events "carry ids, never content", and nothing on the Python side sees a heartbeat except a `describe()`, so a topic would be a poll republished. The dialog already polls `read_preview` every 2 s (`RESLICE_POLL_MS`); `progress` rides on that read. Revisit when the Rust worker has a path to publish and the realtime contract allows a percent.

---

### Task 15: Dashboards, docs and the agent's view

**Files:**
- Modify: `deploy/grafana/scadbuddy-printing.json` (a "Slice duration" panel on `scadbuddy_render_stage_seconds{stage="slice"}`, p50/p95, and its count)
- Modify: `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §5.4 (a one-line note pointing at Task 14's call), `docs/ai/operating.md` (a line: `slicer_backend` and what the agent sees in `get_settings`), `CLAUDE.md` Layout (`backend/scadbuddy/slicing/` bullet: models, transfer, presets, slice_info, store, preview, component; `workflows/slicing.py`; the internal blob routes)
- Check: `agent/src/tools/coverage.ts` needs no entry (the routes are unchanged; the internal ones are hidden from the schema); `cd agent && pnpm test -- coverage` confirms

- [ ] **Step 1: Add the panel and run the lint** — `KUSTOMIZE=<path to kustomize 5.6.0> .github/scripts/lint-dashboard.sh` → clean (the series is declared in `core/metrics.py:278`).
- [ ] **Step 2: Docs edits.**
- [ ] **Step 3: Agent check** — `cd agent && pnpm test -- coverage` → PASS.
- [ ] **Step 4: Commit**

```bash
git add deploy/grafana docs CLAUDE.md
git commit -m "docs(slicing): dashboard panel, layout notes and the operating guide for the native slicer"
```

---

## Self-review notes

- **Spec coverage.** §5.1 boundary → Tasks 5, 6. §5.2 activity, queue, `TransferUrl` inputs and PUT outputs, no credentials or database in the worker, the env-only configuration, heartbeats, `SlicerError`, 192 non-retryable, process-group kill, 60 s heartbeat timeout, concurrency `cpus/2` → Tasks 1, 3, 6, 7, 10, 11. §5.2's internal blob routes and presigned S3 → Task 10 (the `presign_*` pair consumed from the S3 plan, not defined). §5.3 presets as files, flattening, overrides, `local:` via Bambuddy, `preset_key` with slicer and version → Task 8 (the profiles reach the API image by `COPY --from=slicer`, my call, noted there). §5.4 `slicer_backend`, `SliceWorkflow`, `native-slicer` patch, `output_bambuddy_slices` column, preview switch, progress → Tasks 11–14 (the realtime topic deferred, Task 14 note). §5.5 failure surface → Tasks 6, 12, 13. §7.1 workspace layout (`sb-transfer`, `sb-temporal`, `sb-slicer`, `bins/scadbuddy-slicer`; `sb-render` and `scadbuddy-render-tools` named, not built), vectors, OTel, the stage histogram → Tasks 1–4, 6. §7.2 activities only, `task_queue=` behind a patch, no queue renamed → Tasks 7, 11, 13. §7.3 is the render-tools plan's: nothing here is a render activity or queue. §7.4 one-activity workflow until 1.31 → Task 11.
- **Types.** `TransferUrl`, `SlicePlateInput/Output`, `SliceProgress` (Task 1) are the names in Tasks 2, 3, 6, 10, 11; `PendingPut`, `Transfers.mint_get/mint_put/finish_put` (Task 10) in Task 11; `PresetFiles`, `build_presets`, `native_preset_key` (Task 8) in Tasks 11–13; `NativeSliceStore` methods in Tasks 11–13; `SliceWorkflowInput` (Task 11) in Task 13.
- **Review Focus.** 1–3 → Task 6; 4 → Task 8; 5 → Task 10.
- **Proportion.** Fifteen tasks against a four-section spec slice; code blocks are tests and signatures, bodies only where the argv, the token format and the fake CLI's protocol are fixed by the spec.
- **Notes to the parent.** (a) The Bambuddy print path still needs the sliced file in Bambuddy's library (`print_sliced_upload`, Task 13), so a native slice on a Bambuddy-printed job costs one upload; tier 3's native print path removes it. (b) Resolved by the spec change: the worker reaches every store through URLs the API mints. (c) `SlicePreview.slots[].side` is `None` on the native slicer: the side comes from Bambuddy's `filament-requirements` today; `extruders.py`'s own nozzle plan can fill it in a follow-up. (d) `transfer_token.py` derives its key from the KEK (`SCADBUDDY_SECRET_KEY_FILE`), which flows already require on prod; a deployment without it cannot use the native slicer on a non-S3 store, and `build_store`-style readiness says so at start.
