# Outputs cut over from NFS (phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After the copy has every output's record in a row and every output's bytes in the blob store, ScadBuddy stops reading and writing `data/outputs/` (#7257, epic #7254). The folder stays on the share, unread. The owner deletes it later.

**Architecture:** Two releases. Release A (phase 3a) adds what release B needs and keeps every phase 2 path. It adds the cut-over mark (`output_cutover`), which the API writes once the copies are done. It moves the outputs list and the print links from `output_index` to `outputs`. It moves the id claim to a table. It makes each write into an existing output's folder tolerate a missing folder once the mark is written. Release B (phase 3b) removes the file fallback, the file writes, `meta.json`, the copies, the directory scans, `OutputIndex` and `output_index`. Its migration refuses to run while the mark is missing, so release B cannot reach a database whose copy is not done.

**Tech Stack:** Python 3.12, psycopg 3 + psycopg_pool, FastAPI, Temporal (housekeeping sweeps, `workflow.patched`), pytest with the repo's Postgres fixtures and `tests/support/store.py` `local_content`.

**Spec:** `docs/superpowers/specs/2026-11-23-outputs-in-postgres-design.md` (§4.5 phase 3, §4.6, §5, §6.3, §6.4). Phase 2 plan: `docs/superpowers/plans/2026-11-24-output-bytes-phase-2.md`; its rulings are the last comment of #7256.

## Global Constraints

- Only the API process holds SQL (root `CLAUDE.md`, "Database access"). No worker gains a database client. The render worker's allowlist (`api/crud_render.py` `READ_KINDS`) never admits the `output` kind.
- Bytes go to `ContentStore`, never `bytea`. Images move as blobs and `{name, mediaType}` pointers, never base64.
- Migrations: one NEW file per release, `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`. Never edit a merged file. A migration may depend only on files already on main.
- One transaction per save, never one per file (prod commit latency 1–2 s, known).
- No change to an output id, a URL or an API response shape. A response may only gain an optional field.
- A change to `Housekeeping.run`'s commands goes behind `workflow.patched`, with a replay history in `tests/fixtures/housekeeping_histories/`. Never re-record a merged history.
- A periodic pass is an activity in `workflows/housekeeping.py` `SWEEPS`, never a loop in the API.
- Phase 1 and phase 2 rulings carry over: DB-clock marks, guarded writes, rows before files on delete, sync readers as `async with … as path: await asyncio.to_thread(reader, path)`.
- After release B, no code reads, writes, lists or deletes anything under `data/outputs/`.
- Docs and comments in Simplified Technical English. No counts in docs. A doc that a diff makes false changes in the same diff.
- Run backend commands as `backend/CLAUDE.md` says (uv; on this machine a Postgres container of its own per parallel run, `CLAUDE.local.md`).

## Decisions (the plan's calls; the owner may change them)

- **D1. Two releases, not one.** During a roll, old pods run beside new ones (prod: `RollingUpdate`, `maxSurge` 1, `maxUnavailable` 0, 3 replicas; read with `kubectl -n bambuddy get deploy scadbuddy -o jsonpath='{.spec.strategy}'` on 2026-11-24). A phase 2 pod writes into an output's folder on a rename (`_replace(directory / META_NAME)`), a thumbnail upload (the `write` step in `OutputRows.keep_thumbnail`) and an attributed analysis (`write_attributed`). Each of these raises `FileNotFoundError` when the folder is missing. If one release stopped writing folders, an old pod would fail those operations for every output a new pod saved, until the roll ended. So release A still writes every folder and makes its own folder writes tolerate a missing folder once the mark is written. Release B stops writing folders; during its roll the old pods are release A pods, which tolerate it. The same reason holds for `output_index` and `output_hold_slugs`: release A stops reading them and makes its writes to them tolerate a dropped table, then release B drops them.
- **D2. The gate is a mark in the database, and release B's migration enforces it.** Release A writes one row of `output_cutover` when all of these hold in one statement: a row copy run has a `finished_at`; the last byte run has a `finished_at`, no `refused` and an empty `failed`; no `outputs` row has `bytes_at` NULL. Release A tries at each start and after each byte run. Release B's migration raises (and so stops the API's start, and with it the roll) unless the mark exists or the database never held an output (`outputs` and `output_index` both empty: a new install or a test schema). It raises too while any row has `bytes_at` NULL. On `store_backend=bambuddy` the mark is never written (D7), so release B refuses every start on such an install that holds an output. That is the decision, not a gap: outputs never go into Bambuddy's library (spec §6.4), so release B has no store to read them from there. The migration's text names the cause and the remedy: move the store to `s3` or `local`, let release A run the copies until `cut_over_at` is set, then upgrade. With `maxUnavailable` 0 a refused start leaves the release A pods serving. Reason: a runbook check can be skipped; a migration cannot. Release B keeps no fallback code, so the gate must hold before its code runs, not inside it.
- **D3. Release A keeps the phase 2 reads.** Release A reads as phase 2 does (the blob first, the file when the row names no blob or the blob is lost). Release B removes the file reads. Before the mark, release A behaves as phase 2 except for the new writes (D1) and the list (D4).
- **D4. The copies stop at the mark.** In release A, `copy_outputs` and `copy_output_bytes` do nothing once the mark exists (start and sweep). After the mark the files are no longer the record: a row copy run would drop a row whose folder a tolerant write did not make (`drop_unseen`). Release B retires both sweeps behind a new patch and keeps their activity names registered as no-ops for one release (follow-up issue to remove them with `workflow.deprecate_patch`).
- **D5. `outputs` takes `output_index`'s columns.** `name` and `source` are stored generated columns of `meta` (a phase 2 pod that writes `meta` keeps them right during the roll). `params_text` is a plain column that release A writes on every save and on every `OutputIndex.record`. Release A's migration fills it from `output_index`; release B's migration fills the rows still NULL (outputs a phase 2 pod saved during release A's roll), then sets the rest to `''`.
- **D6. The id claim moves to a table.** `OutputStore.claim` (a quote's attempts, #5390) is a `mkdir` on the share today. Release A adds `output_claims` and claims there first, then makes the folder. Release B claims in the table only. Both releases' pods then share one claim during release B's roll. The hold reaper deletes claims older than `ORPHAN_HOLD_GRACE`.
- **D7. `local` gets an output store of its own (phase 2 Ruling R1).** On `store_backend=local`, release A builds a `ContentStore` over `LocalContentBackend(paths.output_blobs)` (`data/output-blobs/`) for outputs only, with the shared index. So dev and the tests save, copy and cut over as prod does. On `bambuddy` the byte copy stays refused (spec §6.4), the mark is never written, and release B's migration refuses with a text that names the cause and the remedy (D2: move the store to `s3` or `local`, run the copies, then upgrade). Prod runs `s3`.
- **D8. A save in release B stages on the pod's own disk.** `prepare` writes into a temporary folder under `OutputBytes.spool`, a new setting `SCADBUDDY_OUTPUT_SPOOL_DIR` (default `<tempfile.gettempdir()>/scadbuddy-outputs`), never the data volume. Phase 2's `local_file` temporary files move there too: today they go to `ContentStore.spool`, which is `data/cache/blob-spool` on the NFS share (`store/factory.py`), against spec §4.3. Prod's API container has `readOnlyRootFilesystem: false` and no volume at `/tmp` (read on 2026-11-24), so the default is the node's disk.
- **D9. A lost blob after release B is a 500, not a 404.** With no file to fall back to, `open_role`, `read_role`, `open_file` and `model_3mf` raise `OutputBytesLostError(output_id, role, key)`, logged at error level. The routes answer `ApiError(500, "the blob store has lost this output's <role>; the old outputs share may still hold a copy")`. A 404 would say the output does not exist, which is false. The asset sweep treats it as an unread reference and removes nothing.
- **D10. Release B touches the share in no way.** It does not delete a deleted output's or model's folder either: the folder is the owner's backup. `DataPaths.outputs` and `DataPaths.output_dir` go, so `mypy` finds any caller that is left.
- **D11. Columns and tables that only the copies wrote stay for now.** `outputs.copied_at`, `files_stamp`, `bytes_stamp`, `output_copy_runs` and `output_byte_runs` stay in release B: release A pods still write `copied_at` during its roll, and `GET /outputs/copy-status` keeps answering the last runs. A follow-up issue drops them after release B is deployed.

## Review Focus

1. Release B deployed before the mark exists: its migration raises, the new pod does not start, and the release A pods keep serving (`test_drop_output_index_refuses_without_cut_over`, Task B1).
2. A release A pod renames, uploads a thumbnail for, or analyses an output that a release B pod saved (no folder): the operation succeeds through the row and the blob (`test_rename_without_folder_after_cut_over`, `test_put_thumbnail_without_folder_after_cut_over`, `test_write_attributed_without_folder_after_cut_over`, Task A5).
3. A blob lost after release B: the download answers 500 with the text of D9 and reads no file (`test_download_of_lost_blob_is_500`, Task B3).
4. Two overlapping quote attempts on two pods name one output id: one claim wins; a failed save frees its claim; a claim of an id that has a row fails (`test_claim_refused_while_claimed_or_saved`, Task A4).
5. Something still touches the share: with `data/outputs` replaced by a plain file, so any access raises, a save, list, download, rename, thumbnail upload, delete, model delete, hold reap and asset sweep all pass (`test_no_output_path_touches_the_share`, Task B5).

---

## Part A: release A (phase 3a), one PR

PR title `feat(outputs): ready the cut-over from NFS, phase 3a (#7257)`, body `Part of #7257` (not `Fixes`).

### Task A1: Schema A and the rows' new reads and writes

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_outputs_cut_over.sql`
- Create: `backend/scadbuddy/library/output_query.py` (moved out of `output_index.py`: `DEFAULT_LIMIT`, `AfterNotListedError`, `OutputQuery`, `params_text`, `saved_by_storefront_run`; `output_index.py` imports them from here)
- Modify: `backend/scadbuddy/library/output_rows.py`, `backend/scadbuddy/library/outputs.py` (`_insert_row` sets `params_text`), `backend/scadbuddy/library/output_index.py` (`record` also sets `outputs.params_text` for the id, in its statement's transaction)
- Test: `backend/tests/test_output_rows.py`, `backend/tests/test_output_index.py`

**Interfaces:**
- Produces, in the migration (it opens with a comment that cites spec §4.2, §4.5 and #7257):
  - `ALTER TABLE outputs ADD COLUMN name text GENERATED ALWAYS AS (meta->>'name') STORED, ADD COLUMN source text GENERATED ALWAYS AS (COALESCE(meta->>'source', 'back_office')) STORED, ADD COLUMN params_text text`.
  - `UPDATE outputs o SET params_text = x.params_text FROM output_index x WHERE x.output_id = o.output_id`.
  - `CREATE INDEX outputs_created ON outputs (created_at DESC, output_id DESC)`.
  - `CREATE TABLE output_cutover (only boolean PRIMARY KEY DEFAULT true CHECK (only), at timestamptz NOT NULL DEFAULT clock_timestamp(), byte_run_id bigint NOT NULL REFERENCES output_byte_runs)`.
  - `CREATE TABLE output_claims (output_id text PRIMARY KEY, slug text NOT NULL, claimed_at timestamptz NOT NULL DEFAULT clock_timestamp())`.
- Produces on `OutputRows`:
  - `OutputRow.params_text: str | None = None`; `insert` writes it and deletes the id's `output_claims` row in its transaction.
  - `try_cut_over() -> bool`: one `INSERT INTO output_cutover (byte_run_id) SELECT r.id … ON CONFLICT DO NOTHING` whose `WHERE` holds the D2 conditions (the last `output_byte_runs` row by id has `finished_at` set, `refused` NULL, `failed = '[]'`; `EXISTS` a finished `output_copy_runs` row; `NOT EXISTS` an `outputs` row with `bytes_at` NULL); then answers `cut_over()`.
  - `cut_over() -> bool`: whether the mark exists; once True it stays True for the object (as `copied()`).
  - `cut_over_at() -> datetime | None`.
  - `query(query: OutputQuery) -> tuple[list[str], int]`: `OutputIndex.query` over `outputs` (`name`, `source`, `COALESCE(params_text, '')`, `created_at`, `output_id`), same filters, order and `AfterNotListedError`.
  - `claim(output_id: str, slug: str) -> bool`: `INSERT INTO output_claims … SELECT … WHERE NOT EXISTS (SELECT 1 FROM outputs WHERE output_id = %s) ON CONFLICT DO NOTHING`; True when it inserted.
  - `release_claim(output_id: str) -> None`.
  - `prune_claims(grace: timedelta) -> int`: deletes the claims older than `now() - grace`.

- [ ] **Step 1:** Write tests: `test_name_and_source_follow_meta` (insert, `update_meta` with a new name and `source` `storefront`: the columns follow), `test_insert_writes_params_text_and_drops_claim`, `test_try_cut_over_needs_every_condition` (one case per condition: no finished row run; last byte run refused; last byte run with a failed entry; a row with `bytes_at` NULL; then all hold: True, and `cut_over_at` is set), `test_try_cut_over_is_written_once`, `test_query_on_outputs_matches_index_query` (the fixture rows of `tests/test_output_index.py`'s query tests, asserted through `OutputRows.query`: same ids, order, totals, `AfterNotListedError`), `test_index_record_fills_params_text`, `test_claim_refused_while_claimed_or_saved` (Review Focus 4: a second claim is False; `release_claim` lets it claim again; a claim of an id with a row is False), `test_prune_claims_drops_only_old_claims`.
- [ ] **Step 2:** Run them (`uv run --frozen pytest tests/test_output_rows.py tests/test_output_index.py -q`); expect FAIL.
- [ ] **Step 3:** Write the migration, `output_query.py` and the `OutputRows` methods.
- [ ] **Step 4:** Run them and `tests/test_output_*.py`; expect PASS. Run ruff and mypy.
- [ ] **Step 5:** Commit `feat(outputs): the cut-over mark, output claims and the list's columns on outputs (#7257)`.

### Task A2: The gate: try the mark, stop the copies, report it

**Files:**
- Modify: `backend/scadbuddy/main.py` (`_copy_outputs_once`, `_copy_output_bytes_once`, `_copy_outputs_and_bytes_logged`), `backend/scadbuddy/library/output_copy.py` (`copy_outputs`), `backend/scadbuddy/library/output_byte_copy.py` (`copy_output_bytes`), `backend/scadbuddy/api/outputs.py` (`OutputCopyStatus`)
- Test: `backend/tests/test_output_cut_over.py`, `backend/tests/api/test_output_copy_status.py`

**Interfaces:**
- Consumes: Task A1's `try_cut_over`, `cut_over`, `cut_over_at`.
- Produces:
  - `copy_outputs(...)` and `copy_output_bytes(...)` return `None` at once, before the lock, when `rows.cut_over()` (logged at info: "the outputs are cut over (#7257); nothing to copy").
  - `_copy_output_bytes_once` calls `rows.try_cut_over()` in a thread after a byte run that ended, and logs the mark once when it is new. `_copy_outputs_and_bytes_logged` calls `try_cut_over()` first, so a start after the mark copies nothing.
  - `OutputCopyStatus.cut_over_at: datetime | None = None` (optional, additive).

- [ ] **Step 1:** Write tests: `test_byte_run_that_finished_writes_the_mark` (a fixture tree copied with `copy_outputs` and `copy_output_bytes` on `local_content`, then `_copy_output_bytes_once`: the mark exists), `test_no_mark_while_a_row_has_no_bytes` (one output whose `model.3mf` read fails: `failed`, no mark), `test_copies_do_nothing_after_the_mark` (mark set; remove one folder; run both copies: no run row is added and the row stays), `test_copy_status_reports_cut_over_at`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, `tests/test_output_copy*.py`, `tests/test_output_byte_copy*.py`; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): the copies write the cut-over mark and stop after it (#7257)`.

### Task A3: The list, the print links and `local` stop needing the files' companions

**Files:**
- Modify: `backend/scadbuddy/api/outputs.py` (`list_recent_outputs`, `_metas_or_forget`), `backend/scadbuddy/bambuddy/print_links.py` (`_QUEUE_JOBS`, `_LAN_JOBS`: `LEFT JOIN outputs o` in place of `output_index o`), `backend/scadbuddy/library/output_index.py` (`record`, `rename`, `forget`, `forget_slug`), `backend/scadbuddy/library/outputs.py` (`rename`, `delete`, `forget_indexed`, `hold_parts`'s `_record_slug`, `release_parts`, `_record_slugs`), `backend/scadbuddy/core/paths.py` (`output_blobs`), `backend/scadbuddy/main.py` (lifespan: D7)
- Test: `backend/tests/api/test_recent_outputs.py`, `backend/tests/bambuddy/test_print_links*.py` (the file that tests `_QUEUE_JOBS`), `backend/tests/test_output_cut_over.py`, `backend/tests/test_output_byte_copy.py`

**Interfaces:**
- Consumes: Task A1's `OutputRows.query`.
- Produces:
  - `list_recent_outputs` reads `state.outputs.rows.query` once `rows.copied()`, else `OutputIndex.query` as today. `_metas_or_forget` forgets index rows only on the `OutputIndex` path.
  - `def _tolerate_dropped(action: Callable[[], None], what: str) -> None` in `output_index.py`: runs `action`; on `psycopg.errors.UndefinedTable` logs at warning ("`output_index` is gone (#7257)") and returns. `record`, `rename`, `forget`, `forget_slug` use it, and so do the `output_hold_slugs` statements in `outputs.py` (a helper of the same shape there). Any other error raises as today.
  - `DataPaths.output_blobs -> Path` (`root / "output-blobs"`), made by `ensure`.
  - Lifespan: when `state.store.backend == "local"`, `state.outputs.bytes = OutputBytes(ContentStore(LocalContentBackend(paths.output_blobs), BlobIndex(pool), spool=…))`; on `s3`, as today; on `bambuddy`, none (D7). `_copy_output_bytes_once` passes `state.outputs.bytes.content` in place of `state.store.content` when it is set, so the byte copy runs on `local`.

- [ ] **Step 1:** Write tests: `test_recent_outputs_list_from_rows` (drop `output_index` in the test schema, then `GET /api/v1/outputs` with `q`, `slug`, `after` and `ordered`: same answers as before the drop), `test_print_links_name_slug_from_outputs` (a send of an output with no `output_index` row: its slug is read from `outputs`), `test_index_writes_tolerate_dropped_table` (drop `output_index` and `output_hold_slugs`; a save, a rename, a delete and a model's output forget succeed and log), `test_local_store_puts_output_bytes` (an app on `local`: a save names blob keys and sets `bytes_at`), `test_byte_copy_runs_on_local` (replaces the `local` half of `test_byte_copy_refuses_on_bambuddy_and_local`, which keeps its `bambuddy` half).
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, `tests/api/test_output*.py`, `tests/test_output_*.py`; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): the list and print links read outputs; local holds output blobs (#7257)`.

### Task A4: The id claim in a table

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`claim`), `backend/scadbuddy/library/output_operations.py` (`create_run`'s failure path: `release_claim`, then the folder as today), `backend/scadbuddy/library/outputs.py` (`_reap_orphan_holds` ends with `rows.prune_claims(grace)` when the store has rows)
- Test: `backend/tests/test_output_claims.py`, `backend/tests/orders/test_quote*.py` (the test that overlaps two attempts, #5390)

**Interfaces:**
- Consumes: Task A1's `claim`, `release_claim`, `prune_claims`.
- Produces: `OutputStore.claim(slug, output_id) -> bool` claims the row first (`rows.claim`), then makes the folder as today; a False from either is False (a folder that exists after a row claim succeeded releases the row claim). With no rows, the folder only.

- [ ] **Step 1:** Write tests: `test_claim_takes_row_then_folder`, `test_failed_save_releases_claim` (a save that fails after the claim: a second attempt claims the id), `test_reaper_prunes_stale_claims` (grace 0).
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them and the quote tests that claim ids; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): a save claims its id in output_claims (#7257)`.

### Task A5: Writes into a folder tolerate its absence after the mark; the reaper reads rows

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`rename`, `mark_storefront`, `_write_backfill`, `clear_backfill`, `attach_backfill`, `write_thumbnail`, `write_drawn_thumbnail`, `_keep_geometry`, `write_attributed`, `_reap_orphan_holds`)
- Test: `backend/tests/test_output_cut_over.py`, `backend/tests/test_output_blob_gc.py`

**Interfaces:**
- Consumes: Task A1's `cut_over`.
- Produces:
  - `OutputStore._folder_write(output_id: str, write: Callable[[Path], None]) -> None`: finds the folder (`_find_dir`) and runs `write`. When `self._cut_over()` and the folder does not exist, it logs at debug and returns. Before the mark, a missing folder raises as today. Every method above uses it for its file step. The row step runs as today.
  - `write_drawn_thumbnail` with no folder after the mark answers True (the row decides; `draw_thumbnail` writes the key only while the row names none). `keep_thumbnail`'s `write` and `draw_thumbnail`'s `write` steps pass through `_folder_write`.
  - `_reap_orphan_holds` after the mark: no directory scan. It releases every `OUTPUT_HOLDER` hold older than `grace` whose holder has no `outputs` row and no `output_claims` row younger than `grace`, in one query, then prunes the claims. Before the mark, as today.

- [ ] **Step 1:** Write tests (mark set; an output saved, then its folder removed): `test_rename_without_folder_after_cut_over`, `test_put_thumbnail_without_folder_after_cut_over`, `test_put_drawn_thumbnail_without_folder_after_cut_over`, `test_write_attributed_without_folder_after_cut_over`, `test_backfill_writes_without_folder_after_cut_over`, `test_missing_folder_raises_before_cut_over` (no mark: the rename raises `FileNotFoundError`, as phase 2), `test_reaper_after_cut_over_reads_rows_only` (outputs root removed: holds of a deleted output past the grace go; a live output's and a claimed id's stay).
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, `tests/test_output_*.py`, `tests/test_output_blob_gc.py`, `tests/api/test_output*.py`; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): after the cut-over mark a missing output folder fails no write (#7257)`.

### Task A6: Docs for release A

**Files:**
- Modify: `docs/architecture/data.md` ("Saved outputs": the mark, D1's two releases, the list on `outputs`, `output_claims`, `local`'s output store; the table list's line for the new tables), `backend/CLAUDE.md` (the saved-outputs and housekeeping paragraphs), `README.md` (the outputs line near "State lives in `/data`": `local` keeps output blobs in `data/output-blobs/`), `docs/features/preview-and-3mf.md`, `docs/features/arrange-and-plates.md`, `docs/features/versions-and-history.md` (each line that says `output_index` lists outputs), the spec §4.2 and §4.5 (as built for 3a)

- [ ] **Step 1:** Update each page. `grep -rn "output_index\|OutputIndex\|claim" docs/architecture docs/features backend/CLAUDE.md README.md` shows no line this release made false.
- [ ] **Step 2:** Run the full backend lint, format check, mypy and `pytest -n 4`; expect PASS.
- [ ] **Step 3:** Commit `docs(outputs): phase 3a as built: the cut-over mark and two releases (#7257)`.

### After release A deploys

1. Confirm the API pods run release A's image (the `kubectl … -o jsonpath` image check that `docs/architecture/data.md` gives for the copies' roll-out).
2. Read `GET /api/v1/outputs/copy-status`: `cut_over_at` is set. If it is not, read `bytes` (`refused`, `failed`, `pending`) and fix that first; a start or the next sweep tries the mark again.
3. Post the mark time and the copy-status answer on #7257. Release B starts only from that state.

---

## Part B: release B (phase 3b), one PR

PR title `feat(outputs): cut over from NFS, phase 3b (#7257)`, body `Fixes #7257`. Open it only after release A's mark is on prod.

### Task B1: Schema B: the gate, the last fill, and the drops

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_outputs_drop_output_index.sql`
- Delete: `backend/scadbuddy/library/output_index.py`, `backend/tests/test_output_index.py` (its query tests moved to `tests/test_output_rows.py` in Task A1)
- Modify: `backend/scadbuddy/api/deps.py` (no `OutputIndex`), `backend/scadbuddy/main.py` (no `_index_outputs_logged`), `backend/scadbuddy/library/outputs.py` (no `index` attribute), `backend/scadbuddy/api/outputs.py` (`list_recent_outputs` reads `rows.query` only; `_metas_or_forget` becomes `_metas`, which forgets nothing)
- Test: `backend/tests/test_output_cut_over_migration.py`

**Interfaces:**
- Produces, in the migration (comment cites D2 and #7257):
  - `DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM output_cutover) AND (EXISTS (SELECT 1 FROM outputs) OR EXISTS (SELECT 1 FROM output_index)) THEN RAISE EXCEPTION 'the outputs are not cut over (#7257): the copies have not finished, or the store is bambuddy, where outputs are never copied (spec 6.4). Move the store to s3 or local, run release 3a until GET /api/v1/outputs/copy-status names cut_over_at, then upgrade'; END IF; IF EXISTS (SELECT 1 FROM outputs WHERE bytes_at IS NULL) THEN RAISE EXCEPTION 'outputs whose bytes are only in files remain (#7257): read GET /api/v1/outputs/copy-status'; END IF; END $$;`
  - `UPDATE outputs o SET params_text = x.params_text FROM output_index x WHERE x.output_id = o.output_id AND o.params_text IS NULL;` then `UPDATE outputs SET params_text = '' WHERE params_text IS NULL;` then `ALTER TABLE outputs ALTER COLUMN params_text SET DEFAULT '', ALTER COLUMN params_text SET NOT NULL;`
  - `DROP TABLE output_index; DROP TABLE output_hold_slugs;`

- [ ] **Step 1:** Write tests (each on a schema migrated up to the file before this one, then this file applied with `render/pg_store.py` `migrate`): `test_drop_output_index_refuses_without_cut_over` (an `outputs` row and no mark, as on a `bambuddy` store: `migrate` raises with the text that names `bambuddy` and the remedy, and `output_index` still exists, since the files apply in one transaction), `test_drop_output_index_refuses_with_bytes_pending` (mark set, one row with `bytes_at` NULL), `test_drop_output_index_on_new_database` (both tables empty: applies), `test_drop_output_index_fills_params_text`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Write the migration; remove `OutputIndex` and its callers.
- [ ] **Step 4:** Run them, `tests/api/test_recent_outputs.py`, and the migration-ledger tests `backend/CLAUDE.md` names; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): drop output_index behind the cut-over mark (#7257)`.

### Task B2: Retire the copy sweeps

**Files:**
- Modify: `backend/scadbuddy/workflows/housekeeping.py`, `backend/scadbuddy/main.py` (`copy_outputs_to_rows`, `copy_output_bytes_to_blobs` become no-ops; `_copy_outputs_once`, `_copy_output_bytes_once`, `_copy_outputs_and_bytes_logged` and the start task go)
- Delete: `backend/scadbuddy/library/output_copy.py`, `backend/scadbuddy/library/output_byte_copy.py`, `backend/tests/test_output_copy.py`, `test_output_copy_sweep.py`, `test_output_byte_copy.py`, `test_output_byte_copy_sweep.py`
- Create: `backend/tests/fixtures/housekeeping_histories/outputs_cut_over.json`
- Test: `backend/tests/test_housekeeping_replay.py`, `backend/tests/api/test_output_copy_status.py`

**Interfaces:**
- Produces: `OUTPUTS_CUT_OVER_PATCH = "housekeeping-outputs-cut-over"`. In `Housekeeping.run`, before the `OUTPUT_COPY_PATCH` check: `if sweep in (OUTPUT_COPY_SWEEP, OUTPUT_BYTES_SWEEP) and workflow.patched(OUTPUTS_CUT_OVER_PATCH): continue`. `SWEEPS` keeps both names in place, so a recorded run replays its commands. The two activities stay registered and log "the outputs are cut over (#7257); nothing to copy". `GET /outputs/copy-status` keeps its shape and reads the last runs from their tables (`OutputRows.last_run`, `last_byte_run`, `pending_bytes`, `cut_over_at`).
- A recorded run that scheduled a copy activity before the upgrade replays with `patched` False (no marker in its history) and keeps that command.

- [ ] **Step 1:** Write the test changes: `outputs_cut_over` in the replay test's names; `output_copy.json` and `output_bytes.json` still replay; `test_copy_sweeps_do_nothing` (both activities return and touch no table).
- [ ] **Step 2:** Run the replay test; expect FAIL (no `outputs_cut_over.json`).
- [ ] **Step 3:** Implement; record `outputs_cut_over.json` with `SCADBUDDY_RECORD_HISTORIES=1` as the module docstring says, and add its line to that docstring.
- [ ] **Step 4:** Run `tests/test_housekeeping_replay.py`, the housekeeping tests and `tests/api/test_output_copy_status.py`; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): retire the copy sweeps behind a patch (#7257)`.

### Task B3: `OutputStore` reads rows and blobs only

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py`, `backend/scadbuddy/api/outputs.py` (the byte routes map `OutputBytesLostError`; `get_edit_target` loses the `provenance` branch), `backend/scadbuddy/analyzers/gather.py` (`_model_path` always takes the stack; `_model_file` goes), `backend/scadbuddy/library/backfill.py` (`_slug_of` from the row only), `backend/scadbuddy/api/library_associations.py` and `backend/scadbuddy/api/models.py` (`ids_for` from rows), `backend/scadbuddy/library/assets.py` (`referenced_asset_ids` reads rows only; `_referenced_in_tree` goes; a lost blob raises `OSError` so the sweep removes nothing), `backend/scadbuddy/main.py` (lifespan: `OutputStore(paths, rows=…, bytes=…, prints=…)`; on `bambuddy` the start raises `OutputStoreUnusableError`)
- Modify (tests): `backend/tests/support/output_blobs.py`, `backend/tests/support/saved_outputs.py` (`save_files` goes), and the test files that build `OutputStore(paths)` with no rows (`grep -rl "OutputStore(" backend/tests`), which take a new fixture
- Create: `backend/tests/support/row_outputs.py`
- Test: `backend/tests/test_output_store_rows_only.py`, `backend/tests/api/test_output_bytes_routes.py`

**Interfaces:**
- Produces:
  - `OutputStore.__init__(self, paths: DataPaths, *, rows: OutputRows, bytes: OutputBytes, prints: OutputPrintStore | None = None)`. `rows` and `bytes` are required.
  - `class OutputBytesLostError(Exception)` with `output_id: str`, `role: str`, `key: str`.
  - Gone: `_find_dir`, `_files_dir`, `directory`, `directory_from_files`, `row_from_files`, `provenance`, `_provenance_in_tree`, `file_path`, `_role_file`, `_geometry_path`, `_attributed_path`, `thumbnail_path`, `_scan_plate_cover`, `plate_cover_archive`, `_copied`, `reading`'s files branch, and the caches `_dirs`, `_gone`, `_covers`, `_cover_epoch`.
  - `_row_of(output_id) -> OutputRow` raises `OutputNotFoundError` for no row. `get`, `name_and_slug`, `params`, `inputs`, `list_for`, `bom`, `manifest`, `arranged_from`, `backfill`, `record`, `existing`, `pending_backfills`, `_oldest_first` read rows only.
  - `ids_for(slug: str) -> list[str]`: `rows.ids_for_slug(slug)` (new `OutputRows.ids_for_slug`, one query).
  - `open_role`, `read_role`/`local_role`, `open_file`, `model_3mf`: the blob; `None` when the row names no key; `OutputBytesLostError` when the blob is lost (D9). `role_size`, `has_role`, `role_version` read the index only.
  - `def output_store(tmp_path: Path, pool: Pool) -> OutputStore` in `tests/support/row_outputs.py`: rows on `pool`, bytes on `local_content(tmp_path / "blobs", pool)`.

- [ ] **Step 1:** Write tests: `test_get_of_id_without_row_is_not_found_and_reads_no_file` (the outputs root removed), `test_download_of_lost_blob_is_500` (Review Focus 3: remove the blob object; the answer is 500 with D9's text), `test_view_of_lost_model_blob_is_500`, `test_asset_sweep_skips_when_a_model_blob_is_lost`, `test_ids_for_reads_rows`, `test_start_on_bambuddy_store_refuses`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement; move the remaining tests to `output_store`. Delete the tests whose subject is gone (the fallback, the file caches, the folder lookups: in `test_output_store_bytes.py`, `test_output_store_lookup.py`, `test_output_derived.py`, `test_output_store_rows.py`), each by name in the commit body.
- [ ] **Step 4:** Run `tests/test_output_*.py`, `tests/api/test_output*.py`, `tests/test_packing_and_outputs.py`, `tests/test_arrange_outputs.py`, `tests/analyzers/`, `tests/test_assets*.py`; expect PASS. Run mypy.
- [ ] **Step 5:** Commit `feat(outputs): output reads use rows and blobs only (#7257)`.

### Task B4: Writes stop touching the share

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`prepare`, `commit`, `discard`, `save`, `rename`, `delete`, the backfill writes, `put_thumbnail`, `put_drawn_thumbnail`, `claim`; `mark_storefront`, `write_thumbnail`, `write_drawn_thumbnail`, `_write_thumbnail_file`, `_write_meta`, `_folder_write`, `_keep_geometry`'s and `write_attributed`'s files go), `backend/scadbuddy/library/output_rows.py` (`keep_thumbnail` and `draw_thumbnail` lose their `write` step; `write_thumbnail_file` goes), `backend/scadbuddy/library/output_bytes.py` (`spool`), `backend/scadbuddy/core/settings.py` (`output_spool_dir`), `backend/scadbuddy/library/output_operations.py` (`create_run`: `async with outputs.saving(...)`, filing from the staged 3MF, no folder removal on failure; `thumbnail_run`), `backend/scadbuddy/api/outputs.py` (`_draw_and_keep`)
- Test: `backend/tests/test_output_save_staged.py`, `backend/tests/api/test_output_save_bytes.py`

**Interfaces:**
- Produces:
  - `Settings.output_spool_dir: Path` (`SCADBUDDY_OUTPUT_SPOOL_DIR`, default `Path(tempfile.gettempdir()) / "scadbuddy-outputs"`). `OutputBytes(content, spool=…)`; `local_file` and the staging use `OutputBytes.spool`, made on first use (D8).
  - `@dataclass(frozen=True) class SavedOutput(meta: OutputMeta, model_3mf: Path)`.
  - `OutputStore.saving(job, **the save keywords) -> AbstractAsyncContextManager[SavedOutput]`: `prepare` into `tempfile.mkdtemp(dir=bytes.spool)` (in a thread), `put_folder`, `commit` (the row, its `output_files` rows and holds in one transaction), then yields; the staged folder goes on exit, error or not. A failed put or commit removes the staged folder and raises; its blobs are unheld, so the sweep removes them. `save(...)` is `async with self.saving(...) as saved: return saved.meta`.
  - `commit(prepared, blobs: OutputBlobs) -> OutputMeta`: no `meta.json`.
  - `rename`, the backfill writes and `delete` write the row only. `claim` is `rows.claim` only (D6).
  - `put_thumbnail(output_id, png)`: staged put, then `rows.keep_thumbnail(output_id, key, staged, slug=)`. `put_drawn_thumbnail(output_id, png) -> bool`: staged put, then `rows.draw_thumbnail(...)`; a failed put is logged and answers False.

- [ ] **Step 1:** Write tests: `test_save_writes_no_output_folder` (the outputs root is a plain file: the save succeeds; the row names every blob), `test_save_stages_on_spool_and_cleans_up` (the spool is empty after a save and after a failed put), `test_save_store_full_is_507_and_leaves_nothing` (no row, no hold, empty spool), `test_create_run_files_from_staged_3mf` (the native library gets the save's bytes; no blob download: count `OutputBytes.local_file` calls), `test_rename_and_backfill_write_rows_only`, `test_put_thumbnail_writes_no_file`, `test_put_drawn_thumbnail_keeps_upload` (a key already named: False).
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, `tests/api/test_pipeline_api.py`, `tests/test_output_*.py`, `tests/api/test_output*.py`, the quote tests; expect PASS.
- [ ] **Step 5:** Commit `feat(outputs): saves and edits write rows and blobs only (#7257)`.

### Task B5: The reapers and the catalogue read rows; the outputs path goes

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`reap_orphan_holds(refs, store, *, grace)`: the rows path of Task A5 only; `_live_outputs`, `_remove_orphan_directory`, `_remove_unheld_directories`, `_record_slug`, `_record_slugs`, `hold_parts`'s slug write, `restore_rows`, `forget_plate_cover` and `remembers_plate_cover` go), `backend/scadbuddy/library/catalogue.py` (`delete`: rename the model first, then `_forget_outputs`, a failure there logged and left to `sweep_orphans`; `sweep_orphans`: the orphan slugs are `rows.slugs()` minus the slugs with a model directory, each forgotten, no folder removed; `_clear_derived` removes no outputs folder; the `restore_rows` and `forget_plate_cover` calls go), `backend/scadbuddy/library/output_rows.py` (`slugs() -> set[str]`, one `SELECT DISTINCT slug`), `backend/scadbuddy/core/paths.py` (`outputs`, `output_dir` and the `ensure` entry go)
- Test: `backend/tests/test_output_share_untouched.py`, `backend/tests/test_catalogue*.py`, `backend/tests/test_output_blob_gc.py`, `backend/tests/test_output_backfill.py` (its `output_hold_slugs` tests go)

**Interfaces:**
- Consumes: Task A5's rows-only reaper path, Task B3's `OutputStore`.
- Produces: `OutputRows.slugs() -> set[str]`. `Catalogue.sweep_orphans` reports a forgotten slug's outputs as `outputs/<slug>` in its removed list, as today, though it removes no folder.

- [ ] **Step 1:** Write tests: `test_no_output_path_touches_the_share` (Review Focus 5: an app whose `data/outputs` is a plain file runs a save, the outputs list, a download, a rename, a thumbnail upload, an output delete, a model delete, `reap_orphan_holds` and `sweep_assets`; all pass), `test_model_delete_forgets_rows_after_rename`, `test_sweep_orphans_forgets_rows_of_missing_model`, `test_reaper_releases_holds_of_output_without_row`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement. `uv run --frozen mypy` then names every caller of the removed `DataPaths` members; fix each.
- [ ] **Step 4:** Run the full backend suite (`pytest -n 4`), ruff and mypy; expect PASS. `grep -rn "paths.outputs\|output_dir(\|META_NAME\|meta.json" backend/scadbuddy` finds nothing.
- [ ] **Step 5:** Commit `feat(outputs): the reapers and the catalogue read rows; nothing touches the share (#7257)`.

### Task B6: Docs for release B

**Files:**
- Modify: `docs/architecture/data.md` ("Saved outputs": the rows and blobs are the only record; the folder is an unread backup; the copies are retired; drop "The byte copy" runbook text that no longer applies, keep what copy-status still answers; the table list: no `output_index`; "Files on the data volume": `data/outputs/` is no longer written), `README.md` (the "State lives in `/data`" line: outputs are in Postgres and the blob store; the connection notes near line 470 lose the copies' advisory locks), `backend/CLAUDE.md` (the saved-outputs paragraph, the housekeeping paragraph's copy sweeps), `docs/features/preview-and-3mf.md`, `docs/features/arrange-and-plates.md`, `docs/features/versions-and-history.md`, the spec §4.2 and §4.5 (as built for 3b)

- [ ] **Step 1:** Update each page. `grep -rn "output_index\|OutputIndex\|meta\.json\|data/outputs\|outputs/<slug>\|fallback\|byte copy\|copy_outputs" docs/architecture docs/features backend/CLAUDE.md README.md` shows no line this release made false.
- [ ] **Step 2:** Run the full backend lint, format check, mypy and tests; regenerate the API types (`cd frontend && pnpm gen:api && pnpm typecheck`; `cd agent && pnpm gen:api && pnpm typecheck`); expect PASS.
- [ ] **Step 3:** Commit `docs(outputs): phase 3b as built: rows and blobs are the only record (#7257)`.

### After release B deploys

1. Confirm every API pod runs release B's image. While the first new pod's migration refuses (D2), the pod restarts and the release A pods keep serving; read its log line, fix the cause on release A, and the roll continues by itself.
2. Read `GET /api/v1/outputs/copy-status`: `cut_over_at` is set and `bytes.pending` is 0.
3. Measure, and post the numbers on #7257:
   - `GET /api/v1/print/library` (spec §1: 1.5–13 s on 2026-11-23, against 0.1 s for other routes): its server span's duration in Tempo (`{ name = "GET /api/v1/print/library" }`) over 24 h after the roll, as p50, p95 and max, against the same query on a day before phase 1. A p95 over 1 s is a new issue that links the slowest trace.
   - The outputs lists, the same way: `GET /api/v1/outputs` and `GET /api/v1/models/{slug}/outputs`.
   - In the same 24 h there is no span named `outputs.find_dir`, `outputs.scan_all`, `outputs.scan_plate_cover` or `output_index.sync`.
   - A `/proc/<pid>/fd` sample of an API pod (as in spec §1) lists no path under `/data/outputs/`.
4. Tell the owner that `data/outputs/` is now an unread backup, and that deleting it is the owner's step.

### Follow-up issues to file with release B

- Remove the no-op copy activities and replace `workflow.patched(OUTPUTS_CUT_OVER_PATCH)` with `workflow.deprecate_patch`, then drop `OUTPUT_COPY_PATCH`, `OUTPUT_BYTES_PATCH` and the two names from `SWEEPS`, once no recorded run that names them can replay (one `housekeeping_timeout` after release B), with the replay fixtures that still name them.
- Drop `outputs.copied_at`, `files_stamp`, `bytes_stamp` and (once copy-status no longer reports them) `output_copy_runs` and `output_byte_runs` (D11).
