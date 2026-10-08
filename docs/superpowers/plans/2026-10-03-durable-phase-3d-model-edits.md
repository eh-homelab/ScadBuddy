# Phase 3d: a model's edits as commands on `library` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every git write to a model that is still a plain request becomes an `Operation` (§4.2) on the `library` queue:
- the source: `PUT /models/{slug}/source` (including `?merge_base=`) and `POST /models/{slug}/source/patch`;
- the sidecars: `PUT`/`DELETE /models/{slug}/thumbnail` and `PUT`/`DELETE /models/{slug}/readme`;
- the sibling files: `PUT`/`DELETE /models/{slug}/files/{name}`;
- the history: `POST /models/{slug}/versions/{commit}/restore` and `POST /models/{slug}/upstream/{merge,dismiss,detach}`.

**Architecture:**
- **Kinds.** All in `library/model_operations.py` `model_kinds(state)`, beside 3c's five. They are all `done`, on `queue="library"`, and wrapped in `answered_as_routes`.
- **Routes.** Each route keeps the refusals that read only its request: `require_mine`, the NUL check, conflict markers, `base` with `merge_base`, the PNG check, `_require_sibling`. It then calls `run_operation` with `responses=OPERATION_RESPONSES` and `idempotency_key: IdempotencyKey`.
- **Checks and runs.**
  - Each check makes the cheap refusals that read the volume: `require_model_exists`, the stale-`base` 409 through `_require_base`, `_require_revision`.
  - Each run is the route's former body, moved into a function that the run and nothing else calls: `_save_source`, `catalogue.write_thumbnail`, and so on. The run makes those refusals again where the former body did.
- **Claims.** Large bytes go by claim check, as in 3c: a source, the `SourcePatch` body as JSON, a README, a thumbnail and a sibling file's content. The request carries only the claim names.
- **Responses** are unchanged: a 200 with the record (or `UpstreamMerge`, or `ModelVersion`), and a 202 with the operation past the deadline.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.1–§4.3 (the `library` row: "every git, file and download command"). Stacked on #1126 (`feat/1054-models`).

## Rulings

1. **A merge conflict is answered by the route, not the operation.**
   - The conflict 409 carries `merged`, the whole three-way result, which can be up to 1M characters. As a refusal or a recorded failure, it would sit in workflow history (activity failure details) and in the record.
   - So the route first reads `catalogue.upstream_status(slug)`, which is read only and computes `plan_merge`. When the preview has conflicts, the route answers today's 409 itself, with `merged`, `merge_base`, `conflicts`, `taken` and `kept`, and starts nothing.
   - If the run still meets a `MergeConflictError` (the upstream moved in between), it answers 409 with `state="update"` and no `merged`, telling the client to merge again. That 409 already exists for "kept changing".
   - Cost if wrong: one extra read of the upstream per merge.
2. **The stale-`base` 409 keeps its `base`/`current` extensions.** They are short commit ids, so they are safe in history.
3. **A patch claims its whole body.** `SourcePatch` (a diff, or up to 100 edits) is serialised as JSON and claimed. The run reads it, applies it to the source as it stands, and saves. The check's `_require_base` is the early stale refusal, and `write_source`'s `expected_version` under the lock is the real one.
4. **The parse check runs in the run.** It is openscad, so it can be slow. The kind's `run_timeout` is `PIN_TIMEOUT`, because a save can clone missing library checkouts (`resolve_search_path`).
5. **`require_mine` stays in the routes**, as in 3b and 3c.

## Global Constraints

- **Kind names:**
  - `model_source_put`, `model_source_patch`
  - `model_thumbnail_put`, `model_thumbnail_delete`
  - `model_readme_put`, `model_readme_delete`
  - `model_file_put`, `model_file_delete`
  - `model_restore`
  - `model_upstream_merge`, `model_upstream_dismiss`, `model_upstream_detach`
- **The subject is the slug.**
- **Status codes and bodies are unchanged**, apart from 202 past the deadline.
- **Inline requests** stay under `MAX_REQUEST_BYTES` (128 KB). Anything that can be larger goes by claim.
- **Commits:** never commit `backend/openapi.json` or a `schema.d.ts`. Commit with `git add <paths>`.

## Review Focus

1. A re-sent source save with the same `Idempotency-Key` makes one revision.
2. A source save over 128 KB, and a patch whose edits exceed it, go by claim, never into history.
3. A stale `base` still answers 409 with `base` and `current`, whether the check or the run finds it.
4. A merge with conflicts answers the 409 with `merged`, and the operation history holds no `merged`.
5. The UI's editor save, README and thumbnail edits, file edits, restore and upstream actions, and the agent's tools for them, follow a 202.

---

### Task 1: Source saves as kinds

**Files:**
- Modify `backend/scadbuddy/library/model_operations.py`:
  - add `source_put_check` / `source_put_run` and `source_patch_check` / `source_patch_run`;
  - register `kind("model_source_put", …, run_timeout=PIN_TIMEOUT)` and `kind("model_source_patch", …, run_timeout=PIN_TIMEOUT)`.
- Modify `backend/scadbuddy/api/models.py`:
  - `put_source` and `patch_source` take `response`, `ops: OperationsDep`, `paths: PathsDep` and `idempotency_key`;
  - they claim the bytes, call `run_operation`, and return `operation_answer(result, ModelRecord)`;
  - add a module function `save_source_run(slug, source, *, message, force, merge_base, expected_version, state) -> ModelRecord`, which wraps `_save_source` with the state's deps (`CheckoutFetcher(state.libraries, state.installs, state.checkouts)`);
  - add `patch_source_run(slug, body: SourcePatch, state) -> ModelRecord`: the former `patch_source` body from reading the source onward.
- **Test:** `backend/tests/api/test_model_edit_operations.py` (new):
  - `test_a_repeated_source_save_makes_one_revision` (`_commits` helper as in `test_model_operations.py`);
  - `test_a_large_source_save_goes_by_claim` (900 KB source, `?force=true`, history under 100 KB);
  - `test_a_stale_base_is_a_409_with_current`;
  - `test_a_patch_with_large_edits_goes_by_claim`;
  - `test_a_slow_save_answers_202` (patch `save_source_run` to sleep past the deadline, as `test_a_slow_create_answers_202` does).
  - The existing `tests/api/test_source*.py` and `test_models.py` source tests keep their assertions.

- [ ] Step 1: Write the failing tests.
- [ ] Step 2: Run `uv run --frozen pytest tests/api/test_model_edit_operations.py -q -p no:randomly`. Expected: FAIL, because no `model_source_put` operation is recorded.
- [ ] Step 3: Implement it. Request shapes:
  - put: `{"slug", "source": <claim>, "message", "force", "merge_base", "base"}`;
  - patch: `{"slug", "body": <claim of SourcePatch JSON>}`.
  The check does `require_model_exists`, and when a `base` is given, `_require_base(slug, base, catalogue.version(slug))` off the loop.
- [ ] Step 4: Run the new tests and `tests/api/test_source*.py tests/api/test_models.py`. Expected: PASS.
- [ ] Step 5: Commit `feat(models): source saves are commands (#1054)`.

### Task 2: Sidecars and sibling files as kinds

**Files:**
- Modify `backend/scadbuddy/library/model_operations.py`: add six kinds, `model_thumbnail_put`/`_delete`, `model_readme_put`/`_delete` and `model_file_put`/`_delete`. Each check is `require_model_exists`. Each run calls the catalogue method the route called, maps the same errors, and emits the same event: `ModelEvent("model.updated")` for thumbnail and README, `announce_source_change` for files.
- Modify `backend/scadbuddy/api/models.py`: the thumbnail and README routes. `delete_thumbnail` and `delete_readme` become `async def`.
- Modify `backend/scadbuddy/api/model_files.py`: both file routes.
- Claims: the thumbnail PNG, the README text and the file content.
- **Test** (added to `test_model_edit_operations.py`):
  - `test_sidecar_and_file_edits_are_operations`: set and remove the thumbnail, set and remove the README, write and remove a sibling. Each one records its kind, and each answer is unchanged.
  - `test_removing_a_missing_readme_is_still_404`.

- [ ] Step 1: Write the failing tests.
- [ ] Step 2: Run them. Expected: FAIL.
- [ ] Step 3: Implement.
- [ ] Step 4: Run them, plus `tests/api/test_model_files.py tests/api/test_readme*.py tests/api/test_thumbnail*.py` (whichever exist). Expected: PASS.
- [ ] Step 5: Commit `feat(models): thumbnail, README and file edits are commands (#1054)`.

### Task 3: Restore and upstream as kinds

**Files:**
- Modify `backend/scadbuddy/library/model_operations.py`:
  - `model_restore`: check `require_model_exists`, `require_history` and `_require_revision`; run is the former `restore_version` body, returning `ModelVersion`;
  - `model_upstream_merge`, `model_upstream_dismiss`, `model_upstream_detach`: the run is `upstream._answer(slug, …)` as the routes call it.
- Modify `backend/scadbuddy/api/versions.py` and `backend/scadbuddy/api/upstream.py`. The routes become `async def` and call `run_operation`.
  - The merge route first calls `await asyncio.to_thread(_answer, slug, lambda: catalogue.upstream_status(slug))`. When `status.preview` has conflicts, it raises the conflict 409 with today's fields (Ruling 1) and starts nothing.
  - In the run, a `MergeConflictError` maps to `409 state="update"`, without `merged`.
- **Test** (added to `test_model_edit_operations.py`):
  - `test_a_restore_is_an_operation`;
  - `test_a_conflicting_merge_answers_merged_and_starts_nothing`: build the conflict as `tests/api/test_upstream.py` does, then assert `_workflow_ids(app, "model_upstream_merge") == []` and that `merged` is in the 409;
  - `test_a_clean_merge_is_an_operation`;
  - `test_dismiss_and_detach_are_operations`.
  - The existing `tests/api/test_upstream.py` and `test_versions*.py` keep their assertions.

- [ ] Step 1: Write the failing tests.
- [ ] Step 2: Run them. Expected: FAIL.
- [ ] Step 3: Implement.
- [ ] Step 4: Run them, plus `tests/api/test_upstream.py tests/api/test_versions.py`. Expected: PASS.
- [ ] Step 5: Commit `feat(models): restore and upstream actions are commands (#1054)`.

### Task 4: Clients follow the commands

**Files:**
- Modify `frontend/src/api/client.ts`. These go through `command()`:
  - the source save (`:554`) and the merge-resolution save (`:609`);
  - `setThumbnail`, `deleteThumbnail`, `setReadme` and `deleteReadme`;
  - the source-file write and delete;
  - `restoreVersion`, `mergeUpstream`, `dismissUpstream` and `detachUpstream`.
  The conflict 409 still reaches the merge UI with `merged`, because `command()` keeps a problem's extensions.
- Modify `agent/src/tools/`: `update_source`, `apply_patch`, `write_source_file`, `delete_source_file`, `set_readme`, `delete_readme`, `set_model_thumbnail`, `delete_model_thumbnail`, `restore_version` and `update_from_upstream` (and the dismiss and detach tools, if they exist) go through `command(ctx, what, (headers) => …)`.
- **Test:** `frontend/src/api/client.test.ts` and `agent/test/tools.test.ts`. Each call sends an `Idempotency-Key` and follows a 202 to the operation's result. The merge conflict keeps `merged`.

- [ ] Step 1: Write the failing tests.
- [ ] Step 2: Run them. Expected: FAIL.
- [ ] Step 3: Implement.
- [ ] Step 4: Run `pnpm lint && pnpm typecheck && pnpm test` in `frontend/` and in `agent/`. Expected: PASS.
- [ ] Step 5: Commit `feat(models): the UI and the agent follow model edit commands (#1054)`.

### Task 5: Docs

- Spec §10: "As built so far (3d)".
- `CLAUDE.md`: the edit kinds in `library/model_operations.py`, and Ruling 1.
- Commit `docs: model edits as commands (#1054)`.
