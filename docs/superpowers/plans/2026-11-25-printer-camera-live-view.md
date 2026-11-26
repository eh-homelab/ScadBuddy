# Printer camera live view (camera phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Changes during execution

These decisions replace the task text below. The task bodies are kept as written; where they disagree, this list and the code are correct.

- **Edge read password.** The edge's HLS and WHEP ports need a read password: an `any` user with no password (Global Constraints, Task 3 Config) was replaced by the edge read user (`MTX_AUTHINTERNALUSERS_0_*`), and the API sends it as HTTP Basic auth. The API reads four settings: `SCADBUDDY_CAMERA_EDGE_WHEP_URL`, `SCADBUDDY_CAMERA_EDGE_HLS_URL`, `SCADBUDDY_CAMERA_EDGE_READ_USER` and `SCADBUDDY_CAMERA_EDGE_READ_PASS`. The NetworkPolicy stays: a request that names an HLS session id passes without the password.
- **Pull lifetime.** The edge sets `hlsMuxerCloseAfter: 10s`. The e2e checks that the relay loses the edge's reader within 60 s of the last request, not 20 s (Task 3 e2e; measured 48.5 s).
- **HLS query keys.** The proxy passes `_HLS_msn`, `_HLS_part`, `_HLS_skip`, `session` and `cookieCheck` (Task 4). MediaMTX names the session in `session` and checks cookies with `cookieCheck`.
- **HLS name regex.** The proxy uses `[A-Za-z0-9_-]{1,128}\.(?:m3u8|mp4|m4s)`, from the names MediaMTX v1.21.4 serves (`index.m3u8`, `video1_stream.m3u8`, `<hex>_video1_seg<N>.mp4` …), not the Task 4 regex.
- **`camera_live`.** It is true when the printer is recorded and all four edge settings are set (`Settings.camera_edge_ready()`), not only the edge URL (Task 5).

**Goal:** Show each recorded printer's live camera in ScadBuddy without Bambuddy: a live edge reads the relay, the API proxies WHEP and LL-HLS on ScadBuddy's own origin, and the camera dialog plays it, with the feed state and a "Reconnect" button.

**Architecture:** A third camera deployment, `scadbuddy-camera-edge`, runs MediaMTX (WebRTC and LL-HLS, reading each printer's path from the relay on demand) and a small controller that follows the camera change stream with `crud.camera`. The API proxies the browser's WHEP exchange and HLS requests to the edge under `/api/v1/camera/live/...`, after its session check. The browser tries WebRTC first and falls back to LL-HLS (hls.js) when ICE does not connect, so phase 3 works whether or not the cluster lets WebRTC media reach browsers. A session route reads the feed state; a reconnect route emits `camera.reconnect`, which makes the relay restart that printer's pull.

**Tech Stack:** Python 3.12, FastAPI, httpx, MediaMTX v1.21.4 (already pinned), React + TypeScript, hls.js, msw, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-23-printer-camera-recording-design.md` (§6.1 "Live edge", §6.7, §6.8, §6.9 "Manual reconnect", §9 phase 3). Issue #7330, epic #7334.

## Global Constraints

- Only the API holds SQL. The edge holds no database secret, no access code, no bucket keys, and runs no untrusted code. It refuses to start with `SCADBUDDY_DATABASE_URL`, `PGPASSWORD`, `PGPASSFILE`, `SCADBUDDY_SECRET_KEY_FILE` or `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` set (exit 2), as the relay and recorder do (`camera/settings.py` `_forbidden`).
- The edge uses the existing `crud.camera` credential: ServiceAccount `scadbuddy-camera-edge` maps to caller `camera` in `auth/exchange.py` `SERVICE_ACCOUNTS`. No new caller, no migration.
- The browser reaches live video only on ScadBuddy's origin, under `/api/v1/camera/live/{printer_id}/...` (spec §6.7). No CORS change. The API never forwards the browser's cookies or `Authorization` header to the edge.
- MediaMTX's control API (9997) and metrics (9998) listen on `127.0.0.1` only in the edge too. The edge's HTTP ports (WHEP 8889, HLS 8888) admit only the API (a NetworkPolicy in eh-homelab/clusters; the edge config sets no read password for them).
- The edge reads the relay with the relay read secret (`SCADBUDDY_CAMERA_RELAY_READ_USER`/`_PASS`, as the recorders do), and each path uses `sourceOnDemand: yes`, so the edge pulls from the relay only while someone watches.
- Printer ids in a live URL are the ScadBuddy printer UUID. The API answers 404 for a printer that is not recorded (`CameraStore` `_sources` decides, as the CRUD `/printers/{id}` route does). A path segment after the id is checked against a fixed allowlist of shapes (below) before any proxy call.
- The Bambuddy MJPEG routes stay (spec §6.7). The dialog uses the new source when the printer is recorded and falls back to Bambuddy's MJPEG otherwise.
- MediaMTX v1.21.4 only; every new MediaMTX fact in docs says how it was measured (CLAUDE.md #259).
- Docs in Simplified Technical English. Never commit `backend/openapi.json` or `schema.d.ts`.

## Review Focus

1. A viewer on a network where WebRTC UDP is blocked: the player must switch to LL-HLS within 6 s and play, not spin.
2. Closing the dialog (or switching printers) must end the WHEP session (`DELETE` on the session URL) and stop hls.js, so the edge's reader count drops and the on-demand pull stops.
3. A printer that is not recorded, or a path like `../`, `%2e%2e` or a second printer's id inside the HLS path: 404 before any proxy call.
4. The edge or relay down: the dialog shows "Camera unavailable" with the feed state and the Reconnect button, never an endless spinner (a 10 s cap per attempt).
5. HLS blocking playlist requests (`_HLS_msn`): the proxy must stream and allow at least 15 s for a reply, or LL-HLS stalls.

---

### Task 1: Feed state and reconnect, for the browser

**Files:**
- Modify: `backend/scadbuddy/core/events.py` (new `CameraReconnectEvent`, add it to `Event`)
- Modify: `backend/scadbuddy/api/realtime.py` (topics for the new event, beside `CameraFeedEvent`)
- Create: `backend/scadbuddy/api/camera.py` (session routes; register the router where the other `/api/v1` routers are)
- Modify: `backend/scadbuddy/camera/store.py` (a read of one printer's camera state)
- Test: `backend/tests/api/test_camera_routes.py`

**Interfaces:**
- Produces: `class CameraReconnectEvent(BaseEvent)`, `kind: Literal["camera.reconnect"]`, `printer_id: str`.
- Produces: `GET /api/v1/printers/{printer_id}/camera` → `PrinterCamera {recorded: bool, live: bool, feeds: list[CameraFeed]}`, `CameraFeed {part: Literal["relay","recorder"], state: FeedState, detail: str | None, updated_at: datetime}`. `recorded` is true when the printer qualifies (`_sources`); `live` is true when `recorded` and all four of `SCADBUDDY_CAMERA_EDGE_WHEP_URL`, `SCADBUDDY_CAMERA_EDGE_HLS_URL`, `SCADBUDDY_CAMERA_EDGE_READ_USER` and `SCADBUDDY_CAMERA_EDGE_READ_PASS` are set. 404 `printer-not-found` for an unknown printer.
- Produces: `POST /api/v1/printers/{printer_id}/camera/reconnect` → 202, emits `CameraReconnectEvent`. 409 `camera-not-recorded` when the printer is not recorded. Needs the same permission as other printer actions (follow `api/printers.py`'s patch route).

- [ ] **Step 1: Write the failing tests**: `test_camera_state_lists_both_feeds` (two `camera_feeds` rows → both in `feeds`, `recorded` true), `test_camera_state_for_a_printer_without_a_stream` (`recorded` false, `feeds` empty), `test_reconnect_emits_the_event` (a fake bus records one `CameraReconnectEvent` with the id), `test_reconnect_refuses_a_printer_not_recorded` (409), `test_camera_reconnect_passes_the_change_stream` (the CRUD `/camera/changes` stream carries `camera.reconnect`; `CAMERA_KINDS` already lists it), `test_live_is_false_without_an_edge_url`.
- [ ] **Step 2: Run them; expect failures** (`uv run pytest tests/api/test_camera_routes.py -q`).
- [ ] **Step 3: Implement** the event, its realtime topics (`["printers", f"printer:{id}"]`), the store read and the two routes.
- [ ] **Step 4: Run the tests and the camera suite; expect pass.**
- [ ] **Step 5: Commit** `feat(camera): feed state and reconnect routes for the browser (#7330)`.

### Task 2: The relay restarts a pull on `camera.reconnect`

**Files:**
- Modify: `backend/scadbuddy/camera/follow.py` (`ChangeFollower` takes an optional `on_reconnect: Callable[[str], Awaitable[None]] | None = None`; a `camera.reconnect` change calls it when set, else `on_change`)
- Modify: `backend/scadbuddy/camera/relay.py` (`Relay.restart(printer_id)`: remove the MediaMTX path, then `reconcile(printer_id)`; wire it as `on_reconnect`)
- Test: `backend/tests/camera/test_follow.py`, `backend/tests/camera/test_relay.py`

**Interfaces:**
- Consumes: `CameraReconnectEvent` from Task 1 (payload `kind`, `printer_id`).
- Produces: `ChangeFollower(..., on_reconnect=...)`; `Relay.restart(printer_id: str) -> None`. Recorders and the edge pass no `on_reconnect`, so a reconnect is a plain reconcile for them.

- [ ] **Step 1: Failing tests**: `test_a_reconnect_change_calls_on_reconnect`, `test_a_reconnect_without_a_handler_is_a_change`, `test_restart_removes_then_puts_the_path` (the fake control API sees DELETE then PUT for that path, and no other path is touched), `test_restart_of_a_printer_no_longer_recorded_only_removes`.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.** Keep `_printer_id`'s checks; return the kind too.
- [ ] **Step 4: Run the camera suite; expect pass.**
- [ ] **Step 5: Commit** `feat(camera): the relay restarts a printer's pull on reconnect (#7330)`.

### Task 3: The live edge

**Files:**
- Create: `deploy/camera/mediamtx-edge.yml`
- Create: `backend/scadbuddy/camera/edge.py` (controller, `python -m scadbuddy.camera.edge`)
- Modify: `backend/scadbuddy/camera/settings.py` (`EdgeSettings`, with `check_environment`)
- Modify: `backend/scadbuddy/auth/exchange.py` (`"scadbuddy-camera-edge": "camera"`)
- Modify: `Dockerfile` (target `camera-edge` after `camera-recorder`: copies `mediamtx-edge.yml` to `/etc/mediamtx/mediamtx.yml`, `EXPOSE 8888 8889 8189/udp 9090`, `CMD ["python","-m","scadbuddy.camera.edge"]`)
- Modify: `.github/workflows/build-image.yml` (`camera` matrix `part: [relay, recorder, edge]`; only one leg writes the `camera` cache scope, or each leg its own, matching what the #7407/#7408 PR does to that job), `.github/workflows/ci.yml` (`camera-image`: build `camera-edge`, add it to the smoke loop)
- Test: `backend/tests/camera/test_edge.py`, `backend/tests/camera/test_settings.py`, and an edge leg in `backend/tests/camera/test_e2e_mediamtx.py`

**Interfaces:**
- Consumes: `ChangeFollower`, `MediaMtx` (`camera/mediamtx.py`), `process.serve_health` (`/healthz`, `/readyz`, `/metrics` on 9090), the CRUD `camera` routes `GET /printers`, `GET /printers/{id}`, `GET /changes`.
- Produces: `class Edge` with `reconcile(printer_id) / reconcile_all()`; each recorded printer gets path `<printer_id>` with `source: rtsp://<read user>:<pass>@<SCADBUDDY_CAMERA_RELAY_RTSP_URL host>/<printer_id>`, `sourceOnDemand: yes`, `sourceOnDemandCloseAfter: 10s`, `rtspTransport: tcp`. Gauge `scadbuddy_camera_viewers{printer}`, read from MediaMTX's `GET /v3/paths/list` `readers` at scrape time (a custom collector, not a poll loop).
- Config `mediamtx-edge.yml`: as the relay's for `logLevel`, `readTimeout`, `api`/`metrics` on 127.0.0.1, the localhost `any` user with `api` and `metrics`; `rtsp: no`, `rtmp: no`, `srt: no`, `moq: no`; `webrtc: yes`, `webrtcAddress: :8889`, `webrtcLocalUDPAddress: :8189`, `webrtcLocalTCPAddress: ""`, `webrtcIPsFromInterfaces: yes`, `webrtcAdditionalHosts: []` (clusters sets `MTX_WEBRTCADDITIONALHOSTS` to the address browsers reach); `hls: yes`, `hlsAddress: :8888`, `hlsVariant: lowLatency`, `hlsAlwaysRemux: no`; an `any` user with `read` permission and no password (the NetworkPolicy is the boundary, Global Constraints); `paths: {}`.
- Env: `SCADBUDDY_API_INTERNAL_URL`, `SCADBUDDY_SERVICE_TOKEN_FILE`, `SCADBUDDY_LOG_LEVEL`, `SCADBUDDY_CAMERA_RELAY_RTSP_URL`, `SCADBUDDY_CAMERA_RELAY_READ_USER`, `SCADBUDDY_CAMERA_RELAY_READ_PASS`.

- [ ] **Step 1: Failing tests**: `test_reconcile_puts_an_on_demand_path_from_the_relay`, `test_reconcile_removes_a_printer_no_longer_recorded`, `test_reconcile_all_drops_unknown_paths`, `test_the_read_password_is_never_logged` (caplog), `test_edge_refuses_a_database_secret` (exit 2), `test_viewers_gauge_reads_readers`, and in the e2e: a WHEP-free check that `GET http://<edge>:8888/<printer_id>/index.m3u8` answers 200 within 15 s while ffmpeg publishes, and the relay path shows one reader then none 20 s after the last request.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement** (reuse the relay's reconcile shape; the edge never deletes a path a viewer reads except when the printer stops being recorded).
- [ ] **Step 4: Run** the camera suite, `docker build --target camera-edge .`, the smoke commands from `ci.yml`, actionlint and hadolint as the `lint` job runs them.
- [ ] **Step 5: Commit** `feat(camera): the live edge (#7330)`.

### Task 4: The API's live proxy

**Files:**
- Modify: `backend/scadbuddy/api/camera.py` (Task 1's router)
- Modify: the API settings module that holds the other internal URLs (add `SCADBUDDY_CAMERA_EDGE_WHEP_URL`, `SCADBUDDY_CAMERA_EDGE_HLS_URL`, `SCADBUDDY_CAMERA_EDGE_READ_USER` and `SCADBUDDY_CAMERA_EDGE_READ_PASS`, all unset by default; the password a `SecretStr`)
- Modify: `backend/scadbuddy/auth/routes.py` (`SCRIPTLESS_GETS` gets the HLS GET route, so hls.js and Safari's native `<video>` pass on the cookie, as the MJPEG route does)
- Test: `backend/tests/api/test_camera_live.py`

**Interfaces:**
- Produces:
  - `POST /api/v1/camera/live/{printer_id}/whep` (body `application/sdp`, ≤ 64 KiB) → forwards to `{edge}/{printer_id}/whep`; answers the edge's status, `application/sdp` body, and `Location` rewritten from the edge's `/{printer_id}/whep/{session}` to `/api/v1/camera/live/{printer_id}/whep/{session}`. Also passes `ETag` and `Link` (ICE servers) headers.
  - `PATCH` and `DELETE /api/v1/camera/live/{printer_id}/whep/{session}` (session `[A-Za-z0-9-]{1,64}`) → forwarded alike (`application/trickle-ice-sdpfrag` for PATCH).
  - `GET /api/v1/camera/live/{printer_id}/hls/{name}` where `name` matches `^(index|main_stream|stream)?[A-Za-z0-9_-]*\.(m3u8|mp4|m4s)$` and holds no `/`; query string passed through (`_HLS_msn`, `_HLS_part`, `_HLS_skip` only). Streams the edge's body (`StreamingResponse`, as the MJPEG route closes its client), `Cache-Control: no-store`, read timeout 20 s.
  - 503 `camera-edge-unset` when the route's edge URL (`SCADBUDDY_CAMERA_EDGE_WHEP_URL` for WHEP, `SCADBUDDY_CAMERA_EDGE_HLS_URL` for HLS) or the read user (`SCADBUDDY_CAMERA_EDGE_READ_USER`/`_PASS`) is unset; 404 when the printer is not recorded; 502 `camera-edge-unreachable` when the edge does not answer.
- [ ] **Step 1: Failing tests** with an `httpx.MockTransport` edge: the offer reaches the edge's path; `Location` is rewritten; a `../x` or a foreign query key never reaches the edge (404 / dropped); no `Cookie` or `Authorization` header reaches the edge; a blocking playlist that answers after 12 s still succeeds; the edge down gives 502; unset URL gives 503; signed-out gives 401.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.** One `httpx.AsyncClient` per request, as `client_for` does.
- [ ] **Step 4: Run the tests; expect pass.** Also `ruff`, `mypy`, and the API export (`uv run python -m scadbuddy.export_openapi` as backend/CLAUDE.md says) so the frontend types exist.
- [ ] **Step 5: Commit** `feat(camera): proxy WHEP and LL-HLS to the live edge (#7330)`.

### Task 5: The live player and the camera dialog

Load the `frontend-design` skill first (spec §10: the UI layout is the builder's).

**Files:**
- Modify: `frontend/package.json` (`hls.js`, exact version, a release at least two weeks old)
- Create: `frontend/src/components/prints/LivePlayer.tsx` (+ `LivePlayer.test.tsx`)
- Modify: `frontend/src/api/client.ts` (`printerCamera(id)`, `reconnectPrinterCamera(id)`, `cameraLiveUrl(id)`)
- Modify: `frontend/src/components/prints/NowPrinting.tsx` (`CameraDialog`/`CameraView`; the Camera button on `PrinterCard`)
- Modify: `backend/scadbuddy/bambuddy/printers_now.py` (`PrinterNow.camera_live: bool = False`, true when the printer is recorded and the edge URL is set), and the msw mocks in `frontend/src/mocks/features/` for the new routes
- Test: `frontend/e2e/camera-live.spec.ts`

**Interfaces:**
- Consumes: Tasks 1 and 4 routes.
- Produces: `<LivePlayer printerId={string} onState={(s: 'connecting'|'webrtc'|'hls'|'failed') => void} />`.
- Behaviour:
  - WebRTC first: `RTCPeerConnection` with one recvonly video transceiver (and audio if the offer has it), POST the offer to `/whep`, set the answer. If `iceConnectionState` is not `connected` within 6 s, or the POST fails, close it (DELETE the session if one was made) and start hls.js on `/hls/index.m3u8` (native HLS when `video.canPlayType('application/vnd.apple.mpegurl')` and hls.js is unsupported). If HLS has no frame within 10 s, state `failed`.
  - Unmount or printer change: DELETE the WHEP session, close the peer connection, `hls.destroy()`.
  - `<video muted playsInline autoPlay>`; a small badge shows "WebRTC" or "HLS".
- Dialog: when `camera_live` is true, `CameraView` uses `LivePlayer`; otherwise the current Bambuddy MJPEG view, unchanged. The side panel shows each feed's state from `GET /printers/{id}/camera` (re-read on the `printer:<id>` realtime hint that `camera.feed` already emits) and a "Reconnect" button that calls the reconnect route and shows the 409 problem's text when refused. The Camera button shows when `camera_live` or `printer_id !== null`.
- [ ] **Step 1: Failing unit tests** (`LivePlayer.test.tsx` with a fake `RTCPeerConnection` and a fake hls.js): falls back to HLS after 6 s; DELETEs the session on unmount; `failed` after the HLS cap. Dialog tests: the feed states show; Reconnect calls the route; a printer without `camera_live` keeps the MJPEG `<img>`.
- [ ] **Step 2: Run; expect failures** (`pnpm gen:api` first, then `pnpm exec vitest run <files>`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm typecheck`, `pnpm lint`, `pnpm test`, and the Playwright spec (`camera-live.spec.ts`: with mocks, the dialog opens, falls back to HLS against a mocked playlist route, shows the feed states, and Reconnect posts once).
- [ ] **Step 5: Commit** `feat(camera): live view in the camera dialog (#7330)`.

### Task 6: Docs, dashboard and the clusters draft

**Files:**
- Modify: `README.md` "Printer cameras (#7328)" (the edge: image, env, ports 8888/8889/8189 UDP, `MTX_WEBRTCADDITIONALHOSTS`, the NetworkPolicy, the API's `SCADBUDDY_CAMERA_EDGE_WHEP_URL`, `SCADBUDDY_CAMERA_EDGE_HLS_URL`, `SCADBUDDY_CAMERA_EDGE_READ_USER` and `SCADBUDDY_CAMERA_EDGE_READ_PASS`; remove "the live edge comes in phase 3")
- Modify: `docs/features/printer-cameras.md` (live view, reconnect, the fallback, tests)
- Modify: `deploy/grafana/scadbuddy-printing.json` (a "Live viewers" panel on `scadbuddy_camera_viewers`, in the Cameras row; `lint-dashboard.sh` passes)
- Modify: the spec §6.7 only if a measurement here changes it (with date and method)
- [ ] **Step 1:** Write the docs; grep for every "phase 3" and "live edge" mention made stale.
- [ ] **Step 2:** Run `lint-dashboard.sh` (with `KUSTOMIZE` as CLAUDE.md says) and markdownlint as the `markdown` job runs it.
- [ ] **Step 3:** Put the eh-homelab/clusters draft for `scadbuddy-camera-edge` (Deployment, Service, ServiceAccount, NetworkPolicies, how browsers reach UDP 8189 or that HLS alone is used, the API's `SCADBUDDY_CAMERA_EDGE_WHEP_URL`, `SCADBUDDY_CAMERA_EDGE_HLS_URL`, `SCADBUDDY_CAMERA_EDGE_READ_USER` and `SCADBUDDY_CAMERA_EDGE_READ_PASS`) in the PR body, as a comment on eh-homelab/clusters#4331 after merge.
- [ ] **Step 4: Commit** `docs(camera): the live view (#7330)`.
