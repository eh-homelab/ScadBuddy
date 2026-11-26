# Unknown Spool Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a printer reports a loaded tray that no ScadBuddy spool holds, ScadBuddy records it, makes and links a spool at once for an RFID tray, asks the owner which spool an untagged tray holds, and refuses ScadBuddy's own prints to that printer until the owner answers.

**Architecture:** A table `filament_unknown_trays` holds one row per unmatched, untagged, loaded tray. **Python (existing code, extended):** a capture step (`capture.note_unknown`, its own savepoint in the report write) writes and clears the rows and makes RFID spools; `prepare_run` and `lan_print` refuse. **Go (`tools/go/core`, owner policy of epic #7667; changed 2026-11-26):** the three public routes (list with ranked candidates, resolve in one transaction, dismiss) and the print worker's CRUD read of the block, in the new package `internal/unknowntrays`, area `unknown-trays`, on the person-auth helper of #8038. The frontend adds a "Needs you" source, a resolve dialog and its entry points; the agent gets two tools. Go serves the area only when `SCADBUDDY_CORE_AREAS` names it: an out-of-repo clusters step (Task 9) switches it on.

**Tech Stack:** Python 3.12, psycopg 3, Postgres 18, pytest, Temporal activities (the capture and block parts); Go (`tools/go/core`: pgx, `internal/auth`, `internal/crud`, `testdb`); React 19, vitest, msw; the agent's TypeScript tools (zod, vitest).

**Spec:** `docs/superpowers/specs/2026-11-23-unknown-spool-flow-design.md`

**Issue:** #7143, in the spool onboarding epic [#7171](https://github.com/eh-homelab/ScadBuddy/issues/7171).

## Global Constraints

- Table `filament_unknown_trays`: `id`, `printer_id` (FK `printers`, `ON DELETE CASCADE`), `ams_id`, `tray_id`, `tray_uuid`, `tag_uid`, `material`, `colour_rgba`, `remain_pct` (nullable), `first_seen_at`, `seen_at`; unique `(printer_id, ams_id, tray_id)`. A row exists while the tray is loaded and unmatched; resolving, dismissing (D10) or emptying deletes it. Three cases keep a row as it is: a tray that is loading, an AMS unit the report no longer shows, and a tray whose lock is busy (D11).
- Routes, **all served by Go (G1, G2; changed 2026-11-26)**: `GET /api/v1/filament/unknown-trays`, `POST /api/v1/filament/unknown-trays/{id}/resolve`, `POST /api/v1/filament/unknown-trays/{id}/dismiss` (D10), and the worker's `GET /api/v1/internal/crud/print/filament/printers/{printer_id}/unknown-trays` (the spec's D7). Python has no handler for any of them. The area name is `unknown-trays` (`unknown-trays:read` for the two GETs).
- Problem codes: `tray_changed` (409), `code_unknown` (404, reused from `api/codes.py`), `spool_archived` (409), `no_ams_kind` (409), and the block's `unknown_spool_loaded` (422). Use these strings everywhere: backend, mocks, frontend, agent.
- Block text, word for word: `"<printer name> has an unknown spool in <slot>: link it on the Filament page first."`; several slots join with `", "` and a final `" and "`. `<slot>` is `bambuddy/trays.py` `tray_label` ("AMS-A slot 2").
- Needs-you title: `"Unknown spool in <printer name> <slot>"` ("Unknown spool in H2C AMS-A slot 2").
- Codes parse as `codes/parse.py` `parse_code` (any case, dash optional); only prefix `S` names a spool here.
- Candidates: at most 10, unarchived spools only.
- Only the API process and `tools/go/core` touch the database (#5295 and its #7667 exception; changed 2026-11-26). The print worker reads the block through the Go CRUD route with its service credential. Go checks a public route's caller with `internal/auth` (#8038), never with a check of its own.
- New migration file only; no new event kind (the resolve books `weighed`, capture books `created`, `loaded`, `moved`, `ams_reading`).
- Every capture write stays inside the report write's transaction, in a savepoint of its own; a failure is logged (`prints.FILAMENT_CAPTURE_FAILED`, `what="unknown_trays"`) and never fails the report write.
- A tray ScadBuddy can match is never recorded unknown. An untagged spool is never linked by a heuristic.
- Prints started in Bambuddy or on the printer are not blocked; only `prepare_run` and `lan_print` refuse.
- **No local test runs (G15; changed 2026-11-26).** Locally run only `gofmt -l`, `go vet`, `go build`, `ruff`, `mypy`, `eslint` and `typecheck`. CI runs every test (`backend-pytest`, `go`, `frontend*`, `agent`). Each behaviour still gets its test first; "Step 2" type-checks it and CI shows the rest. Go tests use `tools/go/core/internal/testdb`.
- Go-served routes have no OpenAPI entry, so `pnpm gen:api` writes no types for them: the frontend's and the agent's types for these routes are hand-written (G12). Never commit `backend/openapi.json` or a `schema.d.ts`.
- Float and JSON formatting: Go writes these routes' numbers with `encoding/json`; Python serves none of them, so no parity with Python's text is needed (G10).
- Commits: conventional title ending `(#7143)`, body ending with the attribution lines that the executing session's system reminder gives (changed 2026-11-26: the earlier fixed trailer named another session).

## Settled "Left to the builder" items

- **Colour distance:** `bambuddy/filaments.py` `colour_distance` (CIEDE2000, alpha dropped), the one `close_spools` and `inventory.tray_agreement` use. The material check is `inventory.material_key` against `inventory.material_names` (#6706 keys), as `tray_agreement` does.
- **RFID type match:** no existing helper matches by material and colour alone (`SpoolTypeStore.match` needs brand, subtype and label weight; a tray reports none of them). New `SpoolTypeStore.match_tray`: the oldest unarchived type whose material has the tray's `material_key` and whose `rgba` equals the tray's `rgba`, a type with a preset whose `slicer_filament` equals the tray's `tray_info_idx` first. No match: a spool with no type, its `material_id` from `catalog.resolve_or_add(conn, "materials", material, sort_order=1000)` and the tray's `rgba`. That is what the Bambuddy pull does (`bambuddy_pull._create` makes a typeless spool; it never makes a type), and type proposals group it later.
- **Polling cadence:** `ATTENTION_POLL_MS` (15 s, `frontend/src/agent/attention.ts:26`), plus a read on `visibilitychange` and `focus`, as `useGrantRequests` (`frontend/src/lib/needsYou.ts`) reads the flow grants.
- **Test split (changed 2026-11-26):** row SQL, capture, `link_on` and the vectors in `backend/tests/filament/`; the list, ranking, resolve, dismiss and the block read in Go (`tools/go/core/internal/unknowntrays`, `testdb`); the worker's client and the block in `backend/tests/crud/` and `backend/tests/printers/`; dialog and sources in vitest; agent tools in `agent/test/`.

## Decisions the code forced (deviations from the spec's wording)

D1 to D5 are the spec's D1 to D5 ("Decisions made while planning"), with the reasons the code gave. P1 is this plan's own.

- **D1 `seen_at` is a version, not a heartbeat.** Capture runs only when the tray projection changes (`capture.trays_changed`), and a change in any tray of the printer runs it. If every run refreshed `seen_at`, a change in slot 1 would refuse a resolve of slot 2. The upsert moves `seen_at` only when the row's reported fields (`tray_uuid`, `tag_uid`, `material`, `colour_rgba`, `remain_pct`) change. Still true; the resolve activity compares `seen_at` as today's code does (Task 3B).
- **D2 External holders (ams 255) never get a row.** `vt_tray` has no presence state; `_filled` is true whenever a `tray_type` is configured, so an empty external holder would block the printer for good. An RFID external tray is still auto-created. Decided 2026-11-23 (spec "Decisions made while planning").
- **D3 `remain_pct` is stored but is NULL in practice.** `trays._remain` gives a remain only for a tray with RFID keys (#6263 preflight C4), and only untagged trays get rows. The prefill code exists; it fires only if `_remain` changes. Decided 2026-11-23 (spec "Decisions made while planning").
- **D4 The label weight override applies only to a first weight.** The spec's rule makes `label_weight_g` the typed net weight when it differs from the type's. For a half-used spool that sets the label to the grams left and breaks every `remain_pct` reading. The override applies only when the spool has no event other than `created`, `edited`, `moved`, `tag_bound` and `tag_unbound` (no reading, use or load yet). The `weighed` reading is booked either way. Decided 2026-11-23 (spec "Decisions made while planning"). **Still Python (2026-11-26): the rule runs in the resolve activity (Task 3B, G5).**
- **D5 "The owner is told" of an RFID spool** = the list route also returns the spools capture made from tags in the last 24 h (`made_from_tags`), and the frontend toasts each new one once. Decided 2026-11-23 (spec "Decisions made while planning"). **Changed 2026-11-26: the Go list route reads them from `filament_spool_events` (Task 3A).**
- **P1 The rows' Python SQL lives in its own module** (`filament/unknown_tray_rows.py`) because `inventory.py` must clear a row inside `link_on`. **Changed 2026-11-26: there is no Python `unknown_trays.py` now; the list, resolve and dismiss SQL is Go's (`internal/unknowntrays`). Python keeps only `note_on`, `clear_on`, `rows_on`.**

## Changes after the design review (2026-11-23): spec D9, D10, D11

These bind the tasks named, on top of their text below.

- **D9, Task 3 (Python, unchanged):** `FilamentInventory.reconcile_trays` (`filament/inventory.py:448`) also handles an untagged tray of a fresh report that holds no placed spool. When Bambuddy's assignment names a linked, unarchived SB spool that agrees with the tray (`_verdict`), place it there (`place_on`, a `moved` event by `RECONCILE_ACTOR`, `data.reason` `"assigned_to_empty_tray"`) and `clear_on` the tray's row in the same transaction. An assigned spool that another reported tray holds stays where it is, as today. Tests: `test_reconcile_places_an_assigned_spool_into_an_unknown_tray` (row gone, spool placed, one `moved` event) and `test_reconcile_leaves_a_disagreeing_assigned_spool` (row kept).
- **D9, Task 4:** on the Bambuddy path, `prepare_run` leaves out a row whose tray its own lazy `read_inventory` read (Task 4's `inventory`, not shared with `_plan_sliced`'s later read) gives an SB spool. Test: `test_bambuddy_path_does_not_block_an_assigned_tray`. The native path keeps the block (no Bambuddy read there).
- **D10, Task 3A (changed 2026-11-26: a Go route):** `POST /filament/unknown-trays/{id}/dismiss` deletes the row (204; 404 when it is gone). **Task 5:** the resolve dialog has "Dismiss" with the text "Only for a slot that is no longer there; if a spool is still loaded, it comes back on the next report." **Task 7:** no agent tool. Tests: the route (204 then 404) and the dialog button.
- **D11, Task 2:** in `note_unknown`, take `try_tray_lock` before every `clear_on` too (the "empty" and "holder found" branches); when it is busy, leave the row. Test: `test_a_busy_tray_lock_leaves_the_row_on_clear` (a row at (0,2), the lock held by another connection, an empty-tray report: the row is still there).
- **Task 8:** the spec's "Data" section now states the three cases that keep a row; the as-built notes keep them.

## Changes after the Go ruling (2026-11-26): G1 to G17

The owner's policy of epic #7667 (spec `docs/superpowers/specs/2026-11-25-go-service-migration-design.md`) and the issue's ruling comment of 2026-11-26: new routes go in Go. These bind the tasks named, on top of their text below. Each reads `Ruling: <what> — <why> — <cost if wrong>`.

- **G1** Ruling: the three public routes are Go routes in `tools/go/core/internal/unknowntrays`, at their full paths, area `unknown-trays` (`unknown-trays:read` for the list), each wrapped by `gate.Wrap(auth.Route{Class: "api", Template: …}, h)` from #8038 — owner policy of #7667 — cost if wrong: none for users; the plan waits for PR #8053 and cannot start Task 3A before it merges.
- **G2** Ruling: the worker's block read is a Go CRUD route on caller `print`, `GET /filament/printers/{printer_id}/unknown-trays`, in the same area name — the service auth Go already has (`internal/crud`, `auth.Resolver`) covers it; core serves a CRUD route for any caller its credential check names — cost if wrong: the print worker must reach core's CRUD port (`:8082`), not Python's own listener. Task 9 checks that on prod before the switch.
- **G3** Ruling: Python still lists `unknown-trays` in `CRUD_AREAS["print"]` and `PRINT_CRUD_AREAS`, and the worker fails open when the route is missing (`CrudUnavailableError` is a warning and no block, in `prepare_run` and in `lan_print`) — Python's `/areas` answers every name (spec §4 "Switch"), so the worker's start gate cannot see whether Go serves the area; a hard failure would stop every print until the switch — cost if wrong: until `SCADBUDDY_CORE_AREAS` names the area, the worker side does not block (the API-side check and the dialog still do, in process). A real outage of the CRUD port is also treated as no block; the other CRUD reads of the run fail first. **This replaces Review Focus 9's "a failed read in `prepare_run` fails the run" for `CrudUnavailableError`; a 403 (`CrudRefusedError`) still fails it.**
- **G4** Ruling: before the area is switched on, the list route is a 404 (Python has no handler); the frontend takes a 404 as "no unknown trays" (the Needs-you source stays empty, no toast, no callout) and the agent tools show the 404 — a 404 says nothing about the printer — cost if wrong: an unswitched prod hides unknown trays from the owner; the explicit Task 9 step and its prod check close that.
- **G5** Ruling (changed 2026-11-26 after eee8e050's answer, fleet chat epic-7667-go-core #264): the Go resolve route does not write spools. It checks the body and the caller, then runs today's Python write as a Temporal activity, `filament_resolve_unknown_tray`, on the `library` queue, which the API process serves (`workflows/housekeeping.py` `library_worker`; the API is the only process with a database secret, #5295). The activity runs `resolve` in Python: `link_on`, the first weight (D4) and the `weighed` event, in one transaction, with today's lock order. Go starts it through core's Temporal client, `internal/temporalx` (inventory K1 layer 4, not on main), as an update-with-start or a workflow it waits on, and maps the activity's typed failures to the problem codes. Never the queue `inventory`, which Go serves — why: inventory K3/K4 move today's spool code to Go unchanged, so a Go copy of `link_on` now would be a third version to reconcile; #7667's direction is Python only as Temporal activities — cost if wrong: resolve waits for K1 layer 4's `temporalx` to merge, and one resolve costs a Temporal round trip (about 100 ms). G9 and G13's `links` vectors no longer apply to Go; Python's own tests hold the lock order.
- **G6** Ruling: resolve answers `{id, number, code, location_id}`, not the full `Spool` — Go has no `Spool` view (`effective`, low stock, grams) and building one is a port of its own; `id` keeps the agent's `kinds: 'spool'` mapping working — cost if wrong: a caller that wants more reads `GET /filament/spools/{id}` (Python) after the resolve. **This changes Task 3's old answer type, the dialog's `onResolved` argument and the agent tool's answer.**
- **G7** Ruling: the candidates are read straight from SQL (effective values by `coalesce`, the stored `grams_left` column), and CIEDE2000 is ported to Go with Python's results as vectors — `colour_distance` has no SQL form — cost if wrong: a distance that differs in the last digits changes a tie in the order; the vectors use a 1e-9 tolerance and CI catches a larger drift.
- **G8** Ruling: resolve and dismiss publish nothing on the event log — the spool events go to `filament_spool_events`, and no trigger on these tables calls `scadbuddy_publish_event`; the frontend polls — cost if wrong: if a later change publishes (a Needs-you push), it must take the event-log lock first (`bus.HoldLog`, which is on the inventory K1 branch, not on main; eee8e050), before the tray lock and any row lock. `TestResolvePublishesNoEvent` pins today's rule.
- **G9** Ruling (changed 2026-11-26, see G5): the lock order stays in Python's resolve activity, today's order (the tray's advisory lock, then the unknown row `FOR UPDATE`, then the tag, then the spools by id, then the place `FOR SHARE`). Go takes no lock for resolve — cost if wrong: none in Go; Python's tests hold the order.
- **G10** Ruling: Go writes these routes' numbers with `encoding/json`, not `jsonv.Float` — Python serves no copy of them (18fe662e's `1e+06` against `1000000.0` only matters where both serve one route) — cost if wrong: none unless Python later serves the same route; then use `jsonv.Float`.
- **G11** Ruling: core has no public-route mount on main, so Task 3A adds `crud.Router.Public` (a `*http.ServeMux` tried first) unless a merged PR has one by then — the first public Go route needs one place to mount — cost if wrong: a second PR adds a different mount; check `git grep gate.Wrap origin/main` first.
- **G12** Ruling: types for Go-served routes are hand-written: `frontend/src/api/unknownTrays.ts` and `agent/src/api/unknownTrays.ts` (a `paths` fragment for a second typed client) — `pnpm gen:api` exports Python's OpenAPI (`scadbuddy.tools.export_openapi`) and Python has no route to export — cost if wrong: types drift from Go; the golden JSON of Task 3A and the frontend mock's own test hold the key sets, and a route that moves to Python later gets generated types back.
- **G13** Ruling: the Python test `test_unknown_tray_vectors.py` writes `backend/tests/fixtures/unknown-tray-vectors.json` (`UPDATE_UNKNOWN_TRAY_VECTORS=1`, as `auth-vectors.json`) and the Go tests read it — the only way two languages stay equal without a shared runtime — cost if wrong: a vector file that was hand-edited passes both sides; the Python test must also run without the variable and fail on a stale file.
- **G14** Ruling: a Go area is added by a `case "unknown-trays":` in `newRouter` (like `fleet`), not by `areaPackages` — the default case mounts every area on caller `agent` — cost if wrong: the CRUD route lands on the wrong caller and the worker's credential gets 403 (`TestTheBlockReadRefusesAnotherCallersCredential` and a `newRouter` test catch it).
- **G15** Ruling: no local test runs beyond gofmt, vet, build, ruff, mypy, eslint and typecheck (the owner's note) — CI has the Postgres — cost if wrong: a failure shows one CI round later.
- **G16** Ruling: switching the area on is Task 9, out of this repo (eh-homelab/clusters, `scadbuddy.yaml`), and it merges only after the image that has the area is deployed — core stops its start on an area name it does not know (`newRouter`), and e26a6fdf found an old core 404s a new route in an area it owns; a new area avoids the second, not the first — cost if wrong: the API pod crash-loops on the old image.
- **G17** Ruling: the spool lookup is its own package, `internal/spoolref` (`Find` by id or code), and resolve's write needs a tray row, so #7167's "identify this spool" (RFID scan, QR code or manual pick) reuses `Find` and adds a tag lookup there — the owner's ruling that a spool going into a dry box is always identified by the person — cost if wrong: #7167 reshapes `Ref`; the type is small and has one caller now. #7167 itself is not planned here.

## Review Focus

1. **A stale row blocks a printer after the tray was settled another way.** This covers the old tray link (`PUT /printers/{id}/trays/{ams}/{tray}/spool`), the print dialog's tray question (`api/printing.py` `post_tray_spool`, which calls `link`), and a capture that read "no holder" while a resolve was committing. Expected: no row once the tray has a holder. `link_on` deletes the row in its transaction. Capture writes a row only under `pg_try_advisory_xact_lock` on the tray's key, and it checks the holder again after it takes the lock. Tests in Task 2 and Task 3 (Python `link_on`).
2. **An external holder with a configured type and no spool** (D2): no row, no block. Test in Task 2.
3. **Two people resolve one tray at once, or the tray changes during a resolve:** one wins; the other gets 409 `tray_changed`, and nothing of it is written. Tests in Task 3B (`test_two_resolves_of_one_tray_one_wins`, `test_a_stale_seen_at_is_tray_changed`).
4. **A tray flips unknown → known → unknown between reports:** the second unknown is a new row (new `id`, new `first_seen_at`); a resolve sent with the first id gets 409 `tray_changed`. Tests in Task 2 and Task 3B.
5. **An RFID spool made by capture, then Bambuddy's pull of the BB spool it auto-added:** the pull links that BB spool to the SB spool (`bambuddy_pull._holder` finds the unlinked spool that holds the tag); it never makes a second one. Test in Task 2.
6. **Resolving with an archived spool:** 409 `spool_archived`. **With a spool in another tray:** that spool moves; the other tray's place no longer holds it. **With a code that names a place (`L-3`):** 404 `code_unknown`. Tests in Task 3B.
7. **No location kind flagged `ams`:** an untagged tray still gets its row (with no place); an RFID tray falls back to a row; a resolve answers 409 `no_ams_kind`. Tests in Task 2 and Task 3B.
8. **A printer deleted or merged:** deleting it removes its rows (cascade); merging it drops the merged printer's rows (the kept printer's next capture writes its own). Tests in Task 1.
9. **The CRUD read fails or the route is missing (changed, G3):** at `lan_print` the start goes on (logged). In `prepare_run` a missing route or an outage (`CrudUnavailableError`) is logged and counts as no block; a 403 fails the run. Tests in Task 4.
10. **The area is not switched on (G4):** the list route is a 404; the frontend shows no Needs-you source and no error; the worker does not block. Tests in Task 3A (`TestAnUnswitchedAreaForwardsToNext`), Task 4 and Task 5. Task 9 switches it on.
11. **The resolve activity is not served, or it times out (G5):** the route answers 503 `resolve_pending`, and nothing is half-written, because the activity runs in one transaction. Tests in Task 3B (`TestResolveTimeoutIs503`, `test_the_library_worker_serves_the_activity`).
12. **A person-auth refusal:** no session → 401, a read-only access token on resolve or dismiss → 403, in `enforce`; `observe` serves and counts. Test in Task 3A (`TestPublicRoutesUseTheGate`).

---

### Task 1: The table, its rows and the wire types

**Files:**
- Create: `backend/scadbuddy/migrations/20261123T1200Z_filament_unknown_trays.sql` (any UTC stamp that sorts after `20261123T0211Z_storefront_renders_kind.sql`; take `$(date -u +%Y%m%dT%H%MZ)` when you create it)
- Create: `backend/scadbuddy/filament/unknown_tray_rows.py`
- Modify: `backend/scadbuddy/printers/store.py` (`PRINTER_REFERENCES`, after the `filament_override_slots` entry at :395)
- Test: `backend/tests/filament/test_unknown_tray_rows.py`

**Interfaces:**
- Produces (all in `filament/unknown_tray_rows.py`):
  - `class UnknownTrayRow(BaseModel)`: `id: UUID, printer_id: UUID, ams_id: int, tray_id: int, tray_uuid: str | None, tag_uid: str | None, material: str, colour_rgba: str | None, remain_pct: int | None, first_seen_at: datetime, seen_at: datetime`.
  - `note_on(conn: Conn, printer_id: UUID, seen: SeenTray, seen_at: datetime) -> UnknownTrayRow`: upsert. `first_seen_at` and `seen_at` are `seen_at` on insert. On conflict it sets the reported fields and `seen_at` only `WHERE (tray_uuid, tag_uid, material, colour_rgba, remain_pct) IS DISTINCT FROM EXCLUDED's` (D1). `colour_rgba = seen.rgba`, `remain_pct = seen.remain`.
  - `clear_on(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int) -> bool` (a row was deleted).
  - `rows_on(conn: Conn, printer_id: UUID | None = None) -> list[UnknownTrayRow]`, ordered by printer, `ams_id`, `tray_id`.
  - `class UnknownSpoolRef(BaseModel)`: `id: UUID, slot: str`.
  - `class UnknownSpoolBlock(BaseModel)`: `count: int, trays: list[UnknownSpoolRef]`.
  - `class UnknownSpoolReads(Protocol)`: `async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]: ...`
  - `class PoolUnknownTrays`: `__init__(self, pool: ConnectionPool[Conn])`; `async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]` (to_thread over `rows_on`; `slot = tray_label(ams_id, tray_id)`).
  - `UNKNOWN_SPOOL_LOADED: Final = "unknown_spool_loaded"`; `TRAY_CHANGED: Final = "tray_changed"`.
  - `blocked_detail(printer_name: str, refs: Sequence[UnknownSpoolRef]) -> str`: the Global Constraints text.

- [ ] **Step 1: Write the failing tests**

```python
def test_note_inserts_then_keeps_seen_at_while_nothing_changes(pg_pool): ...
    # note_on(T0) then note_on(T0+5s) with the same SeenTray: one row, first_seen_at == seen_at == T0
def test_a_changed_colour_moves_seen_at_only(pg_pool): ...
    # note_on(T0, red) then note_on(T1, blue): same id, first_seen_at T0, seen_at T1, colour_rgba "#0000FFFF"
def test_clear_deletes_one_tray_only(pg_pool): ...
    # rows at (0,1) and (0,2); clear_on(0,1) is True, again False; rows_on == [(0,2)]
def test_deleting_the_printer_deletes_its_rows(pg_pool): ...
    # DELETE FROM printers → rows_on(printer) == []
def test_merging_a_printer_drops_its_rows(pg_pool): ...
    # PrinterStore.merge(merged, into) (as tests/printers/test_store.py does): merged's rows gone, into's kept
def test_blocked_detail_names_every_slot() -> None:
    assert blocked_detail("H2C", [UnknownSpoolRef(id=A, slot="AMS-A slot 2")]) == (
        "H2C has an unknown spool in AMS-A slot 2: link it on the Filament page first.")
    # three refs → "in AMS-A slot 1, AMS-A slot 2 and HT-A: link it ..."
async def test_pool_blocking_lists_slots(pg_pool): ...
    # one row at (128, 0) → [UnknownSpoolRef(id=row.id, slot="HT-A")]
```

The printer fixture is `_printer` from `tests/filament/test_capture.py` (copy it; it is module-private).

- [ ] **Step 2: Type-check the tests** (no local test run, G15; CI's `backend-pytest` lane runs them)

Run: `cd backend && uv run --frozen ruff check tests/filament && uv run --frozen mypy`
Expected: the only errors name `scadbuddy.filament.unknown_tray_rows`.

- [ ] **Step 3: Write the migration, the module and the merge hook**

The migration: `CREATE TABLE filament_unknown_trays` with `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`, `printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE`, `ams_id integer NOT NULL CHECK (ams_id BETWEEN 0 AND 255)`, `tray_id integer NOT NULL CHECK (tray_id BETWEEN 0 AND 15)`, `tray_uuid text`, `tag_uid text`, `material text NOT NULL`, `colour_rgba text`, `remain_pct integer CHECK (remain_pct BETWEEN 0 AND 100)`, `first_seen_at timestamptz NOT NULL`, `seen_at timestamptz NOT NULL`, `UNIQUE (printer_id, ams_id, tray_id)`. In `printers/store.py`, add `PrinterReference("filament_unknown_trays", "printer_id", repoint=_drop_unknown_trays)`; `_drop_unknown_trays` deletes the merged printer's rows, as `_drop_override_slots` does. Without it, `tests/printers/test_store.py`'s foreign-key coverage test fails.

- [ ] **Step 4: Lint and type-check** (CI runs the tests)

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no errors. After the push, `backend-pytest` must pass `tests/filament/test_unknown_tray_rows.py` and `tests/printers/test_store.py`.

- [ ] **Step 5: Commit** — `feat(filament): a table of the unknown trays a printer reports (#7143)`

---

### Task 2: Capture records unknown trays and makes RFID spools

**Files:**
- Modify: `backend/scadbuddy/filament/capture.py` (extract `_book_load` from `reconcile` :301-337; add `note_unknown`; call it in `reconcile_all` :361)
- Modify: `backend/scadbuddy/printers/reports.py` (`_capture` :129-187: a step `"unknown_trays"` after `"trays"`)
- Modify: `backend/scadbuddy/filament/inventory.py` (add `try_tray_lock` beside `_tray_lock` :838)
- Modify: `backend/scadbuddy/filament/types.py` (`SpoolTypeStore.match_tray`)
- Test: `backend/tests/filament/test_capture_unknown.py`

**Interfaces:**
- Consumes: `note_on`, `clear_on`, `rows_on` (Task 1).
- Produces:
  - `capture.note_unknown(conn: Conn, printer_id: str, report: Mapping[str, Any], seen_at: datetime, *, actor: str) -> UnknownTrayResult`.
  - `@dataclass(frozen=True) class UnknownTrayResult: made: int = 0; noted: int = 0; cleared: int = 0` (with `__add__`, as `CaptureResult`).
  - `capture.RFID_MADE: Final = "rfid_capture"`: the `data.by` of a `created` event capture books. Task 3's `made_from_tags` reads it.
  - `inventory.try_tray_lock(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int) -> bool`: `pg_try_advisory_xact_lock(hashtext('filament_tray:<printer>:<ams>:<tray>'))`, the same key as `_tray_lock`.
  - `SpoolTypeStore.match_tray(conn: Conn, material: str, rgba: str | None, tray_info_idx: str | None) -> m.SpoolType | None` (static; `None` for no `rgba` or a material with no key).
  - `capture._book_load(conn, printer: UUID, name: str, seen: SeenTray, holder: UUID, found_by: FoundBy, *, actor: str, at: datetime, printing: bool, job: str | None) -> CaptureResult`: the existing `loaded` + `place_on` + `ams_reading` booking, which `reconcile` now calls.

`note_unknown` walks the keys `reconcile` walks (`_walk`), plus each existing row's key whose unit the report shows. A row of a unit the report does not show is left alone, as `_walk` leaves its spools. Per key:

```
key in loading_trays(status)              -> leave the row
seen is None (empty)                       -> clear_on
holder_on(conn, printer, seen) finds one   -> clear_on
seen has tray_uuid or tag_uid              -> with conn.transaction(): _spool_from_tag(...)   # savepoint per tray
                                              on any Exception: log FILAMENT_CAPTURE_FAILED (what="rfid_spool"),
                                              then fall through to the untagged branch (the owner sees a row)
ams_id == EXTERNAL                          -> no row (D2)
untagged                                   -> if not try_tray_lock: leave the row (a link or resolve is settling it)
                                              holder_on again; found -> clear_on
                                              place: ensure_ams_on in a savepoint (NoAmsKindError -> no place)
                                              note_on
```

`_spool_from_tag(conn, printer: UUID, name: str, seen: SeenTray, *, actor: str, at: datetime, printing: bool, job: str | None) -> UUID`, in this order:
1. `try_tray_lock`; if it is busy, raise `_TrayBusy` (caught: leave the tray to the next report).
2. `holder_on` again; a holder now means `clear_on` and return it.
3. The type is `SpoolTypeStore.match_tray(conn, seen.material, seen.rgba, seen.tray_info_idx)`. Insert `filament_spools` with `type_id`, else `material_id` (`resolve_or_add(conn, "materials", …, sort_order=1000)`) and `rgba`, as `bambuddy_pull._create` inserts. Then `ledger.record(conn, spool, "created", printer=printer, ams_id=…, tray_id=…, actor=actor, at=at, data={"by": RFID_MADE})` and `FilamentSpoolStore.read(conn, spool, check=True)`.
4. `FilamentSpoolStore.tag_on` then `bind_on(conn, spool, uid, tray, kind, None, actor)`; `kind` is `"bambu_mifare"` when `tray_uuid` is set, else `"other"`, as `FilamentInventory.link` picks it.
5. `_book_load(...)` with `found_by="tag"` (`ensure_ams_on`, `loaded`, `place_on`, `ams_reading` when `seen.remain` is set), then `clear_on`. Log at info: "made a spool from an unknown RFID tag", with `printer_id`, `ams_id`, `tray_id` and `number`.

`_capture` runs `"unknown_trays"` when `trays` is true, after `"trays"`. `reconcile_all` runs `note_unknown` after `reconcile` in a savepoint of its own (its own `try`, logged as `what="backstop_unknown"`), so the backstop heals a row that a missed or busy capture left.

- [ ] **Step 1: Write the failing tests** (helpers `_printer`, `_tray`, `_report`, `_spool`, `_link` as in `tests/filament/test_capture.py`; `_advance(pool, pid, report, at)` runs `PrinterReportStore.advance_in` in a transaction)

```python
def test_an_untagged_tray_with_no_holder_gets_a_row(pg_pool): ...
    # _advance(_report(_tray(2))): rows_on(pid) == one row (0, 2), material "PLA", colour_rgba "#FF0000FF", remain_pct None
def test_a_linked_tray_gets_no_row(pg_pool): ...
    # _link spool at (0,1), then _advance(_report(_tray(1))): rows_on == []
def test_emptying_the_tray_clears_its_row(pg_pool): ...
    # row at (0,2); next report _tray(2, tray_type="", state=0): rows_on == []
def test_a_loading_tray_keeps_its_row(pg_pool): ...
    # row at (0,2); next report _tray(2, state=5): the row is still there with its id
def test_a_unit_the_report_omits_keeps_its_rows(pg_pool): ...
    # row at (0,2); report with only unit "1": the row is kept
def test_a_tray_that_flips_known_and_back_gets_a_new_row(pg_pool): ...
    # row r1 at T0; link it (T1 report: no row); unlink, then a T2 report with a new colour: a row with id != r1.id, first_seen_at == T2
def test_an_external_holder_gets_no_row(pg_pool): ...
    # report {"vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "FF0000FF"}}: rows_on == []
def test_an_rfid_tray_gets_a_spool_of_the_matching_type(pg_pool): ...
    # a type (PLA, #FF0000FF, a preset slicer_filament "GFA00"); _advance(_report(_tray(1, U1, tray_info_idx="GFA00", remain=80)))
    # one new spool: type_id == that type, open binding of U1, location == the (0,1) tray place,
    # events created(data.by "rfid_capture"), tag_bound, loaded, moved, ams_reading(remain_pct 80); rows_on == []
def test_an_rfid_tray_with_no_type_gets_a_typeless_spool(pg_pool): ...
    # no type: the spool has type_id None, material_id of "PLA", rgba "#FF0000FF"
def test_an_archived_spools_tag_goes_to_the_new_spool(pg_pool): ...
    # U1 bound to an archived spool: a new spool holds U1; the old binding closed reason "rebound"
def test_the_pull_links_the_bb_spool_to_the_rfid_spool(pg_pool): ...
    # after the auto-create, bambuddy_pull._holder(conn, {"tag_uid": None, "tray_uuid": U1}, 7) == (new_spool, False)
def test_no_ams_kind_still_writes_the_rows(pg_pool): ...
    # archive every kind flagged ams; untagged (0,2) → a row (no place); RFID (0,1) → a row too, no spool made
def test_a_busy_tray_lock_writes_no_row(pg_pool): ...
    # another connection holds pg_advisory_lock on the tray's key; _advance → rows_on == []; the report is written
def test_a_failing_unknown_step_still_writes_the_report(pg_pool, monkeypatch, caplog): ...
    # monkeypatch capture.note_unknown to raise RuntimeError("secret"); reports.get(pid) is not None;
    # one FILAMENT_CAPTURE_FAILED record with what == "unknown_trays"; "secret" not in caplog.text
def test_the_backstop_writes_a_row_the_watch_missed(pg_pool): ...
    # _write_report (as test_capture.py) then capture.reconcile_all(pool, [pid], now=T0+1s): one row
def test_match_tray_prefers_the_preset_of_the_trays_idx(pg_pool): ...
    # two PLA #FF0000FF types, the newer with preset "GFA00": match_tray(conn, "PLA", "#FF0000FF", "GFA00") is the newer;
    # with idx None it is the older; rgba "#00FF00FF" → None
```

- [ ] **Step 2: Type-check the tests** (no local test run, G15)

Run: `cd backend && uv run --frozen ruff check tests/filament && uv run --frozen mypy`
Expected: the only errors name `note_unknown`, `try_tray_lock`, `match_tray`.

- [ ] **Step 3: Implement `try_tray_lock`, `match_tray`, `_book_load`, `note_unknown` and the `_capture` step as the Interfaces block says**

- [ ] **Step 4: Find the suites that write reports**

Run: `grep -rln "lan-report-h2c" backend/tests`
Expected: a test that puts a report with an unbound tag (`lan-report-h2c.json`'s tray `0/1` holds tag keys) now gets a spool from capture. If one counts spools or events, make its count say so, and say in the commit body which tests changed and why. CI's `backend-pytest` lane runs `tests/filament`, `tests/printers` and `tests/crud`; it must pass.

- [ ] **Step 5: Lint and type-check**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no errors.

- [ ] **Step 6: Commit** — `feat(filament): capture records unknown trays and makes RFID spools (#7143)`

---

### Task 3: Python's `link_on` clears the row, and the shared vectors

Python keeps the writers that capture and the old tray link use. This task makes them clear a row, and writes the vectors that hold the Go port (Tasks 3A and 3B) equal to Python (G13).

**Files:**
- Modify: `backend/scadbuddy/filament/inventory.py` (`link` :654-723 → `link_on` + `link`)
- Modify: `backend/scadbuddy/filament/inventory.py` (`reconcile_trays` :448, D9)
- Test: `backend/tests/filament/test_link_clears_unknown_row.py`, `backend/tests/filament/test_unknown_tray_vectors.py`
- Create (written by the test with `UPDATE_UNKNOWN_TRAY_VECTORS=1`, then committed): `backend/tests/fixtures/unknown-tray-vectors.json`

**Interfaces:**
- Consumes: Task 1's `clear_on`.
- Produces:
  - `FilamentInventory.link_on(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int, spool_id: UUID, seen: SeenTray, place_id: UUID, *, actor: str) -> None` (static). It holds today's transaction body, from `_tray_lock` to `bind_on`, then `clear_on(conn, printer_id, ams_id, tray_id)`. `link` keeps its signature and calls it.
  - The vectors file, with two lists: `colour_distance` (`{left, right, distance}`: at least 20 pairs, among them a `None` side, equal colours, near reds, and black/white), `labels` (`{ams_id, tray_id, tray_label, place_label_for_printer_H2C}`: AMS 0 to 3, HT 128 and 129, the external holder 255). It has no `links` list (G5: the resolve write stays in Python).

- [ ] **Step 1: Write the failing tests**

```python
def test_the_old_tray_link_clears_the_row(pg_pool): ...
    # row at (0,1) (note_on); FilamentInventory(pool).link(...) for that tray → rows_on == []
def test_reconcile_places_an_assigned_spool_into_an_unknown_tray(pg_pool): ...   # D9: row gone, spool placed, one `moved` event
def test_reconcile_leaves_a_disagreeing_assigned_spool(pg_pool): ...   # D9: row kept
def test_link_on_clears_only_its_tray(pg_pool): ...
    # rows at (0,1) and (0,2); link (0,1) → rows_on == [(0,2)]
def test_the_vectors_match_what_python_does(pg_pool): ...
    # reads the file; for each pair/label/scenario recomputes with colour_distance, tray_label, place_label, link
    # and asserts equal; with UPDATE_UNKNOWN_TRAY_VECTORS=1 it writes the file instead
```

- [ ] **Step 2: Type-check the tests** (no local test run; CI's `backend-pytest` lane runs them)

Run: `cd backend && uv run --frozen ruff check tests/filament && uv run --frozen mypy`
Expected: only the errors that name the missing `link_on`.

- [ ] **Step 3: Implement `link_on`; write the vectors once with `UPDATE_UNKNOWN_TRAY_VECTORS=1`** — this one run of the vectors test is allowed locally only if a Postgres is at hand; otherwise a CI job with the variable set produces the file as an artifact. Do not hand-write the file.

- [ ] **Step 4: Lint and type-check**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no errors. After the push, CI's `backend-pytest` must pass `tests/filament`, `tests/api/test_filament_sync_nudge.py` and every tray-link test.

- [ ] **Step 5: Commit** — `feat(filament): a tray link clears the unknown row; vectors for the Go port (#7143)`

---

### Task 3A: Go — the mount, the list, the block read and dismiss

**Files:**
- Create: `tools/go/core/internal/unknowntrays/unknowntrays.go` (`Areas`, `Caller`, the routes), `list.go`, `rank.go`, `colour.go` (CIEDE2000), `labels.go` (`TrayLabel`, `PlaceLabel`), `block.go` (the CRUD read)
- Create: `tools/go/core/internal/spoolref/spoolref.go` (the spool lookup, G17)
- Modify: `tools/go/core/internal/crud/crud.go` (`Router.Public`), `tools/go/core/main.go` (`newRouter`: the `unknown-trays` case), `tools/go/core/README.md` ("Public routes" and the area table)
- Test: `tools/go/core/internal/unknowntrays/*_test.go`, `tools/go/core/internal/spoolref/spoolref_test.go`, `tools/go/core/unknown_trays_test.go` (through `newRouter`)
- Create: `backend/tests/fixtures/unknown-trays-block.json` (the block's JSON; Go and Python tests read it)

**Interfaces:**
- Consumes: `auth.NewGate`, `auth.Route`, `auth.CallerOf` (PR #8053, issue #8038; this task starts only after it merges, G11), `crud.Router`, `testdb.New`. Never copy the auth check.
- Produces:
  - `crud.Router.Public *http.ServeMux`: tried first in `ServeHTTP`; a path it does not match goes on to the CRUD routes, then to `Next`. If another merged PR already added a public mount by then, use it and drop this edit (check `git grep -n "gate.Wrap" origin/main -- tools/go/core` first).
  - `unknowntrays.Areas(pool *pgxpool.Pool, gate *auth.Gate) …`: the names `unknown-trays` (every route) and `unknown-trays:read` (the GETs). `newRouter` builds the gate once from `auth.ParseMode(env("SCADBUDDY_AUTH_MODE"))` and `tel.Meter()` and passes it in; it has its own `case "unknown-trays"` (and `:read`), because `Areas`' default case mounts everything on caller `agent`. The public routes go on `Router.Public` and the CRUD route on `router.Add("print", …)` (G1, G2).
  - Public routes, each `gate.Wrap(auth.Route{Class: "api", Template: "/api/v1/filament/unknown-trays…"}, h)`: `GET /api/v1/filament/unknown-trays` (answers `{trays: […], made_from_tags: […]}`, the JSON shapes of the earlier Python models, kept: `UnknownSpoolTray`, `UnknownSpoolCandidate`, `TagMadeSpool`), `POST …/{id}/dismiss` (204; 404 problem when the row is gone; D10). The resolve route is Task 3B.
  - CRUD route `GET /filament/printers/{printer_id}/unknown-trays` on caller `print` (`crud.Route{Method: "GET", Path: …}`), answers `{count: int, trays: [{id, slot}]}`; `slot` is `TrayLabel(ams_id, tray_id)`.
  - `TrayLabel(amsID, trayID int) string` and `PlaceLabel(printerName string, amsID, trayID int) string`: `bambuddy/trays.py` `tray_label` and `place_label`.
  - `ColourDistance(left, right *string) *float64`: `bambuddy/filaments.py` `colour_distance` (CIEDE2000, alpha dropped; nil when a side has no colour).
  - `Rank(row Row, spools []SpoolRow, keys map[string]map[string]bool, limit int) []Candidate`: the earlier `rank` key (unplaced first, the tray's material, nearest colour with no colour last, number).
  - `spoolref.Ref{ID string; Code string}`, `spoolref.Find(ctx context.Context, q pgx.Tx or Querier, ref Ref) (SpoolRow, error)` with `ErrNotFound` (no spool), `ErrCodeUnknown` (a code that does not parse as `codes/parse.py` `parse_code`, a prefix other than `S`, or no spool), `ErrArchived`. A caller adds a tag lookup later without changing `Find`'s users (#7167; G17).

`GET` list reads in SQL, in one read-only transaction: the rows, printer names, each row's place (`filament_locations` by printer, `ams_id`, `tray_id`, unarchived), the unarchived spools with their effective material, rgba and label weight (`coalesce(s.…, t.…)` as `spools.py` `_SPOOL_COLUMNS` does) and the stored `grams_left` column, and `filament_material_names`. `made_from_tags` is the `created` events of `filament_spool_events` with `data->>'by' = 'rfid_capture'` in the last 24 h, newest first, at most 20. Numbers go out through `encoding/json`; Python serves none of these routes, so no float parity is needed (G10).

- [ ] **Step 1: Write the failing tests** (Go, `testdb.New`; rows and spools inserted with SQL; the printer fixture inserts a `printers` row)

```go
func TestColourDistanceMatchesPython(t *testing.T)   // every pair of backend/tests/fixtures/unknown-tray-vectors.json, tolerance 1e-9; nil for a nil side
func TestLabelsMatchPython(t *testing.T)             // the labels list of the same file
func TestRankPutsUnplacedThenMaterialThenColour(t *testing.T)
	// row PLA #FF0000FF; A placed PLA red, B unplaced PETG red, C unplaced PLA #EE0000FF, D unplaced PLA blue → numbers [C, D, B, A]
func TestRankKeepsTenAndSkipsArchived(t *testing.T)
func TestListNamesPrinterSlotAndPlace(t *testing.T)  // printer "H2C", slot "AMS-A slot 2", place_code starts "L-"
func TestListHasTheSpoolsMadeFromTags(t *testing.T)  // an event created data.by "rfid_capture" 1 h ago: listed; 25 h ago: not
func TestDismissDeletesThenNotFound(t *testing.T)    // 204, then a 404 problem
func TestTheBlockReadAnswersTheGoldenJSON(t *testing.T)
	// two rows → the body equals backend/tests/fixtures/unknown-trays-block.json byte for byte
func TestTheBlockReadRefusesAnotherCallersCredential(t *testing.T)   // a render credential → 403, as the crud tests check
func TestPublicRoutesUseTheGate(t *testing.T)
	// SCADBUDDY_AUTH_MODE=enforce, no cookie → 401 signed-out; a read token POSTing dismiss → 403 out_of_scope (auth.WouldRefuse's answers)
func TestAnUnswitchedAreaForwardsToNext(t *testing.T)  // newRouter without the area: the path reaches Next
func TestAnUnknownAreaNameStillStopsTheStart(t *testing.T)  // existing behaviour kept
func TestSpoolrefFind(t *testing.T)   // by id; "s-12", "S12", " S-12 " find spool 12; "L-3", "x", "S-999999" → ErrCodeUnknown; an archived spool → ErrArchived
```

- [ ] **Step 2: Build and vet** (no local test run; CI's `go` job runs `test -race`)

Run: `cd tools/go/core && gofmt -l . && go vet ./... && go build ./...`
Expected: the only errors name the missing package.

- [ ] **Step 3: Implement the package, the mount, the `newRouter` case and the README rows**

The README gets the two area names, the four routes and a line that says the three public routes use `internal/auth`.

- [ ] **Step 4: Check that CI selects the Go lane for the fixture paths**

Run: `grep -n "fixtures" .github/scripts/select-go.sh .github/scripts/select-lanes.sh`
Expected: `backend/tests/fixtures/` is a Go-lane input, as `auth-vectors.json` is (`internal/auth/vectors_test.go` reads it). If it is not, add the path to the lane's pattern and to `select-go.test.sh` (root CLAUDE.md "CI and caching rules").

- [ ] **Step 5: Format, vet, build** — `cd tools/go/core && gofmt -l . && go vet ./... && go build ./...`

- [ ] **Step 6: Commit** — `feat(core): list and dismiss unknown trays in Go, and the print worker's block read (#7143)`

---

### Task 3B: Go route, Python activity — resolve

Blocked by inventory K1 layer 4 (`tools/go/core/internal/temporalx`, go.temporal.io/sdk v1.52.0). Build Steps 1-3 (Python) before it lands; Steps 4-6 (Go) after.

**Files:**
- Create: `backend/scadbuddy/filament/unknown_resolve.py` (the activity and its input/output models), `tools/go/core/internal/unknowntrays/resolve.go` (the route)
- Modify: the `library` worker's activity list (where `library_worker`'s `activities` are built for the API), `tools/go/core/internal/unknowntrays/unknowntrays.go`, `tools/go/core/README.md`
- Test: `backend/tests/filament/test_unknown_resolve.py`, `tools/go/core/internal/unknowntrays/resolve_test.go`

**Interfaces:**
- Consumes: Task 3's `link_on` and `UnknownTrayStore`, `codes.resolve` (spool by code), Task 3A's package, `auth.CallerOf`, `temporalx`.
- Produces:
  - Python activity `filament_resolve_unknown_tray(ResolveInput) -> ResolveOutput` on queue `library`. `ResolveInput{tray_id: UUID, spool_id: UUID | None, code: str | None, net_weight_g: float, seen_at: datetime, actor: str}`; `ResolveOutput{id, number, code, location_id}` (G6). Typed failures (non-retryable `ApplicationError` with `type`): `tray_changed`, `code_unknown`, `spool_archived`, `no_ams_kind`, `spool_missing`, `tag_clash` (message kept).
  - Public route `POST /api/v1/filament/unknown-trays/{id}/resolve`, `gate.Wrap(auth.Route{Class: "api", Template: …}, h)`. Body `{spool_id?: uuid, code?: string, net_weight_g: number (> 0, <= 100000), seen_at: RFC 3339 with zone}`; exactly one of `spool_id` and `code`, else 422 (checked in Go before the start). It runs the activity with a 30 s timeout, no retry, and maps each failure `type` to its problem: `tray_changed` 409, `code_unknown` 404, `spool_archived` 409, `no_ams_kind` 409, `spool_missing` 404, `tag_clash` 409 with the message. A timeout is 503 `resolve_pending`.

The activity, in one transaction, in this order (today's code, D4 and D9 unchanged): the tray's advisory lock; the row `FOR UPDATE` and its `seen_at` check; the spool by id or code; `link_on` (it deletes the row); the first weight (D4) through `update_on`; one `weighed` event through `ledger.record` (`source='person'`, `ref_kind='unknown_tray'`). It publishes no event (G8).

- [ ] **Step 1: Write the failing Python tests** (`test_unknown_resolve.py`, calling the activity function directly with the `pg_pool` fixture):

```python
async def test_resolve_by_code_links_books_and_clears(): ...      # place is the tray's; weighed grams_after 980 ref_kind "unknown_tray"; row gone
async def test_a_first_weight_that_differs_sets_the_label_weight(): ...  # label 1000, only `created`, net 1012 -> 1012 and an edited event
async def test_a_used_spool_keeps_its_label_weight(): ...
async def test_a_stale_seen_at_is_tray_changed(): ...              # ApplicationError type "tray_changed"; no event
async def test_two_resolves_of_one_tray_one_wins(): ...            # gather two: one output, one tray_changed; one weighed event
async def test_an_archived_spool_is_refused(): ...                 # "spool_archived"; the row stays
async def test_a_place_code_is_code_unknown(): ...                 # "L-1"
async def test_no_ams_kind_is_refused(): ...
async def test_the_library_worker_serves_the_activity(): ...       # the API's activity list names it
```

- [ ] **Step 2: Implement the activity** (ruff, mypy; CI runs the tests).
- [ ] **Step 3: Commit** — `feat(filament): resolve an unknown tray as a library activity (#7143)`
- [ ] **Step 4: Write the failing Go tests** (`resolve_test.go`, with a fake starter in place of `temporalx`):

```go
func TestResolveBodyNeedsExactlyOneOfSpoolAndCode(t *testing.T)  // both, neither, net 0 -> 422; nothing started
func TestResolveStartsTheActivityOnLibrary(t *testing.T)         // queue "library", input carries auth.CallerOf's actor
func TestResolveMapsEachFailureType(t *testing.T)               // table: type -> status and code
func TestResolveTimeoutIs503(t *testing.T)
func TestResolveAnswersTheSpoolsIdentity(t *testing.T)          // {id, number, code, location_id}
```

- [ ] **Step 5: Implement the route** (gofmt, vet, build; CI runs the tests).
- [ ] **Step 6: Commit** — `feat(core): resolve an unknown tray through a library activity (#7143)`

---

### Task 4: Block ScadBuddy's prints to a printer with an unknown tray

The block stays Python (`prepare_run`, `lan_print`, `check_print`); only its worker-side read goes to the Go CRUD route of Task 3A (G2, G3).

**Files:**
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`RunRefusalError` :887; `PrintCheck` :988; `check_print` :1465 and its `RunRefusalError` catches at :1543 and :1625; `_native_check` :1865 and its catch at :1905; `prepare_run` :3000, the check before `if source.is_sliced:` at :3149; the callers at :2507 and :2575)
- Modify: `backend/scadbuddy/workflows/print_activities.py` (`PrintDeps` :146, the `prepare_run` call :385)
- Modify: `backend/scadbuddy/printers/print_activities.py` (`LanDeps` :208; `lan_print` :362, before `self._command`)
- Modify: `backend/scadbuddy/api/crud_areas.py` (`CRUD_AREAS["print"]` and its comment: the area is served by `scadbuddy-core` when `SCADBUDDY_CORE_AREAS` names it), `backend/scadbuddy/worker.py` (`PRINT_CRUD_AREAS` :1498, `PrintDeps` :1405, `LanDeps` :1425), `backend/scadbuddy/crud/print.py` (`RemoteUnknownTrays`)
- Modify: `backend/scadbuddy/main.py` (`PrintDeps` :1772, `LanDeps` :1806), `backend/scadbuddy/api/printing.py` :1600 and `backend/scadbuddy/api/library_print.py` :1717 (pass `unknown_trays=PoolUnknownTrays(pool)` to the check)
- Test: `backend/tests/printers/test_unknown_spool_block.py`, `backend/tests/crud/test_crud_unknown_trays.py`

There is no Python CRUD route and no `api/crud_print.py` change. The API process still reads the rows in process (`PoolUnknownTrays`): it is the SQL holder (#5295).

**Interfaces:**
- Consumes: `UnknownSpoolReads`, `UnknownSpoolRef`, `UnknownSpoolBlock`, `PoolUnknownTrays`, `blocked_detail`, `UNKNOWN_SPOOL_LOADED` (Task 1); the golden JSON `backend/tests/fixtures/unknown-trays-block.json` (Task 3A).
- Produces:
  - `class UnknownSpoolLoadedError(RunRefusalError)`: `__init__(self, printer_name: str, refs: Sequence[UnknownSpoolRef])`, detail `blocked_detail(...)`, extension `code=UNKNOWN_SPOOL_LOADED`, attribute `refs`.
  - `async def refuse_unknown_spools(reads: UnknownSpoolReads | None, row: PrinterRow | None, *, inventory: Callable[[], Awaitable[Inventory]] | None = None) -> None`: does nothing when `reads` or `row` is `None`; raises `UnknownSpoolLoadedError(row.name, refs)` when `reads.blocking(row.id)` is non-empty. D9: when the refs are non-empty and `inventory` is given, it awaits `inventory()` once (`bambuddy/filaments.py` `read_inventory`) and drops each ref whose tray that result gives an SB spool before it decides. The read is lazy, so a printer with no unknown tray costs no Bambuddy read. The native path passes `None`. **Changed (G3):** a `CrudUnavailableError` from `reads` is logged at warning (`area="unknown-trays"`, `error` = type name) and counts as no block; a `CrudRefusedError` still fails the run.
  - `PrintCheck.unknown_trays: list[UnknownSpoolRef] = Field(default_factory=list)`.
  - `_refused_check(error: RunRefusalError) -> PrintCheck`: `PrintCheck(errors=[error.detail], unknown_trays=error.refs if isinstance(error, UnknownSpoolLoadedError) else [])`, used at the three catches.
  - A new keyword `unknown_trays: UnknownSpoolReads | None = None` on `prepare_run`, `check_print`, `_native_check` and the two check callers; `PrintDeps.unknown_trays` and `LanDeps.unknown_trays` (same type, default `None`).
  - `CRUD_AREAS["print"]` ends with `"unknown-trays"` and `PRINT_CRUD_AREAS` has it. Python's `/areas` lists every name, Go-served ones too, so this gate cannot see the Go switch (G3).
  - `class RemoteUnknownTrays` in `crud/print.py`: `__init__(self, client: CrudClient)`; `async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]` (GET `{BASE}/filament/printers/{printer_id}/unknown-trays`, parsed as `UnknownSpoolBlock`).

`prepare_run` takes a new keyword `sb_reads: SbSpoolReads | None = None` (`filament/inventory.py`, the type of `print_run.py`'s other `reads` and `sb_reads` keywords and of both callers' reads). Both callers on the Bambuddy path pass theirs: `PrintActivities` (the run) and `check_print` (`print_run.py`, its `prepare_run` call; it already holds `sb_reads` for `reading`), so the check and the run drop the same refs. `_native_check` passes none (native path). It calls `await refuse_unknown_spools(unknown_trays, row, inventory=…)` once the row is known (after the `if route is not None and route.native: … else: …` block), before `if source.is_sliced:`. On the Bambuddy path `inventory` is `lambda: read_inventory(bambuddy_client(client), sb_reads)`; on the native path it is `None`. `prepare_run` reads no inventory today (the run's plan reads it later, `_plan_sliced` too, after `prepare_run`), so this read is the one D9 uses, a sliced file included. It is a read of its own: `check_print`'s `reading` task exists only for an unsliced source with `filament_plan.slots`, so it cannot stand in for it. So the check and the run both refuse, a sliced file too, and nothing is uploaded. `PrintActivities` passes `self.d.unknown_trays`. In `lan_print`, after the "an earlier attempt's start was taken" branch, call `self._refuse_unknown(row)`: a non-empty answer raises `_failed(blocked_detail(row.name, refs), status_code=422)`. Any exception from the read is logged at warning ("the unknown trays could not be read before a LAN start", `error` = type name) and the start goes on.

- [ ] **Step 1: Write the failing tests** (`tests/printers/test_unknown_spool_block.py`: `prepare`, `lan_linked`, `bambuddy_routes` and `run_request` as in `tests/printers/test_run_printer.py`; `Scripted` and `_input` as in `tests/printers/test_lan_start.py`; `class FakeReads` with `refs: list[UnknownSpoolRef]` and `fail: Exception | None`)

```python
async def test_prepare_run_refuses_a_printer_with_an_unknown_tray(...): ...
    # prepare(..., unknown_trays=FakeReads([ref "AMS-A slot 2"])) raises UnknownSpoolLoadedError;
    # .status == 422; .detail == "Garage has an unknown spool in AMS-A slot 2: link it on the Filament page first."
    # the error's problem extension code == "unknown_spool_loaded"; no Bambuddy upload route called
async def test_prepare_run_without_unknown_trays_prepares(...): ...   # FakeReads([]) → PreparedRun
async def test_a_sliced_file_is_refused_too(...): ...
async def test_the_check_says_it_as_an_error_with_the_tray_ids(...): ...
    # check_print(..., unknown_trays=FakeReads([ref])) → errors == [detail], unknown_trays == [ref]
async def test_prepare_run_goes_on_when_the_area_is_not_served(..., caplog): ...   # G3
    # FakeReads(fail=CrudUnavailableError("404 without X-ScadBuddy-Crud")) → PreparedRun; one warning with area "unknown-trays"
async def test_prepare_run_fails_on_a_refused_credential(...): ...   # FakeReads(fail=CrudRefusedError(...)) → raises
async def test_lan_print_refuses_before_the_command(pg_pool): ...
    # LanDeps(unknown_trays=FakeReads([ref])); ApplicationError; details[0].status == 422; Scripted.commands == []
async def test_lan_print_starts_when_the_read_fails(pg_pool, caplog): ...
    # FakeReads(fail=RuntimeError) → LanStarted; one warning record
async def test_a_start_already_taken_is_not_refused(pg_pool): ...
    # baseline report already shows subtask_name RUNNING: LanStarted, FakeReads not called
async def test_bambuddy_path_does_not_block_an_assigned_tray(...): ...   # D9
    # FakeReads([ref]) and an inventory whose tray for that ref holds an SB spool → PreparedRun;
    # check_print with the same request → no unknown_spool_loaded error (the check agrees with the run);
    # the same on the native path → UnknownSpoolLoadedError (inventory is None there)
```

`tests/crud/test_crud_unknown_trays.py`:

```python
def test_the_print_worker_waits_for_the_unknown_trays_area() -> None:
    assert "unknown-trays" in PRINT_CRUD_AREAS
    assert CRUD_AREAS["print"][-1] == "unknown-trays"
async def test_remote_blocking_parses_the_golden_block(...): ...
    # a stub CrudClient answers backend/tests/fixtures/unknown-trays-block.json (the file Go's test pins);
    # RemoteUnknownTrays(crud).blocking(pid) == the refs of that file, in order
async def test_remote_blocking_raises_when_the_route_is_missing(...): ...
    # a stub that raises CrudUnavailableError → RemoteUnknownTrays lets it pass up (refuse_unknown_spools catches it)
```

- [ ] **Step 2: Type-check the tests** (no local test run; CI's `backend-pytest` lane runs them)

Run: `cd backend && uv run --frozen ruff check tests/printers tests/crud && uv run --frozen mypy`
Expected: only errors that name the missing symbols.

- [ ] **Step 3: Implement the error, `refuse_unknown_spools`, the `PrintCheck` field, the keywords, `RemoteUnknownTrays`, the area name, the LAN check and the wiring**

- [ ] **Step 4: Find the tests the block changes**

Run: `grep -rln "loaded\|lan-report" backend/tests/api/test_print_destination_native.py backend/tests/api/test_print_plates.py backend/tests/api/test_print_library.py backend/tests/printers`
Expected: a test whose LAN printer's fixture report shows a loaded untagged tray with no linked spool is now refused. Link that tray in the test's setup, or assert the refusal when the test is about it. Never pass `unknown_trays=None` to dodge it. CI's `backend-pytest` lane must pass after the push.

- [ ] **Step 5: Lint and type-check** — `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`; expected: no errors.

- [ ] **Step 6: Commit** — `feat(print): refuse ScadBuddy's prints to a printer with an unknown spool (#7143)`

---

### Task 5: The frontend's Needs-you source and the resolve dialog

Load the `frontend-design` skill before this task and Task 6 (the dialog and the callouts are UI).

**Files:**
- Create: `frontend/src/api/unknownTrays.ts` (hand-written types and three calls, G12: Go serves these routes, so `pnpm gen:api` has no types for them)
- Create: `frontend/src/mocks/features/filament/unknownTrays.ts`
- Modify: `frontend/src/mocks/features/filament/index.ts` (export `placeSpoolAtTray`)
- Create: `frontend/src/lib/unknownSpools.ts`
- Modify: `frontend/src/lib/needsYou.ts`, `frontend/src/agent/attentionNotifications.ts` (`noticeTitle`), `frontend/src/components/AppShell.tsx` (:212-276), `frontend/src/test/needsYou.tsx`
- Create: `frontend/src/components/filament/UnknownSpoolDialog.tsx`
- Modify: `frontend/src/components/activity/NeedsYou.tsx` (`kindLabel`, `sourceText`, `askText`, `NeedsYouRow` actions)
- Test: `frontend/src/lib/unknownSpools.test.ts`, `frontend/src/components/filament/UnknownSpoolDialog.test.tsx`, `frontend/src/components/activity/NeedsYou.test.tsx`

**Interfaces:**
- Consumes: Task 3A's and 3B's routes (hand-written types; the key sets match Task 3A's golden JSON).
- Produces:
  - `api/unknownTrays.ts` (interfaces written by hand from the Go route's JSON: `UnknownSpoolTray`, `UnknownSpoolTrays`, `UnknownSpoolCandidate`, `UnknownSpoolResolve`, `UnknownTrayResolved = {id: string; number: number; code: string; location_id: string | null}` (G6), `TagMadeSpool`): `listUnknownTrays(): Promise<UnknownSpoolTrays>` (a 404 `ApiError` gives `{trays: [], made_from_tags: []}`, G4); `resolveUnknownTray(id: string, body: UnknownSpoolResolve): Promise<UnknownTrayResolved>` (POST with `request`); `dismissUnknownTray(id: string): Promise<void>`.
  - `lib/unknownSpools.ts`:
    - `codeFromScan(text: string): string | null`: `"S-142"`, `" s142 "`, `"https://sb.example/x/S-142"` and `"/x/s-142"` give `"S-142"`; an `L-` code or junk gives `null`.
    - `prefillNetWeight(remainPct: number | null | undefined, labelWeightG: number | null | undefined): number | null`: `Math.round(remainPct * labelWeightG / 100)` when both are set, else `null`.
    - `unknownSpoolTitle(tray: UnknownSpoolTray): string`: `"Unknown spool in <printer_name> <slot>"`.
    - `useUnknownSpools(): { data: UnknownSpoolTrays | null; refresh: () => void }`: read at once, every `ATTENTION_POLL_MS`, on `visibilitychange` and `focus`; a failed read keeps the last answer (as `useGrantRequests`); a 404 is an empty answer, not a failure (G4).
    - `useTagMadeToasts(made: readonly TagMadeSpool[] | null): void`: one toast per spool that first appears after the first answer: title `"Spool <code> made from its RFID tag"`, detail `"<printer_name> <slot>"`, action "Open" → `filamentLinks.spool(spool_id)`.
  - `needsYou.ts`: `NeedsYouItem` adds `{ kind: 'unknown_spool'; key: string; since: string; tray: UnknownSpoolTray }` (key `unknown_spool:<id>`, `since` = `first_seen_at`). `needsYouItems(waiting, grants, pairings, unknown: readonly UnknownSpoolTray[] = [])`; `NeedsYouSource` adds `'filament'`; `sourceOf`, `toastTitle`, `toastDetail` and `needsYouNotices` handle the kind (`reason: 'unknown_spool'`, message `unknownSpoolTitle(tray)`, `href: NEEDS_YOU_PATH`). `noticeTitle('unknown_spool')` is `'An unknown spool is loaded'`.
  - `UnknownSpoolDialog({ tray, open, onOpenChange, onResolved }: { tray: UnknownSpoolTray; open: boolean; onOpenChange: (open: boolean) => void; onResolved: (resolved: UnknownTrayResolved) => void })`.

The dialog (`components/ui/dialog`) shows: the title `unknownSpoolTitle(tray)`; what the tray reports (material, `RgbaSwatch` of `colour_rgba`); a "Spool code" input that runs `codeFromScan` on change and paste; the candidates as a radio list (code, material, swatch, grams left, the place name or "No place yet"); "Net weight (g)", prefilled by `prefillNetWeight(tray.remain_pct, candidate.label_weight_g)` when a candidate is picked and the field is still empty, required; a link "Add from an invoice" to `filamentLinks.invoices`; the button "Link spool". It sends `{spool_id | code, net_weight_g, seen_at: tray.seen_at}`. On 409 `tray_changed` it says "This tray changed since the dialog opened. Close it and open it again." and calls `ctx.refresh()`. On 404 `code_unknown` it says "No spool has the code <code>." Other `ApiError`s show `detail`. In `NeedsYouRow`, an `unknown_spool` item has the action "Tell ScadBuddy which spool…" (opens the dialog, compact or not), `kindLabel` "Unknown spool", `sourceText` `"<printer_name> <slot>"`, and `askText` `"Prints to <printer_name> wait until you link the spool in <slot>."`. `AppShell` and `test/needsYou.tsx` read `useUnknownSpools()` and pass `data?.trays` to `needsYouItems`; `refreshNeeds` refreshes it too.

The mock (`unknownTrays.ts`, exports `handlers` and `reset`) seeds one tray on `H2C` (`seed.ts`): ams 0, tray 1, PLA `#FF0000FF`, `remain_pct: null`, with candidates from the mock's spools ranked unplaced first, then by number, at most 10. `made_from_tags` starts empty. The resolve answers 409 `{code: 'tray_changed'}` for a stale `seen_at` or an id it does not hold, and 404 `{code: 'code_unknown'}` for a code no spool has. Otherwise it calls `placeSpoolAtTray(spoolId, printerId, amsId, trayId)` (new in `index.ts`: moves the mock spool to the tray's place, making the place as `index.ts` makes tray places), drops the tray and answers `{id, number, code, location_id}` (G6). The dismiss route answers 204.

- [ ] **Step 1: Write the failing tests**

```ts
// lib/unknownSpools.test.ts
it.each([['S-142','S-142'],[' s142 ','S-142'],['https://sb.example/x/S-142','S-142'],['/x/s-142','S-142'],['L-3',null],['hello',null]])(
  'codeFromScan(%j) is %j', (text, code) => expect(codeFromScan(text)).toBe(code))
it('prefills from remain and label weight', () => {
  expect(prefillNetWeight(50, 1000)).toBe(500); expect(prefillNetWeight(null, 1000)).toBeNull() })
it('needsYouItems lists an unknown spool by its first sight', () => {
  // needsYouItems([], null, [], [tray]) → [{ kind: 'unknown_spool', key: `unknown_spool:${tray.id}`, since: tray.first_seen_at }]
  // sourceOf(item) === 'filament'
})

// UnknownSpoolDialog.test.tsx (msw mocks)
it('links the picked candidate with the typed weight')  // POST body { spool_id, net_weight_g: 980, seen_at }; onResolved called
it('takes a scanned label URL as a code')               // type 'https://x/x/S-1' → body.code === 'S-1'
it('says when the tray changed')                        // server.use a 409 tray_changed → that text; ctx.refresh called
it('says when no spool has the code')                   // 404 code_unknown → "No spool has the code S-999."
it('needs a weight')                                    // "Link spool" is disabled while the weight field is empty

// NeedsYou.test.tsx
it('shows nothing, and no error, when the list route is a 404')   // G4: needsYouItems gets []; no toast
it('shows an unknown spool and opens its dialog')
  // NeedsYouHarness → a row headed "Unknown spool" with "H2C AMS-A slot 2"; clicking "Tell ScadBuddy which spool…" opens the dialog
```

- [ ] **Step 2: Type-check the tests** (no local test run, G15; CI's `frontend-unit` job runs them)

Run: `cd frontend && pnpm typecheck`
Expected: the only errors name the missing modules.

- [ ] **Step 3: Implement the API calls, the mock, the lib, the source, the dialog and the row**

- [ ] **Step 4: Type-check, and read CI**

Run: `cd frontend && pnpm typecheck`
Expected: no errors. After the push, `frontend-unit` must pass `src/lib`, `src/components/activity`, `UnknownSpoolDialog.test.tsx`, `AppShell.test.tsx` and `src/mocks/features/filament`. Re-run once before debugging a timeout (the root CLAUDE.md "Known flakes").

- [ ] **Step 5: Lint and type-check**

Run: `cd frontend && pnpm lint && pnpm typecheck`
Expected: no errors.

- [ ] **Step 6: Commit** — `feat(filament): ask which spool an unknown tray holds, from Needs you (#7143)`

---

### Task 6: The dialog's other entry points and the RFID toasts

Split from Task 5: a reviewer can accept the dialog and its source and still reject where it shows up. Load the `frontend-design` skill (Task 5 says so too).

**Files:**
- Create: `frontend/src/components/filament/UnknownSpoolButton.tsx`
- Modify: `frontend/src/pages/filament/LocationPage.tsx`, `frontend/src/pages/filament/LocationsTab.tsx`, `frontend/src/components/print/PrintVerdict.tsx`, `frontend/src/components/AppShell.tsx` (`useTagMadeToasts`)
- Test: `frontend/src/pages/filament/LocationPage.test.tsx`, `frontend/src/pages/filament/LocationsTab.test.tsx`, `frontend/src/components/print/PrintVerdict.test.tsx` (create if absent), `frontend/src/lib/unknownSpools.test.ts`

**Interfaces:**
- Consumes: Task 5's dialog, `listUnknownTrays` (`api/unknownTrays.ts`), `useTagMadeToasts`; Task 4's `PrintCheck.unknown_trays`.
- Produces:
  - `UnknownSpoolButton({ trayId, label, onResolved }: { trayId: string; label?: string; onResolved?: () => void })`: on click it reads `listUnknownTrays()`, finds `trayId` and opens `UnknownSpoolDialog`. When the tray is gone it says "That tray is no longer unknown." The default label is "Tell ScadBuddy which spool…".
  - `LocationPage`: when `place.printer_id`, `place.ams_id` and `place.tray_id` match a tray of `listUnknownTrays()`, a callout above "Spools": `"An unknown spool is loaded here. Prints to this printer wait until you link it."` with the button; after a resolve it reloads the place.
  - `LocationsTab`: when trays are unknown, a list above the tree, one line per tray (`unknownSpoolTitle`), each with the button.
  - `PrintVerdict`: under the errors, one `UnknownSpoolButton` per `verdict.unknown_trays` entry, labelled `"Link the spool in <slot>"`; its `onResolved` calls the dialog's existing re-check (`onRetry`).
  - `AppShell` calls `useTagMadeToasts(unknown.data?.made_from_tags ?? null)`.

- [ ] **Step 1: Write the failing tests**

```ts
// LocationPage.test.tsx
it('says an unknown spool is loaded in this slot and links one')
  // msw: the place of H2C ams 0 tray 1 → the callout text; the button opens the dialog; after Link spool the callout is gone
it('says nothing for a place with no unknown tray')
// LocationsTab.test.tsx
it('lists the unknown trays above the tree')        // "Unknown spool in 3DP-31B-598 AMS-A slot 2"
// PrintVerdict.test.tsx
it('offers to link the spool the block names')
  // verdict { errors: [detail], unknown_trays: [{ id, slot: 'AMS-A slot 2' }] } → a button "Link the spool in AMS-A slot 2"
// unknownSpools.test.ts
it('toasts a spool made from a tag only when it appears after the first read')
  // first render with [a] → no toast; rerender with [a, b] → one toast titled "Spool S-2 made from its RFID tag"
```

- [ ] **Step 2: Type-check the tests** (no local test run, G15)

Run: `cd frontend && pnpm typecheck`
Expected: the only errors name the missing button and props.

- [ ] **Step 3: Implement the button, the callouts, the verdict button and the toasts**

- [ ] **Step 4: Type-check, and read CI**

Run: `cd frontend && pnpm typecheck`
Expected: no errors; `frontend-unit` passes `src/pages/filament`, `src/components/print`, `src/components/PrintPicker.test.tsx` and `src/lib/unknownSpools.test.ts` after the push.

- [ ] **Step 5: Lint and type-check** — `cd frontend && pnpm lint && pnpm typecheck`

- [ ] **Step 6: Commit** — `feat(filament): link an unknown spool from its place, Locations and the print dialog (#7143)`

---

### Task 7: The agent's tools

**Files:**
- Modify: `agent/src/tools/filament.ts` (two tools after `filament_printer_labels`; `filament_unknown_tray_resolve: 'spool'` in `filamentTools`' kinds)
- Create: `agent/src/api/unknownTrays.ts` (a hand-written `paths` fragment for the three Go routes, G12)
- Modify: `agent/src/sessions/touched.ts` (`TOUCHES_NOTHING`, beside `filament_spool_move`)
- Test: `agent/test/filament.test.ts`

**Interfaces:**
- Consumes: Task 3A's and 3B's routes (the public ones; the tools call `/api/v1/filament/unknown-trays…`, never the CRUD route).
- Produces:
  - `filament_unknown_trays`: risk `read`; input `{}`; route `GET /api/v1/filament/unknown-trays`. The description says what a row is, what `candidates` are and how they are ordered, that `made_from_tags` lists spools capture made from RFID tags in the last 24 h, and that ScadBuddy refuses prints to a printer with a row (422 `unknown_spool_loaded`).
  - `filament_unknown_tray_resolve`: risk `write`, not gated; input `{ tray_id: uuid, spool_id?: uuid, code?: string, net_weight_g: number > 0, seen_at: string }`. Exactly one of `spool_id` and `code` is required, else `ToolError('give spool_id or code, not both')`. Route `POST /api/v1/filament/unknown-trays/{tray_id}/resolve` (answers `{id, number, code, location_id}`, G6); `summarize`: `Link spool ${code ?? spool_id} to unknown tray ${tray_id}`. The description lists 409 `tray_changed` (read the list again), 404 `code_unknown`, 409 `spool_archived`, 409 `no_ams_kind`, and "only what the user says: never pick a candidate yourself".

- [ ] **Step 1: Write the failing tests** (beside `describe('filament location tools')`)

```ts
it.each([['filament_unknown_trays', 'read'], ['filament_unknown_tray_resolve', 'write']])(
  '%s is %s tier, never gated', (name, risk) => { expect(tool(name).risk).toBe(risk); expect(tool(name).gated).toBe(false) })
it('lists unknown trays and resolves one by code')
  // msw GET → { trays: [{ id: ROW, candidates: [{ spool_id: ROW }] }], made_from_tags: [] };
  // the answer's candidates[0] has spool_ui_path `/filament/spools/${ROW}`;
  // POST body === { code: 'S-12', net_weight_g: 980, seen_at: T }; the answer has ui_path `/filament/spools/${ROW}`
it('refuses both or neither of spool_id and code')    // isError true, no request made
```

`agent/test/touched.test.ts` already fails for a write tool that is not classified; it passes once `TOUCHES_NOTHING` has the new name.

- [ ] **Step 2: Type-check the tests** (no local test run, G15; CI's `agent` job runs them)

Run: `cd agent && pnpm gen:api && pnpm typecheck`
Expected: the only errors name the missing tools.

- [ ] **Step 3: Implement the two tools and the `TOUCHES_NOTHING` entry.** `schema.d.ts` has no entry for Go-served routes, so the tools call through a second client typed by the `paths` fragment in `agent/src/api/unknownTrays.ts`, made over the same base URL, headers and fetch as `backend` (find where `backend` is built in `agent/src/`). A 404 from the list tool is a problem the tool shows as it stands (G4).

- [ ] **Step 4: Lint and type-check**

Run: `cd agent && pnpm lint && pnpm typecheck`
Expected: no errors. After the push, the `agent` job must pass `test/filament.test.ts`, `test/touched.test.ts` and `test/filamentLinks.test.ts`.

- [ ] **Step 5: Commit** — `feat(agent): list unknown trays and resolve one (#7143)`

---

### Task 8: Docs

**Files:**
- Modify: `backend/CLAUDE.md` (the `filament/` bullet that describes `capture.py`, near :864; the print block beside the `filament` CRUD area text near :719-738)
- Modify: `frontend/CLAUDE.md` (the Filament section, :251-267: the dialog, its entry points, the Needs-you source)
- Modify: `agent/CLAUDE.md` (beside the filament tools, near :327-331)
- Modify: `docs/features/spools-and-filament.md` (What it does, How it works, Code, Data, Tests, Limits)
- Modify: `docs/architecture/data.md` (the new table, beside the other `filament_*` tables)
- Modify: `tools/go/core/README.md` (if Task 3A did not finish it: the area table rows for `unknown-trays` and `unknown-trays:read`), `docs/architecture/processes.md` (the core's public routes and the area, near :249)
- Modify: `docs/superpowers/specs/2026-11-23-unknown-spool-flow-design.md` (an "As built (#7143)" section after "Left to the builder")
- Modify: `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.3: one "As built in #7143" line)

Write each note in ASD-STE100 style. Cite each measured fact with where it was measured (root CLAUDE.md "PR conventions", #259).

- [ ] **Step 1: Write the notes.** They say:
  - The table and when a row exists. D1: `seen_at` moves only when the reported fields change.
  - Capture's `unknown_trays` step: RFID auto-create (`match_tray`, else a typeless spool, `data.by = "rfid_capture"`), the try-lock, D2 (no external rows), and the backstop.
  - The routes (Go, area `unknown-trays`, #8038's gate), their codes and the ranking key. D4 (first weight only). D5 (`made_from_tags`). G5 to G10: the Go resolve is a second writer held equal to Python's `link` by the vectors; it answers `{id, number, code, location_id}`; it publishes no event; the lock order.
  - The block: `prepare_run`, the check's `unknown_trays`, the Go CRUD route `unknown-trays` on caller `print`, and the `lan_print` check. Both fail open on a missing route (G3). Say that the worker-side block works only once `SCADBUDDY_CORE_AREAS` names the area.
  - The Needs-you source (15 s), the dialog, its four entry points, and the agent's two tools.
  - Limits: D2 and D3; prints started in Bambuddy or on the printer are not blocked; a row whose unit the report no longer shows stays until that unit returns (deleting or merging the printer drops it).

- [ ] **Step 2: Check that no doc now says "Needs you" has three sources**

Run: `cd "$(git rev-parse --show-toplevel)" && grep -rn "Needs you" --include=*.md docs frontend/CLAUDE.md agent/CLAUDE.md backend/CLAUDE.md | grep -i "grant\|pairing"`
Expected: each hit that lists the sources also names the unknown spools.

- [ ] **Step 3: Commit** — `docs(filament): the unknown spool flow as built (#7143)`

---

### Task 9: Switch the area on (outside this repo; eh-homelab/clusters)

This is an explicit step of the issue, not code in this repo. It follows G16 and G2.

- [ ] **Step 1: Wait for the image.** The `scadbuddy-core` image on prod must hold the `unknown-trays` area (Task 3A and 3B merged and deployed: read the pod images, `kubectl -n bambuddy get pods -o jsonpath` as `deploy/CLAUDE.md` "Prod checks" says). Core stops its start on an area name it does not know, so never merge Step 3 first.
- [ ] **Step 2: Check the worker's route.** Read the print worker's CRUD base URL and the core container's `SCADBUDDY_CORE_CRUD_LISTEN`. Expected: the worker reaches core's CRUD port (`:8082`), not Python's own listener. If it does not, the Go CRUD route is unreachable: stop and say so on the issue (G2).
- [ ] **Step 3: Open the clusters PR.** In `scadbuddy.yaml`, add `unknown-trays` to `SCADBUDDY_CORE_AREAS` on the `scadbuddy-core` container (with the CRUD `print` caller's credential unchanged). Title `feat(scadbuddy): serve the unknown-trays area from core (eh-homelab/ScadBuddy#7143)`.
- [ ] **Step 4: Check prod after the sync** (`status.history[-1].deployedAt` of the `scadbuddy` Argo CD app, root CLAUDE.md "Prod gotchas"). Expected: `GET /api/v1/filament/unknown-trays` from the signed-in page answers 200, and a service-credential `GET …/crud/print/filament/printers/<id>/unknown-trays` answers 200 with `X-ScadBuddy-Crud: 1`. Post both on #7143.
- [ ] **Step 5: Rollback** is dropping the name from `SCADBUDDY_CORE_AREAS`: the routes 404 again, the frontend hides the source and the worker fails open (G3, G4).
