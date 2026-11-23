# Printer camera recording, phase 1 (Record) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every RTSPS printer camera all the time, with one pull per printer, into
fMP4 segments in the bucket, indexed in Postgres, with no gap when a recorder restarts.

**Architecture:** A relay (MediaMTX plus a Python controller) holds the only pull per
printer. Two recorders (MediaMTX with recording on, plus a Python controller each) read the
relay; per printer, the one holding a CRUD-backed lease uploads and indexes segments, the
other keeps a short local buffer for a gapless handover. Both controllers follow the event
bus over a CRUD server-sent event stream; nothing polls.

**Tech Stack:** Python 3.12 (FastAPI, psycopg, pydantic-settings, aiobotocore, httpx),
MediaMTX (pinned), Postgres migrations, Docker stages, Grafana dashboard JSON.

**Spec:** `docs/superpowers/specs/2026-11-23-printer-camera-recording-design.md` (§6.1,
§6.2, §6.3, §6.8, §6.9, §9 phase 1; decisions D7, D13, D14, D15). Later phases (per-print
video and flows, live view, timelapse, retention) each get their own plan.

## Global Constraints

- Only the API holds SQL. The relay and recorder processes refuse to start with
  `SCADBUDDY_DATABASE_URL`, `SCADBUDDY_SECRET_KEY_FILE` or `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`
  set (CLAUDE.md "Database access"; pattern: `profile_fetch/settings.py` `check_environment`).
- New callers `camera` and `camera-relay`; their routes live under
  `/api/v1/internal/crud/camera/` and `/api/v1/internal/crud/camera-relay/` (`crud/wire.py`
  `PREFIX`), each requiring its own service credential from the first PR.
- Access codes: only `camera-relay` routes return them; never logged, never written to disk;
  given to MediaMTX through its control API only.
- Bucket camera keys: recorder pods only.
- No re-encode anywhere in this phase. MediaMTX copies H.264.
- Segment target length 4 s; MediaMTX `recordFormat: fmp4`, `recordSegmentDuration: 4s`.
- Object key: `<prefix>/camera/<printer_id>/<yyyy>/<mm>/<dd>/<HHMMSS.ffffff>.mp4` (UTC start).
- Lease period 20 s, renewed every 5 s; standby keeps segments that end at or after the
  index's last end; local disk cap `SCADBUDDY_CAMERA_DISK_MAX_BYTES` (default 2 GiB).
- A printer qualifies when: LAN link with a sealed code, last report's `ipcam.rtsp_url`
  starts with `rtsps://`, and `printers.camera_recording` is true (default true).
- Migrations: new files only, `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` (root CLAUDE.md
  "Migrations").
- Every external or measured fact in docs cites its source (#259).
- Conventional-commit titles, scope `camera`.

## Review Focus

1. **Handover with a segment straddling the index end.** The new leader must upload the
   local segment that contains `indexed_until`, flagged `discontinuity`, never skip it
   (Task 8 `test_handover_uploads_straddling_segment`).
2. **API or bucket down for minutes.** Segments stay on disk, upload oldest first after
   recovery, upload strictly before index; at the disk cap the oldest unuploaded are dropped
   and a gap is recorded (Task 8 `test_backlog_drops_oldest_at_cap`).
3. **A printer whose report loses `rtsp_url` or whose toggle turns off mid-recording.** Relay
   removes the path; recorders upload what they hold, then report `stopped` (Task 7
   `test_reconcile_removes_unqualified`, Task 8 `test_stop_flushes_then_reports`).
4. **The change stream drops or answers `resync`.** Controllers re-read the full list once,
   never act on a stale row (Task 6 `test_follower_rereads_on_resync`).
5. **Both recorders call acquire at once.** Exactly one gets the lease (Task 3
   `test_acquire_is_exclusive`).

---

### Task 1: The `camera` and `camera-relay` callers

**Files:**
- Modify: `backend/scadbuddy/crud/wire.py` (`CrudCaller`), `backend/scadbuddy/auth/service_credentials.py` (`ServiceCaller`, `SCOPES`), `backend/scadbuddy/auth/routes.py` (`RouteClass`, `CRUD_CLASSES`), `backend/scadbuddy/auth/exchange.py`, `backend/scadbuddy/auth/service_auth.py`, `backend/scadbuddy/api/crud_areas.py`, `frontend/src/mocks/features/serviceCredentials.ts`, `docs/security/trust-boundaries.md`
- Test: `backend/tests/auth/test_service_credentials.py`, `backend/tests/auth/test_routes.py`

Follow exactly what commit `3576b1712` did for `profile-fetch` in each of these files.

**Interfaces:**
- Produces: `CrudCaller` includes `"camera"`, `"camera-relay"`; `SCOPES["camera"] == {"crud.camera"}`, `SCOPES["camera-relay"] == {"crud.camera-relay"}`; `crud_router("camera")`, `crud_router("camera-relay")` usable.

- [ ] **Step 1: Write failing tests**: `test_camera_scopes` asserts the two `SCOPES` entries above; `test_camera_routes_classed` asserts a route at `/api/v1/internal/crud/camera/x` is class `crud.camera`, at `/api/v1/internal/crud/camera-relay/x` is `crud.camera-relay`, and that a `camera` credential calling a `camera-relay` route gets reason `wrong_caller`.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/auth -k camera -v`. Expected: FAIL.
- [ ] **Step 3: Add the callers** in every file listed, as for `profile-fetch`. In `trust-boundaries.md`, add both zones with what each holds (spec §6.8 table).
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/auth tests/crud -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(camera): the camera and camera-relay service callers`.

### Task 2: Schema and the printer toggle

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_camera_recording.sql`
- Create: `backend/scadbuddy/camera/__init__.py`, `backend/scadbuddy/camera/models.py`
- Modify: `backend/scadbuddy/printers/models.py` (`PrinterRow`, `PrinterPatch`), `backend/scadbuddy/printers/store.py` (`patch`, row read), `backend/scadbuddy/printers/watch.py` (`_announce`)
- Test: `backend/tests/camera/test_models.py`, `backend/tests/printers/test_store.py`, `backend/tests/printers/test_watch.py`

**Interfaces:**
- Produces (in `camera/models.py`):
  - `def rtsp_url(report: Mapping[str, Any]) -> str | None` — `report["ipcam"]["rtsp_url"]` when it starts with `rtsps://`, else `None`. `report` is the stored `print` object (`PrinterReportStore.get` first item).
  - `class CameraSource(BaseModel)`: `printer_id: str`, `host: str`, `port: int`, `path: str`, `cert_sha256: str`.
  - `class CameraSourceWithCode(CameraSource)`: `access_code: SecretStr`.
  - `class SegmentIn(BaseModel)`: `started_at: datetime` (aware UTC), `duration_ms: int (gt=0)`, `object_key: str`, `bytes: int (gt=0)`, `init_bytes: int (gt=0)`, `init_sha256: str (64 hex)`, `discontinuity: bool`.
  - `class LeaseAnswer(BaseModel)`: `held: bool`, `holder: str | None`, `expires_at: datetime | None`, `indexed_until: datetime | None`.
  - `FeedState = Literal["pulling", "retrying", "stalled", "stopped"]`; `class FeedReport(BaseModel)`: `part: Literal["relay", "recorder"]`, `state: FeedState`, `error: str | None`, `recorder: str | None`, `last_segment_at: datetime | None`, `upload_lag_s: float | None`, `buffered_bytes: int | None`.
- Produces (schema): `printers.camera_recording boolean NOT NULL DEFAULT true`; `camera_segments(id bigserial, printer_id uuid REFERENCES printers ON DELETE CASCADE, started_at timestamptz, duration_ms int, object_key text UNIQUE, bytes bigint, init_bytes int, init_sha256 text, recorder text, discontinuity bool, indexed_at timestamptz DEFAULT now())` with index `(printer_id, started_at)`; `camera_leases(printer_id uuid PRIMARY KEY REFERENCES printers ON DELETE CASCADE, holder text, expires_at timestamptz)`; `camera_feeds(printer_id uuid, part text, report jsonb, updated_at timestamptz, PRIMARY KEY (printer_id, part))`.
- Produces: `PrinterRow.camera_recording: bool`, `PrinterPatch.camera_recording: bool | None`.

- [ ] **Step 1: Failing tests**: `test_rtsp_url_from_h2c_report` loads `.claude/skills/h2c-print-from-scadbuddy/evals/decode-mapping-from-printer-report/files/printer-report-boys1.json` and asserts `rtsp_url(...) == "rtsps://172.16.0.187:322/streaming/live/1"`; `test_rtsp_url_none_for_rtsp_or_missing` (`"rtsp://…"`, `""`, no `ipcam` → `None`); `test_patch_camera_recording` patches `False` and reads it back, and asserts a `printer.changed` event; `test_watch_announces_changed_on_rtsp_url_change` writes two reports whose `ipcam.rtsp_url` differ and asserts one `printer.changed`, and two reports that differ only in progress emit none.
- [ ] **Step 2: Run** `uv run pytest tests/camera/test_models.py tests/printers/test_store.py tests/printers/test_watch.py -k "camera or rtsp" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration, the models, the toggle and, in `watch.py`, a `printer.changed` emit when `rtsp_url(written.print)` differs from the last announced one (track it next to `announced`).
- [ ] **Step 4: Run** the step 2 command and `uv run pytest tests/printers -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(camera): segment, lease and feed tables; the printer's camera toggle`.

### Task 3: The camera store

**Files:**
- Create: `backend/scadbuddy/camera/store.py`
- Test: `backend/tests/camera/test_store.py`

**Interfaces:**
- Consumes: Task 2 schema and models.
- Produces (`class CameraStore`, built on the API's pool like `PrinterReportStore`):
  - `sources(printer_id: str | None = None) -> list[CameraSource]` — qualifying printers (or one; empty list when it does not qualify or is gone); joins `printers`, the LAN link and `printer_reports`.
  - `sources_with_codes(printer_id: str | None = None) -> list[CameraSourceWithCode]` — same, codes opened with `printers.store.open_access_code`.
  - `index(printer_id: str, recorder: str, segments: list[SegmentIn]) -> int` — inserts, `ON CONFLICT (object_key) DO NOTHING`; returns rows inserted. Refuses (raises `NotLeaderError`) unless `recorder` holds a live lease for `printer_id`.
  - `lease(printer_id: str, recorder: str, action: Literal["acquire", "renew", "release"], now: datetime) -> LeaseAnswer` — one statement each: acquire/renew set `holder=recorder, expires_at=now+20s` only when the row is free, expired or already `recorder`'s; release clears it only when `recorder` holds it. Always answers `indexed_until = max(started_at + duration_ms)` for the printer.
  - `report_feed(printer_id: str, report: FeedReport) -> bool` — upsert; `True` when `state` changed.

- [ ] **Step 1: Failing tests** (Postgres fixture as in `tests/printers/test_store.py`): `test_acquire_is_exclusive` (two recorders acquire at the same `now`: exactly one `held`); `test_lease_expires_after_20s`; `test_release_only_by_holder`; `test_index_refuses_non_leader`; `test_index_is_idempotent_by_key`; `test_indexed_until_is_last_end`; `test_sources_skip_unqualified` (no code, no `rtsps` URL, toggle off → absent); `test_report_feed_says_changed`.
- [ ] **Step 2: Run** `uv run pytest tests/camera/test_store.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `CameraStore`. The lease statement is a single `INSERT … ON CONFLICT (printer_id) DO UPDATE … WHERE camera_leases.holder = EXCLUDED.holder OR camera_leases.expires_at < now RETURNING holder`.
- [ ] **Step 4: Run** the step 2 command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(camera): the camera store: sources, index, leases, feed state`.

### Task 4: CRUD routes and the `camera.feed` event

**Files:**
- Create: `backend/scadbuddy/api/crud_camera.py`, `backend/scadbuddy/api/crud_camera_relay.py`
- Modify: `backend/scadbuddy/core/events.py` (new `CameraFeedEvent`), `backend/scadbuddy/api/realtime.py` (route it to `printers` and `printer:<id>`), `backend/scadbuddy/api/crud_areas.py`, the app's router registration (where `crud_profile_fetch` is included)
- Test: `backend/tests/api/test_crud_camera.py`

**Interfaces:**
- Consumes: `CameraStore` (Task 3).
- Produces routes (`crud_router("camera")`):
  - `GET …/camera/printers` → `list[CameraSource]`; `GET …/camera/printers/{printer_id}` → `CameraSource` or 404 `not_recorded`.
  - `POST …/camera/printers/{printer_id}/segments` body `{recorder: str, segments: list[SegmentIn]}` (max 500) → `{indexed: int}`; 409 `not_leader`.
  - `POST …/camera/printers/{printer_id}/lease` body `{recorder: str, action}` → `LeaseAnswer`.
  - `POST …/camera/printers/{printer_id}/feed` body `FeedReport` (with `part="recorder"`) → 204.
- Produces routes (`crud_router("camera-relay")`): `GET …/camera-relay/printers` → `list[CameraSourceWithCode]` (code as plain string in JSON); `GET …/camera-relay/printers/{printer_id}`; `POST …/camera-relay/printers/{printer_id}/feed` (`part="relay"`).
- Produces event: `class CameraFeedEvent(BaseEvent)`: `kind: Literal["camera.feed"]`, `printer_id: str`; emitted only when `report_feed` answers `True`.

- [ ] **Step 1: Failing tests**: `test_camera_cannot_read_codes` (a `camera` credential on `camera-relay/printers` → 403 `wrong_caller`); `test_relay_reads_codes`; `test_segments_409_when_not_leader`; `test_feed_change_emits_once` (same state twice → one `camera.feed`); `test_lease_answers_indexed_until`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_crud_camera.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** both routers, the event, its realtime mapping and the areas entries (`"camera": ["printers"]`, `"camera-relay": ["printers"]`; Task 5 adds `"changes"` to both, so a controller started against an older API waits in `wait_for_areas` instead of getting a 404).
- [ ] **Step 4: Run** step 2 plus `uv run pytest tests/auth/test_routes.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(camera): CRUD routes for sources, segments, leases and feed state`.

### Task 5: Change streams for the camera callers

**Files:**
- Modify: `backend/scadbuddy/api/crud_agent_changes.py` (extract the body of `changes` into `def change_stream(request, state, hub, last_event_id, *, keep: Callable[[Change], bool] | None) -> StreamingResponse`), `backend/scadbuddy/agent_changes/hub.py` (a stream may carry a `keep` filter applied in `ChangeStream.offer` and in replay)
- Modify: `backend/scadbuddy/api/crud_camera.py`, `backend/scadbuddy/api/crud_camera_relay.py` (add `GET …/changes`)
- Test: `backend/tests/api/test_crud_camera_changes.py`; the existing `tests/api/test_crud_agent_changes*.py` must stay green.

**Interfaces:**
- Produces: `CAMERA_KINDS = frozenset({"printer.changed", "camera.reconnect"})`; both `…/changes` routes stream only `scadbuddy_events` changes whose decoded `kind` is in `CAMERA_KINDS`, with the agent stream's frames (`change`, `ready`, `reconnected`, keepalive) and `Last-Event-ID` replay. (`camera.reconnect` is defined in phase 3; the filter names it now.)

- [ ] **Step 1: Failing tests**: `test_camera_stream_filters_kinds` (emit `printer.report`, `printer.changed`, `job.done`: only `printer.changed` arrives); `test_camera_stream_replays_after_last_event_id` (filtered replay); `test_camera_stream_resync_on_bad_id`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_crud_camera_changes.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the extraction and filter; the agent route passes `keep=None` and behaves as before Add `"changes"` to both callers' areas in `crud_areas.py`.
- [ ] **Step 4: Run** step 2 and `uv run pytest tests/api -k changes -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(camera): filtered change streams for the camera callers`.

### Task 6: Camera process foundations

**Files:**
- Create: `backend/scadbuddy/camera/settings.py`, `backend/scadbuddy/camera/follow.py`, `backend/scadbuddy/camera/mediamtx.py`, `backend/scadbuddy/camera/mp4.py`
- Test: `backend/tests/camera/test_settings.py`, `test_follow.py`, `test_mediamtx.py`, `test_mp4.py`

**Interfaces:**
- Produces:
  - `class RelaySettings(BaseSettings)`, `class RecorderSettings(BaseSettings)` (`env_prefix="SCADBUDDY_"`), with `check_environment(env)` refusing the forbidden variables (Global Constraints; reuse `intake_worker.settings.FORBIDDEN_ENV`). Recorder adds `camera_s3_*` (endpoint, bucket, prefix, keys as `SecretStr`), `camera_disk_max_bytes: int = 2 * 1024**3`, `camera_recorder_name: str` (default: pod hostname), `camera_relay_rtsp_url: str` (e.g. `rtsp://scadbuddy-camera-relay:8554`), `camera_relay_read_user`, `camera_relay_read_pass: SecretStr`.
  - `class ChangeFollower`: `__init__(client: CrudClient, path: str, on_change: Callable[[str], Awaitable[None]], on_resync: Callable[[], Awaitable[None]])`, `async run() -> None`. Parses SSE frames, reconnects with back-off 0.5 s doubling to 30 s with jitter, sends `Last-Event-ID`, treats 45 s of silence as dead, calls `on_resync` on `ready{resync|reconnect}` and `reconnected`, and `on_change(printer_id)` for each `printer.changed` or `camera.reconnect`.
  - `class MediaMtx`: `__init__(base_url: str = "http://127.0.0.1:9997")`; `async paths() -> dict[str, dict]`; `async put_path(name: str, conf: dict) -> None` (add, or replace when present); `async delete_path(name: str) -> None` (404 is fine). Uses `/v3/config/paths/*`.
  - `def init_span(path: Path) -> tuple[int, str]` — walks top-level MP4 boxes; returns the byte length of the leading `ftyp`+`moov` and the sha256 of those bytes; raises `ValueError` when the file does not start with `ftyp` then `moov`.
  - `def segment_start(path: Path) -> datetime` — parses MediaMTX's `recordPath` name `%Y-%m-%d_%H-%M-%S-%f` as UTC. MediaMTX names files in the container's local time, so the recorder image sets `TZ=UTC` (Task 10) and `RecorderSettings.check_environment` refuses a `TZ` other than `UTC` (test `test_recorder_refuses_non_utc_tz`).

- [ ] **Step 1: Failing tests**: `test_recorder_refuses_database_url`; `test_follower_rereads_on_resync` (fake SSE server sends `ready {"resync": true}` then `change` for printer `p1`: `on_resync` once, `on_change("p1")` once); `test_follower_reconnects_with_last_event_id`; `test_put_path_replaces` (fake MediaMTX API via `httpx.MockTransport`); `test_init_span_of_fixture` on a 2-second fMP4 fixture made once with `ffmpeg -f lavfi -i testsrc=size=320x240:rate=10 -t 2 -c:v libx264 -g 10 -movflags +frag_keyframe+empty_moov` committed under `backend/tests/camera/fixtures/seg.mp4` (via LFS if over the repo's size rule; see `backend/CLAUDE.md` "LFS fixtures"); `test_init_span_rejects_non_mp4`; `test_segment_start_parses_name`.
- [ ] **Step 2: Run** `uv run pytest tests/camera -k "settings or follow or mediamtx or mp4" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the four modules.
- [ ] **Step 4: Run** step 2. Expected: PASS.
- [ ] **Step 5: Commit** `feat(camera): settings, change follower, MediaMTX client and fMP4 init reader`.

### Task 7: The relay process

**Files:**
- Create: `backend/scadbuddy/camera/relay.py` (with `main()`, run as `python -m scadbuddy.camera.relay`), `deploy/camera/mediamtx-relay.yml`
- Test: `backend/tests/camera/test_relay.py`

**Interfaces:**
- Consumes: `ChangeFollower`, `MediaMtx`, `CameraSourceWithCode`, the `camera-relay` routes.
- Produces: `class Relay`: `async reconcile_all() -> None`, `async reconcile(printer_id: str) -> None`, `async on_hook(printer_id: str, event: Literal["ready", "not_ready"]) -> None`. Path name is the printer id. Path conf: `source: rtsps://bblp:<code>@<host>:<port><path>`, `sourceFingerprint: <cert_sha256 lowercase hex>`, `sourceOnDemand: false`, `runOnReady`/`runOnNotReady` calling `curl -fsS -X POST http://127.0.0.1:9091/hook/$MTX_PATH/ready` (`…/not_ready`). The local hook server listens on `127.0.0.1:9091` only; `/healthz` and `/metrics` on `9090`.
- `mediamtx-relay.yml`: `api: yes`, `apiAddress: 127.0.0.1:9997`, `metricsAddress: 127.0.0.1:9998`, `rtspAddress: :8554`, `rtmp/hls/webrtc/srt: no`, `authInternalUsers` with one read user from env (`MTX_READ_USER`/`MTX_READ_PASS`) allowed `read` on all paths and the localhost API user allowed `api`.

- [ ] **Step 1: Failing tests** (fake MediaMTX + fake CRUD): `test_reconcile_all_adds_and_removes`; `test_reconcile_removes_unqualified` (404 for the id → `delete_path`, then feed `stopped`); `test_reconcile_replaces_on_change` (new host → `put_path` with new source); `test_hook_reports_pulling_and_retrying`; `test_code_never_logged` (caplog holds no access code after a full reconcile).
- [ ] **Step 2: Run** `uv run pytest tests/camera/test_relay.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `Relay` and `main()`: check environment, `exchange_source` for `camera-relay`, `wait_for_areas`, `reconcile_all`, then run the follower and the hook server.
- [ ] **Step 4: Run** step 2. Expected: PASS.
- [ ] **Step 5: Commit** `feat(camera): the relay process`.

### Task 8: The recorder process

**Files:**
- Create: `backend/scadbuddy/camera/recorder.py`, `backend/scadbuddy/camera/uploads.py`, `deploy/camera/mediamtx-recorder.yml`
- Test: `backend/tests/camera/test_recorder.py`, `backend/tests/camera/test_uploads.py`

**Interfaces:**
- Consumes: Tasks 4 and 6.
- Produces:
  - `class LocalSegment`: `path: Path`, `printer_id: str`, `started_at: datetime`, `duration_ms: int`, `bytes: int`, `init_bytes: int`, `init_sha256: str`. `ends_at` property.
  - `class Uploader`: `__init__(s3: S3Settings-like, prefix: str)`; `async put(seg: LocalSegment) -> str` (returns the object key; key per Global Constraints).
  - `class Recorder`: `async on_segment(printer_id: str, path: Path, duration_s: float) -> None` (from MediaMTX `runOnRecordSegmentComplete` with `MTX_PATH`, `MTX_SEGMENT_PATH`, `MTX_SEGMENT_DURATION`, via the local hook server on `127.0.0.1:9091`); `async tick(printer_id: str) -> None` (every 5 s per recorded printer: lease `acquire` or `renew`; if held, upload then index pending segments oldest first in batches ≤ 500; if not held, delete local segments that end before `indexed_until`); `async stop(printer_id: str) -> None` (flush if leader, `release`, delete path, feed `stopped`); `enforce_disk_cap() -> None`.
  - Handover rule (D14): on the first tick that newly holds the lease, the first segment uploaded is the local one with `started_at <= indexed_until < ends_at` (or, when none contains it, the oldest one after it); it is indexed with `discontinuity=True`. When `indexed_until` is `None`, upload everything.
  - Gap rule: a segment whose `started_at` is more than 1 s after the previous segment's `ends_at` on this recorder, or whose `init_sha256` differs from it, is `discontinuity=True`.
  - Path conf for each recorded printer: `source: rtsp://<read user>:<pass>@relay:8554/<printer_id>`, `record: yes`, `recordPath: /segments/%path/%Y-%m-%d_%H-%M-%S-%f`, `recordFormat: fmp4`, `recordPartDuration: 1s`, `recordSegmentDuration: 4s`, `recordDeleteAfter: 0s` (the controller deletes).
  - Feed reports (`part="recorder"`) on state change only: `pulling` while segments arrive, `stalled` when the leader has no new segment for 120 s, `stopped` on stop.

- [ ] **Step 1: Failing tests** (fake CRUD with real lease semantics from Task 3 in memory, fake S3 via `moto` server or an in-memory fake `Uploader`): `test_leader_uploads_before_index` (an index call never names a key not yet put); `test_standby_prunes_before_indexed_until`; `test_handover_uploads_straddling_segment`; `test_gap_marks_discontinuity`; `test_backlog_drops_oldest_at_cap` (cap reached while CRUD fails: oldest unuploaded deleted, next uploaded segment `discontinuity=True`); `test_api_down_keeps_segments` (lease calls fail: nothing deleted below cap); `test_stop_flushes_then_reports`; `test_stalled_after_120s`; `test_bucket_keys_never_logged`.
- [ ] **Step 2: Run** `uv run pytest tests/camera/test_recorder.py tests/camera/test_uploads.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `Uploader` (aiobotocore, as `store/s3.py`), `Recorder`, `main()`: check environment, `exchange_source` for `camera`, `wait_for_areas`, list sources, start paths and per-printer tick loops, run the follower (`on_change` → re-read that printer: start, replace or `stop`), the hook server, `/healthz` and `/metrics` on `9090`. On SIGTERM: for each printer it leads, flush and `release` before exit (spec §6.1).
- [ ] **Step 4: Run** step 2. Expected: PASS.
- [ ] **Step 5: Commit** `feat(camera): the recorder process with leader handover`.

### Task 9: End-to-end recording test with real MediaMTX

**Files:**
- Create: `backend/tests/camera/test_e2e_mediamtx.py`, `backend/tests/camera/conftest.py` (fixture that starts the pinned MediaMTX binary or skips when absent, as the LFS/docker-dependent tests do)
- Modify: `.github/workflows/ci.yml` (the backend pytest job fetches the pinned MediaMTX release by sha256 into `$PATH`)

**Interfaces:**
- Consumes: everything above. A publisher (`ffmpeg -re -f lavfi -i testsrc … -c:v libx264 -g 10 -f rtsp`) stands in for the printer: the relay's path for the test uses `source: rtsp://…` instead of `rtsps` (a test-only path: the test builds `Relay` with a `source_for: Callable[[CameraSourceWithCode], str]` argument that returns the plain `rtsp://` URL; `main()` always passes the `rtsps://` builder, and no setting or environment variable can change it).

- [ ] **Step 1: Write** `test_two_recorders_no_gap_on_leader_kill`: start relay, two recorders, a fake CRUD backed by `CameraStore` on the test Postgres, and the publisher; wait for 10 indexed segments; kill the leader's process; wait for 10 more; assert the indexed segments, ordered by `started_at`, have no hole longer than 1 s between one's end and the next's start, and that exactly one `discontinuity=True` appears after the kill.
- [ ] **Step 2: Run** `uv run pytest tests/camera/test_e2e_mediamtx.py -v`. Expected: PASS (it exercises code already written; a failure is a bug in Tasks 6–8, fixed there).
- [ ] **Step 3: Commit** `test(camera): gapless leader handover against real MediaMTX`.

### Task 10: Images, CI, metrics, dashboard and docs

**Files:**
- Modify: `Dockerfile` (stages `camera-relay` and `camera-recorder`: the profile-fetch stage pattern plus the pinned MediaMTX release binary checked by sha256, and `curl` in both stages for MediaMTX's hooks; the recorder stage also sets `ENV TZ=UTC`), `.github/workflows/build-image.yml` and `ci.yml` (build both, as `profile-fetch`), `.github/scripts/select-lanes.sh` (+ its test), `deploy/grafana/scadbuddy-printing.json` (a "Cameras" row: segments indexed per printer, upload lag, local backlog bytes, pull reconnects, leader per printer), `backend/CLAUDE.md` (layout: `camera/`), `docs/features/printer-cameras.md` (new feature page in the `docs/features/` format), `docs/architecture/processes.md`, `README.md` ("Deploying": the three env sets, the relay read secret, network policies), the spec §2 (phase 0's measurements, when they exist)
- Test: `.github/scripts/select-lanes.test.sh`, `.github/scripts/lint-dashboard.sh`

**Interfaces:**
- Consumes: metrics registered in Tasks 7 and 8: `scadbuddy_camera_segments_indexed_total{printer}`, `scadbuddy_camera_upload_lag_seconds{printer}`, `scadbuddy_camera_backlog_bytes{printer}`, `scadbuddy_camera_leader{printer,recorder}`, `scadbuddy_camera_relay_reconnects_total{printer}`.

- [ ] **Step 1: Add a regression pin** to `.github/scripts/select-lanes.test.sh`: "a change under `backend/scadbuddy/camera/` selects `backend` and `image`". `select-lanes.sh` already selects `backend` for every `backend/` path, so it passes at once; run `bash .github/scripts/select-lanes.test.sh`. Expected: PASS.
- [ ] **Step 2: Build** `docker build --target camera-relay -t camera-relay:dev . && docker build --target camera-recorder -t camera-recorder:dev .`. Expected: both build; `docker run --rm camera-recorder:dev mediamtx --version` prints the pinned version.
- [ ] **Step 3: Run** `KUSTOMIZE=<v5.8.1 path> bash .github/scripts/lint-dashboard.sh`. Expected: PASS with the new panels.
- [ ] **Step 4: Write** the docs listed; every fact cites its source.
- [ ] **Step 5: Commit** `feat(camera): images, CI lanes, dashboard and docs for camera recording`.
- [ ] **Step 6: Deployment** is in eh-homelab/clusters, not this repo: open an issue there (with the owner's approval) listing the three Deployments (`scadbuddy-camera-relay` 1 replica `Recreate` on the node pool that reaches the printer LAN; `scadbuddy-camera-recorder` 2 replicas, pod anti-affinity, `emptyDir` with `sizeLimit` = `SCADBUDDY_CAMERA_DISK_MAX_BYTES` + 25%; `scadbuddy-camera-edge` deferred to phase 3), the ServiceAccounts for the k8s proof (`sub` `camera`, `camera-relay`), the bucket keys for the recorders, the relay read secret, and NetworkPolicies (relay 8554 from recorders only; relay egress to the printer LAN; recorders egress to relay, API, bucket).
