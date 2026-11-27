# Outputs in Postgres and the blob store — design

Date: 2026-11-23. Status: approved by the owner, 2026-11-23.
Epic: #7254. Phases: #7255 (records), #7256 (bytes), #7257 (cut-over).
Found with the diagnostics of #7245.

## 1. Intent

An output (a saved render) is today a folder on the NFS share,
`data/outputs/<slug>/<output-id>/`, with `meta.json` as its record. To find an
output by id, `OutputStore._find_dir` (`backend/scadbuddy/library/outputs.py`) globs
`outputs/*/<id>/meta.json` over every model's folder. A glob that finds nothing is not
cached. So a listing that names outputs which were deleted globs NFS again on every
request.

Measured on 2026-11-23, prod:

- One glob miss on the share took 0.2–0.4 s, in an API pod. That is the cost of one glob
  over every model's folder (158 of them), not of one file operation.
- The mount's `GET /api/v1/print/library` took 1.5–13 s, against 0.1 s for other
  routes. Some traces had a 4–7 s gap with no span after the last SQL query, where
  `api/outputs.py` `_existing` globs once per output id.
- `/proc/<pid>/fd` samples in an API pod showed many models' `meta.json` and
  `model.3mf` files open during normal use.

The owner asked why this is not "just a DB" and chose to migrate. The goal is that
Postgres is the record of every output and the blob store (`ContentStore`, S3 on DO
Spaces in prod) holds its bytes, as the native print library already does
(`print_library/native.py`, `print_library/store.py`). The NFS folder is then not read
or written.

Owner direction that applies (2026-11-23): ScadBuddy owns its records; no new
dependency on Bambuddy data. Repo rules that apply: only the API process holds SQL
(root `CLAUDE.md`, "Database access"); state goes in Postgres, bytes in the blob store,
never `bytea`.

## 2. Goals, non-goals, anti-goals

Goals

- An output found by id is one indexed SQL read. No code path globs or scans the
  outputs tree.
- Every output's record is a row; every output's bytes are blobs.
- The existing outputs move too. Orders, print runs, library files and prints point at
  them by id.
- The mount's listing loses the `_existing` glob in the first phase.

Non-goals

- `data/models/` (the catalogue's `model.json` and sources). It is on NFS too; it is a
  separate migration.
- The render worker, slicer and print worker. They already reach outputs over HTTP or
  by blob; their interfaces do not change.
- The mount (`tools/go/bambuddy-fs`) changes (background refresh, write-back flush).
  Separate work.
- Tracing. The span work is a separate PR, already in progress.

Anti-goals (a failure even if it works)

- Any output lost, or any byte of one changed (the 3MF's sha256 must match).
- An output id, URL or API response shape that changes.
- Two sources of truth that stay after the move: the NFS fallback is removed at the
  end, not kept.
- A slower create or save. Prod Postgres has 1–2 s commit latency (known): a create
  writes its rows in one transaction, not one per file.

## 3. Approaches

1. **In phases, behind `OutputStore` (chosen by the owner).** Move the records first
   (indexed lookups at once, the glob removed), then the bytes, one path-based caller
   at a time, then copy the existing outputs and remove NFS.
2. **All at once.** One change moves records, bytes and data. Fewer steps, but one
   review of about 32 files, and the speed fix waits until the end.
3. **Only make `output_index` authoritative.** Small and fast, but the bytes stay on
   NFS, so it does not meet the goal.

## 4. Design

### 4.1 What an output holds today

| File | Size | Changes after create | Goes to |
|---|---|---|---|
| `meta.json` | small | `rename`, `mark_storefront` | `outputs` row columns + `meta` jsonb |
| `params.json`, `inputs.json` | small | no | `outputs` jsonb columns |
| `record.json`, `manifest.json` | small | `attach_backfill` | `outputs` jsonb columns |
| `bom.json`, `arranged_from.json` | small | no | `outputs` jsonb columns |
| `backfill.json` | small | written, then deleted | `outputs.backfill` jsonb, null when none |
| `geometry*.json`, `geometry-attributed*.json` | small | rewritten per analysis version | `output_derived` rows |
| `model.3mf` | KB–MB | no | blob, kind `output` |
| `preview.glb` | KB–MB | no | blob, kind `output` |
| `thumbnail.png` | KB | user upload, drawn once | blob, kind `output` |
| `files/` | any | no | one blob per file, kind `output` |

### 4.2 Tables

- `outputs`: one row per output. `output_id` text primary key, `slug`, `name`,
  `created_at`, `source` (as `output_index`), `params_text` (as `output_index`), then
  `meta`, `params`, `inputs`, `record`, `manifest`, `bom`, `arranged_from`, `backfill`
  as jsonb, `has_plate_cover` boolean (the 3MF has `plate_1.png`, found once at
  create), and one blob key column per byte role (`model_blob`, `preview_blob`,
  `thumbnail_blob`, null when absent), `copied_at` (when a writer last wrote the row;
  §6.5 compares the folder's files with it) and `damaged` (jsonb, the missing byte
  roles, empty when none; §6.3). Indexes as `output_index` has (created, slug).
  The last-print overlay stays in `output_last_prints`.
- `output_files`: `(output_id, name)` → blob key, size, for the `files/` folder.
- `output_derived`: `(output_id, kind, plate)` → `version`, `data` jsonb, for the
  geometry caches. A version mismatch is a miss, as today.
- `outputs` replaces `output_index`. Its readers move to `outputs`, and the last
  phase drops `output_index` (new migration file; merged migrations are never edited).
- As built in phase 1 (#7255, `migrations/20261124T0053Z_outputs_records.sql`):
  `outputs` has the record columns (`output_id`, `slug`, `meta`, `params`, `inputs`,
  `record`, `manifest`, `bom`, `arranged_from`, `backfill`, `has_plate_cover`,
  `created_at`), plus `copied_at` and `files_stamp` for the copy (§6.5). `name`,
  `source` and `params_text` stay in `output_index` in this phase (phase 3a adds them to
  `outputs`, below; the storefront rule `saved_by_storefront_run` and the list's query
  are in `library/output_query.py` since then). The blob key columns,
  `output_files` and `output_derived` come with #7256, the phase whose code writes them.
  Its indexes are `(slug, created_at)` and one on the rows with a `backfill`. Each run
  of the copy is a row of `output_copy_runs`.
- As built in phase 2 (#7256, `migrations/20261124T0245Z_output_bytes.sql`): `outputs`
  gains `model_blob`, `preview_blob`, `thumbnail_blob` (NULL when the role has no blob),
  `bytes_stamp` (the byte copy's stamp of the byte files), `bytes_at` (set when the
  blobs hold the bytes; NULL while only the files do) and `damaged` (jsonb, `[]` when
  none), with an index on the rows whose `bytes_at` is NULL. `output_files` is as above.
  `output_derived` holds, per `(output_id, kind, plate)`, the kinds `geometry`,
  `attributed`, `asset_ids`, `plate_names` and `plate_cover` (plate 0 is the whole
  output; a plate cover row names the key of a small blob, or none). Each run of the
  byte copy is a row of `output_byte_runs`.
- As built in phase 3a (#7257, `migrations/20261125T0758Z_outputs_cut_over.sql`):
  `outputs` gains `name` and `source` (stored columns generated from `meta`; `source` is
  `back_office` when `meta` has none) and `params_text` (a plain column, filled from
  `output_index` and written by each save and each `OutputIndex.record`; NULL for a row
  that a phase 2 pod saves), with the index `outputs_created` (`created_at`,
  `output_id`, both descending). `output_cutover` holds the cut-over mark, one row (key
  column `one`, as `ONLY` is a reserved word), with its time and the byte run it
  followed. `output_claims` holds the id claims (`output_id`, `slug`, `claimed_at`).
  `output_index` and `output_hold_slugs` stay until phase 3b drops them.
- As built in phase 3b (#7257, `migrations/20261125T1737Z_outputs_drop_output_index.sql`):
  the migration first refuses to run while the cut-over mark is missing (unless
  `outputs` and `output_index` are both empty) or while a row has `bytes_at` NULL. Then it
  fills `params_text` of the rows a phase 2 pod saved from their `output_index` rows, makes
  it `NOT NULL DEFAULT ''`, and drops `output_index` and `output_hold_slugs`. `copied_at`,
  `files_stamp`, `bytes_stamp`, `output_copy_runs` and `output_byte_runs` stayed, unwritten,
  until `migrations/20261127T0259Z_outputs_drop_copy_state.sql` (#7887, plan D11) dropped
  them, with `damaged` and `output_cutover`.

### 4.3 Blobs

- A new `BlobKind` `output` (`store/content_models.py`). Key
  `output-<output_id>-<role>` (and `output-<output_id>-file-<hash>` for `files/`: the
  first 32 hex digits of the name's sha256, so a name added or gone moves no other key).
- Each output holds its blobs in `blob_refs` as `(OUTPUT_HOLDER, output_id)`, the same
  holder its manifest parts already use (`hold_parts`). Delete releases them. The
  sweep removes unheld blobs only for kinds in `SWEPT_KINDS`
  (`store/content_models.py`), which holds `piece`, `snapshot`, `slice`, `workspace`
  and `media` today. Phase 2 adds `output` to it. Without that, the blobs of a failed
  save stay in the store.
- Readers that need bytes get bytes (`content.read` / streamed `get`). A reader that
  needs a path (the slicer, `zipfile` on a large 3MF, `analyze_3mf`) gets a temporary
  local file from one helper, on the pod's local disk (not NFS), removed after use.
- Downloads stream from the blob store; the API never holds a whole large file in
  memory.

### 4.4 Interface

`OutputStore` keeps its public methods. Its internals change from paths to rows and
blobs. Methods that return a `Path` today (`directory`, `file_path`, `thumbnail_path`,
`plate_cover`, `plate_cover_archive`) are replaced, caller by caller, with methods that
return bytes, a stream or the temporary-file helper. `directory` is removed at the end.

The `_dirs`, `_gone` and `_covers` in-process caches are removed: a row read replaces
them, and the API runs several replicas, so per-process caches were never shared
anyway.

The fallback cover of a model becomes a query: the oldest output of the slug with
`has_plate_cover`, invalidated by nothing (the row changes with the output).

### 4.5 Phases

1. **Records** (#7255). Create `outputs` (`output_files` and `output_derived` are
   deferred to #7256, which writes them). A create writes
   the row in one transaction (today `OutputIndex.record` writes `output_index` on its
   own connection and only logs a failure; see §6.6), and still writes the files. Lookups by id, `list_for`, `name_and_slug`, `_existing`, the cover
   choice and `pending_backfills` read rows. A copy fills a row for every
   existing output from its `meta.json` and the other small files (idempotent). It is
   not an operation kind: it runs as a task at each API start, and as the housekeeping
   sweep `housekeeping_copy_outputs` (`workflows/housekeeping.py` `OUTPUT_COPY_SWEEP`)
   on the sweeps' Schedule, under an advisory lock (`library/output_copy.py`,
   `main.py` `_copy_outputs_logged`). Until a run finishes, a missing row falls back to
   the files. This phase removes the glob from the hot path. (Phase 3b removed the copy
   and the fallback; see phase 3.)
2. **Bytes** (#7256). Creates also put the 3MF, preview, thumbnail and `files/` as blobs. Move
   each path-based reader (downloads, preview, thumbnail, `files/`, `analyze_3mf`,
   `read_provenance`, `stamp`, `plates_of`, `analyzers/gather.py`,
   `print_library/filing.py`, `library/backfill.py`, the asset sweep's 3MF scan) to
   bytes or the temporary-file helper. The copy also uploads existing
   outputs' bytes and checks each 3MF's sha256 against the file.

   As built (#7256, #7477): a save writes the folder, uploads its bytes with no index row,
   then inserts the row, writes the index rows of the output's keys naming those objects,
   and writes its `output_files` rows and its `blob_refs` holds, in one transaction: one
   commit per save (`docs/architecture/data.md` has its rules for a lost reuse and a
   deadlock). Until that commit the save holds a shared advisory lock on each object it
   uploaded, on a small pool of its own, and every removal of an object claims that lock;
   what a save that died left with no row, the unrecorded sweep removes past its grace,
   on `s3` and in `local`'s own output store (data.md). A save that fails on the row (an id
   another output holds) moved no key of that output, so it keeps its bytes (#7478
   review). A put into a full store is a 507 and leaves no folder, row or hold. The keys
   are `output-<id>-<role>` and `output-<id>-file-<hash>` (the first 32 hex digits of the
   name's sha256), held by `(output, <id>)`. A thumbnail upload puts its blob first,
   under `output-<id>-thumbnail-<sha256>`, then names it under a Postgres advisory lock
   per output. Readers read the blob first and the file when the row names none or the
   blob is lost (`library/outputs.py` `read_role`); a reader that needs a path gets a
   temporary file and runs in a thread. What readers derive of the model is kept once in
   `output_derived`; the plate cover is a blob under `output-<id>-plate_cover-<sha256>`.
   The byte copy (`library/output_byte_copy.py` `copy_output_bytes`) runs after the row
   copy at each API start and in the housekeeping sweep `housekeeping_copy_output_bytes`
   (`OUTPUT_BYTES_SWEEP`), under a session advisory lock that holds one autocommit
   connection of its own (not a pool's) for the whole run (in the sweep up to its timeout, 30 min,
   `OUTPUT_BYTES_TIMEOUT`; at a start with no bound). It compares
   each byte file with its blob by sha256, puts a changed one with a compare-and-swap,
   reads back what it put (and every blob of a row not copied before), and names the
   keys in batches. Before any put it checks the store's caps against the bytes and
   count of the rows still in files; past one it is `refused` and names the shortfall.
   That count can be more than it adds, never less: blobs a failed run already put are
   counted again until the sweep removes them. It is refused on `bambuddy` (§6.4). Phase 2
   refused it on `local` too (no content store, plan Ruling R1); since phase 3a, `local`
   keeps the outputs' blobs in a content store of their own under `data/output-blobs/`,
   with no caps, and the copy runs there (#7257); a store migration to `bambuddy` is refused while
   any `output` blob exists (since phase 3b, always). Phase 3b removed this copy and the
   file reads; see phase 3. `GET /outputs/copy-status` answers its last run as `bytes`,
   with `pending`, the rows whose bytes the files still hold, and the rows' damage from
   every run (`damaged_rows`, `damaged_sample`). A missing file never drops a key the row
   names: it is `damaged`. An outputs folder that is missing or lists no slug folder
   while rows exist, or an output's missing folder, makes the run `incomplete`, and those
   rows get no write; a start copies the bytes only after a row copy run that finished.

   Derived rows are written only while the model sha256 the reader read is still the
   row's (or the files hold the bytes). When the copy moves a copied row's model key, it
   deletes that output's derived rows (`OutputRows.drop_derived`). A copied row whose
   model blob is lost keeps no derived rows: readers fall back to the file and derive
   again until the blob is back. One window is accepted: an attributed analysis made
   from a cached geometry row read between the model swap and `drop_derived` can stay
   stale. It needs a hand-edited `model.3mf` and two reads within about one commit.
3. **Cut over** (#7257). When the copy reports every output copied and verified,
   remove the file fallback, the NFS writes, `meta.json` handling, the orphan and hold
   reapers' directory scans (they read rows), `OutputIndex.sync`'s glob, and
   `output_index`. The NFS folder is left in place, unread, as a backup; deleting it is
   the owner's step, outside this work.

   As built: two releases (plan `docs/superpowers/plans/2026-11-25-outputs-cutover-phase-3.md`,
   D1), because a phase 2 pod writes into an output's folder during a roll and fails when
   it is missing. Release 3a keeps the phase 2 reads and every folder write. The API
   writes the cut-over mark (`output_cutover`) when it gets both copies' advisory locks,
   the last row copy run and the last byte run finished (the byte run with no `refused`
   and no `failed`), and no row has `bytes_at` NULL. It tries at each start and after
   each byte run that ended. After the mark, both copies do nothing (checked before and
   under their locks); `GET /outputs/copy-status` names it as `cut_over_at`. After the
   mark, a write into a missing folder skips its file step, and the hold reaper reads
   rows only. The list and the print links read `outputs`. A save that names its id (a
   quote's attempt, #5390) claims it in `output_claims` before its folder. Other saves
   take a new id and no claim. As with a phase 2 pod's save, their holds are kept only
   while they are younger than the grace. A claim ends with the save's row, a failed
   save's release, or the reaper's prune past `ORPHAN_HOLD_GRACE`, which is so the claim's
   lease: a save longer than that can lose its claim (phase 3b keeps a live save's claim,
   below). A
   phase 2 pod that saves after the mark takes no table claim; its holds are kept only
   within the grace. A phase 2 pod's row copy after the mark can leave a row with
   `bytes_at` NULL; the runbook in `docs/architecture/data.md` "The cut-over" checks
   `pending` and names the recovery.

   As built in release 3b: the row and the blobs are the only record, and no code reads
   or writes `data/outputs/`. A save stages its files on the spool (`ContentStore.spool`,
   the pod's disk, plan D8), uploads them, and commits the row as in phase 2; it writes no
   folder and no `meta.json`. A reader reads the blob the row names; a lost blob is
   `OutputBytesLostError`, logged at error level and answered 500, never a file read
   (plan D9). The row copy, the byte copy, the file fallback, `OutputIndex` and its sync,
   and the reapers' directory scans are gone; the housekeeping run with
   `OUTPUTS_CUT_OVER_PATCH` schedules neither copy, and their activities stay as no-ops
   for replay until a follow-up (plan D4). A save that names its id claims it in
   `output_claims` only, and makes the claim young again while it runs
   (`claim_refresh_seconds`), so the reaper's prune never takes a live save's claim. The
   API does not start on the `bambuddy` store, even with no output, and a settings save
   or a store migration to it is refused (§6.4). The migration refuses to run without the
   mark or while a row has `bytes_at` NULL (§4.2). `GET /outputs/copy-status` still
   answers the copies' last runs and `cut_over_at`.

Each phase ships as one or more PRs, each releasable on its own.

### 4.6 Tests

- Each phase runs the existing output tests against the row-backed store.
- The copy: a fixture tree of outputs (including a deleted output named by a
  library file, an arranged output, one with `files/`, one with a backfill) copies to
  rows and blobs, a second run changes nothing, and a corrupted 3MF fails the
  verification.
- A test that no output code path calls `glob` on `paths.outputs` once phase 3 lands.

## 5. Constraints

- Only the API holds SQL; all of this is in the API process (with its in-process
  `library` worker). No worker gains a database client.
- Migrations: one new file per change, `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`.
- Prod commit latency of 1–2 s: one transaction per create, never per file.
- The blob store has a quota check on put (seen in the upload trace); existing outputs'
  bytes count against it.
- `docs/architecture/data.md` and README.md lines 246-247 (the "**State** lives in `/data` …: models (a git repository),
  outputs, downloaded fonts and caches. Back up the volume." bullet)
  change with phase 3.

## 6. Decisions from the builder check (author's calls; the owner may change them)

1. **The phase 1 fallback ends when the copy ends, not at phase 3.** A copy run that
   walked every folder records its end in `output_copy_runs.finished_at`. Every replica
   reads it (`OutputRows.copied`). A run whose walk may have missed folders (a listing
   failed, or the outputs folder read empty while rows exist) records `incomplete` and
   no `finished_at`, so it does not end the fallback.
   After that, a missing row is a missing output: a deleted output that a listing names
   costs one SQL read and no glob. Until then, a miss falls back to the files, as
   today. The copy runs at each API start, so the speed gain lands in phase 1.
2. **Outputs still die with their model, as today.** This work changes no product
   behaviour. Where `catalogue.py` deletes `outputs/<slug>` today (`delete`, `sweep_orphans`,
   `_clear_derived`), the same code path deletes the slug's rows first
   (`Catalogue._forget_outputs`), and from phase 2 releases their blobs. Whether outputs that an order names should outlive their model is a separate
   product question, outside this work.
3. **Damaged outputs do not block the cut-over.** The copy copies what it can read.
   A folder with no readable `meta.json` gets no row and goes on a "not copied" list.
   A folder with a readable record but a missing or unreadable byte file gets its row,
   marked `damaged` with the missing roles named. The copy checks the sha256 of every
   byte file it uploads: 3MF, preview, thumbnail and each `files/` entry. The cut-over
   needs every readable byte verified. The owner reviews the "not copied" list.
   Nothing is deleted from NFS by this work, so nothing is lost. In phase 1, which
   copies no bytes, `damaged` names each record side file (such as `bom.json`) that
   could not be read and was left out of its row.
4. **Quota and backends** (phase 2 and the cut-over). Output blobs count against the store's caps like every other
   kind. Before it starts, the copy measures the bytes it will add and refuses to
   start, naming the shortfall, if that would pass `store_max_total_bytes` or
   `store_max_count`. Raising a cap is then a settings change before the run.
   On `store_backend=bambuddy`, the cut-over refuses to run, because outputs must not
   go into Bambuddy's library (owner direction, 2026-11-23). `local` is allowed for
   development and tests. Prod runs `s3`.
5. **The copy reconciles; it does not only fill.** Each run compares every folder with
   its row. It reads the folder again and refreshes the row when the folder's
   `files_stamp` differs from the row's. The stamp is a sha256 of the sorted name, size
   and `mtime_ns` of the record files (`meta.json`, `params.json`, `inputs.json`,
   `record.json`, `manifest.json`, `bom.json`, `arranged_from.json`, `backfill.json`;
   `library/output_copy.py` `files_stamp`), so no NFS clock is compared with the
   database's. A save writes no stamp, so the next run reads that folder once. Live
   writers (a save, a rename, a backfill change) set `copied_at` to the database's
   clock. Before it reads a batch, the copy takes a mark from that clock. Its writes are
   guarded by the mark (`OutputRows.write_copy`): a refresh is `UPDATE … WHERE copied_at
   < mark`, and a new row is `INSERT … ON CONFLICT DO NOTHING`, so a live write wins.
   After each batch commits, `undo_copy` deletes each row it wrote at that mark whose
   `meta.json` is gone; the run's end checks the rows it inserted once more. A row whose
   folder the walk did not see is dropped only when its `copied_at` is before the run's
   start. When a listing or a stat fails (ESTALE, EIO), the run drops nothing and is
   `incomplete`. The copy runs at every start and in every housekeeping sweep
   (`OUTPUT_COPY_SWEEP`, on the sweeps' Schedule; sweeps off, `asset_sweep_interval` 0,
   leaves only the start's run). That sweep, not a manual re-run, is what catches the
   writes of old pods: a save, a delete, a rename or a backfill change that a pod on the
   older build wrote during the roll-out, or after a rollback, reaches the rows at the
   next housekeeping sweep. After a roll-out, confirm the pod images, then read
   `GET /outputs/copy-status` and check that a finished run started after the last old
   pod went. "A second run changes nothing" holds only when no folder changed between
   runs.
6. **The row is the record, so a failed row write fails the save.** The write order in
   `create` is: put the blobs (phase 2), then one transaction that inserts the row and
   its `blob_refs` holds. In phase 1 the files are still written first. If the
   transaction fails, the save fails, and the files written for it are removed, as
   `create` already does for a failure part way through. Blobs whose transaction
   failed are unheld, and the sweep removes them once `output` is in `SWEPT_KINDS` (§4.3). One transaction per save
   keeps the cost to one commit at the 1–2 s latency.

## 7. Left to the builder

- Column names and jsonb shapes beyond what §4.2 fixes.
- How the copy batches and reports progress.
- The temporary-file helper's location and cleanup.
- PR boundaries inside each phase.
