# Filament RFID: tags, overrides and NTAG (#6264) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **UI work (Tasks 9 and 10 touch the frontend):** the implementer loads `frontend-design:frontend-design` before writing any UI. Pass this line on to every UI subagent too.

**Goal:** A person re-binds RFID tags, uses a Bambu tag on another brand's spool (override), writes and reads NTAG tags through Bambuddy's SpoolBuddy, and prints spool labels with a number and a QR code. Every reconcile write to Bambuddy is state-based and never costs ScadBuddy its own record.

**Architecture:** ScadBuddy's binding is always written first, in Postgres. A one-row-per-tag queue (`filament_bb_tag_pending`) marks a tag whose Bambuddy state may differ. A state-based reconcile (a new step in the `SpoolRefresh` schedule, plus one best-effort try after a request) compares what SB wants with what Bambuddy holds and writes only the difference. Everything that calls Bambuddy runs in the API process (library worker and routes), so no worker gets a pool or a route. Labels render in the browser.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (sync, `dict_row`), pydantic v2, httpx, respx, `websockets`, Temporal Python SDK, Postgres 18; React 19, Vite, shadcn/ui, msw, vitest, Playwright; TypeScript agent tools (zod, msw).

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§3, §5.4, §7, §8.2, §11 "SpoolBuddy" and "Settings", §12 row #6264). Issue #6264 and its comment (labels belong here). Epic #6260. Plans that bind this one: `docs/superpowers/plans/2026-11-14-filament-data-model.md` (#6261, its "Decisions made while building"), `2026-11-15-filament-ui.md` (#6262), and the in-flight `2026-11-15-filament-capture.md` (#6263, PR #6590, on `origin/feat/filament-capture`).

## Global Constraints

- Database rule (CLAUDE.md "Database access (#5295)"): only the CRUD service (API process) has SQL. This plan adds no pool, no grant, no worker route. Every Bambuddy call here runs in the API process, as the BB pull does (`filament/bambuddy_pull.py` `read_and_pull`).
- Every reconcile write to Bambuddy (link, clear, setting, slot configure) is state-based: it reads Bambuddy's state first and writes only a difference. A person-triggered `write-tag` or `cancel-write` is one call per click (Decision 13). A failure never rolls back or loses SB's own record: SB commits first, Bambuddy second.
- Bambuddy calls go through `bambuddy/client.py` `BambuddyClient._send`, with a `Scope` and an `Operation` for each (`bambuddy/errors.py` `Scope`). Scopes are never invented; Task 1 confirms each one.
- Migrations (#491): new files `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`. Never edit a merged file.
- Timestamps `timestamptz`; enums `text` with `CHECK`. Colour is `#RRGGBBAA`.
- One open binding per tag and per spool (§5.4). A re-bind closes the old binding and writes `tag_unbound` and `tag_bound` (`FilamentSpoolStore.bind_on`). Nothing here refuses a re-bind. A failure reason stored for a tag, slot or status is `rfid_push.error_label(exc: ApiError) -> str`: the problem `type`, plus `:<bambuddy_status>` when set. Never the message, never the exception class (the client wraps every transport error in `ApiError`).
- A `SpoolRefresh` change goes behind `workflow.patched(...)`, with a new history recorded beside the old ones in `backend/tests/fixtures/spool_refresh_histories/`. Never re-record an old history. Task 4 puts its step last in `run()`, after #6590's capture step, behind its own patch.
- Every new public `/api/v1` route sets `summary=` and `description=` and gets an agent tool or a `NOT_A_TOOL` entry (`agent/src/tools/coverage.ts`). Every tool works in classic and durable mode.
- Generated API files (`backend/openapi.json`, both `schema.d.ts`) are never committed. Run `pnpm gen:api` after a route change.
- Every new BB fact gets a link; every measured fact says where and how it was measured (#259). Bambuddy facts cite the Bambuddy source tag `v1.3.1` or the recorded OpenAPI.
- Never a write to the live Bambuddy. Writes are tested with respx against recordings. Live reads are `GET` only, with no key (Task 1: on a 401, stop and report; never look for a vault item).
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
| `data_origin` values Bambuddy itself writes on a spool: `"rfid_auto"` (auto-added for an unknown tag, `spool_tag_matcher.py` line 265), `"rfid_linked"` (Bambuddy adopts a tag onto a matching untagged spool, same file line 417), `"nfc_link"` (link-tag), `"opentag3d"` (a write-tag result). Task 1 records each with its line. | `backend/app/services/spool_tag_matcher.py`, `inventory.py` line 2747, `spoolbuddy.py`, v1.3.1 |
| `GET /api/v1/spoolbuddy/devices` lists devices: `device_id, hostname, has_nfc, nfc_ok, last_seen, pending_command, ...`. | OpenAPI `DeviceResponse` |

Spec deviation, decided here: spec §7 says the NTAG carries OpenSpool data with the SB spool id. Bambuddy's write path cannot do that. The tag carries OpenTag3D data of the **linked Bambuddy spool**, and SB identifies the tag by its UID through its binding (kind `ntag_opentag`, which §5.4 already lists). Task 1 corrects §7.

## Decisions this plan makes

Each names its cost if wrong.

1. **SB first, Bambuddy second, state-based.** A binding change inserts a `filament_bb_tag_pending` row (one per tag, deduped) in its own transaction. The reconcile never replays a stored payload. It recomputes SB's wish and Bambuddy's state. Cost: up to one refresh interval (`spool_overlay_refresh_seconds`, default 300 s) of lag when the best-effort try after a request fails.
2. **`filament_sync.tags: "off" | "sb_to_bb"`, default `off`.** #6265 widens it to the spec's four values. Cost: the owner turns it on in Settings before SB's bindings reach Bambuddy. Chosen because a Bambuddy write path is never silently on.
3. **Override means "this Bambu tag stays on a spool of another filament".** It is an explicit flag, valid only for kind `bambu_mifare` (422 otherwise). Binding with it locks the spool's weight (one `edited` event `changes.weight_locked = [false, true]`). Unbinding never unlocks. #6262's TagPanel sent `override: true` for "Move it here"; that changes to `false`, because a move is not an override. Cost: a person unlocks the weight by hand.
4. **RFID override mode lives in `filament_rfid_state`**, a one-row table with `override_mode` and `saved_auto_add`, written only by `PUT /filament/rfid/override-mode`. It is not in `StoredSettings`, so a generic settings save cannot change it without the restore state. Cost: Settings reads it through a route.
5. **`saved_auto_add` is written before Bambuddy is touched and never overwritten while set.** A crash, a retry or a person flipping the value back never lets SB save its own `false` as "the old value". Cost: none.
6. **Moving a tag on a 409 clears the holder, then links again, once.** The holder is cleared with `PATCH /inventory/spools/{holder}` `{"tag_uid": null, "tray_uuid": null}`. The holder need not be an SB-linked spool (spec: "SB moves it"). Cost: a Bambuddy spool that a person tagged by hand loses that tag when SB's binding says it is elsewhere. Two more effects: a cleared holder that SB does not know becomes a new SB spool on the next pull (the pull makes one for every unlinked BB spool, except one it skipped as a duplicate: Decision 29), and Bambuddy itself adopts a tag onto a matching untagged spool (`data_origin: "rfid_linked"`), so a spool can regain a tag without SB's write.
7. **An unbind in SB clears the tag in Bambuddy only when Bambuddy's holder is linked to an SB spool** (`filament_bb_links`). A holder SB does not know is left alone. Cost: a stale tag on an unknown Bambuddy spool stays.
8. **NTAG write needs a linked Bambuddy spool** (409 `no_bambuddy_spool` otherwise; creating one is #6265) and **refuses a spool with an open override binding** unless `replace: true` (one binding per spool: an NTAG would close the Bambu tag the AMS needs). Cost: one extra click.
9. **NTAG write is three calls, not a held request:** `ntag/write` queues, `ntag/confirm` reads the Bambuddy spool and binds the new UID (kind `ntag_opentag`), `ntag/cancel` cancels. The UI polls confirm every 2 s for 60 s. The pull would bind it later anyway. `write` records the Bambuddy record's `tag_uid` in `filament_ntag_writes`; `confirm` binds only a UID that differs from it, and binds nothing when no write is queued (review of #6617). Cost: one more Bambuddy read per write.
10. **NTAG scan is a bounded WebSocket listen** (`POST /filament/rfid/scan`, at most 50 s: Decision 22), not a standing connection. Cost: a scan outside the window is lost; the person scans again.
11. **Override configures the slot through Bambuddy's `configure` route only** (no LAN path in SB for AMS filament settings). It runs in the reconcile, so the slot is configured within one refresh interval of the load. Cost: that lag; the print dialog never depends on it. #6590 does not change this (Task 0).
12. **Label format.** One label is 50 x 25 mm: the QR code (19.8 mm inside a 4-module quiet zone, Decision 2; error correction M) on the left; on the right `#<number>` large, then material and colour name, then brand. The QR payload is the text `scadbuddy:spool:<uuid>`: no host in it (UI links follow the host the user is on), and the same string the later scan flow (#6268) parses. Two layouts: `roll` (one label per page, `@page size 50mm 25mm`) and `sheet` (a `flex-wrap` row 190 mm wide, so 3 labels per row on A4). The layout is remembered in `localStorage` (try/catch). It renders in the browser, in a new page `/filament/labels`, with the npm `qrcode` package. Cost: BB's label templates (custom sizes) wait for #6270's parity sweep. Task 11 records the `scadbuddy:spool:` prefix in the spec's §7 (the spec says only "QR code of the spool id"; the payload contains the id).
13. **Reconcile writes are state-based; person-triggered calls are one per click.** The link, the clear, the setting write and the slot configure read state first and write a difference. `write-tag` and `cancel-write` are single calls that a person starts; they are not retried or compared. Cost: a double click queues two writes.

## Pre-flight rulings (2026-11-17)

F1: Task 0 rewritten (Tasks 1 and 9 on main now; Tasks 2-8, 10, 11 wait for #6590, then merge main first), overlapping files listed; Task 11 Step 1 follows — Tasks 2-8, 10 and 11 edit files #6590 changes.
F2: Task 4 builds the activity with `drain_tags` only and defines `SlotReport`; Task 5 adds `reconcile_mode` first and `/rfid/reconcile` with `slots=None`; Task 6 adds the third step and fills `slots` — Task 4 named functions that Tasks 5 and 6 had not built.
F3: `last_error` stores `error_label(exc)` (the `ApiError` problem type plus `bambuddy_status`), asserted in Tasks 4, 5, 6 and set in Global Constraints — the client wraps every transport error in `ApiError`, so a class name always read `ApiError`.
F4: Task 4 step 5 re-reads the SB spool's open bindings before a clear, the drain orders by `(queued_at, tag_id)`, new test `test_a_rebind_across_kinds_never_clears_the_new_tag`, Review Focus 1 — a same-transaction tie could wipe the new tag in Bambuddy.
F5: Task 6 `reconcile_slots` also compares the report's tray values, the hash stays an attempt guard, new test `test_a_reloaded_tray_is_configured_again` — the AMS resets a slot on reload and the hash alone said "unchanged".
F6: Task 2 adds `api/filament.py` (422 mapping) and an API test, and `bind_on` updates a held binding whose `override` differs in place, with a test and docstring — the 422 was mapped nowhere and `override=True` on a bound tag was a silent no-op.
F7: Task 2 adds `core/setting_docs.py` and its test; Task 11 Step 1 greps `read-only toward Bambuddy|never writes to Bambuddy|pull only` and names `backend/CLAUDE.md` line 731 — docs this diff makes false are Blocking.
F8: Task 1 gives `rfid-routes.json` the `components.schemas` layout plus `routes`, `conflict_409`, `ws_events` and adds `SpoolResponse` to its test; Task 3 adds `tests/bambuddy/conftest.py` (`recorded_schema` reads both files); File structure — respx bodies must be checked against the recording.
F9: Task 1 sends no key (on a 401, stop and report) and drops the `op.exe` clause; Global Constraints likewise — no vault item is named and writes need no key.
F10: Decision 13, Global Constraints and the Goal say reconcile writes are state-based while `write-tag` and `cancel-write` are one call per click — the old "every write idempotent" contradicted Task 8.
F11: Task 5 restore test asserts no PATCH when Bambuddy already reads the saved `false` — the hedged assertion was weak.
F12: Decision 9 accepts a stale NTAG bind by `confirm`; Decision 12 and Task 11 record the QR prefix in spec §7; Task 1 keeps the §7 NTAG correction with line cites — the spec was wrong about Bambuddy's write path.
F13: Task 9 adds `App.tsx` and `e2e/page-titles.spec.ts`; Task 10 adds `mocks/features/index.ts`, rewrites the TagPanel comment and drops the TagsTab item; Task 7 adds the `PullReport` mock; the facts table and Task 1 (g) record `rfid_auto` and `rfid_linked` — typecheck and e2e would fail without them.
F14: Task 5 adds `rfid: RfidModeStore` to `Filament` (`filament/component.py`, `main.py`) and `RfidState` to `models.py`; Task 4's activity factory takes `capture_backstop_activity`'s arguments plus `Filament` — nothing built `RfidModeStore` for the activity.
F15: Task 4 puts `_rfid()` last in `run()` after `_capture` behind its own patch, records one history with all three markers, and adds `test_spool_refresh_workflow.py`; Task 0's table follows — replay and the fake worker would otherwise break after the merge.
F16: Decision 6's cost names the new SB spool for a cleared unknown holder and Bambuddy's own `rfid_linked` adoption — both surprise a person and need no code.

## Review Focus

1. A tag is re-bound while Bambuddy is down: SB keeps the new binding, the pending row stays with `attempts` counted, and the next reconcile links it once, so Bambuddy never ends with two spools holding that tag (Task 4). A rebind across kinds (an NTAG, then a Bambu tag, on one spool) never clears the new tag (Task 4, `test_a_rebind_across_kinds_never_clears_the_new_tag`).
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
  rfid_push.py     tag push: enqueue_tag, drain_tags, error_label  Tasks 2, 4
  rfid_mode.py     override mode and auto_add_unknown_rfid        Task 5
  rfid_slots.py    configure a slot for an override binding       Task 6
  ntag.py          NTAG write, confirm, cancel, scan              Task 8
  spools.py        override rules in bind_on; enqueue             Task 2
  bambuddy_pull.py  kind from data_origin; rfid_auto guard        Task 7
  models.py        SlotReport, TagPushReport, RfidState, RfidStatus, RfidReconcileReport, ScanResult, Ntag*   Tasks 4-8
  component.py     Filament gains `rfid: RfidModeStore`           Task 5
backend/scadbuddy/bambuddy/client.py, errors.py, models.py        Tasks 3, 8
backend/scadbuddy/core/settings.py, core/setting_docs.py   FilamentSync.tags and its doc   Task 2
backend/scadbuddy/spools/activities.py, workflows/spool_refresh.py, main.py   Tasks 4-6 (activity, patch)
backend/scadbuddy/api/filament.py        rfid and ntag routes     Tasks 5, 8
backend/tests/bambuddy/recordings/openapi/rfid-routes.json        Task 1
backend/tests/bambuddy/conftest.py       recorded_schema reads both recordings   Task 3
backend/tests/filament/test_rfid_*.py, test_ntag.py               Tasks 2-8
agent/src/tools/filament.ts, coverage.ts, agent/test/filament.test.ts   Tasks 5, 8
frontend/src/api/filament.ts                                      Tasks 8, 10
frontend/src/pages/filament/LabelsPage.tsx, components/filament/Label.tsx, labelSheet.ts, App.tsx, e2e/page-titles.spec.ts   Task 9
frontend/src/components/filament/TagPanel.tsx, NtagPanel.tsx      Task 10
frontend/src/pages/settings/sections/filament.tsx                 Task 10
frontend/src/mocks/features/filament/rfid.ts, mocks/features/index.ts, frontend/e2e/filament-rfid.spec.ts   Task 10
docs/superpowers/specs/2026-11-14-filament-management-design.md   Task 1 (§3, §7), Task 11
backend/CLAUDE.md, frontend/CLAUDE.md                             Task 11
```

---

### Task 0: Order of work, and the merge after #6590

No code. This note binds the branch.

PR #6590 (#6263, `origin/feat/filament-capture`) is in flight. It edits files that Tasks 2 to 8, 10 and 11 also edit (pre-flight F1). The order is exactly this:

1. **Now, on main:** Task 1 and Task 9. They touch none of the files below.
2. **Wait until PR #6590 merges.** Then `git merge origin/main` into `feat/filament-rfid` (Step 1) and run Step 2.
3. **After the merge:** Tasks 2, 3, 4, 5, 6, 7, 8, 10 and 11, in that order. No task resolves conflicting hunks later: the merge comes first.

Files both PRs edit, and the tasks that wait because of them:

| File | #6590 change | Tasks |
|---|---|---|
| `core/settings.py`, `core/setting_docs.py` | `FilamentSync` docstring and its doc text | 2 |
| `bambuddy/client.py`, `bambuddy/models.py` | `Operation` gains `"spools.usage"`; `as_utc`, `SpoolUsage` | 3, 8 |
| `spools/activities.py`, `main.py` | `capture_backstop_activity` and its registration | 4, 5, 6 |
| `workflows/spool_refresh.py`, `tests/test_spool_refresh_workflow.py` | `_capture` step, `workflow.patched("filament-capture-backstop")`; the fake worker lists each activity | 4 |
| `filament/bambuddy_pull.py` | `read_and_pull` rewritten, `PullReport.usage_booked`, `usage_unlinked` | 7 |
| `mocks/features/filament/index.ts` | two lines in the `PullReport` mock | 7, 10 |
| `backend/CLAUDE.md` | filament notes | 11 |

Other names #6590 changes, and what this plan does about each:

| #6590 name | Effect on this plan |
|---|---|
| `filament/inventory.py` `FilamentInventory.holder_on(conn, printer_id, seen) -> TrayHolder` (static; `trays` calls it). Keys no binding holds fall through to the placed-spool rule. | Task 6 calls `FilamentInventory.trays(printer_id, seen)`. Override slots are found by `found_by == "tag"`, which `holder_on` keeps. |
| `place_on`, `locations.ensure_ams_on`, `move_on`, `bambuddy/trays.py` `place_label` | Unused here. Slots are keyed by `(printer_id, ams_id, tray_id)`. |
| `filament/capture.py` (`reconcile`, `touched`, `lock`, `trays_changed`, `WATCH_ACTOR`, `BACKSTOP_ACTOR`) | Capture locks spools and never takes a tag lock. This plan's writers take the **tag first, then the spools** (`bind_on`). Task 2's enqueue is an `INSERT ... ON CONFLICT` on a tag-keyed row inside `bind_on`'s transaction: no spool lock. |
| `ledger.py` `PRINT_RUN`, `BB_USAGE`, `BB_REFS`, `job_of`, `refresh_derived(conn, spool_id, event=None)` | Task 2's `edited` event goes through `ledger.record`, which passes its event on. |
| `bambuddy_pull.py` `read_and_pull` | Task 7 changes `_bind`'s kind rule and adds `PullReport.rfid_duplicates`, on top of #6590's `PullReport`. |
| `workflows/spool_refresh.py` `_capture`, the `filament-capture-backstop` patch | Task 4 puts `_rfid()` **last** in `run()`, after `_capture`, behind `workflow.patched(FILAMENT_RFID_PATCH)`. It records one new history that carries all three markers (`spool-providers-gone`, `filament-capture-backstop`, `filament-rfid`). Never re-record an old one. |
| `printers/reports.py` `advance_in` runs capture; `printers/status.py` `RUNNING`; `SeenTray.remain` | Not touched here. |

- [ ] **Step 1:** After #6590 merges, `git merge origin/main` into `feat/filament-rfid`. Resolve any conflict in the spec and `backend/CLAUDE.md` (Task 1's spec hunks do not overlap #6590's §5.5, §6, §8.2/§9). Do this before Task 2.
- [ ] **Step 2:** Run `uv run --frozen pytest tests/filament tests/test_spool_refresh_replay.py tests/bambuddy tests/printers` and the three commands of Global Constraints. All pass before Task 2.

### Task 1: Record the Bambuddy facts and finish the read-only measurements

**Files:**
- Create: `backend/tests/bambuddy/recordings/openapi/rfid-routes.json`
- Modify: `backend/tests/bambuddy/recordings/README.md` (one table row), `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§3 paragraph, §7 NTAG bullet)
- Test: `backend/tests/bambuddy/test_recordings_rfid.py`
- Scratch only, never committed: any script, in the session scratchpad, apart from the data it reads. Run Python with `-I`.

**Interfaces:**
- Produces `rfid-routes.json`: for each route of the Bambuddy facts table (link-tag, by-tag, `PATCH /spools/{id}`, `GET`+`PATCH /settings/`, configure, the four `nfc/*` routes, `cancel-write`, `GET /spoolbuddy/devices`, `POST /auth/ws-token`) and for `SpoolResponse` (the source of `tag_type` and `data_origin`, used by Task 3). The file has the layout of `scadbuddy-routes.json`: a top-level `components.schemas` with every schema those routes reference, copied from the live `/openapi.json`, plus `routes` (per route: method, `operationId`, parameters, request schema name), plus `conflict_409` (the `tag_already_linked` detail above, from the source) and `ws_events` (`spoolbuddy_tag_matched`, `spoolbuddy_unknown_tag`, `spoolbuddy_tag_written`, `spoolbuddy_tag_write_failed`, `spoolbuddy_tag_link_failed` with their fields, from `spoolbuddy.py`). Tasks 3 and 8 build their respx bodies from it through `tests/bambuddy/conftest.py` `recorded_schema` and `shaped`, which Task 3 extends to search both recordings.
- Produces a scope table in the spec's §3: for each route, Bambuddy's permission and the `Scope` ScadBuddy uses.

Strictly read-only: `GET` requests and a listening WebSocket that sends nothing. No `kubectl exec`. No key is sent: `.claude/skills/filament-intake/SKILL.md` names no vault item and says writes need no key here. If a route answers 401, stop and report; never guess a vault item or call `op.exe`.

- [ ] **Step 1: Write the failing test** `test_the_rfid_recording_holds_what_the_plan_rests_on`: loads `rfid-routes.json`; asserts `LinkTagRequest` has properties `tag_uid, tray_uuid, tag_type, data_origin`; `conflict_409["code"] == "tag_already_linked"` and its keys are `code, message, spool_id, field`; `WriteTagRequest` requires `device_id` and `spool_id`; `AppSettingsUpdate` has `auto_add_unknown_rfid`; `ws_events` lists `spoolbuddy_tag_matched` and `spoolbuddy_unknown_tag`; `SpoolResponse` has `tag_type` and `data_origin`; the recorded `data_origin` values include `rfid_auto` and `rfid_linked`, each with a source line.
- [ ] **Step 2:** Run it — FAIL (no file).
- [ ] **Step 3: Measure, read-only, against `https://bambuddy.internal.nullreference.io`, and record each answer's shape (never a secret or an access code):**
  - (a) `GET /openapi.json`: extract the routes above into `rfid-routes.json`.
  - (b) `GET /api/v1/auth/status` (existing `auth.status` operation): is auth on? This decides whether override mode can write the setting (Decision 5 and Review Focus 2).
  - (c) `GET /api/v1/settings/`: the current `auto_add_unknown_rfid`. Record the value in the spec §3 paragraph and in the PR body only.
  - (d) `GET /api/v1/spoolbuddy/devices`: the device ids, `has_nfc`, `nfc_ok`. If the list is empty, say so; Task 8's tests use a recorded body.
  - (e) `GET /api/v1/inventory/spools/by-tag?tray_uuid=<one tray_uuid from the recorded inventory-spools.json>`: the 200 body, and `...?tag_uid=00000000` for the 404 body.
  - (f) WebSocket `GET /api/v1/ws` (no token if (b) says auth is off; otherwise report that `POST /auth/ws-token` is needed and what scope refuses the key): listen 15 s, send nothing, record the `type` of the first frames.
  - (g) Record the `data_origin` values (`rfid_auto`, `rfid_linked`, `nfc_link`, `opentag3d`) from `spool_tag_matcher.py` (lines 265 and 417), `inventory.py` and `spoolbuddy.py` at `v1.3.1`, with line numbers, in the spec §3 paragraph.
  - (h) Permissions of the routes: read the dependency of each route in `spoolbuddy.py`, `inventory.py` (link-tag, `by-tag`, `PATCH /spools/{id}`) and the configure route in `printers.py` at tag `v1.3.1` (`Permission.*`, with line numbers) and map each through `_APIKEY_SCOPE_BY_PERMISSION` in `backend/app/core/auth.py` to a ScadBuddy `Scope`. Defaults to confirm: link-tag, by-tag, clear, write-tag, cancel-write: `MANAGE_INVENTORY`; configure: `CONTROL_PRINTER`; settings write: `ADMIN_SETTINGS`; devices list: `READ_STATUS`.
  - Not measurable without a write, so taken from the source with a line number and tested only with respx: the 409 body, the clear by `PATCH` null, the configure answer, the write-tag answer. Say that in the paragraph.
  If a fact does not hold, stop and report before Task 3.
- [ ] **Step 4:** Write `rfid-routes.json`, the README row, the spec §3 paragraph (date, URLs, endpoints, versions 1.3.1 against the spec's 1.2.6b1, the scope table), and in §7 replace the NTAG bullet's "OpenSpool data with the SB spool id" by the Decision of "Spec deviation" above, with its source lines.
- [ ] **Step 5:** Run the test — PASS. Commit `docs(filament): the Bambuddy RFID routes, measured read-only (#6264)`.

### Task 2: Override binding rules, the pending queue and the sync setting

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_rfid.sql`, `backend/scadbuddy/filament/rfid_push.py`
- Modify: `backend/scadbuddy/filament/spools.py` (`bind_on`, `unbind_tag`, `_archive`), `filament/models.py`, `core/settings.py` (`FilamentSync`), `core/setting_docs.py` (`filament_sync`: it says "ScadBuddy never writes to Bambuddy"), `backend/scadbuddy/api/filament.py` (`_SPOOL_REFUSED`, `_refused`: map `OverrideKindError` to 422)
- Test: `backend/tests/filament/test_rfid_binding.py`, `backend/tests/filament/test_migrations.py`, `backend/tests/test_settings*.py` (the file that pins `FilamentSync`), the test that pins `setting_docs.py`, `backend/tests/api/test_filament.py`

**Interfaces:**
- Produces three tables. `filament_bb_tag_pending(tag_id uuid PRIMARY KEY REFERENCES filament_tags ON DELETE CASCADE, queued_at timestamptz NOT NULL DEFAULT now(), attempts integer NOT NULL DEFAULT 0, last_attempt_at timestamptz, last_error text)`. `filament_rfid_state(singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), override_mode boolean NOT NULL DEFAULT false, saved_auto_add boolean, changed_at timestamptz NOT NULL DEFAULT now())` with its one row inserted. `filament_override_slots(printer_id <the type of printers.id> REFERENCES printers ON DELETE CASCADE, ams_id integer, tray_id integer, spool_id uuid NOT NULL REFERENCES filament_spools, params_hash text, configured_at timestamptz, attempts integer NOT NULL DEFAULT 0, last_error text, PRIMARY KEY (printer_id, ams_id, tray_id))`. The migration also queues every tag that has an open binding.
- Produces `rfid_push.enqueue_tag(conn: Conn, tag_id: UUID) -> None` (`INSERT ... ON CONFLICT (tag_id) DO UPDATE SET queued_at = now(), attempts = 0, last_error = NULL`).
- `FilamentSpoolStore.bind_on` calls `enqueue_tag` for the bound tag and for every tag it closes. `unbind_tag` and `_archive` (which closes the open binding) call it for the tag they close. The queue is filled whatever the sync setting says (cost: one row per tag, bounded by the tag count).
- `bind_on(..., override=True)`: raises `OverrideKindError(ValueError)` unless the tag's kind is `bambu_mifare` (the route maps it to 422); sets the spool's `weight_locked` to true when it is false, and records one `edited` event `data={"changes": {"weight_locked": [False, True]}}` in the same transaction. A bind of a tag already on the spool and already carrying the same `override` changes nothing. A held binding whose `override` differs is updated in place: the flag is set, the weight is locked once, one `edited` event is written, and no `tag_unbound`/`tag_bound` pair. Update the `bind_on` docstring to say so.
- `FilamentSync` gains `tags: Literal["off", "sb_to_bb"] = "off"`. Update its docstring and the `filament_sync` text in `setting_docs.py` (both said ScadBuddy only reads Bambuddy).
- `POST /filament/spools/{id}/tags` maps `OverrideKindError` to 422 through `_refused`.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_override_bind_locks_the_weight` (spool unlocked; `bind_tag(..., kind="bambu_mifare", tray_uuid=T, override=True)` → `spool.weight_locked is True`, one `edited` event whose `data.changes == {"weight_locked": [False, True]}`, the binding's `override is True`).
  - `test_an_override_on_an_ntag_is_refused` (`kind="ntag_opentag", override=True` → `OverrideKindError`; no binding, no event).
  - `test_a_second_override_bind_adds_no_second_lock_event` (bind the same tag twice → one `edited` event).
  - `test_override_on_a_held_tag_updates_the_binding` (tag bound with `override=False`, then bound again with `override=True` → same binding id, `override is True`, weight locked, one `edited` event, no new `tag_bound`); `test_the_route_answers_422_for_an_override_on_an_ntag` (API test).
  - `test_a_rebind_queues_both_tags` (bind T1 to A, then T2 to A → `filament_bb_tag_pending` holds T1 and T2); `test_an_unbind_and_an_archive_queue_the_tag`.
  - `test_the_migration_queues_open_bindings` (the `MIGRATIONS[:idx]` pattern of `tests/test_pg_migrations.py`: one open and one closed binding before it → only the open one's tag is pending after it; `filament_rfid_state` has one row, `override_mode` false, `saved_auto_add` null).
  - `test_filament_sync_tags_defaults_off_and_refuses_other_values`.
- [ ] **Step 2:** Run them — FAIL.
- [ ] **Step 3:** Write the migration; implement the rules, the enqueue and the 422 mapping; edit the `FilamentSync` docstring and `setting_docs.py`.
- [ ] **Step 4:** Run them and `tests/filament tests/test_pg_migrations.py` — PASS.
- [ ] **Step 5:** Commit `feat(filament): override bindings lock the weight; tags queue for Bambuddy (#6264)`.

### Task 3: The Bambuddy client for tags, settings, slots and SpoolBuddy

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py` (`Operation`, methods), `bambuddy/errors.py` (`TagAlreadyLinkedError`), `bambuddy/models.py` (`Spool.tag_type`, `Spool.data_origin`, `SpoolBuddyDevice`, `SlotConfigure`)
- Modify: `backend/tests/bambuddy/conftest.py` (`recorded_schema` searches `openapi/scadbuddy-routes.json` and `openapi/rfid-routes.json`; `shaped` is unchanged)
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
- Modify: `backend/scadbuddy/filament/rfid_push.py`, `filament/models.py` (`TagPushReport`, `SlotReport`), `backend/scadbuddy/spools/activities.py` (new activity), `workflows/spool_refresh.py` (`FILAMENT_RFID_ACTIVITY`, `FILAMENT_RFID_PATCH`, `_rfid()`), `backend/scadbuddy/main.py` (register it), `api/filament.py` (inline try after a bind)
- Test: `backend/tests/filament/test_rfid_push.py`, `backend/tests/test_spool_refresh_replay.py`, `backend/tests/test_spool_refresh_workflow.py` (its fake worker lists each activity), `backend/tests/fixtures/spool_refresh_histories/` (one new history)

**Interfaces:**
- Consumes: Task 2 `filament_bb_tag_pending`; Task 3 client methods; `filament_bb_links (spool_id, bb_spool_id)`; `FilamentSpoolStore.open_binding`.
- Produces `TagPushReport(pushed: int = 0, moved: int = 0, cleared: int = 0, unchanged: int = 0, failed: int = 0)`, and `SlotReport(configured: int = 0, unchanged: int = 0, no_preset: int = 0, no_nozzle: int = 0, failed: int = 0)` in `filament/models.py` (Task 6 fills it; defining it here lets Task 5's report name it).
- Produces `error_label(exc: ApiError) -> str` in `rfid_push.py`: the problem `type`, plus `:<bambuddy_status>` when the error has one. Tasks 5 and 6 use it.
- Produces `async def drain_tags(store: Filament, client: BambuddyClient, *, limit: int = 50) -> TagPushReport`. For each pending tag, ordered by `(queued_at, tag_id)` (a closed and a new tag of one `bind_on` share `queued_at`), in its own short transaction and try block:
  1. Read the tag's keys and its open binding. `desired` = the Bambuddy id in `filament_bb_links` of the bound spool, else `None`.
  2. `observed` = `client.spool_by_tag(tray_uuid=..., tag_uid=...)` (the BB spool's id, else `None`).
  3. `desired == observed`: delete the pending row; count `unchanged`.
  4. `desired` is set: `client.link_tag(desired, tag_uid=..., tray_uuid=..., tag_type="bambu" for kind bambu_mifare else "ntag", data_origin="nfc_link")`. On `TagAlreadyLinkedError`: `client.clear_spool_tag(holder_id)`, then `link_tag` again **once**; count `moved`. Else count `pushed`.
  5. `desired` is `None` and `observed` is linked to an SB spool (`filament_bb_links`): re-read that SB spool's open bindings first. If it now holds an open binding, do not clear (the tag moved to another kind, and clearing both keys would wipe the new tag in Bambuddy); count `unchanged`. Otherwise `clear_spool_tag(observed)`; count `cleared`. An unknown holder is left alone (Decision 7).
  6. On success, delete the pending row. On any failure, `attempts += 1`, `last_attempt_at = now()`, `last_error = error_label(exc)` (never the message), count `failed`, and go on with the next tag.
  7. The drain does not touch the pull's recorded BB snapshot. After a link the pull sees BB's tag equal SB's open binding, and `bind_on` returns the held binding unchanged (a test below pins it).
- The whole drain runs only when `filament_sync.tags == "sb_to_bb"`; otherwise it returns `TagPushReport()` and leaves the rows.
- Produces `FILAMENT_RFID_ACTIVITY = "filament_rfid_reconcile"` and `FILAMENT_RFID_PATCH = "filament-rfid"`. The activity (`spools/activities.py`, in the API process like `filament_pull`) is built by a factory that takes the arguments of `capture_backstop_activity` (#6590) plus the `Filament` component. In this task it runs `drain_tags` only. Task 5 adds `reconcile_mode` as its first step and Task 6 adds `reconcile_slots` as its third; each step fails alone. `SpoolRefresh.run` calls `_rfid()` **last**, after `_capture` (#6590), behind its own `workflow.patched(FILAMENT_RFID_PATCH)` on both branches, with `retry_policy=_ONCE`; a failed activity logs a warning and the run goes on.
- `POST /filament/spools/{id}/tags` (existing route) after its commit calls `drain_tags` once under `waiting_on_bambuddy()` with `asyncio.timeout(RFID_INLINE_SECONDS)` (`2.5`), swallowing any error and logging its type. The reply never waits on Bambuddy beyond that, and never fails because of it.

- [ ] **Step 1: Write the failing tests** (`test_rfid_push.py`, respx, a `Filament` over `pg_pool`, `filament_sync.tags="sb_to_bb"`):
  - `test_a_bound_tag_is_linked_once`: spool A linked to BB 11 holds tag T; respx `by-tag` 404 then `link-tag` 200 → `report.pushed == 1`, one PATCH to `/inventory/spools/11/link-tag`, the pending row gone; a second `drain_tags` makes no PATCH and its respx `by-tag` answers BB 11 → nothing pending.
  - `test_a_409_moves_the_tag` (Review Focus 1): `by-tag` answers BB 7; `link-tag` to 11 answers the recorded 409 (`spool_id: 7`) first; then the clear PATCH to `/inventory/spools/7` (body two nulls), then `link-tag` 200 → `report.moved == 1`, calls in exactly that order.
  - `test_a_failed_push_keeps_the_row` (Review Focus 1): `link-tag` raises a connect error → `failed == 1`, `attempts == 1`, `last_error == error_label(...)` of the mapped transport `ApiError` (its problem `type`, not `"ConnectError"` and not the message), the SB binding intact, the row still pending; the next drain with Bambuddy up pushes it and no second BB spool ends with T in the respx call log.
  - `test_an_unbind_clears_only_a_known_holder`: tag unbound in SB; BB holder 11 is linked → cleared; with BB holder 99 unlinked → no PATCH, `unchanged == 1`.
  - `test_a_rebind_across_kinds_never_clears_the_new_tag` (Review Focus 1): spool A (BB 11) holds NTAG T1, then Bambuddy tag T2 in one `bind_on`; both are pending with equal `queued_at`. Bambuddy still shows T1 on BB 11; the drain handles both by `tag_id` order and links T2; for T1 `desired` is `None` and A holds an open binding, so no clear PATCH is sent and the T2 link stays.
  - `test_a_spool_with_no_bambuddy_link_pushes_nothing` (desired `None`, observed `None` → `unchanged`).
  - `test_the_drain_is_off_unless_tags_is_sb_to_bb` (setting `off` → no HTTP call, rows stay).
  - `test_the_pull_after_a_push_changes_nothing` (after a push, `pull(...)` with the BB spool carrying T → `PullReport.updated == 0` and the SB spool still holds one open binding, same id).
  - `test_the_route_binds_even_when_bambuddy_is_down` (POST the tag route with respx raising `ConnectError` → 200 and the binding exists).
  - Replay: record one new history after the patch (`SCADBUDDY_RECORD_HISTORIES=1`) that carries all three markers; the old histories still replay. `test_spool_refresh_workflow.py`'s fake worker lists the new activity.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. Lock order stays tag, then spool.
- [ ] **Step 4:** Run `tests/filament tests/test_spool_refresh_replay.py tests/api/test_filament.py` and the quality commands — PASS.
- [ ] **Step 5:** Commit `feat(filament): push tag bindings to Bambuddy, idempotently (#6264)`.

### Task 5: RFID override mode and `auto_add_unknown_rfid`

**Files:**
- Create: `backend/scadbuddy/filament/rfid_mode.py`
- Modify: `filament/models.py` (`RfidState`, `RfidStatus`, `RfidReconcileReport`), `filament/component.py` (`Filament` gains `rfid: RfidModeStore`, built from its pool), `backend/scadbuddy/main.py` (the wiring), `api/filament.py`, `spools/activities.py` (the activity's first step), `agent/src/tools/filament.ts`, `agent/src/tools/coverage.ts`
- Test: `backend/tests/filament/test_rfid_mode.py`, `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`

**Interfaces:**
- Produces `RfidStatus(override_mode: bool, state: Literal["off", "applied", "pending", "blocked"], bambuddy_auto_add: bool | None, saved_auto_add: bool | None, detail: str | None, devices: list[SpoolBuddyDevice])`. `state` is `applied` when the mode is on and Bambuddy reads `false`; `pending` when on and a write has not landed or Bambuddy is down; `blocked` when Bambuddy refused the write with the `ADMIN_SETTINGS` 409 (`detail` carries its reason); `off` when the mode is off and nothing is saved. `detail` for a `pending` state caused by an error is `error_label(exc)`.
- Produces `RfidState` (the `filament_rfid_state` row as a model: `override_mode`, `saved_auto_add`, `changed_at`), distinct from `RfidStatus` (the route's answer).
- Produces `class RfidModeStore(pool)` (built once in `Filament`; the activity reaches it as `store.rfid`): `state() -> RfidState` (the row), `set_mode(enabled: bool) -> RfidState`, `save_found(value: bool) -> bool` (sets `saved_auto_add` only when it is `NULL`; returns whether it wrote), `clear_saved() -> None`.
- Produces `async reconcile_mode(store: RfidModeStore, client: BambuddyClient) -> RfidStatus`, **state-based**:
  - mode on: read `auto_add_unknown_rfid()`; `save_found(read_value)` **before** any write; if the value is `true`, `set_auto_add_unknown_rfid(False)`; then read it again.
  - mode off and `saved_auto_add` is set: `set_auto_add_unknown_rfid(saved)` if Bambuddy's value differs; on success `clear_saved()`.
  - mode off and nothing saved: no call.
  - A `blocked` or connect failure never clears `saved_auto_add` and never raises out of the activity.
- Routes (`prefix="/filament"`): `GET /rfid/status` (read-only: it reads Bambuddy and lists devices best effort, a Bambuddy error leaves the fields `None`/empty with `state` from the row); `PUT /rfid/override-mode` body `{enabled: bool}` writes the row first, then runs `reconcile_mode`, answers `RfidStatus` (200 even when `blocked`); `POST /rfid/reconcile` runs the steps of the activity that exist so far once and answers `RfidReconcileReport(mode: RfidStatus, tags: TagPushReport, slots: SlotReport | None)`; here `slots` is always `None` (Task 6 runs the third step and fills it).
- Tools: `filament_rfid_status` (read), `filament_rfid_override_mode` (outward, gated, Decision 33; "Only what the user says; this turns Bambuddy's auto-add of unknown RFID spools off while on"), `filament_rfid_reconcile` (outward, gated, Decision 33), each with `routes:`.

- [ ] **Step 1: Write the failing tests** (respx, `pg_pool`):
  - `test_turning_the_mode_on_saves_the_found_value_then_turns_auto_add_off`: Bambuddy reads `true` → `saved_auto_add is True` before the PATCH (assert by the call order), the PATCH body is `{"auto_add_unknown_rfid": false}`, `state == "applied"`.
  - `test_a_second_reconcile_never_overwrites_the_saved_value`: after the first, Bambuddy reads `false`; reconcile again → `saved_auto_add` is still `True`, no PATCH.
  - `test_a_flip_back_in_bambuddy_is_turned_off_again` (Review Focus 3): Bambuddy then reads `true` (a person flipped it) → one PATCH `false`; `saved_auto_add` still `True`.
  - `test_turning_the_mode_off_restores_the_original_value` (Review Focus 3): mode off → PATCH `{"auto_add_unknown_rfid": true}`, `saved_auto_add is None`, `state == "off"`. With the original `false` (a person had it off): Bambuddy already reads the saved `false`, so there is **no PATCH at all** (assert the respx log has none), and `saved_auto_add is None`.
  - `test_a_blocked_write_keeps_the_mode_and_the_saved_value` (Review Focus 2): 403 on PATCH → HTTP 200 from the route, `state == "blocked"`, `detail` names "auth is on", `override_mode is True`, `saved_auto_add` kept.
  - `test_bambuddy_down_leaves_the_mode_pending` (connect error arrives as `ApiError` 502 → `pending`, `detail == error_label(...)`, row unchanged).
  - `test_the_status_route_reads_without_writing` (no PATCH in the respx log).
  - Agent: `filament_rfid_override_mode` posts `{enabled: true}` to `PUT /api/v1/filament/rfid/override-mode`; `agent/test/coverage.test.ts` passes.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. The activity (Task 4) gains `reconcile_mode` as its first step, with the store from `store.rfid`, so a person flipping Bambuddy's value back is fixed within one refresh.
- [ ] **Step 4:** Run backend tests; `cd agent && pnpm gen:api && pnpm test filament coverage && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): RFID override mode and Bambuddy's auto-add (#6264)`.

### Task 6: Configure the slot of an override tag

**Files:**
- Create: `backend/scadbuddy/filament/rfid_slots.py`
- Modify: the activity of Task 4 (its third step), `api/filament.py` (`POST /rfid/reconcile` now fills `RfidReconcileReport.slots`). `SlotReport` already exists (Task 4).
- Test: `backend/tests/filament/test_rfid_slots.py`

**Interfaces:**
- Consumes: `FilamentInventory.trays(printer_id, seen) -> TrayView` (`TrayHolder.found_by`), `presets.effective_presets`, `preset_for`, `effective_k_values`, `PrinterReportStore`/`report_status`, `seen_trays`, `printers.bambuddy_printer_id`, `filament_override_slots`, `BambuddyClient.configure_slot`, `SlotConfigure`.
- Consumes `SlotReport` (Task 4, `filament/models.py`) and `error_label` (Task 4).
- Produces `installed_nozzle(report: Mapping[str, Any], ams_id: int) -> str | None`. Step 1 measures from a recorded report in `backend/tests/printers/fixtures` which key holds the installed nozzle diameter per extruder, and which extruder an AMS unit feeds. It cites the fixture and the key in its docstring. `None` when the report does not say.
- Produces `slot_params(spool: Spool, presets, k_values, printer_model: str, nozzle: str, tray_color: str) -> SlotConfigure | None` (`None` when `preset_for(presets, printer_model, nozzle)` is `None`): `tray_info_idx` is the preset's `slicer_filament`; `tray_type` the effective material name; `tray_sub_brands` the preset's `slicer_filament_name`, else the subtype name; `tray_color` the spool's effective `rgba` upper-case with no `#`; temperatures the effective ones; `cali_idx`, `setting_id`, `k_value` from the `EffectiveKValue` row of this printer, the extruder the AMS feeds and the nozzle, else `cali_idx=-1`, `k_value=None`.
- Produces `async def reconcile_slots(store: Filament, client: BambuddyClient, *, printers: PrinterStore, reports: PrinterReportStore) -> SlotReport`. For each registry printer that has a `bambuddy_printer_id` and a fresh report: for each filled tray, resolve through `trays`; only a holder with `found_by == "tag"` whose spool has an **open binding with `override=True`** counts. Compute `slot_params`; The slot counts as `unchanged` only when **both** hold: `digest()` equals `filament_override_slots.params_hash` for `(printer, ams, tray)` and the same `spool_id` (the hash is an attempt guard: what SB sent), **and** the report's current tray values (`tray_info_idx`, `tray_type`, colour) equal those of `slot_params`. The AMS re-reads a Bambu tag on reload and resets the slot, so SB never trusts the hash alone. Any difference: `configure_slot`, then upsert the row with the hash and `configured_at = now()`. A failure records `attempts`, `last_error = error_label(exc)` and goes on. The activity's third step calls it and fills `RfidReconcileReport.slots`. A missing preset is `no_preset`, a missing nozzle `no_nozzle`; neither calls Bambuddy.
- Bambuddy off (`client_for` raises the not-configured 409): the step is skipped.

- [ ] **Step 1: Write the failing tests** (respx; a printer row, a report fixture with one tray carrying tray uuid `T`, material `PLA`, colour `#FF0000FF`; spool A of type "Acme PLA" with preset H2C/0.4 `GFL99` bound to `T` with `override=True`):
  - `test_an_override_tag_resolves_through_trays` (the tray's holder is A, `found_by == "tag"`; runs unchanged after the #6590 merge).
  - `test_an_override_slot_is_configured_with_the_types_preset`: one POST to `/printers/1/slots/0/1/configure`; its query has `tray_info_idx=GFL99`, `tray_color=FF0000FF`, `nozzle_diameter=0.4`, `tray_type=PLA`.
  - `test_a_second_reconcile_makes_no_call`: the row's hash equals the digest and the report's tray already shows `GFL99`, `PLA`, red → `unchanged == 1`, no POST.
  - `test_a_reloaded_tray_is_configured_again`: the row's hash equals the digest, but the report's tray now shows the Bambu tag's own `tray_info_idx` and material (the AMS reloaded) → one POST, `configured == 1`.
  - `test_a_changed_preset_configures_again` (edit the preset to `GFL98` → one POST).
  - `test_a_plain_binding_is_not_configured` (`override=False` → no POST, the Bambu tag's own settings stand).
  - `test_no_preset_for_the_nozzle_calls_nothing` (only H2C/0.6 exists → `no_preset == 1`).
  - `test_a_failed_configure_is_retried_next_time` (500 → `failed == 1`, `attempts == 1`, `last_error == error_label(...)`, no hash stored; next run POSTs).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/filament tests/printers` — PASS.
- [ ] **Step 5:** Commit `feat(filament): configure the slot of an override tag (#6264)`.

### Task 7: The pull and tags: NTAG kind, and no duplicate for an auto-added spool

**Files:**
- Modify: `backend/scadbuddy/filament/bambuddy_pull.py` (`_bind`, step 3, `PullReport`), `frontend/src/mocks/features/filament/index.ts` (the `PullReport` mock `satisfies S['PullReport']`: add `rfid_duplicates` and `rfid_duplicate_bb_ids`, or typecheck fails), `frontend/src/api/filament.ts` only if `PullReport` is typed there
- Test: `backend/tests/filament/test_bambuddy_pull.py`

**Interfaces:**
- `_bind`'s kind rule: `bambu_mifare` when the tray uuid is set; else `ntag_opentag` when `bb.data_origin == "opentag3d"`; else `other`. `_bind` gets the BB spool (or its `data_origin`) as a parameter.
- `PullReport.rfid_duplicates: int = 0` and `rfid_duplicate_bb_ids: list[int]`. A BB spool with no link whose tag keys are held by an **open binding (override or not)** of an SB spool that **already has a BB link to another BB spool** creates no SB spool. It is counted and logged (`a Bambuddy spool made for a tag a linked spool holds is left alone`, a warning the first time for a BB id, then debug). Step 2 (link by tag) stays for an SB spool with no BB link.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_opentag_spool_binds_as_ntag_opentag` (BB spool with `tag_uid="04AABB", data_origin="opentag3d"`, no tray uuid → the SB binding's tag kind is `ntag_opentag`); `test_a_plain_uid_stays_other`; `test_a_tray_uuid_stays_bambu_mifare`.
  - `test_an_auto_added_bb_spool_for_an_override_tag_makes_no_second_sb_spool` (Review Focus 5): SB spool A linked to BB 11 holds override tag T; BB spool 40 (`data_origin: "rfid_auto"`, `tray_uuid: T`) appears → no new SB spool, `rfid_duplicates == 1`, `rfid_duplicate_bb_ids == [40]`, A's binding unchanged.
  - `test_an_unlinked_sb_spool_with_the_tag_is_still_linked` (SB spool B holds T override, no BB link; BB 40 carries T → B is linked to 40, as step 2 does today).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. #6590's `PullReport` fields are already on the branch (Task 0).
- [ ] **Step 4:** Run `tests/filament/test_bambuddy_pull.py`; `cd frontend && pnpm gen:api && pnpm typecheck` — PASS.
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
- Routes: `POST /filament/rfid/scan` body `{timeout_s: int (1..50, default 30), device_id?: str}`; `POST /filament/spools/{id}/ntag/write` body `{device_id: str, replace: bool = False}`; `POST /filament/spools/{id}/ntag/confirm`; `POST /filament/spools/{id}/ntag/cancel` body `{device_id: str}`. 404 for an unknown spool. Each runs on the API process, with `actor(caller)`.
- Tools: `filament_rfid_scan` (waits up to the given seconds; "the user holds a tag to a SpoolBuddy reader"), `filament_spool_ntag_write` (outward, gated), `filament_spool_ntag_confirm` (write), `filament_spool_ntag_cancel` (outward, gated; Decision 33).
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
  - API: scan with `timeout_s=51` → 422; the scan reply on a timeout is 200 with `status == "timeout"`.
  - Agent: `filament_spool_ntag_write` posts `{device_id, replace: false}`; `coverage.test.ts` passes.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. The WS read is `asyncio.timeout(timeout)` around `websockets.asyncio.client.connect`; the URL is built from `BambuddyConfig` (`https` to `wss`, `http` to `ws`).
- [ ] **Step 4:** Run backend tests; `cd agent && pnpm gen:api && pnpm test filament coverage && pnpm typecheck`; `cd frontend && pnpm gen:api && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): NTAG scan and write through SpoolBuddy (#6264)`.

### Task 9: Spool labels with a number and a QR code

**Files:**
- Create: `frontend/src/pages/filament/LabelsPage.tsx`, `frontend/src/components/filament/Label.tsx`, `frontend/src/components/filament/labelSheet.ts`
- Modify: `frontend/package.json` (`qrcode`, `@types/qrcode`, each an exact version at least two weeks old; the lockfile), the filament routes (`/filament/labels`) in `frontend/src/App.tsx`, the literal route paths in `frontend/e2e/page-titles.spec.ts` (add `/filament/labels` and its title), `frontend/src/components/filament/BulkBar.tsx` ("Labels"), `frontend/src/pages/filament/SpoolPage.tsx` ("Print label"), `frontend/src/pages/filament/tabs.ts` only if a route registry lives there, the Filament mocks in `frontend/src/mocks/features/filament/`
- Test: `frontend/src/components/filament/labelSheet.test.ts`, `Label.test.tsx`, `pages/filament/LabelsPage.test.tsx`, `BulkBar.test.tsx`

**Interfaces:**
- Produces `labelPayload(spoolId: string): string` = `` `scadbuddy:spool:${spoolId}` `` (Decision 12). `parseLabelPayload(text: string): string | null` returns the id or `null`.
- Produces `qrSvg(payload: string): Promise<string>` using `QRCode.toString(payload, { type: 'svg', errorCorrectionLevel: 'M', margin: 0 })`.
- Produces `Label({ spool, svg })`: 50 x 25 mm; the SVG at 19.8 mm inside a 4-module quiet zone (Decision 2); `#<number>` large; material and colour name; brand. A long colour name truncates with an ellipsis and never overflows the 50 mm.
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
- Modify: `frontend/src/components/filament/TagPanel.tsx` (rewrite its doc comment, which says "Move it here sends `override: true`"), `frontend/src/pages/filament/SpoolPage.tsx` (mount `NtagPanel`), `frontend/src/pages/settings/sections/filament.tsx`, `frontend/src/api/filament.ts`, `frontend/src/mocks/features/index.ts` (register `rfid.ts`'s `handlers` and `reset`)
- Test: `components/filament/TagPanel.test.tsx`, `NtagPanel.test.tsx`, `pages/settings/sections/filament.test.tsx`

**Interfaces:**
- `TagPanel`: the checkbox now reads "This Bambu tag belongs to a different filament (override)", is enabled only for a tag with a tray UUID, and sends `override` to the bind route. "Move it here" sends `override: false` (Decision 3). A "Scan a tag" button calls `scanTag` (30 s), shows "Hold the tag to the reader" while it waits, fills the UID, tray UUID and kind from the result, and says which spool already holds it (`sb_spool`). The TagPanel shows the open binding's "Override" mark and a line "Weight locked: the AMS percentage never changes grams left".
- `NtagPanel({ spool })`: a device picker from `getRfidStatus().devices` (only `has_nfc`); "Write NTAG" calls `writeNtag`; then polls `confirmNtag` every 2 s for 60 s and says "Bound tag <uid>" or "Not seen yet"; "Cancel" calls `cancelNtag` (also after "Not seen yet", and on unmount). The `override_tag_open` 409 shows a confirm "This replaces the Bambu tag the AMS reads. Replace it?" that retries with `replace: true`. The `no_bambuddy_spool` 409 shows its detail. Warnings from the write are listed.
- Settings → Filament gains: "Tags to Bambuddy" (`Off` / `Send ScadBuddy's bindings`) bound to `filament_sync.tags`; "RFID override mode" (a switch that calls `setOverrideMode`) with a status line from `getRfidStatus()`: `applied` "Bambuddy no longer adds unknown RFID spools", `pending`, `blocked` with its `detail` (the auth-on reason), and "Apply now" (`reconcileRfid`). No hard-coded choice: both are remembered settings.
- The mocks serve `/api/v1/filament/rfid/*` and `…/ntag/*`; `rfid.ts` exports `handlers` and `reset` and owns those routes (one route, one feature module); `mocks/features/index.ts` registers it. `TagsTab` already shows an Override column (`TagsTab.tsx` line 158): no change.

- [ ] **Step 1: Write the failing tests:**
  - `TagPanel.test.tsx`: `test_move_it_here_is_not_an_override` (the POST body has `override: false`); `test_the_override_checkbox_needs_a_tray_uuid` (disabled with only a UID; enabled with a tray UUID; checked, the body has `override: true` and `kind: "bambu_mifare"`); `test_scanning_fills_the_form` (msw answers `status: "tag"` with `tag_uid`, `tray_uuid` null, `kind: "ntag_opentag"` → the inputs hold them and the kind select reads `ntag_opentag`); `test_a_scan_timeout_says_so` (no crash, "No tag seen").
  - `NtagPanel.test.tsx`: write then confirm: msw `confirm` answers `bound: false` once, then `bound: true` → "Bound tag <uid>" after two polls (fake timers); the `override_tag_open` 409 shows the replace confirm and the second POST has `replace: true`; the `no_bambuddy_spool` 409 shows its detail and no polling starts.
  - `filament.test.tsx` (settings): switching override mode on posts `{enabled: true}`; a `blocked` status shows its `detail` and the switch stays on; the "Tags to Bambuddy" select saves `filament_sync.tags`.
  - e2e `filament-rfid.spec.ts` (msw): bind a Bambu tag with override on a spool, see the override mark and the weight-lock line; write an NTAG and see "Bound tag <uid>"; open `/filament/labels?ids=...` and see a `#N` label with an `<svg>`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. Load `frontend-design:frontend-design` first.
- [ ] **Step 4:** Run `pnpm gen:api && pnpm lint && pnpm typecheck`, the vitest files, `pnpm exec playwright test e2e/filament-rfid.spec.ts` — PASS.
- [ ] **Step 5:** Commit `feat(filament): RFID panels, override mode and NTAG in the UI (#6264)`.

### Task 11: Notes, whole-branch check and PR

**Files:**
- Modify: `backend/CLAUDE.md` (the filament layout lines: the four new modules, the `filament_rfid_reconcile` step, `filament_sync.tags`, the pending table, the rule "SB first, Bambuddy second"), `frontend/CLAUDE.md` (labels page, override panel, the `localStorage` key), `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§7 "As built in #6264": the override rules, the label format and the QR payload prefix `scadbuddy:spool:`, the scan and write calls; §12: the `tags` sync value; §11: label templates and BB `label-templates` listed as a parity gap for #6270)

- [ ] **Step 1:** Confirm main is merged (Task 0). Fix every note this PR makes false: `grep -rn "override\|read-only toward Bambuddy\|never writes to Bambuddy\|pull only" backend/CLAUDE.md frontend/CLAUDE.md backend/scadbuddy/core docs/superpowers/specs/2026-11-14-filament-management-design.md` (`backend/CLAUDE.md` line 731 says "read-only toward Bambuddy") and `grep -rn "Labels with QR" docs` (the #6262 plan says labels go to #6264: it is now true).
- [ ] **Step 2:** Run the whole check: `uv run --frozen pytest tests/filament tests/bambuddy tests/printers tests/spools tests/api/test_filament.py tests/api/test_openapi.py tests/api/test_routes.py tests/test_spool_refresh_replay.py tests/test_pg_migrations.py`, ruff and mypy; `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm test`; `cd agent && pnpm gen:api && pnpm test && pnpm typecheck`; `lint-base64-images.sh`.
- [ ] **Step 3:** Open the PR `feat(filament): RFID tags, overrides, NTAG and labels (#6264)`, body `Closes #6264`, the Task 1 measurements (date, versions, URLs; the auto-add value only here), the spec deviation (OpenTag3D, not OpenSpool with an SB id), the defaults (`tags` off), the session marker, and the footer. Shepherd it.

## Decisions made while building (#6264)

Each line: what was decided — why — what it costs if wrong. Where a later round changed a ruling, this is the final state.

1. **NTAG deviation.** The tag carries OpenTag3D data of the linked Bambuddy spool and is bound by UID as `ntag_opentag`, not OpenSpool data with an SB id — Bambuddy's write path has no field for another id, so the spec text could not be built (§7 corrected in Task 1) — if wrong: a later Bambuddy field lets SB add the id; the UID binding still holds.
2. **QR quiet zone.** The `qrcode` library margin stays 0, and the label gives the QR a layout quiet zone of 4 modules (2.6 mm) on every side, so the QR is 19.8 mm, not the 21 mm of Decision 12 — a scanner needs the quiet zone; the plan's `margin: 0` was only the library option — if wrong: a slightly smaller QR.
3. **Override is checked against the stored tag kind.** `override: true` is valid only when the stored tag's kind is `bambu_mifare`, whatever kind the caller sends (422 `OverrideKindError`, rolled back with a new NTAG tag), and the UI shows the box only for a Bambu tag — the caller's kind can be wrong — if wrong: a refusal a person sees.
4. **Override off keeps the weight locked.** Turning an override binding off queues the tag but does not unlock the weight (Decision 3) — SB cannot know the weight was ever right again — if wrong: a person unlocks it by hand.
5. **Slot rows cascade with their spool.** `filament_override_slots.spool_id` has `ON DELETE CASCADE` (the migration was unmerged, so edited in place) — a slot row means nothing without its spool — if wrong: nothing.
6. **`SlotConfigure` requires three params.** The recorded route needs them, so the model has no default for them and a recorded-route test pins it — a default would send a value SB did not choose — if wrong: a configure that fails with 422 at Bambuddy.
7. **The error label for a non-`ApiError` is the fixed text `internal`.** The class name is not stored in `last_error` — a class name leaks code detail into a row a person reads — if wrong: a less specific label.
8. **The drain takes a try-lock.** `push_pending` holds `scadbuddy:filament:rfid_push` on its own connection; a concurrent drain returns empty — two drains would race the same Bambuddy tag — if wrong: a drain is skipped until the next tick.
9. **`push_pending` gates on the settings.** The wrapper `push_pending(store, load, sync)` reads `filament_sync.tags` and calls `drain_tags` only for `sb_to_bb` — `drain_tags` cannot read settings and stays pure — if wrong: one wrapper rename.
10. **`blocked` is the client's mapped `ADMIN_SETTINGS` refusal,** whatever Bambuddy's HTTP status (the brief said 409, a test 403) — the client already maps the refusal to one scope — if wrong: one mapping.
11. **Activity steps fail alone.** `filament_rfid_reconcile` runs the mode, the push and the slots each in its own try, mode first; the activity reports failed if any fails, and `POST /filament/rfid/reconcile` runs the same steps, each in its own try (a step that fails answers `pending` with detail `internal` for the mode, and `failed: 1` for the tags and the slots) — one broken step must not stop the others — if wrong: a retry that repeats a good step (they are state-based, so it costs a read).
12. **Mode SQL is guarded and the mode is locked.** `save_found` and `clear_saved` are conditional in SQL, and `reconcile_mode` holds the session advisory lock `scadbuddy:filament:rfid_mode` from its state read to its last write; the activity and `POST /reconcile` try the lock and skip (not a failure); `PUT` waits up to 5 s, then answers pending with detail `busy` — a racing PUT would lose the saved value — if wrong: a PUT answers `busy` under contention.
13. **No Bambuddy configured means pending, not a failure.** The mode step is skipped, the status says pending with detail `no Bambuddy`, and the activity does not fail — there is nothing to retry — if wrong: a noisy warning.
14. **`GET /filament/rfid/status` reports `blocked`** when the saved refusal stands and Bambuddy answered — the person sees why the mode is not applied — if wrong: a stale `blocked` until the next reconcile.
15. **A slot whose holder no longer qualifies gets no write, and its row is deleted.** SB cannot know the tag's own values (the AMS re-reads on reload); deleting the row lets a later override configure again — if wrong: a tray keeps SB's preset until the AMS reloads.
16. **Tray temperatures fall back, then count.** With no effective temperatures SB uses the tray's reported `nozzle_temp_min` and `nozzle_temp_max`; with none at all it counts `SlotReport.no_temps` and makes no call — a configure needs both — if wrong: one counter.
17. **`rfid_slots` holds a lock.** `reconcile_slots_now` holds the advisory lock `scadbuddy:filament:rfid_slots` for the whole step, so two runs never configure one tray twice — if wrong: a step skipped until the next tick.
18. **Repeated reconfigures warn after 3.** A slot configured 3 times in a row with unchanged params logs a warning, because an AMS that does not report back what was sent would be configured on every refresh; #6270's live sweep checks it — if wrong: a log line.
19. **Duplicates are any open binding.** The pull skips an unlinked Bambuddy spool whose tag any open binding (not only an override) of an SB spool linked elsewhere holds, and warns once — a second SB spool for one physical spool is never right — if wrong: a Bambuddy spool stays unmirrored until linked by hand.
20. **`device_id` has a strict pattern and is quoted.** `DeviceId` is `^[A-Za-z0-9._:-]{1,50}$`, and every client path with a device id goes through `urllib.parse.quote(safe='')` — an id reached a Bambuddy path unencoded (`../x` is 422) — if wrong: a device id outside the pattern is refused.
21. **A matched NTAG's kind comes from SB's binding, then Bambuddy's `data_origin`.** `ntag_opentag` when either says so — a matched scan has no kind of its own — if wrong: a tag shows the wrong kind until the pull.
22. **Scan `timeout_s` is at most 50.** The ingress answers at 60 s, so a longer listen would die as a gateway error (Decision 10's "60 s" is now 50 s) — if wrong: a scan ends 10 s earlier than it could.
23. **A connect or close failure is not a timeout.** Both map to `ApiError` (502), not to "no tag seen" — a person must tell an unreachable reader from an empty scan — if wrong: a misleading message.
24. **The WebSocket logger is at WARNING.** `websockets` logs its request line, `?token=` included, at DEBUG; the client's logger is set to WARNING so the token never reaches a log — if wrong: a token in a debug log.
25. **UI wording.** A scan that ends in 502 or 504 reads "Could not reach the reader through Bambuddy", apart from "No tag seen"; the detail `busy` reads "Another update is running; it applies on the next refresh" — each cause needs its own next step — if wrong: wording only.
26. **The weight-lock line comes from `spool.weight_locked`,** not from the override binding — the lock outlives the override (ruling 4) — if wrong: a wrong line on a spool with a lock but no override.
27. **"Tags to Bambuddy" saves with "Save Filament".** It joins the section's own save; the override-mode switch stays immediate with "Apply now", as an action with a status — a setting that saves alone would surprise a person — if wrong: one extra click.
28. **A pull never changes a held binding's `override`.** `bind_on(..., override=None)` keeps the flag; the pull and a tray link pass `None`, a new binding then takes False — the push links an override tag on its BB spool, the next pull sees new tag keys and would have flipped the binding off (slot row dropped, weight lock kept, tag queued again) — if wrong: a person who unticks override in Bambuddy-side data cannot undo it through a pull; only the bind route changes it.
29. **A skipped duplicate is remembered** in `filament_bb_ignored` (`bb_spool_id`, `reason` `rfid_duplicate`, the tag keys, `recorded_at`; in the branch's own unmerged migration) — the push clears the duplicate's tag, and an untagged unlinked BB spool is otherwise a new SB spool one refresh later, which Decision 19 forbids — if wrong: a BB spool that a person later wants as its own SB spool stays skipped until its row is deleted by hand (no UI for it).
30. **The reconcile activity has one 6 minute budget for its three steps.** A Temporal timeout is per activity, and one activity per step changes the recorded commands; 6 minutes covers the mode (5 s of lock wait and a few calls), up to 50 tags at four calls each, and a configure per override tray, and held, pull, capture and this step still add up to the run's 15 minutes — if wrong: a very slow Bambuddy cancels the activity mid-step; the next tick repeats it, as every step is state-based.
31. **Lock attempts survive a cancel.** `acquire_lock` runs the connect in a task that a cancel does not stop and closes its result when it lands — a cancelled inline push or a client that left would drop an unclosed connection and keep the advisory lock until garbage collection — if wrong: none beyond a few lines of code. The mode lock polls every 250 ms, not 50 ms (about 20 connects in 5 s, not 100).
32. **The drain takes rows with the fewest attempts first,** then oldest — a row that always fails (an archived target answers 400) would otherwise starve the rest past the 50-row limit — if wrong: a row that failed once waits behind fresh ones; it is still sent in the same run when fewer than 50 are queued.
33. **Four tools that write Bambuddy are `outward` and gated:** `filament_rfid_override_mode`, `filament_rfid_reconcile`, `filament_spool_ntag_write` and `filament_spool_ntag_cancel` — each changes a Bambuddy setting, an AMS slot or a tag on a reader, as the virtual-printer tools do (`printers.ts`). Bind and unbind stay `write`: their Bambuddy push is the sync setting's side effect, and their descriptions say so — if wrong: one more approval per call for the user; lowering a tier is an override in Settings.
34. **The Override mark is read again after every bind** (`TagPanel` counts its own changes in the `useAsync` key) — `bind_on` flips a held binding's `override` in place, so the tag id does not change and the first read went stale — if wrong: one extra GET per bind.
35. **NTAG panel leaves nothing queued.** "Cancel" is offered after "Not seen yet", and unmounting the panel sends a cancel for a write it queued — a write left on the reader would tag a spool nobody watches; Bambuddy queues one write per device, so the cancel drops that device's write, not only this spool's — if wrong: a write another person queued on the same reader is dropped too.
36. **Docs about Bambuddy writes say what writes.** The tags setting gates only the tag push; the override mode, the slot configure and the NTAG write and cancel write Bambuddy whatever it says (`setting_docs`, `FilamentSync`, `api/filament.py`, `backend/CLAUDE.md`, Settings help text) — a sentence that said otherwise was false — if wrong: none.
37. **`next_tag_scan` passes `proxy=True`** to `websockets.connect`, as httpx follows the proxy environment — checked: `websockets` 17.1's `connect` takes `proxy: str | Literal[True] | None = True` (the `>=15` pin has it) — if wrong: a deployment behind `HTTP_PROXY` with no `NO_PROXY` for Bambuddy sends the socket through the proxy, as it does its HTTP calls.
38. **NTAG confirm compares with a baseline taken at the write** (review of #6617). `ntag/write` reads Bambuddy's record and stores its `tag_uid` in `filament_ntag_writes` (the unmerged RFID migration gained the table); `confirm` binds only a UID that differs from it, and binds nothing when no write is queued, so a tag the person just unbound, still on the record, is not bound again — if wrong: one more Bambuddy read per write.
39. **The NTAG reader is fixed while a write is queued.** The Reader select is disabled in `waiting` and `missed`, and Cancel posts `queued.current ?? deviceId`, so Cancel always reaches the reader that holds the write — if wrong: none.
40. **`unbind_tag` pushes at once like bind** (`_push_now`, at most 2.5 s, `filament_sync.tags` `sb_to_bb` only); otherwise the refresh sends the clear — the tool text says exactly that — if wrong: a clear waits for the next refresh.
41. **Slot configure ignores the override mode, by design.** `rfid_slots.py` configures the slot of every open `override` binding whatever `filament_rfid_state.override_mode` says; the migration comment and the tool text no longer say "while override mode is on" — if wrong: a slot is configured with the mode off.
42. **"Apply now" reports failures.** `reportLine` adds `tags.failed` and `slots.failed` to its sentence, so a failing reconcile never reads as success — if wrong: none.

## Self-review

- Spec §7 bullet 1 (override binding): binding and weight lock Task 2; slot configure Task 6; `link-tag` with 409 move Task 4; `auto_add_unknown_rfid` off while the mode is on, with restore Task 5. §7 NTAG: Tasks 3 and 8 (with the OpenTag3D correction, Task 1). §7 labels: Task 9. §5.4 re-bind events: already in `bind_on`; Task 2 adds the queue. §8.1 "Tag binding → `link-tag`": Task 4. §11 SpoolBuddy "NFC scan and write": Task 8; "Settings: `auto_add_unknown_rfid`": Task 5, 10. The issue's comment (labels with QR): Task 9. Gap: BB label templates and `/inventory/labels` PDF output are not matched; Task 11 lists them for #6270.
- Types agree across tasks: `TagPushReport` and `SlotReport` (defined in Task 4; used by Tasks 5, 6), `error_label` (Task 4; Tasks 5, 6), `RfidState` row against `RfidStatus` answer (Task 5), `RfidStatus` (Tasks 5, 8, 10), `TagAlreadyLinkedError(holder_id, field)` (Tasks 3, 4), `SlotConfigure.digest()` (Tasks 3, 6), `FilamentSync.tags` (Tasks 2, 4, 10), `OverrideKindError` (Task 2, mapped to 422 in the same task), `reconcile_mode` (Task 5), `reconcile_slots` (Task 6).
- #5295: Tasks 4, 5, 6, 8 call Bambuddy from the API process only; no new pool, no worker route, no grant.
- Plan length is a fraction of the code it describes; bodies appear only where a signature and tests do not decide them.
