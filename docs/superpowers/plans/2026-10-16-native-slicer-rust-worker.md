# Native Slicer on a Rust Temporal Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A slice is a ScadBuddy activity: a Rust worker runs the Bambu Studio CLI with ScadBuddy's own preset files, reports progress as heartbeats, and the print dialog and `PrintRunWorkflow` use it when `slicer_backend` is `native`, with Bambuddy's slicer still selectable.

**Architecture:** A new Rust workspace `workers/rust/` holds the first Rust worker, `scadbuddy-slicer`, on task queue `slicer`. It serves one activity, `slice_plate`, whose input carries transfer URLs the Python caller mints (the API's internal blob routes on every store backend, or a presigned S3 GET; Ruling C3), never bytes, store keys or credentials. The Python side resolves presets into files (`backend/scadbuddy/slicing/`), mints the URLs and records the result in the store (`slicing/transfer.py`), and wraps the activity in a one-activity `SliceWorkflow` on the `library` queue (the API process) until Temporal 1.31's standalone activities; the print dialog and `PrintRunWorkflow` share one execution per copy and presets (Rulings C1, C2). Payload shapes are pinned across the two languages by a JSON vector file, as the agent and agent-durable pin theirs.

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
Items 1–3 shipped with Task 6. For Tasks 8–15:

4. **A preset whose `inherits` chain loops or names a missing parent.** Bambu Studio's profiles are hand-maintained; flattening must refuse a loop and a missing parent with the preset's name in the error, not recurse forever. Test in Task 8.
5. **A transfer token for one blob opening another, probing for keys, or outliving its two hours.** A token minted for a GET of key A must not PUT, must not read key B, and must be refused after `expires_at`; a missing key answers 404 only to a valid token for it, 403 to any other. Test in Task 10.
6. **The dialog and the run asking for the same plate at once.** A person opens the dialog (its background slice starts), then presses Print before it finishes: one slice must run, not two, and both must read its result. Test in Task 13.
7. **A slice that waits in the queue longer than its URLs live.** With one slicer pod busy, a slice can start after its two-hour URLs expire; the worker refuses them (`TransferRefused`), and the slice must be prepared again with fresh URLs once, not fail. Test in Task 11.
8. **A native preview whose id equals a Bambuddy preview's.** Both id spaces start at 1; a read or a toolpath of native job 3 must never answer Bambuddy's job 3, nor the reverse, even after `slicer_backend` changes while the dialog is open. Test in Task 12.

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

### Task 8: Presets as files (`slicing/profiles.py`, `slicing/presets.py`)

**Files:**
- Create: `backend/scadbuddy/slicing/profiles.py`, `backend/scadbuddy/slicing/presets.py`
- Modify: `Dockerfile`: split a `slicer-dist` stage out of `slicer` (the download, the sha256 check and the extraction only), and copy the profiles into `app` from it (Step 4)
- Modify: `backend/scadbuddy/core/settings.py`: `slicer_profiles_dir: Path = Path("/opt/slicer-profiles")` beside `api_internal_url`, with a `BOOTSTRAP_FIELDS` entry ("Where the image puts Bambu Studio's BBL profiles and their VERSION, copied from the slicer's AppImage at build time: not a setting.")
- Modify: `backend/pyproject.toml` `markers`: `"requires_slicer_profiles: needs Bambu Studio's BBL profiles under SCADBUDDY_SLICER_PROFILES_DIR (the app and test images ship them at /opt/slicer-profiles)"`
- Test: `backend/tests/slicing/test_presets.py`, `backend/tests/slicing/conftest.py` (fixture `profiles`), fixtures `backend/tests/slicing/profiles/VERSION` (`02.08.02.61`) and `backend/tests/slicing/profiles/BBL/{BBL.json,machine,process,filament}/…` (a three-level `inherits` chain, a loop `loop_a`↔`loop_b`, an `orphan` naming a missing parent; small JSON files)

**Interfaces:**
- Consumes: `SlicePlan` (`bambuddy/dispatch.py:61`), `PresetRef` (`bambuddy/models.py:34`), `ProcessValue` (`bambuddy/models.py:43`), `slice_request` (`bambuddy/dispatch.py:219`) and its `SliceRequest.preset_key` (`bambuddy/models.py:739`), `BambuddyClient.preset_values` (`bambuddy/client.py:490`, process slot only), `read_catalogue(client).names()` (`bambuddy/catalogue.py`, `_Catalogue.names`).
- Produces:
  ```python
  # profiles.py
  class ProfileError(ValueError): ...     # a missing preset, a missing parent, an inherits loop, a preset the native slicer cannot read; the message names it
  Flat = dict[str, str | list[str]]
  def flatten(name: str, load: Callable[[str], dict[str, Any] | None]) -> Flat
      # follows "inherits" to the root, child over parent; drops "inherits"; refuses a loop and a missing parent
  class Profiles:
      def __init__(self, root: Path) -> None          # root holds BBL/ and VERSION
      @property
      def version(self) -> str                        # root/VERSION, stripped: the image's BAMBU_STUDIO_VERSION (Ruling C7)
      @property
      def profile_version(self) -> str                # BBL/BBL.json "version": a check only, never a key
      def machine(self, ref_or_name: str) -> Flat
      def process(self, ref_or_name: str) -> Flat
      def filament(self, ref_or_name: str) -> Flat     # each looks up by setting_id, then by name
      @classmethod
      def installed(cls, root: Path) -> Profiles | None   # None when root/BBL/BBL.json is absent (a dev run outside the image)

  # presets.py
  class PresetFiles(BaseModel):                       # never crosses a workflow history (Task 11 puts each as a blob)
      machine: bytes; process: bytes; filaments: list[bytes]     # JSON documents
  def apply_overrides(process: Flat, overrides: Mapping[str, ProcessValue]) -> Flat
      # a str over a str; a list over a list position by position (#717); a str over a list sets every position
  async def build_presets(plan: SlicePlan, profiles: Profiles, client: BambuddyClient | None) -> PresetFiles
  def native_preset_key(plan: SlicePlan, plate_id: int, version: str) -> str
      # f"native@{version}/" + slice_request(plan, plate_id).preset_key
  ```

Resolution rules, the decisions the implementer cannot make alone:
- A `standard` or `cloud` ref resolves in `Profiles` by `setting_id == ref.id`, then, when that misses and `client` is given, by the name `read_catalogue(client).names()[(ref.source, ref.id)]`. Verify first which of the two Bambuddy's ids are (compare a few `standard`/`cloud` rows under `backend/tests/bambuddy/recordings/` with the image's profile files) and record the answer, with how it was measured, in `profiles.py`'s docstring.
- A `local` process ref takes `client.preset_values(ref).values` (`resolved` false is a `ProfileError` carrying its `reason`). A `local` printer or filament ref, an `orca_cloud` ref and a cloud *user* preset with no BBL file are a `ProfileError` ("the native slicer cannot read <source> <kind> preset <name>: Bambuddy serves only process preset values"): `GET /slicer/preset-values?slot=filament` answers `400 "Only the 'process' slot is supported"` (measured 2026-09-27, `docs/superpowers/plans/2026-09-27-spool-first-print.md`). Re-measure it against the live Bambuddy before building on it; if it still refuses, Task 15 records the gap in spec §5.3.
- Bambu Studio's `inherits` has list rules beyond child-over-parent (spec §9's open decision): read `PresetBundle::load_vendor_configs_from_json` in the v02.08.02.61 source, record the rules in `profiles.py`'s docstring with the source link, and add a fixture and a test for each rule that changes a flattened value.
- `plan.bed_type` must reach the slice. Verify first, in the slicer image, how the v02.08.02.61 CLI takes the plate type (`docker run --rm scadbuddy-slicer:dev /opt/bambu-studio/AppRun --help | grep -i bed`). If the process JSON's `curr_bed_type` carries it, `build_presets` sets it there; if only a CLI flag does, stop and report to the controller: Task 6's argv and the vectors would change.
- `plan.filament_colours` are not written into the filament files: the laid-out 3MF carries the colours.

- [ ] **Step 1: Write the failing tests**

```python
def test_flatten_child_wins_and_drops_inherits(profiles):            # chain: leaf -> mid -> root
    out = profiles.process("leaf")
    assert out["layer_height"] == "0.16" and out["wall_loops"] == "3" and "inherits" not in out

def test_flatten_refuses_a_loop(profiles):
    with pytest.raises(ProfileError, match="loop.*loop_a"): profiles.process("loop_a")

def test_flatten_refuses_a_missing_parent(profiles):
    with pytest.raises(ProfileError, match="orphan.*no such"): profiles.process("orphan")

def test_a_ref_resolves_by_setting_id(profiles):                      # the fixture's leaf has "setting_id": "GP_LEAF"
    assert profiles.process("GP_LEAF") == profiles.process("leaf")

def test_version_is_the_images_not_the_profiles(profiles):
    assert profiles.version == "02.08.02.61" and profiles.profile_version != profiles.version

def test_installed_is_none_without_profiles(tmp_path):
    assert Profiles.installed(tmp_path) is None

def test_apply_overrides_per_extruder():
    assert apply_overrides({"outer_wall_speed": ["200", "200"]}, {"outer_wall_speed": ["150", "200"]}) == {"outer_wall_speed": ["150", "200"]}
    assert apply_overrides({"outer_wall_speed": ["200", "200"]}, {"outer_wall_speed": "150"}) == {"outer_wall_speed": ["150", "150"]}

async def test_build_presets_gives_one_file_per_slot(profiles, plan):   # plan: standard refs resolving in the fixture
    files = await build_presets(plan, profiles, None)
    assert len(files.filaments) == 2 and json.loads(files.process)["layer_height"] == "0.16"

async def test_a_local_filament_is_refused_by_name(profiles, plan):
    local = plan.model_copy(update={"filament_presets": [PresetRef(source="local", id="7")]})
    with pytest.raises(ProfileError, match="local filament preset"):
        await build_presets(local, profiles, None)

def test_native_preset_key_is_distinct_from_bambuddys(plan):
    assert native_preset_key(plan, 1, "02.08.02.61") == "native@02.08.02.61/" + slice_request(plan, 1).preset_key

@pytest.mark.requires_slicer_profiles
def test_the_images_h2c_machine_flattens():
    profiles = Profiles.installed(Path(os.environ.get("SCADBUDDY_SLICER_PROFILES_DIR", "/opt/slicer-profiles")))
    assert profiles is not None and "printable_height" in profiles.machine("Bambu Lab H2C 0.4 nozzle")
```

The `requires_slicer_profiles` test skips, saying why, unless `<slicer_profiles_dir>/BBL/BBL.json` exists (register the skip where the other `requires_*` markers are decided, `backend/tests/conftest.py`).

- [ ] **Step 2: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_presets.py -v` → import errors.

- [ ] **Step 3: Implement `profiles.py` and `presets.py`.** `Profiles` indexes `BBL/{machine,process,filament}/*.json` by `name` and `setting_id` once, lazily, off the event loop (`asyncio.to_thread` at the async callers).

- [ ] **Step 4: Split `slicer-dist` and copy the profiles into `app`**

`slicer-dist`: `FROM` the same pinned `ubuntu:24.04@sha256:68ee2200…` as `slicer`; installs only `ca-certificates curl`; takes the `TARGETARCH`, `BAMBU_STUDIO_*` ARGs and the download/check/extract `RUN` from `slicer` as they are, and also writes `printf '%s\n' "$BAMBU_STUDIO_VERSION" > /opt/bambu-studio/VERSION`. `slicer` keeps its runtime libraries and everything after the extraction, with `COPY --from=slicer-dist /opt/bambu-studio /opt/bambu-studio` in place of the `RUN`. `app` gets `COPY --from=slicer-dist /opt/bambu-studio/resources/profiles/BBL /opt/slicer-profiles/BBL` (the real directory, not the `profiles` symlink) and `COPY --from=slicer-dist /opt/bambu-studio/VERSION /opt/slicer-profiles/VERSION`. Move the comment about the pin to `slicer-dist`.

Run: `docker build --target app --progress=plain -t scadbuddy:app . 2>&1 | grep -c 'cargo build'` → `0` (the `app` and `test` builds, CI's backend shards included, never compile the worker), then `docker build --target slicer -t scadbuddy-slicer:dev . && docker run --rm scadbuddy-slicer:dev scadbuddy-slicer --check-cli` → passes, and `docker run --rm scadbuddy:app cat /opt/slicer-profiles/VERSION` → `02.08.02.61`. `hadolint --config .hadolint.yaml Dockerfile` → clean.

- [ ] **Step 5: Run and commit**

Run: `cd backend && uv run --frozen pytest tests/slicing -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS (the image-only test skipped locally, with its reason). Then in the image: `docker build --target test -t scadbuddy:test . && docker run --rm scadbuddy:test uv run --frozen pytest tests/slicing/test_presets.py -v` → the `requires_slicer_profiles` test passes.

```bash
git add backend/scadbuddy/slicing backend/tests/slicing backend/scadbuddy/core/settings.py backend/pyproject.toml backend/tests/conftest.py Dockerfile
git commit -m "feat(slicing): presets as flattened Bambu Studio profile files with ScadBuddy's overrides"
```

---

### Task 9: Reading a sliced 3MF (`slicing/slice_info.py`)

**Files:**
- Create: `backend/scadbuddy/slicing/slice_info.py`
- Create: `backend/tests/support/sliced3mf.py` (the fixture writer Tasks 11–13 reuse)
- Modify: `backend/scadbuddy/bambuddy/preview.py`: `_LOAD` (:57) and `MAX_GCODE_BYTES` (:54) move to `slice_info.py`; `_plate_gcode` (:349) keeps its name and signature (`tests/api/test_print_preview.py` monkeypatches it) and keeps the download and `MAX_SLICED_BYTES` (:52), its zip part becomes `slice_info.plate_gcode`; `_filament_changes` (:341) calls `slice_info.filament_changes`. `read_toolpath` is unchanged.
- Test: `backend/tests/slicing/test_slice_info.py`

**Interfaces:**
- Consumes: `read_filaments(payload, sliced=True)` (`bambuddy/filaments3mf.py:405`): its `LocalFilaments.sliced` (grams per slot per plate, `_sliced` :268) and `.profile.plate(i).print_seconds` (`SlicedPlate` :108, `prediction`). Extend nothing there; do not parse `slice_info.config` a second time.
- Produces (`slice_info.py` imports nothing from `bambuddy/preview.py`, which imports it):
  ```python
  MAX_GCODE_BYTES = 256 * 1024 * 1024
  def plate_gcode(payload: bytes, plate_id: int) -> bytes | None
      # Metadata/plate_<id>.gcode, else the only plate's; None for none, a bad zip, or past MAX_GCODE_BYTES (moved from preview.py)
  def filament_changes(gcode: bytes) -> int        # M620 S<n>A loads, 255 excluded, minus one, never below 0
  class NativeSliceRead(BaseModel):                # not `SlicedPlate`: filaments3mf.py has one
      plate_id: int
      print_time_seconds: int | None
      grams_by_slot: dict[int, float]
      filament_used_g: float | None                # the slots' sum, rounded to 2
      filament_changes: int | None
  def read_sliced(payload: bytes, plate_id: int) -> NativeSliceRead
      # the only plate when the file has one; ApiError 404 for a plate a file of several lacks;
      # an UnreadableFilamentsError reads as every field None and no grams
  def sliced_3mf(*, plates: Mapping[int, tuple[int, list[float]]], gcode: Mapping[int, bytes]) -> bytes   # tests/support/sliced3mf.py
      # plate -> (prediction seconds, used_g per slot from 1); a minimal Metadata/project_settings.config of "{}" and whatever else read_filaments(sliced=True) needs
  ```

- [ ] **Step 1: Write the failing tests**

```python
ONE = sliced_3mf(plates={1: (3600, [12.5, 3.0])}, gcode={1: b"M620 S0A\nM620 S1A\nM620 S0A\n"})

def test_read_sliced_reads_time_grams_and_changes():
    read = read_sliced(ONE, 1)
    assert (read.print_time_seconds, read.grams_by_slot, read.filament_used_g, read.filament_changes) == (3600, {1: 12.5, 2: 3.0}, 15.5, 2)

def test_one_plate_is_read_whatever_its_number():          # a whole-file slice (plan.whole_file) names plate 0
    assert read_sliced(ONE, 2).print_time_seconds == 3600

def test_a_plate_a_file_of_several_lacks_is_404():
    two = sliced_3mf(plates={1: (60, [1.0]), 2: (60, [1.0])}, gcode={1: b"", 2: b""})
    with pytest.raises(ApiError) as missing: read_sliced(two, 3)
    assert missing.value.status_code == 404

def test_unloads_are_not_changes():
    assert filament_changes(b"M620 S255A\nM620 S0A\nM620 S1A\n") == 1

def test_an_unreadable_file_reads_as_unknown():
    read = read_sliced(b"not a zip", 1)
    assert read.print_time_seconds is None and read.grams_by_slot == {} and read.filament_changes is None
```

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_slice_info.py -v` → import error.
- [ ] **Step 3: Implement** `slice_info.py`, `sliced3mf.py`, and the `preview.py` move.
- [ ] **Step 4: Run the slicing and preview tests** — `cd backend && uv run --frozen pytest tests/slicing tests/api/test_print_preview.py -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .` → PASS (the preview tests unchanged; they skip without Postgres and Temporal, so run them with both).
- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/slicing/slice_info.py backend/scadbuddy/bambuddy/preview.py backend/tests/slicing/test_slice_info.py backend/tests/support/sliced3mf.py
git commit -m "feat(slicing): read time, grams and filament changes from a sliced 3MF"
```

---

### Task 10: Transfer URLs and the internal blob routes (`slicing/transfer.py`)

Ruling C3: the internal routes serve every store backend, and the only presigned URL is an optional `S3ContentBackend.presign_get` of an object that already exists. Presigned PUT (and adopting its `pending/` object) is a follow-up issue, filed in Task 15: a presigned PUT to a key `sweep_unrecorded` does not write is never swept (`store/s3.py:192`).

**Files:**
- Create: `backend/scadbuddy/slicing/transfer_token.py`, `backend/scadbuddy/slicing/transfer.py`, `backend/scadbuddy/api/internal_blobs.py` (its own module with a `router`: `main._api_router` (`main.py:180`) mounts the one attribute named `router` per module)
- Create: a NEW migration `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_transfer_puts.sql`, named by `date -u` when you create it (CLAUDE.md "Migrations"; edit nothing else there)
- Modify: `backend/scadbuddy/store/s3.py` (`S3ContentBackend.presign_get`), `backend/scadbuddy/core/settings.py` (`BOOTSTRAP_FIELDS["api_internal_url"]`: also the base the API mints the slicer's transfer URLs under)
- Test: `backend/tests/slicing/test_transfer_token.py`, `backend/tests/api/test_internal_blobs.py` (`requires_postgres`; the api `settings` on moto's S3 through `tests/support/store.py` `s3`, with a KEK file written to `tmp_path` and `api_internal_url="http://scadbuddy"`)

**Interfaces:**
- Consumes: `ContentStore.put` (`store/content.py:175`; keys are `f"{kind}-{sha256}"`, :186), `ContentStore.stat(key) -> BlobStat | None` (:431), `ContentStore.get(ref: BlobRef)` (:318, takes a ref: `stat(key).ref`), `ContentStore.spool`, `load_kek` (`core/secrets.py:83`), HKDF as `bambuddy/catalogue.py` `catalogue_name_key` derives (`cryptography` is a dependency).
- Produces:
  ```python
  # transfer_token.py
  TRANSFER_KEY_INFO = b"scadbuddy:slicing:transfer:v1"
  def transfer_key(kek: Kek) -> bytes                         # HKDF-SHA256, 32 bytes, salt None
  class TransferToken(BaseModel): method: Literal["GET", "PUT"]; blob_key: str; expires_at: datetime
  class TokenError(ValueError): ...
  def sign(secret: bytes, token: TransferToken) -> str
  def verify(secret: bytes, value: str, *, method: str, blob_key: str, now: datetime) -> TransferToken
  # the format, fixed: urlsafe_b64(f"{method}\n{blob_key}\n{int(expires_at.timestamp())}") + "." + urlsafe_b64(HMAC-SHA256),
  # compared with hmac.compare_digest; refuses another method, another key, a tampered mac, a past expiry

  # transfer.py
  TRANSFER_TTL = timedelta(hours=2)
  TRANSFER_HEADER = "X-ScadBuddy-Transfer"
  INTERNAL_BLOBS = "/api/v1/internal/blobs"
  PENDING_PREFIX = "pending-"
  class TransferUnavailableError(RuntimeError): ...           # no api_internal_url or no KEK: says which variable
  class TransferRecordError(RuntimeError): ...                # finish_put on nothing PUT, or a sha/size the PUT did not have
  class PendingPut(BaseModel): url: TransferUrl; pending_key: str          # pending_key = PENDING_PREFIX + uuid4().hex
  class TransferPuts:                                          # the transfer_puts table, over a pool
      def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None
      def record(self, pending_key: str, *, key: str, sha256: str, size: int) -> None   # upsert: a retried attempt PUTs again
      def get(self, pending_key: str) -> TransferPut | None
      def prune(self, older_than: timedelta) -> int
  class Transfers:
      def __init__(self, content: ContentStore, puts: TransferPuts, *, internal_base_url: str | None, secret: bytes | None,
                   now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None
      async def mint_get(self, key: str) -> TransferUrl
          # BlobMissingError for a key stat() does not find; on an S3ContentBackend, the presigned GET of stat.ref.backend_id
          # (headers {}); else f"{internal_base_url}{INTERNAL_BLOBS}/{key}" with {TRANSFER_HEADER: sign(GET, key)}; expires now+TTL
      def mint_put(self) -> PendingPut                         # the internal PUT route of a new pending key, on every backend
      async def finish_put(self, put: PendingPut, *, sha256: str, size: int) -> str     # the stored key
  # store/s3.py
  async def presign_get(self, backend_id: str, expires: timedelta) -> str     # S3ContentBackend
  ```
  ```sql
  CREATE TABLE transfer_puts (
    pending_key text PRIMARY KEY,
    key text NOT NULL,
    sha256 text NOT NULL,
    size bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
  );
  CREATE INDEX transfer_puts_created ON transfer_puts (created_at);
  ```
  Routes (`api/internal_blobs.py`, `router = APIRouter(prefix=INTERNAL_BLOBS.removeprefix("/api/v1"), include_in_schema=False)`, as `api/internal_library.py:18`):
  - `GET /{key:path}`: `verify(..., method="GET", blob_key=key)` else 403; then `stat(key)` None → 404; else a `StreamingResponse` of `content.get(stat.ref)` with `Content-Length: stat.ref.size`, `application/octet-stream`.
  - `PUT /{key:path}`: `verify(..., method="PUT")` else 403; a key without `PENDING_PREFIX` → 404; the body streamed (`request.stream()`) into a temp file under `content.spool`, never into memory; `content.put("slice", file, name="sliced.3mf", scope=BlobScope(folder="work"))`; `TransferPuts.record(key, key=f"slice-{ref.sha256}", sha256=ref.sha256, size=ref.size)`; 204.
  - A missing, malformed, tampered or expired token is 403 (nothing to challenge, so not 401), and the token is checked before the key, so a token never probes a key it was not minted for. Neither route ever redirects: the Rust side treats a 3xx as `TransferRefused` (`sb-transfer` follows no redirects).
  - Without a content store (`store_backend=local`, `store/factory.py:84`) both answer 404: the native slicer needs `store_backend` `s3` or `bambuddy` (Task 15 records it in spec §5.2).
  - `presign_get`: aiobotocore's `generate_presigned_url("get_object", Params={"Bucket", "Key": self._key(backend_id)}, ExpiresIn=…)`; verify in the pinned aiobotocore (`backend/uv.lock`) that it is a coroutine before awaiting it.

- [ ] **Step 1: Write the failing token tests** (`test_transfer_token.py`, `AT = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)`, `SECRET = transfer_key(kek_from_base64(base64.b64encode(bytes(32)).decode()))`)

```python
def test_token_round_trips():
    t = TransferToken(method="GET", blob_key="slice-abc", expires_at=AT)
    assert verify(SECRET, sign(SECRET, t), method="GET", blob_key="slice-abc", now=AT - timedelta(minutes=1)) == t

@pytest.mark.parametrize("method,blob_key,now", [("PUT", "slice-abc", AT - timedelta(minutes=1)),
                                                 ("GET", "slice-other", AT - timedelta(minutes=1)),
                                                 ("GET", "slice-abc", AT + timedelta(seconds=1))])
def test_token_refuses_other_method_key_or_expiry(method, blob_key, now):
    with pytest.raises(TokenError):
        verify(SECRET, sign(SECRET, TransferToken(method="GET", blob_key="slice-abc", expires_at=AT)), method=method, blob_key=blob_key, now=now)

def test_tampered_mac_is_refused():          # flip one character of the part after "."
def test_another_kek_is_refused():           # signed under transfer_key(another KEK)
```

- [ ] **Step 2: Write the failing route and `Transfers` tests** (`test_internal_blobs.py`, through the TestClient, `follow_redirects=False` on every request):
  - `test_a_minted_get_reads_the_blob`: `content.put("slice", b"hello", …)` → `mint_get(key)` → GET the URL's path with its headers → 200, body `b"hello"`, `Content-Length: 5`.
  - `test_get_without_the_header_is_403`, `test_a_get_token_cannot_put` (PUT with a GET token → 403).
  - `test_a_token_for_key_a_cannot_read_key_b` (Review Focus 5): A's token on B's URL → 403.
  - `test_a_missing_key_is_404_only_after_the_token` (Review Focus 5): a token minted for missing key C → 404; a bad token on C → 403.
  - `test_an_expired_url_is_403`: `Transfers(now=lambda: AT - TRANSFER_TTL - timedelta(seconds=1))` mints; the request at real time → 403.
  - `test_put_then_finish_put`: `mint_put()`, PUT 5 bytes → 204 (not a 3xx); `finish_put(sha256(b"hello"), 5)` → `"slice-<sha>"`, and `content.read(content.stat(key).ref) == b"hello"`.
  - `test_a_second_put_to_the_same_pending_key_upserts`: PUT `b"one"` then `b"two"` to one `PendingPut` → both 204; `finish_put(sha256(b"two"), 3)` succeeds and `finish_put(sha256(b"one"), 3)` raises `TransferRecordError`.
  - `test_finish_put_refuses_a_wrong_digest_or_nothing_put`.
  - `test_put_to_a_non_pending_key_is_404` (a valid PUT token for `slice-abc`).
  - `test_mint_without_internal_url_or_kek_says_which`: `TransferUnavailableError` matching `SCADBUDDY_API_INTERNAL_URL`, then `SCADBUDDY_SECRET_KEY_FILE`.
  - `test_s3_presigned_get_reads_the_object`: `mint_get` on the moto store gives a URL with no headers whose `httpx.get` returns the bytes.
  - `test_transfer_puts_prune`: a row backdated two days is pruned by `prune(timedelta(days=1))`, a fresh one kept.

- [ ] **Step 3: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_transfer_token.py tests/api/test_internal_blobs.py -v` → import errors.

- [ ] **Step 4: Implement** the migration (a NEW file, `$(date -u +%Y%m%dT%H%MZ)_transfer_puts.sql`), `transfer_token.py`, `transfer.py`, `api/internal_blobs.py`, `presign_get`.

- [ ] **Step 5: Run, lint, commit**

Run: `cd backend && uv run --frozen pytest tests/slicing tests/api/test_internal_blobs.py tests/api/test_routes.py -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS (`test_routes.py`: no two routes match one request)

```bash
git add backend/scadbuddy/slicing backend/scadbuddy/api/internal_blobs.py backend/scadbuddy/migrations backend/scadbuddy/store/s3.py backend/scadbuddy/core/settings.py backend/tests
git commit -m "feat(slicing): signed internal blob routes and transfer URLs for the slicer worker"
```

---

### Task 11: `slicer_backend`, `native_slices` and `SliceWorkflow` on the `library` queue

Rulings C1, C2, C4, C5. `SliceWorkflow` runs on the `library` queue (the API process: content store, Bambuddy client, native library). Its first activity reads the laid-out copy and builds the presets, so neither bytes nor presets ever enter a history: the workflow's input is the copy's library file id, the `SlicePlan` (refs and overrides) and the plate.

**Files:**
- Create: a NEW migration `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_native_slices.sql`, named by `date -u` when you create it
- Create: `backend/scadbuddy/slicing/store.py`, `backend/scadbuddy/slicing/component.py`, `backend/scadbuddy/slicing/start.py`, `backend/scadbuddy/workflows/slicing.py`, `backend/scadbuddy/workflows/slicing_activities.py`, `backend/scadbuddy/workflows/slicing_models.py`
- Modify: `backend/scadbuddy/core/config.py` (`Slicer = Literal["bambuddy", "native"]` beside `StoreBackend` :92 and `LibraryBackend` :96)
- Modify: `backend/scadbuddy/core/settings.py`: `slicer_backend: Slicer = "bambuddy"` after `library_backend` (:151); `temporal_task_queue_slicer: str = "slicer"` after `temporal_task_queue_agent_tools` (:277), added to `_temporal_without_whitespace`'s field list (:346-350) and to `BOOTSTRAP_FIELDS` beside the other queues ("Paired with the Temporal address: the API starts slices on it, and the scadbuddy-slicer worker must name the same queue."); `APPLIES["slicer_backend"] = "live"`
- Modify: `backend/scadbuddy/library/settings_store.py` (`StoredSettings.slicer_backend` beside `library_backend` :304, `SettingsUpdate.slicer_backend: Slicer | None` beside :557), `backend/scadbuddy/api/settings.py` (the response field beside `library_backend` :114 and its fill beside :356)
- Modify: `backend/scadbuddy/slicing/models.py`: `TRANSFER_REFUSED = "TransferRefused"` beside `SLICER_ERROR`
- Modify: `backend/scadbuddy/main.py` `_run_library_worker` (:1091): the slicing activities into `activities` (:1117-1123) and `SliceWorkflow` into `workflows=` (:1129)
- Modify: `backend/tests/api/conftest.py`: `"temporal_task_queue_slicer": f"{queue}-slicer"` beside :140
- Test: `backend/tests/slicing/test_store.py` (`requires_postgres`), `backend/tests/api/test_slice_workflow.py` (`requires_temporal`, `requires_postgres`), `backend/tests/support/fake_slicer.py`, `backend/tests/test_slice_replay.py`, `backend/tests/fixtures/slice_workflow_histories/` (new)

**Interfaces:**
- Consumes: Task 8 (`Profiles.installed`, `Profiles.version`, `build_presets`, `native_preset_key`), Task 9 (`sliced_3mf`, in the tests), Task 10 (`Transfers.mint_get/mint_put/finish_put`, `PendingPut`, `TransferUnavailableError`), Task 1 (`SlicePlateInput`, `SlicePlateOutput`, `SliceProgress`, `SLICER_ERROR`, `VALIDATION_EXIT`), `served_reads` (`print_library/reads.py:55`), `PrintLibrary.serving_native` (`print_library/component.py:76`), `BlobRefs.add/drop_holder` (`store/refs.py:21,29`), `ContentStore.touch` (`store/content.py:444`).
- Produces:
  ```sql
  CREATE TABLE native_slices (
    id bigserial PRIMARY KEY,
    workflow_id text NOT NULL UNIQUE,            -- native_workflow_id(library_file_id, preset_key)
    subject text NOT NULL,                       -- the starter's run subject, as print_preview_slices.subject
    library_file_id bigint NOT NULL,             -- the laid-out copy (Bambuddy's or the native library's)
    plate_id integer NOT NULL,
    preset_key text NOT NULL,                    -- native_preset_key(...)
    input_sha256 text,                           -- set by slice_prepare
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    failure text,
    sliced_blob_key text,
    warnings jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
  );
  CREATE INDEX native_slices_reuse ON native_slices (input_sha256, preset_key) WHERE status = 'completed';
  ```
  ```python
  # slicing/store.py
  HOLDER = "native_slice"                                      # blob_refs holder kind; holder id str(row.id)
  class NativeSlice(BaseModel): id: int; workflow_id: str; subject: str; library_file_id: int; plate_id: int; preset_key: str
      input_sha256: str | None; status: Literal["running", "completed", "failed"]; failure: str | None
      sliced_blob_key: str | None; warnings: list[str]; created_at: datetime; finished_at: datetime | None
  class NativeSliceStore:                                      # over the settings pool; every method async (asyncio.to_thread)
      def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None
      async def start(self, *, workflow_id: str, subject: str, library_file_id: int, plate_id: int, preset_key: str) -> NativeSlice
          # insert, or the row of that workflow_id; a failed one is set back to running (failure, finished_at cleared)
      async def prepared(self, slice_id: int, input_sha256: str) -> None
      async def finish(self, slice_id: int, *, status: Literal["completed", "failed"], sliced_blob_key: str | None,
                       failure: str | None, warnings: list[str]) -> None
      async def get(self, slice_id: int, subject: str) -> NativeSlice | None
      async def reusable(self, input_sha256: str, preset_key: str) -> NativeSlice | None     # completed, newest first
      async def sweep(self, older_than: timedelta) -> list[int]                           # finished rows past it: deleted, ids returned
  def native_workflow_id(library_file_id: int, preset_key: str) -> str
      # f"slice-{library_file_id}-{sha256(preset_key)[:32]}": known before the bytes are read, so the dialog and the run
      # name one execution (Ruling C2); reuse across copies of identical bytes is slice_prepare's (Ruling C1)

  # slicing/component.py: a Component (core/components.py), discovered; nothing is built until first use, as PRINT_LIBRARY
  class Slicing:
      store: NativeSliceStore; puts: TransferPuts; refs: BlobRefs
      profiles: Profiles | None                                # Profiles.installed(settings.slicer_profiles_dir)
      starter: NativeSliceStarter | None                       # native_slice_starter(settings)
      def transfers(self) -> Transfers                         # RuntimeError without a content store (store_backend=local)
      def temporal(self) -> Client                             # AppState.temporal (main.py:940)
  SLICING: Key[Slicing]

  # slicing/start.py: what preview_run (bambuddy queue) and slice_run (library queue) both call
  class NativeSliceStarter(BaseModel):
      library_queue: str; slicer_queue: str                   # Settings.temporal_task_queue_library / _slicer
      version: str                                            # Profiles.version: the image's BAMBU_STUDIO_VERSION
  def native_slice_starter(settings: Settings) -> NativeSliceStarter | None
      # None when Profiles.installed(settings.slicer_profiles_dir) finds no profiles (a run outside the image)
  async def start_native_slice(temporal: Client, store: NativeSliceStore, starter: NativeSliceStarter, *, library_file_id: int,
                               plan: SlicePlan, plate_id: int, subject: str) -> NativeSlice
      # preset_key = native_preset_key(plan, plate_id, starter.version); row = store.start(...); unless row.status == "completed":
      # temporal.start_workflow(SliceWorkflow.run, SliceWorkflowInput(...), id=row.workflow_id, task_queue=starter.library_queue,
      #   id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
      # (the pattern of workflows/commands.py `start_command`); returns the row
  async def slice_progress(temporal: Client, workflow_id: str) -> SliceProgress | None
      # describe().raw_description.pending_activities, the one whose activity_type.name == "slice_plate" (during slice_prepare
      # another is first); its last heartbeat_details decoded with temporal.data_converter as SliceProgress; None when there is none.
      # The first beat is all zeros (Rust's SliceProgress::default), so a running slice reads total_percent 0, not None.
      # Verify the describe and payload-decoding calls in the pinned temporalio (1.34.0, backend/uv.lock) before relying on them.

  # workflows/slicing_models.py
  class SliceWorkflowInput(BaseModel): slice_id: int; library_file_id: int; plan: SlicePlan; plate_id: int; preset_key: str
                                       slicer_queue: str
  class SlicePrepared(BaseModel): plate_input: SlicePlateInput | None; put: PendingPut | None
                                  reused_blob_key: str | None     # a completed slice of the same bytes and presets: nothing to slice
  class SliceRecordInput(BaseModel): slice_id: int; put: PendingPut | None; output: SlicePlateOutput | None
                                     reused_blob_key: str | None; failure: str | None
  class SliceWorkflowResult(BaseModel): status: Literal["completed", "failed"]; sliced_blob_key: str | None
                                        failure: str | None; warnings: list[str]

  # workflows/slicing.py
  SLICE_WORKFLOW = "SliceWorkflow"
  SLICE_TIMEOUT = timedelta(minutes=30)                        # one slice_plate attempt's start-to-close
  SLICE_HEARTBEAT = timedelta(seconds=60)                      # spec §5.2; the worker beats every 10 s
  SLICE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=4)   # "retries three times" (§5.5, C4)
  @workflow.defn(name=SLICE_WORKFLOW)
  class SliceWorkflow:
      @workflow.run
      async def run(self, input: SliceWorkflowInput) -> SliceWorkflowResult
  ```
  `SliceWorkflow.run`, in order:
  1. `slice_prepare(input)` on its own queue (SHORT, a read retry): reads the copy. On the native library the input is the file's own blob (`print_library/native.py:50` `blob_key(file_id)`, its ref by `content.stat`): no bytes move and nothing is linked (see "Notes to the parent" (e)). On Bambuddy's, the download (`served_reads(library_file_id, client, None)`) is put as kind `slice`. `NativeSliceStore.prepared(slice_id, sha)`; a `reusable(sha, preset_key)` row with a blob `stat` finds → `SlicePrepared(reused_blob_key=…)`; else `build_presets` → each document put as kind `slice` (`ContentStore.put` dedupes by sha) → `mint_get` for the input and each preset, `mint_put` for the output. Every key it puts or names is `touch`ed and then held with `BlobRefs.add(key, HOLDER, str(slice_id))` before the URLs are minted (Ruling C5: `slice` is a `SWEPT_KINDS` kind, `content_models.py:37`). A `ProfileError` or `TransferUnavailableError` is a non-retryable `ApplicationError` whose message is the slice's failure.
  2. Unless reused, `slice_plate(prepared.plate_input)` with `task_queue=input.slicer_queue`, `start_to_close_timeout=SLICE_TIMEOUT`, `heartbeat_timeout=SLICE_HEARTBEAT`, `retry_policy=SLICE_RETRY`, `result_type=SlicePlateOutput`. A failure whose cause is an `ApplicationError` of type `TRANSFER_REFUSED` (URLs expired in a long queue wait) runs step 1 and this step once more, then fails.
  3. `slice_record(...)` (SHORT, `RECORD_RETRY` as `workflows/printing.py` defines it): `finish_put` → the key, `BlobRefs.add(key, HOLDER, …)`, `NativeSliceStore.finish(completed, …)`; or `finish(failed, failure=…)`. The failure text (Ruling C4): for a `SlicerError`, its details' `stderr_tail` (`cause.details[0]["stderr_tail"]`, decoded only for that type, as `README.md` "Slicer worker" says) after `"The slicer refused the plate:"` (exit 192) or `"The slicer failed:"`; for any other type, `"The slicer worker failed (<type>)."` with no message.
  Every activity but `slice_plate` runs on `library`. No bytes and no preset document cross the history: `SlicePrepared` carries URLs (spec §5.2, Global Constraints) and the record carries keys.

- [ ] **Step 1: Write the failing store tests** (`test_store.py`): `start` twice with one `workflow_id` returns one row; `start` on a failed row returns it `running` with `failure` cleared; `reusable` finds a completed row and not a failed one; `get` under another subject is `None`; `sweep(timedelta(0))` deletes finished rows and returns their ids and leaves running ones; `native_workflow_id(7, "k") == native_workflow_id(7, "k") != native_workflow_id(8, "k")`.

- [ ] **Step 2: Write `fake_slicer.py`** — `fake_slice_plate(app)`: a Python `@activity.defn(name="slice_plate")` that GETs each `TransferUrl` with its headers through `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))` (as `tests/api/test_library_native.py` reaches the internal routes), heartbeats `SliceProgress(0, 1, 40, 40, "Slicing")`, PUTs `sliced_3mf(plates={1: (3600, [12.5, 3.0])}, gcode={1: b"M620 S0A\nM620 S1A\nM620 S0A\n"})` to `sliced_3mf`, and returns a `SlicePlateOutput` with its sha and size. `FakeSlicer` controls it: `refuse_plate` (raises `ApplicationError("validation failed", {"exit_code": 192, "stderr_tail": "plate 99 is empty"}, type="SlicerError", non_retryable=True)`: details positional, `ApplicationError(msg, *details, type=, non_retryable=)`), `crash_times` (raises a retryable `SlicerError` that many times first), `expire_first` (raises `ApplicationError("expired", type="TransferRefused", non_retryable=True)` on its first call), `hold` (an `asyncio.Event` it waits on after its heartbeat), `calls`. A `fake_slicer_worker(client: TestClient, queue: str)` context runs a `Worker` with it in the app's portal (`client.portal`), where the in-process workers run.

- [ ] **Step 3: Write the failing workflow tests** (`test_slice_workflow.py`, the api `client` with `store_backend="s3"` on moto, a KEK, `api_internal_url="http://scadbuddy"`, `slicer_backend="native"`, the fixture profiles as `slicer_profiles_dir`):
  - `test_a_slice_completes_and_holds_its_blob`: start through `start_native_slice` for a library copy the fake Bambuddy serves → the row is `completed`, `content.read(content.stat(row.sliced_blob_key).ref)` is the fixture bytes, and `blob_refs` holds the sliced key, the input key and each preset key under `("native_slice", str(row.id))`.
  - `test_a_held_blob_survives_the_blob_sweep` (C5): `sweep_content(content, refs, grace=0)` while the row stands → the sliced key still `stat`s.
  - `test_a_validation_failure_fails_once_with_the_stderr_tail`: `refuse_plate` → `failed`, `failure == "The slicer refused the plate: plate 99 is empty"`, `calls == 1`.
  - `test_a_crash_retries_three_times`: `crash_times=3` → `completed` after 4 calls; `crash_times=4` → `failed` with `"The slicer failed:"`.
  - `test_an_expired_url_prepares_again_once` (Review Focus 7): `expire_first` → `completed`; the history has two `slice_prepare` activities.
  - `test_another_error_type_hides_its_message`: the fake raises `ApplicationError("secret detail", type="SlicerWorkerError")` four times → `failure == "The slicer worker failed (SlicerWorkerError)."`.
  - `test_the_same_bytes_and_presets_reuse_the_slice`: a second copy with identical bytes (another library file id) → its row `completed` with the first's `sliced_blob_key`; the fake's `calls` unchanged.
  - `test_progress_reads_the_slice_plate_heartbeat`: with `hold` set, `slice_progress` → `SliceProgress(total_percent=40, …)`; before the fake has beaten, `None` or 0, never another activity's details.
  - `test_no_bytes_or_presets_in_the_history`: in the completed history, no payload holds the fixture's 3MF bytes or a preset document's `"layer_height"`.
  - `test_native_library_input_moves_no_bytes`: `library_backend="native"` as `test_library_native.py` sets it → completed, and the input key is the native file's own `blob_key` (no `slice-` copy of it is put).
  - `test_on_the_local_store_the_slice_fails_saying_why`: `store_backend="local"` → `failed`, failure naming `store_backend`.

- [ ] **Step 4: Write the replay test** — `tests/test_slice_replay.py` as `tests/test_housekeeping_replay.py`: `test_slice_workflow_replays_its_recorded_history` over `["completed", "refused", "prepared_again"]` with `Replayer(workflows=[SliceWorkflow], data_converter=pydantic_data_converter)`, and a `SCADBUDDY_RECORD_HISTORIES=1` recording test (stub activities) that writes the three. The `library` worker is unversioned, so a later change to `SliceWorkflow`'s commands goes behind `workflow.patched` and adds a history beside these; the module docstring says so, as the housekeeping one does.

- [ ] **Step 5: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_store.py tests/api/test_slice_workflow.py tests/test_slice_replay.py -v` → import errors.

- [ ] **Step 6: Implement** the migration (a NEW file, `$(date -u +%Y%m%dT%H%MZ)_native_slices.sql`), the settings fields, `store.py`, `component.py`, `start.py`, the workflow, models and activities, and the registration in `_run_library_worker`. Then record the histories: `SCADBUDDY_RECORD_HISTORIES=1 uv run --frozen pytest tests/test_slice_replay.py -v`, and commit them.

- [ ] **Step 7: Run, lint, commit**

Run: `cd backend && uv run --frozen pytest tests/slicing tests/api/test_slice_workflow.py tests/test_slice_replay.py tests/api/test_settings.py tests/api/test_settings_runtime.py tests/test_settings_store.py -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS; `test_settings_runtime.py` lists `slicer_backend` among `ENV_SEEDED` and `"live"` in `applies`, and nothing else moves.

```bash
git add backend/scadbuddy backend/tests
git commit -m "feat(slicing): slicer_backend, native_slices and SliceWorkflow around slice_plate"
```

---

### Task 12: The print dialog's preview on the native slicer

Ruling C1: `preview_run` runs on the `bambuddy` queue, which in prod is the storeless print worker (`build_print_deps`, `worker.py:888`), so it only starts the slice (`start_native_slice` with `activity.client()`); every byte moves on `library`. The read routes run in the API.

**Files:**
- Create: `backend/scadbuddy/slicing/preview.py`
- Modify: `backend/scadbuddy/bambuddy/preview.py`: `PreviewStarted` (:60) gains `slicer: Literal["bambuddy", "native"] = "bambuddy"`; `SlicePreview` (:75) gains `progress: int | None = None`, `warnings: list[str] = []` and `slicer: Literal["bambuddy", "native"] = "bambuddy"`; `start_preview` (:89) gains `native: NativeSliceStarter | None = None` and, after `plan_run`, branches on `settings.slicer_backend`; `read_toolpath` (:264)'s semaphore, cache and parse become `_toolpath(key, fetch: Callable[[], Awaitable[bytes | None]]) -> bytes`, which it and the native read share
- Modify: `backend/scadbuddy/bambuddy/operations.py`: `bambuddy_kinds_over` (:80) takes `native_slices: NativeSliceStore | None = None` and `native: NativeSliceStarter | None = None`; `preview_run` (:321) passes them to `start_preview` with `activity.client()`; `bambuddy_kinds` (:67) and the print worker's call of `bambuddy_kinds_over` (`worker.py`, beside `build_print_deps` :888) build them from the settings pool and `native_slice_starter(settings)`
- Modify: the four read routes: `api/printing.py` `get_preview_slice` (:1045) and `get_preview_toolpath` (:1065), `api/library_print.py` (:1367 and :1382): a query parameter `slicer: Literal["bambuddy", "native"] = "bambuddy"`; `native` reads through `slicing/preview.py`, `bambuddy` as today. The slicer comes from the request, never from the live setting, so a switch while a dialog is open cannot misroute, and a native id equal to a Bambuddy preview row's id reads its own slicer's row (Review Focus 8)
- Modify: `backend/scadbuddy/workflows/housekeeping.py`: two sweeps, each behind its own patch in `_run`'s loop as the others are (:210-224), both appended last to `SWEEPS` (:120), with timeouts in `sweep_timeout`:
  - `NATIVE_SLICES_SWEEP = "housekeeping_sweep_native_slices"`, `NATIVE_SLICES_PATCH = "housekeeping-sweep-native-slices"`: `NativeSliceStore.sweep(PREVIEW_GRACE)` then `BlobRefs.drop_holder(HOLDER, str(id))` per id; `sweep_content` deletes the blobs on its next pass (no `content.delete` here). It obeys the sweeps' switch, as `PREVIEWS_SWEEP` does: with the sweeps off, `sweep_content` does not run either.
  - `TRANSFER_PUTS_SWEEP = "housekeeping_prune_transfer_puts"`, `TRANSFER_PUTS_PATCH = "housekeeping-prune-transfer-puts"`: `TransferPuts.prune(timedelta(days=1))`; also in `PRUNE_SWEEPS` (:145), like the claims sweep, so it runs with the sweeps off.
  - Their activities in `main.py` `_housekeeping_activities` (:512), as `sweep_preview_slices` (:558) is.
- Test: `backend/tests/api/test_print_preview_native.py` (`requires_temporal`, `requires_postgres`; Task 11's fake slicer), `backend/tests/test_housekeeping_replay.py`, `backend/tests/fixtures/housekeeping_histories/native_slices.json` (new)

**Interfaces:**
- Consumes: `start_native_slice`, `native_slice_starter`, `slice_progress`, `NativeSliceStore`, `NativeSliceStarter`, `HOLDER`, `SLICING` (Task 11), `TransferPuts.prune` (Task 10), `read_sliced`, `plate_gcode` (Task 9), `MAX_SLICED_BYTES` (`preview.py:52`).
- Produces:
  ```python
  # slicing/preview.py
  async def read_native_preview(slicing: Slicing, slice_id: int, subject: str) -> SlicePreview
      # 404 for no row of that subject; running → status "running", progress = slice_progress(...).total_percent (None before a beat);
      # completed → read_sliced(the blob, row.plate_id): print_time_seconds, slots = [PreviewSlot(slot_id, grams, side=None)],
      #   filament_used_g, filament_changes, warnings; failed → status "failed", failure; always slicer="native"
  async def read_native_toolpath(slicing: Slicing, slice_id: int, subject: str) -> bytes
      # 404 / 409 as read_toolpath; the blob read whole past no MAX_SLICED_BYTES (else 422), plate_gcode, then preview._toolpath
      # under a cache key no Bambuddy entry can equal: (row.id, 0, row.sliced_blob_key, row.plate_id)
  ```
  `start_preview` gains `native: NativeSliceStarter | None = None, native_slices: NativeSliceStore | None = None, temporal: Client | None = None`. Its native branch: `ApiError` 503 when any is `None` ("the native slicer is not set up here: <which>"); else `start_native_slice(temporal, native_slices, native, library_file_id=planned.library_file_id, plan=plate.plan, plate_id=plate.plate_id, subject=subject)` and `PreviewStarted(job_id=row.id, plate_id=plate.plate_id, nozzle_plan=planned.nozzle_plan, slicer="native")`. The Bambuddy branch is today's code. `side` stays `None` on native: the side-mismatch warning (`PreviewPane.tsx:101`) goes quiet there, a follow-up issue Task 15 files.

- [ ] **Step 1: Write the failing tests** (`test_print_preview_native.py`, the Task 11 app, through the routes):
  - `test_a_native_preview_completes`: `POST …/preview-slice` → `slicer == "native"`; polling `GET …/preview-slices/{id}?slicer=native` reaches `completed` with `print_time_seconds == 3600`, `filament_used_g == 15.5`, `filament_changes == 2`, `slots[*].side is None`.
  - `test_the_same_choices_answer_the_same_job`: a second POST → the same `job_id`, the fake's `calls` still 1.
  - `test_progress_while_running`: with the fake's `hold`, the read is `running` with `progress == 40`.
  - `test_a_refused_plate_fails_with_its_stderr`: `failure` contains `plate 99 is empty`.
  - `test_another_subject_reads_404`.
  - `test_the_toolpath_of_a_native_slice` (preflight: the toolpath route): `GET …/preview-slices/{id}/toolpath?slicer=native` → 200 and the bytes `toolpath.parse(gcode).encode()` gives; 409 while running.
  - `test_a_native_id_never_reads_a_bambuddy_preview` (Review Focus 8): with a Bambuddy preview row of the same id recorded for the same subject, `?slicer=native` reads the native row and no `?slicer` reads Bambuddy's.
  - `test_native_without_profiles_is_503`.
  - `test_the_sweep_removes_a_settled_slice_and_its_holds`: a row backdated past `PREVIEW_GRACE`; the native-slices sweep, then `sweep_content(grace=0)` → the row gone, its sliced key no longer `stat`s.
  - `test_the_bambuddy_preview_is_unchanged`: `slicer_backend="bambuddy"` → `slicer == "bambuddy"` and `tests/api/test_print_preview.py` passes as it is.
  - `test_native_library_with_native_slicer` (preflight: the combination): `library_backend="native"` and `slicer_backend="native"` → `completed`.

- [ ] **Step 2: Extend the housekeeping replay** — rename `test_record_print_attachments` to `test_record_native_slices`, writing `native_slices.json` (never rewrite `print_attachments.json`; its assertion `names == list(SWEEPS)` now includes the two sweeps); add `"native_slices"` to the replay list; extend the module docstring with the two patches.

- [ ] **Step 3: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/api/test_print_preview_native.py -v` → fails.

- [ ] **Step 4: Implement**, then record: `SCADBUDDY_RECORD_HISTORIES=1 uv run --frozen pytest tests/test_housekeeping_replay.py -k record -v`.

- [ ] **Step 5: Run the preview and housekeeping tests, both slicers**

Run: `cd backend && uv run --frozen pytest tests/api/test_print_preview_native.py tests/api/test_print_preview.py tests/test_housekeeping_replay.py $(git grep -l PRUNE_SWEEPS -- tests | tr '\n' ' ') -v`
Expected: PASS; every earlier housekeeping history replays.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat(slicing): the print dialog's background slice runs on the native slicer when selected"
```

---

### Task 13: `PrintRunWorkflow` on the native slicer

Rulings C1, C2. The run neither builds presets nor touches the store: a `library`-queue activity `slice_run` starts the shared `SliceWorkflow` (or joins the one the dialog started) and waits for it, and `print_sliced_upload`, also on `library`, puts the sliced file where the queue takes it from. A child workflow is not used: in temporalio 1.34.0 `start_child_workflow` takes no `id_conflict_policy` (checked in the pinned `temporalio/workflow/_workflow_ops.py`), so a dialog's running execution of the same id would fail the child's start.

**Files:**
- Create: a NEW migration `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_output_bambuddy_slices_blob.sql`, named by `date -u` when you create it: `ALTER TABLE output_bambuddy_slices ADD COLUMN sliced_blob_key text;`
- Modify: `backend/scadbuddy/bambuddy/print_run.py`: `PlannedRun` (:1390) gains `slicer: Literal["bambuddy", "native"] = "bambuddy"` and `library_queue: str = "library"` (the defaults keep every recorded `print_plan` result reading as Bambuddy's); `plan_run` (:1472) takes `native: NativeSliceStarter | None = None` and, with `settings.slicer_backend == "native"`, sets `slicer="native"` and `library_queue=native.library_queue`, or refuses with `ApiError` 503 when `native` is `None`; its reuse lookup (`source.recorded_slice(…, slice_request(plan, plate_id).preset_key)`, around :1628) uses `native_preset_key(plan, plate_id, native.version)` when native, so a reprint reuses the native slice's uploaded file
- Modify: `backend/scadbuddy/workflows/print_activities.py`: `PrintDeps` (:114) gains `native: NativeSliceStarter | None = None`, which `print_plan` passes to `plan_run`; the API's and the print worker's `PrintDeps` are built with `native_slice_starter(settings)`
- Modify: `backend/scadbuddy/bambuddy/dispatch.py` `QueueOutcome` (:37): `native_slice_id: int | None = None`, `sliced_blob_key: str | None = None`. `slice_job_id` stays a Bambuddy job id and is `None` on native, so `REUSED_GONE_PATCH`'s `job_id is not None` test (`workflows/printing.py:447`) reslices a gone native upload through `_slice` again (natively: the row is `completed`, so it re-uploads without slicing)
- Modify: `backend/scadbuddy/bambuddy/uploads.py`: `SlicedCopy` (:34) gains `sliced_blob_key: str | None = None`; `_record_sliced`'s insert (:743) writes it. It records which blob was sliced, as provenance: nothing reads it, and its blob is swept with its `native_slices` row (Task 15 says so in spec §5.4)
- Modify: `backend/scadbuddy/bambuddy/print_source.py` (:489): `SlicedCopy(…, sliced_blob_key=outcome.sliced_blob_key)`
- Modify: `backend/scadbuddy/workflows/printing.py`: `NATIVE_SLICER_PATCH = "native-slicer"` beside `REUSED_GONE_PATCH` (:180); `_slice` (:328) returns `tuple[SliceStarted | NativeSliced, int]`, and the `QueueOutcome(...)` (:472) takes `native_slice_id`/`sliced_blob_key` from a `NativeSliced`
- Modify: `backend/scadbuddy/workflows/print_models.py` (`SliceRunInput`, `NativeSliced`, `SlicedUploadInput`), `backend/scadbuddy/workflows/slicing_activities.py` (`slice_run`, `print_sliced_upload`, registered on `library` in `_run_library_worker` with Task 11's)
- Test: `backend/tests/api/test_print_run_native.py` (`requires_temporal`, `requires_postgres`), `backend/tests/test_print_replay.py`, `backend/tests/fixtures/print_run_histories/native_sliced.json`, `native_slicer_refused.json` (new)

**Interfaces:**
- Consumes: `start_native_slice`, `NativeSliceStarter`, `native_slice_starter`, `Slicing.starter`, `SliceWorkflowResult`, `SLICE_TIMEOUT`, `SLICE_RETRY` (Task 11), `native_preset_key`, `Profiles` (Task 8), `FAILED` (`workflows/print_models.py:35`) and `problem_of` (`workflows/problems.py:68`), `upload_into` (`bambuddy/commits.py:66`), `NativeLibrary.upload` (`print_library/native.py:253`).
- Produces:
  ```python
  class SliceRunInput(BaseModel): library_file_id: int; plan: SlicePlan; plate_id: int; subject: str
  class NativeSliced(BaseModel): slice_id: int; preset_key: str; sliced_blob_key: str
  class SlicedUploadInput(BaseModel): planned: PlannedRun; plate_id: int; sliced: NativeSliced
  # activity slice_run (library): start_native_slice, then await the workflow's handle.result() while heartbeating every 10 s;
  #   completed → NativeSliced; failed → ApplicationError(failure, PrintRunError(status=422 if refused else 502,
  #   title="The slice failed", detail=failure), type=FAILED, non_retryable=True), so problem_of reports it (never SLICE_LOST)
  SLICE_RUN_TIMEOUT = SLICE_RETRY.maximum_attempts * SLICE_TIMEOUT + timedelta(minutes=10)
  # activity print_sliced_upload (library) -> int: the sliced blob's bytes into the copy's folder as
  #   f"{stem of the copy's filename}.plate{plate_id}.sliced.3mf": upload_into(client, …, folder_id=planned.folder_id) on Bambuddy's
  #   library, NativeLibrary.upload on the native one; the library file id print_enqueue queues
  ```
  In `_slice`, after the `plate.plan is None` refusal: `if planned.slicer == "native" and workflow.patched(NATIVE_SLICER_PATCH):` → `slice_run` with `SliceRunInput(library_file_id=planned.library_file_id, plan=plate.plan, plate_id=plate.plate_id, subject=accepted.run.subject)` (`_slice` gains a `subject: str` parameter, which `_print` passes) (`task_queue=planned.library_queue`, `start_to_close_timeout=SLICE_RUN_TIMEOUT`, `heartbeat_timeout=HEARTBEAT`, `retry_policy=READ_RETRY`: re-attaching is idempotent) → `print_sliced_upload` (`task_queue=planned.library_queue`, SHORT, READ_RETRY) → `return native, uploaded`. `patched` is asked only on that branch, so a Bambuddy run's history never records the marker and every recorded history replays unchanged. The library queue's name reaches the workflow in `PlannedRun.library_queue` (a workflow reads no settings). No `print_slice_start` or `print_slice_wait` runs on this branch.

- [ ] **Step 1: Write the failing tests** (`test_print_run_native.py`, the Task 11 app with the fake slicer and the fake Bambuddy routes `tests/api/test_print_run_choices.py` `run_routes` sets up):
  - `test_a_native_run_slices_uploads_and_queues`: the run succeeds; the queue's `library_file_id` is the fake Bambuddy's upload id of `<stem>.plate1.sliced.3mf`; `output_bambuddy_slices.sliced_blob_key` is the row's; the history has `slice_run` and `print_sliced_upload` and no `print_slice_start`; `QueueOutcome.slice_job_id is None` and `native_slice_id` is the row id.
  - `test_a_refused_plate_fails_the_run_with_its_stderr`: the run ends `failed` with `plate 99 is empty` in its error, never `SLICE_LOST`.
  - `test_the_dialog_and_the_run_share_one_slice` (Review Focus 6, C2): start the dialog's preview with the fake's `hold`, start the run, release → one `slice_plate` call; both read the same `native_slices` row.
  - `test_a_gone_native_upload_is_resliced` (`REUSED_GONE`): the fake Bambuddy answers the first `print_enqueue` with the gone-slice refusal → `_slice` runs again, `slice_plate` is not called again (the row is completed), a second upload is queued.
  - `test_a_reprint_reuses_the_native_upload`: a second run of the same choices queues the first upload (`plan_run`'s reuse under the native key).
  - `test_bambuddy_runs_are_unchanged`: `slicer_backend="bambuddy"` → `print_slice_start` in the history, no patch marker.
  - `test_native_library_run` (preflight: the combination): `library_backend="native"` and `slicer_backend="native"` → succeeds; the sliced file is a native library file.

- [ ] **Step 2: Record and add the histories** — in `test_a_native_run_slices_uploads_and_queues` and `test_a_refused_plate_fails_the_run_with_its_stderr`, with `SCADBUDDY_RECORD_HISTORIES=1`, write the run's history to `native_sliced.json` and `native_slicer_refused.json`; add both names to `test_print_run_replays_its_recorded_history`'s list (`tests/test_print_replay.py`) and a `NATIVE_SLICER_PATCH` paragraph to its docstring. Never re-record an existing history.

- [ ] **Step 3: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/api/test_print_run_native.py -v` → fails.

- [ ] **Step 4: Implement** the migration (a NEW file), then record the two histories (`SCADBUDDY_RECORD_HISTORIES=1 uv run --frozen pytest tests/api/test_print_run_native.py -k "queues or refused" -v`).

- [ ] **Step 5: Run the print suite** — `cd backend && uv run --frozen pytest tests/api/test_print_run_native.py tests/test_print_replay.py $(ls tests/api/test_print_run*.py | tr '\n' ' ') -v` → PASS; all 22 recorded histories and the two new ones replay.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat(printing): PrintRunWorkflow slices on the native slicer behind the native-slicer patch"
```

---

### Task 14: The print dialog's progress, its slicer, and the setting

**Files:**
- Modify: `frontend/src/lib/usePreviewSlice.ts`: `PreviewSliceState.progress: number | null` (the read's `progress` while running, `null` otherwise); the `PreviewStarted.slicer` it got is passed to every read and to the toolpath URL
- Modify: `frontend/src/lib/printSource.ts` (`readPreview(jobId, slicer)`, `toolpathUrl(jobId, slicer)`, :55-57 and :133-149) and `frontend/src/api/client.ts` (`getPreviewSlice`, `getLibraryPreviewSlice`, `previewToolpathUrl`, `libraryPreviewToolpathUrl`, :1726-1734): `?slicer=native` only when native, so a Bambuddy read's URL is today's
- Modify: `frontend/src/components/print/PreviewPane.tsx`: the `slicing` branch of `slice-status` (:269) shows `Slicing… 40%` and a `<progress max={100} value={40} data-testid="slice-progress">` when `progress != null`; the text alone otherwise
- Modify: `frontend/src/pages/SettingsPage.tsx`: a `slicer_backend` select (`bambuddy` "Bambuddy's slicer", `native` "ScadBuddy's slicer (Bambu Studio CLI)") beside `library_backend` (:112, :200, :1272), applying live (no restart badge)
- Modify: `frontend/src/mocks/features/printPreview.ts` (:52: a native read answers `running` with `progress` once, then `completed`), `frontend/src/mocks/fixtures.ts` and `frontend/src/mocks/features/settings.ts` (`slicer_backend: 'bambuddy'`)
- Test: `frontend/src/components/print/PreviewPane.test.tsx`, `frontend/src/lib/usePreviewSlice.test.ts`, `frontend/src/pages/SettingsPage.sections.test.tsx`

**Interfaces:**
- Consumes: `PreviewStarted.slicer`, `SlicePreview.progress`, `.slicer`, `.warnings`, the `slicer` query parameter (Task 12), `slicer_backend` on the settings routes (Task 11), all from the regenerated `schema.d.ts` (`pnpm gen:api` runs in `typecheck`).

- [ ] **Step 1: Write the failing tests**
  - `PreviewPane`: a running read with `progress: 40` renders `slice-progress` with `value` 40 and the text `Slicing… 40%`; a Bambuddy read without `progress` renders `Slicing…` and no bar; a failed native read's `failure` (`"The slicer refused the plate: plate 99 is empty"`) is the `slice-error` alert's text verbatim (spec §5.5).
  - `usePreviewSlice`: `progress` follows two polls (20 then 60); a native start's reads and toolpath URL carry `slicer=native`, a Bambuddy start's carry none.
  - `SettingsPage`: the `slicer_backend` select saves `native` with no restart notice.
- [ ] **Step 2: Run to verify they fail** — `cd frontend && pnpm test -- PreviewPane usePreviewSlice SettingsPage` → FAIL.
- [ ] **Step 3: Implement** — a native `<progress>` styled with the pane's tokens; no new library.
- [ ] **Step 4: Run** — `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build` → PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): the print dialog shows the native slicer's progress; slicer_backend in Settings"
```

> **My call, as spec §5.4 now reads:** no `slice:<job>` realtime topic. `realtime.ts` documents that events "carry ids, never content", and nothing on the Python side sees a heartbeat except a `describe()`, so a topic would be a poll republished. The dialog already polls every 2 s (`RESLICE_POLL_MS`); `progress` rides on that read.

---

### Task 15: Dashboards, docs, spec and follow-ups

**Files:**
- Modify: `deploy/grafana/scadbuddy-printing.json` (a "Slice duration" panel on `scadbuddy_render_stage_seconds{stage="slice"}`, p50/p95, and its count; the series is declared in `backend/scadbuddy/core/metrics.py:287`)
- Modify: `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md`, each change a sentence or two citing its ruling or measurement:
  - §5.2: the internal blob routes serve every store backend that has a content store (`s3`, `bambuddy`; not `local`, which has none, `store/factory.py:84`); presigned S3 is GET only (`presign_get`), presigned PUT and adopting its object a follow-up (Ruling C3, the issue below); the token is new, signed under a key derived from the KEK, so the native slicer needs `SCADBUDDY_SECRET_KEY_FILE` and `SCADBUDDY_API_INTERNAL_URL` (`RemoteOutputs` has no token: correct the "as `RemoteOutputs` already uses" line); `slice` blobs are held by `blob_refs` while their `native_slices` row stands (Ruling C5).
  - §5.3: the profiles are copied into the API image from the `slicer-dist` stage at build time, not mirrored as a `preset_bundle` blob (no such kind exists); the preset key's version is the API image's `BAMBU_STUDIO_VERSION` (Ruling C7); a `local` filament or printer preset is refused on the native slicer while Bambuddy serves process values only (Task 8's re-measurement, with its date), unless that measurement found otherwise.
  - §5.4: `SliceWorkflow` runs on the `library` queue and is started, never run, by the print worker's `preview_run`; the run shares it through the `slice_run` activity (Rulings C1, C2); in tier 2 the sliced file is still uploaded to the print library for the queue (`print_sliced_upload`), and `sliced_blob_key` is provenance whose blob is swept with its row; the read routes take `slicer`; `side` is `None` on native (the follow-up below).
  - §5.5: the retry policy is four attempts (three retries), and a `TransferRefused` prepares again once (Ruling C4); the failure text is `stderr_tail` for a `SlicerError` only.
  - §8: the slicer image and the API image deploy together, since the preset key carries the API image's Bambu Studio version (Ruling C7).
- Modify: `README.md` "Slicer worker (#3821)": the API side (the KEK and `SCADBUDDY_API_INTERNAL_URL` on the API, the slicer pod reaching that URL, `store_backend` `s3` or `bambuddy`, the print worker's `SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY` and `_SLICER` matching the API's) and a "Rolling out" list: deploy both images together, check `scadbuddy-slicer --check-cli` and one preview slice with `slicer_backend` still `bambuddy`… then flip `slicer_backend` to `native` in Settings (live), and back the same way.
- Modify: `docs/ai/operating.md`: a line that `get_settings` shows `slicer_backend` and `update_settings` changes it (an outward call, as every settings write).
- Modify: `CLAUDE.md` Layout: a `backend/scadbuddy/slicing/` bullet (models, profiles, presets, slice_info, transfer and transfer_token, store, start, preview, component; `workflows/slicing.py` on `library`; `api/internal_blobs.py`, hidden; the replay fixtures `tests/fixtures/slice_workflow_histories/`).
- Check: `agent/src/tools/coverage.ts` needs no entry (no new public route; the internal ones are hidden, and the four read routes only gained a query parameter); `cd agent && pnpm test -- coverage` confirms.

- [ ] **Step 1: Add the panel and lint** — `KUSTOMIZE=<kustomize v5.6.0> .github/scripts/lint-dashboard.sh` → clean.
- [ ] **Step 2: The spec, README, operating guide and CLAUDE.md edits.**
- [ ] **Step 3: File the follow-ups** with `gh issue create --repo eh-homelab/ScadBuddy`, each `Refs #3818, #3822` with a session tag: "presigned S3 PUT for the slicer's output, with the backend adopting its `pending/` object" (C3), "the native slicer's `side` per slot for the side-mismatch warning" (`extruders.py`'s plan can fill it), and, if Task 8's re-measurement still refuses filament values, "`local` filament presets on the native slicer". Put their numbers in the spec lines above.
- [ ] **Step 4: Agent check** — `cd agent && pnpm test -- coverage` → PASS.
- [ ] **Step 5: Commit**

```bash
git add deploy/grafana docs README.md CLAUDE.md
git commit -m "docs(slicing): dashboard panel, spec rulings, rollout and layout notes for the native slicer"
```

---

### Task 15b: The #4080 review nits (#4099)

Ruling C6. Closes #4099.

**Files:**
- Modify: `workers/rust/crates/sb-transfer/src/lib.rs` (`TransferError::InvalidHeader`'s `Display`, :86: `invalid transfer header: {why}`, as the other variants carry `http:` and `io:`)
- Modify: `workers/rust/crates/sb-slicer/tests/runner.rs`, and a fake beside `tests/fake-bambu-studio.sh` (or a `FAKE_` switch in it) that starts `setsid sh -c 'echo held >&2; sleep 30' &`, writes the child's pid to `$FAKE_HOLDER_PID_FILE`, and exits 0 with its sliced 3MF written
- Create: `.github/scripts/rust-toolchain-pin.sh`, `.github/scripts/rust-toolchain-pin.test.sh`, `.github/workflows/rust-toolchain-pin.yml`
- Modify: `CLAUDE.md` "CI and caching rules" (a line on the check), `.github/workflows/ci.yml` only to point the pin's comment (:1611) at the check

**Interfaces:**
- `rust-toolchain-pin.sh [--check]`: reads the SHA after `dtolnay/rust-toolchain@` in `ci.yml` and `gh api repos/dtolnay/rust-toolchain/commits/master --jq .sha`; prints both; with `--check` exits 1 when they differ, naming the line to change. The action has no release tags, so Dependabot never moves this pin (#4099).
- `rust-toolchain-pin.yml`: `schedule` weekly and `workflow_dispatch`, `ubuntu-latest`, `permissions: contents: read`, runs the script with `--check` and `GH_TOKEN: ${{ github.token }}`; a failure is the reminder, as `agent-durable-pin.yml` is for its pin.

- [ ] **Step 1: Write the failing tests**
  - `sb-transfer`: `assert_eq!(TransferError::InvalidHeader("invalid header name \"a b\"".into()).to_string(), "invalid transfer header: invalid header name \"a b\"")`.
  - `runner.rs` `stderr_held_outside_the_group_is_abandoned_after_the_grace`: the setsid fake → `run` returns `Ok` with `exit_code == 0` within `STDERR_GRACE + 2 s`, and `stderr_tail` contains `held`; the test then kills the holder by the pid in its file (`nix::sys::signal::kill`), never by name.
  - `rust-toolchain-pin.test.sh`: with a stub `gh` on `PATH` answering a fixed SHA, `--check` passes on a `ci.yml` copy pinned to it and fails, naming the line, on one pinned elsewhere.
- [ ] **Step 2: Run to verify they fail** — `cd workers/rust && cargo test --workspace` → the two Rust tests fail; `bash .github/scripts/rust-toolchain-pin.test.sh` → fails (no script).
- [ ] **Step 3: Implement** the prefix, the fake and the script and workflow.
- [ ] **Step 4: Run the `rust` job's commands and the lint job's** — `cd workers/rust && cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`, then `shellcheck .github/scripts/*.sh && bash .github/scripts/rust-toolchain-pin.test.sh && actionlint` → PASS.
- [ ] **Step 5: Commit**

```bash
git add workers/rust .github CLAUDE.md
git commit -m "chore(slicer): #4080 review nits: InvalidHeader prefix, setsid stderr test, toolchain pin check

Fixes #4099"
```

---

## Self-review notes

- **Spec coverage.** §5.1 boundary → Tasks 5, 6. §5.2 activity, queue, `TransferUrl` inputs and PUT outputs, no credentials or database in the worker, the env-only configuration, heartbeats, `SlicerError`, 192 non-retryable, process-group kill, 60 s heartbeat timeout, concurrency `cpus/2` → Tasks 1, 3, 6, 7, 10, 11. §5.2's internal blob routes → Task 10, on every backend with a content store; presigned GET only (Ruling C3; presigned PUT a follow-up filed in Task 15). §5.3 presets as files, flattening, overrides, `local:` via Bambuddy, `preset_key` with slicer and version → Task 8 (profiles copied into the API image from `slicer-dist`; version from the API image, Ruling C7). §5.4 `slicer_backend`, `SliceWorkflow`, `native-slicer` patch, `output_bambuddy_slices` column, preview switch, progress → Tasks 11–14 (no realtime topic, as §5.4 says). §5.5 failure surface → Tasks 6, 11 (four attempts, Ruling C4), 12, 13, 14 (the alert's text). §7.1–§7.3 → Tasks 1–7 as before. §7.4 one-activity workflow until 1.31 → Task 11. §8 rollout → Task 15 (README; both images together). Where a ruling departs from the spec, Task 15 edits the section.
- **Rulings.** C1 → Tasks 11 (`slice_prepare` on `library`), 12 (`preview_run` only starts), 13 (`slice_run`, `print_sliced_upload` on `library`). C2 → Task 11 `start_native_slice` (`USE_EXISTING`) and Task 13 `slice_run`, with `native_workflow_id` known before the bytes. C3 → Task 10. C4 → Task 11 `SLICE_RETRY`, the re-prepare and the failure text. C5 → Task 11 holds, Task 12 drops. C6 → Task 15b. C7 → Task 8 `Profiles.version`, Task 15 §8. Plus: `transfer_puts` on `PRUNE_SWEEPS` (Task 12); `slice_progress` by activity type (Task 11); presets built in `slice_prepare` (Task 11); the `/toolpath` routes and the id collision (Task 12, the `slicer` parameter); `NativeSliceRead` (Task 9); `slicer-dist` (Task 8); `QueueOutcome.slice_job_id` stays Bambuddy's (Task 13); the spec notes and follow-up issues (Task 15); README rollout (Task 15).
- **History.** No task puts bytes or preset documents in a history: `SliceWorkflowInput` carries a library file id, a `SlicePlan` (refs and overrides, as `print_plan` already records) and a plate; `SlicePrepared` carries URLs; the records carry keys. Task 11 asserts it.
- **Temporal versioning.** `PrintRunWorkflow` (the unversioned-in-API, pinned-in-worker `bambuddy` queue): `NATIVE_SLICER_PATCH`, asked only on the native branch; the 22 recorded histories replay and two new ones are added (Task 13). `Housekeeping` (unversioned `library`): two new sweeps, each behind its own patch, one new history (Task 12). `SliceWorkflow` (unversioned `library`, new): three histories from the start, so its first change needs a patch (Task 11).
- **Types.** `Profiles.version`, `build_presets`, `native_preset_key` (Task 8) in Tasks 11–13; `plate_gcode`, `read_sliced`, `NativeSliceRead`, `sliced_3mf` (Task 9) in Tasks 11–13; `Transfers.mint_get(key)`, `mint_put()`, `finish_put(put, sha256=, size=)`, `PendingPut`, `TransferPuts.prune` (Task 10) in Tasks 11, 12; `NativeSliceStore`, `HOLDER`, `native_workflow_id`, `start_native_slice`, `NativeSliceStarter`, `slice_progress`, `SliceWorkflowResult`, `SLICE_TIMEOUT`, `SLICE_RETRY` (Task 11) in Tasks 12, 13; `PreviewStarted.slicer`, `SlicePreview.progress/.slicer/.warnings`, the `slicer` parameter (Task 12) in Task 14.
- **Review Focus.** 1–3 → Task 6; 4 → Task 8; 5 → Task 10; 6 → Task 13; 7 → Task 11; 8 → Task 12.
- **Proportion.** Code blocks are tests, signatures, two tables and the token format; bodies only where the token format and the fake CLI's protocol are fixed.
- **Notes to the parent.** (a) The Bambuddy print path still needs the sliced file in the print library (`print_sliced_upload`), so a native slice on a Bambuddy-printed job costs one upload; tier 3's native print path removes it. (b) `local` has no content store, so the native slicer needs `store_backend` `s3` or `bambuddy` (Task 10, recorded in §5.2). (c) `side` is `None` on native (follow-up, Task 15). (d) The transfer token's key derives from the KEK, which flows already require on prod; without it `slice_prepare` fails the slice saying so. (e) Ruling C1 names `ContentStore.link` for a native library input; this plan mints the GET on the library file's own key instead, because a linked key keeps its ref's kind `library`, and `print_library/sweep.py` `sweep_library` removes, after its one-hour `ORPHAN_GRACE`, every `library` key no library row names, which a slice's linked key is not. No bytes move either way.
