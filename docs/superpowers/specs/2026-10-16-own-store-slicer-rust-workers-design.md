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

On `store_backend=s3` the caller mints presigned GET and PUT URLs (two hours, longer
than any slice). On `bambuddy` or `local` it mints URLs to new internal API routes,
`GET` and `PUT /api/v1/internal/blobs/{key}` (`api/internal.py`, a per-call signed
token as `RemoteOutputs` already uses for outputs), and the API moves the bytes to and
from its store. So the native slicer works on every store backend, tier 2 does not
depend on tier 1, and the only credentials outside Python are the API's internal key
the print worker already holds. The caller records the `store_blobs` row for the
output after the worker reports its sha256 and size (`slicing/transfer.py`:
`mint_get`, `mint_put`, `finish_put`). A new `BlobKind`, `slice`, swept like `piece`.

The worker's whole configuration is its environment: `SCADBUDDY_TEMPORAL_ADDRESS`,
`SCADBUDDY_TEMPORAL_NAMESPACE`, `SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER`,
`SCADBUDDY_SLICER_CONCURRENCY` and the `OTEL_*` variables. It reads no settings and
needs no database role.

The activity downloads its inputs into a per-attempt temp dir, writes a fifo, starts the CLI with `--pipe <fifo>
--load-settings <machine>;<process> --load-filaments <f1;…> --slice <plate>
--export-3mf <out> [--arrange …] [--allow-newer-file] [--debug N] <input>`, reads the
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

Flattening reads the profiles dir shipped in the slicer image, mirrored into the store
as a `preset_bundle` blob per Bambu Studio version at image build time, so the Python
side reads one archive and never needs the binary. `local:` presets (Bambuddy's
OrcaSlicer imports) are still fetched from Bambuddy when a Bambuddy is configured, so
a user's custom filament keeps working on both slicers. `preset_key` stays the dedupe
key, extended with the slicer's name (`bambuddy` or `native`) and the Bambu Studio
version.

### 5.4 Where the slice sits in the print flow

`PrintRunWorkflow` today runs `print_slice_start` and `print_slice_wait` per plate
against Bambuddy. A `slicer` setting (`slicer_backend`: `bambuddy` | `native`, applies
live, default `bambuddy` until the owner flips it) chooses: with `native`, the
workflow runs `slice_plate` instead, behind `workflow.patched("native-slicer")`, and
the sliced blob replaces the "sliced library file" in `print_record` and
`output_bambuddy_slices` (a new nullable `sliced_blob_key` column beside
`sliced_library_file_id`). The print dialog's preview slice (`start_preview`,
`read_preview`) gets the same switch: with `native`, `preview_slice` starts
`slice_plate` as its own one-activity workflow (`SliceWorkflow`, one activity, no
other logic, until Temporal 1.31's standalone activities let it be the bare activity)
and `read_preview` reads the workflow's heartbeat details for progress. The browser
polls as it does today (every 2 s), and the heartbeat's `total_percent` rides that
answer, so the pane shows a progress bar instead of "Slicing…"; no realtime topic
(realtime events carry ids, not content).

Filament requirements (grams per slot, side) and filament changes today come from
Bambuddy's `filament-requirements` and ScadBuddy's own G-code parse. With the native
slicer both come from the sliced 3MF: `Metadata/slice_info.config` holds per-filament
`used_g` and `used_m`, and the existing `M620 S<n>A` count on `Metadata/plate_N.gcode`
stays. A new `backend/scadbuddy/slicing/slice_info.py` reads `slice_info.config`.

### 5.5 Failure surface

A `SlicerError` with exit 192 is shown as the slicer's own message (the CLI prints the
validation failure to stderr before exiting; `stderr_tail` carries it). The preview
pane's `slice-error` alert shows it verbatim, and the run fails with it as its
`failure`. A retryable exit (a crash) retries three times with the activity's retry
policy, then fails the same way. An exit 0 that leaves no sliced 3MF (a plate with
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
the file, `use_ams` and `ams_mapping` from ScadBuddy's nozzle plan and tray colours,
`bed_type` from the plan, `subtask_name` the output's name), then `FollowPrint`'s
native variant, which subscribes to `device/<serial>/report` and publishes the same
`print.*` events the Bambuddy follower does, from `print_percent`, `layer_num`,
`mc_remaining_time`, `gcode_state` and the `print_error` code.

The printer's live state (`/printers/now`, the print dialog's trays, the nozzle
plan's inputs) comes from whichever link answers: Bambuddy's `printer_status` when
linked, else the MQTT report cached by a `printer_watch` long-running activity per
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
either way.

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
deploy the slicer worker, flip `slicer_backend` (tier 2); link printers, set
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
