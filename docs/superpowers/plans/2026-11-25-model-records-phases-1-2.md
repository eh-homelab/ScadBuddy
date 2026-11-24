# Model records in Postgres (phases 1 and 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A list of models costs SQL reads and no file operation: each template has a derived `templates` row, written under the models write lock, and the readers use the rows once a copy run has finished (#7317, epic #7289).

**Architecture:** `model.json` in git stays the record. Phase 1 adds the `templates` and `template_copy_runs` tables, a builder that reads one template's files into a `TemplateRow`, a hook in `ModelHistory` that writes the touched templates' rows in the transaction that records the new HEAD (or in a transaction of its own when no commit lands), and a reconciling copy at every API start and in every housekeeping sweep. Readers do not change in phase 1. Phase 2 moves every reader in spec §5.4 and §5.5 to the rows, with the file walk as the fallback until the first finished copy run.

**Tech Stack:** Python 3.12, psycopg 3 + psycopg_pool, pydantic, FastAPI, Temporal (`workflow.patched`), git, pytest with the repo's Postgres fixtures.

**Spec:** `docs/superpowers/specs/2026-11-25-model-records-in-postgres-design.md` (§3, §5.1–§5.7, §7, §8). The pattern is the outputs phase 1 plan, `docs/superpowers/plans/2026-11-23-output-records-phase-1.md`, as built in PR #7360.

## Global Constraints

- Only the API process holds SQL. Every row read and write here runs in the API (root `CLAUDE.md`, "Database access (#5295)"). No worker gains a client or a route.
- Migrations: one NEW file, `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_templates.sql`, slug `[a-z0-9_]`. Never edit a merged one (root `CLAUDE.md`, "Migrations (#491)").
- A save that commits adds no Postgres transaction: its rows go in the transaction that upserts `models_repo_head` and sends the NOTIFY (spec §5.2, §6). Prod commit latency is 1–2 s.
- A failed refresh never fails the write. It is logged with the template ids and counted (spec §7.3).
- No change to a `ModelRecord` field, a template id, a URL or a response shape (spec §2 anti-goals).
- Source files, `ui/`, `pipeline/`, media bytes and history stay in the `models/` repository. `exists()` does not change. Filtering and sorting stay in Python (`select_models`, `sort_models`, `tag_counts`).
- `library_users` and `library_pins` keep reading the files in both phases (spec §7.4).
- A change to `Housekeeping.run` goes behind `workflow.patched`, with a recorded replay history (`workflows/housekeeping.py` docstring). A new periodic pass is an activity in `SWEEPS`, never a loop in the API (`backend/CLAUDE.md`).
- Docs and comments in Simplified Technical English. No counts in docs.
- Run backend commands as `backend/CLAUDE.md` says: `uv run --frozen` ruff, mypy and pytest; each parallel run uses its own Postgres database (`CLAUDE.local.md`).

## Plan decisions (the spec left these to the builder, §11)

1. `RepoHold.record(change, also=None)`: `also` is a callable that `_PgHold.record` runs on its own connection inside the HEAD transaction. `GitRepo._landed` asks `self._also_record(change)` for it; only `ModelHistory` answers one. The callable only runs SQL with values prepared before the transaction. It runs in a savepoint: whatever it raises rolls back the savepoint only, and the HEAD and its NOTIFY still commit (revised after the Task 3 review, B-I1).
2. The hook is attached (`history.template_rows = …`) in `_start_render` right after `projection.open()` (which applies the migrations) and before `_prepare_catalogue`. So `ensure_repo`'s boot commit, which runs before the migrations, writes no rows. The start copy fills them.
3. With no coordinator (`_hold` is None: tests, one process), the rows of a commit are written in a transaction of their own, still under the lock (`GitRepo._landed_alone`).
4. A change with no commit (`ModelHistory.commit` or `restore` answers `None`, or raises) refreshes the ids of its paths under the lock. The path `_builtin` alone maps to no id: the built-in sync at boot is followed by the start copy, and a built-in's media writes refresh their own id (`_overlay_change`).
5. The no-commit refresh reads each id's version with `history.last_commit(model_path(id))`. The copy reads one `last_commits()` walk outside the lock; under the lock, when HEAD moved since the walk, it corrects the batch with `ModelHistory.last_commits_since(base, ids)` (one `git log base..HEAD`).
6. `files_stamp` covers more than the spec's `files` set, so the copy catches every change a row derives from: (name, size, `mtime_ns`) of `model.scad`, `model.json`, `README.md`, `thumbnail.png`, each entry of `media/`, each top-level file of `pipeline/`, each entry of `builtin-media/<slug>/` for a built-in, and the JSON of the template's `template_media` rows and its chosen cover. Without the last two, a reorder by an old pod (rows only, no file, no commit) would never be repaired.
7. Shapes: `files` keys are `thumbnail.png`, `media/<file>` and `builtin-media/<file>`. `cover` is `{"path": <relative to DataPaths.root>, "content_type", "sha256"}`. `colour_params` is NULL when `model.scad` cannot be read (so `part_print_settings` still only warns then). `meta` holds the dict `read_raw_meta` answers, also when `meta_from_raw` refuses it; then `meta_error` is set too.
8. The copy refreshes in batches of 8 under the write lock, and prunes `template_copy_runs` older than 30 days at each run's start.
9. `TEMPLATE_COPY_SWEEP = "housekeeping_copy_templates"` behind `TEMPLATE_COPY_PATCH = "housekeeping-copy-templates"`, directly after `GIT_SWEEP` in `SWEEPS`, not in `PRUNE_SWEEPS`, timeout 15 minutes. This place does not depend on whether #7360 (`OUTPUT_COPY_SWEEP`) merges first.
10. The copy-status route lives in `api/models.py`; the agent's `NOT_A_TOOL` list names it.
11. Phase 2: the write methods (`create`, `update`, …) keep building the record they answer from the files, so a write's own response shows the write even after a failed refresh. `record(slug)` (the `GET` routes) reads the row, and falls back to the files when the row is missing but `exists(slug)` is true (a failed refresh, an old pod's create). Lists do not fall back once a run has finished (spec §7.6).
12. Phase 2: a thumbnail 200 sends the `ETag` of what it read (the hash of the cover bytes, or the output or preview id it read), never one computed before the read, so a cover that moves between the check and the read is never cached under the wrong tag.
13. Phase 2 starts after #7360 has merged and phase 1 has run a week on prod with no repair (spec §8). Its output covers use `OutputRows` once `outputs.rows.copied()`, else `plate_cover_output` per template as today.
14. `slugs()` from rows sorts `template_id COLLATE "C"`, so the order equals Python's `sorted` of the folder names.

## Every place that writes `model.json` or moves the models HEAD

All commits pass `GitRepo._commit_locked` → `_landed` (rows in the HEAD transaction, Task 3). A change with no commit passes `ModelHistory.commit`'s or `restore`'s no-commit path (Task 3), `_overlay_change` (Task 4) or, without git, `Catalogue.notify_change` (Task 4). The table-driven test in Task 4 calls each one.

| Writer (`library/catalogue.py` unless named) | Path to the history | Row refreshed by |
|---|---|---|
| `create` | `history.commit("Add …")` | HEAD transaction |
| `duplicate` | `media_store.replace`, then `_commit` | HEAD transaction |
| `update`, `pin_library`, `unpin_library` | `_commit_change` (`write_raw_meta`) | HEAD transaction |
| `write_thumbnail`, `delete_thumbnail` | `_commit` / `_commit_change` | HEAD transaction |
| `add_media` (image), `remove_media` of mine | `_commit_change` | HEAD transaction |
| `add_media` (video only), `set_caption`, `reorder`, `set_cover` of mine | `_commit_change`, nothing staged | no-commit path |
| built-in `add_media`, `set_caption`, `reorder`, `remove_media`, `set_cover` | `_overlay_change` (lock, no commit) | `_overlay_change` |
| `write_readme`, `delete_readme` | `history.commit(prepare, rollback)` / `_commit` | HEAD transaction |
| `write_source`, `_write_edit`, the merge `resolve` | `history.commit(prepare, rollback)` / `_commit_change` | HEAD transaction |
| `write_file`, `update_files` | `_commit_change` / `history.commit(prepare, rollback)` | HEAD transaction |
| `merge_upstream`, `dismiss_upstream`, `detach_upstream` (`_write_upstream`, `_advance_base`) | `_commit_change` | HEAD transaction |
| `delete` | `_commit("Delete …")` (folder gone) | HEAD transaction drops the row |
| `sync_builtins` (and `_drop_builtin_media`) | `_commit(SYNC_MESSAGE, "_builtin")` | HEAD transaction (each touched `builtin:<slug>`); no commit: the start copy |
| `link_seeded` | `_commit_change(LINK_MESSAGE)` | HEAD transaction |
| `ModelHistory.restore` (`api/versions.py` `restore_run`) | `_restore_locked` | HEAD transaction; no commit: no-commit path |
| `GitRepo.ensure_repo` (boot recovery commit) | `_commit_locked(".")` before the hook is attached | the start copy |
| `GitRepo._settle(adopt=True)` (boot) | `hold.record(HeadChange(head))`, no models | the start copy |
| a failed commit, a lock-wait failure in `_commit` | logged | no-commit path (failure inside the lock) or the next copy run |
| no git (`history.available` false) | `notify_change` | `notify_change` refresh |
| an old pod, a person on the volume, a restored volume | none | the copy (start and sweep) |

`GitRepo.maintain` (gc) and `push` move no HEAD. The flows repository writes no rows: the hook is `ModelHistory`'s only.

## Review Focus

1. A save on one replica, then a list on another at once: the other reads rows that already show the commit, because they commit with the HEAD, before the NOTIFY (`test_rows_commit_with_the_head_and_before_the_notify`, Task 3).
2. A commit lands between the copy's walk and its batch: the copy must not write the older version over the newer row (`test_copy_does_not_undo_a_commit_made_after_its_walk`, Task 5).
3. A video-only media add, a caption, a reorder: no commit lands, and the row's `files` and `cover` still change (rows of the writers table, Task 4; `test_stamp_changes_when_media_rows_reorder`, Task 2).
4. A `models/` listing that fails with ESTALE: the run is `incomplete`, drops nothing and has no `finished_at`, so the fallback stays (`test_copy_listing_error_is_incomplete_and_drops_nothing`, Task 5).
5. A `model.json` that is not JSON: the row has `meta` NULL, `meta_error` and `meta_text`; `X-Total-Count` counts it, the list and `/model-facets` leave it out, the pin index skips it, copy-status names it (Task 2, Task 5, `test_unreadable_meta_counts_but_is_not_listed`, Task 7).

---

## Phase 1: rows (one PR, readers unchanged)

Branch `feat/model-records-7317` from `origin/main`. Open the PR `feat(models): template rows in Postgres, phase 1 (#7317)` after Task 6, body `Part of #7289`, `Fixes` none (phase 2 closes #7317).

### Task 1: The tables and `TemplateRows`, the row store

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_templates.sql`
- Create: `backend/scadbuddy/library/template_rows.py`
- Test: `backend/tests/test_template_rows.py`

**Interfaces:**
- Produces: table `templates` (`template_id text PRIMARY KEY`, `origin text NOT NULL CHECK (origin IN ('mine','builtin'))`, `meta jsonb`, `meta_error text`, `meta_text text`, `name text NOT NULL`, `version text`, `updated_at timestamptz NOT NULL`, `has_readme boolean NOT NULL`, `inputs_version int NOT NULL DEFAULT 0`, `colour_params text[]`, `files jsonb NOT NULL DEFAULT '{}'`, `cover jsonb`, `files_stamp text NOT NULL`, `refreshed_at timestamptz NOT NULL DEFAULT clock_timestamp()`, `refreshed_head text`). Table `template_copy_runs` (`id bigserial PRIMARY KEY`, `started_at timestamptz NOT NULL DEFAULT clock_timestamp()`, `finished_at timestamptz`, `incomplete boolean NOT NULL DEFAULT false`, `added int NOT NULL DEFAULT 0`, `refreshed int NOT NULL DEFAULT 0`, `dropped int NOT NULL DEFAULT 0`, `unreadable jsonb NOT NULL DEFAULT '[]'`). The file opens with a comment that cites spec §5.1 and #7317.
- Produces, in `template_rows.py`:
  - `@dataclass(frozen=True) class TemplateRow` with one field per column except `refreshed_at`: `template_id: str`, `origin: Literal["mine", "builtin"]`, `meta: dict[str, Any] | None`, `meta_error: str | None`, `meta_text: str | None`, `name: str`, `version: str | None`, `updated_at: datetime`, `has_readme: bool`, `inputs_version: int`, `colour_params: list[str] | None`, `files: dict[str, int]`, `cover: dict[str, str] | None`, `files_stamp: str`, `refreshed_head: str | None`.
  - `META_TEXT_MAX = 64 * 1024`.
  - `write_rows(conn: Connection[Any], rows: Sequence[TemplateRow], gone: Sequence[str]) -> None`: one `INSERT … ON CONFLICT (template_id) DO UPDATE SET …, refreshed_at = clock_timestamp()` (executemany) and one `DELETE … WHERE template_id = ANY(%s)`, on the caller's connection and transaction. Module-level, so `_PgHold`'s tuple-row connection can run it.
  - `class TemplateRows(pool: ConnectionPool[Connection[DictRow]])`: `put(rows, gone) -> None` (`write_rows` in one transaction of its own); `get(template_id) -> TemplateRow | None`; `all() -> list[TemplateRow]` (mine first, then built-ins, each by `template_id COLLATE "C"`); `ids() -> list[str]` (same order); `stamps() -> dict[str, tuple[str, str | None]]` (stamp, version); `start_run() -> int` (also deletes runs that started more than 30 days ago); `finish_run(run_id, *, added, refreshed, dropped, unreadable: list[dict[str, str]]) -> None`; `end_incomplete_run(run_id, *, …same…) -> None` (`incomplete` true, no `finished_at`); `last_run() -> dict[str, Any] | None`; `copied() -> bool` (a run with `finished_at` exists; once true, cached true for the object's life).

- [ ] **Step 1: Write the failing tests** in `test_template_rows.py` (`requires_postgres`, a `TemplateRows` on the migrated `pg_pool`): `test_put_then_get_round_trips_every_column`, `test_put_replaces_a_row_and_moves_refreshed_at`, `test_put_drops_the_gone_ids`, `test_all_and_ids_are_mine_then_builtins_in_c_order` (ids `"b"`, `"B"`, `"_x"`, `"é"`, `"builtin:a"`; expect `["B", "_x", "b", "é", "builtin:a"]`, which is `sorted()` of the mine ids, then the built-ins), `test_stamps_answer_stamp_and_version`, `test_copied_false_until_a_run_finishes_then_stays_true`, `test_incomplete_run_does_not_count_as_copied`, `test_start_run_prunes_runs_older_than_30_days`, `test_last_run_answers_the_newest`.
- [ ] **Step 2: Run them, expect FAIL** (`uv run --frozen pytest tests/test_template_rows.py`: relation or module missing).
- [ ] **Step 3: Write the migration and `template_rows.py`.** Synchronous, as `output_index.py`; module docstring cites spec §5.1 and says `model.json` in git is the record and any row can be rebuilt from the tree (§7.1).
- [ ] **Step 4: Run them, expect PASS.** Run the migration-ledger tests `backend/CLAUDE.md` names, ruff and mypy on the new files.
- [ ] **Step 5: Commit** `feat(models): the templates and template_copy_runs tables, TemplateRows (#7317)`.

### Task 2: The builder: one template's files to a `TemplateRow`

**Files:**
- Modify: `backend/scadbuddy/library/catalogue.py` (new `template_row`, and `_media_listing`/`_views`/`_item_views` take the sizes they use from a parameter so Task 7 can reuse them)
- Modify: `backend/scadbuddy/library/template_rows.py` (`template_stamp`)
- Test: `backend/tests/test_template_row_builder.py`

**Interfaces:**
- Consumes: Task 1's `TemplateRow`, `META_TEXT_MAX`.
- Produces:
  - `template_stamp(paths: DataPaths, template_id: str, media: Sequence[MediaItem], cover: str | None) -> str`: sha256 hex over the sorted `(name, size, mtime_ns)` of plan decision 6's files plus `json.dumps([item.model_dump(mode="json") for item in media], sort_keys=True)` and `cover`. A missing file or folder is left out; any other `OSError` raises.
  - `Catalogue.template_row(template_id: str, *, version: str | None, head: str | None) -> TemplateRow | None`: None when the folder has no `model.scad` (after `revalidate_dirs` of the template's folder, spec §5.2). It reads `model.json` once (`read_raw_meta`; `InvalidModelMetaError` → `meta` NULL, `meta_error`, `meta_text` = the file's text cut to `META_TEXT_MAX`), parses it with `_meta` (a refusal sets `meta_error` and keeps `meta`), and reads the media rows (`media_store.items`, a built-in's `media_store.cover`) once. `name` = the parsed name, or `meta_from_raw`'s default for the id when it does not parse. `updated_at`, `has_readme`, `inputs_version` as `_record` computes them. `colour_params` = the `// color` names of `find_annotations(model.scad)`, NULL when it cannot be read. `files` = the sizes of plan decision 7's keys. `cover` = `_cover`'s choice with the sha256 of its bytes. `files_stamp` = `template_stamp(...)` with the same media rows.
  - `_media_listing(..., sizes: Callable[[Path], int | None] = _file_size)` and the same `sizes` parameter on `_views`, `_item_views`: today's behaviour by default.

- [ ] **Step 1: Write the failing tests** (`requires_git`, `MemoryMediaStore` as `tests/test_history_replicas.py` does): `test_row_matches_the_record_of_a_plain_template` (name, `updated_at`, `has_readme`, `inputs_version`, `version` equal `catalogue.record(slug)`'s), `test_row_files_match_the_media_views` (image, video without poster, a missing file: `files` sizes equal each `MediaView.size`, missing ones absent), `test_row_cover_hash_is_the_thumbnail_hash` (`cover["sha256"] == sha256(catalogue.thumbnail(slug)[0])` for a media image, a legacy `thumbnail.png`, a built-in with an added image chosen as cover), `test_row_of_meta_that_is_not_json` (`meta is None`, `meta_error` starts `"not JSON"`, `meta_text` is the text), `test_row_of_meta_that_does_not_parse_keeps_meta`, `test_row_colour_params_are_the_color_annotations`, `test_no_model_scad_is_no_row`, `test_stamp_is_stable_when_nothing_changes`, `test_stamp_changes_when_model_json_is_touched`, `test_stamp_changes_when_a_media_file_is_added`, `test_stamp_changes_when_media_rows_reorder`, `test_stamp_raises_on_an_os_error_other_than_not_found` (monkeypatch `os.stat` on one path to raise `OSError(errno.ESTALE)`).
- [ ] **Step 2: Run them, expect FAIL** (`template_row` missing).
- [ ] **Step 3: Implement** `template_stamp` and `Catalogue.template_row`. Reuse `_meta`, `_media_listing`, `_cover`, `_inputs_version`; add no second copy of their rules.
- [ ] **Step 4: Run them and `tests/test_media_store.py`, `tests/api/test_models.py`, `tests/api/test_model_images.py`, expect PASS.**
- [ ] **Step 5: Commit** `feat(models): build a template's row from its files (#7317)`.

### Task 3: The HEAD transaction and the no-commit path in the history

**Files:**
- Modify: `backend/scadbuddy/core/gitrepo.py` (`RepoHold.record`, `GitRepo._landed`, new `_also_record`, `_landed_alone`)
- Modify: `backend/scadbuddy/library/repo_lock.py` (`_PgHold.record` runs `also`)
- Modify: `backend/scadbuddy/library/history.py` (`TemplateRowsHook`, `ModelHistory.template_rows`, overrides, `commit`/`restore` no-commit path, `last_commits_since`)
- Modify: `backend/tests/test_history_replicas.py:243` (`_Unrecordable.record` takes `also`)
- Test: `backend/tests/test_history_rows.py`

**Interfaces:**
- Consumes: Task 1's `write_rows` (the tests' fake hook writes real rows).
- Produces:
  - `RepoHold.record(self, change: HeadChange, also: Callable[[Any], None] | None = None) -> None`. `_PgHold.record` calls `also(self._conn)` inside its transaction, after the HEAD upsert and before `pg_notify`. It runs in a savepoint: any exception from it rolls back the savepoint only, is logged with the commit and the template ids, and the HEAD upsert and `pg_notify` still commit (Task 3 review B-I1, B-M1).
  - `GitRepo._also_record(self, change: HeadChange) -> Callable[[Any], None] | None` (None here) and `GitRepo._landed_alone(self, commit: str) -> None` (no-op here; `_landed` calls it when `_hold` is None).
  - In `history.py`: `class TemplateRowsHook(Protocol)`: `for_commit(self, models: list[str], commit: str) -> Callable[[Any], None] | None` (reads the files; never raises; None on failure) and `refresh_locked(self, models: list[str], *, path: Literal["commit", "no_commit", "no_git"]) -> None` (reads and writes in a transaction of its own; never raises). `ModelHistory.template_rows: TemplateRowsHook | None = None`.
  - `ModelHistory._also_record(change)`: `template_rows.for_commit(change.models, change.commit)` when both are set, else None. `ModelHistory._landed_alone(commit)`: `refresh_locked(self._changed_models(commit), path="commit")`.
  - `ModelHistory.commit` and `ModelHistory.restore`: under the same `_exclusive()`, when the commit answers None or raises, call `template_rows.refresh_locked(_ids_of(paths), path="no_commit")`, then answer or re-raise as today. `_ids_of(paths: Sequence[str]) -> list[str]` maps a slug to itself and `_builtin/<slug>` to `builtin:<slug>`; the bare `_builtin` maps to nothing (plan decision 4).
  - `ModelHistory.last_commits_since(self, base: str, ids: Sequence[str]) -> dict[str, str]`: the newest commit in `base..HEAD` that touched each of `ids`, from one `git log --format=%x1e%H --name-only base..HEAD -- <model paths>`; ids not touched are absent.

- [ ] **Step 1: Write the failing tests** in `test_history_rows.py` (`requires_git`, `requires_postgres`; a `ModelHistory` with `PgRepoCoordinator(pg_conninfo)` on a migrated schema, and a fake `TemplateRowsHook` whose `for_commit` answers `partial(write_rows, rows=[<a TemplateRow for each model>], gone=[])` and whose `refresh_locked` records its calls and whether the lock is held):
  - `test_rows_commit_with_the_head_and_before_the_notify`: LISTEN on `HEAD_CHANNEL` on a second connection; a commit; when the notification arrives, `SELECT version FROM templates WHERE template_id = 'demo'` on that connection answers the new commit, and `models_repo_head.head` is it.
  - `test_a_failing_row_write_still_records_the_head_and_its_notify`: `for_commit` answers a callable that runs invalid SQL, or one that raises a non-psycopg error; `commit` answers the new sha; `models_repo_head` holds the new head; no `templates` row; the notification is sent.
  - `test_a_hook_that_cannot_read_still_records_the_head`: `for_commit` answers None; the HEAD is recorded.
  - `test_a_commit_with_nothing_staged_refreshes_its_ids_under_the_lock`: `refresh_locked(["demo"], path="no_commit")`, called while `history._hold is not None`.
  - `test_a_failed_commit_refreshes_its_ids_then_raises` (make `_commit_locked` raise `GitError`).
  - `test_an_identical_restore_refreshes_its_id`.
  - `test_the_builtin_dir_alone_refreshes_nothing`.
  - `test_without_a_coordinator_the_rows_follow_the_commit` (`coordinator=None`: `refresh_locked(["demo"], path="commit")` after the commit).
  - `test_last_commits_since_names_only_the_later_commits`.
- [ ] **Step 2: Run them, expect FAIL.**
- [ ] **Step 3: Implement** the interfaces above. Fix `_Unrecordable.record` in `test_history_replicas.py` to take `also=None`.
- [ ] **Step 4: Run them and `tests/test_history.py`, `tests/test_history_replicas.py`, `tests/test_models_head_signal.py`, the flows repository tests (`grep -rl "flows_coordinator\|FlowRepository" tests`), expect PASS.**
- [ ] **Step 5: Commit** `feat(models): write template rows with the models HEAD, under its lock (#7317)`.

### Task 4: `TemplateRefresh`: every writer refreshes its rows

**Files:**
- Create: `backend/scadbuddy/library/template_refresh.py`
- Modify: `backend/scadbuddy/library/catalogue.py` (`Catalogue.refresh: TemplateRefresh | None`; `notify_change` refreshes when there is no history; `_overlay_change` refreshes under its lock when there is one)
- Modify: `backend/scadbuddy/core/metrics.py` (`template_refresh_failures`)
- Modify: `backend/scadbuddy/api/deps.py` (build `TemplateRows` and `TemplateRefresh`; set `catalogue.refresh`)
- Modify: `backend/scadbuddy/main.py` (`_start_render`: `state.history.template_rows = state.catalogue.refresh` right after `projection.open()`, plan decision 2)
- Test: `backend/tests/test_template_refresh.py`

**Interfaces:**
- Consumes: Task 1's `TemplateRows`, `write_rows`; Task 2's `Catalogue.template_row`; Task 3's `TemplateRowsHook`.
- Produces: `class TemplateRefresh(catalogue: Catalogue, rows: TemplateRows, on_failure: Callable[[str], None] | None = None)`, a `TemplateRowsHook`:
  - `read(ids: Sequence[str], versions: Mapping[str, str | None], head: str | None) -> tuple[list[TemplateRow], list[str]]`: the rows of the ids that have a `model.scad`, and the ids that are gone. Raises what the reads raise.
  - `for_commit(models, commit)`: `read(models, {id: commit for id in models}, commit)`, then `partial(write_rows, rows=…, gone=…)`. On any exception: log with the ids, `on_failure("commit")`, answer None.
  - `refresh_locked(models, *, path)`: versions from `history.last_commit(model_path(id))` (None without git), `head` from `history.head()`, then `rows.put`. On any exception: log, `on_failure(path)`.
  - Each read runs in a span `templates.refresh` with `scadbuddy.templates` (the count) and `scadbuddy.path`.
- Produces: `Metrics.template_refresh_failures = Counter("scadbuddy_template_refresh_failures_total", …, ["path"])`, API role only (not in `ROLE_SERIES`).

- [ ] **Step 1: Write the failing table-driven test** `test_every_writer_leaves_the_row_equal_to_the_walk` in `test_template_refresh.py` (`requires_git`, `requires_postgres`; a real `PgRepoCoordinator`, `PostgresMediaStore` and `TemplateRows` on one migrated schema, the hook attached). One parameter per row of the writer table above: `update`, `pin_library`, `unpin_library`, `write_file`, `update_files`, `write_source`, `write_readme`, `delete_readme`, `write_thumbnail`, `delete_thumbnail`, `add_media` of an image, `add_media` of a video, `set_caption`, `reorder`, `remove_media`, `set_cover`, a built-in's `add_media` and `set_cover`, `merge_upstream`, `dismiss_upstream`, `detach_upstream`, `duplicate`, `create`, `delete`, `sync_builtins`, `link_seeded`, `history.restore`. After each call:

```python
expected = catalogue.template_row(model_id, version=catalogue.version(model_id), head=history.head())
assert _strip(rows.get(model_id)) == _strip(expected)   # _strip: refreshed_head dropped
# delete: rows.get(model_id) is None
```

  Also `test_without_git_create_update_delete_refresh_through_notify_change` (history `None`), `test_a_failed_refresh_is_logged_counted_and_the_write_stands` (`rows.put` raises `psycopg.OperationalError`: the write answers, `on_failure` got `"no_commit"`), and `test_the_hook_is_attached_after_the_migrations` (an app start in `tests/api`: `state.history.template_rows is state.catalogue.refresh`).
- [ ] **Step 2: Run it, expect FAIL.**
- [ ] **Step 3: Implement** `template_refresh.py`, the two catalogue calls, the counter and the wiring.
- [ ] **Step 4: Run it, Task 3's tests, and the catalogue suites (`tests/api/test_models.py`, `test_model_details.py`, `test_model_files.py`, `test_model_images.py`, `test_versions.py`, `tests/test_media_store.py`, `tests/test_library_seed.py`), expect PASS.**
- [ ] **Step 5: Commit** `feat(models): every catalogue write refreshes its template rows (#7317)`.

### Task 5: The copy at every API start and in every housekeeping sweep

**Files:**
- Create: `backend/scadbuddy/library/template_copy.py`
- Modify: `backend/scadbuddy/main.py` (`_copy_templates_once`, `_copy_templates_logged`, a start task `copy templates` after `_start_render`, joined in `_join_background`; the activity `TEMPLATE_COPY_SWEEP` in `_housekeeping_activities`, as #7360's `copy_outputs_to_rows`)
- Modify: `backend/scadbuddy/workflows/housekeeping.py` (`TEMPLATE_COPY_SWEEP`, `TEMPLATE_COPY_PATCH`, `TEMPLATE_COPY_TIMEOUT = timedelta(minutes=15)`, `sweep_timeout`, the `workflow.patched` gate in `run`; plan decision 9)
- Modify: `backend/tests/test_housekeeping.py` (timeouts map; place test), `backend/tests/test_housekeeping_replay.py` (`template_copy` in the list, docstring, the recording test renamed `test_record_template_copy`), `backend/tests/test_library_worker_task.py` (`copy_templates` broken in `test_a_failing_sweep_fails_its_activity`), `backend/tests/api/conftest.py` (autouse `app_template_copy_off` unless marked `template_copy`), `backend/pyproject.toml` (the `template_copy` marker)
- Create: `backend/tests/fixtures/housekeeping_histories/template_copy.json` (recorded with `SCADBUDDY_RECORD_HISTORIES=1`)
- Test: `backend/tests/test_template_copy.py`

**Interfaces:**
- Consumes: Task 1's `TemplateRows`; Task 2's `template_stamp`; Task 3's `last_commits_since`; Task 4's `TemplateRefresh.read`.
- Produces: `copy_templates(catalogue: Catalogue, rows: TemplateRows, refresh: TemplateRefresh, pool: ConnectionPool[Connection[DictRow]], stop: threading.Event | None = None) -> TemplateCopyResult | None` (None when another replica holds the copy's lock); `@dataclass class TemplateCopyResult(added: int, refreshed: int, dropped: int, unreadable: list[dict[str, str]], incomplete: bool)`. `COPY_LOCK = "scadbuddy:templates:copy"`, `BATCH = 8`.

Behaviour (spec §5.3):
- `pg_try_advisory_lock(hashtextextended(COPY_LOCK, 0))` on a connection of its own; not got → log, None. `rows.start_run()`.
- Outside the write lock: list `models/` and `_builtin/` as `slugs()` does; `walk_head = history.head()`; `versions = catalogue.versions()`; one `media_store.items_for(ids)` and `covers_for(builtins)`; `template_stamp` per id. Need a refresh: no row, a different stamp, or a different version.
- Per batch of `BATCH`, under `history.write_lock()` (no lock without git): if `history.head() != walk_head`, overlay `last_commits_since(walk_head, batch)` on the batch's versions; `refresh.read(batch, versions, head)`; `rows.put`. Honour `stop` between batches.
- Then, under the lock: each row id the walk did not see, and for which `catalogue.exists(id)` is false, is dropped.
- A listing or stat error other than not-found, a missing `models/`, or a `models/` that reads empty while rows exist: `end_incomplete_run`, drop nothing. Else `finish_run`. A stop leaves the run without `finished_at`.
- `unreadable` = `{template_id, error}` of each row the run wrote or kept whose `meta_error` is set.

- [ ] **Step 1: Write the failing tests** (`requires_git`, `requires_postgres`, a real coordinator): `test_copy_fills_every_row_and_finishes_the_run`, `test_a_second_run_changes_nothing` (`added == refreshed == dropped == 0`), `test_copy_repairs_a_hand_edit_of_model_json`, `test_copy_repairs_a_commit_made_outside_the_catalogue` (`git commit` in the folder by hand), `test_copy_drops_a_row_whose_folder_is_gone`, `test_copy_does_not_undo_a_commit_made_after_its_walk` (monkeypatch `Catalogue.versions` to answer the walk's versions and then make a commit through the catalogue, so the commit lands after the walk and before the first batch: the row keeps the new version), `test_copy_listing_error_is_incomplete_and_drops_nothing` (`iterdir` of `models/` raises `OSError(errno.ESTALE)`), `test_copy_names_unreadable_templates`, `test_copy_runs_once_under_its_lock`, `test_copy_stops_unfinished_on_stop`, `test_copy_without_git_fills_rows_with_no_version`. In `test_housekeeping.py`: `test_the_template_copy_follows_the_git_sweep_on_the_sweeps_schedule` (`SWEEPS[SWEEPS.index(GIT_SWEEP) + 1] == TEMPLATE_COPY_SWEEP`, not in `PRUNE_SWEEPS`, `sweep_timeout` 15 min).
- [ ] **Step 2: Run them, expect FAIL.**
- [ ] **Step 3: Implement** `template_copy.py`, the start task, the activity, the sweep constants and gate, the api conftest fixture and the marker.
- [ ] **Step 4: Record the replay history**: `SCADBUDDY_RECORD_HISTORIES=1 uv run --frozen pytest tests/test_housekeeping_replay.py -k record`, then run the whole file without the variable (every recorded history replays, `template_copy` too). Run Task 5's tests, `tests/test_housekeeping.py`, `tests/test_library_worker_task.py`; expect PASS.
- [ ] **Step 5: Commit** `feat(models): copy template rows at every start and in every housekeeping sweep (#7317)`.

### Task 6: The copy-status route and the docs

**Files:**
- Modify: `backend/scadbuddy/api/models.py` (`TemplateCopyStatus`, `GET /model-records/copy-status`)
- Modify: `agent/src/tools/coverage.ts` (`NOT_A_TOOL` entry for `GET /api/v1/model-records/copy-status`, as #7360's for the outputs')
- Modify: `docs/architecture/data.md` (the two tables; the row is derived and git is the record), `docs/features/catalogue-and-models.md` (the refresh under the lock, the copy, the route), `backend/CLAUDE.md` (the `library/` entry names `template_rows.py`, `template_refresh.py`, `template_copy.py`, and the sweep)
- Test: `backend/tests/api/test_template_copy_status.py`

**Interfaces:**
- Consumes: `TemplateRows.last_run()`.
- Produces: `GET /api/v1/model-records/copy-status` → `TemplateCopyStatus` (`started_at: datetime`, `finished_at: datetime | None`, `incomplete: bool`, `added: int`, `refreshed: int`, `dropped: int`, `unreadable: list[dict[str, str]]`); 404 with `"no copy of the templates to rows has run"` before any run. Read-only, the same dependencies as `GET /models`.

- [ ] **Step 1: Write the failing tests** `test_copy_status_reports_the_last_run` (marked `template_copy`: the app's start copy runs; poll the route until `finished_at` is set; `added` equals the fixture's template count) and `test_copy_status_404_before_any_run`.
- [ ] **Step 2: Run them, expect FAIL.**
- [ ] **Step 3: Implement** the route, the coverage entry and the docs.
- [ ] **Step 4: Run them; run `cd agent && pnpm exec vitest run test/coverage.test.ts` (after `pnpm gen:api`); run the full backend ruff, ruff format check and mypy. Expect PASS.**
- [ ] **Step 5: Commit** `feat(models): the template copy's status route; docs (#7317)`.

### After phase 1

- Open the PR. After it deploys: check the API pods' images (`kubectl -n bambuddy get pods -o jsonpath` of the images), then read `GET /api/v1/model-records/copy-status` for a finished, complete run that started after the last old pod went (spec §8).
- Watch the runs for a week: `refreshed` should count only old pods' writes during a roll-out. Any other repair names a writer this plan missed: fix it before phase 2. Read the `templates.refresh` span's p90 in Tempo; above 0.5 s, read only the files the commit changed (spec §10).

---

## Phase 2: readers (one PR)

Start only when #7360 has merged and phase 1 has run a week with no repair (plan decision 13). Branch `feat/model-records-readers-7317` from `origin/main`. PR `feat(models): read template rows, phase 2 (#7317)`, body `Fixes #7317`.

Every reader below reads rows when `catalogue.rows is not None and catalogue.rows.copied()` (`Catalogue._from_rows() -> bool`), else the files as today. Tests that need the rows mark the app `template_copy` or call `copy_templates` themselves.

### Task 7: `slugs`, `list_models` and `record` from rows

**Files:**
- Modify: `backend/scadbuddy/library/catalogue.py` (`rows: TemplateRows | None`, `_from_rows`, `slugs`, `list_models`, `record`, a shared `_build_record`, `_fresh_record` for the writes)
- Modify: `backend/scadbuddy/library/previews.py` (`PreviewStore.preview_ids`)
- Modify: `backend/scadbuddy/library/output_rows.py` (`OutputRows.plate_covers_for`)
- Modify: `backend/scadbuddy/api/deps.py` (`catalogue.rows = rows`)
- Test: `backend/tests/api/test_models_from_rows.py`

**Interfaces:**
- Consumes: Task 1's `TemplateRows.all()`, `ids()`, `get()`, `copied()`; Task 2's `sizes` parameter.
- Produces:
  - `Catalogue._build_record(slug: str, raw: dict[str, Any], *, version: str | None, upstream_version: Callable[[str], str | None], upstream_exists: Callable[[str], bool], updated_at: datetime, has_readme: bool, inputs_version: int, media: list[MediaView], media_cover: str | None, thumbnail: ThumbnailOrigin, ui_version: str | None, previous_version: str | None) -> ModelRecord`: today's `_record` body after its reads (the built-in pops of `origin_url` and `upstream` included). `_record` (files) and the rows path both call it.
  - `slugs()` from rows: `rows.ids()`.
  - `list_models(slugs=None)` from rows: `rows.all()` (filtered to `slugs`), one `media_store.items_for`, one `covers_for`, one `PreviewStore.preview_ids(ids) -> dict[str, str]`, and the output covers from `OutputRows.plate_covers_for(slugs: Sequence[str]) -> dict[str, str]` (the oldest output with `has_plate_cover` per slug, one query) once `outputs.rows.copied()`, else `plate_cover_output` per template. Media views use `sizes` from the row's `files`. A row with `meta_error` is left out with the same warning as today. `upstream_state` reads the upstream's row (exists = it has a row; revision = its `version`). No file and no git call.
  - `record(slug)` from rows: the row, with `version_pair` and `ui_version` still read from git. No row: `_record` from the files when `exists(slug)`, with a warning; else `ModelNotFoundError` (plan decision 11).
  - `_fresh_record(slug)`: today's `record` from the files. Every write method that answered `self.record(slug)` answers `self._fresh_record(slug)`.

- [ ] **Step 1: Write the failing tests** in `tests/api/test_models_from_rows.py` (fixture: templates of mine and built-ins, media, a duplicate with an upstream, an output with a plate cover, a preview, a non-JSON `model.json`; one client before the copy, then `copy_templates`, then the same calls): `test_get_models_is_the_same_before_and_after_the_copy` (no params; `limit`/`after` pages; `q`; `tag`; `origin`; `sort=name` and `sort=updated`; bodies and `X-Total-Count` equal), `test_model_facets_are_the_same_before_and_after_the_copy`, `test_get_model_is_the_same_before_and_after_the_copy`, `test_unreadable_meta_counts_but_is_not_listed`, `test_list_models_makes_no_file_or_git_call_after_the_copy` (monkeypatch `Path.stat`, `Path.is_file`, `Path.iterdir`, `Path.read_text`, `Path.read_bytes` and `ModelHistory._run` to raise; `catalogue.list_models()` answers), `test_slugs_order_is_the_walks_order` (ids `"b"`, `"B"`, `"_x"`, `"é"`), `test_a_template_with_no_row_is_not_listed_after_the_copy`, `test_record_of_a_template_with_no_row_falls_back_to_its_files`, `test_a_write_answers_its_change_when_the_refresh_failed` (`rows.put` raises; `PATCH` answers the new name).
- [ ] **Step 2: Run them, expect FAIL.**
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run them, Task 4's table test and `tests/api/test_models.py`, `test_model_details.py`, `test_model_images.py`, `test_versions.py`, `tests/test_previews.py`; expect PASS.**
- [ ] **Step 5: Commit** `feat(models): list and read models from template rows (#7317)`.

### Task 8: The other readers

**Files:**
- Modify: `backend/scadbuddy/library/catalogue.py` (`library_pin_index`, `print_settings`, `part_print_settings`, `_require_name_free`, `duplicates_of`, new `raw_metas`, `list_summaries`)
- Modify: `backend/scadbuddy/api/library_associations.py` (`_raw_meta` and the walk in `_implied_for_file` use `raw_metas`)
- Modify: `backend/scadbuddy/api/activity.py:350`, `backend/scadbuddy/api/print_imports.py:218`, `backend/scadbuddy/api/outputs.py:1070` (`list_summaries`)
- Modify: `backend/scadbuddy/api/models.py` (`select_models` takes any `Selectable`)
- Test: `backend/tests/api/test_model_readers_from_rows.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ModelSummary(slug: str, origin: Literal["builtin", "mine"], name: str, description: str, tags: list[str])` and `Catalogue.list_summaries(slugs: list[str] | None = None) -> list[ModelSummary]`: from rows, one query and no media, cover or preview work; before the copy, built from `list_models`. Rows with `meta_error` are left out, as in `list_models`.
  - `class Selectable(Protocol)` (`slug`, `origin`, `name`, `description`, `tags`) in `api/models.py`; `select_models[T: Selectable](records: list[T], …) -> list[T]`.
  - `Catalogue.raw_metas(slugs: Sequence[str] | None = None) -> dict[str, dict[str, Any] | None]`: each template's `model.json` dict, None where it is not JSON. From rows: `meta`.
  - `library_pin_index` from rows: each row's `meta["libraries"]` where `meta` is not NULL, with today's first-entry-per-name and validation rules.
  - `print_settings(slug)`, `part_print_settings(slug)` from rows: `_meta(slug, row.meta)`; `meta_error` set → `InvalidModelMetaError(slug, row.meta_error)`; no row → `{}`. `part_print_settings` checks names against `row.colour_params` (NULL → only a warning, as an unreadable `model.scad` today).
  - `_require_name_free` from rows: the other rows' `name` where `meta_error` is NULL. `duplicates_of` from rows: rows of origin `mine` whose `meta["upstream"]["id"]` is the id.
  - `previews.due()` needs no change: it reads `slugs()`.

- [ ] **Step 1: Write the failing tests** (before and after `copy_templates`, same answers): `test_pin_index_is_the_same_after_the_copy` (a non-JSON `model.json` is skipped; one that does not validate still counts), `test_library_associations_are_the_same_after_the_copy` (a model made from a library file; `GET /library/files/{id}/associations` and `GET /models/{slug}/associations`), `test_activity_facets_name_models_after_the_copy`, `test_print_import_suggestions_are_the_same_after_the_copy`, `test_outputs_tag_filter_is_the_same_after_the_copy`, `test_print_settings_come_from_the_row` (no `model.json` read: monkeypatch `Catalogue.read_raw_meta` to raise), `test_part_print_settings_refuse_an_unknown_colour_from_the_row`, `test_a_rename_onto_a_taken_name_is_refused_from_the_rows`, `test_duplicates_of_reads_rows`, `test_library_users_still_reads_the_files` (spec §7.4: a pin added by a hand edit with no refresh still counts).
- [ ] **Step 2: Run them, expect FAIL.**
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run them and `tests/api/test_library_associations.py`, `test_libraries.py`, `test_library_delete.py`, `tests/api/test_print_*` that read print settings (`grep -rl "print_settings" tests/api`), `tests/test_preview_backfill.py`; expect PASS.**
- [ ] **Step 5: Commit** `feat(models): pins, associations, print settings and names read template rows (#7317)`.

### Task 9: The thumbnail, and the phase 2 docs

**Files:**
- Modify: `backend/scadbuddy/library/catalogue.py` (`thumbnail_ref`)
- Modify: `backend/scadbuddy/library/previews.py` (`PreviewStore.current_image`)
- Modify: `backend/scadbuddy/library/outputs.py` (`OutputStore.plate_image(output_id: str) -> bytes | None`, if it has no such reader)
- Modify: `backend/scadbuddy/api/models.py` (`get_thumbnail`)
- Modify: `docs/features/catalogue-and-models.md`, `docs/architecture/data.md`, `backend/CLAUDE.md` (the readers use the rows once a run has finished; the thumbnail's `ETag` names its source)
- Test: `backend/tests/api/test_thumbnail_from_rows.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ThumbnailRef(etag: str, read: Callable[[], tuple[bytes, str, str] | None])`: `read` answers the bytes, the content type and the `ETag` of what it read.
  - `Catalogue.thumbnail_ref(slug: str) -> ThumbnailRef | None` (`ModelNotFoundError` when there is no row). From rows: the row's `cover` → `etag = f'"{cover["sha256"]}"'`, `read` reads `paths.root / cover["path"]` and tags the hash of those bytes; else the output cover → `'"output-<id>"'`, `read` reads that output's plate image; else the preview → `'"preview-<preview_id>"'` (`PreviewStore.preview_id`), `read` uses `PreviewStore.current_image(slug) -> tuple[str, bytes] | None` (the preview id and the image from one row) and tags that id; else None. Before the copy: today's `require_model_exists` and `thumbnail`, with the ETag over the bytes.
  - `get_thumbnail`: a matching `If-None-Match` on `ref.etag` → 304 with no read; else `read()`, a 200 with its own tag (plan decision 12). `Cache-Control: no-cache` stays.

- [ ] **Step 1: Write the failing tests** (after `copy_templates`): `test_own_cover_etag_is_the_hash_of_its_bytes` (the same value as before the copy, so clients' copies stay valid), `test_a_304_reads_no_file` for each source (own cover, output, preview: monkeypatch `Path.read_bytes`, `Path.open`, `zipfile.ZipFile` and `PreviewStore.image` to raise; the request with the current tag answers 304), `test_output_cover_etag_names_the_output`, `test_preview_etag_names_the_preview`, `test_a_200_tags_what_it_read` (the preview changes between `thumbnail_ref` and `read`: the 200's tag is the new preview id), `test_no_row_is_404_with_no_stat`, `test_no_cover_is_404`.
- [ ] **Step 2: Run them, expect FAIL.**
- [ ] **Step 3: Implement** the interfaces above and update the docs.
- [ ] **Step 4: Run them, `tests/test_thumbnail.py`, `tests/api/test_model_images.py`; run the full backend ruff, ruff format check and mypy. Expect PASS.**
- [ ] **Step 5: Commit** `feat(models): the thumbnail's ETag from the template row, a 304 reads no file (#7317)`.

### After phase 2

- After it deploys: check the pod images, then time `GET /api/v1/models`, `/api/v1/model-facets` and a thumbnail 304 in Tempo against spec §1 (spec §8). The agent's MCP `resources/list` reads `GET /api/v1/models` (`agent/src/resources/server.ts` `slugs`), so it needs no change; time it too.
- Phase 3 (remove the fallback) is a separate plan.
