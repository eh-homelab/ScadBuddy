# 3DFP profile-fetch worker (#6625) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new spool type with a 3dfilamentprofiles.com (3DFP) id gets a slicer preset for
every nozzle of every printer model SB has (H2C: 0.2, 0.4, 0.6, 0.8), fetched by a worker
that runs a real Chromium logged in to 3DFP, imported into Bambuddy's local presets and set
as the type's presets per nozzle, with K values from the printers' K tables where one
matches.

**Architecture:** The API opens a fetch run (a row) and starts the `ProfileFetch` workflow on
a new Temporal queue, `profile-fetch`. A new worker process, `python -m
scadbuddy.profile_fetch`, polls that queue in its own pod and image. It holds no database
secret and only a `profile-fetch` service credential. Its activity logs in to 3DFP with a
headed Chromium under Xvfb and a normal Chrome user agent. It exports one profile per printer
model and nozzle from `/my/spools/details/<id>`, and PUTs each file's bytes to a CRUD route
that stores it as a blob held by the run. A second activity asks the API to import the run.
The API reads the blobs, checks them, posts them as one zip to Bambuddy's `POST
/local-presets/import`, maps the names to local preset ids, sets the type's presets for the
fetched keys, sets the K values from `GET /printers/{id}/kprofiles/`, refreshes the kept copy
of local presets, and records what is still missing. Invoice confirm starts a fetch for each
new type with a 3DFP id; a route, two agent tools and a button start or read one by hand.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Temporal Python SDK, Playwright for Python
(Chromium), Xvfb; TypeScript agent (`defineTool`); React + TanStack Query; Docker buildx;
Kubernetes manifests and the 1Password operator in eh-homelab/clusters.

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` §9 ("3DFP needs
a real browser", steps 7-8, "As built in #6267"). Earlier plan:
`docs/superpowers/plans/2026-11-18-filament-invoice-import.md` (Decision 1). How 3DFP is
driven today: `.claude/skills/filament-intake/SKILL.md`, `scripts/3dfp.mjs`, `scripts/intake.py`
(`link`). The pattern for a worker with no database secret: `backend/scadbuddy/intake_worker/`
and README "Intake worker (#6133)".

## Global Constraints

- Database rule (#5295): only the CRUD service has SQL. The worker holds no database secret,
  no KEK, no store credential and no Bambuddy key, and refuses to start when any of
  `SCADBUDDY_DATABASE_URL`, `PGPASSWORD`, `PGPASSFILE`, `SCADBUDDY_SECRET_KEY_FILE` is set
  (reuse `intake_worker/settings.py` `FORBIDDEN_ENV`). It imports no Postgres code
  (`psycopg` absent from `sys.modules` after `import scadbuddy.profile_fetch.worker`).
- The browser runs untrusted web content. Its pod holds only the narrow `profile-fetch` CRUD
  credential and the 3DFP login. New cross-service data goes through `crud_router("profile-fetch")`
  routes that require that credential from the first PR.
- Secrets reach the pod only through the OnePasswordItem's Secret, as env. They are
  `pydantic.SecretStr`. They never appear in a log line, an exception text, a Temporal payload
  or a run's `error`.
- Bambuddy: tests use respx and the recordings in `backend/tests/bambuddy/recordings/`.
  During the build, live Bambuddy access is GET only. No task writes to the live Bambuddy.
- 3DFP is never hit by CI or by any test. Tests use the replica pages under
  `backend/tests/profile_fetch/site/` and the sample export beside them.
- Blobs move as bytes in a request body named by their sha256, and as `{name, mediaType}`
  pointers. Never base64 in JSON (`lint-base64-images.sh`, #2479).
- Migrations: one new file `$(date -u +%Y%m%dT%H%MZ)_filament_profile_fetch.sql` in
  `backend/scadbuddy/migrations/`. Edit no merged file.
- Every new public `/api/v1` route has `summary=` and `description=`, and an agent tool or a
  `NOT_A_TOOL` entry (`agent/src/tools/coverage.ts`). Every agent write tool is classified in
  `agent/src/sessions/touched.ts`. Tools work in classic and durable modes.
- Every setting is a remembered setting (`core/settings.py`, `core/setting_docs.py`, the
  Settings page), never a constant a person cannot change.
- Images: amd64 only. Every new `docker/setup-buildx-action` step sets
  `driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1` and the
  `buildkitd-config-inline` docker.io mirror (`lint-buildx-mirror.sh`). A new `type=gha` scope
  uses `mode=min`. Every job runs on `ubuntu-latest`.
- Load `frontend-design` for the UI task. Mocks never call `crypto.randomUUID`.
- Commands: backend `cd backend && uv run --frozen pytest <files> -n 4` with
  `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/scadbuddy_test_6625`;
  `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`.
  Agent and frontend: `pnpm typecheck && pnpm lint && pnpm test`.
- Cite sources (#259): every external fact gets a link; every measured fact says where and how.

## Decisions this plan makes

1. **The presets go into Bambuddy's local presets, not a store of SB's own.** The API posts
   the run's files as one zip to `POST /api/v1/local-presets/import` (BB OpenAPI:
   `ImportResponse {success, imported, skipped, errors}`), then maps names to ids with `GET
   /local-presets/`, as `intake.py link` does. Why: a type preset's `slicer_filament` is a BB
   local id, and the resolver and the native slicer read those through the kept copy
   (`slicing/held_presets.py`, whose docstring argues for one record). After the import the
   API runs `refresh_held_presets` once, so a native run offers the new presets at once. BB
   gates the import on its settings-update permission, which an API key lacks while BB auth
   is on (`create_local_preset` docstring). Then the run fails with that reason, and no
   preset is set. Prod BB takes writes without a key today (filament-intake skill).
2. **The import runs in the API, not the worker.** The worker has no Bambuddy key and no
   database. It returns files only. Why: #5295, and the worker runs untrusted web content.
3. **The worker is Python, in the backend package** (`backend/scadbuddy/profile_fetch/`), with
   Playwright for Python. It reuses `CrudClient`, `ServiceAuth`, `exchange_source` and the
   intake worker's shape. It does not port `3dfp.mjs` to a Node service. Why: one wire-model
   module shared by the worker and the CRUD routes, one test stack, a known pattern.
4. **A new image, `ghcr.io/eh-homelab/scadbuddy-profile-fetch`.** It is built from a new
   Dockerfile stage `profile-fetch` on `python:3.12-slim-bookworm@sha256:…` (Docker Hub
   through the mirror; Dependabot moves it). The stage installs the backend with `uv sync
   --frozen --no-dev --group profile-fetch`, then `playwright install --with-deps chromium`
   into `/ms-playwright`, and `xvfb`, `xauth` and `tini`. `playwright` is pinned exactly in a
   new `[dependency-groups] profile-fetch`, so the app image does not carry it. The entrypoint
   is `tini -- xvfb-run -a python -m scadbuddy.profile_fetch`. It runs as uid 10001, amd64 only.
5. **Headed Chromium under Xvfb, with a normal Chrome user agent.** This is the only mode
   measured to pass Vercel's check on a fresh profile. The skill says headed passes it, and
   headless with the `HeadlessChrome` UA gets 403 since 2026-11-01. The UA string is
   `3dfp.mjs`'s. The profile directory is on the pod's emptyDir. The worker logs in at start
   (a bad secret fails the start, not the first fetch), and again when a page redirects to
   `/login`. It keeps `3dfp.mjs`'s in-page blob hook (`window.__dl`), because the measured
   crash is on the blob download.
6. **Chromium's own sandbox stays on.** There is no `--no-sandbox`. Its user namespaces need
   the pod's seccomp `Unconfined` and the Localhost AppArmor profile
   `scadbuddy-intake-sandbox` (`unconfined` plus `userns,`), which the nodes already load for
   the intake sandbox. Why: a renderer exploit must not reach the process that holds the
   password and the CRUD credential. If the sandbox cannot start, Chromium's launch fails and
   the worker exits. It fails closed.
7. **The worker's credential reaches one area, `runs`, and only runs the API opened.** The
   routes are `PUT …/profile-fetch/runs/{run_id}/files/{sha256}` (raw bytes,
   `application/json`), `POST …/runs/{run_id}/import` (`Idempotency-Key`) and `POST
   …/runs/{run_id}/failed`. Each answers 409 `not_running` for a run that is not `running`. The
   file route checks the sha256, a 512 KiB cap, a JSON object whose `name` is a string of 1 to
   200 characters, and that the `printer_model` and `nozzle` query values name one of the run's
   targets. A compromised worker can therefore only add files to a fetch that a person or a
   confirm started.
8. **The data.** The migration adds `filament_spool_types.tdfp_id integer CHECK (tdfp_id > 0)`
   and a table `filament_profile_fetches`. Its columns are `id uuid` (the run id; the workflow
   id is `profile-fetch:<type_id>`), `type_id` (FK, ON DELETE CASCADE), `tdfp_id`, `targets
   jsonb`, `files jsonb` (per key: blob key, preset name), `status` (`running`, `imported`,
   `partial`, `failed`), `missing jsonb`, `k_values int`, `notes jsonb`, `error text`,
   `started_by text`, `started_at` and `finished_at`. There is a partial unique index of one
   `running` row per type. The files are held in `blob_refs` with holder kind `profile_fetch`
   and holder id the run id. A delete trigger releases them, as
   `20261114T0603Z_intake_worker.sql` does. The migration also adds `'profile-fetch'` to the
   caller checks of `service_credentials` and `service_caller_suspensions`.
9. **Targets.** The printer models are the distinct non-null `model` of SB's printers
   (`printers/store.py` `list`). The nozzles come from a new remembered setting,
   `profile_fetch_nozzles`, with default `["0.2", "0.4", "0.6", "0.8"]`. Each value must be in
   `filament/presets.py` `NOZZLES`. The 3DFP printer option is the model string itself
   (`H2C`). The slicer option is the constant `"Bambu Studio"`, because SB slices only with
   Bambu Studio. A model or nozzle the dialog does not offer is a missing key with that reason.
   A label map waits until a printer needs one (YAGNI).
10. **Merge, not replace.** The fetched keys replace the type's preset rows of those
    `(printer_model, nozzle_diameter)` keys. The type's other rows stay. A spool's own rows are
    untouched (`presets.effective_presets` already lets them win).
11. **K values only where a match is exact.** For each SB printer with a `bambuddy_printer_id`
    whose model is a target, and for each fetched nozzle, the API reads `GET
    /printers/{id}/kprofiles/?nozzle_diameter=<n>`. A profile matches when its `filament_id`
    equals the imported preset document's `filament_id` and that id does not start with `GF`
    (a Bambu system id that many types share). Two matches with different `k_value` for one
    `(extruder, nozzle)` give none. The K rows replace only the fetched keys, as in Decision 10,
    with `extruder=extruder_id`, `nozzle_type=nozzle_id`, `cali_idx=slot_id`, `name` and
    `setting_id`. A printer that does not answer is a note, not a failure.
12. **Who starts a fetch.** Invoice confirm starts one for each type it created that has a
    `tdfp_id` and no preset rows. It starts after the commit; a failure to start is logged
    and never fails the confirm. `POST /api/v1/filament/types/{id}/profile-fetch` starts one
    by hand. A start while one runs joins it (`WorkflowIDConflictPolicy.USE_EXISTING`, and the
    partial unique index), and the answer says `joined: true`.
13. **Failures.** The export activity has `start_to_close` 20 min, `heartbeat` 2 min,
    `schedule_to_start` 30 min ("no profile-fetch worker picked the fetch up") and 3 attempts
    from 1 min. A login refusal (`login_failed`) and an unknown 3DFP id (`tdfp_not_found`) are
    non-retryable. A Vercel check that does not clear within 15 s (`bot_check`) is retried. One
    nozzle's failure does not stop the others. It is reported, and the run ends `partial`. When
    the workflow gives up, `failed` posts the reason. The `failed` activity always runs, as in
    the intake worker.
14. **Egress: public 443, not an FQDN list.** The NetworkPolicy allows DNS, the API's 8082,
    Temporal's frontend and TCP 443 to `0.0.0.0/0` except `10.0.0.0/8`, `172.16.0.0/12`,
    `192.168.0.0/16`, `100.64.0.0/10` and `169.254.0.0/16`. Why: the site loads assets from
    hosts we have not measured, and an FQDN list would break the export silently. The real
    risk is that the browser reaches the LAN, and the except-list blocks that.
15. **The secret.** OnePasswordItem `scadbuddy-3dfp` makes Secret `scadbuddy-3dfp` with keys
    `email` and `password`. They become env `SCADBUDDY_TDFP_EMAIL` and
    `SCADBUDDY_TDFP_PASSWORD`. The owner keeps the item "3D Filament Profiles" in the vault the
    cluster's 1Password operator reads (`homelab - prod`), with a `password` field (it exists)
    and a new text field `email` (the account email, which the item's username lacks today).
16. **One export at a time.** The worker sets `max_concurrent_activities=1`. The Deployment has
    one replica with strategy `Recreate`, so two browsers never share one login.
17. **CI tests the browser in the image, against a replica.** The driver test
    (`requires_chromium`) runs inside the built `profile-fetch` image with `--network none`
    against a local server that serves replica pages. These copy the structure `3dfp.mjs`
    relies on: the login form, the "Add to My Spools" button, the "Export slicer profile"
    button, a `[role=dialog]` with three comboboxes, the acknowledge checkbox and a "Download"
    button that clicks a `blob:` anchor. A Vercel-check page and a `/login` redirect are
    replicas too. backend-pytest tests the rest with a fake driver. The job runs in the
    `image` lane and is in `CI Summary`'s `needs`.

## Review Focus

1. **The 3DFP password or email leaks.** A login failure, a Playwright timeout on the
   filled form, or a traceback must not carry either value into logs, `error`, or Temporal
   history. Pinned in Task 5 (`test_a_failed_login_logs_no_secret`) and Task 6
   (`test_failed_reason_carries_no_secret`).
2. **A start while a fetch runs.** It joins the run and never starts a second browser on the
   same type. Pinned in Task 4 (`test_a_second_start_joins_the_running_fetch`).
3. **A preset name BB already has.** On a re-fetch, BB answers `skipped`. The key maps to the
   existing local preset's id and does not fail. Pinned in Task 3
   (`test_a_name_bb_already_has_maps_to_its_id`).
4. **A model or nozzle that 3DFP does not offer.** That key is listed as missing with its
   reason, the others are imported, and the run is `partial`. Pinned in Task 3
   (`test_a_missing_key_makes_the_run_partial`) and Task 5
   (`test_an_option_the_dialog_lacks_is_reported`).
5. **No worker deployed, or the worker is down.** The run ends `failed` with "no
   profile-fetch worker picked the fetch up" and does not stay `running`. Pinned in Task 6
   (`test_no_worker_fails_the_run`).

---

### Task 1: The `profile-fetch` caller, the data, and the type's 3DFP id

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_profile_fetch.sql`
- Create: `backend/scadbuddy/profile_fetch/__init__.py` (docstring only), `backend/scadbuddy/profile_fetch/wire.py`
- Modify: `backend/scadbuddy/auth/service_auth.py` (`ServiceCaller`), `crud/wire.py` (`CrudCaller`), `auth/routes.py` (scope `crud.profile-fetch`), `auth/service_credentials.py`, `auth/exchange.py` (`SERVICE_ACCOUNTS["scadbuddy-profile-fetch"]`), `api/crud_areas.py` (`"profile-fetch": []` until Task 3), `filament/models.py` (`tdfp_id` on `SpoolTypeCreate`, `SpoolTypePatch`, `SpoolType`), `filament/types.py`, `core/settings.py` + `core/setting_docs.py` (`temporal_task_queue_profile_fetch = "profile-fetch"`, `profile_fetch_nozzles`)
- Test: `backend/tests/auth/test_routes.py`, `backend/tests/crud/test_crud_port.py`, `backend/tests/filament/test_types.py`, `backend/tests/profile_fetch/test_wire.py`

**Interfaces:**
- Produces in `profile_fetch/wire.py`. It imports no database or app code. `DEFAULT_QUEUE = "profile-fetch"`, `FETCH_WORKFLOW = "ProfileFetch"`, `EXPORT_ACTIVITY = "profile_fetch_export"`, `IMPORT_ACTIVITY = "profile_fetch_import"`, `FAILED_ACTIVITY = "profile_fetch_failed"`, `AREAS = frozenset({"runs"})`, `SLICER = "Bambu Studio"`, `MAX_FILE_BYTES = 512 << 10`, `def fetch_workflow_id(type_id: str) -> str` (`"profile-fetch:<type_id>"`).
- Models: `ExportTarget{printer_model: str, nozzles: list[str]}`, `FetchAsk{run_id: str, type_id: str, tdfp_id: int, targets: list[ExportTarget]}`, `ExportFailure{printer_model, nozzle, reason: str (≤300)}`, `ExportReport{exported: int, failures: list[ExportFailure]}`, `MissingKey{printer_model, nozzle_diameter, reason}`, `ImportOutcome{status: Literal["imported","partial","failed"], presets: int, k_values: int, missing: list[MissingKey], notes: list[str]}`, `FailedAsk{reason: str (≤500)}`.
- `SpoolType.tdfp_id: int | None`.

- [ ] **Step 1: Write the failing tests.** `test_profile_fetch_caller_has_its_scope` (in `test_routes.py`: `"profile-fetch": "crud.profile-fetch"`); `test_profile_fetch_service_account_maps_to_its_caller`; `test_a_type_keeps_its_tdfp_id` (create with `tdfp_id=30173`, read back 30173; `tdfp_id=0` → 422); `test_one_running_fetch_per_type` (a second `running` row for one type raises a unique violation); `test_deleting_a_fetch_releases_its_blobs` (a `blob_refs` row with holder `profile_fetch` is gone after the type is deleted); `test_wire_imports_no_database_code` (in a subprocess, `import scadbuddy.profile_fetch.wire` leaves `psycopg` out of `sys.modules`); `test_nozzles_setting_refuses_an_unknown_size` (`["0.5"]` → 422).
- [ ] **Step 2: Run** `uv run --frozen pytest tests/auth/test_routes.py tests/crud/test_crud_port.py tests/filament/test_types.py tests/profile_fetch -n 4`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration (Decision 8), the caller in each list (copy the `intake` lines), the field through `types.py`'s insert, update and select, and the two settings with docs.
- [ ] **Step 4: Run** the same command. Expected: PASS. Then run ruff and mypy.
- [ ] **Step 5: Commit** `feat(filament): the profile-fetch caller, its runs and a type's 3DFP id (#6625)`.

### Task 2: Bambuddy client — import local presets, read a K table

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py` (operations list and two methods), `backend/scadbuddy/bambuddy/models.py`
- Test: `backend/tests/bambuddy/test_client_presets_import.py`

**Interfaces:**
- Produces: `async def import_local_presets(self, zip_bytes: bytes) -> PresetImport`. It sends a multipart `file=profiles.zip` to `POST /local-presets/import`, with operation `slicer.local_presets.import` and scope `MANAGE_LIBRARY`.
- Produces: `async def k_profiles(self, printer_id: int, nozzle_diameter: str) -> list[KProfile]`. It sends `GET /printers/{id}/kprofiles/?nozzle_diameter=`, with operation `printers.kprofiles`.
- Models: `PresetImport{success: bool, imported: int, skipped: int, errors: list[str]}` and `KProfile{slot_id, extruder_id=0, nozzle_id, nozzle_diameter, filament_id, name, k_value: str, setting_id: str | None}`. The fields match BB's `ImportResponse` and `KProfile` in `backend/tests/fixtures/bambuddy-openapi-1.2.6b1.json`.

- [ ] **Step 1: Write the failing tests** (respx): `test_import_sends_one_zip_as_multipart` (the request's part is named `file`, its filename is `profiles.zip`, and its bytes are the zip); `test_import_403_names_the_settings_permission` (the error names scope and operation, as other 403s do); `test_k_profiles_passes_the_nozzle` (query `nozzle_diameter=0.6`; the answer `{"profiles":[…],"nozzle_diameter":"0.6"}` parses); `test_new_operations_are_in_bambuddy_openapi` (an existing test checks each operation's path against the fixture; extend its table).
- [ ] **Step 2: Run** `uv run --frozen pytest tests/bambuddy/test_client_presets_import.py -n 4`. Expected: FAIL.
- [ ] **Step 3: Implement** both methods beside `create_local_preset`.
- [ ] **Step 4: Run** it again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(bambuddy): import local presets and read a printer's K table (#6625)`.

### Task 3: The import, and the worker's CRUD routes

**Files:**
- Create: `backend/scadbuddy/filament/profile_fetch.py` (the store, the file check, the import), `backend/scadbuddy/api/crud_profile_fetch.py`
- Modify: `backend/scadbuddy/api/crud_areas.py` (`"profile-fetch": ["runs"]`, and a docstring line), the router registration in `main.py`, `filament/types.py` (`merge_presets`, `merge_k_values`: replace only the given keys, in one transaction)
- Test: `backend/tests/filament/test_profile_fetch.py`, `backend/tests/api/test_crud_profile_fetch.py`; fixture `backend/tests/profile_fetch/site/export-h2c-0.4.json` (a filament preset in the shape a Bambu Studio user preset has: `name` `"Inland PLA+ @BBL H2C"`, `inherits`, `filament_id` `"P1a2b3c4"`, `from` `"User"`, `compatible_printers`)

**Interfaces:**
- Consumes: Task 1's wire models; Task 2's `import_local_presets` and `k_profiles`; `slicing/held_presets.py` `refresh_held_presets(store, client)`; `ContentStore.put`; `store/refs.py` `hold`.
- Produces: `class ProfileFetches` with `open(type_id: UUID, *, actor: str) -> tuple[ProfileFetch, bool]` (the run, and whether it joined one), `latest(type_id) -> ProfileFetch | None`, `gaps(type_id) -> list[MissingKey]` (the targets minus the type's preset keys, computed live).
- Produces: `async def store_file(state, run_id: UUID, sha256: str, printer_model: str, nozzle: str, body: bytes) -> None` and `async def import_run(state, run_id: UUID, key: str, failures: list[ExportFailure]) -> ImportOutcome`.
- Produces: the public model `ProfileFetch{id, type_id, tdfp_id, status, targets, missing, k_values, notes, error, started_at, finished_at}`.
- Routes (area `runs`): `PUT /api/v1/internal/crud/profile-fetch/runs/{run_id}/files/{sha256}?printer_model=&nozzle=` → 204; `POST …/runs/{run_id}/import` (body `{failures: list[ExportFailure]}`, `Idempotency-Key` required, 428 without) → `ImportOutcome`; `POST …/runs/{run_id}/failed` (`FailedAsk`) → 204.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_file_is_stored_as_a_blob_held_by_its_run`: after the PUT, `blob_refs` has `(key, 'profile_fetch', run_id)` and `files` names the key and the preset name.
  - `test_a_file_is_refused_unless_valid`, parametrised: a wrong sha256 → 422, more than 512 KiB → 413, not JSON → 422, no `name` → 422, a target the run lacks → 422, a run not `running` → 409 `not_running`.
  - `test_another_caller_gets_403` (the `intake` credential on these routes).
  - `test_import_posts_one_zip_and_sets_the_type_presets`: respx answers BB import `{success:true, imported:4, skipped:0}` and `GET /local-presets/` with four ids; the type's rows are `("H2C","0.2".."0.8")` with those ids and names; the status is `imported`.
  - `test_a_name_bb_already_has_maps_to_its_id`: BB answers `skipped:1`; the key still gets the listed id (Review Focus 3).
  - `test_a_missing_key_makes_the_run_partial`: there are 3 files and one `ExportFailure("H2C","0.2","3DFP offers no 0.2 mm nozzle for H2C")`; the status is `partial` and `missing` holds that key and reason (Review Focus 4).
  - `test_other_keys_are_kept`: a type row for `("X1C","0.4")` is still there after the import (Decision 10).
  - `test_k_values_need_an_exact_non_gf_match`: a profile with `filament_id` `"P1a2b3c4"` sets a K row (`extruder`, `cali_idx=slot_id`, `nozzle_type=nozzle_id`); one with `"GFL99"` sets none; two matches with different `k_value` set none; a printer whose kprofiles answers 502 adds a note.
  - `test_bb_refusing_the_import_fails_the_run`: a 403 gives status `failed` with an `error` that names the permission, and no preset rows.
  - `test_import_twice_with_one_key_imports_once`: BB's import is called once.
  - `test_the_held_copy_is_refreshed_after_import`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/filament/test_profile_fetch.py tests/api/test_crud_profile_fetch.py -n 4`. Expected: FAIL.
- [ ] **Step 3: Implement.** In `import_run`, read each held blob, build the zip in memory, name each entry `<sha256>.json` (never a name from the site), import it, list the local presets, take each file's preset name to its id among `filament` presets, call `merge_presets` and `merge_k_values`, refresh the held copy, and write the outcome. It answers the stored outcome for a run already ended.
- [ ] **Step 4: Run** it again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): import a fetched run's 3DFP profiles as the type's presets (#6625)`.

### Task 4: Start and read a fetch (public routes, the confirm hook)

**Files:**
- Modify: `backend/scadbuddy/api/filament.py` (two routes), `backend/scadbuddy/filament/profile_fetch.py` (`FetchStarter`, after `orders/storefront/intake.py` `TemporalChecker`), `backend/scadbuddy/api/filament_purchasing.py` (`confirm_invoice` starts fetches), `filament/invoices.py` (`confirm` also returns the ids of the types it created)
- Test: `backend/tests/api/test_filament_profile_fetch.py`, `backend/tests/api/test_filament_purchasing.py`

**Interfaces:**
- Produces: `POST /api/v1/filament/types/{type_id}/profile-fetch` → 202 `ProfileFetchStarted{run: ProfileFetch, joined: bool}`. It answers 404 for no such type, 422 `no_tdfp_id` for a type without one, and 422 `no_printer_models` when no SB printer has a model. Summary: "Fetch a spool type's slicer presets from 3DFP". The description says what it does, that a running fetch is joined, and the errors.
- Produces: `GET /api/v1/filament/types/{type_id}/profile-fetch` → `ProfileFetchStatus{run: ProfileFetch | None, gaps: list[MissingKey]}`. Summary: "Read a spool type's 3DFP preset fetch".
- Produces: `async def start_profile_fetches(state, type_ids: Sequence[UUID], *, actor: str) -> None`. It logs a failure and never raises one.

- [ ] **Step 1: Write the failing tests:** `test_start_opens_a_run_and_starts_the_workflow` (a fake starter records `FETCH_WORKFLOW`, id `profile-fetch:<type>`, queue `profile-fetch`, and a `FetchAsk` whose targets are `[{"H2C", ["0.2","0.4","0.6","0.8"]}]`); `test_a_second_start_joins_the_running_fetch` (`joined: true`, the same run id, and one start call; Review Focus 2); `test_targets_follow_the_nozzles_setting`; `test_no_tdfp_id_is_422`; `test_no_printer_model_is_422`; `test_status_lists_the_gaps`; `test_confirm_starts_a_fetch_for_each_new_type_with_a_tdfp_id` (a new type without a `tdfp_id` and an existing type start none); `test_a_start_failure_never_fails_the_confirm`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/api/test_filament_profile_fetch.py tests/api/test_filament_purchasing.py -n 4`. Expected: FAIL.
- [ ] **Step 3: Implement** the routes, the starter (`USE_EXISTING`, `ALLOW_DUPLICATE`) and the confirm hook.
- [ ] **Step 4: Run** it again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): start and read a type's 3DFP preset fetch (#6625)`.

### Task 5: The 3DFP driver

**Files:**
- Create: `backend/scadbuddy/profile_fetch/site.py`
- Create: `backend/tests/profile_fetch/site/` (`login.html`, `my-spool.html`, `filament.html`, `checkpoint.html`, a small server, and `export-h2c-0.4.json` from Task 3), `backend/tests/profile_fetch/test_site.py`
- Modify: `backend/pyproject.toml` (`[dependency-groups] profile-fetch = ["playwright==<exact>"]`, and the marker `requires_chromium`, which skips without Playwright's Chromium and fails when `SCADBUDDY_TEST_CHROMIUM_REQUIRED=1`), `uv.lock`

**Interfaces:**
- Produces: `class ThreeDfpSite` with `async def __aenter__/__aexit__`, `async def login(self) -> None`, `async def ensure_in_my_spools(self, tdfp_id: int) -> None`, `async def export(self, tdfp_id: int, printer_model: str, nozzle: str) -> ExportedFile`, where `ExportedFile{name: str, text: str}`. The constructor is `ThreeDfpSite(base_url: str, email: SecretStr, password: SecretStr, profile_dir: Path, headless: bool = False)`.
- Errors: `class SiteError(Exception)` with `kind: Literal["login_failed","tdfp_not_found","bot_check","option_missing","timeout"]` and a message that never holds either secret.
- Constants: `USER_AGENT` (copied from `3dfp.mjs`), `SITE = "https://3dfilamentprofiles.com"`.

- [ ] **Step 1: Write the failing tests** (`requires_chromium`; every test serves the replica on `127.0.0.1` and passes its URL as `base_url`): `test_login_fills_the_form_and_reaches_my_spools`; `test_a_failed_login_logs_no_secret` (the replica answers with `/login` again; this raises `SiteError("login_failed")`, and neither value is in `caplog.text` or `str(error)`; Review Focus 1); `test_add_clicks_add_to_my_spools_once`; `test_export_picks_slicer_printer_and_nozzle_and_reads_the_blob` (it returns the replica's JSON for `H2C`/`0.4`); `test_an_option_the_dialog_lacks_is_reported` (`0.2` absent → `SiteError("option_missing")`; Review Focus 4); `test_a_checkpoint_that_never_clears_is_bot_check`; `test_a_redirect_to_login_logs_in_again`; `test_the_user_agent_is_not_headless` (the replica echoes `navigator.userAgent`; the value equals `USER_AGENT`).
- [ ] **Step 2: Run** `uv run --frozen --group profile-fetch pytest tests/profile_fetch/test_site.py` after `uv run --group profile-fetch playwright install chromium`. Expected: FAIL.
- [ ] **Step 3: Implement.** Port `3dfp.mjs`'s `login`, `add` and `export` steps: the same selectors, the same `addInitScript` blob hook, and the per-spool page `/my/spools/details/<id>`. Use `launch_persistent_context(profile_dir, headless=headless, user_agent=USER_AGENT, chromium_sandbox=True)`. Fill the password with `page.fill`, and never pass it through `evaluate`, a log or an exception message. Turn tracing and HAR recording off.
- [ ] **Step 4: Run** it again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): drive 3dfilamentprofiles.com like 3dfp.mjs, in Python (#6625)`.

### Task 6: The worker process

**Files:**
- Create: `backend/scadbuddy/profile_fetch/settings.py`, `activities.py`, `workflow.py`, `worker.py`, `__main__.py`
- Test: `backend/tests/profile_fetch/test_settings.py`, `test_activities.py`, `test_workflow.py` (`requires_temporal`), `test_worker.py`

**Interfaces:**
- Consumes: Task 1's wire, Task 3's routes, Task 5's `ThreeDfpSite`.
- Produces: `ProfileFetchSettings` (prefix `SCADBUDDY_`): `api_internal_url`, `service_token_file | service_bootstrap_file` (exactly one), `temporal_address`, `temporal_namespace`, `temporal_task_queue_profile_fetch = DEFAULT_QUEUE`, `tdfp_email: SecretStr`, `tdfp_password: SecretStr` (both required), `profile_fetch_work_dir: Path = /tmp`, `log_level`. `check_environment` refuses `FORBIDDEN_ENV`.
- Produces: `ProfileFetchActivities.export(ask: FetchAsk) -> ExportReport`, which heartbeats and PUTs each file as it comes; `.import_(run_id: str, report: ExportReport) -> ImportOutcome`, with key `activity_key()`; `.failed(run_id: str, reason: str) -> None`.
- Produces: the workflow `ProfileFetch.run(ask: FetchAsk) -> ImportOutcome`, with Decision 13's timeouts and retries.
- `/healthz` and `/metrics` on 9090. The metric is `scadbuddy_profile_fetch_exports_total{outcome}`.

- [ ] **Step 1: Write the failing tests:** `test_refuses_to_start_with_a_database_secret`; `test_refuses_to_start_without_the_3dfp_login`; `test_settings_repr_holds_no_secret`; `test_export_puts_each_file_and_reports_failures` (with a fake site: 4 nozzles, one `option_missing`, so 3 PUTs and one failure); `test_login_failed_is_non_retryable`; `test_failed_reason_carries_no_secret` (Review Focus 1); `test_the_workflow_imports_after_export` (Temporal test env, activities mocked by name); `test_no_worker_fails_the_run` (the export activity times out on `schedule_to_start` and `failed` posts "no profile-fetch worker picked the fetch up"; Review Focus 5); `test_worker_imports_no_database_code`; `test_worker_waits_for_its_areas` (as intake's `wait_for_areas`).
- [ ] **Step 2: Run** `uv run --frozen pytest tests/profile_fetch -n 4`. Expected: FAIL.
- [ ] **Step 3: Implement** after `intake_worker/worker.py`. At start, the worker logs in once (`SiteError("login_failed")` → exit 1 with "3DFP login failed", without the secret). It runs one activity at a time (Decision 16).
- [ ] **Step 4: Run** it again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): the profile-fetch worker on its own queue, with no database secret (#6625)`.

### Task 7: The image, CI and the deploy pin

**Files:**
- Modify: `Dockerfile` (stage `profile-fetch`, Decision 4), `.github/workflows/build-image.yml` (job `profile-fetch`: target `profile-fetch`, `platforms: linux/amd64`, `type=gha,scope=profile-fetch,mode=min`, the mirror driver opts, image `ghcr.io/eh-homelab/scadbuddy-profile-fetch`), `.github/workflows/ci.yml` (job `profile-fetch-image` in the `image` lane: it builds the target without push, then runs `docker run --rm --network none <img> xvfb-run -a python -m pytest tests/profile_fetch/test_site.py` with `SCADBUDDY_TEST_CHROMIUM_REQUIRED=1`; it is in `summary`'s `needs` and its lane check), `.github/workflows/deploy.reusable.yml` (`PROFILE_FETCH_IMAGE`, `MANIFEST_PROFILE_FETCH: applications/scadbuddy/scadbuddy-profile-fetch.yaml`, resolved and pinned like the slicer: optional until the file is in clusters), `.github/scripts/deploy-pin.test.sh` if it lists the images, `.dockerignore` if the test files are excluded
- Test: the jobs themselves; `bash .github/scripts/lint-buildx-mirror.sh`; `actionlint`; `hadolint Dockerfile`

- [ ] **Step 1: Build locally** with `docker build --target profile-fetch -t sb-profile-fetch .`. If the local daemon hangs (CLAUDE.local.md), use a temporary branch with a push-triggered workflow on a hosted runner, then delete the branch. Expected: the image runs `python -c "import scadbuddy.profile_fetch.worker"` as uid 10001.
- [ ] **Step 2: Run** the driver test in it with `--network none`. Expected: PASS. Run `tini -- xvfb-run -a python -m scadbuddy.profile_fetch` with no env. Expected: exit 1, naming the missing settings.
- [ ] **Step 3: Lint:** `actionlint`, `hadolint --config .hadolint.yaml Dockerfile`, `bash .github/scripts/lint-buildx-mirror.sh`, and the `.github/scripts/*.test.sh` suites. Expected: clean.
- [ ] **Step 4: Commit** `ci: build and test the profile-fetch image, and pin it on deploy (#6625)`.

### Task 8: Agent tools

**Files:**
- Modify: `agent/src/tools/filament.ts` (`filament_type_fetch_profiles`, `filament_type_profile_fetch`; `tdfp_id` in `filament_type_upsert`'s fields), `agent/src/tools/coverage.ts`, `agent/src/sessions/touched.ts` (the fetch touches the type), the tool registry if it lists names
- Test: `agent/src/tools/filament.test.ts`, `agent/test/touched.test.ts`

- [ ] **Step 1: Write the failing tests:** each tool's route, method and input schema (`type_id` uuid); the start tool is a write tool that needs approval as `filament_type_set_presets` does; its result names `run.id`, `status` and `joined`; the read tool returns `gaps`; coverage has no uncovered route; `touched` names the type.
- [ ] **Step 2: Run** `cd agent && pnpm test`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm typecheck && pnpm lint && pnpm test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): fetch and read a type's 3DFP presets (#6625)`.

### Task 9: UI

Load `frontend-design` first.

**Files:**
- Modify: the spool type form (a "3DFP id" number field, with a link to `https://3dfilamentprofiles.com/filament/details/<id>` when it is set), the type's presets panel ("Fetch from 3DFP" button, the latest run's status, its missing keys and its notes; the panel reads the status again every 10 s while the run is `running`), Settings → Printing → Filament (`profile_fetch_nozzles` as four checkboxes), `frontend/src/api` hooks, `frontend/src/mocks` handlers and fixtures
- Test: vitest beside each component; one Playwright case in `e2e/filament-types.spec.ts` (or the existing type spec): set a 3DFP id, press Fetch, and see "Running", then "Imported" with four nozzle rows

- [ ] **Step 1: Write the failing tests:** the button is disabled without a 3DFP id and says why; a joined start shows the running run; a `partial` run lists its missing keys in plain words; the nozzles setting saves and reloads.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest <files>` (after `pnpm gen:api`). Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm typecheck && pnpm lint && pnpm test`, then the Playwright case with `--workers=2`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(frontend): fetch a spool type's presets from 3DFP (#6625)`.

### Task 10: Docs and skills

**Files:**
- Modify: `README.md` (a "Profile-fetch worker (#6625)" section under "Deploying", after "Intake worker", with the env, the secret, the pod security of Decision 6, the egress of Decision 14 and the deploy pin), `backend/CLAUDE.md` (a layout line for `profile_fetch/`, `filament/profile_fetch.py` and `api/crud_profile_fetch.py`; the `requires_chromium` marker), `docs/security/trust-boundaries.md` (the worker in the zone diagram and its list: untrusted web content, CRUD credential only), `plugins/scadbuddy/skills/filament/SKILL.md` ("Profiles": rule 5 now sets `tdfp_id`; confirm starts the fetch; the session reads `filament_type_profile_fetch` and reports gaps; the end check of spec §9 step 8), `.claude/skills/filament-intake/SKILL.md` (step 6 points to the worker for SB types), spec §9 "As built in #6625" note (Decisions 1, 9, 11, 12), this plan's "Decisions made while building"
- Test: `bash .github/scripts/lint-plugin.sh`

- [ ] **Step 1: Write** the docs. Each external fact has a link, and each measured fact says where and how.
- [ ] **Step 2: Run** `bash .github/scripts/lint-plugin.sh`. Expected: PASS. Keep the plugin version bump that the hook makes.
- [ ] **Step 3: Commit** `docs(filament): the profile-fetch worker as built (#6625)`.

### Task 11: eh-homelab/clusters PR (a separate PR in that repo)

Branch from clusters' `main`. Copy the shape of `applications/scadbuddy/scadbuddy-intake.yaml`. Do not open a PR on any third-party repo.

**Files (clusters):**
- Create: `applications/scadbuddy/scadbuddy-profile-fetch.yaml`:
  - ServiceAccount `scadbuddy-profile-fetch` with `automountServiceAccountToken: false`.
  - Deployment `scadbuddy-profile-fetch`, with `replicas: 1` and `strategy: Recreate`, and image `ghcr.io/eh-homelab/scadbuddy-profile-fetch:<tag>@sha256:<digest>` (deploy pins it after this).
  - Pod `securityContext`: `runAsNonRoot`, uid and gid 10001, seccomp `Unconfined`, and AppArmor `Localhost` `scadbuddy-intake-sandbox`. Container: `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]`.
  - Env: `SCADBUDDY_API_INTERNAL_URL=http://scadbuddy:8082`, `SCADBUDDY_SERVICE_TOKEN_FILE` (a projected token of the ServiceAccount, audience `scadbuddy`, outside `/var/run/secrets/kubernetes.io`), `SCADBUDDY_TEMPORAL_ADDRESS`, `SCADBUDDY_TEMPORAL_NAMESPACE`, `SCADBUDDY_TEMPORAL_TASK_QUEUE_PROFILE_FETCH=profile-fetch`, `SCADBUDDY_TDFP_EMAIL` (secretKeyRef `scadbuddy-3dfp`/`email`), `SCADBUDDY_TDFP_PASSWORD` (`scadbuddy-3dfp`/`password`) and `SCADBUDDY_LOG_LEVEL=INFO`.
  - Volumes: an emptyDir at `/tmp` (`sizeLimit: 1Gi`) and an emptyDir `medium: Memory` at `/dev/shm` (`sizeLimit: 512Mi`).
  - Resources: requests `250m`/`1Gi`, limit `2Gi`.
  - Probes: `/healthz` on 9090. The start probe allows 2 minutes for the login.
- Create: `applications/scadbuddy/onepassworditem-scadbuddy-3dfp.yaml`: a `OnePasswordItem` named `scadbuddy-3dfp`, with `itemPath: "vaults/homelab - prod/items/3D Filament Profiles"`.
- Create or modify: the NetworkPolicy of Decision 14, and the Temporal frontend's allowlist, which gets the profile-fetch pods.
- Modify: `applications/scadbuddy/kustomization.yaml` (resources).

- [ ] **Step 1: The owner's part, stated in the PR body.** The item "3D Filament Profiles" must be in `homelab - prod`. It needs a `password` field (it exists) and a new text field `email` that holds the account email. The pod stays in `CreateContainerConfigError` until both exist.
- [ ] **Step 2: Run** `kustomize build` with the pinned v5.6.0, and clusters' own lint. Expected: clean.
- [ ] **Step 3: Open** the PR. Its title is `feat(scadbuddy): profile-fetch worker for 3DFP presets (ScadBuddy#6625)`. Its body links ScadBuddy#6625 and lists the owner's 1Password step.

### Task 12: Check after the rollout (read-only)

- [ ] **Step 1:** Confirm the rollout by the pod's image: `GOGC=off /usr/bin/kubectl -n bambuddy get pod -l app=scadbuddy-profile-fetch -o jsonpath='{..image}'` shows the pinned digest. The pod is Ready, and that means the login succeeded.
- [ ] **Step 2:** Read the worker's log for "polling profile-fetch". Check that it holds no line with `@` or the password's length. Read `GET /api/v1/internal/crud/profile-fetch/areas`, which lists `runs`.
- [ ] **Step 3:** The first real fetch is the owner's, at their next invoice import or by the button. Afterwards, read `GET /api/v1/filament/types/{id}/profile-fetch` (GET only). Expected: `status: imported` and `gaps: []`. Add what you measured to this plan's "Decisions made while building".

## Decisions made while building (#6625)

(The builder adds one numbered line per decision, each with its reason.)

1. Task 1 widened CRUD_PATH in auth/routes.py from `[a-z]` to `[a-z-]`, so `profile-fetch` routes classify as `crud.profile-fetch`.
2. `profile_fetch_nozzles` is env-seeded, so it is in SettingsPatch, the Settings response and APPLIES "live"; it needs at least one nozzle and no duplicates, because an empty list would make the worker fetch nothing without saying so.
3. `test_profile_fetch_caller_has_its_scope` stays beside `test_every_route_has_a_class`: it pins the exact scope name, which the generic test does not.
4. `KProfile.extruder_id` defaults to 0 and `PresetImport` counts default to 0 and its errors to `[]`, so a Bambuddy answer that leaves them out still parses; every other `KProfile` field is required, because a K row without its filament or value is useless to the worker.
5. `k_profiles` returns the rows (`list[KProfile]`) and a private `KProfileTable` model reads Bambuddy's `{"profiles": [...], "nozzle_diameter": ...}` envelope, so the worker never sees the envelope.
6. `k_profiles` uses scope `READ_STATUS` (it reads a printer's state) and `import_local_presets` uses `MANAGE_LIBRARY` (as the brief says, and as `create_local_preset` does).
7. The openapi test checks only that the two paths and methods exist in the 1.2.6b1 fixture, because the fixture has paths but no component schemas; the model fields follow the brief.
8. A run's files are kept with `orders/attachments.py` `keep_file` on `state.store.blobs` (key `attachment-<sha256>`), not `ContentStore.put`: `state.store.content` is None on `store_backend` local, and the blob store works on every backend, as an invoice's file does.
9. `files` is nested, `{printer_model: {nozzle: {key, name, sha256}}}`, so no separator is needed between a model and a nozzle; the sha256 names the zip entry (`<sha256>.json`), and identical bytes for two keys go into the zip once.
10. A file sent again for a key replaces that key's entry, and the run's hold of a blob that no key names any more is dropped in the same transaction, so a retry leaves no orphan hold.
11. `ProfileFetches.open` locks the type's row, so two opens join one run before the partial unique index is ever reached; it refuses with `FetchRefusedError` codes `type_not_found`, `no_tdfp_id` and `no_targets` (no printer has a model), which Task 4 maps to HTTP.
12. `ProfileFetches` takes the nozzle setting as a callable asked on each use (`profile_fetch_nozzles` is a live setting), and `gaps` gives each gap the reason of the type's last run, else "no preset for this printer and nozzle yet".
13. A Bambuddy 4xx on the import (403 without the settings permission, 409 with no Bambuddy configured) ends the run `failed` with its detail; a 5xx or a transport error raises and records nothing, so the worker's retry with the same key imports again, and Bambuddy's `skipped` then maps the names to their ids.
14. The run ends `failed` when no preset was set (no file at all, or no imported name listed), `partial` when any target key is missing, else `imported`. A target with neither a file nor an `ExportFailure` is missing with "the profile-fetch worker sent no file for it"; a failure for a key the run lacks, or for a key that has a file, is ignored.
15. The import's Bambuddy calls run outside a transaction; its writes (preset and K merges, the run's end, the ledger row) run in one, under the run's row lock. An import that finds the run ended there answers the stored outcome and writes nothing, so two imports at once set the presets once.
16. K values are read only for a mapped file whose `filament_id` is a string not starting with `GF`, so a printer is never asked for a nozzle that cannot match. A merge replaces only the `(printer, extruder, nozzle)` keys that matched; a key with no match keeps what it had, because no match is no information, not a zero.
17. Any `ApiError` from `k_profiles` (a 403 too) is a note naming the printer and nozzle, never a failure (ruling for Task 3). A K value outside `numeric(6,4)`'s 0 to under 100 is no match.
18. The kept copy is refreshed only when `spool_overlay_refresh_seconds` is not 0: with the refresh off the API forgets the copy at start (`slicing/component.py`), and a copy made once would then go stale. A failed refresh is a note.
19. The file route answers 404 `run_not_found` and 409 `not_running` as problems with a `code` extension (type `https://scadbuddy.dev/problems/profile-fetch/<code>`); `failed` on an ended run is 409 too (Decision 7).
