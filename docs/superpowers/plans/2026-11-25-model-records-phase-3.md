# Model records in Postgres (phase 3: no file fallback) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The catalogue's readers read the `templates` rows always, not only once a copy run has finished. The "before the copy" walks of the folders and of the git history go from every list, summary, facet and name path. The walk then lives only in the copy (#7317, spec §5.6 item 3).

**Architecture:** One release. Phase 1 and 2 pods write the same rows by the same refresh, and phase 3 changes no write, no table and no workflow, so old and new pods can serve side by side during the roll. A database with no finished copy run (a fresh install, a test's database) gets its first copy at the API's start, before the app serves. Then the readers drop the `copied()` gate and the code that walked the files before it. Single-template file reads that are not a "before the copy" fallback stay (see Decisions D3).

**Tech Stack:** Python 3.12, psycopg 3 + psycopg_pool, FastAPI lifespan, pytest with the repo's Postgres fixtures (`tests/conftest.py` `pg_conninfo`, `pg_pool`).

**Spec:** `docs/superpowers/specs/2026-11-25-model-records-in-postgres-design.md` (§5.4, §5.6 item 3, §6, §7.4, §7.6, §8, §10). Phases 1 and 2: `docs/superpowers/plans/2026-11-25-model-records-phases-1-2.md`, built in PR #7518 and PR #7631. The two-release pattern this plan does not need (and why): `docs/superpowers/plans/2026-11-25-outputs-cutover-phase-3.md` on `origin/feat/output-cutover-a-7257`, decisions D1 and D2.

## Global Constraints

- Only the API process holds SQL (root `CLAUDE.md`, "Database access (#5295)"). No worker gains a client or a route.
- No migration in this phase. If one is needed after all, it is a NEW file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one (root `CLAUDE.md`, "Migrations (#491)").
- No change to `Housekeeping.run`: the copy sweep `TEMPLATE_COPY_SWEEP` stays as it is. No new replay history.
- No change to a `ModelRecord` field, a template id, a URL, a response shape or an `ETag` value (spec §2 anti-goals).
- A save adds no Postgres transaction (prod commit latency 1–2 s, known). This phase changes no write path.
- Git stays the record (spec §7.1). Nothing on NFS is removed or stops being written (spec §5.6 item 3).
- `library_users` and `library_pins` keep reading the files (spec §7.4).
- No sync file, git or SQL call on the event loop: use `asyncio.to_thread`.
- Do not include #7661 (the `list_summaries` nit): it is its own PR.
- TDD: watch each new test fail before the change.
- Docs and comments in Simplified Technical English. Cite sources. No counts in docs. A doc that the diff makes false changes in the same diff.
- Commits: conventional titles that name #7317, messages end `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never amend, never force-push.
- Backend commands as `backend/CLAUDE.md` says: `uvx uv@0.13.9 run --frozen` ruff, mypy, pytest. On this machine, Postgres in Docker on a unique port, one container per parallel run, removed by name (`CLAUDE.local.md`).

## Decisions (the plan's calls; the owner may change them)

- **D1. One release, not two.** The outputs cut-over needs two releases because a phase 2 output pod writes into folders that release B no longer makes (outputs plan D1). Here no write changes: phase 1, 2 and 3 pods all write the same rows by `TemplateRefresh`, under the same write lock, and the files stay the record. Prod runs 3 replicas with `RollingUpdate`, `maxSurge` 1, `maxUnavailable` 0 (outputs plan D1, read 2026-11-24). During the roll a phase 2 pod reads the rows (its database has a finished run) and a phase 3 pod reads the same rows. A rollback to phase 2 or 1 also works: they read rows or files, and their writes refresh rows. A rollback to a build before phase 1 writes no rows; the start copy of the next phase 3 pod and the sweep repair them (spec §8).
- **D2. No mark in the database and no migration gate.** The outputs release B refuses to start without its mark because it removes the copies and stops writing the files, so nothing could fill a row later (outputs plan D2). Here the copy stays at each start and in each sweep, and a row can always be rebuilt from the tree (spec §7.1). The gate that phase 3 needs is only "the rows exist before the app serves". D4 gives that at the start, where it costs prod nothing.
- **D3. Which file reads stay, by design.**
  - Writes: every write method answers `_fresh_record(slug)` from the files, so its answer shows the write even when its refresh failed (phase 1–2 plan decision 11). It is one template, read after the writer wrote it.
  - Single-template readers: `record(slug)`, `print_settings`, `part_print_settings` and the thumbnail keep one `Catalogue.exists` check (a gone folder is a 404). They read the template's files only when its row is missing (a failed refresh, a hand-made folder), when Postgres cannot store its `model.json` (`_not_storable`, `meta_error` `not storable (…)`), or, for the thumbnail and the media sizes, while the row lags its media (`TemplateRow.media_newer`).
  - Rows Postgres cannot store: every reader keeps reading their file (`raw_metas`, `list_summaries`, `_list_rows` `_unstored_record`, `_other_names`, `duplicates_of`). Refusing them would drop a template that exists from the list (spec §2 anti-goal). They are rare: prod's copy reported `unreadable` `[]` when phase 2 started (progress ledger, 2026-11-25). Each costs reads of its own files only, never a walk.
  - Git: `version_pair` and `ui_version` for one record (spec §7.5), the copy's `last_commits` walk, and every history route.
  - Source, `ui/`, `pipeline/`, media bytes, a thumbnail 200, editing, rendering, restore, diff, export, duplicate and merge (spec §2 non-goals). `exists()` for writes and renders.
  - `library_users` and `library_pins` walk the folders (`_walked_slugs`, spec §7.4).
  - The copy (`library/template_copy.py`): the only walk of the folders and of the history for the rows.
  - The output cover's fallback to the output folders before the outputs copy (`_list_rows` `plate_cover_output`, `record` `output_covers`) belongs to the outputs cut-over (#7257) and stays here.
- **D4. The start copies first when no run has finished.** `_first_template_copy` runs in the lifespan after `_start_render` and before the app serves. When `TemplateRows.copied()` is already true (prod) it does nothing and costs one query. Otherwise it runs one copy and waits for it. When another replica holds the copy lock (3 replicas of a fresh install start together), it polls `copied()` every `FIRST_COPY_POLL` (1 s) up to `FIRST_COPY_WAIT` (120 s; amended from 300 s after the Task 1 review: the API's startupProbe allows about 300 s for the whole start, eh-homelab/clusters `applications/scadbuddy/scadbuddy.yaml`), and takes the lock itself when the other replica's run ends unfinished. A SIGTERM or SIGINT stops the copy and the wait. A copy that fails or ends `incomplete`, or a wait that ends, is logged at error level and the app serves anyway: an incomplete run still wrote a row for every template it read, and the sweep repairs the rest. The background start copy (`_copy_templates_logged`) does not run when this start's first copy finished; otherwise it stays as it is.
- **D5. A reader with no rows open is an error, not a walk.** `Catalogue.rows` stays `TemplateRows | None` (the lifespan sets it after the migrations). A list reader asks `Catalogue._rows() -> TemplateRows`, which raises `RuntimeError("the template rows are not open: the API opens them after its migrations (#7317)")` when it is None. In prod only `api/deps.py` makes a `Catalogue`, and no request is served before the lifespan sets `rows`.
- **D6. The span attribute `scadbuddy.from_rows` goes** from `catalogue.list_models` and `catalogue.list_summaries`: it is always true now. No dashboard or doc reads it (`grep -rn from_rows deploy docs agent/src frontend/src` finds only the phase 1–2 plan).
- **D7. The merge gate is "Before merge", not a fixed week.** Phase 2 has been on prod since 2026-11-25 12:25Z (sha-00eb878). Spec §8's week of watching was the gate before phase 2. Phase 3 merges once "Before merge" below passes on prod at merge time, over every `template_copy_runs` row since the phase 1 deploy (amended by the controller's ruling of 2026-11-25; spec §8).

## Review Focus

1. A template that a person makes by hand on the volume: the list does not show it until the next copy run; `GET /models/{slug}` reads it from its files at once (`test_a_hand_made_template_is_listed_after_the_next_copy`, Task 2).
2. A create whose row refresh failed: the create's answer and `GET /models/{slug}` show it; the list does not until the next copy run (`test_a_create_whose_refresh_failed_is_readable_but_not_listed`, Task 2).
3. A fresh install whose replicas start together: one copies, the others wait for its finished run and then serve the full list (`test_first_copy_waits_for_another_replicas_run`, Task 1).
4. A row whose `model.json` Postgres cannot store: listed, summarised, name-checked and judged as a duplicate from its file, with no walk (`test_a_not_storable_row_is_read_from_its_file_only`, Task 2).
5. Code that reads the list before the rows are open: a clear `RuntimeError`, not an empty list (`test_list_readers_refuse_without_rows`, Task 2).

---

PR title `feat(models): the readers read only template rows, phase 3 (#7317)`, body `Fixes #7317` and `Part of #7289`. Branch `feat/model-records-phase-3-7317` from `origin/main`.

### Task 1: The first copy at start, and tests that seed files after the start

**Files:**
- Modify: `backend/scadbuddy/main.py` (`_copy_templates_once` returns its result; new `_first_template_copy`, `FIRST_COPY_WAIT`, `FIRST_COPY_POLL`; the lifespan calls it right after `await _start_render(state)`, near `main.py:2618`)
- Create: `backend/tests/support/template_rows.py`
- Modify: `backend/tests/conftest.py` (`app_template_copy_off` docstring), `backend/pyproject.toml` (the `template_copy` marker text)
- Modify: every test that the full run in Step 6 shows failing because it seeds template files after its app started
- Test: `backend/tests/api/test_template_first_copy.py`

**Interfaces:**
- Produces, in `main.py`:
  - `async def _copy_templates_once(state: AppState, stop: threading.Event) -> TemplateCopyResult | None` (it answers `copy_templates`' result; None when another replica holds the lock or `refresh` is None).
  - `FIRST_COPY_WAIT = 120.0` (amended from 300.0, D4), `FIRST_COPY_POLL = 1.0` (seconds).
  - `async def _first_template_copy(state: AppState, stop: threading.Event) -> None`: D4. Every SQL and the copy run in threads.
- Produces, in `tests/support/template_rows.py`:
  - `def sync_template_rows(client: TestClient) -> TemplateCopyResult`: runs `copy_templates(state.catalogue, refresh.rows, refresh, state.projection.pool)` on the app's state and asserts a result that is not None and not `incomplete`. A test calls it after it writes template files with no catalogue write (a hand edit).
  - `def open_template_rows(catalogue: Catalogue, pool: PgPool) -> TemplateRows`: for a test that makes its own `Catalogue`: sets `catalogue.refresh = TemplateRefresh(catalogue, TemplateRows(pool))`, `catalogue.rows = catalogue.refresh.rows` and, when the catalogue has a history, `history.template_rows = catalogue.refresh`; then runs one `copy_templates` and answers the rows.

- [ ] **Step 1: Write the failing tests** in `tests/api/test_template_first_copy.py` (no `template_copy` marker; templates in `seed_dir` and in `data_dir/models` before the `TestClient` enters):
  - `test_a_start_with_no_finished_run_copies_before_serving`: the first `GET /api/v1/models` lists every seeded template; `template_copy_runs` holds one row with `finished_at` set.
  - `test_a_start_after_a_finished_run_does_not_copy_first`: insert a finished `template_copy_runs` row in `pg_conninfo` before the start; monkeypatch `scadbuddy.main.copy_templates` to count calls; after the `TestClient` enters, `_first_template_copy` made none (the background copy is off by `app_template_copy_off`).
  - `test_first_copy_waits_for_another_replicas_run`: hold `COPY_LOCK` (`pg_advisory_lock(hashtextextended(COPY_LOCK, 0))`) on a connection of its own; start `_first_template_copy` in a task with `FIRST_COPY_POLL` 0.05; it has not returned after 0.3 s; insert a finished run row; it returns within 2 s.
  - `test_first_copy_gives_up_after_the_wait`: the lock held and no run; `FIRST_COPY_WAIT` 0.2; it returns and logs at error level (`caplog`).
  - `test_a_failed_first_copy_still_serves`: `copy_templates` monkeypatched to raise `OSError`; the app starts and `GET /api/v1/models` answers 200 (empty or partial).
- [ ] **Step 2: Run them; expect FAIL** (`_first_template_copy` does not exist; the list is served from the files, not after a run).
  Run: `cd backend && uvx uv@0.13.9 run --frozen pytest tests/api/test_template_first_copy.py -q`
- [ ] **Step 3: Implement** `_first_template_copy`, the result of `_copy_templates_once`, the lifespan call and `tests/support/template_rows.py`.
- [ ] **Step 4: Run Step 2's command; expect PASS.**
- [ ] **Step 5: Run the full backend suite** (`uvx uv@0.13.9 run --frozen pytest -n 2 -q`, one run at a time on this machine). Every test app now reads rows (its start finished a copy), so a test that writes template files after its app started, and then reads a list, a summary, a name check, `duplicates_of` or the pin index, fails here. List the failures.
- [ ] **Step 6: Fix each failure** by its class, and record the class in the commit message:
  - (a) The test writes or edits files with no catalogue write, then reads: call `sync_template_rows(client)` after the write. The hand edit is what the copy repairs (spec §5.3).
  - (b) The test asserts the "before the copy" answer (`tests/api/test_models_from_rows.py` `_copy` asserts `not state.catalogue._from_rows()`; the "same before and after" tests): rewrite the baseline. Compare each listed record with `catalogue._fresh_record(slug).model_copy(update={"previous_version": None, "ui_version": None})`, and each `GET /models/{slug}` with `_fresh_record(slug)`. Keep the `X-Total-Count`, page, `q`, `tag`, `origin` and `sort` cases.
  - (c) The test makes its own `Catalogue` and has no rows: leave it for Task 2 (it still walks until then).
  Do not change production code in this step.
- [ ] **Step 7: Run the full suite again; expect PASS.** Run `ruff check`, `ruff format --check` and `mypy` over `backend/`.
- [ ] **Step 8: Commit** `feat(models): the start copies the templates to rows before it serves when no run has finished (#7317)`.

### Task 2: The readers read only rows

**Files:**
- Modify: `backend/scadbuddy/library/catalogue.py`:
  - remove `_from_rows` (`:1214`), `versions` (`:962`, its only caller is `_list_models`), `_list_models` (`:1952`);
  - `slugs` (`:1649`): `self._rows().ids()`; `_walked_slugs` stays for `_library_entries` only;
  - `list_models` (`:1838`): `_list_rows` always; no `scadbuddy.from_rows` attribute (D6);
  - `list_summaries` (`:1790`): remove the branch built from `list_models`; no `scadbuddy.from_rows` attribute;
  - `raw_metas` (`:1701`): remove the walk for "not copied"; keep the file read of a not-storable row and of a slug with no row (D3);
  - `_other_names` (`:2243`): remove the `self.slugs()` walk of every `model.json`;
  - `duplicates_of` (`:3494`): remove the `_templates_in(self.paths.models)` walk; keep the not-storable candidates' file reads;
  - `record` (`:1219`), `_settings_meta` (`:1377`), `_thumbnail_ref` (`:1092`): remove the "not copied" branch; keep the no-row, not-storable and `media_newer` file reads (D3);
  - `thumbnail_ref` (`:1062`): `_files_thumbnail_ref` stays only for a row that lags its media and a template with files but no row;
  - new `def _rows(self) -> TemplateRows` (D5), used by every reader above in place of `assert self.rows is not None`;
  - the docstrings and the `rows` attribute comment (`:803-806`) no longer say "once a copy run has finished" or "before the copy".
- Modify: the tests of Task 1's class (c) (`grep -rln "Catalogue(" backend/tests`; those that call a list reader: `test_template_row_builder.py`, `test_print_settings.py`, `test_libraries.py`, `test_history.py`, `test_media_store.py`, `test_template_part_settings.py`, `test_template_refresh.py`, `test_previews.py`, `test_history_replicas.py`, `test_output_store_rows.py`, `api/test_upstream.py`, `api/test_models.py`): give the catalogue rows with `open_template_rows(catalogue, pg_pool)`.
- Modify: `backend/tests/api/test_models_from_rows.py`, `test_model_readers_from_rows.py`, `test_thumbnail_from_rows.py`: remove each assertion on `_from_rows()` and each "before the copy" case that Task 1 did not already rewrite.
- Test: `backend/tests/api/test_models_no_fallback.py`

**Interfaces:**
- Consumes: Task 1's `sync_template_rows`, `open_template_rows`.
- Produces: `Catalogue._rows() -> TemplateRows`. No other new name. `TemplateRows.copied()` stays (Task 1 and copy-status use it).

- [ ] **Step 1: Write the failing tests** in `tests/api/test_models_no_fallback.py`. The fixture: an app; templates made through `POST /api/v1/models`, so their rows come from the refresh; then `DELETE FROM template_copy_runs` on the app's pool, so no run has finished:
  - `test_lists_read_rows_with_no_finished_run`: monkeypatch `Path.iterdir`, `Path.stat`, `Path.is_file`, `Path.read_text`, `Path.read_bytes` (under the data root only, as `test_thumbnail_from_rows.py` does) and `ModelHistory._run` to raise; `catalogue.slugs()`, `list_models()`, `list_summaries()`, `raw_metas()` and `library_pin_index()` answer the created templates. Fails today: they walk.
  - `test_model_facets_read_rows_with_no_finished_run`: the same guard; `GET /api/v1/model-facets` answers 200 with `total` equal to the created count.
  - `test_a_thumbnail_304_reads_no_file_with_no_finished_run`: a template with its own cover; `GET …/thumbnail` gives its `ETag` (the cover's sha256); with the guard on, the same request with `If-None-Match` answers 304.
  - `test_a_rename_onto_a_taken_name_is_refused_with_no_finished_run`: the guard on `read_bytes` of `model.json`; `PATCH` onto another template's name answers 409 `ModelNameTakenError`.
  - `test_a_hand_made_template_is_listed_after_the_next_copy` (Review Focus 1): write a template folder on the volume; the list lacks it; `GET /api/v1/models/{slug}` answers 200 from its files; after `sync_template_rows(client)` the list has it.
  - `test_a_create_whose_refresh_failed_is_readable_but_not_listed` (Review Focus 2): `TemplateRows.put` and the HEAD transaction's row write raise (monkeypatch `template_rows.write_rows`); `POST /api/v1/models` answers 201 with the record; `GET /api/v1/models/{slug}` answers 200; `GET /api/v1/models` lacks it; after `sync_template_rows(client)` it lists it.
  - `test_a_not_storable_row_is_read_from_its_file_only` (Review Focus 4): a `model.json` with `NaN`; the list, `list_summaries`, the name check and `duplicates_of` answer it; with `Path.iterdir` refused under `models/`, all still answer (no walk).
  - `test_list_readers_refuse_without_rows` (Review Focus 5): a `Catalogue` with `rows` None; `slugs()` and `list_models()` raise `RuntimeError` whose text names #7317.
- [ ] **Step 2: Run them; expect FAIL** (the first four walk the files; the last answers from the walk).
  Run: `cd backend && uvx uv@0.13.9 run --frozen pytest tests/api/test_models_no_fallback.py -q`
- [ ] **Step 3: Implement** the removals and `_rows()` above, and give the class (c) tests their rows.
- [ ] **Step 4: Run Step 2's command; expect PASS.**
- [ ] **Step 5: Run the full backend suite, `ruff check`, `ruff format --check` and `mypy`; expect PASS.** Then show that no "before the copy" reader is left: `grep -n "_from_rows\|copied()\|def versions\|_list_models" backend/scadbuddy/library/catalogue.py` finds nothing, and `grep -n "_walked_slugs\|_templates_in(" backend/scadbuddy/library/catalogue.py` finds only `_walked_slugs`' definition, `_library_entries` and `_templates_in`'s own definition.
- [ ] **Step 6: Commit** `feat(models): the readers read only template rows, with no file fallback (#7317)`.

### Task 3: The docs

**Files:**
- Modify: `docs/architecture/data.md` ("Template records", `:144-235`): the readers read the rows always; a database with no finished run gets its first copy at the start before the app serves (D4); the list of file reads that stay (D3) in place of "before that, they read the files".
- Modify: `docs/features/catalogue-and-models.md` (`:20-36` and the sequence diagram line `:47`, "read the rows (after a copy run; before it, model.json from Git)").
- Modify: `backend/CLAUDE.md` (the "Template records" paragraph, `:1542-1559`): phase 3; the first copy at start; a test's app copies at start, and a test that edits template files by hand calls `tests/support/template_rows.py` `sync_template_rows`.
- Modify: `docs/superpowers/specs/2026-11-25-model-records-in-postgres-design.md` §5.4 only if its text is now false for the built code; §5.6 item 3 says what phase 3 removes, and §6 asks for these docs (no other spec change).

- [ ] **Step 1:** `grep -rn "before the copy\|once a copy run has finished\|once they are copied\|_from_rows\|template_copy\b" docs backend/CLAUDE.md backend/scadbuddy backend/tests/conftest.py backend/pyproject.toml` and list each hit that phase 3 makes false.
- [ ] **Step 2:** Fix each hit, and write the paragraphs above. No counts; cite `scadbuddy/main.py` `_first_template_copy` and `scadbuddy/library/catalogue.py` `_rows`.
- [ ] **Step 3:** Run `.github/scripts/lint-*.sh` that touch docs if any apply (none is known to), and re-run Step 1's grep: each remaining hit is true.
- [ ] **Step 4: Commit** `docs(models): the readers read only template rows, phase 3 (#7317)`.

## Before merge (spec §8, D7)

Read on prod with `deploy/CLAUDE.md` "Prod checks" (`kubectl -n bambuddy exec scadbuddy-db-1 -c postgres -- psql -U postgres -d scadbuddy -tAc …`):

1. `SELECT id, started_at, finished_at, incomplete, added, refreshed, dropped, jsonb_array_length(unreadable) FROM template_copy_runs WHERE started_at > '2026-11-25T12:25Z' AND (added + refreshed + dropped > 0 OR incomplete OR finished_at IS NULL) ORDER BY id`.
2. Match each row to a roll-out: the `scadbuddy` Argo CD app's `status.history[].deployedAt` (`kubectl -n argocd get application scadbuddy -o json`). A repair outside a roll-out window is a writer that §5.2 misses: file an issue with the template ids (`SELECT template_id, refreshed_at FROM templates ORDER BY refreshed_at DESC LIMIT 20`) and fix it before this merge.
3. `scadbuddy_template_refresh_failures_total` did not increase in the week (Grafana, by `path`).
4. `SELECT count(*) FROM templates WHERE meta_error LIKE 'not storable%'`: note the count in the PR (D3: each is read from its file).
5. Post the result on #7317.

## After deploy

1. Check that every API pod runs the new image: `kubectl -n bambuddy get pods -l app.kubernetes.io/name=scadbuddy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.containers[*].image}{"\n"}{end}'` (use the labels the deployment has).
2. Read `GET /api/v1/model-records/copy-status`: a run that started after the last old pod went, with `finished_at` set, `incomplete` false, `refreshed` and `dropped` 0 (only an older pod's writes during the roll may show), `unreadable` as in "Before merge" step 4.
3. Time, in Tempo (the `tempo` service in namespace `tempo`, through a `kubectl port-forward`, as spec §1), over the hours after the roll: server `GET /api/v1/models`, server `GET /api/v1/model-facets`, server `GET /api/v1/models/{slug}/thumbnail` answered 304. Compare with phase 2 (2026-11-25: `/models` 0.29–0.43 s, `/model-facets` 0.42–0.54 s, thumbnail 304 0.07–0.12 s) and with spec §1. Expect them equal: phase 2 already read rows on prod.
4. Check that no list span walks: a `catalogue.list_models` span has no child `catalogue.slugs`, `model_history.last_commits` or `outputs.scan_plate_cover` span (TraceQL `{ name = "catalogue.list_models" } >> { name = "model_history.last_commits" }` finds no trace after the roll).
5. Post the numbers on #7317 and close it if the PR did not.
