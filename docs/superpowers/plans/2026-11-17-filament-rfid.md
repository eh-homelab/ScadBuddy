# Filament RFID: tags, overrides and NTAG (#6264) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **UI work (Tasks 9 and 10 touch the frontend):** the implementer loads `frontend-design:frontend-design` before writing any UI. Pass this line on to every UI subagent too.

**Goal:** A person re-binds RFID tags, uses a Bambu tag on another brand's spool (override), writes and reads NTAG tags through Bambuddy's SpoolBuddy, and prints spool labels with a number and a QR code. Every Bambuddy write is idempotent and never costs ScadBuddy its own record.

**Architecture:** ScadBuddy's binding is always written first, in Postgres. A one-row-per-tag queue (`filament_bb_tag_pending`) marks a tag whose Bambuddy state may differ. A state-based reconcile (a new step in the `SpoolRefresh` schedule, plus one best-effort try after a request) compares what SB wants with what Bambuddy holds and writes only the difference. Everything that calls Bambuddy runs in the API process (library worker and routes), so no worker gets a pool or a route. Labels render in the browser.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (sync, `dict_row`), pydantic v2, httpx, respx, `websockets`, Temporal Python SDK, Postgres 18; React 19, Vite, shadcn/ui, msw, vitest, Playwright; TypeScript agent tools (zod, msw).

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§3, §5.4, §7, §8.2, §11 "SpoolBuddy" and "Settings", §12 row #6264). Issue #6264 and its comment (labels belong here). Epic #6260. Plans that bind this one: `docs/superpowers/plans/2026-11-14-filament-data-model.md` (#6261, its "Decisions made while building"), `2026-11-15-filament-ui.md` (#6262), and the in-flight `2026-11-15-filament-capture.md` (#6263, PR #6590, on `origin/feat/filament-capture`).

## Global Constraints

- Database rule (CLAUDE.md "Database access (#5295)"): only the CRUD service (API process) has SQL. This plan adds no pool, no grant, no worker route. Every Bambuddy call here runs in the API process, as the BB pull does (`filament/bambuddy_pull.py` `read_and_pull`).
- Every Bambuddy write is idempotent. It reads Bambuddy's state first and writes only a difference. Its failure never rolls back or loses SB's own record: SB commits first, Bambuddy second.
- Bambuddy calls go through `bambuddy/client.py` `BambuddyClient._send`, with a `Scope` and an `Operation` for each (`bambuddy/errors.py` `Scope`). Scopes are never invented; Task 1 confirms each one.
- Migrations (#491): new files `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`. Never edit a merged file.
- Timestamps `timestamptz`; enums `text` with `CHECK`. Colour is `#RRGGBBAA`.
- One open binding per tag and per spool (§5.4). A re-bind closes the old binding and writes `tag_unbound` and `tag_bound` (`FilamentSpoolStore.bind_on`). Nothing here refuses a re-bind.
- A `SpoolRefresh` change goes behind `workflow.patched(...)`, with a new history recorded beside the old ones in `backend/tests/fixtures/spool_refresh_histories/`. Never re-record an old history.
- Every new public `/api/v1` route sets `summary=` and `description=` and gets an agent tool or a `NOT_A_TOOL` entry (`agent/src/tools/coverage.ts`). Every tool works in classic and durable mode.
- Generated API files (`backend/openapi.json`, both `schema.d.ts`) are never committed. Run `pnpm gen:api` after a route change.
- Every new BB fact gets a link; every measured fact says where and how it was measured (#259). Bambuddy facts cite the Bambuddy source tag `v1.3.1` or the recorded OpenAPI.
- Never a write to the live Bambuddy. Writes are tested with respx against recordings. Live reads are `GET` only; a key is never printed (`op.exe` writes to stderr: capture it into a variable and print its length only).
- No `pkill -f`, no `killall`, no `git stash`, no amend.
- Commands: `cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test uv run --frozen pytest <files>`, then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`; frontend `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm exec vitest run <file>`; agent `cd agent && pnpm gen:api && pnpm test <file> && pnpm typecheck`.

## Bambuddy facts this plan rests on

Measured by the planner on 2026-11-17, read-only: `GET https://bambuddy.internal.nullreference.io/openapi.json` (200, 1,245,373 bytes, `info.version` **1.3.1**; the repo's `backend/tests/fixtures/bambuddy-openapi-1.2.6b1.json` holds paths and operation ids only, no schemas) and `GET https://raw.githubusercontent.com/maziggy/bambuddy/v1.3.1/backend/app/...`. Task 1 records them in the repo. The spec names 1.2.6b1; the live instance has moved.

| Fact | Source |
|---|---|
| `PATCH /api/v1/inventory/spools/{spool_id}/link-tag`, body `LinkTagRequest {tag_uid, tray_uuid, tag_type, data_origin="nfc_link"}` (all optional, `null` means "leave as is"). A tag held by another **active** spool: 409, `detail = {"code": "tag_already_linked", "message", "spool_id": <holder BB id>, "field": "tag_uid" \| "tray_uuid"}`. A tag on an **archived** spool is cleared silently. An archived target: 400. | OpenAPI `/api/v1/inventory/spools/{spool_id}/link-tag`; `backend/app/api/routes/inventory.py` lines 2765-2845 and `backend/app/services/tag_conflict.py` `tag_already_linked`, at v1.3.1 |
| `PATCH /api/v1/inventory/spools/{spool_id}` applies `model_dump(exclude_unset=True)`, so `{"tag_uid": null, "tray_uuid": null}` clears both (`SpoolUpdate` has `tag_uid`, `tray_uuid`, `tag_type`, `data_origin`, `weight_locked`). | OpenAPI `SpoolUpdate`; `inventory.py` line 1701 and 1716, v1.3.1 |
| `GET /api/v1/inventory/spools/by-tag?tray_uuid&tag_uid&include_archived`: 404 when nothing matches; `tray_uuid` first, `tag_uid` the fallback. Accepts `inventory:read` or `inventory:update`. | OpenAPI description of the route |
| `auto_add_unknown_rfid`: a boolean of `AppSettings` / `AppSettingsUpdate`, default **true**. Read with `GET /api/v1/settings/`, written with `PATCH /api/v1/settings/`. | OpenAPI `AppSettings`, `AppSettingsUpdate`; `backend/app/main.py` lines 3393-3394 (`_auto_add_raw is None or == "true"`) |
| A settings write needs `settings:update`, which no API key can hold: a key fails with 403 while Bambuddy's auth is on (`bambuddy/errors.py` `Scope.ADMIN_SETTINGS`, `_mapped`). Auth is off on the owner's instance (`.claude/skills/filament-intake/SKILL.md`: "Write endpoints need no API key"). | repo, as cited |
| `POST /api/v1/printers/{printer_id}/slots/{ams_id}/{tray_id}/configure`: **query parameters** `tray_info_idx, tray_type, tray_sub_brands, tray_color` (RRGGBBAA, no `#`), `nozzle_temp_min, nozzle_temp_max, cali_idx, nozzle_diameter, setting_id, kprofile_filament_id, kprofile_setting_id, k_value, orca_profile_id`. It sends `ams_filament_setting` then `extrusion_cali_sel` to the printer. | OpenAPI `configure_ams_slot_...` |
| `POST /api/v1/spoolbuddy/nfc/write-tag`, body `{device_id, spool_id}` (**Bambuddy's** spool id). It queues the write and answers `{"status": "queued", "warnings"?}`. It encodes **OpenTag3D** (v1 and v2) from Bambuddy's spool (`encode_opentag3d`), not OpenSpool, and has no field for an SB id. 404 for an unknown device or spool; 503 in Spoolman mode. | `backend/app/api/routes/spoolbuddy.py` lines 827-957, v1.3.1 |
| `POST /api/v1/spoolbuddy/nfc/write-result` is posted by the SpoolBuddy daemon, not by SB. On success, in local mode, Bambuddy sets the spool's `tag_uid` (upper-case), `tag_type = "ntag"`, `data_origin = "opentag3d"`, `encode_time`, and clears the tag from any other spool. `POST /devices/{device_id}/cancel-write` cancels a pending write. | `spoolbuddy.py` lines 960-1190, v1.3.1 |
| A tag scan reaches no REST route. The daemon posts `nfc/tag-scanned`, and Bambuddy **broadcasts** `spoolbuddy_tag_matched` (`device_id, tag_uid, tray_uuid, spool{id,...}`) or `spoolbuddy_unknown_tag` (`device_id, tag_uid, tray_uuid, sak, tag_type, tag_data`) on its WebSocket `/api/v1/ws`. With auth off the socket needs no token; with auth on it needs `?token=` from `POST /api/v1/auth/ws-token`. | `spoolbuddy.py` lines 579-808; `backend/app/api/routes/websocket.py` lines 1-136; `main.py` line 12700, v1.3.1 |
| `GET /api/v1/spoolbuddy/devices` lists devices: `device_id, hostname, has_nfc, nfc_ok, last_seen, pending_command, ...`. | OpenAPI `DeviceResponse` |

Spec deviation, decided here: spec §7 says the NTAG carries OpenSpool data with the SB spool id. Bambuddy's write path cannot do that. The tag carries OpenTag3D data of the **linked Bambuddy spool**, and SB identifies the tag by its UID through its binding (kind `ntag_opentag`, which §5.4 already lists). Task 1 corrects §7.

## Decisions this plan makes

Each names its cost if wrong.

1. **SB first, Bambuddy second, state-based.** A binding change inserts a `filament_bb_tag_pending` row (one per tag, deduped) in its own transaction. The reconcile never replays a stored payload. It recomputes SB's wish and Bambuddy's state. Cost: up to one refresh interval (`spool_overlay_refresh_seconds`, default 300 s) of lag when the best-effort try after a request fails.
2. **`filament_sync.tags: "off" | "sb_to_bb"`, default `off`.** #6265 widens it to the spec's four values. Cost: the owner turns it on in Settings before SB's bindings reach Bambuddy. Chosen because a Bambuddy write path is never silently on.
3. **Override means "this Bambu tag stays on a spool of another filament".** It is an explicit flag, valid only for kind `bambu_mifare` (422 otherwise). Binding with it locks the spool's weight (one `edited` event `changes.weight_locked = [false, true]`). Unbinding never unlocks. #6262's TagPanel sent `override: true` for "Move it here"; that changes to `false`, because a move is not an override. Cost: a person unlocks the weight by hand.
4. **RFID override mode lives in `filament_rfid_state`**, a one-row table with `override_mode` and `saved_auto_add`, written only by `PUT /filament/rfid/override-mode`. It is not in `StoredSettings`, so a generic settings save cannot change it without the restore state. Cost: Settings reads it through a route.
5. **`saved_auto_add` is written before Bambuddy is touched and never overwritten while set.** A crash, a retry or a person flipping the value back never lets SB save its own `false` as "the old value". Cost: none.
6. **Moving a tag on a 409 clears the holder, then links again, once.** The holder is cleared with `PATCH /inventory/spools/{holder}` `{"tag_uid": null, "tray_uuid": null}`. The holder need not be an SB-linked spool (spec: "SB moves it"). Cost: a Bambuddy spool that a person tagged by hand loses that tag when SB's binding says it is elsewhere.
7. **An unbind in SB clears the tag in Bambuddy only when Bambuddy's holder is linked to an SB spool** (`filament_bb_links`). A holder SB does not know is left alone. Cost: a stale tag on an unknown Bambuddy spool stays.
8. **NTAG write needs a linked Bambuddy spool** (409 `no_bambuddy_spool` otherwise; creating one is #6265) and **refuses a spool with an open override binding** unless `replace: true` (one binding per spool: an NTAG would close the Bambu tag the AMS needs). Cost: one extra click.
9. **NTAG write is three calls, not a held request:** `ntag/write` queues, `ntag/confirm` reads the Bambuddy spool and binds the new UID (kind `ntag_opentag`), `ntag/cancel` cancels. The UI polls confirm every 2 s for 60 s. The pull would bind it later anyway. Cost: none.
10. **NTAG scan is a bounded WebSocket listen** (`POST /filament/rfid/scan`, at most 60 s), not a standing connection. Cost: a scan outside the window is lost; the person scans again.
11. **Override configures the slot through Bambuddy's `configure` route only** (no LAN path in SB for AMS filament settings). It runs in the reconcile, so the slot is configured within one refresh interval of the load. Cost: that lag; the print dialog never depends on it. #6590 does not change this (Task 0).
12. **Label format.** One label is 50 x 25 mm: the QR code (21 mm square, error correction M) on the left; on the right `#<number>` large, then material and colour name, then brand. The QR payload is the text `scadbuddy:spool:<uuid>`: no host in it (UI links follow the host the user is on), and the same string the later scan flow (#6268) parses. Two layouts: `roll` (one label per page, `@page size 50mm 25mm`) and `sheet` (a grid of 3 columns). The layout is remembered in `localStorage` (try/catch). It renders in the browser, in a new page `/filament/labels`, with the npm `qrcode` package. Cost: BB's label templates (custom sizes) wait for #6270's parity sweep.

## Review Focus

1. A tag is re-bound while Bambuddy is down: SB keeps the new binding, the pending row stays with `attempts` counted, and the next reconcile links it once, so Bambuddy never ends with two spools holding that tag (Task 4).
2. Bambuddy's auth is on (settings write is 403): override mode shows `blocked` with the reason, `saved_auto_add` is kept, and nothing claims that auto-add is off (Task 5).
3. A person turns `auto_add_unknown_rfid` back on in Bambuddy while override mode is on: the next reconcile turns it off again. Turning the mode off restores the value found first, never the `false` SB wrote (Task 5).
4. NTAG write for a spool with an open override binding, or with no Bambuddy link: both refused with a clear 409, and no binding changes (Task 8).
5. With auto-add still on in Bambuddy, it creates a spool for an override tag: the pull creates no second SB spool and counts it (Task 7).

---

## File structure

```
backend/scadbuddy/migrations/
  <ts>_filament_rfid.sql                 Task 2  (pending, state, slot tables; seeds)
backend/scadbuddy/filament/
  rfid_push.py     tag push: enqueue_tag, drain_tags              Tasks 2, 4
  rfid_mode.py     override mode and auto_add_unknown_rfid        Task 5
  rfid_slots.py    configure a slot for an override binding       Task 6
  ntag.py          NTAG write, confirm, cancel, scan              Task 8
  spools.py        override rules in bind_on; enqueue             Task 2
  bambuddy_pull.py  kind from data_origin; rfid_auto guard        Task 7
  models.py        RfidStatus, reports, ScanResult, Ntag*         Tasks 4-8
backend/scadbuddy/bambuddy/client.py, errors.py, models.py        Tasks 3, 8
backend/scadbuddy/core/settings.py       FilamentSync.tags        Task 2
backend/scadbuddy/spools/activities.py, workflows/spool_refresh.py, main.py   Task 4 (activity, patch)
backend/scadbuddy/api/filament.py        rfid and ntag routes     Tasks 5, 8
backend/tests/bambuddy/recordings/openapi/rfid-routes.json        Task 1
backend/tests/filament/test_rfid_*.py, test_ntag.py               Tasks 2-8
agent/src/tools/filament.ts, coverage.ts, agent/test/filament.test.ts   Tasks 5, 8
frontend/src/api/filament.ts                                      Tasks 8, 10
frontend/src/pages/filament/LabelsPage.tsx, components/filament/Label.tsx, labelSheet.ts   Task 9
frontend/src/components/filament/TagPanel.tsx, NtagPanel.tsx      Task 10
frontend/src/pages/settings/sections/filament.tsx                 Task 10
frontend/src/mocks/features/filament/rfid.ts, frontend/e2e/filament-rfid.spec.ts   Task 10
docs/superpowers/specs/2026-11-14-filament-management-design.md   Task 1 (§3, §7), Task 11
backend/CLAUDE.md, frontend/CLAUDE.md                             Task 11
```

---

### Task 0: Order of work, and the merge after #6590

No code. This note binds the branch.

PR #6590 (#6263, `origin/feat/filament-capture`) is in flight. This plan is written on main and touches the tray resolver and the refresh schedule that #6590 changes. The branch `feat/filament-rfid` **merges main after #6590 lands**, before Task 4's activity registration is finished and before the whole-branch check (Task 11). Execute Tasks 1-3, 5-10 on main as they are; they do not touch the files below.

Names #6590 changes (from its plan), and what this plan does about each:

| #6590 name | Effect on this plan |
|---|---|
| `filament/inventory.py` `FilamentInventory.holder_on(conn, printer_id, seen) -> TrayHolder` (static; `trays` calls it). Keys no binding holds fall through to the placed-spool rule. | Task 6 calls `FilamentInventory.trays(printer_id, seen)`, which is the same on both sides. Override slots are found by `found_by == "tag"`, which `holder_on` keeps. Task 6's `test_an_override_tag_resolves_through_trays` runs unchanged after the merge. |
| `place_on(conn, spool_id, location_id, seen, *, actor, at)`, `locations.ensure_ams_on`, `move_on`, `bambuddy/trays.py` `place_label` | Unused here. Slots are keyed by `(printer_id, ams_id, tray_id)`, never by a place name. |
| `filament/capture.py` (`reconcile`, `touched`, `lock`, `trays_changed`, `WATCH_ACTOR`, `BACKSTOP_ACTOR`) | Capture locks spools and never takes a tag lock. This plan's writers take the **tag first, then the spools** (`bind_on`), as before; no new lock order is added. Task 2's enqueue is an `INSERT ... ON CONFLICT` on a tag-keyed row inside `bind_on`'s transaction: it adds no spool lock. |
| `ledger.py` `PRINT_RUN`, `BB_USAGE`, `BB_REFS`, `job_of`, `refresh_derived(conn, spool_id, event=None)` | Task 2's `edited` event for the weight lock goes through `ledger.record`, which passes its event on. Nothing else here reads these. |
| `bambuddy_pull.py` `read_and_pull` reads `spool_rows()`, `spool_usage()`, `spool_catalog()`, then `archives()`; `PullReport.usage_booked`, `usage_unlinked` | Task 7 changes `_bind`'s kind rule and adds `PullReport.rfid_duplicates`. Merge both `PullReport` field lists. |
| `bambuddy/client.py` `Operation` gains `"spools.usage"`; `bambuddy/models.py` gains `as_utc`, `SpoolUsage` | Task 3 adds `Operation` entries. Merge both literals. |
| `workflows/spool_refresh.py`: new `workflow.patched("filament-capture-backstop")` step; "one new history carries both markers" | Task 4 adds `FILAMENT_RFID_PATCH = "filament-rfid"` after `_pull()`. After the merge, run `tests/test_spool_refresh_replay.py`. Record **one new history** that carries every marker present (`spool-providers-gone`, `filament-capture-backstop`, `filament-rfid`). Never re-record an old one. |
| `printers/reports.py` `advance_in` runs capture; `printers/status.py` `RUNNING`; `SeenTray.remain` | Not touched here. |

- [ ] **Step 1:** After #6590 merges, `git merge origin/main` into `feat/filament-rfid`. Resolve the `Operation`, `PullReport` and `SpoolRefresh` conflicts as the table says.
- [ ] **Step 2:** Run `uv run --frozen pytest tests/filament tests/test_spool_refresh_replay.py tests/bambuddy tests/printers` and the three commands of Global Constraints. All pass before Task 11.

### Task 1: Record the Bambuddy facts and finish the read-only measurements

**Files:**
- Create: `backend/tests/bambuddy/recordings/openapi/rfid-routes.json`
- Modify: `backend/tests/bambuddy/recordings/README.md` (one table row), `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§3 paragraph, §7 NTAG bullet)
- Test: `backend/tests/bambuddy/test_recordings_rfid.py`
- Scratch only, never committed: any script, in the session scratchpad, apart from the data it reads. Run Python with `-I`.

**Interfaces:**
- Produces `rfid-routes.json`: for each route of the Bambuddy facts table (link-tag, by-tag, `PATCH /spools/{id}`, `GET`+`PATCH /settings/`, configure, the four `nfc/*` routes, `cancel-write`, `GET /spoolbuddy/devices`, `POST /auth/ws-token`): the method, `operationId`, parameters, request schema and the schemas it references, copied from the live `/openapi.json`; plus `conflict_409` (the `tag_already_linked` detail above, from the source) and `ws_events` (`spoolbuddy_tag_matched`, `spoolbuddy_unknown_tag`, `spoolbuddy_tag_written`, `spoolbuddy_tag_write_failed`, `spoolbuddy_tag_link_failed` with their fields, from `spoolbuddy.py`). Tasks 3 and 8 build their respx bodies from it through `tests/bambuddy/conftest.py` `recorded_schema` and `shaped`.
- Produces a scope table in the spec's §3: for each route, Bambuddy's permission and the `Scope` ScadBuddy uses.

Strictly read-only: `GET` requests and a listening WebSocket that sends nothing. No `kubectl exec`. The key (when a route answers 401) comes from the vault item named in `.claude/skills/filament-intake/SKILL.md`; capture `op.exe` output (stderr) into a variable, print only its length, send it only as `X-API-Key`.

- [ ] **Step 1: Write the failing test** `test_the_rfid_recording_holds_what_the_plan_rests_on`: loads `rfid-routes.json`; asserts `LinkTagRequest` has properties `tag_uid, tray_uuid, tag_type, data_origin`; `conflict_409["code"] == "tag_already_linked"` and its keys are `code, message, spool_id, field`; `WriteTagRequest` requires `device_id` and `spool_id`; `AppSettingsUpdate` has `auto_add_unknown_rfid`; `ws_events` lists `spoolbuddy_tag_matched` and `spoolbuddy_unknown_tag`.
- [ ] **Step 2:** Run it — FAIL (no file).
- [ ] **Step 3: Measure, read-only, against `https://bambuddy.internal.nullreference.io`, and record each answer's shape (never a secret or an access code):**
  - (a) `GET /openapi.json`: extract the routes above into `rfid-routes.json`.
  - (b) `GET /api/v1/auth/status` (existing `auth.status` operation): is auth on? This decides whether override mode can write the setting (Decision 5 and Review Focus 2).
  - (c) `GET /api/v1/settings/`: the current `auto_add_unknown_rfid`. Record the value in the spec §3 paragraph and in the PR body only.
  - (d) `GET /api/v1/spoolbuddy/devices`: the device ids, `has_nfc`, `nfc_ok`. If the list is empty, say so; Task 8's tests use a recorded body.
  - (e) `GET /api/v1/inventory/spools/by-tag?tray_uuid=<one tray_uuid from the recorded inventory-spools.json>`: the 200 body, and `...?tag_uid=00000000` for the 404 body.
  - (f) WebSocket `GET /api/v1/ws` (no token if (b) says auth is off; otherwise report that `POST /auth/ws-token` is needed and what scope refuses the key): listen 15 s, send nothing, record the `type` of the first frames.
  - (g) Permissions of the routes: read the dependency of each route in `spoolbuddy.py`, `inventory.py` (link-tag, `by-tag`, `PATCH /spools/{id}`) and the configure route in `printers.py` at tag `v1.3.1` (`Permission.*`, with line numbers) and map each through `_APIKEY_SCOPE_BY_PERMISSION` in `backend/app/core/auth.py` to a ScadBuddy `Scope`. Defaults to confirm: link-tag, by-tag, clear, write-tag, cancel-write: `MANAGE_INVENTORY`; configure: `CONTROL_PRINTER`; settings write: `ADMIN_SETTINGS`; devices list: `READ_STATUS`.
  - Not measurable without a write, so taken from the source with a line number and tested only with respx: the 409 body, the clear by `PATCH` null, the configure answer, the write-tag answer. Say that in the paragraph.
  If a fact does not hold, stop and report before Task 3.
- [ ] **Step 4:** Write `rfid-routes.json`, the README row, the spec §3 paragraph (date, URLs, endpoints, versions 1.3.1 against the spec's 1.2.6b1, the scope table), and in §7 replace the NTAG bullet's "OpenSpool data with the SB spool id" by the Decision of "Spec deviation" above, with its source lines.
- [ ] **Step 5:** Run the test — PASS. Commit `docs(filament): the Bambuddy RFID routes, measured read-only (#6264)`.

### Task 2: Override binding rules, the pending queue and the sync setting

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_rfid.sql`, `backend/scadbuddy/filament/rfid_push.py`
- Modify: `backend/scadbuddy/filament/spools.py` (`bind_on`, `unbind_tag`, `_archive`), `filament/models.py`, `core/settings.py` (`FilamentSync`)
- Test: `backend/tests/filament/test_rfid_binding.py`, `backend/tests/filament/test_migrations.py`, `backend/tests/test_settings*.py` (the file that pins `FilamentSync`)

**Interfaces:**
- Produces three tables. `filament_bb_tag_pending(tag_id uuid PRIMARY KEY REFERENCES filament_tags ON DELETE CASCADE, queued_at timestamptz NOT NULL DEFAULT now(), attempts integer NOT NULL DEFAULT 0, last_attempt_at timestamptz, last_error text)`. `filament_rfid_state(singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), override_mode boolean NOT NULL DEFAULT false, saved_auto_add boolean, changed_at timestamptz NOT NULL DEFAULT now())` with its one row inserted. `filament_override_slots(printer_id <the type of printers.id> REFERENCES printers ON DELETE CASCADE, ams_id integer, tray_id integer, spool_id uuid NOT NULL REFERENCES filament_spools, params_hash text, configured_at timestamptz, attempts integer NOT NULL DEFAULT 0, last_error text, PRIMARY KEY (printer_id, ams_id, tray_id))`. The migration also queues every tag that has an open binding.
- Produces `rfid_push.enqueue_tag(conn: Conn, tag_id: UUID) -> None` (`INSERT ... ON CONFLICT (tag_id) DO UPDATE SET queued_at = now(), attempts = 0, last_error = NULL`).
- `FilamentSpoolStore.bind_on` calls `enqueue_tag` for the bound tag and for every tag it closes. `unbind_tag` and `_archive` (which closes the open binding) call it for the tag they close. The queue is filled whatever the sync setting says (cost: one row per tag, bounded by the tag count).
- `bind_on(..., override=True)`: raises `OverrideKindError(ValueError)` unless the tag's kind is `bambu_mifare` (the route maps it to 422); sets the spool's `weight_locked` to true when it is false, and records one `edited` event `data={"changes": {"weight_locked": [False, True]}}` in the same transaction. A bind of a tag already on the spool changes nothing.
- `FilamentSync` gains `tags: Literal["off", "sb_to_bb"] = "off"`. Update its docstring (it said `spools` only).

- [ ] **Step 1: Write the failing tests:**
  - `test_an_override_bind_locks_the_weight` (spool unlocked; `bind_tag(..., kind="bambu_mifare", tray_uuid=T, override=True)` → `spool.weight_locked is True`, one `edited` event whose `data.changes == {"weight_locked": [False, True]}`, the binding's `override is True`).
  - `test_an_override_on_an_ntag_is_refused` (`kind="ntag_opentag", override=True` → `OverrideKindError`; no binding, no event).
  - `test_a_second_override_bind_adds_no_second_lock_event` (bind the same tag twice → one `edited` event).
  - `test_a_rebind_queues_both_tags` (bind T1 to A, then T2 to A → `filament_bb_tag_pending` holds T1 and T2); `test_an_unbind_and_an_archive_queue_the_tag`.
  - `test_the_migration_queues_open_bindings` (the `MIGRATIONS[:idx]` pattern of `tests/test_pg_migrations.py`: one open and one closed binding before it → only the open one's tag is pending after it; `filament_rfid_state` has one row, `override_mode` false, `saved_auto_add` null).
  - `test_filament_sync_tags_defaults_off_and_refuses_other_values`.
- [ ] **Step 2:** Run them — FAIL.
- [ ] **Step 3:** Write the migration; implement the rules and the enqueue; edit the `FilamentSync` docstring.
- [ ] **Step 4:** Run them and `tests/filament tests/test_pg_migrations.py` — PASS.
- [ ] **Step 5:** Commit `feat(filament): override bindings lock the weight; tags queue for Bambuddy (#6264)`.

### Task 3: The Bambuddy client for tags, settings, slots and SpoolBuddy

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py` (`Operation`, methods), `bambuddy/errors.py` (`TagAlreadyLinkedError`), `bambuddy/models.py` (`Spool.tag_type`, `Spool.data_origin`, `SpoolBuddyDevice`, `SlotConfigure`)
- Test: `backend/tests/bambuddy/test_client_rfid.py` (respx; bodies from `rfid-routes.json`)

**Interfaces:**
- `Operation` gains `"spools.link_tag"`, `"spools.clear_tag"`, `"spools.by_tag"`, `"spools.read"`, `"settings.update"`, `"printers.slot_configure"`, `"spoolbuddy.devices"`, `"spoolbuddy.write_tag"`, `"spoolbuddy.cancel_write"`.
- `class TagAlreadyLinkedError(ApiError)` with `holder_id: int`, `field: Literal["tag_uid", "tray_uuid"]`.
- `BambuddyClient`, each with the scope Task 1 confirmed and a docstring that cites its source line:
  - `async link_tag(self, spool_id: int, *, tag_uid: str | None, tray_uuid: str | None, tag_type: str | None = None, data_origin: str = "nfc_link") -> Spool`: `PATCH /inventory/spools/{id}/link-tag`; `passthrough={409}`; a 409 whose `detail.code == "tag_already_linked"` raises `TagAlreadyLinkedError`; any other 409 maps as `map_response` does.
  - `async clear_spool_tag(self, spool_id: int) -> Spool`: `PATCH /inventory/spools/{id}` with `json={"tag_uid": None, "tray_uuid": None}`.
  - `async spool_by_tag(self, *, tray_uuid: str | None, tag_uid: str | None) -> Spool | None`: 404 is `None` (`passthrough={404}`).
  - `async spool_row(self, spool_id: int) -> Spool`: `GET /inventory/spools/{id}`.
  - `async auto_add_unknown_rfid(self) -> bool`: `GET /settings/`; a missing or non-boolean key is a 502 `ApiError`, as `capture_finish_photo` does.
  - `async set_auto_add_unknown_rfid(self, value: bool) -> None`: `PATCH /settings/`, `json={"auto_add_unknown_rfid": value}`, `Scope.ADMIN_SETTINGS`.
  - `async configure_slot(self, printer_id: int, ams_id: int, tray_id: int, params: SlotConfigure) -> None`: `POST .../configure` with `params.query()` as query parameters.
  - `async spoolbuddy_devices(self) -> list[SpoolBuddyDevice]`, `async queue_tag_write(self, device_id: str, spool_id: int) -> list[str]` (the answer's `warnings`, else `[]`), `async cancel_tag_write(self, device_id: str) -> None`.
- `SlotConfigure(BaseModel)`: `tray_info_idx: str`, `tray_type: str`, `tray_sub_brands: str | None`, `tray_color: str` (RRGGBBAA, no `#`), `nozzle_temp_min: int | None`, `nozzle_temp_max: int | None`, `cali_idx: int | None`, `nozzle_diameter: str`, `setting_id: str | None`, `k_value: float | None`; `query() -> dict[str, str | int | float]` omits `None`; `digest() -> str` is the sha256 hex of its sorted-key JSON.

- [ ] **Step 1: Write the failing tests** (respx on `https://bambuddy.test`):
  - `test_link_tag_sends_the_recorded_body`: the request JSON equals `{"tag_uid": "04AABB", "tray_uuid": None, "tag_type": None, "data_origin": "nfc_link"}` and the header `X-API-Key` is the configured key.
  - `test_a_409_names_the_holder`: respx answers 409 with the recorded `conflict_409` body (`spool_id: 7`, `field: "tray_uuid"`) → `TagAlreadyLinkedError` with `holder_id == 7`, `field == "tray_uuid"`; a 409 with another detail raises the generic mapped `ApiError`, not this one.
  - `test_clear_spool_tag_sends_two_nulls` (the PATCH body is exactly `{"tag_uid": null, "tray_uuid": null}`).
  - `test_by_tag_404_is_none_and_200_is_a_spool`.
  - `test_auto_add_reads_the_boolean_and_refuses_a_missing_key` (502); `test_setting_auto_add_with_auth_on_is_a_scope_409` (respx 403 → `ApiError` 409 whose `required_scope == "Settings (admin only)"`, as `_mapped` builds it).
  - `test_slot_configure_puts_the_values_in_the_query` (`tray_color=FF0000FF`, no `#`, `None` fields absent); `test_a_digest_changes_with_any_field`.
  - `test_queue_tag_write_returns_the_warnings`; `test_cancel_write_posts_to_the_device_path`.
  - `test_every_new_operation_is_a_declared_name` (each string used is in `typing.get_args(Operation)`).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. `Spool.tag_type` and `Spool.data_origin` are `str | None`; cite `SpoolUpdate`/`SpoolResponse` in the recording.
- [ ] **Step 4:** Run `tests/bambuddy` and the quality commands — PASS.
- [ ] **Step 5:** Commit `feat(bambuddy): client calls for tags, auto-add, slots and SpoolBuddy (#6264)`.

### Task 4: Push a tag binding to Bambuddy, idempotently

**Files:**
- Modify: `backend/scadbuddy/filament/rfid_push.py`, `filament/models.py` (`TagPushReport`), `backend/scadbuddy/spools/activities.py` (new activity), `workflows/spool_refresh.py` (`FILAMENT_RFID_ACTIVITY`, `FILAMENT_RFID_PATCH`, `_rfid()`), `backend/scadbuddy/main.py` (register it), `api/filament.py` (inline try after a bind)
- Test: `backend/tests/filament/test_rfid_push.py`, `backend/tests/test_spool_refresh_replay.py`, `backend/tests/fixtures/spool_refresh_histories/` (one new history)

**Interfaces:**
- Consumes: Task 2 `filament_bb_tag_pending`; Task 3 client methods; `filament_bb_links (spool_id, bb_spool_id)`; `FilamentSpoolStore.open_binding`.
- Produces `TagPushReport(pushed: int = 0, moved: int = 0, cleared: int = 0, unchanged: int = 0, failed: int = 0)`.
- Produces `async def drain_tags(store: Filament, client: BambuddyClient, *, limit: int = 50) -> TagPushReport`. For each pending tag, oldest first, in its own short transaction and try block:
  1. Read the tag's keys and its open binding. `desired` = the Bambuddy id in `filament_bb_links` of the bound spool, else `None`.
  2. `observed` = `client.spool_by_tag(tray_uuid=..., tag_uid=...)` (the BB spool's id, else `None`).
  3. `desired == observed`: delete the pending row; count `unchanged`.
  4. `desired` is set: `client.link_tag(desired, tag_uid=..., tray_uuid=..., tag_type="bambu" for kind bambu_mifare else "ntag", data_origin="nfc_link")`. On `TagAlreadyLinkedError`: `client.clear_spool_tag(holder_id)`, then `link_tag` again **once**; count `moved`. Else count `pushed`.
  5. `desired` is `None` and `observed` is linked to an SB spool (`filament_bb_links`): `clear_spool_tag(observed)`; count `cleared`. An unknown holder is left alone (Decision 7).
  6. On success, delete the pending row. On any failure, `attempts += 1`, `last_attempt_at = now()`, `last_error = <exception type name>` (never the message), count `failed`, and go on with the next tag.
  7. The drain does not touch the pull's recorded BB snapshot. After a link the pull sees BB's tag equal SB's open binding, and `bind_on` returns the held binding unchanged (a test below pins it).
- The whole drain runs only when `filament_sync.tags == "sb_to_bb"`; otherwise it returns `TagPushReport()` and leaves the rows.
- Produces `FILAMENT_RFID_ACTIVITY = "filament_rfid_reconcile"` and `FILAMENT_RFID_PATCH = "filament-rfid"`. The activity (`spools/activities.py`, in the API process like `filament_pull`) runs, each step failing alone: `rfid_mode.reconcile` (Task 5), `drain_tags`, `rfid_slots.reconcile_slots` (Task 6). `SpoolRefresh.run` calls `_rfid()` after `_pull()` behind `workflow.patched(FILAMENT_RFID_PATCH)` on both branches, with `retry_policy=_ONCE`; a failed activity logs a warning and the run goes on.
- `POST /filament/spools/{id}/tags` (existing route) after its commit calls `drain_tags` once under `waiting_on_bambuddy()` with `asyncio.timeout(RFID_INLINE_SECONDS)` (`2.5`), swallowing any error and logging its type. The reply never waits on Bambuddy beyond that, and never fails because of it.

- [ ] **Step 1: Write the failing tests** (`test_rfid_push.py`, respx, a `Filament` over `pg_pool`, `filament_sync.tags="sb_to_bb"`):
  - `test_a_bound_tag_is_linked_once`: spool A linked to BB 11 holds tag T; respx `by-tag` 404 then `link-tag` 200 → `report.pushed == 1`, one PATCH to `/inventory/spools/11/link-tag`, the pending row gone; a second `drain_tags` makes no PATCH and its respx `by-tag` answers BB 11 → nothing pending.
  - `test_a_409_moves_the_tag` (Review Focus 1): `by-tag` answers BB 7; `link-tag` to 11 answers the recorded 409 (`spool_id: 7`) first; then the clear PATCH to `/inventory/spools/7` (body two nulls), then `link-tag` 200 → `report.moved == 1`, calls in exactly that order.
  - `test_a_failed_push_keeps_the_row` (Review Focus 1): `link-tag` raises a connect error → `failed == 1`, `attempts == 1`, `last_error == "ConnectError"`, the SB binding intact, the row still pending; the next drain with Bambuddy up pushes it and no second BB spool ends with T in the respx call log.
  - `test_an_unbind_clears_only_a_known_holder`: tag unbound in SB; BB holder 11 is linked → cleared; with BB holder 99 unlinked → no PATCH, `unchanged == 1`.
  - `test_a_spool_with_no_bambuddy_link_pushes_nothing` (desired `None`, observed `None` → `unchanged`).
  - `test_the_drain_is_off_unless_tags_is_sb_to_bb` (setting `off` → no HTTP call, rows stay).
  - `test_the_pull_after_a_push_changes_nothing` (after a push, `pull(...)` with the BB spool carrying T → `PullReport.updated == 0` and the SB spool still holds one open binding, same id).
  - `test_the_route_binds_even_when_bambuddy_is_down` (POST the tag route with respx raising `ConnectError` → 200 and the binding exists).
  - Replay: record one new history after the patch (`SCADBUDDY_RECORD_HISTORIES=1`); the old histories still replay.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. Lock order stays tag, then spool.
- [ ] **Step 4:** Run `tests/filament tests/test_spool_refresh_replay.py tests/api/test_filament.py` and the quality commands — PASS. After #6590 merges, follow Task 0.
- [ ] **Step 5:** Commit `feat(filament): push tag bindings to Bambuddy, idempotently (#6264)`.

### Task 5: RFID override mode and `auto_add_unknown_rfid`

**Files:**
- Create: `backend/scadbuddy/filament/rfid_mode.py`
- Modify: `filament/models.py` (`RfidStatus`, `RfidReconcileReport`), `api/filament.py`, `spools/activities.py` (the activity's first step), `agent/src/tools/filament.ts`, `agent/src/tools/coverage.ts`
- Test: `backend/tests/filament/test_rfid_mode.py`, `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`

**Interfaces:**
- Produces `RfidStatus(override_mode: bool, state: Literal["off", "applied", "pending", "blocked"], bambuddy_auto_add: bool | None, saved_auto_add: bool | None, detail: str | None, devices: list[SpoolBuddyDevice])`. `state` is `applied` when the mode is on and Bambuddy reads `false`; `pending` when on and a write has not landed or Bambuddy is down; `blocked` when Bambuddy refused the write with the `ADMIN_SETTINGS` 409 (`detail` carries its reason); `off` when the mode is off and nothing is saved.
- Produces `class RfidModeStore(pool)`: `state() -> RfidState` (the row), `set_mode(enabled: bool) -> RfidState`, `save_found(value: bool) -> bool` (sets `saved_auto_add` only when it is `NULL`; returns whether it wrote), `clear_saved() -> None`.
- Produces `async reconcile_mode(store: RfidModeStore, client: BambuddyClient) -> RfidStatus`, **state-based**:
  - mode on: read `auto_add_unknown_rfid()`; `save_found(read_value)` **before** any write; if the value is `true`, `set_auto_add_unknown_rfid(False)`; then read it again.
  - mode off and `saved_auto_add` is set: `set_auto_add_unknown_rfid(saved)` if Bambuddy's value differs; on success `clear_saved()`.
  - mode off and nothing saved: no call.
  - A `blocked` or connect failure never clears `saved_auto_add` and never raises out of the activity.
- Routes (`prefix="/filament"`): `GET /rfid/status` (read-only: it reads Bambuddy and lists devices best effort, a Bambuddy error leaves the fields `None`/empty with `state` from the row); `PUT /rfid/override-mode` body `{enabled: bool}` writes the row first, then runs `reconcile_mode`, answers `RfidStatus` (200 even when `blocked`); `POST /rfid/reconcile` runs the three steps of the Task 4 activity once and answers `RfidReconcileReport(mode: RfidStatus, tags: TagPushReport, slots: SlotReport | None)`.
- Tools: `filament_rfid_status` (read), `filament_rfid_override_mode` (write; "Only what the user says; this turns Bambuddy's auto-add of unknown RFID spools off while on"), `filament_rfid_reconcile` (write), each with `routes:`.

- [ ] **Step 1: Write the failing tests** (respx, `pg_pool`):
  - `test_turning_the_mode_on_saves_the_found_value_then_turns_auto_add_off`: Bambuddy reads `true` → `saved_auto_add is True` before the PATCH (assert by the call order), the PATCH body is `{"auto_add_unknown_rfid": false}`, `state == "applied"`.
  - `test_a_second_reconcile_never_overwrites_the_saved_value`: after the first, Bambuddy reads `false`; reconcile again → `saved_auto_add` is still `True`, no PATCH.
  - `test_a_flip_back_in_bambuddy_is_turned_off_again` (Review Focus 3): Bambuddy then reads `true` (a person flipped it) → one PATCH `false`; `saved_auto_add` still `True`.
  - `test_turning_the_mode_off_restores_the_original_value` (Review Focus 3): mode off → PATCH `{"auto_add_unknown_rfid": true}`, `saved_auto_add is None`, `state == "off"`. With the original `false` (a person had it off): the restore PATCHes `false` or makes no call, and never writes `true`.
  - `test_a_blocked_write_keeps_the_mode_and_the_saved_value` (Review Focus 2): 403 on PATCH → HTTP 200 from the route, `state == "blocked"`, `detail` names "auth is on", `override_mode is True`, `saved_auto_add` kept.
  - `test_bambuddy_down_leaves_the_mode_pending` (connect error → `pending`, row unchanged).
  - `test_the_status_route_reads_without_writing` (no PATCH in the respx log).
  - Agent: `filament_rfid_override_mode` posts `{enabled: true}` to `PUT /api/v1/filament/rfid/override-mode`; `agent/test/coverage.test.ts` passes.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. The activity runs `reconcile_mode` first, so a person flipping Bambuddy's value back is fixed within one refresh.
- [ ] **Step 4:** Run backend tests; `cd agent && pnpm gen:api && pnpm test filament coverage && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): RFID override mode and Bambuddy's auto-add (#6264)`.

### Task 6: Configure the slot of an override tag

**Files:**
- Create: `backend/scadbuddy/filament/rfid_slots.py`
- Modify: `filament/models.py` (`SlotReport`), the activity of Task 4 (its third step)
- Test: `backend/tests/filament/test_rfid_slots.py`

**Interfaces:**
- Consumes: `FilamentInventory.trays(printer_id, seen) -> TrayView` (`TrayHolder.found_by`), `presets.effective_presets`, `preset_for`, `effective_k_values`, `PrinterReportStore`/`report_status`, `seen_trays`, `printers.bambuddy_printer_id`, `filament_override_slots`, `BambuddyClient.configure_slot`, `SlotConfigure`.
- Produces `SlotReport(configured: int = 0, unchanged: int = 0, no_preset: int = 0, no_nozzle: int = 0, failed: int = 0)`.
- Produces `installed_nozzle(report: Mapping[str, Any], ams_id: int) -> str | None`. Step 1 measures from a recorded report in `backend/tests/printers/fixtures` which key holds the installed nozzle diameter per extruder, and which extruder an AMS unit feeds. It cites the fixture and the key in its docstring. `None` when the report does not say.
- Produces `slot_params(spool: Spool, presets, k_values, printer_model: str, nozzle: str, tray_color: str) -> SlotConfigure | None` (`None` when `preset_for(presets, printer_model, nozzle)` is `None`): `tray_info_idx` is the preset's `slicer_filament`; `tray_type` the effective material name; `tray_sub_brands` the preset's `slicer_filament_name`, else the subtype name; `tray_color` the spool's effective `rgba` upper-case with no `#`; temperatures the effective ones; `cali_idx`, `setting_id`, `k_value` from the K row of this printer and nozzle, else `cali_idx=-1`, `k_value=None`.
- Produces `async def reconcile_slots(store: Filament, client: BambuddyClient, *, printers: PrinterStore, reports: PrinterReportStore) -> SlotReport`. For each registry printer that has a `bambuddy_printer_id` and a fresh report: for each filled tray, resolve through `trays`; only a holder with `found_by == "tag"` whose spool has an **open binding with `override=True`** counts. Compute `slot_params`; compare `digest()` with `filament_override_slots.params_hash` for `(printer, ams, tray)` and the same `spool_id`; equal: `unchanged`; else `configure_slot`, then upsert the row with the hash and `configured_at = now()`. A failure records `attempts`, `last_error` (type name) and goes on. A missing preset is `no_preset`, a missing nozzle `no_nozzle`; neither calls Bambuddy.
- Bambuddy off (`client_for` raises the not-configured 409): the step is skipped.

- [ ] **Step 1: Write the failing tests** (respx; a printer row, a report fixture with one tray carrying tray uuid `T`, material `PLA`, colour `#FF0000FF`; spool A of type "Acme PLA" with preset H2C/0.4 `GFL99` bound to `T` with `override=True`):
  - `test_an_override_tag_resolves_through_trays` (the tray's holder is A, `found_by == "tag"`; runs unchanged after the #6590 merge).
  - `test_an_override_slot_is_configured_with_the_types_preset`: one POST to `/printers/1/slots/0/1/configure`; its query has `tray_info_idx=GFL99`, `tray_color=FF0000FF`, `nozzle_diameter=0.4`, `tray_type=PLA`.
  - `test_a_second_reconcile_makes_no_call`: the row's hash equals the digest → `unchanged == 1`, no POST.
  - `test_a_changed_preset_configures_again` (edit the preset to `GFL98` → one POST).
  - `test_a_plain_binding_is_not_configured` (`override=False` → no POST, the Bambu tag's own settings stand).
  - `test_no_preset_for_the_nozzle_calls_nothing` (only H2C/0.6 exists → `no_preset == 1`).
  - `test_a_failed_configure_is_retried_next_time` (500 → `failed == 1`, `attempts == 1`, no hash stored; next run POSTs).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/filament tests/printers` — PASS.
- [ ] **Step 5:** Commit `feat(filament): configure the slot of an override tag (#6264)`.

### Task 7: The pull and tags: NTAG kind, and no duplicate for an auto-added spool

**Files:**
- Modify: `backend/scadbuddy/filament/bambuddy_pull.py` (`_bind`, step 3, `PullReport`), `frontend/src/api/filament.ts` only if `PullReport` is typed there
- Test: `backend/tests/filament/test_bambuddy_pull.py`

**Interfaces:**
- `_bind`'s kind rule: `bambu_mifare` when the tray uuid is set; else `ntag_opentag` when `bb.data_origin == "opentag3d"`; else `other`. `_bind` gets the BB spool (or its `data_origin`) as a parameter.
- `PullReport.rfid_duplicates: int = 0` and `rfid_duplicate_bb_ids: list[int]`. A BB spool with no link whose tag keys are held by an **open override binding** of an SB spool that **already has a BB link to another BB spool** creates no SB spool. It is counted and logged (`a Bambuddy spool made for an override tag is left alone`). Step 2 (link by tag) stays for an SB spool with no BB link.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_opentag_spool_binds_as_ntag_opentag` (BB spool with `tag_uid="04AABB", data_origin="opentag3d"`, no tray uuid → the SB binding's tag kind is `ntag_opentag`); `test_a_plain_uid_stays_other`; `test_a_tray_uuid_stays_bambu_mifare`.
  - `test_an_auto_added_bb_spool_for_an_override_tag_makes_no_second_sb_spool` (Review Focus 5): SB spool A linked to BB 11 holds override tag T; BB spool 40 (`data_origin: "rfid_auto"`, `tray_uuid: T`) appears → no new SB spool, `rfid_duplicates == 1`, `rfid_duplicate_bb_ids == [40]`, A's binding unchanged.
  - `test_an_unlinked_sb_spool_with_the_tag_is_still_linked` (SB spool B holds T override, no BB link; BB 40 carries T → B is linked to 40, as step 2 does today).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. Merge the `PullReport` fields with #6590's after Task 0.
- [ ] **Step 4:** Run `tests/filament/test_bambuddy_pull.py` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the pull knows NTAG tags and leaves an auto-added duplicate alone (#6264)`.

### Task 8: NTAG: devices, scan, write, confirm, cancel

**Files:**
- Create: `backend/scadbuddy/filament/ntag.py`
- Modify: `bambuddy/client.py` (`next_tag_scan`, `ws_token`), `bambuddy/models.py` (`TagScan`), `filament/models.py` (`ScanResult`, `NtagQueued`, `NtagConfirm`), `api/filament.py`, `backend/pyproject.toml` (`websockets` as a direct dependency; update `uv.lock` with the image's uv, see memory "Use the image's uv"), `frontend/src/api/filament.ts`, `agent/src/tools/filament.ts`, `agent/src/tools/coverage.ts`
- Test: `backend/tests/filament/test_ntag.py`, `backend/tests/bambuddy/test_client_scan.py`, `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`

**Interfaces:**
- Produces `TagScan(device_id: str, tag_uid: str, tray_uuid: str | None, tag_type: str | None, bb_spool_id: int | None)` and `BambuddyClient.ws_token(self) -> str | None` (`POST /auth/ws-token`; `None` when Bambuddy's auth is off; `Operation` `"auth.ws_token"`) and `BambuddyClient.next_tag_scan(self, *, timeout: float, device_id: str | None = None) -> TagScan | None` (`Operation` `"spoolbuddy.scan"`): connects to `ws(s)://<bambuddy>/api/v1/ws` (with `?token=` from `ws_token()` when Bambuddy's auth is on, which Task 1(b) decided; a refusal is the scope 409 of `map_response`), sends nothing, reads JSON frames until one has `type` `spoolbuddy_tag_matched` (`bb_spool_id = spool.id`) or `spoolbuddy_unknown_tag` (`bb_spool_id = None`) and, when `device_id` is given, the same `device_id`; closes; `None` on timeout. Other frames are ignored.
- Produces in `ntag.py`:
  - `scan_kind(scan: TagScan) -> TagKind`: `bambu_mifare` when `tray_uuid` is set, else `ntag_opentag` when `tag_type` starts with `NTAG` (any case), else `other`.
  - `async scan(store: Filament, client: BambuddyClient, *, timeout: float, device_id: str | None) -> ScanResult` with `ScanResult(status: Literal["tag", "timeout"], tag_uid, tray_uuid, tag_type, kind: TagKind | None, bambuddy_spool_id: int | None, sb_spool: {id: UUID, number: int} | None)`; `sb_spool` is the SB spool whose open binding holds the keys, else the SB spool linked to `bambuddy_spool_id`, else `None`.
  - `async write(store, client, spool_id: UUID, device_id: str, *, replace: bool) -> NtagQueued(status="queued", warnings: list[str])`. Raises `NtagNoBambuddySpoolError` (spool not linked), `NtagOverrideOpenError` (open binding with `override=True` and `replace` false); the routes map both to 409 with a stable `code`.
  - `async confirm(store, client, spool_id: UUID, *, actor: str) -> NtagConfirm(bound: bool, tag_uid: str | None)`: reads the BB spool; `bound` when its `tag_uid` is set, `data_origin == "opentag3d"` and differs from the SB spool's open tag UID: then `bind_tag(kind="ntag_opentag", tag_uid=..., actor=actor)` (an idempotent no-op when already bound). Otherwise `bound` is false and nothing changes.
  - `async cancel(client, device_id: str) -> None`.
- Routes: `POST /filament/rfid/scan` body `{timeout_s: int (1..60, default 30), device_id?: str}`; `POST /filament/spools/{id}/ntag/write` body `{device_id: str, replace: bool = False}`; `POST /filament/spools/{id}/ntag/confirm`; `POST /filament/spools/{id}/ntag/cancel` body `{device_id: str}`. 404 for an unknown spool. Each runs on the API process, with `actor(caller)`.
- Tools: `filament_rfid_scan` (waits up to the given seconds; "the user holds a tag to a SpoolBuddy reader"), `filament_spool_ntag_write`, `filament_spool_ntag_confirm`, `filament_spool_ntag_cancel`.
- Frontend `api/filament.ts`: `scanTag`, `writeNtag`, `confirmNtag`, `cancelNtag` typed from the generated schema.

- [ ] **Step 1: Write the failing tests:**
  - Client, with a local `websockets.serve` fake that sends frames: `test_the_scan_ignores_other_frames_and_returns_the_tag` (frames `printer_status`, then `spoolbuddy_unknown_tag` `{tag_uid: "04AABBCC", tray_uuid: null, tag_type: "NTAG215"}` → `TagScan(tag_uid="04AABBCC", bb_spool_id=None)`); `test_a_matched_frame_carries_the_bambuddy_spool` (`spoolbuddy_tag_matched`, `spool.id: 11` → `bb_spool_id == 11`); `test_a_scan_with_no_frame_times_out` (`None` after `timeout=0.2`); `test_a_scan_of_another_device_is_skipped`.
  - `test_scan_kind`: `tray_uuid` set → `bambu_mifare`; `NTAG215` → `ntag_opentag`; `None` and `None` → `other`.
  - `test_a_scan_names_the_sb_spool_that_holds_the_tag`.
  - `test_write_queues_for_the_linked_bambuddy_spool` (respx: POST `/spoolbuddy/nfc/write-tag` body `{"device_id": "sb-1", "spool_id": 11}`; answer `{"status": "queued", "warnings": ["brand/vendor not set"]}` → `NtagQueued.warnings == ["brand/vendor not set"]`).
  - `test_write_without_a_bambuddy_link_is_a_409` (Review Focus 4): code `no_bambuddy_spool`, no HTTP call to Bambuddy, no binding change.
  - `test_write_over_an_override_tag_is_refused_unless_replace` (Review Focus 4): code `override_tag_open`; with `replace=True` it queues, and the binding is **not** changed by `write` (only `confirm` binds).
  - `test_confirm_binds_the_new_uid_as_ntag_opentag`: respx `GET /inventory/spools/11` answers `tag_uid: "04AABBCC"`, `data_origin: "opentag3d"` → `bound is True`, the SB spool's open tag has that UID and kind `ntag_opentag`, one `tag_bound` event; a second confirm changes nothing; with `tag_uid` unchanged → `bound is False`.
  - `test_cancel_posts_to_the_device`.
  - API: scan with `timeout_s=61` → 422; the scan reply on a timeout is 200 with `status == "timeout"`.
  - Agent: `filament_spool_ntag_write` posts `{device_id, replace: false}`; `coverage.test.ts` passes.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. The WS read is `asyncio.timeout(timeout)` around `websockets.asyncio.client.connect`; the URL is built from `BambuddyConfig` (`https` to `wss`, `http` to `ws`).
- [ ] **Step 4:** Run backend tests; `cd agent && pnpm gen:api && pnpm test filament coverage && pnpm typecheck`; `cd frontend && pnpm gen:api && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): NTAG scan and write through SpoolBuddy (#6264)`.

### Task 9: Spool labels with a number and a QR code

**Files:**
- Create: `frontend/src/pages/filament/LabelsPage.tsx`, `frontend/src/components/filament/Label.tsx`, `frontend/src/components/filament/labelSheet.ts`
- Modify: `frontend/package.json` (`qrcode`, `@types/qrcode`, each an exact version at least two weeks old; the lockfile), the filament routes (`/filament/labels`), `frontend/src/components/filament/BulkBar.tsx` ("Labels"), `frontend/src/pages/filament/SpoolPage.tsx` ("Print label"), `frontend/src/pages/filament/tabs.ts` only if a route registry lives there, the Filament mocks in `frontend/src/mocks/features/filament/`
- Test: `frontend/src/components/filament/labelSheet.test.ts`, `Label.test.tsx`, `pages/filament/LabelsPage.test.tsx`, `BulkBar.test.tsx`

**Interfaces:**
- Produces `labelPayload(spoolId: string): string` = `` `scadbuddy:spool:${spoolId}` `` (Decision 12). `parseLabelPayload(text: string): string | null` returns the id or `null`.
- Produces `qrSvg(payload: string): Promise<string>` using `QRCode.toString(payload, { type: 'svg', errorCorrectionLevel: 'M', margin: 0 })`.
- Produces `Label({ spool, svg })`: 50 x 25 mm; the SVG at 21 mm; `#<number>` large; material and colour name; brand. A long colour name truncates with an ellipsis and never overflows the 50 mm.
- Produces `LabelsPage`: reads `?ids=<uuid>,<uuid>` (at most 100; more shows "Print at most 100 labels at a time" and no labels), loads each spool through `getSpool`, renders `roll` or `sheet`, and a "Print" button (`window.print()`). `@media print` hides the app chrome. The layout choice is remembered in `localStorage` under `scadbuddy.labels.layout` inside try/catch; the page renders right with storage blocked.
- Links use `AppLink`/`appUrl`. A spool with no `number` cannot exist (the column is an identity), so no case for it.

- [ ] **Step 1: Write the failing tests:**
  - `labelSheet.test.ts`: `labelPayload("0b9e...") === "scadbuddy:spool:0b9e..."`; `parseLabelPayload("scadbuddy:spool:abc") === "abc"`; `parseLabelPayload("https://x/y") === null`; the SVG string from `qrSvg` starts with `<svg` and two different payloads give different SVG.
  - `Label.test.tsx`: renders `#42` and the material text; with a 120-character colour name the text node has the truncation class and the label root has `width: 50mm`.
  - `LabelsPage.test.tsx`: with `?ids=a,b` it fetches both spools (msw) and renders two labels; 101 ids render the limit message and no label; a `localStorage` that throws still renders the roll layout; choosing `sheet` stores `sheet`.
  - `BulkBar.test.tsx`: with two spools selected, "Labels" navigates to `/filament/labels?ids=<id1>,<id2>`.
- [ ] **Step 2:** Run `pnpm exec vitest run` on them — FAIL.
- [ ] **Step 3:** Implement. Load `frontend-design:frontend-design` first; keep the label's look plain and legible: it is print output.
- [ ] **Step 4:** Run `pnpm gen:api && pnpm lint && pnpm typecheck` and the vitest files — PASS. Run `pnpm exec playwright test e2e/filament.spec.ts` (existing) to check the new route breaks nothing.
- [ ] **Step 5:** Commit `feat(filament): spool labels with a number and a QR code (#6264)`.

### Task 10: The RFID UI: TagPanel, NTAG panel and the Settings Filament section

**Files:**
- Create: `frontend/src/components/filament/NtagPanel.tsx`, `frontend/src/mocks/features/filament/rfid.ts`, `frontend/e2e/filament-rfid.spec.ts`
- Modify: `frontend/src/components/filament/TagPanel.tsx`, `frontend/src/pages/filament/SpoolPage.tsx` (mount `NtagPanel`), `frontend/src/pages/settings/sections/filament.tsx`, `frontend/src/api/filament.ts`, `frontend/src/pages/filament/TagsTab.tsx` (an "override" mark per row)
- Test: `components/filament/TagPanel.test.tsx`, `NtagPanel.test.tsx`, `pages/settings/sections/filament.test.tsx`, `pages/filament/TagsTab.test.tsx`

**Interfaces:**
- `TagPanel`: the checkbox now reads "This Bambu tag belongs to a different filament (override)", is enabled only for a tag with a tray UUID, and sends `override` to the bind route. "Move it here" sends `override: false` (Decision 3). A "Scan a tag" button calls `scanTag` (30 s), shows "Hold the tag to the reader" while it waits, fills the UID, tray UUID and kind from the result, and says which spool already holds it (`sb_spool`). The TagPanel shows the open binding's "Override" mark and a line "Weight locked: the AMS percentage never changes grams left".
- `NtagPanel({ spool })`: a device picker from `getRfidStatus().devices` (only `has_nfc`); "Write NTAG" calls `writeNtag`; then polls `confirmNtag` every 2 s for 60 s and says "Written and bound" or "Not seen yet"; "Cancel" calls `cancelNtag`. The `override_tag_open` 409 shows a confirm "This replaces the Bambu tag the AMS reads. Replace it?" that retries with `replace: true`. The `no_bambuddy_spool` 409 shows its detail. Warnings from the write are listed.
- Settings → Filament gains: "Tags to Bambuddy" (`Off` / `Send ScadBuddy's bindings`) bound to `filament_sync.tags`; "RFID override mode" (a switch that calls `setOverrideMode`) with a status line from `getRfidStatus()`: `applied` "Bambuddy no longer adds unknown RFID spools", `pending`, `blocked` with its `detail` (the auth-on reason), and "Apply now" (`reconcileRfid`). No hard-coded choice: both are remembered settings.
- `TagsTab`: an "Override" mark on a row whose binding has `override`.
- The mocks serve `/api/v1/filament/rfid/*` and `…/ntag/*`; `rfid.ts` exports `handlers` and `reset` and owns those routes (one route, one feature module).

- [ ] **Step 1: Write the failing tests:**
  - `TagPanel.test.tsx`: `test_move_it_here_is_not_an_override` (the POST body has `override: false`); `test_the_override_checkbox_needs_a_tray_uuid` (disabled with only a UID; enabled with a tray UUID; checked, the body has `override: true` and `kind: "bambu_mifare"`); `test_scanning_fills_the_form` (msw answers `status: "tag"` with `tag_uid`, `tray_uuid` null, `kind: "ntag_opentag"` → the inputs hold them and the kind select reads `ntag_opentag`); `test_a_scan_timeout_says_so` (no crash, "No tag seen").
  - `NtagPanel.test.tsx`: write then confirm: msw `confirm` answers `bound: false` once, then `bound: true` → "Written and bound" after two polls (fake timers); the `override_tag_open` 409 shows the replace confirm and the second POST has `replace: true`; the `no_bambuddy_spool` 409 shows its detail and no polling starts.
  - `filament.test.tsx` (settings): switching override mode on posts `{enabled: true}`; a `blocked` status shows its `detail` and the switch stays on; the "Tags to Bambuddy" select saves `filament_sync.tags`.
  - e2e `filament-rfid.spec.ts` (msw): bind a Bambu tag with override on a spool, see the override mark and the weight-lock line; write an NTAG and see "Written and bound"; open `/filament/labels?ids=...` and see a `#N` label with an `<svg>`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. Load `frontend-design:frontend-design` first.
- [ ] **Step 4:** Run `pnpm gen:api && pnpm lint && pnpm typecheck`, the vitest files, `pnpm exec playwright test e2e/filament-rfid.spec.ts` — PASS.
- [ ] **Step 5:** Commit `feat(filament): RFID panels, override mode and NTAG in the UI (#6264)`.

### Task 11: Notes, whole-branch check and PR

**Files:**
- Modify: `backend/CLAUDE.md` (the filament layout lines: the four new modules, the `filament_rfid_reconcile` step, `filament_sync.tags`, the pending table, the rule "SB first, Bambuddy second"), `frontend/CLAUDE.md` (labels page, override panel, the `localStorage` key), `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§7 "As built in #6264": the override rules, the label format, the scan and write calls; §12: the `tags` sync value; §11: label templates and BB `label-templates` listed as a parity gap for #6270)

- [ ] **Step 1:** Merge main as Task 0 says (after #6590). Fix every note this PR makes false: `grep -rn "override" backend/CLAUDE.md frontend/CLAUDE.md docs/superpowers/specs/2026-11-14-filament-management-design.md` and `grep -rn "Labels with QR" docs` (the #6262 plan says labels go to #6264: it is now true).
- [ ] **Step 2:** Run the whole check: `uv run --frozen pytest tests/filament tests/bambuddy tests/printers tests/spools tests/api/test_filament.py tests/api/test_openapi.py tests/api/test_routes.py tests/test_spool_refresh_replay.py tests/test_pg_migrations.py`, ruff and mypy; `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm test`; `cd agent && pnpm gen:api && pnpm test && pnpm typecheck`; `lint-base64-images.sh`.
- [ ] **Step 3:** Open the PR `feat(filament): RFID tags, overrides, NTAG and labels (#6264)`, body `Closes #6264`, the Task 1 measurements (date, versions, URLs; the auto-add value only here), the spec deviation (OpenTag3D, not OpenSpool with an SB id), the defaults (`tags` off), the session marker, and the footer. Shepherd it.

## Decisions made while building (#6264)

Empty. The controller adds each ruling here, with its cost if wrong.

## Self-review

- Spec §7 bullet 1 (override binding): binding and weight lock Task 2; slot configure Task 6; `link-tag` with 409 move Task 4; `auto_add_unknown_rfid` off while the mode is on, with restore Task 5. §7 NTAG: Tasks 3 and 8 (with the OpenTag3D correction, Task 1). §7 labels: Task 9. §5.4 re-bind events: already in `bind_on`; Task 2 adds the queue. §8.1 "Tag binding → `link-tag`": Task 4. §11 SpoolBuddy "NFC scan and write": Task 8; "Settings: `auto_add_unknown_rfid`": Task 5, 10. The issue's comment (labels with QR): Task 9. Gap: BB label templates and `/inventory/labels` PDF output are not matched; Task 11 lists them for #6270.
- Types agree across tasks: `TagPushReport` (Tasks 4, 5), `SlotReport` (Tasks 5, 6), `RfidStatus` (Tasks 5, 8, 10), `TagAlreadyLinkedError(holder_id, field)` (Tasks 3, 4), `SlotConfigure.digest()` (Tasks 3, 6), `FilamentSync.tags` (Tasks 2, 4, 10), `OverrideKindError` (Task 2).
- #5295: Tasks 4, 5, 6, 8 call Bambuddy from the API process only; no new pool, no worker route, no grant.
- Plan length is a fraction of the code it describes; bodies appear only where a signature and tests do not decide them.
