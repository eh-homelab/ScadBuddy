# Printer cameras

## What it does

ScadBuddy records the camera of each printer that qualifies, all the time, as fMP4 segments
of about 4 s in the blob store, with an index in Postgres. Phase 1 of
[#7334](https://github.com/eh-homelab/ScadBuddy/issues/7334) ([#7328](https://github.com/eh-homelab/ScadBuddy/issues/7328))
records and indexes only. Per-print video, live view, timelapses and retention are phases
2 to 5 (spec
[§9](../superpowers/specs/2026-11-23-printer-camera-recording-design.md#9-phases-each-one-issue-under-one-new-epic)).

A printer qualifies when all three are true (`camera/store.py` `CameraStore.sources`):

- its LAN link has a sealed access code;
- its last LAN report's `ipcam.rtsp_url` starts with `rtsps://`;
- its toggle `printers.camera_recording` is on. The default is on. The API's
  `PATCH /api/v1/printers/{id}` sets it (`printers/models.py` `PrinterPatch`). Phase 1
  has no UI for it.

## How it works

Two processes do the work. Each runs beside its own MediaMTX in one pod. The design is in
the spec
[§6.1](../superpowers/specs/2026-11-23-printer-camera-recording-design.md#61-parts); the
plan is `docs/superpowers/plans/2026-11-23-printer-camera-recording.md`.

```mermaid
flowchart LR
    PR[Printer RTSPS :322] -->|pull, access code| RM[relay MediaMTX :8554]
    RC[relay controller] -->|control API 127.0.0.1:9997| RM
    RM -->|hooks, curl 127.0.0.1:9091| RC
    RC -->|crud.camera-relay| API
    RM -->|RTSP, read user| M1[recorder 1 MediaMTX] & M2[recorder 2 MediaMTX]
    M1 -->|/segments| C1[recorder 1 controller]
    M2 -->|/segments| C2[recorder 2 controller]
    C1 & C2 -->|crud.camera: lease, index, feed| API
    C1 & C2 -->|put segment| S3[(Bucket)]
```

- **The relay** (`python -m scadbuddy.camera.relay`, `camera/relay.py`) is the only part
  that holds access codes. It reads the qualifying printers from
  `/api/v1/internal/crud/camera-relay/printers`, the only routes that return a code
  (`api/crud_camera_relay.py`). It gives MediaMTX one RTSPS pull per printer through the
  control API, never through a file. The path name is the printer id. MediaMTX's
  `runOnAvailable` and `runOnUnavailable` hooks call the relay's hook server, and the relay
  reports the feed state (`pulling`, `retrying`) to the API.
- **The recorders** (`python -m scadbuddy.camera.recorder`, `camera/recorder.py`) run as two
  pods. Each MediaMTX reads every recorded printer from the relay with the read user and
  writes fMP4 segments under `/segments`. MediaMTX's `runOnRecordSegmentComplete` hook calls
  the controller. For each printer, the recorder that holds the lease (`camera_leases`,
  20 s, renewed every 5 s) is the leader. The leader puts each segment in the bucket, oldest
  first, then indexes it (`POST …/crud/camera/printers/{id}/segments`). The standby keeps the
  segments that the index has not reached yet, so a handover leaves no gap. The object key is
  `<prefix>/camera/<printer_id>/<yyyy>/<mm>/<dd>/<HHMMSS.ffffff>.mp4`, by the segment's UTC
  start (`camera/uploads.py`).
- **Changes.** Both controllers follow a filtered change stream
  (`GET …/crud/camera[-relay]/changes`, `camera/follow.py`). On `resync` they read the full
  list again; they never act on a stale row.
- **The two containers of a pod.** Each pod runs its image twice: MediaMTX
  (`mediamtx /etc/mediamtx/mediamtx.yml`) and the controller (the image's default command).
  This keeps one process per container. The controller reaches MediaMTX's control API on
  `127.0.0.1:9997`, and MediaMTX's hooks run `curl` against the controller's hook server on
  `127.0.0.1:9091` (`camera/process.py` `HOOK_PORT`), so both containers must share the pod's
  network. The recorder's two containers also share `/segments`, an `emptyDir`.
- **Health.** Each controller answers `/healthz` and `/metrics` on port 9090. `/healthz`
  answers 503 when MediaMTX's control API does not answer, or when the change follower has
  stopped (`camera/process.py` `healthy`). Before the follower starts (the controller waits
  for the CRUD service, or reads its first list), only MediaMTX counts, because a restart
  cannot bring the API back. A liveness probe on `/healthz` restarts the controller's
  container only; MediaMTX's container needs its own probe (README ["Printer cameras (#7328)"](../../README.md#printer-cameras-7328)).

### MediaMTX facts (v1.21.4)

The release is pinned by version and sha256 in the Dockerfile's `mediamtx` stage. The
facts the code relies on, with how each was measured, are in the spec
[§2](../superpowers/specs/2026-11-23-printer-camera-recording-design.md#2-measured-and-known-facts).
The official image `bluenviron/mediamtx:1.21.4` has one layer and the entrypoint
`/mediamtx`, and has no `sh` and no `curl` (measured 2026-11-24 with `docker image inspect`
and `docker run --entrypoint sh`/`curl`, which both fail with "executable file not found").
The hooks need `curl`, so the camera images take the release binary on
`python:3.12-slim-bookworm` with `curl` from apt.

## Code

- Processes: `backend/scadbuddy/camera/relay.py`, `recorder.py`, shared `process.py` (hook
  server, `/healthz`, `main_with`), `follow.py`, `feed.py`, `mediamtx.py` (control API
  client), `uploads.py` (bucket), `mp4.py` (init section parse), `settings.py`
  (`RelaySettings`, `RecorderSettings`).
- API side: `backend/scadbuddy/camera/store.py` (`CameraStore`), `models.py`,
  `backend/scadbuddy/api/crud_camera.py`, `api/crud_camera_relay.py`; the callers in
  `auth/exchange.py` `SERVICE_ACCOUNTS` (`scadbuddy-camera`, `scadbuddy-camera-relay`).
- MediaMTX configs: `deploy/camera/mediamtx-relay.yml`, `deploy/camera/mediamtx-recorder.yml`.
- Images: Dockerfile stages `mediamtx`, `camera-deps`, `camera`, `camera-relay`,
  `camera-recorder`; published by `build-image.yml` job `camera`, checked by `ci.yml` job
  `camera-image`.

## Data

- Tables: `camera_segments`, `camera_leases`, `camera_feeds`, and the column
  `printers.camera_recording` (`backend/scadbuddy/migrations/20261124T0059Z_camera_recording.sql`).
- Events: `camera.feed` (a feed state changed) and `camera.reconnect` (`core/events.py`).
- Blobs: one object per segment, under `<prefix>/camera/` in the camera bucket. Only the
  recorder pods hold its keys.
- Metrics (recorders, `camera/recorder.py`): `scadbuddy_camera_segments_indexed_total`,
  `scadbuddy_camera_upload_lag_seconds`, `scadbuddy_camera_backlog_bytes`,
  `scadbuddy_camera_leader`. The "Cameras" row of `deploy/grafana/scadbuddy-printing.json`
  shows them.

## Tests

- Backend: `backend/tests/camera/` (relay, recorder, follower, uploads, mp4, store,
  settings, `/healthz` in `test_process.py`), `backend/tests/api/test_crud_camera.py`,
  `test_crud_camera_changes.py`.
- Real MediaMTX: `backend/tests/camera/test_e2e_mediamtx.py`. ffmpeg publishes a stream, and
  real MediaMTX processes on the `deploy/camera/` configs run the relay and two recorders with
  their `curl` hooks; the test kills the leader and checks that the index has no gap. The
  Dockerfile's `test` stage has ffmpeg and MediaMTX, so it fails there instead of skipping.
- Images: `ci.yml` job `camera-image` (MediaMTX's version, `curl`, `TZ=UTC`, the refusals,
  MediaMTX on each config, and the relay's read user from `MTX_AUTHINTERNALUSERS_0_*`).

## Limits and known issues

- Phase 0's stream measurements (codec, frame rate, bitrate, keyframe interval, audio, how
  many RTSP clients the H2C accepts) are not made yet (spec §2).
- The relay registers no metric. The spec's pull state and reconnects per printer (spec
  [§6.9](../superpowers/specs/2026-11-23-printer-camera-recording-design.md#69-operation))
  are not built, so the dashboard has no relay panel.
- The recorder does not check its clock against the API's `Date` header yet (spec §6.9).
- When MediaMTX's container restarts and the controller's does not, MediaMTX has no paths
  until the controller's next full resync.
- No retention: nothing is deleted until phase 5.
- Deployment lives in eh-homelab/clusters; the issue that asks for it is drafted with the
  phase 1 PR.
