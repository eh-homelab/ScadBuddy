# Own the store, the slicer and the printers: cut Bambuddy out of the hot path

Status: proposed, 2026-10-16. Supersedes nothing yet; it narrows what the
2026-09-27 template-pipelines spec §6 says about the `bambuddy` store backend being
"the one production backend".

## 1. Intent

Every render on prod today moves its pieces, previews and outputs through Bambuddy's
library API, and every slice is a Bambuddy slice job wrapping the Bambu Studio CLI.
Bambuddy is one uvicorn asyncio process. On 2026-10-16 a watched printer camera
(an ffmpeg RTSP-to-MJPEG transcode plus the loop relaying frames) saturated it, each
library call took 0.6 to 1.3 s, and a render of 14 s of OpenSCAD took 2 m 46 s
end to end (trace decomposition in #3789 and #3788; the 10 s 503 on submit is #2340).
Slicing a 15 MB five-filament model took about two minutes of slicer CPU per job,
and jobs with prime-tower plus no-sparse-layers overrides failed Bambu Studio's
validation (exit 192) inside Bambuddy's sidecar, where ScadBuddy sees only a 502.

We already do the work Bambuddy nominally does for us: the 3MF layout, preset
resolution, nozzle planning, prime tower and support overrides, plate placement, the
G-code parse for filament changes and the toolpath, the archive linking. Bambuddy
gives us three things: a place to put bytes, a slicer process, and the printer
connection. This design takes each back, in that order, so that Bambuddy becomes
optional on every path and stays supported on every path.

The long-term direction this serves: ScadBuddy becomes a web-based Bambu Studio,
mirroring the Windows application's workflow (prepare, preview, device) with its own
store, its own slicer and its own printer connections. Nothing in this document builds
that UI; everything in it is a precondition for it.

### 1.1 Goals

- A render never waits on Bambuddy. Pieces, previews, snapshots, assets and fonts live
  in a store ScadBuddy owns.
- The library a person browses and mounts (`bambuddy-fs`, the desktop's way into
  ScadBuddy's files) is served from that store through the same `/print/library/*`
  routes, so the mount changes nothing and a desktop Bambu Studio opens the files.
- A slice is a ScadBuddy activity with live progress, running the Bambu Studio CLI
  under ScadBuddy's control, with ScadBuddy's own preset files, so a validation failure
  comes back as the slicer's message and not a 502.
- One identity per printer, keyed by its serial number, that links the Bambuddy
  printer record and ScadBuddy's own LAN connection to the same machine. Either path
  prints; the user and the agent see one printer.
- Hot-path compute moves off Python: thumbnails, the 3MF split, the per-colour solids
  fan-out, the archive pack and unpack, store transfers, the slicer process. The new
  workers are Rust on the Temporal Rust SDK.
- Bambuddy keeps working at every step. The `bambuddy` store backend, the Bambuddy
  slicer and the Bambuddy print queue remain selectable until the owner retires them.

### 1.2 Non-goals

- Replacing Bambuddy's spool inventory, archives, timelapse or project folders. Those
  stay Bambuddy features reached through the existing client until a later design.
  A native run (the hot path, `docs/superpowers/plans/2026-10-18-hot-path-without-bambuddy.md` D2) reads no Bambuddy project, archive or queue,
  and reads Bambuddy's inventory only through the off-path provider refresh (the
  `spool-providers` Schedule, `bambuddy/spool_providers.py`): its filaments are the
  loaded trays from the LAN report with the physical-spool overlay of cached provider
  records (plan D1), and ScadBuddy's own inventory is tier B's native provider of that
  overlay (#4643). Projects stay the Bambuddy path's; a native run files in the native
  library (plan D6).
- Rewriting the API, the workflows or the agent in Rust. Orchestration stays Python.
- Linking libslic3r directly. It is C++ with no C API; the CLI is the supported
  boundary (§5.1).
- The web-based Bambu Studio UI itself.

### 1.3 Anti-goals

- A fourth place for state. New durable state is Postgres or the blob store, never a
  file on a pod.
- A second printer identity. A printer appears once in the UI, once in the agent's
  tools and once in the `activity` table, whichever path is active.
- Any path that works in one mode and not the other. A Bambuddy-linked printer and a
  native one accept the same print request body.

## 2. Measured facts this rests on

| Fact | Where measured |
|---|---|
| Bambuddy library GET takes 0.6 to 1.3 s while a camera is watched; ffmpeg about 2.3 cores, the event loop about one full core | Bambuddy pod CPU and `Camera viewer attached` log lines, Tempo spans `original_span_name=bambuddy.library.*`, 2026-10-16 |
| A render makes 10 to 14 Bambuddy calls (piece publish, preview, output, snapshot, fonts) | Tempo trace of `POST /api/v1/models/{slug}/render`, 2026-10-16 |
| 89 s render: 26 s OpenSCAD, 24 s `render.thumbnail`, 7 s split, 14 s serial solids, 10 s Bambuddy | same trace; `scadbuddy_render_stage_seconds` |
| Bambu Studio CLI reports progress on `--pipe <fifo>` as JSON lines `{"plate_index","plate_count","plate_percent","total_percent","message"}` and `{"warning"}`, Linux only, 512-byte writes | `src/BambuStudio.cpp` in bambulab/BambuStudio at the 02.07 tag, read 2026-10-16 |
| CLI flags: `--slice 0|i`, `--export-3mf`, `--load-settings <machine>;<process>`, `--load-filaments <f1;f2;…>`, `--arrange 0|1|auto`, `--debug 0..5`, `--export-slicedata`, `--no-check`, `--allow-newer-file`; validation errors exit 192 | same file and `src/libslic3r/PrintConfig.cpp` |
| libslic3r exposes no `extern "C"` surface | search of `src/libslic3r` in bambulab/BambuStudio, 2026-10-16 |
| Temporal Rust SDK is GA: 1.0.0 on 2026-09-04, 1.1.0 on 2026-10-06 (worker versioning in `temporalio-common`/`temporalio-client`, OpenTelemetry experimental) | github.com/temporalio/sdk-rust releases, read 2026-10-16 |
| Temporal Standalone Activities (an activity with no workflow) are public preview, need server 1.31 or later, and have no Rust SDK support yet | docs.temporal.io standalone activities page, read 2026-10-16; prod runs `temporalio/server:1.28.4` (clusters#2146 is the upgrade) |
| Bambu LAN protocol: MQTT over TLS on 8883, user `bblp`, password the printer's access code, topics `device/<serial>/report` and `device/<serial>/request`, `pushing.pushall` for full state, `print.project_file` with `url`, `param`, `subtask_name`, `use_ams`, `ams_mapping`, `bed_type`, `flow_cali`, `timelapse`, `layer_inspect`; the file goes to the printer's storage over FTPS first | Doridian/OpenBambuAPI `mqtt.md`, read 2026-10-16. The FTPS port (990, implicit TLS) and the SD-card path are not stated there; the builder re-measures against a printer before relying on them |
| LAN file delivery on an H2C (firmware 01.02.00.00): the 3MF is stored at the FTPS root `/<name>` and `print.project_file` names it `url: ftp://<name>`; ids `"0"` and an `ams_mapping` without `ams_mapping2` are accepted; MQTT (8883) and FTPS (990) present one certificate; idle reports are whole `push_status` messages every ~1.8 s; once a start is accepted the printer moves the file to `/cache/<name>.gcode.3mf`, so `/<name>` is gone and a retried start uploads again | measured 2026-10-24 against the H2C (`backend/tests/printers/test_lan_live.py` docstring: `result: SUCCESS` in 0.4 s, `RUNNING` at 1.8 s, then stopped); Bambuddy does the same (`print_scheduler.py` "Upload to root directory (not /cache/)", `bambu_mqtt.py` `start_print`, deployed image read 2026-10-24) |
| A rack print on an H2C (firmware 01.02.00.00) sends `nozzle_mapping` in `print.project_file`: 32 entries, one per filament slot (index 0 is slot 1), each a physical nozzle id: `1` the fixed hotend, `15 + position` (16-21) a rack position 1-6, `-1` a slot not printed; no field at all when no slot prints from the rack | Bambuddy's completed dispatches to the live H2C, its `print_queue` read 2026-10-24: item 370 `ams_mapping` `[4, 1, 6, 12, 5, 13]` sent `[21, 18, 20, 17, 1, 17, -1 x26]`, 367 `[3, 15, 0, 11, 8, 5, 6]` sent `[18, 1, 1, 20, 1, 18, 20, -1 x25]`, 374 `[128]` sent `[18, -1 x31]`, each printed; `bambu_mqtt.py` `_RACK_WIRE_SLOTS`, `_FIXED_NOZZLE_ID`, `rack_position_to_nozzle_id` (deployed image, read 2026-10-24); `backend/tests/printers/fixtures/rack-project-file.json` |
| Extruder indices are another namespace: on the wire extruder 0 is the rack's carriage and 1 the fixed hotend. `slice_info.config` names each filament's `group_id` and each group's 1-based slicer `extruder_id` (`<nozzle id=<group> extruder_id=…>`); the H2C preset's `extruder_max_nozzle_count` `["1", "6"]` makes the slicer's second extruder the rack, `physical_extruder_map` `["1", "0"]` the wire's 0 | item 370's `slice_info.config` (`backend/tests/printers/fixtures/rack-slice-info-h2c.config`: group 0 on extruder 1, groups 1-4 on extruder 2, groups by slot `[2, 4, 3, 1, 0, 1]`) against what it sent; Bambuddy `threemf_tools.py` `_rack_plan`, `_group_extruder_indices` (deployed image, read 2026-10-24) |
| A wrong nozzle id levels with one nozzle and prints with another (a first layer in mid-air); omitting `nozzle_mapping` lets the firmware pick | Bambuddy `bambu_mqtt.py` `resolve_rack_nozzle_mapping` docstring and the #2800 A/B in its comments (deployed image, read 2026-10-24) |
| The H2C's report lists the hotend on the rack's carriage as `device.nozzle.info` id 0 and omits its rack id (the idle report of 2026-10-24 omits 19, `src_id`/`tar_id` 19) | `backend/tests/printers/fixtures/lan-report-h2c-idle.json`, recorded 2026-10-24; Bambuddy `_rack_by_position` (same reading) |
| Bambuddy's `Printer` model has no serial; only a hotend's `serial_number` exists | `backend/scadbuddy/bambuddy/models.py`, 2026-10-16 |

## 3. Shape of the change

Three tiers, each shippable alone, each leaving Bambuddy selectable.

```
tier 1   store      ContentBackend "s3"  (Garage / MinIO / any S3)      Python first, Rust transfers later
         library    /print/library/* served from the store (§4.5)      bambuddy-fs mounts it unchanged
tier 2   slicer     Rust worker: bambu-slice activity, CLI + fifo       presets as files, progress as heartbeats
tier 3   printers   printer registry by serial; LAN path (MQTT+FTPS)    Bambuddy path and native path behind one id
cross    rust       scadbuddy-worker crate: thumbnail, split, archive, store, slice, later RenderPiece
```

Dependencies: none between the tiers. Tier 2's worker moves bytes through transfer
URLs the API mints (§5.2), so it runs on every store backend; on `bambuddy` the API
still pays the library round trips for the slice's input and output, so tier 1 is what
makes it fast, not what makes it work. Tier 3's native print needs a sliced file, which
the Bambuddy slicer also produces (as a library file the API can read), so a LAN print
works with either slicer. The Rust worker starts in tier 2 and grows.

## 4. Tier 1: a store ScadBuddy owns

### 4.1 Decision

Add a third `ContentBackend`, `backend = "s3"`, implementing the existing four-method
protocol in `backend/scadbuddy/store/content.py` (`upload`, `download`, `exists`,
`remove`). `ContentStore`, `BlobIndex`, `CachedBlobStore`, `SnapshotStore`,
`RemoteAssets`, `FontMirror` and `DownloadCache` are unchanged: they already sit on
the protocol. `store_backend` gains the literal `"s3"`; `build_store` wires the S3
bundle the same way it wires the Bambuddy one, with `remote=None` (no
`sweep_unrecorded`, no `store_folders`, no `RefusedDeleteError`).

Object key: `<kind>/<sha256>` with a required prefix setting, so one bucket can hold
several instances. `upload` is idempotent by key (a re-put of the same sha is a no-op
after `exists`, as `_store` already does). `download` streams; the whole-blob `read`
callers (`CachedBlobStore._fetch`, `SnapshotStore.materialize`, `FontMirror.sync`,
`RemoteAssets.ensure`) stay as they are in this tier and move to streaming in the Rust
transfer work (§7).

Multipart upload above 64 MiB, with the part size the builder picks within the S3
limits (5 MiB to 5 GiB per part). A render's piece zip is typically under 50 MB; the
MakerWorld mesh fixtures reach 290 MB, so multipart is required, not optional.

Client: `aiobotocore` is the Python choice (async, maintained, mypy stubs via
`types-aiobotocore-s3`), with the endpoint, region, bucket, prefix and credentials
from settings. Path-style addressing on by default (Garage and MinIO need it).

### 4.2 Settings

All in Postgres, `applies: restart`, like the existing store settings:
`store_s3_endpoint`, `store_s3_region`, `store_s3_bucket`, `store_s3_prefix`,
`store_s3_access_key_id`, `store_s3_secret_access_key` (secret; stored like
`bambuddy_render_api_key`), `store_s3_path_style` (default true). Each has its
`SCADBUDDY_<FIELD>` seed. `build_store` raises `StoreNotReadyError` when the backend is
`s3` and the endpoint, bucket or credentials are missing, with the same
`RECOVER_LOCAL_SQL` hint the Bambuddy branch gives. The render worker's own role reads
them through the `render_settings` view, so `RENDER_GRANTS` and the view gain the
columns (`worker_role.py`, and `tests/test_render_worker_role.py` must still render).

### 4.3 Migration between backends

A blob's `backend_id` is per backend, and `_release` refuses a ref from another
backend, so switching `store_backend` with blobs recorded under `bambuddy` leaves them
unreadable by the new store. The migration is two-phase, because the live API and
workers keep running on the old backend while it copies, and a row whose `backend`
changed under them reads as missing (re-rendered and re-put, orphaning the copy):

- **Copy.** A `StoreMigration` workflow on the `library` queue, started by
  `POST /store/migrate` (one per `source, target` pair; a second POST joins it), runs
  `store_migrate_batch` activities: for each `store_blobs` row on the source without a
  copy on the target, download, verify the sha256, upload to the target, and record the
  target's id in the row's `migrated_backend` and `migrated_backend_id` columns, CAS on
  the row's sha and backend. `backend` and `backend_id` are not touched. A row whose
  sha or backend changes (a running render re-put it) loses its copy columns and is
  copied again on the next pass; a row that fails is skipped for the run and listed.
  `GET /store/migrate` reports what the table says (rows copied and remaining) plus the
  run's failed keys, running or closed.
- **Switch.** The operator flips `store_backend` and restarts. At start on the new
  backend, one statement moves every row whose `migrated_backend` is that backend onto
  its copy and clears the copy columns, so the rows flip atomically with the process
  that can read them. The old backend's objects stay where they are (nothing deletes
  them; a later sweep may). A row still on the old backend after the flip (never
  copied) is a cache miss that `RenderPiece` re-renders, as a `PieceStateLostError` is
  today.

The order is: run the copy to `remaining: 0`, flip, restart. The health route's
`configured_backend` already shows a flip waiting on a restart.

### 4.4 Deployment

Garage or MinIO as a StatefulSet in the `bambuddy` namespace on `primary`, on the
same storage class as Postgres, is a clusters-repo change (`clusters/prod/scadbuddy/`),
not this repo's. This repo documents the settings in `README.md` "Deploying" and gives
the compose file a MinIO service for a local run. Backups of the bucket are the
operator's; the design does not prescribe them.

### 4.5 The library a person sees, over the store

The blob store holds pieces, snapshots, assets and fonts: internal bytes. What a person
browses, copies in and out, renames and mounts on their desktop is the *library*:
today Bambuddy's, reached through ScadBuddy's `/print/library/*` routes
(`api/library_print.py`: list, mkdir, upload, download, rename, move, delete), which
`bambuddy-fs` (`tools/go/bambuddy-fs`, spec 2026-10-13) mounts over FUSE so a desktop
Bambu Studio can open the files. That mount is a ScadBuddy API client and must not
change.

So ScadBuddy gets a library of its own, served by the same routes with the same
shapes, chosen by a `library_backend` setting (`bambuddy` | `native`, restart): the
tables `library_folders` (id, parent_id, name, created_at) and `library_files` (id,
folder_id, name, size, sha256, blob key, media type, created_at, updated_at), with the
bytes as `library` blobs in the content store (a fifth `BlobKind`, never swept while a
`library_files` row names it; the row's delete releases it). The folder ids and file
ids the routes return are the native rows' ids on `native` and Bambuddy's on
`bambuddy`; a client sees one id space at a time. Saved outputs, the sliced 3MFs the
native slicer produces and the files a native print uploads are filed into it (the
output's folder is the native library's own top-level `<Template title>/`; the Bambuddy
inbox keeps a sent copy at its top level, `bambuddy/send.py` `upload_copy`), so what
a person prints is what they can mount. `bambuddy-fs` needs no change: its README's
"Mounts Bambuddy's print library" becomes "Mounts ScadBuddy's print library", and its
mount tests run against both backends in CI.

The native library does not replace Bambuddy's for a Bambuddy print: the Bambuddy path
still uploads its copy to Bambuddy's library (`send.py`), as today. The Bambuddy inbox
is needed at start only by `store_backend=bambuddy` (`store/backends.py`
`build_backend`); `library_backend=native` needs a content store (`s3` or `bambuddy`)
and is refused over `local`, and with `s3` nothing at start needs Bambuddy. A user
delete moves a native file to a trash first (the purge releases its blob), and
`library_files` carries `deleted_at`, `output_id`, `filed`, `plates` and
`has_thumbnail` beyond the columns above (plan 2026-10-17, Spec corrections).

### 4.6 What `local` does

Nothing changes for `store_backend=local`. It stays the single-replica, shared-volume
store with no `ContentStore`.

## 5. Tier 2: a slicer ScadBuddy runs

### 5.1 Boundary

The Bambu Studio CLI, as a subprocess, in a Rust worker. libslic3r has no C API, is
built with Boost, TBB, OpenVDB, CGAL and wxWidgets-adjacent code, and changes shape
every release; a Rust FFI binding would be a maintenance project of its own and would
share a process with the worker, so a slicer crash takes the activity with it. The
CLI already has the progress pipe, the validation, the preset loading and the 3MF
export we need, and a crash is an exit code.

The binary comes from Bambu Studio's official Linux release (the Ubuntu AppImage,
extracted; pinned by version and sha256 in the Dockerfile as the OpenSCAD nightly is),
in a new Dockerfile stage `slicer`. The slicer image carries no Python, no OpenSCAD
and no data volume: it reads inputs from the store and writes outputs to it.
Bambu Studio's own profiles (`resources/profiles/BBL/`) ship with the binary and are
the source of the printer and process presets ScadBuddy loads (§5.3).

### 5.2 The activity

One activity, `slice_plate`, on a new task queue `slicer`
(`SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER`), served by the Rust worker. The worker holds
no Postgres connection and no store credentials: its input carries transfer URLs the
Python caller mints, never bytes and never a store key it would have to resolve:

```
TransferUrl { url, headers: {name: value}, expires_at }
SlicePlateInput {
  input_3mf: TransferUrl,          # GET: the laid-out 3MF (replate_3mf / recolour_3mf applied)
  plate: u32,                      # 1-based; 0 means every plate
  machine_preset: TransferUrl,     # GET: a Bambu Studio machine JSON
  process_preset: TransferUrl,     # GET: a process JSON with ScadBuddy's overrides applied
  filament_presets: [TransferUrl], # GET: one per slot, in slot order
  sliced_3mf: TransferUrl,         # PUT: where the --export-3mf result goes
  slicedata: TransferUrl | null,   # PUT: --export-slicedata when asked
  arrange: "no" | "yes" | "auto",
  allow_newer_file: bool,
  debug: u8,                       # the CLI's --debug level, 0..5
}
SlicePlateOutput {
  sliced_sha256: String, sliced_size: u64,
  slicedata_sha256: String | null, slicedata_size: u64 | null,
  warnings: [String],
  exit_code: i32,
  stderr_tail: String,             # the last 8 KiB
}
```

On `store_backend=s3` a GET is S3's presigned URL of an object already stored
(`S3ContentBackend.presign_get`, two hours, longer than any slice). Every other GET, and
every PUT on any backend, is a new internal API route, `GET` and
`PUT /api/v1/internal/blobs/{key}` (`api/internal_blobs.py`, hidden from the OpenAPI
document), and the API moves the bytes to and from its content store (plan
2026-10-16-native-slicer-rust-worker, Ruling C3). Presigned PUT, with the backend adopting
the object the worker wrote under `pending/`, is a follow-up (#4231): a presigned PUT to
a key `sweep_unrecorded` never writes would never be swept (`store/s3.py`). The routes
serve every backend that has a content store, `s3` and `bambuddy`; `local` has none
(`store/factory.py` returns no `ContentStore` for it), so the native slicer needs
`store_backend` `s3` or `bambuddy`, and on `local` a slice fails with a 503 saying so
(§5.5). The route's token is new, not something `RemoteOutputs` has (its
`/internal/outputs` reads carry none): an HMAC over the method, the key and the expiry,
under a key derived by HKDF from the KEK (`slicing/transfer_token.py` `transfer_key`),
checked before the route touches the store (#4215). So the native slicer needs
`SCADBUDDY_SECRET_KEY_FILE` on the API, and `SCADBUDDY_API_INTERNAL_URL`, the base the
slicer pod reaches the routes at; without either `slice_prepare` fails the slice saying
so. Tier 2 does not depend on tier 1, and no store credential leaves Python. The caller
records the `store_blobs` row for the output after the worker reports its sha256 and
size (`slicing/transfer.py`: `mint_get`, `mint_put`, `finish_put`; the pending PUTs in
`transfer_puts`, pruned on the housekeeping prune Schedule). A new `BlobKind`, `slice`,
swept like `piece`. Every `slice` blob a slice reads or writes (the copy as downloaded,
each preset file, the sliced 3MF) is held in `blob_refs` under holder `native_slice` and
the row's id while its `native_slices` row stands (Ruling C5): a row that fails or is
cancelled drops its holds in the same transaction, and the TTL sweep that deletes a
finished row drops the rest (`slicing/store.py`).
Every transfer URL is a bearer credential (the internal route's token in a header, or
S3's signature in the query) valid for two hours (`slicing/transfer.py` `TRANSFER_TTL`),
and it crosses the `library` queue's Temporal history in plaintext, since that queue has
no payload codec: whoever reads that history can read or write the blob until it expires.

The worker's whole configuration is its environment: `SCADBUDDY_TEMPORAL_ADDRESS`,
`SCADBUDDY_TEMPORAL_NAMESPACE`, `SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER`,
`SCADBUDDY_SLICER_CONCURRENCY` and the `OTEL_*` variables. It reads no settings and
needs no database role.

The activity downloads its inputs into a per-attempt temp dir, writes a fifo, starts the CLI with `--pipe <fifo>
--load-settings <machine>;<process> --load-filaments <f1;…> [--filament-colour <c1;…>]
--slice <plate> --export-3mf <out> [--arrange …] [--allow-newer-file] [--debug N]
<input>` (`--filament-colour` carries the slots' colours: unless it names two or more
distinct ones, the CLI turns the prime tower off whenever every filament file has one
preset name, `BambuStudio.cpp:2625` and `:3770-3784` at v02.08.02.61, #3962; a single
colour is passed but changes nothing there, and the colours are in the native slice's
key), reads the
fifo's JSON lines and turns each `total_percent` into a heartbeat carrying
`{plate_index, plate_count, plate_percent, total_percent, message}`; each `warning`
line is collected. Exit 0 PUTs the output to its URL and returns; any other exit fails the
activity with an `ApplicationError` of type `SlicerError`, non-retryable for 192
(validation) and retryable otherwise, carrying `exit_code` and `stderr_tail` in its
details. The activity honours cancellation by killing the process group. Start-to-close
timeout is the caller's; the worker's heartbeat timeout is 60 s, so a hung CLI fails
within a minute of its last progress line.

Concurrency is one slice per core pair: the worker's `max_concurrent_activities` is
a setting with a default of `floor(cpus / 2)`, since Bambu Studio parallelises inside a
slice. The slicer image's requests and limits are the clusters repo's.

### 5.3 Presets as files

Today `SliceRequest` names presets by `{source, id}` and Bambuddy merges
`process_overrides` into the process JSON. The native slicer needs the merged JSON
itself. A new module `backend/scadbuddy/slicing/presets.py` resolves a `SlicePlan` to
three kinds of files:

- the machine JSON: Bambu Studio's own `BBL/machine/<printer> <nozzle> nozzle.json`,
  with its inheritance chain flattened (Bambu Studio's `inherits` is resolved by the
  CLI only for presets in its own resources dir, so a flattened copy is the safe
  input);
- the process JSON: the chosen process preset flattened, then ScadBuddy's
  `process_overrides` applied with the same semantics Bambuddy's
  `services/process_overrides.py` gives them today (the filament-profiles spec of
  2026-10-13 is the authority on the override keys);
- one filament JSON per slot, flattened.

A native run offers the image's presets (hot-path plan D5, `slicing/catalogue.py`
for the lists and `slicing/values.py` for a process preset's values) and Bambuddy's
`local` (OrcaSlicer-import) presets from a kept copy (#4645, `slicing/held_presets.py`,
the table `held_local_presets`). The copy is refreshed off the hot path on the spool
providers' Schedule (`SpoolRefresh`'s `held_presets_refresh`, behind its patch): a list
of `/local-presets/`, then a read of each preset new or edited since (its `updated_at`).
A native run reads only the copy, and flattens a kept document over the image's tree
as it does a preset read from Bambuddy (`Profiles.flatten_over`), so the Rust worker
gets fully resolved files; the slice's key carries each built file's digest, so an edit,
once kept, slices again. A kept copy rather than an import into ScadBuddy's own store:
the presets stay Bambuddy's records (its spools name them by id), as the spool providers'
cache keeps its inventory. Bambu Cloud user presets are still not offered on a native run,
and a remembered one is refused naming it (#5113).

Flattening reads Bambu Studio's profile tree, copied into the API image at build time
from the Dockerfile's `slicer-dist` stage, the same checked download the slicer image
installs (`/opt/slicer-profiles`: `BBL.json`, `VERSION` and `BBL/{machine,process,filament}`;
`settings.slicer_profiles_dir`). No store blob carries it and there is no
`preset_bundle` kind: the Python side reads files and never needs the binary. The
preset key is `native@<version>/` before Bambuddy's preset key, where the version is that
tree's `VERSION`, the API image's `BAMBU_STUDIO_VERSION` (`Profiles.version`,
`slicing/presets.py` `native_preset_key`; Ruling C7), so a native slice never answers for
a Bambuddy one and a new Bambu Studio slices again (§8). A process preset the tree does
not hold (`local`, a cloud user preset) is built from Bambuddy's values
(`GET /slicer/preset-values?slot=process`), and the key then carries their sha256 as well.
A `local` filament or printer preset (a user's own, imported into Bambuddy's
`/local-presets/`, as the spool intake imports 3dfilamentprofiles) is read from
`GET /local-presets/{id}` (#4233): its `setting` is the preset's document as Bambuddy
stores it, merged at import over the OrcaSlicer BBL base its `inherits` names with
`inherits` kept, and what Bambuddy's own slicer slices with verbatim (read in Bambuddy's
source on 2026-10-22: `routes/local_presets.py` `get_local_preset`,
`services/orca_profiles.py` `import_orca_file` and `resolve_preset`,
`services/preset_resolver.py` `_resolve_local`; not yet measured against a live
Bambuddy). ScadBuddy flattens it over the image's tree by that `inherits`
(`Profiles.flatten_over`, as Bambu Studio loads a user preset over the system preset it
inherits), so a parent this Bambu Studio lacks is the plan's 422 naming it, never a
file the CLI fills from its defaults; and the key carries the sha256 of each file so
built (`PresetFiles.from_bambuddy`, `/bambuddy-<slot>=<sha256>` after the process's),
since `PUT /local-presets/{id}` rewrites a preset under the same id. Without Bambuddy
configured such a ref is the plan's 422 saying so. Bambuddy's values route serves no
other slot (re-measured 2026-10-18 against the live Bambuddy,
`ghcr.io/maziggy/bambuddy:daily@sha256:99684c2f…`: `slot=filament` answers
`400 "Only the 'process' slot is supported"`), so a Bambu Cloud user preset in the
filament or printer slot is still refused on the native slicer.

### 5.4 Where the slice sits in the print flow

`PrintRunWorkflow` today runs `print_slice_start` and `print_slice_wait` per plate
against Bambuddy. A `slicer` setting (`slicer_backend`: `bambuddy` | `native`, applies
live, default `bambuddy` until the owner flips it) chooses, read once by `print_plan`
(`PlannedRun.slicer`, so a run never switches slicer halfway).

With `native`, one `SliceWorkflow` per copy, subject and presets slices the plate, on the
`library` queue (the API process: the content store, the Bambuddy client, the native
library). Its activities are `slice_prepare` (reads the copy and builds the presets, so
neither bytes nor preset documents enter a history), `slice_plate` on `slicer`, and
`slice_record`. The print worker never runs it: the dialog's `preview_run` (on
`bambuddy`, the storeless print worker) only starts it, and the run shares it through
its own `library`-queue activity `slice_run`, which starts it or joins the dialog's and
waits (Rulings C1, C2; behind `workflow.patched("native-slicer")`). A child workflow was
not used: temporalio 1.34.0's `start_child_workflow` takes no `id_conflict_policy`, so a
running dialog slice of the same id would fail its start. `slice_run` heartbeats, and
every retryable failure (a Bambuddy blip in its reads among them) retries without a
limit up to `SLICE_RUN_BOUND`, about 4 h 25 min: one realistic round of the slice
(`workflows/slicing.py` `SLICE_ROUND`: the prepare's three attempts of five minutes, one
wait of up to two hours for a slicer worker, since Temporal does not retry a
schedule-to-start timeout, and four `slice_plate` attempts of up to 30 minutes) and a
ten-minute margin (Task 13 review M2, bounded by the Task 14/15 review's ruling). A slice
can outlast that only when attempts that are each taken just before their wait ends then
die, or a `TransferRefused` prepares again; the run then fails with a 504 saying to print
again, both on the schedule-to-close timeout and on a last attempt's own timeout whose
retry would land past it (`printing.py` `outlived_slice`). Printing again joins the slice
if it still runs (`USE_EXISTING`) or takes its completed row at once, so an open run gains
nothing by waiting longer. A cancelled run stops waiting and never cancels the slice the
dialog shares.

The execution's id is `slice-<file id>-<sha256(subject, copy sha256, preset key)>`, known
before any byte is read, so the dialog and the run name one (Ruling C2). The copy's
sha256 (Bambuddy's `file_hash`) is in it as well as its id because Bambuddy's SQLite
library hands a deleted newest file's id out again: its `INTEGER PRIMARY KEY` has no
`AUTOINCREMENT` (measured at Bambuddy 3acc52d9, and reproduced in SQLite in the Task 13
review), so the id alone may name another file. `slice_prepare` compares the bytes it read
with that sha256 and refuses other bytes (409, §5.5). Reuse across copies of identical
bytes and presets is `slice_prepare`'s, by the bytes' sha256 and the preset key (Ruling
C1).

Until tier 3 (and the native library's own layout, Tier A Task 4), Bambuddy's queue
prints only a file in Bambuddy's library, so the run still uploads the sliced 3MF beside
the copy in Bambuddy's library (`print_sliced_upload`, `<copy stem>.plate<N>.gcode.3mf`;
idempotent across its retries, by the file's bytes) and queues that file, on
`library_backend` `native` too. `output_bambuddy_slices` records the uploaded file as
before and gains a nullable `sliced_blob_key`: provenance, whose blob is held by its
`native_slices` row and swept with it, not a second copy the print reads.

The print dialog's preview slice (`start_preview`, `read_preview`) gets the same
switch: with `native` it starts the same `SliceWorkflow` (one execution per copy,
subject and presets, until Temporal 1.31's standalone activities let `slice_plate` be
the bare activity), and `read_preview` reads `slice_plate`'s heartbeat details for
progress. The read routes, the toolpath included, take `?slicer=native` (a native row's
id and a Bambuddy job's may be equal). The browser polls as it does today (every 2 s),
and the heartbeat's `total_percent` rides that answer, so the pane shows "Slicing… 40%"
and a progress bar; no realtime topic (realtime events carry ids, not content). A native
slice's slots carry no `side` (`slicing/preview.py`), so the side-mismatch warning does
not fire for it until #4232.

Filament requirements (grams per slot, side) and filament changes today come from
Bambuddy's `filament-requirements` and ScadBuddy's own G-code parse. With the native
slicer both come from the sliced 3MF: `Metadata/slice_info.config` holds per-filament
`used_g` and `used_m`, and the existing `M620 S<n>A` count on `Metadata/plate_N.gcode`
stays. A new `backend/scadbuddy/slicing/slice_info.py` reads `slice_info.config`.

### 5.5 Failure surface

A `SlicerError` with exit 192 is shown as the slicer's own message (the CLI prints the
validation failure to stderr before exiting; `stderr_tail` carries it). The preview
pane's `slice-error` alert shows it verbatim, and the run fails with it as its
`failure`. The row's failure text decodes details for a `SlicerError` only: `"The slicer
refused the plate: <stderr_tail>"` for 192, `"The slicer failed: <stderr_tail>"`
otherwise, and for any other error type `"The slicer worker failed (<type>)."`, never
its message (Ruling C4). A retryable exit (a crash) is retried by `slice_plate`'s
policy, four attempts in all (three retries; a worker shutdown and a heartbeat timeout
take attempts too), then fails the same way. A `TransferRefused` (an expired or refused
transfer URL) prepares again once, with new URLs (Ruling C4). No slicer worker taking the
activity within two hours fails it as "No slicer worker took the slice".

A run reports a failed slice by why it failed (`workflows/slicing.py` `failure_status`,
Task 13 review M3): 422 for a refusal of the plan (exit 192, a preset neither the tree
nor Bambuddy holds); 409 for a slice to start again (its row swept, the copy's bytes or
Bambuddy's process values changed since the start, the copy gone); 503 for what this
server lacks (no profiles, no content store or transfers, the native library not serving,
no Bambuddy where the copy is); 502 for any other failure, an exit 0 with no sliced 3MF
and a worker crash among them. The dialog's start, which builds the preset key before
the workflow exists, answers 422 for a refused preset and 503 for missing profiles or
content store; a slice that fails after it started shows its failure text in the
`slice-error` alert. An exit 0 that leaves no sliced 3MF (a plate with
nothing to slice) is a non-retryable `SlicerError` with details `{exit_code: 0,
stderr_tail}`, since the same input exports nothing again.

## 6. Tier 3: printers with one identity

### 6.1 The registry

A new table `printers` (and feature `backend/scadbuddy/printers/`): one row per
physical printer, keyed by `serial` (the Bambu serial, unique). Columns: `id` (uuid),
`serial`, `name`, `model` (the Bambu model code the serial implies, e.g. `H2C`),
`bambuddy_printer_id` (nullable; the id in the configured Bambuddy), `lan_host`
(nullable), `lan_access_code` (nullable, sealed under the KEK like the agent's secrets
are, through the same envelope format), `preferred_path` (`bambuddy` | `native` |
`auto`), `created_at`, `updated_at`.

A printer can have the Bambuddy link, the LAN link or both. Linking is explicit and
reversible: `POST /printers` creates from a serial; `PUT /printers/{id}/bambuddy`
links a Bambuddy printer id; `PUT /printers/{id}/lan` sets host and access code and
verifies them by connecting and reading `pushing.pushall` once; `DELETE` on either
unlinks. A Bambuddy printer whose serial is known (Bambuddy's status payload carries
it even though its `Printer` model does not; the builder confirms the field on a live
Bambuddy and records where) is offered for linking in the UI with its serial
pre-filled.

### 6.2 One print request, two paths

Every route that prints today takes a Bambuddy printer id in its request
(`PrintRunRequest`'s target). It takes a `printer_id` from the registry instead, with
the Bambuddy id still accepted and resolved to the registry row that links it (a
request naming an unlinked Bambuddy printer creates the row on the fly, with only the
Bambuddy link, so nothing a user has today breaks). The registry id is the printer's
identity on every surface a person or the agent sees: request bodies, `/printers/now`,
the `activity` rows, the agent's printer tools and the frontend's printer list (tier
3a). The tables that key on the Bambuddy integer today (`settings.printer_id`, the
`rack_nozzle_*` usage tables, `project_print_targets`, remembered bed types and print
choices) keep their key in 3a and are read through one resolver,
`printers.resolve_bambuddy_id`; moving them to the registry id, with a backfill from
the link, is tier 3b, its own plan. A LAN-only printer therefore has no remembered
choices until 3b; that is the one gap 3a leaves, and it is listed in the plan.

The path is chosen by the row's `preferred_path`: `auto` picks native when the LAN
link is set and reachable, else Bambuddy. `PrintRunWorkflow` branches after the slice:
the Bambuddy path is today's `print_enqueue` and `FollowPrint`; the native path is two
new activities on the existing `bambuddy` queue (the print worker; no queue is
renamed): `lan_upload` (fetches the sliced 3MF through the internal blob route of
§5.2, since the print worker has no store, then FTPS it to the printer's storage; the
printer's own name for the file is the run id) and
`lan_print` (`print.project_file` over MQTT with `param` naming the plate's gcode in
the file, `use_ams`, `ams_mapping` and `ams_mapping2` computed by ScadBuddy from the
plan's trays (`printers/ams_mapping.py`, hot-path Task 7), and on an H2C rack print
`nozzle_mapping` (`printers/nozzle_mapping.py`, Task 8: 32 entries of physical nozzle
ids, `1` the fixed hotend, 16-21 the rack positions, `-1` unused, omitted when no slot
prints from the rack; measurements in §2),
`bed_type` from the plan, `subtask_name` the output's name), then `FollowPrint`'s
native variant, which subscribes to `device/<serial>/report` and publishes the same
`print.*` events the Bambuddy follower does, from `print_percent`, `layer_num`,
`mc_remaining_time`, `gcode_state` and the `print_error` code.

The printer's live state (`/printers/now`, the print dialog's trays, the nozzle
plan's inputs) comes from the LAN link first (hot-path plan D4): on a native run it is
always the cached report, and Bambuddy's `printer_status` is read only for a printer
whose path is `bambuddy`. Without a LAN link it comes from Bambuddy's `printer_status`. The
report is the MQTT report cached by a `printer_watch` long-running activity per
LAN printer (one workflow `printer-<serial>`, continue-as-new daily) that writes the
latest report into `printer_reports` (one row per printer, `report jsonb`, `seen_at`)
and publishes `printer:<id>` realtime events. The `PrinterStatus` model becomes the
registry's model; the Bambuddy mapper fills it from Bambuddy's shape, the LAN mapper
from the report's `ams`, `vt_tray`, `nozzle_rack` and `print` sections.

### 6.3 What stays Bambuddy-only in this tier

Spools and inventory, archives, timelapse, projects, the queue's ordering. A native
print on a Bambuddy-linked printer still records to Bambuddy's archive when Bambuddy
sees the print through its own connection; the linking code (`bambuddy/linking.py`)
matches by `content_hash` as it does today. ScadBuddy keeps its own `output_last_prints`
either way, in intent: a LAN print records no queue item, archive or `output_last_prints`
row today (#4644).

### 6.4 Credentials

The access code is a secret: sealed at rest with the KEK the agent and flows already
use (`SCADBUDDY_SECRET_KEY_FILE`), the AAD binding it to the printer's `id` and
`serial`, never returned by any route (a `has_access_code: true` flag instead), never
logged, never in a trace attribute. The print worker reads it only inside `lan_upload`,
`lan_print` and `printer_watch`.

## 7. Cross-cutting: Rust workers

### 7.1 Why Rust, and how it enters

The user's call. Rust gives a worker with no GIL, predictable memory, a single static
binary in a slim image, and a GA Temporal SDK with worker versioning. Go was the
alternative; the Go tools in `tools/go/` stay as they are.

A new workspace `workers/rust/` with one crate per worker binary and shared crates for
the store client, the blob types and the Temporal wiring:

```
workers/rust/
  Cargo.toml                    # workspace
  crates/sb-transfer/           # TransferUrl GET/PUT streaming with sha256 (reqwest); archive pack/unpack
  crates/sb-temporal/           # worker bootstrap: connect, versioning, OTel, /healthz + /metrics on 9090
  crates/sb-slicer/             # slice_plate (tier 2)
  crates/sb-render/             # thumbnail, split (§7.3), as library functions
  bins/scadbuddy-slicer/        # the slicer worker binary (Dockerfile stage `slicer`)
  bins/scadbuddy-render-tools/  # a CLI the Python render worker runs beside openscad (§7.3)
```

Payloads cross the history as JSON through the Python side's pydantic converter, so
each Rust activity's input and output is a `serde` struct that mirrors a pydantic
model in `backend/scadbuddy/workflows/models.py` (the CLI's arguments and its JSON
result on stdout are pinned the same way); a vector file per pair
(`workers/rust/vectors/<activity>.json`, written by the Python tests, opened by the
Rust tests) pins the two, the way `secret-vectors.json` pins the agent and
agent-durable. Flows' sealed payloads (`payload_codec.py`) never reach these queues.

Tracing: `tracing` + `tracing-opentelemetry` with the standard `OTEL_*` variables,
scrubbed the way `core/trace_scrub.py` scrubs (no parameter values, no stderr), and
the Temporal SDK's own interceptor so a Rust activity's span is a child of the
workflow's. The `scadbuddy-renders` dashboard reads `scadbuddy_render_stage_seconds`
by stage, so the Rust activities emit the same histogram under the same name.

### 7.2 Two ways Rust enters

Workflows stay Python until there is a reason to move one. Rust enters in two shapes:

- **A Temporal worker** for work that is an activity of its own and needs nothing from
  the render worker's disk: the slicer (§5.2). It is a new queue, a new image and a
  new deployment.
- **A CLI beside the Python activity** for the stages that are steps inside one
  activity today. `thumbnail`, `split` and the piece's publish are not activities: they
  run inside `finish_piece` (`workflows/activities.py`) on the render worker's local
  directory for the piece. Moving one to another pod would re-add a store round trip per
  stage, which is what this design removes. So they become subcommands of one Rust
  binary, `scadbuddy-render-tools`, built in a Dockerfile stage and copied into the
  `app` image beside `openscad`, and the Python activity runs it as a subprocess
  (`render/template_process.py` already runs template code that way). No Temporal
  SDK, no new queue, no change to the pinned `RenderPiece` history.

### 7.3 Migration order for the render path

By the measured cost, each stage behind its own setting (`render_rust_stages`, a set,
default empty, applies live; a stage not in the set runs the Python code):

1. `thumbnail` (24 s on a 15 MB model today; the numpy rasteriser with Python loops).
   `scadbuddy-render-tools thumbnail <layout.json> <out.png>`: a software rasteriser over
   the same `layout.json` and camera, producing the same PNG dimensions; pixel-compared
   against the Python one on the fixtures with a tolerance the builder measures.
2. `split` (7 s): `scadbuddy-render-tools split <model.3mf> <out dir>`, the 3MF split
   by per-triangle `p1`, streaming the mesh.
3. `solids` fan-out (14 s serial today): one OpenSCAD run per colour. Parallelism here
   is a Python change (a bounded pool of subprocesses in `render_solids`), not a Rust
   one; the builder measures whether it pays before touching it.
4. The piece's pack and publish (`staged_pack`, the upload) and `fetch`'s unpack as
   `scadbuddy-render-tools pack|unpack|put|get`, streaming, which also removes the
   whole-blob `read` paths (§4.1).
5. `RenderPiece` as a Rust Temporal worker end to end, when 1 to 4 have shown the shape
   works and the stages exist as Rust library code (`sb-render`).

### 7.4 Temporal server

Prod runs 1.28.4. Nothing here needs more than that: the Rust SDK 1.1 works against it,
and one-activity workflows stand in for standalone activities. When clusters#2146 lands
1.31 or later, `SliceWorkflow` and any other one-activity wrapper become standalone
activities started from Python (`client.start_activity`), and the Rust side changes
nothing.

## 8. Rollout and settings summary

| Setting | Values | Applies | Default |
|---|---|---|---|
| `store_backend` | `local` / `bambuddy` / `s3` | restart | unchanged |
| `store_s3_*` | §4.2 | restart | unset |
| `library_backend` | `bambuddy` / `native` | restart | `bambuddy` |
| `slicer_backend` | `bambuddy` / `native` | live | `bambuddy` |
| `render_rust_stages` | subset of `thumbnail, split, solids, store` | live | empty |
| per printer `preferred_path` | `bambuddy` / `native` / `auto` | live | `bambuddy` for a row created from a Bambuddy id, `auto` otherwise |

Order on prod: deploy Garage, run `store_migrate`, flip `store_backend` (tier 1);
deploy the slicer worker, flip `slicer_backend` (tier 2; `README.md` "Rolling out" has
the steps). The slicer image and the API image deploy together, from one build: a
native slice's preset key carries the API image's `BAMBU_STUDIO_VERSION`, which must be
the version the slicer runs (Ruling C7), or the key would name a slicer that did not
cut the file; link printers, set
`preferred_path` per printer (tier 3); turn on `render_rust_stages` one at a time as
each lands.

## 9. Open decisions

- **Garage or MinIO.** Garage is lighter and built for a few nodes; MinIO is the
  reference S3 and better known. Either satisfies §4; the clusters repo decides.
- **Which Bambu Studio version to pin first.** 02.07 matches `BAMBU_APPLICATION`
  in `render/bambu3mf.py`; the builder confirms the AppImage runs headless on the
  slim image (it may need `libgl1` and `libgtk-3-0` even for the CLI, as the
  bambu-studio-api image installs).
- **Preset flattening semantics for `inherits` chains with `+`/`-` list edits.** Bambu
  Studio's preset loader has rules for `filament_settings_id` and vendor lists; the
  builder reads `PresetBundle::load_vendor_configs_from_json` and records them in the
  plan.
- **The native follower's error table.** Bambu's `print_error` codes to messages are
  a table in Bambu Studio's resources (`hms`); the builder decides whether to ship it
  or show the code.

## 10. Calls made after the builder check

A reviewer read this document cold and asked five things. Settled here, as the
author's calls:

- Rust render stages are a CLI the Python activity runs, not Temporal activities on
  another pod (§7.2), because the stages share one local directory inside `finish_piece`.
- The registry id reaches every user-facing surface in 3a; the Bambuddy-keyed tables
  move in 3b (§6.2).
- The native slicer reaches every store backend through transfer URLs the API mints,
  and holds no credentials of its own (§5.2). The same route gives the print worker
  the sliced blob (§6.2).
- Non-Python workers read no settings and hold no database role: environment for
  their own wiring, transfer URLs for bytes (§5.2).
- No task queue is renamed (§6.2).

## 11. What the builder decides

Part sizes, the S3 client's connection pool, the exact heartbeat cadence (the fifo
emits often; coalesce to at most one heartbeat a second), the Rust crate names'
prefix, the dashboard panel for the slicer, the UI for linking a printer (the spec
fixes the routes, not the form), and the fixture set for the thumbnail comparison.
