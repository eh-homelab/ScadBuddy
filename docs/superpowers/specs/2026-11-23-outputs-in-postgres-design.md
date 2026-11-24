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

- One glob miss on the share took 0.2–0.4 s (158 model folders), in an API pod.
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
  `thumbnail_blob`, null when absent). Indexes as `output_index` has (created, slug).
  The last-print overlay stays in `output_last_prints`.
- `output_files`: `(output_id, name)` → blob key, size, for the `files/` folder.
- `output_derived`: `(output_id, kind, plate)` → `version`, `data` jsonb, for the
  geometry caches. A version mismatch is a miss, as today.
- `outputs` replaces `output_index`. Its readers move to `outputs`, and the last
  phase drops `output_index` (new migration file; merged migrations are never edited).

### 4.3 Blobs

- A new `BlobKind` `output` (`store/content_models.py`). Key
  `output-<output_id>-<role>` (and `output-<output_id>-file-<n>` for `files/`).
- Each output holds its blobs in `blob_refs` as `(OUTPUT_HOLDER, output_id)`, the same
  holder its manifest parts already use (`hold_parts`). Delete releases them; the
  existing sweep removes unheld blobs.
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

1. **Records** (#7255). Create `outputs`, `output_files`, `output_derived`. A create writes
   the row in one transaction (today `OutputIndex.record` writes `output_index` on its
   own connection and only logs a failure; see §6.6), and still writes the files. Lookups by id, `list_for`, `name_and_slug`, `_existing`, the cover
   choice and `pending_backfills` read rows. A copy operation fills a row for every
   existing output from its `meta.json` and the other small files (idempotent,
   resumable, an operation kind like `store_migrate`). Until it finishes, a missing
   row falls back to the files. This phase removes the glob from the hot path.
2. **Bytes** (#7256). Creates also put the 3MF, preview, thumbnail and `files/` as blobs. Move
   each path-based reader (downloads, preview, thumbnail, `files/`, `analyze_3mf`,
   `read_provenance`, `stamp`, `plates_of`, `analyzers/gather.py`,
   `print_library/filing.py`, `library/backfill.py`, the asset sweep's 3MF scan) to
   bytes or the temporary-file helper. The copy operation also uploads existing
   outputs' bytes and checks each 3MF's sha256 against the file.
3. **Cut over** (#7257). When the copy operation reports every output copied and verified,
   remove the file fallback, the NFS writes, `meta.json` handling, the orphan and hold
   reapers' directory scans (they read rows), `OutputIndex.sync`'s glob, and
   `output_index`. The NFS folder is left in place, unread, as a backup; deleting it is
   the owner's step, outside this work.

Each phase ships as one or more PRs, each releasable on its own.

### 4.6 Tests

- Each phase runs the existing output tests against the row-backed store.
- The copy operation: a fixture tree of outputs (including a deleted output named by a
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
- `docs/architecture/data.md` and README:247 ("outputs live on the backed-up volume")
  change with phase 3.

## 6. Decisions from the builder check (author's calls; the owner may change them)

1. **The phase 1 fallback ends when the copy ends, not at phase 3.** The copy operation
   records "outputs copied" in a settings row when it finishes. Every replica reads it.
   After that, a missing row is a missing output: a deleted output that a listing names
   costs one SQL read and no glob. Until then, a miss falls back to the files, as
   today. The copy runs as the first step after phase 1 deploys, so the speed gain
   lands in phase 1.
2. **Outputs still die with their model, as today.** This work changes no product
   behaviour. Where `catalogue.py` deletes `outputs/<slug>` today (`sweep_orphans`,
   `_clear_derived`), the same code path deletes the slug's rows and releases their
   blobs. Whether outputs that an order names should outlive their model is a separate
   product question, outside this work.
3. **Damaged outputs do not block the cut-over.** The copy copies what it can read.
   A folder with no readable `meta.json` gets no row and goes on a "not copied" list.
   A folder with a readable record but a missing or unreadable byte file gets its row,
   marked `damaged` with the missing roles named. The copy checks the sha256 of every
   byte file it uploads: 3MF, preview, thumbnail and each `files/` entry. The cut-over
   needs every readable byte verified. The owner reviews the "not copied" list.
   Nothing is deleted from NFS by this work, so nothing is lost.
4. **Quota and backends.** Output blobs count against the store's caps like every other
   kind. Before it starts, the copy measures the bytes it will add and refuses to
   start, naming the shortfall, if that would pass `store_max_total_bytes` or
   `store_max_count`. Raising a cap is then a settings change before the run.
   On `store_backend=bambuddy`, the cut-over refuses to run, because outputs must not
   go into Bambuddy's library (owner direction, 2026-11-23). `local` is allowed for
   development and tests. Prod runs `s3`.
5. **The copy reconciles; it does not only fill.** Each run compares every folder with
   its row and overwrites the row when the folder's `meta.json` (or another record
   file) is newer than the row's `copied_at`. It runs again after each phase's
   roll-out, when no old pod remains (check the pod images first), so a rename or a
   backfill change that an old pod wrote during the roll-out reaches the row. "A second
   run changes nothing" holds only when no folder changed between runs.
6. **The row is the record, so a failed row write fails the save.** The write order in
   `create` is: put the blobs (phase 2), then one transaction that inserts the row and
   its `blob_refs` holds. In phase 1 the files are still written first. If the
   transaction fails, the save fails, and the files written for it are removed, as
   `create` already does for a failure part way through. Blobs whose transaction
   failed are unheld, and the existing sweep removes them. One transaction per save
   keeps the cost to one commit at the 1–2 s latency.

## 7. Left to the builder

- Column names and jsonb shapes beyond what §4.2 fixes.
- How the copy operation batches and reports progress.
- The temporary-file helper's location and cleanup.
- PR boundaries inside each phase.
