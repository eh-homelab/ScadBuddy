# Printer cameras: always-on recording, per-print video and timelapses (design)

Status: draft for the owner's review, 2026-11-23. Brainstormed in this session. This is the
first of three designs for printer monitoring. The other two (printer alerts, and a printer
dashboard as the home page that links to inventory epic #6949) come after it and use what
this one builds.

## 1. Intent

ScadBuddy reads each printer's camera itself, records it all the time, and makes each
print's video and timelapse from that recording. Bambuddy and the printer do none of it.

Why:

- The owner wants ScadBuddy free of Bambuddy for printer monitoring (the direction of
  epic #3818).
- Bambuddy decodes the printer's H.264 and re-encodes it as MJPEG at 5 fps
  (`/app/backend/app/services/camera.py` in the Bambuddy 1.3.x image, read 2026-11-23 with
  `kubectl -n bambuddy exec deploy/bambuddy -- grep`). That costs CPU and bandwidth, and the
  feed is choppy.
- The printer's own timelapse lives on the printer's storage. Today a person or a script
  pulls it through Bambuddy (`POST /prints/{archive_id}/timelapse/pull`) before an order
  attempt can copy it. The owner wants timelapses made on ScadBuddy's infrastructure.
- A continuous recording also shows what happened between prints: a plate taken off, a
  failure in the prep stage, a pause on an HMS error.

## 2. Measured and known facts

- The H2C reports its stream in its LAN report: `ipcam.rtsp_url =
  "rtsps://172.16.0.187:322/streaming/live/1"`
  (`.claude/skills/h2c-print-from-scadbuddy/evals/decode-mapping-from-printer-report/files/printer-report-boys1.json:953`).
  The login is user `bblp` with the printer's LAN access code.
- Bambuddy's tests say that the X1, H2 and P2S models serve RTSPS on port 322, and that the
  A1 and P1 models serve JPEG frames over TLS on port 6000
  (`/app/backend/tests/unit/services/test_virtual_printer.py:4409` in the Bambuddy image,
  read 2026-11-23).
- The shop runs the H2C now and plans the H3C (inventory spec §3). Both are RTSPS models.
- **Resolution: 1080p**, from the H2C's own LAN report (`ipcam.resolution`,
  `.claude/skills/h2c-print-from-scadbuddy/evals/decode-mapping-from-printer-report/files/printer-report-boys1.json:952`).
- **Not measured yet:** the H2C stream's codec, frame rate, bitrate, keyframe
  interval, whether it has audio, and how many RTSP clients the printer accepts at one time.
  The X1 family sends H.264; the H2C is expected to do the same. Phase 0 measures all of
  these with `ffprobe` and writes them here, with the date and the method.
- At 2 to 4 Mbit/s, one printer makes about 20 to 45 GB a day. Phase 0 replaces this
  estimate with the measured bitrate.

## 3. Goals

- G1. **One pull per printer, always on.** One process pulls each RTSPS-camera printer's
  stream. It copies the H.264 unchanged; nothing re-encodes it.
- G2. **Continuous recording to the bucket.** The stream is cut into short fMP4 segments at
  keyframes and written to the S3-compatible bucket (DigitalOcean Spaces on prod), with an
  index of every segment in Postgres.
- G3. **Per-print HLS video.** Each print gets an HLS playlist over the segments between its
  start and its end. The playlist ends with `EXT-X-ENDLIST` when the print ends. No bytes
  are copied or re-encoded for a print.
- G4. **Live view** from the same pull, with low latency, for any number of viewers. The
  dashboard design uses it.
- G5. **Timelapses on our side.** After a print, ScadBuddy takes one frame at each layer
  change from the recording and encodes a timelapse video. It becomes the print's
  `timelapse` attachment.
- G6. **Configurable retention**, with deletion that can be turned off completely (§6.6).
- G7. **Room for restreaming later.** The design must not block a later push to YouTube,
  TikTok or Facebook, or an OBS pull.

## 4. Non-goals

- Restreaming to YouTube, TikTok, Facebook or OBS. It is a later wish; G7 only keeps the
  door open.
- Cameras on A1 and P1 printers (the port-6000 JPEG protocol). The shop runs none. The
  recorder's source is one interface so that a later source can be added.
- External (third-party) cameras.
- Failure detection from the image (spaghetti detection). A later design can read the same
  segments.
- Printer alerts and the dashboard: their own designs.
- Audio.

## 5. Anti-goals

- Any re-encode on the live or recording path. Only the timelapse encode decodes video,
  once per print, after the print.
- A copy of the video per print. A print's video is a playlist, not a file.
- A Bambuddy call anywhere in camera, recording or timelapse code.
- Deletion of a segment that a kept print still uses.
- A database secret or untrusted code in any camera pod (CLAUDE.md "Database access").

## 6. Design

### 6.1 Parts

One pull from each printer, multiplexed inside the cluster (owner's decision, option A):

```
printer ──1 RTSPS pull──▶ relay (scadbuddy-camera-relay, 1 replica)
                            │ RTSP inside the cluster: fan-out, no re-encode
              ┌─────────────┼───────────────┐
              ▼             ▼               ▼
        recorder A     recorder B      live edge (scadbuddy-camera-edge)
        (leader)       (standby)        WebRTC / LL-HLS to viewers
              │  (scadbuddy-camera-recorder, 2 replicas)
              ├──segments──▶ bucket (Spaces)
              └──index (CRUD route)──▶ API ──playlists, flows, retention sweep
```

- **Relay** (deployment `scadbuddy-camera-relay`, one replica, `Recreate`). MediaMTX plus a
  small controller.
  - It is the only part that talks to a printer: one RTSPS pull per printer, pinned by the
    certificate fingerprint ScadBuddy already pins at LAN link. It re-serves each stream
    over RTSP inside the cluster to any number of readers.
  - It does not record, upload or serve viewers.
  - Its controller follows the change stream (§6.2), gets the access codes from
    `GET /api/v1/internal/crud/camera-relay/printers` with the `crud.camera-relay` credential, and writes
    MediaMTX's paths through MediaMTX's local control API. The access codes stay in this
    pod's memory only.
  - It runs a pinned MediaMTX and almost no ScadBuddy code, and it is not rolled with
    ScadBuddy's releases (its image changes only when its own code or MediaMTX changes).
    So it restarts almost never. A relay restart is the one planned cause of a gap: the
    pod restart plus the RTSPS reconnect, about 10 to 30 seconds. A standby relay with a
    lease (option B) can be added later if relay restarts happen in practice.
- **Recorders** (deployment `scadbuddy-camera-recorder`, two replicas, on different
  nodes). Each runs MediaMTX, which reads RTSP from the relay and records fMP4 segments
  with no re-encode, and the recorder controller (Python, D7), which uploads, indexes and
  holds the leases.
  - **Leader and standby, per printer.** The leader of a printer holds its lease, a row
    behind a CRUD route (D13). Its MediaMTX cuts segments at keyframes; its controller
    uploads them, indexes them (§6.3) and then deletes the local files.
  - The standby also records the stream from the relay (free inside the cluster; the
    printer does not see it) to its local disk, uploading nothing. It deletes each local
    segment that ends before the index's last end, which each lease call answers. So it
    keeps what the leader has not indexed yet: a few seconds while the leader is healthy
    (its upload lag plus one renewal tick, 5 s), more while the leader is behind.
  - When the leader dies or is redeployed, its lease expires or is released. The standby
    takes the lease, reads the index's last segment end for the printer, and uploads from
    its buffer starting at the segment that contains that end (D14). So a recorder
    restart or a ScadBuddy deploy leaves no gap; up to one segment repeats, and the
    handover segment is marked `discontinuity`.
  - A rolling update replaces one replica at a time, so one recorder is always running.
    A leader that is stopping releases its leases after its last upload.
  - Leases are per printer, so the two replicas can share the printers. The builder picks
    the balancing rule.
  - Each recorder holds the `crud.camera` credential and the bucket's camera keys. It holds
    no access code (it reads the relay with an in-cluster relay credential), no database
    secret, and runs no untrusted code.
- **Live edge** (deployment `scadbuddy-camera-edge`). MediaMTX that reads each printer's
  stream from the relay and serves WebRTC and LL-HLS to viewers (§6.7). Many viewers, or a
  viewer on a bad link, never load the relay. Later restreaming (YouTube, OBS, G7)
  attaches here. It holds only the relay read credential and `crud.camera`.
- **API (CRUD service):** the segment index, the routes, the playlists, retention and the
  flows' records. It is the only process with SQL.
- **Camera worker:** the recorders are also Temporal workers on a new `camera` queue. Its
  activities read segments from the bucket and run `ffmpeg` (timelapse, MP4 download).
  Footage is sensitive, so the bucket's camera keys stay in the recorder pods only. No pod
  that runs untrusted code (the render worker runs template Python) gets them.
- **Two built-in flows** (§6.4a): `print-video` produces a print's HLS playlist when the
  print ends; `print-timelapse` produces its timelapse when that succeeds. They run as
  every flow does, as `ProjectWorkflow` on the projects worker, and call the API's routes.

Why MediaMTX: it pulls RTSPS, serves WebRTC and LL-HLS, records fMP4 segments, and can push
RTMP or SRT later (G7), all without a re-encode. Writing this ourselves (approach B in §7)
would rebuild the same parts.

### 6.2 Which printers are recorded

- A printer is recorded when it has a LAN link (host and sealed access code), its last LAN
  report names an RTSPS stream (`ipcam.rtsp_url` starts with `rtsps://`), and its
  `camera_recording` toggle is on. The toggle defaults to on. The report decides it, not a
  list of models (the anti-goal of an allowlist in code): the H2C's report has
  `rtsp_url: "rtsps://172.16.0.187:322/streaming/live/1"` and `resolution: "1080p"`
  (`.claude/skills/h2c-print-from-scadbuddy/evals/decode-mapping-from-printer-report/files/printer-report-boys1.json:952-953`).
  The relay connects to the link's pinned host with the URL's port and path.
  A report that adds, drops or changes the URL also emits `printer.changed`. This is new:
  `printers/watch.py` gains the comparison with the last announced URL (plan Task 2).
  Today it emits only `printer.report`. The camera streams do not
  carry `printer.report`, which fires on every progress change.
- The relay's controller reads the list, with access codes, from `GET
  /api/v1/internal/crud/camera-relay/printers` (and one printer from
  `…/camera-relay/printers/{id}`): each printer's id, host, certificate fingerprint and
  access code. It follows `crud_print_access_code`
  (`backend/scadbuddy/api/crud_print.py:905`): only the `crud.camera-relay` credential
  reaches these routes. The recorders and the edge read `GET
  /api/v1/internal/crud/camera/printers` (and `…/camera/printers/{id}`) with
  `crud.camera`: the same list with no access code, to know which relay paths exist. Below, "the controller" is any of
  them; each follows the bus the same way.
- **No polling: the controller follows the event bus.** It uses the agent's pattern
  (#5299 D4): one server-sent event stream from the CRUD service, opened with its service
  credential, as `GET /api/v1/internal/crud/agent/changes` is for the agent
  (`agent/src/events/changeStream.ts`, `backend/scadbuddy/api/crud_agent_changes.py`). A
  new `GET /api/v1/internal/crud/camera/changes` (`crud.camera`), and the same stream at
  `…/camera-relay/changes` (`crud.camera-relay`), carry only the events the controllers
  need: `printer.changed` and `camera.reconnect` (§6.9; the route that emits it comes with
  live view, phase 3). No camera setting is read by the relay or the recorders in
  phase 1; the retention settings (phase 5) are read by the API's sweep. The API already emits
  `printer.changed` with the printer's id for every case that matters here: a new printer
  (`create_printer`, `backend/scadbuddy/api/printers.py:136`), a printer the Bambuddy sync
  creates or changes (`printers/bambuddy_sync.py`), a LAN link added
  (`printers/lan_link.py:58`) or removed (`unlink_lan`), a rename or path change
  (`patch_printer`), and a delete (`delete_printer`). The new `camera_recording` toggle
  emits it too. On each event the controller re-reads that one printer (the relay from
  `…/camera-relay/printers/{id}`, the others from `…/camera/printers/{id}`) and acts:
  - an id it does not record yet that now qualifies (§6.2's rule): start its path, so a
    new printer is recorded as soon as it has a LAN link;
  - a recorded printer whose host, access code, fingerprint or model changed: replace its
    path;
  - a recorded printer that no longer qualifies (toggle off, LAN link gone, not an RTSPS
    model) or is gone (404): stop its path, upload what is left on disk, report `stopped`;
  - otherwise nothing.

  It reads the whole list:
  - once at start;
  - after every reconnect, and on `resync`, because it may have missed events. It sends
    `Last-Event-ID`, so the API replays the log when it can.
  It reconnects with the agent's back-off (500 ms doubling to 30 s, with jitter) and treats
  a stream silent past the keepalive as dead. It runs no timer that re-reads the list.
- **The feed publishes events too.** Each part reports its own state for a printer on its
  own caller's route: the relay its pull (`pulling`, `retrying`, `stopped`, with the error)
  through `POST /api/v1/internal/crud/camera-relay/printers/{printer_id}/feed`, the leader
  recorder its recording through `POST /api/v1/internal/crud/camera/printers/{printer_id}/feed` (leader
  replica, last segment time, upload lag, buffered bytes). A report is sent on a change of
  state, not on a timer. The API stores it and emits `camera.feed` on `printers` and
  `printer:<id>`. The UI and the alerts design follow that event; nothing polls the feed.
  Segments do not each emit an event (one every 4 seconds per printer would flood the bus);
  a playlist reader re-reads the index.
- A printer that does not answer keeps its relay path; MediaMTX retries the pull. A gap in
  the recording is normal and is visible in the index (§6.3).

### 6.3 Segments and the index

- **Format:** fMP4, H.264 copied unchanged. A segment is cut at a keyframe. The target
  length is 4 seconds (tune it after phase 0 measures the keyframe interval). Each segment
  file MediaMTX writes starts with its own init (`ftyp` + `moov`), D15.
- **Object keys:** `<prefix>/camera/<printer_id>/<yyyy>/<mm>/<dd>/<HHMMSS.ffffff>.mp4`,
  the segment's start in UTC.
- **Index table** `camera_segments`: `printer_id`, `started_at` (wall-clock time of the
  segment's first frame on the recording recorder's clock, from MediaMTX's file name, not
  the printer's clock), `duration_ms`, `object_key`, `bytes`, `init_bytes` (the init's
  length at the start of the object), `init_sha256`, `recorder` (the replica that recorded
  it), and `discontinuity` (true for the first segment after a gap, a new init, or a
  handover, D14).
- **Route:** `POST /api/v1/internal/crud/camera/printers/{printer_id}/segments`
  (`crud.camera`), refused with 409 to a recorder that does not hold the printer's lease.
  It takes a batch, so a backlog after an API outage uploads quickly. The upload to the bucket happens before the
  index row; a row always names an object that exists.
- An object with no row (the controller died between upload and index) is found and deleted
  by the retention sweep's orphan pass (§6.6).

### 6.4 Prints and their HLS playlists

- **What a print is.** The video belongs to a new record, the **print recording**: one job
  as the printer's LAN report shows it, whoever started it (ScadBuddy, the Bambuddy queue,
  Bambu Studio, the printer's screen). Its start is the report's change to `RUNNING` or
  `PREPARE`; its end is the change to `FINISH`, `FAILED` or `IDLE`. A pause does not end it.
  The new table `print_recordings` keeps `id`, `printer_id`, `started_at`, `ended_at` (null
  while it prints), `subtask_name` (for display only; it repeats on a reprint, so it is not
  a key), `frozen_at` and `timelapse_status`. Its key is `(printer_id, started_at)`.
- **An entity.** A print recording is a new entity kind, `print_recording`, in the
  `entities` table (inventory phase 1). The timelapse, and any photo or note a person adds,
  are its entity attachments. It does not use `print_archive:<id>`, which is Bambuddy's id
  and whose files ScadBuddy does not write.
- **Links to runs, archives and order attempts.** A ScadBuddy run, an order attempt or a
  Bambuddy archive finds its recording by printer and time: the recording on the same
  printer whose `[started_at, ended_at]` contains the run's start. The API stores the link
  when it is first found (`print_recording_links`), so a later lookup does not depend on
  the time match. An order attempt copies the timelapse from its recording, with no
  Bambuddy call.
- **Playlist.** `GET /api/v1/prints/recordings/{id}/index.m3u8` builds an HLS media
  playlist from the index rows in `[started_at - lead, ended_at + tail]`. `lead` and `tail`
  are settings (§6.6), default 30 seconds and 60 seconds. The playlist:
  - uses `EXT-X-MAP` for the init segment and repeats it after each discontinuity;
  - writes `EXT-X-PROGRAM-DATE-TIME` on each discontinuity, so the player can show the
    clock time;
  - while the print runs, is an `EVENT` playlist without `EXT-X-ENDLIST`, so a person can
    watch the print so far; after it ends, is `VOD` and ends with `EXT-X-ENDLIST`.
- **Segment URLs** in the playlist are presigned bucket URLs, so the API does not proxy the
  bytes. Their lifetime is longer than a typical viewing (the builder chooses it; a
  playlist is built on each request, so an expired one is fixed by a reload).
- **Player:** hls.js in the browser (Safari plays HLS natively).
- **Download:** a print's video can be downloaded as one MP4. The API starts a
  `camera`-queue activity that builds it with a remux (`ffmpeg -c copy`), not a re-encode.

### 6.4a Two flows: `print-video`, then `print-timelapse` (owner's decision)

The owner's words: "on print finish/fail/etc event, produce an hls playlist for timestamps
X=>Y", and "on print video produced successfully: run flow to produce timelapse". So the
work is two flows chained by an event. The shop sees each run on the Workflows page, can
edit either script, and can hang its own flows on either event later (post the timelapse
to an order, send it to a customer, restream).

**Both flows are built-ins.** The API registers each at start as version 1 of an ordinary
flow, with its trigger, as it registers `order-fulfilment`
(`backend/scadbuddy/orders/builtin_flow.py`, #4252, #4673): a build updates one only while
its newest version is the built-in's own; once the shop edits or deletes it, no build
rewrites it. One setting, `camera_builtin_flows` (default on), turns both registrations
off, as `orders_builtin_flow` does. None of their steps is outward, so no step waits for
approval.

**Flow 1, `print-video`**

- **Trigger:** a new bus event, `print.recording.ended`, when a print recording gets its
  `ended_at` (end state `FINISH`, `FAILED`, `IDLE` or a cancel). Its facts: `recording_id`,
  `printer_id`, `started_at`, `ended_at`, the end state, and the linked run, order attempt
  and archive when known. The existing `print.finished` and `print.failed` events stay;
  they cover only ScadBuddy's runs, and this one covers every job.
- **Steps** (each a CRUD route):
  1. Wait for `tail`, so the segments after the end are indexed.
  2. `print_video_produce` with `from = started_at - lead` and `to = ended_at + tail`:
     writes the recording's frozen manifest (object keys, durations, discontinuities, init
     hashes for the segments in `[from, to]`) and marks those segments in use (§6.6). From
     then on, the playlist route serves that manifest as a VOD playlist with fresh
     presigned URLs and `EXT-X-ENDLIST`. The flow may pass other bounds (a person can cut a
     video by hand the same way).
  3. `print_recording_link`: stores the links to the run, order attempt and archive that
     the API can find.
- **Result:** when step 2 succeeds, the API emits `print.video.produced` with
  `recording_id`, `printer_id`, `from`, `to`, the segment count, the duration and any gaps.

**Flow 2, `print-timelapse`**

- **Trigger:** `print.video.produced`.
- **Steps:**
  1. `print_recording_timelapse`: starts the timelapse activity (§6.5) on the produced
     video's range and waits for it.
- **Result:** the timelapse is the recording's attachment with role `timelapse`. The API
  emits `print.timelapse.produced`, so a later flow can act on it.

**Retries and failures**

- Each step is idempotent: producing a video that has a manifest with the same bounds
  returns it; a timelapse of a recording that has one returns it.
- If `print-video` is off or fails, the playlist route still builds the playlist from the
  index (§6.4), so no video is lost, and the retention sweep never deletes a segment inside
  an ended recording that has no manifest (§6.6). No `print.video.produced` fires, so no
  timelapse is made. A person can run `print-video` again from the recording's page.
- If `print-timelapse` fails, the video stays. A person can run it again from the
  recording's page.

### 6.5 Timelapses

1. While a print runs, ScadBuddy records each layer change from the LAN report
   (`layer_num` and its time) in `print_recording_layers`.
2. The `print-timelapse` flow starts a `camera`-queue activity. It takes the frame
   nearest each layer change from the segments, using `ffmpeg` with a seek in each segment
   it needs. It decodes only those segments.
3. It encodes the frames as an H.264 MP4 (30 fps by default; a setting) and stores it as a
   blob.
4. It attaches the video to the print recording as an entity attachment with role
   `timelapse` (`backend/scadbuddy/api/entity_attachments.py`). Order attempts copy it from
   the recording, with no pull from the printer.

**Open risk, phase 0 measures it:** a clean timelapse needs the toolhead out of the
picture. Bambu printers park the toolhead at each layer only when the print starts with
`timelapse` on, and the printer then also records its own video. Phase 0 measures on the
H2C:

- whether the print can start with the parking on and the printer's own recording off;
- if it cannot, whether the printer's own recording does any harm (storage space on the
  printer) when ScadBuddy ignores it;
- the time between the layer change in the report and the toolhead at its park position, so
  the frame is taken at the right moment.

Without parking, the timelapse still works, but the toolhead is in each frame.

If parking needs the print's `timelapse` flag (designer's call, owner to check): ScadBuddy
starts its own prints with `timelapse` on, as a remembered print option with a toggle
(#6219), default on. ScadBuddy ignores the printer's own video. Prints started elsewhere
keep whatever their sender chose.

### 6.6 Retention (follows the existing retention settings)

The pattern is `print_run_retention_seconds` and `operation_retention_seconds`
(`backend/scadbuddy/core/setting_docs.py:77`): a seconds value, shown in days, where empty
keeps everything.

| Setting | Label | Default | Meaning |
|---|---|---|---|
| `camera_footage_retention_seconds` | Keep camera footage that no print uses for (days) | 7 days | Empty keeps all footage. A number deletes unused segments older than that. |
| `camera_print_video_retention_seconds` | Keep print videos for (days) | empty | Empty keeps a print's video while the print exists. A number releases a print's segments that long after the print ended; they are then unused footage. |
| `camera_print_lead_seconds` | Start a print's video early by (seconds) | 30 | §6.4 |
| `camera_print_tail_seconds` | End a print's video late by (seconds) | 60 | §6.4 |
| `camera_timelapse_fps` | Timelapse frames per second | 30 | §6.5 |

- With both retention settings empty, ScadBuddy never deletes footage. That is the "deletion
  off" case. The setting's help says how fast the bucket grows at the measured bitrate.
- **What "kept" means.** A print recording is kept until a person deletes it, or until
  `camera_print_video_retention_seconds` has passed since its `ended_at`. Its life does not
  follow print runs (`print_run_retention_seconds`), Bambuddy archives or order records. A
  run or archive that is pruned only loses its link.
- A segment is **in use** when a kept recording's `[started_at - lead, ended_at + tail]`
  covers it: a frozen recording through its manifest, a running or not-yet-frozen one
  through its time range. While a print runs, every segment after its start is in use.
- A recording that is released (deleted or expired) loses its timelapse attachment too,
  unless an order attempt copied it (the copy is the attempt's own attachment).
- The timelapse is a separate blob. It follows the attachment's life, not these settings.
- **Sweep:** a new step in the `Housekeeping` Temporal workflow
  (`backend/scadbuddy/workflows/housekeeping.py`), as the other prunes are. It deletes the
  bucket objects first, then their index rows, in batches. A crash between the two leaves
  rows whose objects are gone; the next pass deletes those rows. An object that is already
  gone is not an error. The orphan pass lists objects under `camera/` older than one day
  that have no row and deletes them.
- Deleting a print releases its segments to the unused-footage rule.

### 6.7 Live view

- The live edge's MediaMTX serves WebRTC (WHEP) and LL-HLS for each printer's path.
- The browser reaches it on ScadBuddy's own origin, under `/api/v1/camera/live/...`, so the
  Bambuddy iframe facts in CLAUDE.md still hold (same origin, no new CORS). The API checks
  the session and then proxies the WHEP exchange; the media goes over WebRTC.
- **Needs measuring in phase 3:** whether WebRTC's UDP media reaches the browser from the
  cluster. If it does not, the fallback is LL-HLS through the same proxy (1 to 2 seconds of
  delay). The builder measures it and picks.
- The existing camera dialog (`frontend/src/components/prints/NowPrinting.tsx:416`) moves
  to the new source. The Bambuddy MJPEG routes stay until the dashboard design removes
  them.

### 6.8 Security and network

- Only the relay must reach the printers' LAN. It runs where the native LAN path already
  reaches them (the builder confirms which node pool and network policy;
  eh-homelab/clusters#3182 is the network work). The recorders and the edge reach only the
  relay, the API, the bucket and Temporal.
- What each part holds:

  | Part | Access codes | Bucket camera keys | Credential |
  |---|---|---|---|
  | Relay | yes, in memory | no | `crud.camera-relay` |
  | Recorders | no | yes | `crud.camera` |
  | Live edge | no | no | `crud.camera` |

  None holds a database secret or runs untrusted code.
- Access codes are never written to disk or logged. MediaMTX gets them through its control
  API, not a config file on disk.
- The relay's RTSP port needs a read credential (an in-cluster secret) and a network policy
  that admits only the recorders and the edge. MediaMTX's control and metrics ports listen
  on localhost only in every part. The edge's HTTP port (WHEP signalling and LL-HLS)
  admits only the API's proxy. Its WebRTC media port must be reachable from browsers;
  how (UDP, or TCP through the cluster's ingress) is phase 3's measurement (§6.7).

### 6.9 Operation

- **Restarts.** A recorder restart or deploy leaves no gap (§6.1). A relay restart leaves
  a gap of about 10 to 30 seconds, marked as a discontinuity. An edge restart drops live
  viewers, who reconnect; it does not touch the recording.
- **Health and alerts.** Each part exports metrics per printer: pull state and reconnects
  (relay); leader, last segment age, upload lag and local backlog (recorders); viewers
  (edge). A Grafana panel shows them. A printer that should be recorded but has no new
  segment for 2 minutes is a `camera.feed` state `stalled`; the alerts design turns it into
  an alert.
- **Upload backlog.** If the bucket or the API is down, the leader keeps segments on a
  size-limited local disk and retries oldest first: the upload before the index, as §6.3
  says. Near the limit it drops the oldest unuploaded segments and records the gap, so a
  long outage loses its oldest footage, never the newest.
- **Clock.** Segment times come from the recorders' clocks; print start and end come from
  the API's. All nodes must run NTP. The recorder rejects (and reports) a clock offset from
  the API larger than 2 seconds, which it learns from the API's `Date` header.
- **Manual reconnect.** The printer's page shows the feed state, with a "Reconnect" button.
  It calls `POST /api/v1/printers/{id}/camera/reconnect`, which emits an event that tells
  the relay to restart that printer's path.

## 7. Approaches considered

- **A. MediaMTX relay plus ScadBuddy recorders (chosen).** One pull per printer, live
  view and later restreaming come from one maintained component. We write the recorders,
  the upload, the index, the playlists, retention and the timelapse.
- **High availability** (owner chose option A of three):
  - **A (chosen).** One relay; two recorders, leader and standby, per printer. Only a relay
    restart leaves a gap.
  - **B.** A plus a standby relay with a lease that does not pull. A relay failure then
    leaves a gap of about 5 to 15 seconds. Can be added later.
  - **C.** Two relays, both pulling (hot-hot). No gap from a relay failure, but it needs
    the printer to accept two clients; phase 0 measures that.
  - **One pod doing everything** (the first draft) was replaced: every deploy left a gap.
- **B. Our own ingest worker** (Go or Rust, one ffmpeg remux per printer) that writes the
  segments directly. Full control, but we would build the fan-out, the reconnects, the live
  WebRTC and the later restreaming ourselves.
- **C. Keep MJPEG.** Rejected: it needs a re-encode, which is the cost this design removes.
- **Per-print recording** (start a recorder only while a print runs). Rejected by the
  owner: continuous recording also keeps what happens between prints, and a print's video
  becomes a playlist with no copy.

## 8. Decisions

By the owner, 2026-11-23:

- One always-on recording per printer, sent to the Spaces bucket.
- A print's video is its own HLS playlist over that recording, from its start time to its
  end time, closed at the end of the print.
- Timelapses are made on ScadBuddy's infrastructure, not by Bambuddy or the printer.
- Retention is configurable, and deletion can be turned off completely, following the
  existing retention settings.
- ScadBuddy handles all of it: recording, index, retention, deletion and timelapses.

By the designer (owner to check):

- D1. MediaMTX as the relay, the recorders' segmenter and the live edge; Python controllers (D7, §6.1).
- D2. 4-second fMP4 segments, each carrying its own init (D15, §6.3).
- D3. Defaults only; both are settings the shop changes (§6.6): unused footage
  (`camera_footage_retention_seconds`) 7 days, empty keeps it all; print videos
  (`camera_print_video_retention_seconds`) empty, so kept while the recording exists.
- D4. Lead 30 s, tail 60 s (§6.4).
- D5. Presigned bucket URLs in playlists; the API does not proxy video bytes (§6.4).
- D6. Live view through the API's origin, WebRTC first, LL-HLS fallback (§6.7).
- D7. The relay controller and the recorder controller are Python processes in
  `backend/scadbuddy/camera/` (`python -m scadbuddy.camera.relay`, `…camera.recorder`),
  as the profile-fetch worker is (`backend/scadbuddy/profile_fetch/`): the service
  credential exchange (`auth/service_auth.py`), the CRUD client (`crud/client.py`) and the
  Temporal worker pattern exist only in Python. The Go client has none of them. All video
  work is MediaMTX's: the recorders run MediaMTX too, with recording on, and the
  controller only uploads, indexes and holds leases.
- D13. A recorder's lease is a row behind CRUD routes (`camera_leases`, compare-and-set
  with an expiry), not a Kubernetes Lease: no RBAC on the API server, and it works the same
  outside Kubernetes (dev, e2e). Renewing it answers the index's last segment end for the
  printer, which each recorder uses to drop its local segments.
- D14. A handover never loses footage: the new leader uploads from its local segment that
  contains the index's last end, so up to one segment repeats, marked `discontinuity`.
- D15. MediaMTX's fMP4 segment files each start with their own init (`ftyp` + `moov`). The
  index stores each segment's init length and init sha256, and the playlist uses
  `EXT-X-MAP` and the segment with `BYTERANGE` on the same object. No separate init
  objects.
- D8. A print's video belongs to a new `print_recording` entity keyed by printer and start
  time from the LAN report, not to a Bambuddy archive or a ScadBuddy run (§6.4).
- D9. A recording lives until a person deletes it or its retention passes; runs, archives
  and orders do not shorten it (§6.6).
- D10. The camera bucket keys stay in the recorder pods; the timelapse and download run
  there on a `camera` queue. The access codes stay in the relay (§6.1, §6.8).
- D11. The `print-video` and `print-timelapse` built-in flows register their own
  triggers, since none of their steps is outward; one setting turns both off (§6.4a).
- D12. If parking needs the `timelapse` flag, ScadBuddy's own prints start with it on, as a
  remembered option with a toggle (§6.5).

By the owner, 2026-11-23 (later): the print video slicing is a flow. On a print's
end, one flow produces its HLS playlist for its time range; when that succeeds, a second
flow produces the timelapse (§6.4a).

By the owner, 2026-11-23 (later):

- The always-on pull may block Bambu Handy, Bambu Studio and Bambuddy from the camera if
  the printer accepts too few clients. That is acceptable; ScadBuddy becomes the way to
  view the camera.
- One pull per printer, multiplexed in the cluster: one relay, two recorders (leader and
  standby), a live edge (§6.1, §7 option A).
- The controllers follow the event bus; nothing polls (§6.2).

## 9. Phases (each one issue, under one new epic)

0. **Measure.** `ffprobe` the H2C stream: codec, profile, resolution, frame rate, bitrate,
   keyframe interval, audio. Count the RTSP clients it accepts. Measure the timelapse
   parking (§6.5). Write the results into §2 with the date and method.
1. **Record.** The relay (MediaMTX plus its controller) and the recorders (leader and
   standby with leases, the handover from the buffer), the `crud.camera` and
   `crud.camera-relay` credentials and their routes, the change stream
   `GET /api/v1/internal/crud/camera/changes`, `camera_segments`, upload and index, feed state and the
   `camera.feed` event, the toggle, the deployments and network policies in
   eh-homelab/clusters, metrics and a Grafana panel (§6.9). A test kills the leader during
   recording and checks that the index has no gap.
2. **Per-print video.** `print_recordings` and its entity kind, start and end from the LAN
   report, the `print.recording.ended` and `print.video.produced` events, the
   `print-video` built-in flow, the playlist route, the player on the recording's page and the print's
   page, the MP4 download.
3. **Live view.** The live edge, the WebRTC proxy (or LL-HLS fallback), the camera
   dialog on the new source, the feed state and "Reconnect" on the printer's page.
4. **Timelapse.** Layer times, the `camera`-queue activity, the `print-timelapse` built-in
   flow and the `print.timelapse.produced` event, the attachment, order attempts copy it from the recording; stop pulling the printer's
   timelapse.
5. **Retention.** The settings, the sweep, the orphan pass. It must merge before phase 1
   runs long on prod, or the default 7 days does not apply yet (with no sweep, nothing is
   deleted, which is safe but grows the bucket).

## 10. Left to the builder

The exact table columns and indexes, the route shapes beyond those named, the flow
script's exact text, how the recorders share printers' leases, the presigned URL lifetime, the MediaMTX version
and its config, the frame-selection details, and the UI layout (load `frontend-design`).
