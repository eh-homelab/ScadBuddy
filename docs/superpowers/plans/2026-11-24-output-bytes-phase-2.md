# Output bytes in the blob store (phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every output's bytes (`model.3mf`, `preview.glb`, `thumbnail.png`, `files/`) and its geometry caches live in the blob store and in Postgres, and every reader reads them there, with the NFS files kept as the fallback until the byte copy finishes (#7256, epic #7254).

**Architecture:** A new `OutputBytes` (`library/output_bytes.py`) wraps the API's `ContentStore` for the new blob kind `output`: keyed puts, streamed reads, and one temporary-file helper for readers that need a path. `OutputStore` asks the row for a role's blob key first and reads the file only when there is none, or when the blob is missing. A save writes its files as in phase 1, puts their bytes, then inserts the row, its `output_files` rows and its `blob_refs` holds in one transaction. A reconciling byte copy (`library/output_byte_copy.py`) uploads and verifies the existing outputs' bytes at every API start and in a new housekeeping sweep, under a quota check, and refuses on the `bambuddy` store.

**Tech Stack:** Python 3.12, psycopg 3 + psycopg_pool, FastAPI, Temporal (housekeeping sweeps), pytest with the repo's Postgres fixtures and `tests/support/store.py` `local_content`.

**Spec:** `docs/superpowers/specs/2026-11-23-outputs-in-postgres-design.md` (§4.1–§4.5 phase 2, §6.2–§6.6). Phase 1 plan: `docs/superpowers/plans/2026-11-23-output-records-phase-1.md`.

## Global Constraints

- Only the API process holds SQL; all of this runs in the API and its in-process `library` worker (root `CLAUDE.md`, "Database access"). The render worker's blob-index allowlist (`api/crud_render.py` `READ_KINDS`, `RENDER_KEY`) never admits the `output` kind.
- Bytes go to `ContentStore`, never `bytea`. Images move as blobs and `{name, mediaType}` pointers, never base64.
- Migrations: one NEW file, `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_output_bytes.sql`; never edit a merged one.
- One transaction per save, never one per file (prod commit latency 1–2 s). The byte copy writes its rows in batches.
- No change to any output id, URL or API response shape. `GET /outputs/copy-status` only gains a field. `thumbnail_version` keeps its type; its value becomes the blob's sha256 prefix.
- Do not remove the NFS reads or writes. A save still writes the folder; every reader still falls back to the file. Phase 3 (#7257) removes them.
- Write order for a live writer of a byte role: the file first, then the blob (the byte copy's compare-and-swap depends on it, Task 9). A thumbnail is the exception: `put_thumbnail` puts the blob first, under its own key (`output_rows.staged_thumbnail_key`, held by nothing). Then one transaction under the output's thumbnail lock, a Postgres advisory transaction lock (`output_rows.lock_thumbnail`: `pg_advisory_xact_lock(hashtextextended('output-thumbnail:' || id, 0))`, so it holds across the API's replicas), points `output-<id>-thumbnail` at that blob, writes the file, then names the key in the row (`OutputRows.keep_thumbnail`). A failed file write leaves the old key and the old file; the sweep removes the new blob. The last step to run wins the file and the blob both.
- Blob keys: `output-<output_id>-<role>`, role in `model`, `preview`, `thumbnail`; `output-<output_id>-file-<n>`, `n` the 0-based index of the name in the sorted `files/` names (spec §4.3). Holder `(OUTPUT_HOLDER, output_id)`, `OUTPUT_HOLDER = "output"` (`library/outputs.py`).
- On `store_backend=bambuddy` no output blob is written, by a save or by the copy (spec §6.4, owner direction 2026-11-23). On `local` (`StoreBundle.content` is None) the bytes stay files (Ruling R1).
- The quota is checked before the byte copy uploads anything (spec §6.4).
- A periodic pass is an activity in `workflows/housekeeping.py` `SWEEPS`, behind its own `workflow.patched` id, with a replay history in `tests/fixtures/housekeeping_histories/`; never a loop in the API (`backend/CLAUDE.md`).
- Phase 1 rulings carry over: DB-clock marks (never Python's clock against the database's), guarded copy writes, stamps instead of mtime-versus-clock, an incomplete walk never finishes a run, rows before files on delete.
- Docs and comments in Simplified Technical English; no counts in docs.
- Run backend commands as `backend/CLAUDE.md` says (uv, a database of its own per parallel run).

## Rulings (the plan's calls where the spec is silent; the owner may change them)

- R1: On the `local` backend there is no `ContentStore`, so phase 2 writes no output blobs there and the byte copy records a refused run. Phase 3 must choose: give `local` a content store for outputs, or refuse the cut-over there. Cost if wrong: one wiring change in Task 4.
- R2: A save whose put fails with `StoreFullError` fails with 507, as a render's snapshot put does (`api/jobs.py`), and its folder and holds go. Cost if wrong: a fallback to files-only saves in Task 5.
- R3: The byte copy verifies each upload by reading it back from the backend, past the download cache (`ContentStore.verify`). It doubles the first copy's transfer. Cost if wrong: drop one call.
- R4: A model delete releases its outputs' byte holds (`output-*` keys) with their rows. It leaves the Parts holds as today (the reaper's documented cost). Cost if wrong: one `LIKE` clause.
- R5: A store migration to a `bambuddy` target is refused while the index holds an `output` blob, so outputs never reach Bambuddy's library through a migration. Cost if wrong: one check in `store/migrate.py`.
- R6 (revised after the Task 3 review): there is no blocking temp-file helper. Sync readers run as `async with blobs.local_file(...) as path: await asyncio.to_thread(reader, path)`: the download stays on the loop and only the reader takes a thread. A thread that waited for the loop's download deadlocked once its callers filled the default executor (reproduced with `max_workers=2`). A sync caller that cannot become async yet keeps its file path (the fallback) until it is turned async. Cost if wrong: callers turned async one by one.

## Review Focus

1. A row names a blob that the store lost (removed by hand, a bad sweep): the download, preview and thumbnail fall back to the NFS file, logged, and do not answer 500 (`test_download_falls_back_to_file_when_blob_missing`, Task 6).
2. An old pod uploads a thumbnail during the roll-out, after the byte copy copied the old one: the next copy run replaces the blob; a new pod's upload that races the copy is not undone (`test_byte_copy_takes_newer_thumbnail_file`, `test_byte_copy_does_not_undo_live_thumbnail`, Task 9).
3. An output deleted while the byte copy uploads its bytes: the row does not come back and no hold is left, so the sweep removes the blobs (`test_byte_copy_leaves_no_hold_for_deleted_output`, Task 9).
4. A store near its cap: the copy refuses before any upload and names the shortfall; a save into a full store answers 507 and leaves no folder, row or hold (`test_byte_copy_refuses_past_cap`, Task 9; `test_save_store_full_is_507_and_cleans_up`, Task 5).
5. A temp file is removed when the body raises or the reader is cancelled during the download or while the file is made, and the download never reopens its path (`test_local_file_removed_after_error`, `test_local_file_cancelled_during_download_leaves_no_file`, `test_local_file_cancelled_while_temporary_runs_leaves_no_file`, Task 3).

---

### Task 1: Schema and the `output` blob kind

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_output_bytes.sql`
- Modify: `backend/scadbuddy/store/content_models.py` (`BlobKind`, `SWEPT_KINDS`)
- Test: `backend/tests/test_output_rows.py` (schema smoke test), `backend/tests/crud/test_crud_render_allowlist.py`

**Interfaces:**
- Produces, in the migration (it opens with a comment that cites spec §4.2, §4.3 and #7256):
  - `ALTER TABLE outputs ADD COLUMN model_blob text, ADD COLUMN preview_blob text, ADD COLUMN thumbnail_blob text` (blob keys, NULL when the role has no blob yet), `ADD COLUMN bytes_stamp text` (the byte copy's stamp of the byte files it read; NULL for a save), `ADD COLUMN bytes_at timestamptz` (set by a save that put its blobs, or by the copy that wrote and verified them; NULL = the files still hold the bytes), `ADD COLUMN damaged jsonb NOT NULL DEFAULT '[]'` (`[{role, error}]` for the byte roles whose file was missing; empty when none; the name the design gives it, §4.2).
  - `CREATE INDEX outputs_bytes_pending ON outputs (output_id) WHERE bytes_at IS NULL`.
  - `output_files (output_id text NOT NULL REFERENCES outputs ON DELETE CASCADE, name text NOT NULL, blob_key text NOT NULL, size bigint NOT NULL, PRIMARY KEY (output_id, name))`.
  - `output_derived (output_id text NOT NULL REFERENCES outputs ON DELETE CASCADE, kind text NOT NULL CHECK (kind IN ('geometry', 'attributed')), plate int NOT NULL, version text NOT NULL, data jsonb NOT NULL, PRIMARY KEY (output_id, kind, plate))`.
  - `output_byte_runs (id bigserial PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz, incomplete boolean NOT NULL DEFAULT false, refused text, copied int NOT NULL DEFAULT 0, verified int NOT NULL DEFAULT 0, skipped int NOT NULL DEFAULT 0, needed_bytes bigint NOT NULL DEFAULT 0, needed_count int NOT NULL DEFAULT 0, failed jsonb NOT NULL DEFAULT '[]', damaged jsonb NOT NULL DEFAULT '[]')`.
- Produces: `BlobKind` gains `"output"`; `SWEPT_KINDS` gains `"output"` (an unheld output blob goes after the sweep's grace).

- [ ] **Step 1:** Write `test_output_bytes_tables_exist` (insert an `outputs` row with `model_blob`, one `output_files` row and one `output_derived` row; delete the output; both child rows are gone) and `test_render_worker_cannot_read_output_kind` (the allowlist test's pattern: a render-worker read of an `output-<id>-model` key is refused).
- [ ] **Step 2:** Run them; expect FAIL (no column; the allowlist test may already pass: keep it as the guard).
- [ ] **Step 3:** Write the migration; add the kind.
- [ ] **Step 4:** Run them, the migration-ledger tests `backend/CLAUDE.md` names, and `tests/store/`; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): output_files, output_derived and the output blob kind (#7256)`.

### Task 2: `OutputRows` holds blob keys, files, derived data and byte runs

**Files:**
- Modify: `backend/scadbuddy/library/output_rows.py`
- Test: `backend/tests/test_output_rows.py`

**Interfaces:**
- Consumes: Task 1's schema.
- Produces:
  - `Role = Literal["model", "preview", "thumbnail"]`; `ROLES: tuple[Role, ...]`.
  - `def output_blob_key(output_id: str, role: Role) -> str`, `def output_file_key(output_id: str, n: int) -> str`.
  - `@dataclass(frozen=True) class OutputFileBlob(name: str, blob_key: str, size: int)`.
  - `@dataclass(frozen=True) class OutputBlobs(model: str | None = None, preview: str | None = None, thumbnail: str | None = None, files: tuple[OutputFileBlob, ...] = ())` with `keys() -> list[str]`.
  - `OutputRow` gains `blobs: OutputBlobs = OutputBlobs()` and `bytes_at: datetime | None = None` (read from the new columns; `_COLUMNS` grows; `files` stay out of `get`).
  - `insert(row, conn=None)` also writes `row.blobs`' columns, its `output_files` rows and one `blob_refs` row `(key, OUTPUT_HOLDER, output_id)` per key, and sets `bytes_at = clock_timestamp()` when `row.blobs.model` is set, all in its one transaction.
  - `set_blob(output_id: str, role: Role, key: str) -> bool`: the column and the hold in one transaction; False when the row is gone (no hold written).
  - `files(output_id: str) -> list[OutputFileBlob]` (by name); `file(output_id: str, name: str) -> OutputFileBlob | None`.
  - `derived(output_id: str, kind: str, plate: int) -> tuple[str, Any] | None` (version, data); `write_derived(output_id: str, kind: str, plate: int, version: str, data: Any) -> None` (upsert; nothing when the row is gone).
  - The byte copy's reads and writes: `byte_states() -> dict[str, tuple[str, str | None, datetime | None]]` (`output_id → (slug, bytes_stamp, bytes_at)`); `@dataclass(frozen=True) class ByteWrite(output_id: str, blobs: OutputBlobs, stamp: str, damaged: list[dict[str, Any]])`; `write_bytes(writes: Sequence[ByteWrite]) -> set[str]` (one transaction: per write, update the columns, `bytes_stamp`, `damaged`, `bytes_at = clock_timestamp()`, replace its `output_files` rows, insert its holds `SELECT … WHERE EXISTS (row)`; returns the ids whose row still existed); `pending_bytes() -> int`.
  - Runs: `start_byte_run() -> int`, `end_byte_run(run_id: int, *, finished: bool, incomplete: bool, refused: str | None, copied: int, verified: int, skipped: int, needed_bytes: int, needed_count: int, failed: list[dict[str, Any]], damaged: list[dict[str, Any]]) -> None`, `last_byte_run() -> dict[str, Any] | None`.
  - Every path that deletes a row also deletes its byte holds (`DELETE FROM blob_refs WHERE holder_kind = 'output' AND holder_id = ANY(%s) AND key LIKE 'output-%%'`) in the same transaction: `delete`, `delete_slug` (Ruling R4), `drop_copied`, `undo_copy`, `drop_unseen`. Parts holds are not touched.

- [ ] **Step 1:** Write tests: `test_insert_with_blobs_writes_columns_files_and_holds`, `test_insert_failure_writes_no_hold`, `test_set_blob_on_deleted_row_writes_no_hold`, `test_files_are_sorted_by_name`, `test_derived_round_trip_and_version`, `test_write_bytes_skips_deleted_row_and_its_holds`, `test_delete_releases_byte_holds_not_part_holds`, `test_delete_slug_releases_every_byte_hold`, `test_undo_copy_and_drop_unseen_release_byte_holds`, `test_byte_run_round_trip`, `test_pending_bytes_counts_rows_without_bytes_at`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement. Writes that span tables take one connection and `conn.transaction()`.
- [ ] **Step 4:** Run them and `tests/test_output_*.py`; expect PASS; ruff and mypy on the file.
- [ ] **Step 5:** Commit `feat(outputs): OutputRows holds the outputs' blob keys, files and derived data (#7256)`.

### Task 3: `OutputBytes`, the blob side, and the temp-file helper

**Files:**
- Create: `backend/scadbuddy/library/output_bytes.py`
- Modify: `backend/scadbuddy/store/content.py` (add `verify`)
- Test: `backend/tests/test_output_bytes.py`, `backend/tests/store/test_content_store.py`

**Interfaces:**
- Consumes: `ContentStore`, `BlobRef`, `BlobScope`, `BlobData`, `StoreFullError`; Task 2's keys and `OutputBlobs`.
- Produces:
  - `ContentStore.verify(ref: BlobRef) -> bool`: downloads from the backend (never from `downloads`) and checks the sha256; False on `BlobCorruptError` or `BlobMissingError`.
  - `def usable(content: ContentStore | None) -> bool`: True only for a content store whose `name` is not `"bambuddy"`.
  - `@dataclass class OutputStream(size: int, sha256: str | None, chunks: AsyncGenerator[bytes], _source: AsyncGenerator[bytes] | None = None)`, with `async aclose()` (closes `chunks`, then `_source`, the backend's read) and `async with` (its exit calls `aclose`). Whoever takes a stream closes it, read or not.
  - `class OutputFileNameError(ValueError)` with `names: tuple[str, ...]`.
  - `class OutputBytes(content: ContentStore)` with:
    - `async put(key: str, data: BlobData, *, slug: str) -> BlobRef` (kind `output`, `BlobScope(slug=slug)`).
    - `async put_if_absent(key: str, data: bytes, *, slug: str) -> bool` (the index's `ref` first: a key that exists answers False with no hash or upload; then `ContentStore.replace(expected=None)`, the guard for a racing put).
    - `async replace(key: str, data: BlobData, *, slug: str, expected: str | None) -> BlobRef | None` (the copy's compare-and-swap on the index sha).
    - `async put_folder(directory: Path, output_id: str, slug: str) -> OutputBlobs`: `model.3mf` and `preview.glb` (required: a missing one raises `FileNotFoundError`), `thumbnail.png` when there, every `files/` entry; each opened and put a file at a time, never read whole. Before any put, a `files/` entry that is not a regular file or fails `FILE_NAME_PATTERN` raises `OutputFileNameError` with every such name.
    - `ref(key: str) -> BlobRef | None` (sync: `content.index.get`, SQL only, no backend call).
    - `async stream(key: str) -> OutputStream` (the first chunk is read before it returns, so a missing blob raises `BlobMissingError` here, not mid-response).
    - `async read(key: str) -> bytes`.
    - `local_file(key: str, suffix: str) -> AbstractAsyncContextManager[Path]`: the blob in a named temporary file under `content.spool` (made on first use; the pod's local disk, `SCADBUDDY_SPOOL_DIR`), removed on exit, error or not. The download writes to the file `mkstemp` opened, never the path again. A sync reader runs as `async with blobs.local_file(...) as path: await asyncio.to_thread(reader, path)` (Ruling R6).
    - `async verify(key: str) -> bool`.

- [ ] **Step 1:** Write tests (`local_content` fixture): `test_put_folder_puts_every_role_and_file`, `test_put_folder_without_preview_raises`, `test_put_if_absent_keeps_first`, `test_replace_refuses_moved_key`, `test_stream_raises_missing_before_first_chunk`, `test_local_file_holds_blob_bytes_then_is_removed`, `test_local_file_removed_after_error`, `test_local_file_cancelled_during_download_leaves_no_file`, `test_local_file_cancelled_while_temporary_runs_leaves_no_file`, `test_put_folder_refuses_a_name_an_output_may_not_hold`, `test_put_if_absent_uploads_nothing_when_key_exists`, `test_stream_aclose_closes_source_unread`, `test_usable_refuses_bambuddy_and_none`; in `test_content_store.py`, `test_verify_reads_backend_not_cache` (corrupt the backend object, keep the cache whole: `verify` is False).
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): OutputBytes, the outputs' blobs and their temporary files (#7256)`.

### Task 4: `OutputStore` reads blobs first, files as the fallback

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py`, `backend/scadbuddy/main.py` (lifespan: after `build_store`, `state.outputs.bytes = OutputBytes(content)` when `usable(state.store.content)`)
- Test: `backend/tests/test_output_store_bytes.py`

**Interfaces:**
- Consumes: Tasks 2–3.
- Produces on `OutputStore`:
  - `bytes: OutputBytes | None` (None: files only, as today).
  - `_blob_key(output_id: str, role: Role) -> str | None` (from `_row_of`; no glob once copied).
  - `async open_role(output_id: str, role: Role) -> OutputStream | None`: the blob; on `BlobMissingError`/`BlobCorruptError` a warning and the file; the file when the row has no key; None when neither exists.
  - `local_role(output_id: str, role: Role) -> AbstractAsyncContextManager[Path | None]`: the blob's temp file, else the NFS file itself (never copied), else None. A sync reader runs as `async with outputs.local_role(id, role) as path: await asyncio.to_thread(reader, path)` (Ruling R6); there is no blocking form.
  - `role_size(output_id: str, role: Role) -> int | None` (index size, else `stat`).
  - `thumbnail_version(output_id: str) -> str | None` (the blob's `sha256[:16]`, else today's `_thumbnail_version` of the file).
  - `has_role(output_id: str, role: Role) -> bool`.
  - `files(output_id)` from `output_files` when the row has `bytes_at`, else the folder; `async open_file(output_id: str, name: str) -> OutputStream` (raises `OutputNotFoundError` as `file_path` does; name checked against `FILE_NAME_PATTERN` first).
  - `async model_3mf(output_id)` reads the `model` blob first (`send.py`, `api/internal.py`, `meshmodel/operations.py` follow unchanged).

- [ ] **Step 1:** Write tests (row-backed store plus `local_content`): `test_open_role_reads_blob_not_file` (remove the file after the put), `test_open_role_falls_back_to_file_without_key`, `test_open_role_falls_back_when_blob_missing`, `test_local_role_gives_nfs_path_without_blob`, `test_files_from_output_files_rows`, `test_open_file_refuses_path_names`, `test_thumbnail_version_is_blob_sha`, `test_model_3mf_from_blob`, `test_store_without_bytes_reads_files`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement; wire it in `main.py`.
- [ ] **Step 4:** Run them and every existing output test (`tests/test_output_*.py`, `tests/test_packing_and_outputs.py`, `tests/test_arrange_outputs.py`, `tests/api/test_recent_outputs.py`); expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): OutputStore reads output bytes from the blob store first (#7256)`.

### Task 5: A save and the thumbnail writers put blobs

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`create` split; `write_thumbnail`, `write_drawn_thumbnail` keep the file write), `backend/scadbuddy/library/output_operations.py` (`create_run`, `thumbnail_run`), `backend/scadbuddy/api/outputs.py` (`_draw_and_keep`)
- Test: `backend/tests/test_output_store_bytes.py`, `backend/tests/api/test_output_save_bytes.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces on `OutputStore`:
  - `prepare(job, *, name, public_url, inputs, index, files_dir, arranged_from, output_id, print_settings, source) -> PreparedOutput` (today's `create` body up to the meta: the folder's files, `meta.json` not yet written). `@dataclass class PreparedOutput(directory: Path, meta: OutputMeta, params, inputs, chosen, index, arranged_from)`.
  - `commit(prepared: PreparedOutput, blobs: OutputBlobs | None) -> OutputMeta` (writes `meta.json`, then `_insert_row` with `blobs` in its one transaction; the index row, cover and listener as today). `discard(prepared) -> None` (removes the folder).
  - `create(...)` keeps its signature: `commit(prepare(...), None)`.
  - `async save(job, **same keywords) -> OutputMeta`: `prepare` in a thread; with `bytes`, `put_folder`; then `commit` in a thread. A failed put or commit runs `discard` and re-raises; the puts left unheld go with the sweep.
  - `async put_thumbnail(output_id: str, png: bytes) -> None`: `write_thumbnail` (file), then `bytes.put` + `rows.set_blob`. `async put_drawn_thumbnail(output_id: str, png: bytes) -> bool`: `write_drawn_thumbnail` (file put-if-absent), then on True `bytes.put_if_absent` + `rows.set_blob`.
- `create_run` calls `outputs.save`; `StoreFullError` → `ApiError(507, "the blob store has no room for this output: …")` after the existing release of Parts (Ruling R2). `thumbnail_run` calls `put_thumbnail`. `_draw_and_keep` keeps its thread for the draw, then the route awaits `put_drawn_thumbnail`.

- [ ] **Step 1:** Write tests: `test_save_puts_bytes_and_row_in_one_transaction` (count commits with a wrapped pool: one for the row), `test_save_row_names_every_blob_and_holds_it`, `test_save_on_bambuddy_store_writes_no_blobs` (`bytes` None; the row has no keys), `test_save_put_failure_removes_folder_and_row`, `test_save_store_full_is_507_and_cleans_up` (API test through `POST /outputs`: 507, no folder, no row, no hold), `test_put_thumbnail_writes_file_then_blob`, `test_put_drawn_thumbnail_keeps_upload`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, `tests/api/test_pipeline_api.py` and the output tests of Task 4; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): a save puts the output's bytes in the blob store (#7256)`.

### Task 6: The API's byte routes read through `OutputStore`

**Files:**
- Modify: `backend/scadbuddy/api/outputs.py` (`_detail`, `download_output`, `get_output_preview`, `output_file`, `get_output_thumbnail`, `_draw_thumbnail`, `get_output_view`, `_model_3mf`, `get_output_plates`, `get_edit_target`'s provenance), `backend/scadbuddy/bambuddy/download.py`, `backend/scadbuddy/library/outputs.py` (`provenance`, `drawable_thumbnail`)
- Test: `backend/tests/api/test_output_bytes_routes.py`

**Interfaces:**
- Consumes: Task 4's `open_role`, `local_role`, `open_file`, `thumbnail_version`, `has_role`.
- Produces: `def stream_response(stream: OutputStream, *, media_type: str, headers: Mapping[str, str]) -> StreamingResponse` in `api/outputs.py` (as `api/library_print.py` `_native_download`: `content-length`, `etag` from the sha when known, `accept-ranges: none`). It returns a `StreamingResponse` subclass whose `__call__` wraps `super().__call__` in `try/finally`, with `anyio.CancelScope(shield=True)` around `await stream.aclose()`, so a client that goes away, or a response never sent, still closes the backend's read. A route that does more work after `stream()` and before it returns the response uses `async with stream:` around that work; `download_3mf(stream: OutputStream, meta: OutputMeta) -> StreamingResponse` in `bambuddy/download.py` (its only caller is this route; same filename and media type).
- Behaviour: downloads, the preview (no plate), `files/{name}` (with `UI_FILE_HEADERS`, same media types) and the thumbnail stream; `get_output_view`, `_draw_thumbnail`'s `draw_view`, `get_output_plates`' `plates_of` and `provenance`'s `read_provenance` take `local_role` paths, each sync reader as `async with outputs.local_role(...) as path: await asyncio.to_thread(reader, path)` (Ruling R6); `provenance` becomes async for it. `drawable_thumbnail` asks `has_role(id, "preview")`. Each 404 text stays as it is.
- Stream rule (Task 4 review): a byte route sets `Content-Length = stream.size`. On an error in the middle of the body (`BlobCorruptError` of a blob over `output_bytes._PREFETCH`, or a transport error) it logs at error level and re-raises, so the body never ends cleanly. A `None` from `open_role` is a 404, whatever `has_role` says.

- [ ] **Step 1:** Write tests (app with a `local_content` store; each byte route once with the file removed after the save, once with no blob): `test_download_streams_blob_with_length_and_etag`, `test_download_falls_back_to_file_when_blob_missing`, `test_preview_streams_blob`, `test_output_file_streams_blob`, `test_thumbnail_streams_blob_and_draws_from_preview_blob`, `test_view_and_plates_read_blob_through_temp_file` (the spool holds no file afterwards), `test_edit_target_provenance_from_blob`, `test_download_of_corrupt_large_blob_ends_short` (a blob over `_PREFETCH` with one byte flipped: the body is shorter than its `Content-Length`).
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them and `tests/api/test_output*.py`; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): the output routes read bytes from the blob store (#7256)`.

### Task 7: The other byte readers, and the derived caches in `output_derived`

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`geometry`, `attributed`, `write_attributed`, `plate_cover`, `plate_cover_archive`), `backend/scadbuddy/api/print_history.py` (`_output_files`, `_output_plate_names`), `backend/scadbuddy/orders/storefront/renders.py` (`output_of`, `variant_preview` returns `OutputStream`), `backend/scadbuddy/api/crud_storefront.py:501`, `backend/scadbuddy/analyzers/gather.py` (`_model_path` → a temp file on an `AsyncExitStack` that `api/analyzers.py` opens around gather and the analyzers' run), `backend/scadbuddy/print_library/filing.py` callers in `library/output_operations.py` (`create_run`'s filing, `native_file_run`: `async with outputs.local_role(id, "model")`), `backend/scadbuddy/library/assets.py` (`referenced_asset_ids`), `backend/scadbuddy/library/backfill.py:244` (slug from `outputs.name_and_slug`)
- Test: `backend/tests/test_output_derived.py`, plus one test per module in its existing test file

**Interfaces:**
- Consumes: Task 2's `derived`/`write_derived`, Task 4's helpers.
- Behaviour:
  - `geometry(id, plate)`: `rows.derived(id, "geometry", plate)` when its version is `str(ANALYSIS_VERSION)`, else the file cache as today (before the copy), else `analyze_3mf` as `async with outputs.local_role(id, "model") as path: await asyncio.to_thread(analyze_3mf, path)` (Ruling R6; `geometry` becomes async, and a caller that cannot become async yet keeps the file path), written with `write_derived` (and still the file, until #7257). `attributed`/`write_attributed`: kind `attributed`, data `{"model_version", "analysis"}`, the same miss rules.
  - `plate_cover(slug)` once copied: the id from `oldest_with_plate_cover`, `PLATE_THUMBNAIL` read as `async with outputs.local_role(id, "model") as path: await asyncio.to_thread(read, path)` (Ruling R6). `Catalogue.thumbnail` is sync: each caller that can become async takes this path; one that cannot yet keeps the NFS file (the fallback) until it is turned async. `plate_cover` gets an async form for the blob path; the sync `Catalogue.thumbnail` keeps the NFS file until it is turned async.
  - `_output_files` sizes from `role_size`; `_output_plate_names` as `async with outputs.local_role(id, "model") as path: await asyncio.to_thread(plate_names, path)` (Ruling R6).
  - `referenced_asset_ids(paths, records, outputs: OutputStore | None = None)`: with `outputs` copied, every row's jsonb is scanned in place of the folder walk, and the 3MF of each row whose `params` is `{}` is read as `async with outputs.local_role(id, "model") as path: await asyncio.to_thread(reader, path)` (Ruling R6; `sweep_assets` is sync and runs in a thread (`main.py` `_sweep_assets_logged`), so the sweep reads its records in that thread, then `_sweep_assets_logged` awaits the async `referenced_asset_ids` on the loop, then runs `state.assets.sweep` in a thread again); else today's walk. The asset sweep passes `state.outputs`.

- [ ] **Step 1:** Write tests: `test_geometry_cached_in_output_derived`, `test_geometry_version_mismatch_recomputes`, `test_attributed_round_trip_in_rows`, `test_plate_cover_from_model_blob`, `test_print_history_sizes_from_index`, `test_variant_preview_streams_blob`, `test_gather_model_3mf_temp_file_lives_through_analysis`, `test_filing_uploads_blob_bytes`, `test_referenced_asset_ids_from_rows_and_blob_3mf`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them and each touched module's existing tests (`grep -rl` the module's name under `backend/tests/`: print history, assets, analyzers' gather, filing, storefront renders, backfill); expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): every output byte reader uses the blob store; geometry caches are rows (#7256)`.

### Task 8: Delete and the collection of output blobs

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`delete`, `forget_indexed`, `_reap_orphan_holds`), `backend/scadbuddy/library/output_operations.py` (`delete_run`)
- Test: `backend/tests/test_output_blob_gc.py`

**Interfaces:**
- Consumes: Task 2's releasing deletes; `sweep_content`.
- Behaviour: `delete` keeps its order (index row, then the row with its byte holds in one transaction, then the folder). `delete_run`'s `release_parts` stays. `_reap_orphan_holds` treats an output that has a row as live (one `rows.existing(candidates)` query, when `rows` is set), so it never releases a live output's holds because of its folder.
- The derived blobs go too (Task 7 fix round): the plate cover `output-<id>-plate_cover` (`output_rows.plate_cover_key`, put on the first read of a model's fallback cover and held by the output) and the staged thumbnail keys `output-<id>-thumbnail-<sha256>` (held by nothing). The row's delete releases the cover's hold with its other byte holds (`_RELEASE_BYTES`, every key `output-<id>-`), and its `output_derived` rows go by the foreign key.

- [ ] **Step 1:** Write tests: `test_deleted_output_blobs_go_after_grace` (save, read the plate cover so its blob is kept, delete, `sweep_content` with grace 0 removes every `output-<id>-*` key and object, the `plate_cover` one included), `test_live_output_blobs_survive_sweep`, `test_model_delete_releases_byte_holds_keeps_part_holds`, `test_failed_save_blobs_are_swept`, `test_reaper_keeps_holds_of_output_with_row_and_no_meta`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them and `tests/test_output_holds*.py`, `tests/test_catalogue*.py`; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): a delete releases the output's blobs for the sweep (#7256)`.

### Task 9: The byte copy, at every start and as a housekeeping sweep

**Files:**
- Create: `backend/scadbuddy/library/output_byte_copy.py`
- Modify: `backend/scadbuddy/workflows/housekeeping.py` (`OUTPUT_BYTES_SWEEP = "housekeeping_copy_output_bytes"`, `OUTPUT_BYTES_PATCH = "housekeeping-copy-output-bytes"`, right after `OUTPUT_COPY_SWEEP` in `SWEEPS`, `OUTPUT_BYTES_TIMEOUT = timedelta(minutes=30)`), `backend/scadbuddy/main.py` (the activity, with its own stop as `copy_outputs_to_rows`; the start task runs it after `_copy_outputs_logged`), `backend/scadbuddy/store/migrate.py` (Ruling R5)
- Create: `backend/tests/fixtures/housekeeping_histories/output_bytes.json`
- Test: `backend/tests/test_output_byte_copy.py`, the housekeeping replay test, `backend/tests/store/test_migrate.py`

**Interfaces:**
- Consumes: Tasks 2–3; `OutputStore`, `ContentStore.usage`.
- Produces: `async def copy_output_bytes(store: OutputStore, rows: OutputRows, content: ContentStore | None, pool: ConnectionPool[Connection[DictRow]], stop: threading.Event | None = None) -> ByteCopyResult | None` (None when another replica holds the lock `"scadbuddy:outputs:bytes"`); `@dataclass ByteCopyResult(copied: int, verified: int, skipped: int, failed: list[dict], damaged: list[dict], refused: str | None, incomplete: bool)`; `def bytes_stamp(directory: Path) -> str` (sha256 of the sorted name, size and `mtime_ns` of `model.3mf`, `preview.glb`, `thumbnail.png` and each `files/` entry, a missing one as absent).

Behaviour (spec §4.5 phase 2, §6.3, §6.4, §6.5):
- `content` None → a run ended `refused="the local store has no blob store; output bytes stay on the share"`; `content.name == "bambuddy"` → `refused="outputs do not go into Bambuddy's library"`. No upload.
- Walk `rows.byte_states()`; for each, `bytes_stamp` of its folder; skip when `bytes_at` is set and the stamp equals the row's (`skipped`). A stat that fails with anything but a missing file sets `incomplete`.
- Quota first: over the outputs with `bytes_at` NULL, the sum of their byte files' sizes and their count. If `usage.bytes + needed > max_total_bytes` or `usage.count + count > max_count` (a cap of 0 is no cap), end the run `refused` with a text that names both numbers, the cap's setting name and the shortfall, and upload nothing.
- Per output, per role file there: read the index sha of its key first, then hash the file; equal → no upload; else `OutputBytes.replace(key, file, expected=<that sha or None>)`; None (a live writer moved the key) → keep the live blob. Each uploaded blob is checked with `OutputBytes.verify`; a False is a `failed` entry `{output_id, role, error}` and the output's `bytes_at` stays NULL. A missing `model.3mf` or `preview.glb` is a `damaged` entry and goes in the row's `damaged`. The copy lists `files/` as `put_folder` does (`output_bytes.put_names`), so a `files/` entry that is not a regular file or has a name an output may not hold raises `OutputFileNameError`, which the copy catches: the output is a `damaged` entry with the names (`{output_id, role: "files", error, names}`), goes in the row's `damaged`, and is never copied. Any other read error is `failed`.
- A thumbnail key is the exception to the plain compare-and-swap. The copy writes a thumbnail key only in a transaction that holds the output's advisory thumbnail lock (`output_rows.lock_thumbnail`), the one `put_thumbnail`'s step takes on every replica, and only when the row's thumbnail key is still the one the copy read; it never writes over a newer put.
- A row whose `bytes_at` is NULL can already name a thumbnail key that `put_thumbnail` wrote. The copy keeps that key or replaces it with a newer one; it never drops it.
- `rows.write_bytes` per batch of `BATCH_BYTES = 50` outputs; an id it does not return was deleted meanwhile: its blobs stay unheld for the sweep.
- `stop` is checked between outputs: the batch so far is written, the run ends unfinished. The run has `finished_at` only when nothing was refused, failed or incomplete.
- `store/migrate.py`: a migration whose target is `bambuddy` is refused while `store_blobs` holds a row of kind `output`.

- [ ] **Step 1:** Write tests (fixture tree of outputs: one plain, one arranged, one with `files/`, one with a backfill, one with no `preview.glb`): `test_byte_copy_uploads_and_verifies_every_role`, `test_byte_copy_second_run_changes_nothing`, `test_byte_copy_corrupt_upload_fails_verification`, `test_byte_copy_marks_missing_preview_damaged`, `test_byte_copy_marks_bad_file_name_damaged`, `test_byte_copy_takes_newer_thumbnail_file`, `test_byte_copy_does_not_undo_live_thumbnail` (a live `put_thumbnail` between the copy's index read and its replace wins), `test_byte_copy_leaves_no_hold_for_deleted_output`, `test_byte_copy_refuses_past_cap`, `test_byte_copy_refuses_on_bambuddy_and_local`, `test_byte_copy_runs_once_under_lock`, `test_byte_copy_stops_unfinished_on_stop`; the replay test with `output_bytes.json`; `test_migrate_to_bambuddy_refused_with_output_blobs`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement; record the replay history as the phase 1 sweep's was recorded.
- [ ] **Step 4:** Run them, `tests/test_output_copy.py` and the housekeeping tests; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): copy the existing outputs' bytes to the blob store (#7256)`.

### Task 10: Copy status for the bytes, and docs

**Files:**
- Modify: `backend/scadbuddy/api/outputs.py` (`OutputCopyStatus` gains `bytes: OutputBytesStatus | None = None`)
- Modify: `docs/architecture/data.md`, `backend/CLAUDE.md` (the outputs and housekeeping paragraphs), the spec (§4.2 and §4.5: what phase 2 built, as phase 1 did), and every page `grep -rln "thumbnail.png\|model.3mf\|outputs/<slug>" docs/architecture docs/features` finds that this phase made false
- Test: `backend/tests/api/test_output_copy_status.py`

**Interfaces:**
- Consumes: `OutputRows.last_byte_run`, `pending_bytes`.
- Produces: `OutputBytesStatus` (`started_at`, `finished_at: datetime | None`, `incomplete: bool`, `refused: str | None`, `copied`, `verified`, `skipped`, `needed_bytes`, `needed_count`, `failed: list[dict]`, `damaged: list[dict]`, `pending: int` (rows with `bytes_at` NULL now)). Read-only; the same auth as today.

- [ ] **Step 1:** Write `test_copy_status_reports_byte_run_and_pending`, `test_copy_status_bytes_null_before_any_byte_run`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement the field; update the docs: bytes are blobs of kind `output` held by `(output, <id>)`, the NFS files stay as the fallback until #7257, the byte sweep and its refusals, the roll-out check below.
- [ ] **Step 4:** Run them; run the full backend lint, type check and the output tests; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): the byte copy's status; docs (#7256)`.

### After the tasks

- Open one PR, `feat(outputs): output bytes in the blob store, phase 2 (#7254)`, body `Fixes #7256`, with this plan in it.
- After it deploys: confirm the API pods run the new image, then read `GET /api/v1/outputs/copy-status`. Its `bytes` run must have started after the last old pod went, have a `finished_at`, no `refused`, no `failed`, and `pending` 0; the owner reviews `damaged_rows` and `damaged_sample` (the rows' damage from every run), not the run's `damaged`, which names only what that run found. A `refused` that names the quota is raised as a settings change, then the next sweep runs. Phase 3 (#7257) starts only from that state.
