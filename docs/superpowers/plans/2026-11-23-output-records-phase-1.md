# Output records in Postgres (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every output's record is a row in `outputs`, and a lookup by id is one indexed read, so no hot path globs `data/outputs/*/<id>/` on NFS (#7255, epic #7254).

**Architecture:** A new synchronous row store `OutputRows` (psycopg pool, like `OutputIndex`) holds the record; `OutputStore` reads it and writes it next to the files it still writes (bytes move in phase 2, #7256). A reconciling copy fills and refreshes rows from the folders at every API start, under an advisory lock, and records each finished run; once a run has finished, a missing row is a missing output and nothing globs.

**Tech Stack:** Python 3.12, psycopg 3 + psycopg_pool, pydantic, FastAPI, pytest with the repo's Postgres fixtures.

**Spec:** `docs/superpowers/specs/2026-11-23-outputs-in-postgres-design.md` (§4.2, §4.5 phase 1, §6.1, §6.2, §6.3, §6.5, §6.6).

## Global Constraints

- Only the API process holds SQL; everything here runs in the API (root `CLAUDE.md`, "Database access").
- Migrations: one NEW file, `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`, slug `[a-z0-9_]`; never edit a merged one (root `CLAUDE.md`, "Migrations").
- One transaction per save; never one per file (prod commit latency 1–2 s).
- No change to any output id, URL or API response shape.
- `output_index` and `OutputIndex` stay as they are in this phase (dropped in #7257).
- Bytes (`model.3mf`, `preview.glb`, `thumbnail.png`, `files/`) and the geometry caches stay files in this phase; `output_files` and `output_derived` are created in #7256 with the code that uses them (spec §4.2 lists them; deferring their creation to the phase that writes them is this plan's call).
- Docs and comments in Simplified Technical English; no counts in docs.
- Run backend commands as `backend/CLAUDE.md` says (uv, the repo's pytest selection, its own database per parallel run).

## Review Focus

1. A listing that names an output deleted long ago, after a finished copy run: one SQL read, `OutputNotFoundError`, no glob (`test_missing_row_after_copy_does_not_glob`, Task 3).
2. An old pod renames an output during the rollout, after a new pod's copy ran: the next copy run takes the newer `meta.json` (`test_copy_refreshes_row_from_newer_meta`, Task 4).
3. A save whose row insert fails: the save raises and its folder is removed (`test_create_fails_and_cleans_up_when_row_insert_fails`, Task 3).
4. A folder with no readable `meta.json`: no row, listed in the run's `not_copied`, the run still finishes (`test_copy_lists_unreadable_folder`, Task 4).
5. Two replicas start at once: only one copy runs; the other skips (`test_copy_runs_once_under_lock`, Task 4).

---

### Task 1: The `outputs` and `output_copy_runs` tables

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_outputs_records.sql`
- Test: `backend/tests/test_output_rows.py` (schema smoke test only here)

**Interfaces:**
- Produces: table `outputs` (`output_id text PRIMARY KEY`, `slug text NOT NULL`, `meta jsonb NOT NULL` (the `OutputMeta` JSON, without the last-print overlay), `params jsonb NOT NULL DEFAULT '{}'`, `inputs jsonb` (NULL = a record from before inputs), `record jsonb`, `manifest jsonb`, `bom jsonb`, `arranged_from jsonb`, `backfill jsonb`, `has_plate_cover boolean NOT NULL DEFAULT false`, `created_at timestamptz NOT NULL`, `copied_at timestamptz NOT NULL DEFAULT clock_timestamp()`, `files_stamp text` (the copy's stamp of the record files it read; NULL for a row a save wrote)); indexes `(slug, created_at)` and a partial index on `output_id WHERE backfill IS NOT NULL`. Table `output_copy_runs` (`id bigserial PRIMARY KEY`, `started_at timestamptz NOT NULL DEFAULT clock_timestamp()`, `finished_at timestamptz`, `copied int NOT NULL DEFAULT 0`, `refreshed int NOT NULL DEFAULT 0`, `dropped int NOT NULL DEFAULT 0`, `not_copied jsonb NOT NULL DEFAULT '[]'`, `damaged jsonb NOT NULL DEFAULT '[]'`).
- The file opens with a comment that cites spec §4.2 and #7255.

- [ ] **Step 1:** Write `test_outputs_tables_exist` (insert one minimal `outputs` row and one `output_copy_runs` row through the migrated test database; read them back).
- [ ] **Step 2:** Run it; expect FAIL (relation does not exist).
- [ ] **Step 3:** Write the migration.
- [ ] **Step 4:** Run it; expect PASS. Run the migration-ledger tests `backend/CLAUDE.md` names.
- [ ] **Step 5:** Commit `feat(outputs): the outputs and output_copy_runs tables (#7255)`.

### Task 2: `OutputRows`, the row store

**Files:**
- Create: `backend/scadbuddy/library/output_rows.py`
- Test: `backend/tests/test_output_rows.py`

**Interfaces:**
- Consumes: Task 1's tables; `OutputMeta`, `BackfillState`, `OutputRecord`, `ManifestObject`, `BomEntry` from `scadbuddy.library.outputs` (import under `TYPE_CHECKING` where a cycle appears, as `output_index.py` does).
- Produces:
  - `@dataclass(frozen=True) class OutputRow`: `output_id: str`, `slug: str`, `meta: OutputMeta`, `params: dict[str, Any]`, `inputs: dict[str, Any] | None`, `record: dict[str, Any] | None`, `manifest: list[Any] | None`, `bom: list[Any] | None`, `arranged_from: list[str] | None`, `backfill: dict[str, Any] | None`, `has_plate_cover: bool`, `copied_at: datetime`, `files_stamp: str | None = None`.
  - `class OutputRows(pool: ConnectionPool[Connection[DictRow]])` with:
    - `get(output_id: str) -> OutputRow | None`
    - `slug_of(output_id: str) -> str | None`
    - `existing(output_ids: Iterable[str]) -> set[str]` (one `= ANY(%s)` query)
    - `for_slug(slug: str) -> list[OutputRow]` (newest first)
    - `insert(row: OutputRow, conn: Connection[DictRow] | None = None) -> None` (inside the given connection's transaction when one is passed; it first deletes a row the copy wrote of the same id and slug, and raises on any other conflict)
    - `drop_copied(output_id: str, slug: str) -> None` (a failed save's clean-up: deletes only a row with the copy's stamp)
    - As built (Task 4 fix), the copy's writes, in place of an `upsert`: `clock() -> datetime` (the mark), `write_copy(new, changed, mark) -> tuple[set[str], set[str]]` (`INSERT … ON CONFLICT DO NOTHING`, and `UPDATE … WHERE copied_at < mark`, in one transaction), `undo_copy(output_ids, mark) -> set[str]` (`DELETE … WHERE copied_at = mark`), `drop_unseen(output_ids, started) -> int` (`DELETE … WHERE copied_at < started`) and `stamps() -> dict[str, str | None]`. The live writers set `copied_at = clock_timestamp()`.
    - `update_meta(output_id: str, meta: OutputMeta) -> None`
    - `set_backfill(output_id: str, backfill: dict[str, Any] | None) -> None`
    - `set_record(output_id: str, record: dict[str, Any] | None, manifest: list[Any] | None) -> None`
    - `delete(output_id: str) -> None`, `delete_slug(slug: str) -> None`
    - `pending_backfills() -> list[tuple[str, dict[str, Any]]]`
    - `oldest_with_plate_cover(slug: str) -> str | None`
    - `ids_copied_before(before: datetime) -> set[str]` (the copy's drop pass: rows not refreshed by the running copy)
    - `copied() -> bool`: whether any `output_copy_runs` row has `finished_at`; once true it is cached true for the life of the object (it never goes back).
    - `start_run() -> int`, `finish_run(run_id: int, *, copied: int, refreshed: int, dropped: int, not_copied: list[str], damaged: list[dict[str, Any]]) -> None`, `last_run() -> dict[str, Any] | None`.

- [ ] **Step 1:** Write tests: `test_insert_then_get_round_trips_meta_and_jsons`, `test_existing_returns_only_known_ids`, `test_for_slug_is_newest_first`, `test_insert_conflict_raises`, `test_update_meta_changes_name`, `test_set_backfill_and_pending_backfills`, `test_delete_slug_drops_every_row_of_slug`, `test_oldest_with_plate_cover_picks_oldest_flagged`, `test_copied_false_until_a_run_finishes_then_stays_true`.
- [ ] **Step 2:** Run them; expect FAIL (module missing).
- [ ] **Step 3:** Implement `output_rows.py` (synchronous; callers on the loop use a thread, as `OutputIndex`; module docstring cites spec §4.2 and says the files still hold the bytes until #7256).
- [ ] **Step 4:** Run them; expect PASS; run ruff and the type checker `backend/CLAUDE.md` names on the file.
- [ ] **Step 5:** Commit `feat(outputs): OutputRows, the output record store (#7255)`.

### Task 3: `OutputStore` reads and writes rows

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`OutputStore.__init__`, `_find_dir`, `get`, `name_and_slug`, `params`, `inputs`, `list_for`, `create`, `bom`, `manifest`, `arranged_from`, `backfill`, `start_backfill`, `fail_backfill`, `clear_backfill`, `pending_backfills`, `attach_backfill`, `record`, `rename`, `mark_storefront`, `delete`, `forget_indexed`, `plate_cover_output`, `provenance`)
- Modify: `backend/scadbuddy/api/deps.py:360` (wire `outputs.rows = OutputRows(pool)` beside `outputs.index`)
- Modify: `backend/scadbuddy/api/outputs.py:471` (`_existing` → `outputs.existing(ids)`)
- Test: `backend/tests/test_output_store_rows.py`

**Interfaces:**
- Consumes: Task 2's `OutputRows`, `OutputRow`.
- Produces: `OutputStore.rows: OutputRows | None` (None keeps today's file behaviour, for tests and tools without a database); `OutputStore.existing(output_ids: Iterable[str]) -> set[str]`.

Behaviour (spec §4.5 phase 1, §6.1, §6.6):
- `_find_dir(id)`: with `rows`, the folder is `paths.output_dir(rows.slug_of(id), id)` — no glob. When `slug_of` is None: if `rows.copied()`, raise `OutputNotFoundError`; else fall back to today's glob.
- Reads (`get`, `name_and_slug`, `params`, `inputs`, `list_for`, `bom`, `manifest`, `arranged_from`, `backfill`, `record`, `pending_backfills`) come from the row when one exists, else (before a finished copy) from the files as today. `get` and `list_for` keep the last-print overlay (`_with_last_prints`). `inputs` keeps `legacy_inputs` for a NULL `inputs`.
- `create`: after writing the files, insert the row (with `has_plate_cover` = whether the stamped 3MF holds `Metadata/plate_1.png`; reuse what `_scan_plate_cover` checks) in one transaction. If the insert raises, remove the folder exactly as `create` already does on a failure part way, and re-raise.
- `rename`, `mark_storefront`: write the file as today, then `rows.update_meta`. Backfill methods and `attach_backfill`: write files as today, then `rows.set_backfill` / `rows.set_record`.
- `delete(id)`: `rows.delete`; `forget_indexed(slug)`: also `rows.delete_slug` (the model delete path, spec §6.2).
- `plate_cover_output(slug)`: `rows.oldest_with_plate_cover(slug)` once `rows.copied()`, else today's scan. `plate_cover_archive`/`plate_cover` build the path from that id and the slug.
- `provenance(id)`: with a row, read the 3MF at the row's folder; no glob.
- `existing(ids)`: `rows.existing(ids)` when `rows.copied()`, else today's per-id `directory()` loop.
- Remove `_dirs` and `_gone` only where every use is gone; leave `_covers` (phase 3).

- [ ] **Step 1:** Write tests (row-backed store on the Postgres fixture, like `tests/test_output_index.py`'s `store` fixture): `test_create_writes_row_with_meta_params_inputs`, `test_get_reads_row_not_meta_file` (delete `meta.json` after create; `get` still answers), `test_missing_row_after_copy_does_not_glob` (finish a run; monkeypatch `Path.glob` to raise; `get("unknown")` raises `OutputNotFoundError`), `test_missing_row_before_copy_falls_back_to_files`, `test_create_fails_and_cleans_up_when_row_insert_fails` (pre-insert a row with the same id; `create` raises; the folder is gone), `test_rename_updates_row`, `test_backfill_round_trip_through_rows`, `test_delete_and_forget_indexed_drop_rows`, `test_existing_is_one_query_after_copy`, `test_plate_cover_output_from_rows`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement the behaviour above.
- [ ] **Step 4:** Run the new tests and every existing output test: `tests/test_output_*.py`, `tests/test_packing_and_outputs.py`, `tests/test_arrange_outputs.py`, `tests/api/test_recent_outputs.py`, `tests/api/test_pipeline_api.py`, and any test `grep -rl "OutputStore\|outputs\." tests` adds; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): OutputStore reads and writes output rows (#7255)`.

### Task 4: The reconciling copy at every API start

**Files:**
- Create: `backend/scadbuddy/library/output_copy.py`
- Modify: `backend/scadbuddy/main.py` (start it next to `_index_outputs_logged`, line ~2376, as a background task that honours `stop`)
- Test: `backend/tests/test_output_copy.py`

**Interfaces:**
- Consumes: Task 2's `OutputRows`; `OutputStore` file readers (the pre-row `get`/`params`/`inputs`/... paths, called with rows bypassed; add a private `_from_files(output_id, directory) -> OutputRow` on `OutputStore` if needed).
- Produces: `copy_outputs(store: OutputStore, rows: OutputRows, pool: ConnectionPool[Connection[DictRow]], stop: threading.Event | None = None) -> CopyResult | None` (None when another replica holds the lock); `@dataclass CopyResult(copied: int, refreshed: int, dropped: int, not_copied: list[str], damaged: list[dict[str, Any]])`.

Behaviour (spec §6.1, §6.3, §6.5):
- Take `pg_try_advisory_lock(hashtextextended('scadbuddy:outputs:copy', 0))` on a dedicated connection; if not got, log and return None.
- `start_run()`; walk `paths.outputs/<slug>/<id>/` (the one place that still scans). For each folder: no readable `meta.json` → `not_copied` (the folder's `<slug>/<id>`); a row missing → `write_copy` insert (`copied`); a row whose `files_stamp` differs from the folder's (a sha256 of the sorted name, size and `mtime_ns` of the record files `meta.json`, `params.json`, `inputs.json`, `record.json`, `manifest.json`, `bom.json`, `arranged_from.json`, `backfill.json`) → `write_copy` refresh (`refreshed`); both under the batch's mark, with `undo_copy` after each batch and once more for the inserted rows at the run's end; an unreadable side file → row written, entry in `damaged` naming the file. Rows in `ids_copied_before(run start)` whose folder the walk did not see → `drop_unseen` (`dropped`), not when a listing or a stat failed. `finish_run(...)`. Honour `stop` between folders (an unfinished run has no `finished_at`, so `copied()` stays false).
- Log one line with the counts; failures are logged and the next start retries (as `_index_outputs_logged`).

- [ ] **Step 1:** Write tests: `test_copy_fills_missing_rows_and_finishes_run`, `test_copy_refreshes_row_from_newer_meta`, `test_copy_second_run_changes_nothing`, `test_copy_lists_unreadable_folder`, `test_copy_marks_damaged_side_file`, `test_copy_drops_rows_whose_folder_is_gone`, `test_copy_runs_once_under_lock` (hold the lock on another connection; `copy_outputs` returns None), `test_copy_stops_unfinished_on_stop`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement `output_copy.py` and the start-time task in `main.py`.
- [ ] **Step 4:** Run them and Task 3's tests; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): copy output records to rows at every start (#7255)`.

### Task 5: Status route and docs

**Files:**
- Modify: `backend/scadbuddy/api/outputs.py` (add `GET /outputs/copy-status` → the last run: started, finished, counts, `not_copied`, `damaged`)
- Modify: `docs/architecture/data.md` (the `outputs` and `output_copy_runs` tables; outputs' record is a row, bytes still on the share until #7256)
- Modify: `backend/CLAUDE.md` only if it describes `OutputStore`'s storage
- Test: `backend/tests/api/test_output_copy_status.py`

**Interfaces:**
- Consumes: `OutputRows.last_run()`.
- Produces: `GET /api/v1/outputs/copy-status` returning `OutputCopyStatus` (`started_at`, `finished_at: datetime | None`, `copied`, `refreshed`, `dropped`, `not_copied: list[str]`, `damaged: list[dict]`), 404 when no run exists. Read-only; same auth dependencies as the other `GET /outputs` routes.

- [ ] **Step 1:** Write `test_copy_status_reports_last_run` and `test_copy_status_404_before_any_run`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement the route and update the docs.
- [ ] **Step 4:** Run them; expect PASS; run the full backend lint and type check.
- [ ] **Step 5:** Commit `feat(outputs): the output copy's status route; docs (#7255)`.

### After the tasks

- Open one PR for the branch, `feat(outputs): output records in Postgres, phase 1 (#7254)`, body `Fixes #7255` and the session marker; the shepherd lands it.
- After it deploys: confirm the API pods run the new image (memory: confirm rollout by pod images), read `GET /api/v1/outputs/copy-status`, and time `GET /api/v1/print/library` again against the numbers in the spec §1.
