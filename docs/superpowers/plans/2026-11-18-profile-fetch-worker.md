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
The API reads the blobs, checks them, strips their G-code, posts them as one zip to Bambuddy's `POST
/local-presets/import`, maps the names to local preset ids a fetch made, sets the type's presets for the
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
5. **Headed Chromium under Xvfb, with a normal Chrome user agent.** The filament-intake
   skill says headed passes Vercel's check (nothing in #6625 measured it against the real site), and
   headless with the `HeadlessChrome` UA gets 403 since 2026-11-01. The UA string is
   `3dfp.mjs`'s. The profile directory is on the pod's emptyDir. The worker logs in at start
   (a bad secret fails the start, not the first fetch), and again when a page redirects to
   `/login`. It keeps `3dfp.mjs`'s in-page blob hook (`window.__dl`), because the measured
   crash is on the blob download.
6. **Chromium's own sandbox stays on.** There is no `--no-sandbox`. Its user namespaces need
   the pod's seccomp `Unconfined` and the Localhost AppArmor profile
   `scadbuddy-intake-sandbox` (`unconfined` plus `userns,`), which the nodes already load for
   the intake sandbox. (Amended 2026-11-20, #6779: the pod now runs its own Localhost
   profiles, seccomp `scadbuddy/profile-fetch.json` and AppArmor `scadbuddy-profile-fetch`;
   see `docs/security/trust-boundaries.md`, "Browser and parser pod profiles".) Why: a renderer exploit must not reach the process that holds the
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
   id is `profile-fetch:<run_id>`), `type_id` (FK, ON DELETE CASCADE), `tdfp_id`, `targets
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
    `setting_id`. A printer that does not answer is a note, not a failure. Amended by R-F4
    (build Decision 113): a fetch replaces the K rows of every key it fetched. An old K row
    for a fetched `(printer, extruder, nozzle)` key that the new preset does not match is
    removed, and the run's notes name each removed row. A printer whose K table did not
    answer keeps its rows.
12. **Who starts a fetch.** Invoice confirm starts one for each type it created that has a
    `tdfp_id` and no preset rows. It starts after the commit; a failure to start is logged
    and never fails the confirm. `POST /api/v1/filament/types/{id}/profile-fetch` starts one
    by hand. A start while one runs joins it (`WorkflowIDConflictPolicy.USE_EXISTING`, and the
    partial unique index), and the answer says `joined: true`.
13. **Failures.** The export activity has `start_to_close` 20 min, `heartbeat` 2 min,
    `schedule_to_start` 6 h (it caps queued work: one worker exports one run at a time) and 3
    attempts from 1 min. A run no worker serves is ended by the API with "no profile-fetch
    worker picked the fetch up" (replaced by build Decisions 115 and 119: that text,
    `NO_WORKER`, only when no worker polls the queue; the export's queue cap records
    `NOT_STARTED_IN_TIME` and the last resort `TOO_LONG`): on a read or a start, once the run is 10 min old and no
    worker polls the `profile-fetch` queue, or, as a last resort, once it is older than the
    export's `schedule_to_start` plus the export's and the import's schedule-to-close
    (replaced by build Decisions 62 and 67: the workflow's 24 h execution timeout plus 1 h). A login refusal (`login_failed`), an unknown 3DFP id (`tdfp_not_found`) and a
    missing dialog option (`option_missing`) are non-retryable. A Vercel check that does not
    clear within 15 s (`bot_check`), a step that timed out (`timeout`) and a failure of the
    browser itself (`browser`: a crash, a closed page, any error the driver did not map) are
    retried, and a `browser` or `timeout` failure closes the browser so the next attempt
    launches one (build Decision 63). One
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
   existing local preset's id only when an earlier fetch of the type bound that id, with a run
   note that BB kept its old content; a preset of that name that no fetch made leaves the key
   missing (build Decisions 20 and 22). Pinned in Task 3
   (`test_a_preset_an_earlier_fetch_set_is_bound_again`,
   `test_a_hand_made_preset_of_the_same_name_is_not_bound`).
4. **A model or nozzle that 3DFP does not offer.** That key is listed as missing with its
   reason, the others are imported, and the run is `partial`. Pinned in Task 3
   (`test_a_missing_key_makes_the_run_partial`) and Task 5
   (`test_an_option_the_dialog_lacks_is_reported`).
5. **No worker deployed, or the worker is down.** The run ends `failed` with "no
   profile-fetch worker picked the fetch up" (replaced by build Decisions 115 and 119: the
   6 h queue cap records `NOT_STARTED_IN_TIME`, "no profile-fetch worker started the fetch
   within 6 h: none is running, or it is busy with other fetches", and the API's last
   resort `TOO_LONG`, "the fetch did not end within 25 h; start it again"; `NO_WORKER`
   stays for a queue no worker polls) and does not stay `running`; a busy worker
   never fails a queued run. Pinned in Task 6 (`test_no_worker_fails_the_run`, the 6 h queue
   cap) and in the API (`test_a_run_no_worker_polls_for_ends_failed`,
   `test_a_run_past_the_longest_run_ends_failed_with_pollers`, build Decision 62).

---

### Task 1: The `profile-fetch` caller, the data, and the type's 3DFP id

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_profile_fetch.sql`
- Create: `backend/scadbuddy/profile_fetch/__init__.py` (docstring only), `backend/scadbuddy/profile_fetch/wire.py`
- Modify: `backend/scadbuddy/auth/service_auth.py` (`ServiceCaller`), `crud/wire.py` (`CrudCaller`), `auth/routes.py` (scope `crud.profile-fetch`), `auth/service_credentials.py`, `auth/exchange.py` (`SERVICE_ACCOUNTS["scadbuddy-profile-fetch"]`), `api/crud_areas.py` (`"profile-fetch": []` until Task 3), `filament/models.py` (`tdfp_id` on `SpoolTypeCreate`, `SpoolTypePatch`, `SpoolType`), `filament/types.py`, `core/settings.py` + `core/setting_docs.py` (`temporal_task_queue_profile_fetch = "profile-fetch"`, `profile_fetch_nozzles`)
- Test: `backend/tests/auth/test_routes.py`, `backend/tests/crud/test_crud_port.py`, `backend/tests/filament/test_types.py`, `backend/tests/profile_fetch/test_wire.py`

**Interfaces:**
- Produces in `profile_fetch/wire.py`. It imports no database or app code. `DEFAULT_QUEUE = "profile-fetch"`, `FETCH_WORKFLOW = "ProfileFetch"`, `EXPORT_ACTIVITY = "profile_fetch_export"`, `IMPORT_ACTIVITY = "profile_fetch_import"`, `FAILED_ACTIVITY = "profile_fetch_failed"`, `AREAS = frozenset({"runs"})`, `SLICER = "Bambu Studio"`, `MAX_FILE_BYTES = 512 << 10`, `def fetch_workflow_id(run_id: str) -> str` (`"profile-fetch:<run_id>"`, build Decision 30).
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
  - `test_a_preset_an_earlier_fetch_set_is_bound_again` and `test_a_hand_made_preset_of_the_same_name_is_not_bound`: BB answers `skipped:1`; the key gets the listed id only when an earlier fetch bound it, else it is missing (Review Focus 3, build Decision 20).
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

- [ ] **Step 1: Write the failing tests:** `test_start_opens_a_run_and_starts_the_workflow` (a fake starter records `FETCH_WORKFLOW`, id `profile-fetch:<run id>` (build Decision 30), queue `profile-fetch`, and a `FetchAsk` whose targets are `[{"H2C", ["0.2","0.4","0.6","0.8"]}]`); `test_a_second_start_joins_the_running_fetch` (`joined: true`, the same run id, and one start call; Review Focus 2); `test_targets_follow_the_nozzles_setting`; `test_no_tdfp_id_is_422`; `test_no_printer_model_is_422`; `test_status_lists_the_gaps`; `test_confirm_starts_a_fetch_for_each_new_type_with_a_tdfp_id` (a new type without a `tdfp_id` and an existing type start none); `test_a_start_failure_never_fails_the_confirm`.
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
- Errors: `class SiteError(Exception)` with `kind: Literal["login_failed","tdfp_not_found","bot_check","option_missing","timeout","browser"]` (`browser` added by build Decision 44) and a message that never holds either secret.
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
  - Pod `securityContext`: `runAsNonRoot`, uid and gid 10001, seccomp `Unconfined`, and AppArmor `Localhost` `scadbuddy-intake-sandbox` (amended 2026-11-20, #6779: now seccomp `Localhost` `scadbuddy/profile-fetch.json` and AppArmor `Localhost` `scadbuddy-profile-fetch`). Container: `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]`.
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
11. `ProfileFetches.open` locks the type's row, so two opens join one run before the partial unique index is ever reached; it refuses with `FetchRefusedError` codes `type_not_found`, `no_tdfp_id` and `no_printer_models` (no printer has a model; `no_targets` until Task 4 renamed it to the HTTP code), which Task 4 maps to HTTP.
12. `ProfileFetches` takes the nozzle setting as a callable asked on each use (`profile_fetch_nozzles` is a live setting), and `gaps` gives each gap the reason of the type's last run, else "no preset for this printer and nozzle yet".
13. A Bambuddy 4xx on the import (403 without the settings permission, 409 with no Bambuddy configured) ends the run `failed` with its detail; a 5xx or a transport error raises and records nothing but the pre-import listing (Decision 20), so the worker's retry with the same key imports again, and Bambuddy's `skipped` presets then bind as ones the run made, because they are absent from the listing its first attempt kept.
14. The run ends `failed` when no preset was set (no file at all, or no imported name listed), `partial` when any target key is missing, else `imported`. A target with neither a file nor an `ExportFailure` is missing with "the profile-fetch worker sent no file for it"; a failure for a key the run lacks, or for a key that has a file, is ignored.
15. The import's Bambuddy calls run outside a transaction; its writes (preset and K merges, the run's end, the ledger row) run in one, under the run's row lock. An import that finds the run ended there answers the stored outcome and writes nothing, so two imports at once set the presets once.
16. K values are read only for a mapped file whose `filament_id` is a string not starting with `GF`, so a printer is never asked for a nozzle that cannot match. A merge replaces only the `(printer, extruder, nozzle)` keys that matched; a key with no match keeps what it had, because no match is no information, not a zero.
17. Any `ApiError` from `k_profiles` (a 403 too) is a note naming the printer and nozzle, never a failure (ruling for Task 3). A K value outside `numeric(6,4)`'s 0 to under 100 is no match.
18. The kept copy is refreshed only when `spool_overlay_refresh_seconds` is not 0: with the refresh off the API forgets the copy at start (`slicing/component.py`), and a copy made once would then go stale. A failed refresh is a note.
19. The file route answers 404 `run_not_found` and 409 `not_running` as problems with a `code` extension (type `https://scadbuddy.dev/problems/profile-fetch/<code>`); `failed` on an ended run is 409 too (Decision 7).
20. (Amended by Decision 123: (b) also counts the ids any earlier run of the type recorded as its import's.) A fetched file binds only to a Bambuddy filament preset of its name that (a) is absent from the listing read before the run's first import (this run made it) or (b) is an id the type's preset rows hold that an earlier fetch of the type bound (its `files` entry's `preset_id`, written when that run ended). Any other preset of that name leaves the key missing with "Bambuddy already has a preset named <name> that no profile fetch made", so the run is `partial`. Why: the name comes from an untrusted website, and binding by name alone could point the type at a preset a person made by hand (Task 3 review, S1). The before-listing is kept on the run (`bambuddy_before`, added to this branch's own unmerged migration) before the import is posted, so a retry after a lost answer still binds what its first attempt made (`test_a_retry_binds_what_its_lost_first_attempt_made`).
21. `store_file` accepts only a filament preset: `type` absent or `filament`, `inherits` a non-empty string, and no `printer_settings_id` or `print_settings_id` (else 422). It drops every top-level key whose name ends in `gcode` (case-insensitive) and `post_process`, and keeps the rest as JSON again (`clean_preset`; the preset inherits its base's G-code). The path's sha256 is checked against the sent bytes; the kept blob, its `files` sha256 and its zip entry name are the cleaned document's. `import_run` cleans each blob again before the zip (defence in depth); a blob that fails the check there makes its key missing. Why: the files come from an untrusted website, and a filament preset's `filament_start_gcode`/`filament_end_gcode` run on the printer (Task 3 review, S2).
22. When Bambuddy's import answers `skipped` > 0 and a key binds through Decision 20 (b), the run gets the note "Bambuddy kept its existing preset <name>; its content was not updated". Why: Bambuddy does not overwrite a preset of a name it has, so a re-fetch of changed 3DFP content would otherwise end `imported` while the type still slices with the old settings (Task 3 review, Minor 1).
23. `clean_preset` refuses (422) a preset file nested deeper than 32 levels (the document itself is level 1), counted with an explicit stack, not recursion; a body deep enough that `json.loads` itself raises `RecursionError` is the same 422. Why: the file comes from an untrusted website, and a deep body would otherwise answer 500 or be stored for Bambuddy to parse (Task 3 re-review ruling).
24. `clean_preset` refuses (422) NaN, Infinity and -Infinity (`parse_constant`) and a number that overflows a float to infinity, such as `1e400` (`parse_float`). Why: none of these is JSON (RFC 8259 §6), and `json.dumps` would write them back as non-JSON tokens Bambuddy may not parse (Task 3 re-review ruling; the `1e400` case was found while building it).
25. A start whose workflow Temporal does not take ends the run `failed` with "the fetch could not be started" (`ProfileFetches.fail_unstarted`), and the route answers 503. Why: a joined start never starts the workflow again (Review Focus 2), so a `running` row with no workflow would be joined forever and never end. A start that Temporal did take but whose answer was lost is the rare cost: its workflow then meets 409 `not_running` on the run.
26. The refusal code for no printer model is `no_printer_models` in `FetchRefusedError` too (it was `no_targets`), so the route passes the code through and one name is used end to end. The 422s are problems typed `https://scadbuddy.dev/problems/profile-fetch/<code>` with a `code` extension, as the CRUD routes' are (Decision 19).
27. `InvoiceStore.confirm` returns `(invoice, confirmed_now, new_type_ids)`: the ids of the types it made from proposed types, empty on a repeat. `start_profile_fetches` opens each one and skips a type without a `tdfp_id` silently (most new types have none); another refusal is logged as a warning, and any failure is logged with its traceback, never raised. A type the confirm made has no preset rows, so Decision 12's "no preset rows" needs no check of its own.
28. `ProfileFetches.open` and `gaps` read the nozzle setting before they take their connection, because the setting read takes a connection of its own (`filament/component.py` `defaults` does the same).
29. The route tests fake the Temporal client under the real `FetchStarter` (`profile_fetch.starter` patched), so they check the workflow name, id, queue and both policies; the confirm tests sit beside them in `tests/api/test_filament_profile_fetch.py` to share that fake, not in `test_filament_purchasing.py`.
30. The workflow id is per run, `profile-fetch:<run_id>`, still with `USE_EXISTING` and `ALLOW_DUPLICATE`. Why: with a per-type id, a new run started while an ended run's workflow still ran (Decision 25's lost answer, or the time between the workflow ending its run and the workflow itself ending) attached to that workflow under `USE_EXISTING`; that workflow posts to its own ended run and gets 409 `not_running`, so nothing ever ended the new run and every later start joined it (Task 4 review, I1). The database already allows one `running` run per type (Decision 11, the partial unique index), so a per-type id adds nothing; a retry of the same run's start still joins its own workflow. `TERMINATE_EXISTING` was rejected because it kills a workflow without its run being told.
31. `start_profile_fetches` starts the confirm's fetches at once (`asyncio.gather`) and bounds each Temporal start at `START_TIMEOUT_SECONDS` (10 s, `asyncio.timeout` in `start_fetch`); a start that times out ends its run `failed` as Decision 25 does, and is logged, never the confirm's failure. Why: the confirm is committed before the starts, and a hung Temporal (`rpc_timeout` does not bound a lazy client's connect, `render/submit.py`) would otherwise hold its answer (Task 4 review, M1). The route's own start stayed unbounded, as `TemporalChecker` is, until Decision 33.
32. `start_profile_fetches` logs `no_printer_models` once per confirm, not once per type, and only when the confirm made at least one type with a `tdfp_id` (the refusal is only reached for such a type, Decision 11's order). Why: a warning per type on every confirm with no printer model is noise (Task 4 review, M2).
33. The public `POST /filament/types/{id}/profile-fetch` bounds its Temporal start at `START_TIMEOUT_SECONDS` (10 s) too; a start that times out ends the run `failed` with "the fetch could not be started" and the route answers 503, as Decision 25 says (`test_a_hung_start_fails_the_run_and_answers_503`). Why: a hung Temporal would otherwise hold the request for as long as the lazy client's connect takes, while the run stays `running` with no workflow (ruling on the Task 4 review).
34. Playwright is pinned `playwright==1.64.1`: the newest release at least two weeks old on 2026-11-18 (PyPI's upload times, https://pypi.org/pypi/playwright/json: 1.64.1 on 2026-10-22, 1.65.0 on 2026-11-09). Its Chromium (build 1248, 156.0.8078.4) launched headless and headed with `chromium_sandbox=True` on the builder's WSL2 machine. The `dev` group includes `profile-fetch` (`{include-group = "profile-fetch"}`), so mypy and the driver's tests see Playwright for Python; the app image syncs `--no-dev` and still does not carry it. `uv.lock` was written with the Dockerfile's uv, 0.13.7.
35. `requires_chromium` asks a child process for `chromium.executable_path` once per test process (`functools.cache` in `tests/conftest.py`), because Playwright's sync API refuses to start inside a running event loop. It skips when Playwright or its Chromium is missing and fails when `SCADBUDDY_TEST_CHROMIUM_REQUIRED=1` (read at import, as the database URL is, because the API tests scrub `SCADBUDDY_*`).
36. The driver keeps `3dfp.mjs`'s fixed settle waits, scaled by a module `PACE` (1.0; the tests use 0.05), and adds explicit waits where a short wait would race: the load state after "Sign In" and the first `option` after a combobox opens. Why: the waits are what was measured to work on the real client-rendered site, and the replica answers at once.
37. Vercel's check is a page that answers 429 or has the title "Vercel Security Checkpoint" (the skill's SKILL.md). The driver polls the title every 250 ms for up to `BOT_CHECK_SECONDS` (15 s, Decision 13), then loads the page once more for its own status; a check that does not clear, or comes back, is `bot_check`.
38. `tdfp_not_found` is a 404 from the filament or the spool page, and also a spool page with no "Export slicer profile" button (the filament is not in My Spools). The real site's answer to an unknown id was not measured; the replica answers 404.
39. A failure inside `login` is raised as a fresh `SiteError` outside the `except` block, so it has neither `__cause__` nor `__context__`. Why: Playwright's fill error quotes the value it filled (measured with 1.64.1 against the replica's disabled field: the call log reads `- fill("<value>")`). `test_a_login_form_that_times_out_carries_no_secret` pins it beside `test_a_failed_login_logs_no_secret`.
40. `export` loads the spool page for every key (the interface exports one key per call), so a failed key leaves no open dialog for the next; on `option_missing` it presses Escape twice, as `3dfp.mjs` presses it after a failure.
41. The replica is a `ThreadingHTTPServer` in the test process (`tests/profile_fetch/site/server.py`, a package so the tests import it), stopped with `shutdown`; it records logins, adds, exports and both user agents (the header and the page's `navigator.userAgent`). Two tests beyond the brief: `test_an_unknown_id_is_tdfp_not_found` and `test_a_checkpoint_that_clears_is_passed`.
42. `ExportedFile` is a frozen dataclass, not a wire model: it never crosses a process as it is (the worker uploads its text as raw bytes, Decision 7).
43. The persistent context is launched with `accept_downloads=False`. The in-page blob hook is the only way an export is read, so the browser writes nothing outside the profile dir. A download the hook misses (the replica's `download="unhooked"` starts it with a synthetic click event) is refused by Chromium and the export ends `timeout` after `BLOB_WAIT_MS` (15 s, `3dfp.mjs`'s wait, now a named constant). Why: with Playwright's default (`True`) such a download was saved under `$TMPDIR/playwright-artifacts-*` (measured with 1.64.1: the file appeared there), and the site is untrusted content (Task 5 review, I1). `test_a_download_the_hook_misses_writes_nothing` points `TMPDIR` at a test dir and checks that no file appears.
44. Every `PlaywrightError` that is not a timeout in `ensure_in_my_spools` or `export` is `SiteError` kind `browser` (added to `Kind`), with a message that names the step ("adding 3DFP filament <id>", "exporting <model> <nozzle> mm of <id>") and holds no secret; `export` still presses Escape twice on that path (Decision 40). Why: a strict-mode violation, a closed page or a crashed renderer left the methods raw, so Task 6 could not tell a retryable failure (Task 5 review, M1). `login` keeps its own mapping (Decision 39).
45. `_at_checkpoint` and the checkpoint wait raise `SiteError("browser")` at once when the page is closed (`Page.is_closed`) or its renderer crashed (the page's `crash` event sets a flag); any other `PlaywrightError` there still means "not settled yet". Why: a closed page spun for `BOT_CHECK_SECONDS` and was reported as the retryable `bot_check` (Task 5 review, M2).
46. After a combobox opens, the driver waits `OPTION_WAIT_MS` (5 s) for its first option; a list that stays empty is `option_missing`, not a `timeout` after `STEP_TIMEOUT_MS` (30 s) (Task 5 review, M3).
47. `ExportedFile.name` is the site's download name with every `/` replaced by `-`, as `3dfp.mjs` names its file (`d.name.replace(/\//g, '-')`). Why: the name comes from an untrusted site, and Task 6 must never be handed a path separator (Task 5 review, M4).
48. **Replaced by R-F1 (build Decision 110).** A Playwright error inside `login` on a page that is closed or whose renderer crashed (`_page_gone`) is `SiteError` kind `browser`, not `login_failed`; any other Playwright error there stays `login_failed`. Why: Decision 13 retries `browser` and does not retry `login_failed`, and a crash says nothing about the login (Task 6 ruling a). `test_a_failed_login_chain_holds_no_secret` and `test_a_browser_failure_chain_holds_no_secret` walk every `__cause__` and `__context__` and check that none holds the email or the password.
49. The crash tests crash the renderer with `chrome://crash` from `about:blank`, and only on a page that has not posted the login form. Measured with 1.64.1 on the builder's WSL2 machine: from an http(s) page Chromium refuses `chrome://crash` (`net::ERR_ABORTED`); DevTools' `Page.crash` never answers, and after the login form's post the renderer hung on both instead of crashing; Playwright delivers the `crash` event only with the next call on the page; on a crashed page `title` answers `""`, `wait_for_timeout` raises "Page crashed" and `is_closed` stays false. So `test_the_crash_event_is_wired` checks the wait for Vercel's check, where only the event's flag turns that error into `browser`; it fails with the `crash` handler removed.
50. The replica's `option_delay_ms` fills an opened combobox's list after a delay (`test_a_list_that_fills_within_the_option_wait_is_used`: 800 ms within a 3 s `OPTION_WAIT_MS` exports).
51. `ProfileFetch.run` returns `ImportOutcome | None`: the import's outcome; a `failed` outcome (no presets, the reason as its one note) after `failed` posted why a step gave up; None when a CRUD call answered that the run is not running. Why: a run the API already ended (Decision 25, 409 `not_running`, or 404 `run_not_found`) has nothing to report, and posting `failed` there would only be refused again (Task 6 ruling d, `test_a_run_the_api_ended_ends_the_workflow_quietly`). The activities raise it as a non-retryable `ApplicationError` of type `NOT_RUNNING` (`"not_running"`, now in `wire.py` too, so the workflow need not import the driver); `failed` itself treats 409 and 404 as done.
52. The activity's error for a site failure (`activities.site_failure`) carries only a `SiteError`'s own text, which the driver writes without the login, with its kind as the type; any other exception from the site becomes a retryable `browser` failure with a fixed text, logged by its class name only. It is raised outside the `except` block, so it has no cause or context. Why: that message is the run's `error` and sits in Temporal's history, and an unmapped exception's text could quote what the browser was given (Review Focus 1; `test_failed_reason_carries_no_secret` makes the fake site raise an error that quotes both).
53. A key whose export is over `MAX_FILE_BYTES`, or that the API refuses with 413 or 422, is that key's failure ("the exported file is larger than 512 KiB", "ScadBuddy refused the exported file: <detail>"), not the attempt's: the other keys still go, and the import lists it as missing with that reason. Any other CRUD refusal fails the attempt.
54. At start the worker signs in after `wait_for_areas` and before it connects to Temporal. A refused login raises `StartRefusedError("3DFP login failed")` and `python -m scadbuddy.profile_fetch` exits 1; any other site failure at start is logged and the worker starts anyway (build Decision 64), and a browser that does not launch exits 1 (build Decision 65). Why: a worker whose login is refused would only fail every fetch it took; exiting lets the pod's restarts and its status show it.
55. `check_environment` also refuses a `DEBUG` that turns on a Playwright channel: an entry (comma or space separated, as the `debug` package splits them) that starts with `pw:`, or whose `*` wildcards match `pw:api`, `pw:protocol`, `pw:browser` or `pw:channel` (so `pw*`, `p*` and `*` too); an entry starting with `-` is an exclusion. Why: Playwright's debug log writes each call's arguments, a filled password among them, to stderr (Task 6 ruling c). The ruling named `pw:` and `pw*`; the wildcard rule also refuses `*` and `p*`, which turn the same channels on (decided secure by default).
56. `main` logs a refused setting by its location and pydantic's message only (`errors(include_input=False)`), never `str(ValidationError)`, which quotes the input values, and exits 2. The login fields also refuse a blank value.
57. The worker serves its own `/healthz` and `/metrics` (a copy of the intake worker's `_healthz`) rather than importing `intake_worker.worker`: that import would register the intake worker's metrics in this process. `FORBIDDEN_ENV`, `LOG_LEVELS` and `forbid_dumping` are imported from the intake worker as they are.
58. `SiteHolder` keeps one signed-in `ThreeDfpSite` for the worker's life (Decision 16): `get` opens it and signs in when there is none (closing it again when the login fails), and `drop` closes it after a `browser` or `timeout` failure (build Decision 63). The browser's profile is `<profile_fetch_work_dir>/chromium-profile`.
59. `test_no_worker_fails_the_run` runs the workflow alone (no activity worker), with `EXPORT_SCHEDULE_TO_START` shortened to 2 s through `UnsandboxedWorkflowRunner`, then starts the activities: the export is never tried (Temporal does not retry a schedule-to-start timeout) and `failed` posts `NO_WORKER` (replaced by build Decision 115: it posts `NOT_STARTED_IN_TIME`, which the test asserts). It covers a worker that is down or busy once the workflow has run. A profile-fetch worker that is not deployed at all also never runs the workflow itself, which is on the same queue: the workflow cannot end such a run, so the API does (build Decision 62).
60. Chromium is launched with `env=site.browser_env(os.environ)`, an allowlist: `PATH`, `HOME`, `LANG`, every `LC_*`, `TZ`, `TMPDIR`, every `XDG_*`, every `FONTCONFIG_*`, and `DISPLAY` and `XAUTHORITY`. Nothing else: no `SCADBUDDY_*` variable and no token-file path. Why: Playwright's default is the driver's whole environment, so the browser and every renderer held the 3DFP login in their memory, where a renderer exploit could read it (Task 6 review, I1). The ruling's list did not name `DISPLAY` and `XAUTHORITY`; they are kept because `xvfb-run` sets them for the headed browser (Decision 5), and headed Chromium does not start without them (measured with Playwright 1.64.1 on the builder's WSL2 machine: `launch_persistent_context(headless=False)` with only `PATH`, `HOME` and `LANG` failed with "Target page, context or browser has been closed"; with `DISPLAY` and `XAUTHORITY` added it launched). Neither holds a secret. The ruling's test read `/proc/<pid>/environ`, but Chromium writes its process title over that memory: every browser and renderer read as empty or as the tail of its command line, with or without the fix (measured the same way), so such a test cannot fail. `test_the_browser_gets_no_scadbuddy_environment` therefore launches the browser through a script that writes the environment it is given and then execs Chromium, and the renderers through `--renderer-cmd-prefix` with another; with `SCADBUDDY_TDFP_EMAIL`, `SCADBUDDY_TDFP_PASSWORD` and `SCADBUDDY_X` set, it fails without the allowlist (the browser held all of them) and passes with it.
61. `__main__.main` removes `SCADBUDDY_TDFP_EMAIL` and `SCADBUDDY_TDFP_PASSWORD` from `os.environ` (`settings.forget_login`) once `ProfileFetchSettings()` has read them, and also when the settings are refused. Why: Playwright's Node driver is started with a copy of `os.environ`, so it held the login too (Task 6 review, I1). `test_main_drops_the_login_from_the_environment`.
62. The export's `schedule_to_start` is 6 h, not 30 min: it caps how long queued work may wait, since one worker exports one run at a time and an invoice confirm queues many (Task 6 review, M2). "No worker" is found by the API instead (`filament.profile_fetch.end_unserved`), on `GET /filament/types/{id}/profile-fetch` and on a start (the route's and an invoice confirm's) that would join a running run. A `running` run ends `failed` with "no profile-fetch worker picked the fetch up" (replaced for (b) by build Decision 119: the last resort records `TOO_LONG`) (a) when it started more than 10 min ago (`NO_POLLER_GRACE`, by the database's clock) and Temporal's DescribeTaskQueue for queue `profile-fetch`, both the workflow and the activity task queue types, shows no poller; or (b) as a last resort, when it is older than `workflow.LONGEST_RUN`: the 6 h queue wait plus the export's schedule-to-close (3 × 20 min plus the 1 and 2 min backoffs) plus the import's (5 × 10 min plus the 10, 20, 40 and 80 s backoffs), about 7 h 55 min. A queue with pollers (a busy worker) never fails a queued run before (b). The read is `QueueProbe` (`TemporalQueueProbe`, beside `FetchStarter`; the tests fake it), bounded by 5 s; a Temporal error or timeout leaves the run as it is. A start whose run was ended this way opens a new run and starts its workflow; the old workflow, if a worker ever takes it, meets 409 `not_running` (Decision 51). Why: the workflow runs on the same queue as its activities, so with no worker deployed nothing in Temporal would ever end the run (Task 6 concern 1). `test_the_queue_probe_sees_a_polling_worker` checks the real read against the Temporal dev server.
63. A `timeout` failure closes the browser too, like `browser` (`activities.FRESH_BROWSER_KINDS`), so the next attempt launches a fresh one and signs in again. A `bot_check` keeps the browser and the check's cookie in it. Why: a page can wedge with its renderer alive (Decision 49 saw one hang after the login post), and every later export would time out on it until the pod restarted (Task 6 review, M1). `test_a_browser_or_timeout_failure_opens_a_fresh_browser`.
64. At start only a refused login (`login_failed`) ends the worker with exit 1. A `bot_check`, `timeout` or `browser` failure of the start-time sign-in is logged with its kind and the driver's own text (which holds no secret), the browser is closed, and the worker starts polling; the first export signs in again under its retries. Why: a Vercel check that does not clear for a while would otherwise crash-loop the pod (Task 6 concern 5). `test_a_passing_failure_at_start_is_logged_and_the_worker_starts`.
65. Any non-`SiteError` exception from the start-time sign-in (a Chromium that does not launch, for example because its sandbox cannot start, Decision 6) is logged by its class name only and raised as `StartRefusedError("the browser did not start")` outside the handler, so `main` exits 1 with a fixed text and no traceback; a launch error's text can quote the environment (Task 6 review, M3). `test_a_browser_that_does_not_launch_stops_the_worker`, `test_main_exits_1_when_the_browser_does_not_start`.
66. When `end_unserved` ends a run `failed` (no poller, or past `LONGEST_RUN`), it also asks Temporal to cancel that run's workflow `profile-fetch:<run_id>` (`QueueProbe.cancel`, `TemporalQueueProbe` calls the handle's `cancel`), only when its own update ended the run (`fail_unserved` returns whether it did). The cancel is best effort: bounded by `PROBE_TIMEOUT_SECONDS`, and an error is logged and ignored. Why: a worker that took the workflow later would export and upload for a run the API already ended, and only meet 409 `not_running` at its first PUT, after a browser session on the real site (Task 6 reviews). `test_a_run_no_worker_polls_for_ends_failed`, `test_a_run_past_the_longest_run_ends_failed_with_pollers`, `test_a_cancel_that_fails_still_ends_the_run`, `test_cancel_asks_temporal_to_cancel_the_runs_workflow`.
67. `FetchStarter` starts the workflow with `execution_timeout=workflow.EXECUTION_TIMEOUT` (24 h), and `LONGEST_RUN` is that plus 1 h. While a worker polls, the API never fails a run before then; the no-poller rule (10 min, build Decision 62) is unchanged. Why: Decision 62's 7 h 55 min was not a bound. The export's `schedule_to_start` applies to each attempt, so three attempts may each wait 6 h in the queue ([Temporal: Schedule-To-Start Timeout](https://docs.temporal.io/encyclopedia/detecting-activity-failures#schedule-to-start-timeout)), and `failed` retries without a limit, so the API could fail a run whose workflow was still going to serve it. With the execution timeout Temporal itself ends the workflow, and the hour after it covers its last post (Task 6 reviews). The comment in `profile_fetch/workflow.py` that claimed the old bound is replaced, and the unused `EXPORT_SCHEDULE_TO_CLOSE` and `IMPORT_SCHEDULE_TO_CLOSE` are gone. `test_a_queued_run_within_the_execution_timeout_is_left_running`; the start test checks the timeout.
68. `forget_login` removes every variable whose upper-case name is `SCADBUDDY_TDFP_EMAIL` or `SCADBUDDY_TDFP_PASSWORD`, not only those exact names. Why: pydantic-settings matches names without regard to case by default (`case_sensitive=False`, [pydantic-settings docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/#case-sensitivity)), so `scadbuddy_tdfp_password` sets the login and would otherwise stay in the environment Playwright's driver inherits (Task 6 reviews). `test_forget_login_removes_every_spelling_of_the_login`, `test_the_settings_read_a_lower_case_login`.
69. The API keeps DescribeTaskQueue's answer for 30 s per `(namespace, queue)` (`POLLERS_CACHE_SECONDS`, `PollerCache`, the module's `POLLERS` shared by every probe `probe(state)` makes). A failed read is not kept. Why: each read of a fetch's status (the page polls it) asked Temporal twice; a 30 s old answer only delays a 10 min decision (Task 6 reviews). `tests/filament/test_profile_fetch_probe.py`; the real-Temporal `test_the_queue_probe_sees_a_polling_worker` passes a cache of 0 s.
70. An export activity that Temporal cancels (a cancel request, or its start-to-close timeout, which the SDK delivers to an async activity as `asyncio.CancelledError`, with `activity.cancellation_details().timed_out` set: read in temporalio 1.35.0's `activity.py`, the locked version) closes the browser as a `timeout` failure does (build Decision 63), then re-raises the cancellation. Why: the cancel lands mid-export, and the page it leaves may be wedged, so the next attempt must launch a fresh browser (Task 6 reviews). `test_a_cancelled_export_opens_a_fresh_browser`.
71. The image is three Dockerfile stages. `profile-fetch-deps` (python:3.12-slim-bookworm plus git) builds `/opt/venv` with `uv sync --frozen --no-dev --group profile-fetch --no-editable`; `profile-fetch` copies only that venv, so no source tree and no git reach the image; `profile-fetch-test` (never published) is the image with the dev group's venv and `backend/tests`. Why: the backend's lockfile pins `temporal-agent-harness` by a git commit, so `uv sync` needs git, which the browser's image does not need; `--no-editable` puts scadbuddy in the venv itself; and the driver tests need pytest and `psycopg[binary]` (`tests/conftest.py` imports psycopg), which the worker's image must not carry. The base is `python:3.12-slim-bookworm@sha256:935c3c5b…03df` (its index digest on Docker Hub, read with the registry API on 2026-11-18), as Decision 4 says.
72. `playwright install --with-deps chromium` runs as root in the `profile-fetch` stage into `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` (root-owned, world-readable). It installs the full Chromium (the worker runs it headed; the env test wraps its binary) and the headless shell, which the other driver tests launch with `headless=True` (Playwright 1.64.1 picks `chromium_headless_shell-1248` for a headless launch, seen in the CI log). The build fails when importing `scadbuddy.profile_fetch.worker` imports psycopg, or when `chromium.executable_path` is not the full build. `ENTRYPOINT` is `tini --` and `CMD` is `xvfb-run -a python -m scadbuddy.profile_fetch` (Decision 4's line, split so a one-off `docker run <image> python …` needs no display); uid 10001, `HOME=/tmp`, port 9090 (`HEALTH_PORT`).
73. ci.yml's `profile-fetch-image` job runs the driver tests in `profile-fetch-test`, not in `profile-fetch` itself, with `--security-opt seccomp=unconfined` and `--ulimit core=0`. Why: Docker's default seccomp profile refuses Chromium's user-namespace sandbox ("No usable sandbox!", measured on ubuntu-latest 2026-11-18), and the sandbox stays on (Decision 6), as the pod's seccomp Unconfined has it. The runner's `kernel.core_pattern` pipes to systemd-coredump; with a core limit, each renderer the three crash tests crash with `chrome://crash` was held for about five minutes and the `crash` event never came (3 failed, the file took 15 min); with `--ulimit core=0` (or `core_pattern=core`) all three pass in under 4 s (measured in temporary runs on ubuntu-latest, 2026-11-18). The same job checks uid 10001, no psycopg after the worker's import, and that a start with no settings exits 2 naming each missing setting (`__main__` exits 2 for refused settings, build Decision 56, not 1 as the brief said).
74. The deploy pins `ghcr.io/eh-homelab/scadbuddy-profile-fetch` exactly as it pins the slicer: resolved only when `applications/scadbuddy/scadbuddy-profile-fetch.yaml` exists in clusters and names it, one image line in that file, and a build whose continue-on-error `profile-fetch` job published nothing keeps the pin and warns. `deploy-pin.test.sh` has the slicer's cases for it.
75. The worker sets `RLIMIT_CORE` to 0, soft and hard (`worker.forbid_core_dumps`, `resource.setrlimit`), first thing in `run`, before any browser launches; Chromium and its renderers inherit the limit. Why: a node whose `kernel.core_pattern` pipes to a collector (systemd-coredump on ubuntu-latest, build Decision 73) would keep a crashed renderer's memory, where the page and the login lived, and would hold the renderer for minutes; the pod must not depend on the node's setting. The hard limit is 0 too, so no child can raise it again. `test_the_start_path_forbids_core_dumps` and `test_forbid_core_dumps_sets_both_limits_to_zero` check it (Decision 85).
76. `deploy.reusable.yml`'s `pin` job has `timeout-minutes: 150`, not 140. Why: its waits add up to 140 with the profile-fetch worker's 10-minute resolve wait (agent 10, the deploy PR 90, agent-durable 20, slicer 10, profile-fetch 10), so 140 left no time for the job's own steps.
77. `deploy-pin.test.sh` case 8a has the slicer's "file without its line" case for profile-fetch: a `scadbuddy-profile-fetch.yaml` that names another image fails with "expected exactly one profile-fetch image line, found 0". Why: the profile-fetch pin is the slicer's pin (build Decision 74), and that case was the one the slicer had and it lacked.
78. The `profile-fetch-deps` stage's two `uv sync` calls pass `--no-install-package` for `psycopg`, `psycopg-binary` and `psycopg-pool`, every psycopg package in `backend/uv.lock` (there is no `psycopg-c`). The build's check fails when any of `psycopg`, `psycopg_binary`, `psycopg_pool` or `psycopg_c` is importable, before it imports the worker. `profile-fetch-test-deps` syncs without the flags, so the test stage gets psycopg back for `tests/conftest.py`. Why: the worker has no database (#5295), and the browser's image should carry no database client library. Measured on the builder's machine with uv 0.13.7 (the Dockerfile's): `uv sync --frozen --no-dev --group profile-fetch --no-editable` with the three flags installs neither psycopg nor psycopg-pool (89 packages without them, 87 with), and `import scadbuddy.profile_fetch.worker, scadbuddy.profile_fetch.__main__` in that venv succeeds with `find_spec("psycopg")` None. The image itself was not rebuilt locally; ci.yml's `profile-fetch-image` job builds it.
79. The agent's tools are `filament_type_fetch_profiles` (`POST …/types/{type_id}/profile-fetch`, risk `write`, not gated, as `filament_type_set_presets`) and `filament_type_profile_fetch` (`GET`, risk `read`); each takes only `type_id` (a uuid) and answers the route's JSON as it is: `ProfileFetchStarted` (`run.id`, `run.status`, `joined`) and `ProfileFetchStatus` (`run`, `gaps`). A refusal (404, 422 `no_tdfp_id` or `no_printer_models`, 503) is a tool error with the backend's reason (`call.ts` `ok`). Why: the brief asks for the approval `filament_type_set_presets` has. The start's description does not say "Writes ScadBuddy only", because the import writes Bambuddy's local presets (Decision 1); it says so.
80. `filament_type_upsert` keeps its free `fields` record; its description names `tdfp_id` (a positive integer, `null` clears it) and the fetch tool that uses it. Why: the upsert's fields are the backend's `SpoolTypeIn`/`SpoolTypePatch`, which validate `tdfp_id` (Task 1); a field of its own in the tool's schema would duplicate that check.
81. `filament_type_fetch_profiles` is in `touched.ts` `TOUCHES_NOTHING`, beside the other spool-type writes, not under a new resource kind. Why: no resource kind records the filament inventory (#6261's comment there), and a `filament_type` kind would need a change to `ai_session_resources` and the lookup routes beyond this task. The tests are in `agent/test/filament.test.ts` and `agent/test/touched.test.ts` (the brief's `agent/src/tools/filament.test.ts` does not exist; the filament tool tests live in `test/`).
82. `filament_type_fetch_profiles` is risk `outward` (gated) and its description ends "Approved first.", as the other tools that write Bambuddy are (`filament_rfid_reconcile`, `filament_rfid_override_mode`). This replaces build Decision 79's `write`, not gated. Why: the import writes Bambuddy's local presets, and the controller ruled that such a write asks first (Task 8 review). `test/filament.test.ts` expects `outward` and `gated`, and runs the tool with an outward caller.
83. `filament_type_upsert`'s `fields` schema types `tdfp_id`: an integer, at least 1, at most 2147483647, or null (clears it); every other field stays free (`catchall(z.unknown())`). Its description says "a positive integer below 2^31", the API's bound (`TdfpId`, 0 < n < 2**31). This refines build Decision 80: the bound is now in the tool's schema, so a value above it is refused before the call. Why: the review's Minor 1 (a wrong value cost the model a retry).
84. `test_core_dumps_are_off_before_the_browser_launches` runs the worker's start path in a forked child (`multiprocessing`, `fork` context) that reports the limit it saw through a pipe, so the pytest process's own hard `RLIMIT_CORE` is never lowered. It fails when the `forbid_core_dumps()` call is removed (checked). Python 3.12 warns that a fork of a multi-threaded process may deadlock; the child only runs the fake start path.
85. The core-dump limit has two tests, and neither forks. `test_the_start_path_forbids_core_dumps` spies on `forbid_core_dumps` and stops `run` right after the call (a 10 s timeout turns a missing call into a failure). `test_forbid_core_dumps_sets_both_limits_to_zero` runs the function in a fresh interpreter (`subprocess.run`, `timeout=60`) and checks that it prints `(0, 0)`. It skips when the parent's hard `RLIMIT_CORE` is already 0, because the check cannot fail then. Each fails when its call is removed (checked). This replaces Decision 84's forked child, which a hung child could orphan and which had no skip for a hard limit of 0.
86. The fetch UI is one component, `components/filament/ProfileFetchPanel.tsx`, at the top of the type page's "Slicer presets" section. It reads `GET …/profile-fetch` on mount and, while the run is `running`, again every 10 s (`PROFILE_FETCH_POLL_MS`; a `pollMs` prop for tests). The next read is set after each read that finished, so a failed read does not stop the reads, and the next good one clears its error (as `PrepareColours`, #6299 review).
87. "Fetch from 3DFP" uses the saved `tdfp_id`. It is off, and says why (`aria-describedby`), while the type is not saved yet, while the 3DFP id has an unsaved edit ("Save the type first: the fetch uses the saved 3DFP id."), while the type has no saved id, and while a run is `running`. A start answered `joined: true` shows the running run with "A fetch of this type was already running, so this follows it." Why: the API fetches by the stored id, so a fetch from an unsaved one would fetch a different filament than the field shows.
88. The "3DFP id" field is in "What it is", after "Extra colours". The page checks it before the save: empty is `null`, else a whole number from 1 to 2147483647 (`TdfpId`), refused with "Enter the 3DFP id as a whole number from 1 to 2147483647." "Open on 3DFP" links to `https://3dfilamentprofiles.com/filament/details/<id>` (`tdfpUrl`, `target=_blank rel="noopener noreferrer"`) whenever the field holds a valid id, saved or not, so the person can check the id before saving it.
89. When the panel saw a run `running` and then reads an ended one (any run, not only the same id: the API may end one and start another between two reads), it calls `onEnded` once. The type page then reads the type again and replaces the draft's presets and K values with the server's, each only where the draft holds no unsaved edit of it; other unsaved fields are kept. An edited list is kept and the page says "The fetch set new presets. Your unsaved edits to them are kept: discard changes to see the fetched ones." Why: the fetch writes the presets (and K values) on the server, and the brief's e2e expects the four rows without a reload.
90. Status words: `running` "Running", `imported` "Imported", `partial` "Partly imported", `failed` "Failed", each with one line (what it exports, or how many presets were set and K values read, and when it ended). A missing key reads "H2C with a 0.4 mm nozzle: <reason>", the reason being the API's own text; notes are a list; a failed run shows its `error`.
91. The 422 refusals are said in plain words by their `code`: `no_tdfp_id` "This type has no saved 3DFP id. Enter it above and save the type.", `no_printer_models` "No printer has a model, so there is nothing to fetch presets for." with a link to Settings → Printing → Printers (`/settings/printing#printers`, an `AppLink`). A 503 shows one fixed message (Decision 99), and the panel reads the run again, because a 503 ends the run `failed`.
92. While no run is `running`, the panel shows `gaps` under "These printers and nozzles have no preset yet. Fetch from 3DFP adds them:" (`Presets missing`), one line per key in the missing-key form, and leaves out a key the last run already lists as `missing`. `gaps` is not a copy of `missing`: it is the current targets (the printers' models and `profile_fetch_nozzles`, as they stand now) minus the type's preset rows (build Decision 12, `ProfileFetches.gaps`). It grows when a printer model is added, the nozzle setting changes or a person deletes a fetched row, so an `imported` run alone does not mean the type has every preset.
93. Settings → Printing → Filament shows `profile_fetch_nozzles` as four checkboxes (0.2, 0.4, 0.6, 0.8 mm), after "RFID override mode", with its source badge and reset (it is env-seeded), saved with "Save Filament" (`HAND_LAID.filament`). The form holds it comma-joined in that fixed order (`values.ts` `nozzlesValue`, `FETCH_NOZZLES`), so the order of the clicks never makes the section dirty. None ticked is refused before the save with "Choose at least one nozzle." (Decision 2: the API needs one).
94. The mock (`mocks/features/filament/index.ts`) has no worker: a `running` run ends `imported` on its next `GET`, with one type preset per target key (Bambuddy-style numeric ids from 9000, named `<code> @BBL <model> <nozzle> nozzle`), replacing only those keys. The targets are the mock registry printers' distinct models with the mocked `profile_fetch_nozzles`. It joins a running run, refuses `no_tdfp_id` and `no_printer_models` as problems with the `code` extension, and its type routes take and check `tdfp_id`. The settings mock refuses an empty, unknown or repeated nozzle list, as `SettingsPatch` does.
95. R-T9-1: the gaps are shown whenever no run is going, the type never fetched included, as Decision 92 says. Why: after a person adds a printer model, a green "Imported" with no hint of the new model's missing presets is wrong (Task 9 review I1). Test: `ProfileFetchPanel.test.tsx` "lists the gaps no run is filling…" and "lists the gaps when the type was never fetched…".
96. R-T9-2: the `frontend/CLAUDE.md` line on the type page stays; each claim in it is true against the code.
97. R-T9-3: the run's end date uses `formClasses.ts` `when` (medium date, short time), as the other filament pages do, not `toLocaleString()`.
98. R-T9-4: each start bumps a counter; a read that began before the last start is dropped when it answers, and the next read is still set, so a slow mount read never replaces the run the start answered, and polling keeps going while that run is `running`. Test: "lets no read that started before a start overwrite its run, and keeps reading".
99. R-T9-5: a 503 on the start shows one message, "ScadBuddy could not start the fetch. Try again in a minute.", and the re-read `failed` run's `error` (the same reason in the API's words) is not shown beside it.
100. R-T9-6: the button's help is shown whenever no reason turns the button off, and says "Imports a preset for each printer model and nozzle into Bambuddy and sets it here. It replaces this type's presets for the nozzles chosen in Settings → Printing." It is the button's `aria-describedby` (the reason, when there is one). Why: a fetch replaces a hand-set preset for a fetched key (Task 9 review M4).
101. R-T9-7: the `profile_fetch_nozzles` label is "Nozzles to fetch from 3DFP" (`core/setting_docs.py`, which Settings reads), and its help says "Fetch from 3DFP", the button's words, so a person connects the setting to the button.
102. (Replaced by Decision 108.) Task 10: the README section's secret check reads the `email` field with `KEY=$(op.exe read "op://homelab - prod/3D Filament Profiles/email" 2>&1); echo ${#KEY}`, which prints a length only. Why: the owner adds the field, and a length shows it is there without printing a value (memory "op.exe secret to stderr"). The vault path form is a guess at the item's reference; the owner may use the field's own reference.
103. Task 10: the docs describe the pod security from Decisions 6 and 14 and say the clusters PR (Task 11) applies it; they give no manifest. The seccomp `Unconfined` is stated for the Chromium container only, as the brief says.
104. Task 10: the spec §9 note says step 7 is a Temporal workflow and a worker, not a "flow" as the spec's step 7 first said, and that step 8's check is the `gaps` read. The earlier text of step 7 stays, with the note below it, as the #6264 and #6267 notes do.
105. Task 10: the plugin's `filament` skill rule 5 sets `tdfp_id` on the proposed type (`SpoolTypeCreate` has it) and its end check adds a read of `filament_type_profile_fetch`. The skill tells the session never to write a preset to BB by hand to fill a gap, and never to start a fetch to test a change. Both plugin versions go from 0.1.41 to 0.1.42; the edit hook did not run for these edits, so the bump was by hand.
106. Task 10: `backend/CLAUDE.md` had the `requires_chromium` note already (Task 5). Task 10 adds the layout bullet for `profile_fetch/`, `filament/profile_fetch.py` and `api/crud_profile_fetch.py`, placed before the `store/` bullet.
107. R-T10-1: the README's `op://homelab - prod/3D Filament Profiles/email` path stays. It follows op's documented `op://<vault>/<item>/<field>` form and the plan's vault and item names. A live check timed out on 1Password authorization. The path is unchecked until someone runs the README check after the owner adds the `email` field; Task 12 has no 1Password step.
108. R-T10-2: the README's field check reads `KEY=$(op.exe read "op://…" 2>&1)`, then prints "missing" on `*ERROR*` or empty, else `${#KEY}`, then unsets `KEY`. It prints only a length or the word "missing". This replaces Decision 102's `echo ${#KEY}` (Task 10 review I1: op's error text had a non-zero length).
109. R-T10-3: all 14 findings of the Task 10 review (I1-I5, M1-M9) are fixed. Each doc says what was measured and what was not; each unsourced fact gets a link or is removed; `backend/CLAUDE.md` matches the code (`ProfileFetches` methods, `browser_env` in `site.py`); `trust-boundaries.md` names this pod as a second exception (untrusted web content, one CRUD credential, no database secret) in its target text, diagram, zone table and row; Decision 13's time bound is marked as replaced by Decisions 62 and 67.
110. R-F1 (final review I1; replaces Decision 48): only the post-login URL check gives `login_failed`: the site sent the browser back to `/login`, which is also where the site shows its own login error. In `login`, a Playwright timeout is `timeout` and any other Playwright error (a refused connection, a failed navigation, a crash) is `browser`; both are retried, and at start neither ends the worker. Why: a network blip must not crash-loop the worker or fail a whole run as a bad password. `test_a_login_to_a_closed_port_is_browser_not_login_failed` and `test_a_navigation_error_during_the_login_is_browser` check it on the replica; `test_a_failed_login_logs_no_secret` keeps a wrong password `login_failed`.
111. R-F2 (final review I2; its lock, R-F2a, is replaced by Decision 125: a lease on the run row, not a session advisory lock): the import activity's CRUD request waits `workflow.IMPORT_REQUEST_TIMEOUT` (9 min) for its answer, not the client's 10 s. The route's worst case is about 12 Bambuddy calls at the Bambuddy client's 30 s each (6 min), and 9 min is under the activity's 10 min start-to-close. The import route is idempotent per run: a run already ended answers its stored outcome and calls Bambuddy no more, and the whole import holds a per-run lock, so a concurrent second import of the run waits and then answers the first one's outcome. The lock is a session advisory lock (`pg_advisory_lock`, released by `pg_advisory_unlock`; `IMPORT_LOCK`, the run id) on a pooled connection of its own, held until the import ends (a transaction-scoped lock would end with its statement on the autocommit pool), not the ruling's `SELECT … FOR UPDATE` on the run: the import commits the run's `bambuddy_before` on its own before it posts the zip (S1, so a crash after the post still binds what it made), and a row lock held through the import would block that write. Why: a retry must never start a second parallel import, and the workflow and the run must agree. `test_import_waits_out_the_routes_worst_case`, `test_a_retried_import_of_an_imported_run_calls_bambuddy_no_more` and `test_two_concurrent_imports_of_a_run_call_bambuddy_once` (respx) check it.
112. R-F3 (final review I3): README and `trust-boundaries.md` say that the worker reads the 3DFP login from its secret and types it into the site's login form in the browser, so the browser page holds it for the login, and that no other process, log or stored file holds it (replaced by Decision 121: the stored-file claim was not measured; the docs now cite the test that measures the profile). Why: the old text said no browser process held it, which was false.
113. R-F4 (final review M1; amends Decision 11): a fetch replaces the K rows of every key it fetched. A fetched key is a mapped file's `(printer model, nozzle)` on each printer of that model, unless the printer's K table did not answer (its rows stay, and the note says it did not answer). An old K row of such a key that the new preset does not match (a `GF…` or missing `filament_id`, no exact match, or an extruder the match does not cover) is removed in the end's transaction, and the run's notes name each one ("removed the old K value … of <printer>, extruder <n>, <nozzle> mm: the fetched preset does not match it"). `test_a_stale_k_value_of_a_fetched_key_is_removed_and_named` checks it.
114. R-F5 (final review M2): a preset binds per type on purpose (S1: only presets this type's fetches made). The clash text is "Bambuddy already has a preset named <name> that no fetch of this type made", and the plugin skill says a preset another type's fetch made also gives it.
115. R-F6 (final review M3): an export that waits out `EXPORT_SCHEDULE_TO_START` posts `workflow.NOT_STARTED_IN_TIME`, "no profile-fetch worker started the fetch within 6 h: none is running, or it is busy with other fetches", and the plugin skill tells the agent to have the person check both. The API's `end_unserved` keeps `NO_WORKER` ("no profile-fetch worker picked the fetch up") when no worker polls the queue; at the last resort it records `TOO_LONG` (Decision 119). This replaces Decision 59's `NO_WORKER` for the schedule-to-start case. The plugin is 0.1.45.
116. R-F7 (final review M4): the page's blob hook refuses an export whose text is longer than `MAX_FILE_BYTES` (512 KiB; `text.length`, UTF-16 units, which a UTF-8 file never has more of than bytes) before the text crosses CDP. The driver raises the new `SiteError` kind `file_too_large`, "the exported file of <model> <nozzle> mm is larger than 512 KiB". It is not retried, and like `option_missing` it is that key's failure, reported in `missing`, while the other keys go on; the activity's byte check stays behind it. Why: the page is untrusted. The UI shows the reason text as it shows every key's reason, so no UI change is needed. `test_an_export_over_512_kib_is_refused_in_the_page` (replica) and `test_a_file_the_page_refused_is_that_keys_failure` check it.
117. R-F8 (final review M5): `tests/profile_fetch/test_replay.py` replays one recorded happy-path `ProfileFetch` history (`tests/fixtures/profile_fetch_histories/imported.json`, export then import, recorded with `SCADBUDDY_RECORD_HISTORIES=1`) with `Replayer`, as the repo's other `*_replay.py` tests do. It was recorded on the Temporal dev server (`tests.support.temporal.temporal_client`), not the time-skipping environment the ruling named: the history is the same, and the dev server is what the suite already runs. A non-deterministic edit fails it (checked by hand: scheduling another activity first gives `NondeterminismError`). Never re-record it; a later change adds a history beside it.
118. R-F9 (final review M6): `frontend/CLAUDE.md` names the nozzles setting's place as Settings → Printing → Filament.

119. R-F10 (final-fix concern 4): the API's last-resort end of a run (older than `LONGEST_RUN`) records "the fetch did not end within 25 h; start it again" (`workflow.TOO_LONG`), not `NO_WORKER`, because a worker may still poll then. `fail_unserved` takes the reason. README and the plugin skill say it.

120. R-F11 (final re-review I-1): the plan text this fix wave made false is corrected. Decision 111 names the session lock the code takes (`pg_advisory_lock`). Decision 115 names plugin 0.1.45 and the last resort's `TOO_LONG` (Decision 119). Decisions 13, 59 and 62, Decision 112 and Review Focus 5 carry "(replaced by …)" markers that name the texts now recorded. Why: a later editor who "corrects" the code to match the plan would bring back the bugs the fix wave closed.
121. R-F12 (final re-review m1; replaces Decision 112's stored-file claim): the persistent profile keeps the site's cookies, never the login. Before each launch `site.write_preferences` merges into the profile's `Default/Preferences` `autofill.enabled`, `autofill.profile_enabled`, `autofill.credit_card_enabled`, `credentials_enable_service`, `credentials_enable_autosignin` and `profile.password_manager_enabled`, all false, keeping every other key (file 0600, dirs 0700). `login` also sets `autocomplete="off"` on the email and password fields before it fills them, because Blink keeps no form state for such a field. `test_the_profile_keeps_no_login_after_a_login` logs in to the replica in headed Chromium (it needs a `DISPLAY`; CI's `profile-fetch-test` runs under `xvfb-run`) with a fake email and password, closes the browser, and reads every file under the profile dir: neither string is there as UTF-8, UTF-16-LE or URL-encoded bytes. Measured with Playwright 1.64.1's Chromium on the builder's WSL2 machine under `xvfb-run -a`: without both changes the test found the email in `Default/Web Data`, `Default/Login Data` and two `Default/Sessions` files; the preferences alone removed the first two, and autocomplete off removed the `Sessions` copies. The password was in no file in any run. Headless Chromium left neither string even without the changes, so the test runs headed. The test does not check the profile while the browser runs. README and `trust-boundaries.md` now state only what it measures. `test_the_preferences_merge_into_the_profile` checks the merge and the modes.
122. (Replaced by Decision 125: the advisory lock and `_put_back` are gone, and with them this test.) R-F13 (final re-review m2): `_import_lock` awaits `pool.getconn` (run in a thread) through `asyncio.shield`. When the request is cancelled during that wait, the thread still gets a connection, and a done callback (`_put_back`) puts it back in the pool; it holds no lock yet. `test_a_cancel_while_the_lock_waits_for_a_connection_returns_it` fails without the fix (the connection never came back).
123. R-PR1 (PR review, Blocking): as soon as `import_local_presets` succeeds, the import lists Bambuddy's local presets and records on the run, in a commit of its own (`_keep_made`, column `bambuddy_made`, added to this branch's own unmerged migration), the ids the import made: listed ids not in `bambuddy_before` whose name is one of the run's files. A retry that lists again adds what it finds. `_fetched_ids` counts every earlier run's `bambuddy_made` of the type as "made by a fetch of this type", whatever that run's status. It also covers an earlier run that kept a `bambuddy_before` but never recorded `bambuddy_made` (the post may have reached Bambuddy with every listing after it failing): a listed preset named as one of that run's files and absent from its `bambuddy_before` counts as made by it (`_Fetched.made`). Why: a run that failed after Bambuddy took the zip left presets that every later fetch saw in its own `bambuddy_before` and could never bind. The ruling's record alone cannot help when the listing after the post is what fails, which the ruling's test requires, so the unlisted case is read from the earlier run's before-listing. Its limit: a preset of that name a person made after that run's before-listing would count as fetched. `test_presets_an_unfinished_import_made_are_bound_by_the_next_run` (respx: the post succeeds, every later listing answers 502 until the run is failed, a second run binds all four presets) fails without it; `test_import_posts_one_zip_and_sets_the_type_presets` checks the record.
124. R-PR2 (PR review, Non-blocking): `filament_type_profile_fetch` is a `write` tool, because its `GET` can end a stale run `failed` and cancel the run's workflow (`end_unserved`). It is in `touched.ts` `TOUCHES_NOTHING` (the run is the type's, no recorded kind). Its description says the read can end the run and cancel the workflow, and that it writes ScadBuddy only. It is not gated: only `outward` is. `test/filament.test.ts` and `test/touched.test.ts` check it.
125. R-PR3 (PR review, Non-blocking; replaces R-F2a, Decision 111's lock and Decision 122): one run's import holds a lease on the run row, not a session advisory lock. `_claim` runs `UPDATE filament_profile_fetches SET import_lease_until = now() + IMPORT_REQUEST_TIMEOUT WHERE id = … AND status = 'running' AND (import_lease_until IS NULL OR import_lease_until < now()) RETURNING import_lease_until` (column added to this branch's own unmerged migration). A request that does not get the lease checks the ledger for its key and the run's status every `LEASE_POLL_SECONDS` (2 s) with short statements on a pooled connection it gives back at once: it answers the replay or the ended run's stored outcome, or claims the lease once it has expired. The end's transaction clears the lease; a request that ends any other way (a 5xx, a cancel) clears it in its `finally`, only if it still holds that lease (`_release` compares the value). Why: the advisory lock held one pooled connection for the whole import (up to 9 min) and a waiting retry held another. `test_two_concurrent_imports_of_a_run_call_bambuddy_once` (kept), `test_a_waiting_import_holds_no_pool_connection` (the pool's `get_stats`: while one import waits on Bambuddy and a second waits for the lease, the pool's connections in use drop back to the idle count; with the advisory lock it was the idle count plus 2) and `test_an_expired_lease_is_claimed` check it.
126. R-PR5 (PR review, Blocking; narrows R-PR1's unlisted case): when Bambuddy refuses the import, the run records that its import made nothing (`_keep_made(state, row["id"], [])`) before it ends `failed`. A refusal is `imported.success` false, or an `ApiError` below 500 from `import_local_presets` itself (a 403 when the key lacks the settings permission). A run stays unlisted (`bambuddy_before` set, `bambuddy_made` NULL) only when the outcome is unknown: a 5xx from the post, a lost answer (`map_transport` gives a 502 or 504), or a listing that failed after a successful post. Why: `_fetched_ids` read a refused run as unlisted, so a later fetch of the type counted a preset that a person made with one of that run's file names as made by a fetch, and bound it (#6625 S1). `test_a_refused_import_made_nothing_a_later_fetch_binds` (respx, for a 403 and for `success` false: run A's import is refused, then a preset named as A's file appears, and run B leaves it missing with the reason "no fetch of this type made") fails without it.
