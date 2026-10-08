# Durable phase 3e: uploads, outputs, fonts and presets as library commands

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the remaining volume-writing routes onto the operation command shape (`library` queue), with uploaded bytes carried by claim check.

**Routes covered:** model media (upload, caption, order, cover, delete); outputs (create, thumbnail, delete); assets (upload, fetch); font install; preset create, duplicate and patch.

**Architecture:** Each route keeps the refusals that read only the request, writes uploaded bytes to the claim store, and calls `run_operation`. Each kind is an `OperationKind` whose `check` makes the refusals that read the volume or database and whose `run` makes the effect. The same refusals are made again inside the run, where a lock makes them true at the moment of the effect.

Kinds are exported from the feature's `operations.py` as `OPERATION_KINDS`, as `library/operations.py` and `library/model_operations.py` do on this branch. The UI and agent clients send an `Idempotency-Key` and follow a 202 through `command()`.

**Tech stack:**
- Python 3.12, FastAPI, temporalio (`Operation` workflow in `workflows/operation.py`), Postgres;
- React 19 / TS (`frontend/src/api/client.ts`);
- the agent in TS (`agent/src/tools/command.ts`).

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md`:
- §4.2 the command shape;
- §4.3 the `library` row: "thumbnail/readme/media writes, preset writes that need `openscad`, … font install";
- §10 phase 3.

## Global Constraints

- **Pattern:** follow `api/models.py` (thumbnail and README routes), `api/model_files.py` and `library/model_operations.py` exactly. They show `run_operation(ops, response, kind=..., subject=slug, request=..., idempotency_key=..., claimed=...)`, `OPERATION_RESPONSES`, `operation_answer(result, Model)`, `answered_as_routes`, `exists_check`, and `ClaimStore`'s `hold`/`get`/`release`.
- **Size:** an inline request stays under `MAX_REQUEST_BYTES` (128 KB). Uploaded bytes always go by claim.
- **Status codes:** every route answers what it answers today, plus a 202 with the `Operation` past the deadline (`OPERATION_RESPONSES`). A refusal keeps its status, detail and problem extensions (for example the asset quota's `usage`).
- **Determinism:** no I/O in workflow code. The new kinds are activities only, and `Operation` is unchanged.
- **Migrations:** none expected. If one is needed, it is a new file in `backend/scadbuddy/migrations/`.
- **Generated files:** never commit `backend/openapi.json` or a `schema.d.ts`.
- **Tests:** TDD per behaviour. Tests run as CLAUDE.md says, with the api tests on Temporal.

## Rulings made while planning

1. **Library check stays a plain route.** `POST /models/{slug}/libraries/{name}/check` is a dry run: it records nothing, and the checkout it leaves is a cache. It stays a plain route. Cost if wrong: a check that dies with the pod is simply asked again.
2. **Dependencies stay a plain route.** `POST /models/{slug}/dependencies` is read-only.
3. **Preset delete stays a plain route.** `DELETE .../presets/{id}` is one Postgres statement and never runs openscad, so it is not one of §4.3's "preset writes that need openscad".
4. **Output delete is one `library` kind, inbox copies included.** With `delete_inbox_copies`, its run deletes the Bambuddy inbox copies before the files. This deviates from §4.3, which puts the Bambuddy part of an output delete on `bambuddy`.
   - It is acceptable while both workers run in the API process.
   - #1060, which moves `bambuddy` out, must split it.
   - Cost if wrong: the library worker needs the Bambuddy key until then.
5. **Media uploads are claimed by file.** A media upload can be 1 GiB, so it is claimed as a file, never read into memory. `ClaimStore.hold_file(path, digest)` moves an already-streamed file in under its digest, which is computed while streaming.

## Review Focus

1. **Re-sending a 1 GiB media upload with the same key.**
   - Expected: one media item, one commit, and the first answer.
   - Danger: `_receive` streams the body to disk before anything else, so a re-send writes another temp file.
   - Required: the temp file is gone after the answer (claimed, or removed).
2. **Asset quota refusal (413 with `usage`) arriving after a 202.**
   - Expected: the client still sees `usage`.
   - Required: the problem extensions survive through the operation record.
3. **Font install.**
   - Expected: a slow Google Fonts download (tens of seconds) answers 202, and the operation ends with the `InstalledFamily`.
   - Expected: a second install of the same family while the first runs joins it (same key, same subject) rather than downloading twice.
4. **Output create from a job whose blob must be fetched from Bambuddy.**
   - Expected: it runs on the worker that holds the blob store, and its 409/404 refusals still come from the check.
5. **Preset create while openscad is missing.**
   - Expected: still 503, as today.
   - Danger: the operation must not record a 500 instead.

---

### Task 1: Media writes as library kinds

**Files:**
- Modify: `backend/scadbuddy/operations/claims.py` (add `hold_file`)
- Modify: `backend/scadbuddy/api/media.py`
- Modify: `backend/scadbuddy/library/model_operations.py` (or a new `library/media_operations.py` folded into the library `OPERATION_KINDS`)
- Test: `backend/tests/test_claims.py`, `backend/tests/api/test_media_operations.py` (new); keep `tests/api/test_media.py` green

**Interfaces:**
- **Produces:**
  - `ClaimStore.hold_file(path: Path, digest: str) -> Held` moves `path` in as `digest`. If the name already exists, it removes `path` and renews the claim (`created=False`), with the same release semantics as `hold`.
  - `media.py` gets a streaming hash: `_receive` records each part's sha256 while writing.
- **Kinds:**
  - `model_media_upload`: request `{slug, file: claim, file_name, content_type, poster: claim|None, caption}`;
  - `model_media_patch` `{slug, item_id, caption}`;
  - `model_media_order` `{slug, ids}`;
  - `model_media_cover` `{slug, id}`;
  - `model_media_delete` `{slug, item_id}`.
  - All are `exists_check`, plus a check for the 503 "no database" refusal. `run_timeout` stays at the default, except the upload, which uses `PIN_TIMEOUT` (moving a gigabyte and a commit).

- [ ] **Step 1: Write the failing tests.**
  - `test_claims.py`: `test_hold_file_moves_a_streamed_file_in_and_a_second_renews_it`.
  - `test_media_operations.py`:
    - `test_a_repeated_media_upload_makes_one_item` (same `Idempotency-Key` twice → one item, one commit, the same answer, no `cache/upload-*` left);
    - `test_media_edits_are_operations` (patch, order, cover, delete each create a `model_media_*` workflow, using `tests.api.test_model_operations._workflow_ids`);
    - `test_a_media_upload_goes_by_claim` (history under 100 KB for a 2 MB image, `_history_bytes`);
    - `test_too_many_items_is_still_409` (the volume refusal from the check).
- [ ] **Step 2: Run them.** `uv run --frozen pytest -q tests/test_claims.py tests/api/test_media_operations.py`. Expected: FAIL (no kind `model_media_upload`; `hold_file` missing).
- [ ] **Step 3: Implement.**
  - Each route keeps its request-only refusals: multipart shape, sniffing, 415, the 10 MiB image cap, the caption length.
  - The upload hands the claims (`claimed=Claimed(store, [held...])`) to `run_operation`.
  - Each run is today's catalogue call, wrapped in `answered_as_routes` so `MediaOrderError`/403/404 map as before.
  - The routes become `async def` with `responses=OPERATION_RESPONSES`, and answer through `operation_answer(result, ModelRecord)`.
- [ ] **Step 4: Run the tests.** Run the new tests plus `tests/api/test_media.py tests/api/test_media_without_database.py tests/api/test_settings_runtime.py`. Expected: PASS. Then run ruff, ruff format and mypy (`rm -rf .mypy_cache` first).
- [ ] **Step 5: Commit.** `feat(media): a model's media writes as library commands (#1054)`

### Task 2: Outputs as library kinds

**Files:**
- Modify: `backend/scadbuddy/api/outputs.py`, `backend/scadbuddy/library/outputs.py` (only if a helper must move out of `api/`)
- Add kinds: `output_create`, `output_thumbnail`, `output_delete` (in `library/operations.py`'s `OPERATION_KINDS`, or a new `library/output_operations.py` it includes)
- Test: `backend/tests/api/test_output_operations.py` (new); keep `tests/api/test_outputs.py`, `tests/api/test_library_copies.py` green

**Interfaces:**
- `output_create`:
  - Request `{slug, job_id, name, inputs}`; subject: slug.
  - The check runs `require_model_exists`, `require_job`, wrong slug / not done (409) and result gone (404).
  - The run does the materialize, `OutputStore.create`, the event, and the `inputs` mismatch 422 (it needs the job).
  - Result: `OutputDetail`. The route answers 201.
- `output_thumbnail`:
  - Request `{output_id, png: claim}`; the PNG check stays in the route; subject: output id.
  - Result: none. The route answers 204.
- `output_delete`:
  - Request `{output_id, delete_inbox_copies}`; subject: output id.
  - Planning Ruling 4: the run deletes the inbox copies (strict, as today) and then the files.
  - The route answers 204.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_repeated_output_create_makes_one_output` (same key twice → one output directory, the same id);
  - `test_output_writes_are_operations` (one workflow each);
  - `test_a_job_not_done_is_still_409_without_an_operation_record`;
  - `test_an_inbox_copy_that_fails_to_delete_keeps_the_output` (Bambuddy 500 → the problem is recorded, and the output directory still exists).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Mind the 201 and 204 statuses. `run_operation` answers 200 by default, so set `response.status_code` as `create_model` does for its 201.
  - Look at how 3c handled the 201 and 204.
- [ ] **Step 4: Run the tests.** Run the new tests plus `tests/api/test_outputs.py tests/api/test_library_copies.py`, then ruff and mypy.
- [ ] **Step 5: Commit.** `feat(outputs): saving, re-covering and deleting an output as library commands (#1054)`

### Task 3: Assets upload and fetch as library kinds

**Files:**
- Modify: `backend/scadbuddy/api/assets.py`
- Add kinds `asset_upload`, `asset_fetch`
- Test: `backend/tests/api/test_asset_operations.py` (new); keep `tests/api/test_assets.py`, `test_asset_fetch.py`, `test_no_open_redirect.py` green

**Interfaces:**
- `asset_upload`:
  - Request `{slug, file: claim, file_name}`. The 8 MiB 413 stays in the route.
  - The check makes the 404 and the 503 "no database" refusals.
  - The run makes `AssetRejectedError` → 422, the quota 413 with `usage`, the 507, and the mirror.
  - Result `AssetMeta`, 201.
- `asset_fetch`:
  - Request `{slug, url}`; subject: the URL's host (as for an import).
  - Keep the 503 Retry-After budget refusal **in the route**, before `run_operation`, using `before_start`, so a busy budget never becomes a recorded failure.
  - The run does the fetch (30 s) and the store.
  - Result `FetchedAsset`, 201.
  - Redact the URL as `shown_url` does for imports (`api/models.py`): claim the full URL as bytes, and keep only the shown URL inline.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_repeated_asset_upload_stores_one_asset`;
  - `test_the_quota_refusal_keeps_usage_after_a_202` (force the 202 with `partial(start_command, deadline=timedelta(seconds=1))` and a slow run, as `test_model_edit_operations.py::test_a_slow_save_answers_202...` does; assert the operation's error carries `usage`);
  - `test_an_asset_fetch_never_records_the_urls_query`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Run the new tests plus the three existing asset files, then ruff and mypy.
- [ ] **Step 5: Commit.** `feat(assets): uploading and fetching an asset as library commands (#1054)`

### Task 4: Font install as a library kind

**Files:**
- Modify: `backend/scadbuddy/api/fonts.py`
- Add kind `font_install`
- Test: `backend/tests/api/test_font_operations.py` (new); keep `tests/api/test_fonts.py`, `tests/api/test_events.py` green

**Interfaces:**
- `font_install`:
  - Request `{family, force}`; subject: the family lower-cased.
  - `run_timeout = timedelta(minutes=5)` (downloads plus `fc-cache`).
  - Check: none (`no_check`); the run is today's `install`.
  - Result `InstalledFamily`, 200.
  - The best-effort `store.fonts.publish` stays inside the run.

- [ ] **Step 1: Write the failing tests.**
  - `test_font_install_is_an_operation`;
  - `test_two_installs_of_one_family_with_one_key_download_once` (count the fake Google Fonts requests);
  - `test_a_family_not_in_the_catalogue_is_still_404`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Run the new tests plus `tests/api/test_fonts.py tests/api/test_events.py`, then ruff and mypy.
- [ ] **Step 5: Commit.** `feat(fonts): installing a font as a library command (#1054)`

### Task 5: Preset writes that need openscad as library kinds

**Files:**
- Modify: `backend/scadbuddy/api/presets.py`
- Add kinds `preset_create`, `preset_duplicate`, `preset_update`
- Test: `backend/tests/api/test_preset_operations.py` (new); keep `tests/api/test_presets.py` green

**Interfaces:**
- Requests: the route's body plus `{slug}` (and `preset_id`). The params are small JSON, inline under 128 KB; a body over the cap is the 413.
- The check makes the model 404 and the template 403 for update.
- The run makes `_require_valid` (openscad, schema cache) and the Postgres write.
  - The 422, the 409 name-taken/too-many and the 503 openscad-missing refusals keep their problems through `answered_as_routes`.
- `preset_update` without `params` skips validation as today.
- The delete route is unchanged (planning Ruling 3).

- [ ] **Step 1: Write the failing tests.**
  - `test_preset_writes_are_operations`;
  - `test_a_repeated_preset_create_makes_one_preset`;
  - `test_a_preset_create_without_openscad_is_still_503` (monkeypatch the runner to raise as `test_presets.py` does).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Run the new tests plus `tests/api/test_presets.py`, then ruff and mypy.
- [ ] **Step 5: Commit.** `feat(presets): preset writes that validate against the template as library commands (#1054)`

### Task 6: The UI and the agent follow these commands

**Files:**
- Modify:
  - `frontend/src/api/client.ts`: `uploadMedia` (XHR: add the `Idempotency-Key` header, keep it on a re-send, and follow a 202 through the same follow loop `command()` uses; factor that loop out if needed), `patchMedia`, `reorderMedia`, `setMediaCover`, `deleteMedia`, `createOutput`, `putThumbnail`, `deleteOutput`, `uploadAsset`, `installFont`, the three preset writes.
  - `agent/src/tools/outputs.ts` (`save_output`, `delete_output`), `customizer.ts` (`render_model`'s save, `upload_asset`, `fetch_asset`, the preset tools), `libraries.ts` (`install_font`): via `command(ctx, what, (headers) => ...)`.
- Test: `frontend/src/api/client.test.ts` (a describe "uploads, outputs, fonts and presets are commands (#1054)"), `agent/test/tools.test.ts` (an `it.each` over the converted tools: a 202 answer followed to its result).

- [ ] **Step 1: Write the failing tests.**
  - Each converted client call sends an `Idempotency-Key`.
  - A 202 is followed to the operation's result.
  - `uploadMedia` reports progress and still follows a 202.
- [ ] **Step 2: Run them.** `pnpm exec vitest run src/api/client.test.ts` and `pnpm exec vitest run test/tools.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the checks.** In both packages: `pnpm lint && pnpm typecheck` and the two test files.
- [ ] **Step 5: Commit.** `feat(ui): uploads, outputs, fonts and presets follow their commands (#1054)`

### Task 7: Docs

- Spec §10 phase 3: add "As built so far (3e)", naming the kinds, Rulings 1–5, and the claim-by-file.
- `CLAUDE.md` Layout: the new kinds and `ClaimStore.hold_file`.
- `README.md`: only if an operator-visible behaviour changed. The output delete's inbox step on `library` is noted under #1060.
- Commit: `docs: uploads, outputs, fonts and presets as commands (#1054)`.
