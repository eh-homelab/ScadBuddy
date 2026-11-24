# Model records in Postgres — design

Date: 2026-11-25. Status: the author's design; the owner decides only §9's question.
Issue: #7317 (part of #7289). Pattern taken from the outputs design,
`docs/superpowers/specs/2026-11-23-outputs-in-postgres-design.md` (#7254), and its
phase 1 as built in PR #7360 (#7255).

## 1. Intent and the measured problem

A template (a model) is a folder on the NFS share, `data/models/<slug>/` (a built-in:
`data/models/_builtin/<slug>/`), inside one git repository. Its record is `model.json`.
No Postgres table holds what `model.json` says. So every list of models walks the
share (`backend/scadbuddy/library/catalogue.py`):

- `slugs()` lists `models/` and `_builtin/` and stats each folder's `model.scad`
  (`_templates_in`).
- `list_models()` builds each record with `_record()`. For each template it stats
  `model.scad` (`_require`), reads `model.json` (`read_raw_meta`), stats `model.scad`
  and `model.json` again for `updated_at`, stats each media file and poster
  (`_item_views`) or the legacy `thumbnail.png` (`_views`), stats `README.md`, reads
  the pipeline module (`_inputs_version`), and stats the upstream's `model.scad` for a
  duplicate (`exists`). The fallback cover may scan the template's outputs
  (`OutputStore.plate_cover_output`).
- It also asks git for every template's last commit (`ModelHistory.last_commits`, one
  `git log --name-only` over the whole history on NFS, kept per replica until HEAD
  moves).

Measured on prod, 2026-11-24, from the API's spans in Tempo (the `tempo` service in
namespace `tempo`, through a `kubectl port-forward`; TraceQL searches over the 72 hours
up to 17:50 UTC). The share held 159 templates of mine and 37 built-ins (`ls` of
`/data/models/*/model.scad` and `/data/models/_builtin/*/model.scad` in an API pod).

| Span | Count | p50 | p90 | max |
|---|---|---|---|---|
| server `GET /api/v1/models` | 178 | 2.75 s | 15.93 s | 34.75 s |
| server `GET /api/v1/model-facets` | 39 | 7.61 s | 30.78 s | 44.29 s |
| `catalogue.list_models` (all callers) | 227 | 2.43 s | 18.69 s | 44.02 s |
| `catalogue.records` (the per-template reads) | 76 | 1.92 s | 9.73 s | 12.67 s |
| `model_history.last_commits` | 228 | 0.05 s | 7.38 s | 24.84 s |
| `catalogue.slugs` | 79 | 0.42 s | 1.00 s | 3.70 s |
| `catalogue.library_entries` | 8 | 1.15 s | 15.06 s | 15.06 s |
| server `GET /api/v1/models/{slug}/thumbnail` | 164 | 0.15 s | 0.50 s | 1.95 s |

- The slowest `model-facets` (44.29 s, trace `3212d7dc…`) spent 24.84 s in
  `last_commits` (a walk after HEAD moved), 2.37 s in `slugs` and the rest in the
  per-template reads. The slowest `GET /models` (34.75 s, trace `693d9a4c…`) spent
  4.07 s in `last_commits` and about 30 s in the per-template reads.
- `catalogue.records` appears only in the newer traces (the span came with #7253), so
  it has fewer samples than `catalogue.list_models`.
- Callers of `list_models` besides the Models page: the agent's MCP
  `resources/list` (68 in the window, p50 1.34 s, max 19.18 s), and requests whose root
  span is only `GET` or `POST` (outputs `?tag=`, activity facets, print-import
  suggestions).
- The thumbnail route sends `Cache-Control: no-cache` and hashes the image bytes for
  its `ETag` (`api/models.py` `get_thumbnail`), so a 304 still reads the cover from
  NFS.

The goal: a list of models costs SQL reads and no file operation, and the lists stay
correct when any replica, an old pod or a person on the volume changes a template.

Repo rules that apply: only the API process holds SQL (root `CLAUDE.md`, "Database
access (#5295)"); state goes in Postgres, bytes in the blob store, never `bytea`;
migrations are new files only (root `CLAUDE.md`, "Migrations (#491)"); a change to
`Housekeeping.run` goes behind `workflow.patched` with a replay test
(`workflows/housekeeping.py` docstring).

## 2. Goals, non-goals, anti-goals

Goals

- `GET /models`, `/model-facets`, outputs `?tag=`, activity facets, print-import
  suggestions, the libraries report's pin index, `library_associations`, the previews'
  backfill listing and the print settings of a slice read rows, not files and not a
  git walk.
- A thumbnail 304 reads no file.
- A write's own response, and the next list on any replica, show the write.

Non-goals

- Source files, `ui/`, `pipeline/`, media bytes and history stay in the `models/` git
  repository on NFS. Editing, rendering, restore, diff, export, duplicate and merge
  read files as today.
- Media bytes to the blob store, and `model_previews.png` (a `bytea` column today).
  A 200 of the thumbnail route still reads the image. Separate work.
- `exists()` for writes and renders (one stat, revalidated on a miss, #2384) does not
  change.
- Filtering or sorting in SQL. The rows are few; the Python selection and sort
  (`select_models`, `sort_models`, `tag_counts`) stay, so results do not change.

Anti-goals (a failure even if it works)

- A list that misses a template that exists, or shows one that is gone, for longer
  than one housekeeping sweep.
- A `ModelRecord` field, an id or a response shape that changes.
- A slower save. Prod Postgres has 1–2 s commit latency (known): a save that commits
  must not add a Postgres transaction.
- A library checkout removed while a live `model.json` pins it.

## 3. Facts the design rests on

Read in the code at `7b2ad4237` (origin/main, 2026-11-24).

1. Every catalogue write ends in a commit of the models repository, under its write
   lock: `Catalogue._commit_change` runs the change as the commit's `prepare`
   (`core/gitrepo.py` `GitRepo.commit`), and `_commit`, `create`, `delete`,
   `sync_builtins`, `link_seeded`, the media writes and `ModelHistory.restore`
   (used by `api/versions.py` `restore_run`) commit under the same lock
   (`GitRepo._exclusive`).
2. The lock is a Postgres advisory lock (`library/repo_lock.py` `PgRepoCoordinator`).
   Under it, `_settle` waits until git shows the HEAD the last writer recorded, so
   reads under the lock see every replica's writes (#2385).
3. When a commit lands, `GitRepo._landed` records it in ONE transaction that upserts
   `models_repo_head` and `NOTIFY`s `scadbuddy_models_head` with the templates the
   commit touched (`_PgHold.record`; `ModelHistory._change_of` reads them with
   `git diff-tree`). Every replica hears it and revalidates its NFS view
   (`library/repo_signal.py`). A failed record is logged, not raised: the commit has
   landed.
4. Some changes make no commit: a media change that touches only videos (the
   `.gitignore` leaves `*/media/*.mp4` and `*.webm` out, `library/history.py`
   `_gitignore_body`), media added to a built-in (`builtin-media/<slug>/`, outside the
   repository; `_overlay_change`, under the same write lock), a commit that fails
   (logged by `_commit`), and every write when git is missing (`history.available`
   false, no lock).
5. `ModelRecord` is `model.json` plus derived facts: the template's `version` (its last
   commit), `previous_version` and `ui_version` (asked of git per record, `None` in a
   listing), `updated_at` (the newest mtime of `model.scad` and `model.json`), the
   media views with each file's size and `missing` flag, the cover, `has_readme`,
   `inputs_version`, `upstream_state` (needs the upstream's revision) and
   `invalid_libraries`.
6. Already in Postgres: a template of mine's media list (`template_media`), a built-in's
   chosen cover (`template_media_cover`), the default-render preview
   (`model_previews`), and, once #7360 merges, each output's record with
   `has_plate_cover` (`outputs`). A listing already reads the media rows in one query
   (`media_store.items_for`).
7. A `model.json` that cannot be read leaves its template out of the list, but
   `X-Total-Count` counts it (`api/models.py` `list_models`).
8. `library_users` and `library_pins` decide whether a library checkout may be
   removed (`main.py` `_sweep_checkouts`, `api/libraries.py`). They count an
   unreadable `model.json` that mentions the library's name.
9. Only the API process (with its in-process `library` worker) writes `data/models`.
   The render, print and projects workers have no database client.

## 4. Approaches

1. **A derived row per template, refreshed under the models write lock (chosen).**
   `model.json` in git stays the record. A `templates` row holds what a listing needs.
   The writer refreshes it under the lock it already holds, in the HEAD's own
   transaction when it commits. A reconciling copy fills and repairs the rows.
2. **Rows written by each writer of `model.json`** (as the issue first said). There
   are many such writers, and several do not go through `write_raw_meta`: `create`,
   `duplicate`, `write_file`, `update_files`, `merge_upstream`, `sync_builtins`, and
   a restore through the history. One missed writer is a stale list with no error.
3. **Move the record out of git into Postgres** (the outputs design's end state).
   Restore, diff, duplicates and merges read `model.json` at a revision, so the
   history would lose its details. That is a product change; see §9.
4. **Only cache per replica.** The replicas share no memory, and a cache keyed on HEAD
   misses after every commit, which is what `last_commits` does today (24.84 s
   measured).

## 5. Design

### 5.1 Tables

One migration file, `<timestamp>_templates.sql`.

`templates`: one row per template folder that has a `model.scad`.

- `template_id` text primary key (a slug, or `builtin:<slug>`), `origin` (`mine`,
  `builtin`).
- `meta` jsonb: `model.json` as read (null when it cannot be read); `meta_error`
  text: why not (the text of `InvalidModelMetaError`); `meta_text` text: the file's
  text when it is not JSON, capped at 64 KiB, kept only so a later reader can match a
  library name in it.
- `name` text: the name the record shows (`meta_from_raw`'s default when blank).
- `version` text: the template's last commit; null without git.
- `updated_at` timestamptz: as today, the newest mtime of `model.scad` and
  `model.json`.
- `has_readme` boolean, `inputs_version` int, `colour_params` text[] (the `// color`
  parameters of `model.scad`, for `part_print_settings`'s check).
- `files` jsonb: name → size for `thumbnail.png`, each `media/` file and, for a
  built-in, each `builtin-media/<slug>/` file. The media views' `size`, `missing` and
  poster come from it.
- `cover` jsonb: the template's own cover (`file`, `content_type`, `sha256`), or null.
- `files_stamp` text: a sha256 of the sorted name, size and `mtime_ns` of the files
  above, as `library/output_copy.py` `files_stamp` does for outputs.
- `refreshed_at` timestamptz default `clock_timestamp()`, `refreshed_head` text (the
  HEAD the row was read at).

`template_copy_runs`: one row per copy run, as `output_copy_runs` (#7360):
`started_at`, `finished_at` (set only when the walk saw every folder), `incomplete`,
`added`, `refreshed`, `dropped`, `unreadable` (jsonb, the templates whose
`model.json` could not be read).

`previous_version` and `ui_version` are not stored. They are per-record git reads
today, they are `None` in a listing, and a stored copy would need its own repair.

### 5.2 Writers: the refresh under the write lock

One routine, `TemplateRows.refresh(ids)`, reads the templates' files and writes their
rows. It runs only under the models write lock, so its reads see every replica's
writes (§3.2), and no two refreshes of a template interleave. Before it reads a
template, it revalidates that template's directory (`core/paths.py`
`revalidate_dirs`).

- **A change that commits.** In `_landed`, after `git commit` and after
  `_change_of` names the templates the commit touched (`HeadChange.models`),
  `ModelHistory` reads those templates' rows. `_PgHold.record` then writes them in the transaction that upserts
  `models_repo_head` and sends the NOTIFY. So a save adds no Postgres commit, and a
  replica that hears the NOTIFY reads rows that already show the commit. `version` of
  each touched template is the new commit. A touched template whose folder is gone
  (a delete) loses its row.
- **A change that makes no commit** (§3.4, with git present). `ModelHistory.commit`
  and the built-in media writes (`_overlay_change`) refresh the ids they were given,
  still under the lock, in a transaction of their own. These changes are rare.
- **No git.** `Catalogue.notify_change` refreshes the ids. There is no lock and no
  second replica (git is missing only in development and some tests).
- **A failed refresh** (Postgres down, an NFS error) is logged with the template ids
  and counted in a metric. It does not fail the write: the files and the commit are
  already there. The next copy run repairs the row (§5.3).

The flows repository shares `PgRepoCoordinator` and writes no rows: the hook is the
models history's own.

### 5.3 The copy: fill at start, repair in every sweep

`library/template_copy.py`, as `library/output_copy.py` (#7360):

- It runs as a task at each API start and as the housekeeping sweep
  `housekeeping_copy_templates` (`TEMPLATE_COPY_SWEEP` behind `workflow.patched`, with
  a replay history), under an advisory lock, so one replica copies at a time.
- Outside the write lock, it walks the folders (as `slugs()` does today), stats each
  template's files for a `files_stamp`, and reads every template's last commit with
  one `last_commits()`. A template needs a refresh when it has no row, its stamp
  differs, or its `version` differs.
- It refreshes those templates with §5.2's routine, under the write lock, a few at a
  time, so a save waits for at most one small batch.
- Under the lock, it drops each row whose folder has no `model.scad`.
- A listing or stat error (ESTALE, EIO) makes the run `incomplete`: it drops nothing
  and sets no `finished_at`.
- `GET /api/v1/model-records/copy-status` returns the last run (not under `/models/`,
  where `copy-status` would read as a slug).

What this catches: writes by an old pod during a roll-out or after a rollback, a
failed refresh, a commit or a file edit made by hand on the volume, and a restored
volume. The first run fills every row.

### 5.4 Readers

The rows are the answer once a copy run has finished (`template_copy_runs.finished_at`
set), as `OutputRows.copied` does for outputs (#7360, design §6.1). Before that, each
reader walks the files as today.

| Reader | Today | From rows |
|---|---|---|
| `slugs()` | `_templates_in` over both folders | `SELECT template_id`, mine then built-ins, each sorted |
| `list_models()` | `_record` per template + `last_commits` | rows + `items_for` + `covers_for` + output covers + previews: a fixed number of queries |
| `record(slug)` | `_record` | the row; `version_pair` and `ui_version` stay git reads |
| `X-Total-Count`, facets `total` | the walk | rows; `meta_error` rows count only in `X-Total-Count`, as today |
| `upstream_state` | `exists` + the walk's revision | the upstream's row |
| `library_pin_index` | every `model.json` | rows' `meta->'libraries'` |
| `library_associations` (names, `mesh_source`) | `read_raw_meta` per template | rows |
| activity facets, print-import suggestions, outputs `?tag=` | `list_models` | rows (no media or cover work: name and tags only) |
| previews `due()` | `slugs()` | rows |
| `print_settings`, `part_print_settings` | `model.json` and `model.scad` per slice | `meta` and `colour_params` |
| `_require_name_free`, `duplicates_of` | every `model.json` | rows |

`library_users` and `library_pins` keep reading the files (§7.4).

The cover fallback to an output reads the `outputs` rows. Until #7360 merges it
reads the output folders as today. A list is NFS-free only with both.

### 5.5 The thumbnail

- The route takes existence from the row, not a stat.
- The `ETag` is known without reading bytes: the row's `cover.sha256` for the
  template's own cover (the same value as today's hash of the bytes, so clients' copies
  stay valid), `"output-<id>"` for an output's plate image (an output's 3MF never
  changes after its create), and `"preview-<preview_id>"` for the default-render
  preview (`preview_id` changes with each render). A matching `If-None-Match` is a 304
  with no file read.
- A 200 reads the image as today. `Cache-Control` stays `no-cache`.

### 5.6 Phases

1. **Rows** (one PR). The migration, `TemplateRows`, the refresh under the write lock
   (§5.2), the copy at start and in the sweep (§5.3), and the copy-status route.
   Readers do not change. The rows are written and checked on prod while every list
   still walks.
2. **Readers** (one PR, or two: the lists, then the thumbnail). Every reader in §5.4
   and §5.5 reads rows once a copy run has finished.
3. **Cut-over** (one PR). After phase 2 has run on prod for a week with no repair
   (§8), remove each reader's file fallback. The walk then lives only in the copy.
   Nothing on NFS is removed or stops being written.

### 5.7 Tests

- The refresh: each writer (`update`, `pin_library`, `write_file`, `update_files`,
  `merge_upstream`, `duplicate`, `create`, `delete`, media add of an image and of a
  video, a built-in's added media, `sync_builtins`, a restore) leaves the row equal to
  what the walk builds for that template. One table-driven test.
- The HEAD transaction: the rows are written in a savepoint in it. A row write that
  fails rolls back the savepoint only: the HEAD and its NOTIFY are still recorded, the
  failure is logged with the template ids and counted, the commit stands, and the next
  copy run repairs the rows (#7317, Task 3 review B-I1).
- The copy: a fixture tree fills the rows; a second run changes nothing; a hand edit
  of `model.json`, a commit made outside the catalogue and a deleted folder are each
  repaired; an ESTALE listing makes the run incomplete and drops nothing.
- Readers: `GET /models` (with and without filters and pages), `/model-facets`, and the
  thumbnail (200, 304 for each source) answer the same before and after phase 2.
- A test that `list_models` makes no file system call once a run has finished.
- `test_housekeeping_replay.py` with a history from before the patch.

## 6. Constraints

- Only the API holds SQL. The rows are written and read in the API process; no
  worker gains a database client or a route.
- Migrations: one new file per change, `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`.
- Prod commit latency is 1–2 s: a committing save writes its rows in the HEAD's
  existing transaction (§5.2).
- `docs/architecture/data.md`, `docs/features/catalogue-and-models.md` and
  `backend/CLAUDE.md` change with phase 1 (the table) and phase 3 (no fallback).

## 7. Decisions (the author's; the owner may change them)

1. **Git stays the record; the row is derived.** A row is only ever written from the
   files by §5.2's routine. Any row can be rebuilt from the tree, and dropping the
   table loses nothing. This is where the outputs pattern does not fit: outputs moved
   their record into Postgres and leave the NFS folder (outputs design §4.5), but a
   template's history, restore, diff, duplicates and merges need `model.json` in git
   (§4.3).
2. **One refresh routine, under the write lock, in the HEAD's transaction when there
   is a commit.** It catches every writer, including ones that do not touch
   `model.json` through `write_raw_meta` and a restore through the history (§4.2). It
   adds no Postgres commit to a save. Reading under the lock means no stale NFS read
   can overwrite a newer row, so the copy needs no clock mark (unlike #7360's
   `UPDATE … WHERE copied_at < mark`, which guards lock-free writes).
3. **A failed refresh does not fail the write.** Unlike outputs (design §6.6, where
   the row is the record), the files and the commit are the record here and cannot
   be rolled back once the commit lands (§3.3). The copy run repairs the row.
4. **The checkout guards keep reading files.** `library_users` and `library_pins`
   decide a removal that the next render would miss (§3.8). They are rare (8 calls
   in 72 hours) and must count an edit that a failed refresh left out of the rows.
5. **`version` is stored; `previous_version` and `ui_version` are not.** `version` is
   what makes a listing walk git (24.84 s measured). The other two are read for one
   record, per request, as today.
6. **The fallback ends with the first finished copy run**, as for outputs (#7360).
   After that, a template with no row is not listed; the next sweep adds a row that a
   failed refresh missed.
7. **The thumbnail's `ETag` names the source.** Only the template's own cover keeps a
   hash of its bytes, taken once at the refresh. Output and preview images are
   immutable under their ids. Clients fetch an output or preview cover once more
   after the deploy.

## 8. Roll-out

- Phase 1: deploy; check that the API pods run the new image (`kubectl -n bambuddy
  get pods -o jsonpath` of the images); read `GET /api/v1/model-records/copy-status`
  for a finished, complete run that started after the last old pod went. That run
  wrote every row from the files; §5.7's table-driven test is what shows that a row
  equals the walk's record.
- Watch the copy runs for a week: a run's `refreshed` count should be only the writes
  of old pods during a roll-out. Any other repair is a writer that §5.2 misses: fix it
  before phase 2.
- Phase 2: deploy and time `GET /models`, `/model-facets` and a thumbnail 304 again
  against §1.
- Rollback at any phase: an older build walks the files and ignores the rows. Its
  writes do not refresh rows; the new build's first copy run at start repairs them.

## 9. For the owner

- **Should a template's details move out of git into Postgres for good?** (§4.3.)
  Not needed for the speed goal, and this design does not do it. It would end the
  history of details edits, and restore and merge would change. Asked only because
  it is a product change that cannot be undone easily; nothing here depends on the
  answer.

## 10. Risks

- **Slower saves.** The refresh reads the touched templates' files under the write
  lock. The writer has just written most of them, so most reads hit the NFS cache.
  A span `templates.refresh` measures it in phase 1; if it costs more than 0.5 s at
  p90, read only the files the commit changed.
- **Stale rows between sweeps** after a failed refresh, an old pod's write or a hand
  edit. Bounded by the sweeps' Schedule (`SCADBUDDY_ASSET_SWEEP_INTERVAL`); with the
  sweeps off, only the start's run repairs.
- **A writer outside `ModelHistory`'s lock.** Today every writer is under it (§3.1,
  §3.4). The copy's `refreshed` count shows a new one.
- **The copy holds the lock in batches.** A save can wait for one batch. The first
  run refreshes every template; it runs in small batches at start.
- **Rows and NFS disagree after a volume restore.** The copy compares stamps and
  versions with the tree, so the first run after the restore rewrites the rows.

## 11. Left to the builder

- Column names and jsonb shapes beyond §5.1.
- The copy's batch size and how it reports progress.
- How `_PgHold.record` takes the extra writes (a callable from `ModelHistory`, or rows
  passed in the `HeadChange`).
- PR boundaries inside each phase.
