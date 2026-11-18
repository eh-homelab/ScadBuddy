# Filament sync with Bambuddy, configurable per data type (#6265) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **UI work (Task 11 touches the frontend):** the implementer loads `frontend-design:frontend-design` before writing any UI. Pass this line on to every UI subagent too.

**Goal:** A person picks, per data type (spools and types, weights, tags, suppliers and
prices, locations, presets and K), whether filament data moves `off`, `sb_to_bb`,
`bb_to_sb` or `two_way` between ScadBuddy and Bambuddy. SB's changes reach Bambuddy within
seconds, each write is idempotent by field hash, and in two-way mode the newer change wins
per field.

**Architecture:** One Temporal workflow per Bambuddy instance, `FilamentBbSync`, runs on the
existing print worker (`scadbuddy-print`, `bambuddy` queue), which already holds the `print`
service credential and reads the Bambuddy settings through CRUD. The API signals it
(signal-with-start) after every filament write and starts it at boot; a timer gives a full
sweep every `spool_overlay_refresh_seconds`. Each pass reads Bambuddy's rows (GET), posts
them to a CRUD route that computes the writes SB wants (`plan`), performs them against
Bambuddy, and posts the answers back (`results`), which records links, field hashes and
what Bambuddy now holds. Up to four plan rounds per pass let a spool write use the BB id of a
location or supplier created in the round before. The BB→SB side stays the existing pull in
the API's `SpoolRefresh` (#6261), gated per data type and widened to archive, K values,
suppliers and locations. Tag bindings keep #6264's queue and drain.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (sync, `dict_row`), pydantic v2, httpx,
respx, Temporal Python SDK, Postgres 18; React 19, Vite, shadcn/ui, msw, vitest,
Playwright; TypeScript agent tools (zod, msw).

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§3, §8 whole,
§10 "Settings gains a Filament configuration section", §12 row #6265 and the #6264 note
under it). Issue #6265. Epic #6260. Plans that bind this one, with their "Decisions made
while building": `2026-11-14-filament-data-model.md` (#6261), `2026-11-17-filament-rfid.md`
(#6264), `2026-11-17-filament-suppliers.md` (#6266), `2026-11-18-filament-invoice-import.md`
(#6267), and `2026-11-18-filament-weights-sensors.md` on `feat/filament-weights` (#6268,
not merged when this plan was written).

## Global Constraints

- Database rule (root CLAUDE.md "Database access (#5295)"): only the CRUD service (API
  process) has SQL. The print worker gets no pool, no grant, no database secret; it reaches
  SB data only through `crud_router("print")` routes, which require its service credential
  from this PR on. No new database client anywhere.
- No untrusted code moves: the sync runs no template Python, no openscad, no plugin code.
- Never a live Bambuddy write. Every Bambuddy write is tested with respx, and every respx
  body is checked against a recorded schema (`backend/tests/bambuddy/recordings/openapi/`,
  `tests/bambuddy/conftest.py` `recorded_schema`). Live reads are `GET` only, with no key; on
  a 401, stop and report, never look for a vault item.
- Bambuddy calls go through `bambuddy/client.py` `BambuddyClient._send`, each with a `Scope`
  and an operation name added to the client's operation list. A stored failure reason is
  `rfid_push.error_label(exc)`, never a message or a class name.
- SB first, Bambuddy second: a failed or partial pass never changes or rolls back SB data;
  only `results` writes SB, and only for writes Bambuddy answered.
- Every setting is a remembered choice with a toggle: the six directions live in the one
  stored `filament_sync` setting, are shown in Settings and saved with "Save Filament". No
  hard-coded direction anywhere.
- Every tool works in classic and durable mode. Every new public `/api/v1` route sets
  `summary=` and `description=` and gets an agent tool or a `NOT_A_TOOL` entry
  (`agent/src/tools/coverage.ts`). Every agent write tool is classified in
  `agent/src/sessions/touched.ts`.
- Migrations (#491): new files `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`
  only; never edit a merged file. Timestamps `timestamptz`; enums `text` with `CHECK`.
- A workflow added to the print worker is AUTO_UPGRADE (`print_deployment`); its later
  command changes go behind `workflow.patched`, and its committed history replays
  (`tests/test_filament_sync_replay.py`). `SpoolRefresh` is not changed by this plan.
- Frontend mocks never call `crypto.randomUUID`. No image or blob is base64 in JSON.
- Secure by default: every new direction defaults to `off`, except the ones whose current
  behaviour Decision 3 keeps.
- Generated API files are never committed (`pnpm gen:api` after a route change).
- Every new Bambuddy fact cites the Bambuddy source at the tag of the live instance's
  `info.version` (Task 2 Step 1 records it; main already cites `v1.3.2b1-daily.20261113`) or
  the recorded OpenAPI; every measured fact says where and how it was measured (#259).
- No `git stash`, no amend, no `pkill -f`, no `killall`.
- Commands: `cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test uv run --frozen pytest <files>`,
  then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`;
  frontend `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm exec vitest run <file>`;
  agent `cd agent && pnpm gen:api && pnpm test <file> && pnpm typecheck`.

## Decisions this plan makes

Each says why, and what it costs if wrong.

1. **The sync runs on the existing print worker, not a new one.** It already holds the
   `print` service credential, reads the Bambuddy key through `GET …/crud/print/settings`,
   has no database secret, and runs no untrusted code. Why: spec §8.2 and the issue put the
   sync in a worker that calls CRUD routes, and the owner prefers reusing a worker the rules
   allow. Cost: a sync pass shares the print worker's activity slots with print runs; a pass
   is short and bounded (Decision 11).
2. **One clusters change: a `Recreate` roll of `scadbuddy-print`.** Same Deployment, same
   credential, same Bambuddy key; the new CRUD area `filament-sync` is served by the API pod
   the print worker already waits for (`wait_for_areas`). `FilamentBbSync` is a new workflow
   type on the `bambuddy` queue, so README "A release that needs `Recreate`" applies: a
   clusters PR sets `strategy: {type: Recreate}` on `scadbuddy-print` (and removes its
   `rollingUpdate:` block), merges before the deploy PR, and a second clusters PR restores
   `RollingUpdate` after the roll (Task 12). Cost: the print worker is down for one restart
   during the roll. If Bambuddy's auth is turned on, the key needs
   `can_manage_inventory`; a 403 then shows in the sync status with its scope, and the fix is
   the key's flags in Bambuddy, not a manifest.
3. **One setting, six data types, four values.** `FilamentSync` gets
   `SyncDirection = Literal["off", "sb_to_bb", "bb_to_sb", "two_way"]` for `spools`,
   `weights`, `tags`, `suppliers`, `locations`, `presets`, with helpers
   `pulls(d) = d in {"bb_to_sb", "two_way"}` and `pushes(d) = d in {"sb_to_bb", "two_way"}`.
   Defaults keep today's behaviour: `spools`, `weights`, `presets` and `tags` default to
   `bb_to_sb` (the pull already does each of them); `suppliers` and `locations` to `off`.
   Why: widen #6264's and #6268's fields, never duplicate them. Cost: none for an install
   that never changes the setting.
4. **Stored `tags` values are rewritten once, by a migration.** #6264's `off` meant "the pull
   binds BB's tags, nothing is pushed", which is now `bb_to_sb`; its `sb_to_bb` meant "pull
   and push", now `two_way`. The migration rewrites the stored `filament_sync` row's `tags`
   key exactly so (guarded by the old values, a no-op elsewhere). Why: `off` must now mean
   off, and no install may change behaviour silently. Cost: an old API pod during the roll
   reads `two_way` as invalid and falls back to the environment's value for one roll
   (`settings_store` `check_value` already does that).
5. **The BB→SB side stays the pull in the API's `SpoolRefresh`.** It already reads Bambuddy,
   applies "newer change wins" per field and counts consumption once (#6261, #6263). This
   plan gates it per data type and adds archive, K values, suppliers and locations to it.
   Why: moving it into the worker would rewrite #6261-#6268's tested code for no user-visible
   gain. Cost: Bambuddy reads for the pull stay in the CRUD service's pod; Task 12 files a
   follow-up issue to move them to the worker through a CRUD route.
6. **Tags keep #6264's queue and drain** (`filament_bb_tag_pending`, `rfid_push.drain_tags`,
   with its 409 move rule), gated by `pushes(tags)`. The engine never sends `tag_uid` or
   `tray_uuid`; when `results` links a new BB spool, it queues the spool's open tags. Why: the
   drain already handles the 409 `tag_already_linked` move and was reviewed. Cost: a new
   spool's tags reach Bambuddy on the next drain (one refresh interval, or the inline try).
7. **The plan is state-based, the hash only skips.** Each link stores `pushed_fields`
   (field → `field_hash(value)`, the first 16 hex of SHA-256 of canonical JSON). An
   event-driven pass skips a spool whose every hash matches; a full pass (the timer, or "Sync
   now") ignores hashes and compares with Bambuddy's current rows. Why: hashes make a nudge
   cheap; the full pass repairs a change made in Bambuddy while `sb_to_bb` is set, and any
   lost signal. Cost: a Bambuddy-side edit under `sb_to_bb` survives up to one interval.
8. **Newer change wins, per field, ties to Bambuddy.** Under `two_way` a field is pushed only
   when SB's value differs from Bambuddy's and either Bambuddy's value still equals what SB
   last saw there (`filament_bb_links.bb_seen`), or SB's change time is strictly after BB's
   `updated_at`. SB's change time is the newest `edited` event naming the field, else the
   spool's `created_at`, and for an inherited value the later of that and the type's
   `updated_at`; for `weight_used` it is the newest ledger event's `at`; for `location_id`
   the newest `moved` event. This `sb_changed_at` is the one rule: the pull (Task 9) uses it
   too, in place of `_update`'s `edited`-only query. Under `sb_to_bb` a differing field is
   always pushed. Why: the
   pull keeps SB's value only when SB edited strictly after BB's `updated_at`, so a tie must
   go to Bambuddy on both sides, or the two would flip-flop. The clocks agree within about
   0.25 s (spec §8.2, measured 2026-11-15). Cost: an edit made in both systems within that
   skew may resolve to the older one.
9. **SB's own push never comes back as a change.** `results` stores `seen_row(answer)` (Task
   3) as `bb_seen`,
   moves `bb_updated_at` to the answer's `updated_at`, and books one `synced_from_bb` event
   with `data = {"bb": snapshot(answer), "pushed": true, "changes": {}}`, no reading, ref
   `<bb id>@<updated_at>`. The pull compares with the newest event's `data.bb`, so it sees no
   change. Why: reuses the pull's own "before" rule, no pull change. Cost: one ledger row per
   pushed spool write, shown in the spool's history as "sent to Bambuddy".
10. **A create is guarded by an intent.** Before `POST` of a spool, supplier, location,
    catalog entry or filament-catalog entry, the plan inserts `filament_bb_creates (kind,
    sb_id, started_at)`. `results` deletes it when it links the answer. A later plan that
    finds an intent older than the pass adopts a matching Bambuddy row created after
    `started_at` (spools: same `snapshot` values and `material_number`; the others: same upper
    case name) instead of creating again; the pull skips a BB spool such an intent can claim.
    An item with an open intent younger than `snap.read_at` (a create whose reply was lost in
    this pass) is skipped and counted `deferred` until a pass reads Bambuddy again.
    Why: a crash between Bambuddy's answer and `results` must not make a duplicate in either
    system. Cost: two identical spools created in one window may be adopted the other way
    round (they are identical).
11. **A pass is bounded.** At most 4 plan rounds and 200 Bambuddy writes; each round asks the
    plan route for at most `200 − written` writes (`PlanRequest.limit`). With more left the
    pass returns `more=True` and the workflow runs the next one at once. The activity
    heartbeats after every write (heartbeat timeout 60 s, start-to-close 15 min, 3 attempts).
    Why: the first push of an inventory of about 100 spools is about 400 writes. Cost: the
    first full push takes a few passes.
12. **One workflow per Bambuddy instance, and links belong to one instance.** The workflow id
    is `filament-bb-sync-<sha256(normalised bambuddy_url)[:12]>`; the one-row
    `filament_bb_sync_state` records the instance the links were made against (set by the
    first `results` that links anything; for links made before the state row, by the API at
    boot before its first nudge, Task 8). A pass whose instance differs from the stored one
    plans nothing and reports `instance_changed`; a workflow whose instance is no longer the
    configured one ends. Why: a link's BB id means nothing on another Bambuddy, and patching
    it there would overwrite a stranger's spool. Cost: changing Bambuddy URL stops the sync
    until a person resets the links; that reset is out of scope (Task 12 files it).
13. **A spool is created in Bambuddy only when it is unarchived** and `pushes(spools)`.
    Archive and restore are pushed (`POST …/archive`, `…/restore`); a BB 404 on a linked
    spool deletes the link (and its hashes), never on a missing row in a listing. A linked
    spool missing from a complete listing gets one `GET` probe (`spool_row(id)`); a 404 there
    drops the link, any other answer keeps it. A spool
    whose link is gone may be deleted in SB again (`SpoolBambuddyLinkedError` no longer
    applies) and, if unarchived and pushed, is created again on the next pass. Why: #6261 left
    the link's lifecycle to this engine, and a partial listing must never unlink. Cost: a
    person who deletes a spool in Bambuddy under `sb_to_bb` sees it come back.
14. **What each data type carries.** `spools`: material, subtype, brand, category,
    effect_type, color_name, rgba, extra_colors, label_weight, core_weight,
    core_weight_catalog_id, nozzle_temp_min/max, note, cost_per_kg,
    low_stock_threshold_pct, `material_number` (the type's `code`), archive state; plus
    insert types ↔ the spool catalog (`/inventory/catalog`) and one `/filament-catalog/`
    entry per type (`filament_spool_types.bb_filament_id`). `weights`: `weight_used`
    (= effective label weight − `grams_left`, at least 0, one decimal), `weight_locked`,
    `last_dried_at`. `presets`: `slicer_filament`, `slicer_filament_name` (the spool's
    effective preset for its loaded printer model, else the first by model and nozzle),
    `PUT …/filament-presets`, `PUT …/k-profiles` (only K values whose SB printer has a
    `bambuddy_printer_id`; others are counted as `unmapped_k`). `suppliers`: SB suppliers ↔
    BB suppliers (`bb_supplier_id`), and `PUT …/suppliers` per spool: each supplier product
    of the spool's type, with `supplier_article_number` = SKU, `quoted_price_per_kg` = the
    newest price point's per-kg price, `is_purchase_source` = the supplier of the spool's
    invoice. `locations`: SB locations that are not printer places ↔ BB locations
    (`bb_location_id`, name = the SB path joined with ` / `), and the spool's `location_id`; a
    printer place is a location with `printer_id IS NOT NULL` (an AMS unit, a tray, the
    external spool place, a plain printer place); it is never pushed as a BB location, and a
    spool in one leaves BB's `location_id` alone. `rgba` and `extra_colors` compare and hash
    in one canonical form (both sides through `colour.normalise_rgba`) and go to Bambuddy in
    its form (`RRGGBBAA`, no `#`; `extra_colors` comma-joined). Why: spec §8.1, field by field against BB 1.3.1's
    `SpoolCreate`/`SpoolUpdate`. Cost: none known.
15. **What is not pushed.** `sku-settings`: SB has no SKU settings table yet; #6270's parity
    sweep owns it. `last_scale_weight`: `SpoolUpdate` has no such field (BB 1.3.1). Spool
    supplier links and prices are never pulled: prices are SB's (invoices, §8.1 "Invoice: SB
    only"), so `suppliers` `bb_to_sb` pulls supplier rows only. Cost: a quoted price edited in
    Bambuddy is overwritten under `pushes(suppliers)` and ignored otherwise.
16. **Child data types act only on linked spools.** With `spools` `off`, no spool is created
    or linked, so weights, presets, suppliers-per-spool and a spool's location move only on
    spools linked earlier. Supplier and location rows themselves sync on their own setting.
    Why: a child needs the BB spool id. Cost: the Settings help says so.
17. **The nudge.** `FilamentSyncNudger.nudge(full=False)` does a signal-with-start of
    `FilamentBbSync` (signal `changed`, id conflict policy USE_EXISTING) in a background task
    with a 5 s timeout, logged on failure, never raised. A router dependency on both
    `/filament` routers (`api/filament.py`, `api/filament_purchasing.py`) nudges after every
    2xx non-GET response; the same dependency is on `api/tray_spools.py`'s router, and the
    settings PUT nudges when `filament_sync` changes. The workflow debounces signals for 2 s.
    The API also nudges with `full=True` once its library worker connects. Not nudged, left to
    the timer: the report-driven capture and `sync_ams_weights` (`capture.py`), and the print
    worker's CRUD `filament` writes (`…/print-uses/settle`, `bb-to-sb`). Why: one dependency
    per router covers its write routes, and a new route there cannot forget it. Cost: a write
    that changes nothing Bambuddy holds still costs one cheap pass (hashes skip it); a weight
    from capture reaches Bambuddy up to one interval late.
18. **The rolling deploy.** A new workflow type on the `bambuddy` queue is safe only once no
    old build is current (README "Rolling it", `bambuddy_worker` docstring), so the release
    rolls `scadbuddy-print` with `Recreate` (Decision 2, Task 12). A new API pod may still
    signal-with-start before the new print build is current; that workflow task fails until
    the new build takes the queue. Cost: a delay of one roll.
19. **Status lives in `filament_bb_sync_state` and `filament_bb_push_errors`.** The state row
    has `instance`, `last_pass_at`, `last_ok_at`, `last_error`, and the last pass's counts;
    `record_pass` sets `last_error` from `SyncPassResult.error`, and `last_ok_at` only when
    `error` is None;
    `filament_bb_push_errors (kind, sb_id, error, attempts, at)` holds one row per item whose
    last write failed, deleted on success. `GET /api/v1/filament/sync/status` reads both;
    `POST /api/v1/filament/sync/run` nudges with `full=True`. Why: a person (and the agent)
    must see why a spool is not in Bambuddy. Cost: none.
20. **The order of work.** This plan starts after #6268 (`feat/filament-weights`) merges: it
    widens #6268's `FilamentSync.weights` and the weights gate in the pull's `_book`. Task 0
    checks that.

## Review Focus

1. Bambuddy fails in the middle of a pass (a 500 on the third write, or the worker dies
   between Bambuddy's `POST` answer and `results`): SB data is unchanged, the failed item is
   in `filament_bb_push_errors`, and the next pass neither creates a second BB spool nor a
   second SB spool (Tasks 5, 7, 9: `test_an_intent_adopts_the_spool_bambuddy_made`,
   `test_the_pull_leaves_a_spool_an_intent_claims`).
2. Both sides changed one field under `two_way`: the newer change wins on both the push and
   the pull, a tie goes to Bambuddy on both, so the value settles and never flips back and
   forth (Task 4 `test_a_tie_goes_to_bambuddy`, Task 9 `test_push_then_pull_settles`).
3. SB's own push is read back by the next pull: no `bb` reading, no `edited` change, no new
   push (Tasks 5 and 9 `test_a_pushed_value_is_not_pulled_back`).
4. The Bambuddy URL now points at another instance: nothing is written there with the old
   links' ids, and the status says why (Tasks 4, 8 `test_another_instance_plans_nothing`,
   `test_a_workflow_for_an_old_instance_ends`).
5. A linked BB spool is deleted in Bambuddy, or a listing comes back partial: the spool is
   missing from a complete listing, so it gets a `GET` probe; a 404 on the probe drops the
   link and the spool is created again under `sb_to_bb`; a spool missing from a partial
   listing keeps its link (Task 4 `test_a_spool_missing_from_a_complete_listing_is_probed`,
   Task 5 `test_a_404_drops_the_link`, Task 4
   `test_a_spool_missing_from_the_listing_keeps_its_link`).

---

## File structure

```
backend/scadbuddy/migrations/
  <ts>_filament_sync_tags_values.sql      Task 1  (rewrites stored filament_sync.tags)
  <ts>_filament_bb_sync.sql               Task 3  (columns, creates, errors, state)
backend/scadbuddy/core/settings.py, core/setting_docs.py, api/settings.py   Task 1
backend/scadbuddy/bambuddy/client.py, models.py, errors.py                  Task 2
backend/tests/bambuddy/recordings/openapi/sync-routes.json                  Task 2
backend/scadbuddy/filament/
  bb_sync_models.py   BbSnapshot, BbWrite, BbWriteResult, SyncPlan, SyncPassResult, SyncStatus, PlanRequest, seen_row   Tasks 3-8
  bb_sync_plan.py     desired values, field hashes, decide(), plan()                Task 4
  bb_sync_results.py  apply_results(): links, hashes, echo events, intents, errors  Task 5
  bb_sync_run.py      run_pass(): BB reads, plan → write → results rounds (worker)  Task 7
  bb_sync_nudge.py    FilamentSyncNudger, workflow id                               Task 8
  bambuddy_pull.py    gates, archive, K, suppliers, locations, intents              Tasks 1, 9
  rfid_push.py        pushes(tags) gate                                             Task 1
  spools.py           delete removes the spool's intent and error rows             Task 5
backend/scadbuddy/workflows/filament_sync.py    FilamentBbSync, activity names      Task 8
backend/scadbuddy/workflows/client.py           bambuddy_worker registers it        Task 8
backend/scadbuddy/api/crud_print.py, api/crud_areas.py   plan/results routes, area  Task 6
backend/scadbuddy/crud/print.py                 RemoteFilamentSync                  Task 6
backend/scadbuddy/worker.py, main.py            activity wiring, nudge at boot      Tasks 7, 8
backend/scadbuddy/api/filament.py, api/filament_purchasing.py   nudge dependency, status/run routes   Tasks 8, 10
backend/scadbuddy/api/tray_spools.py, api/settings.py   nudge dependency, nudge on a filament_sync save   Task 8
backend/tests/filament/test_bb_sync_*.py, tests/test_filament_sync_replay.py, tests/crud/test_crud_filament_sync.py
agent/src/tools/filament.ts, agent/src/sessions/touched.ts, agent/test/filament.test.ts   Task 10
frontend/src/pages/settings/sections/filament.tsx, pages/settings/values.ts, api/filament.ts, mocks/features/filament/sync.ts, mocks/features/settings.ts, mocks/fixtures.ts   Task 11
docs/superpowers/specs/2026-11-14-filament-management-design.md, backend/CLAUDE.md, frontend/CLAUDE.md   Task 12
eh-homelab/clusters: scadbuddy-print strategy Recreate, then back to RollingUpdate   Task 12
```

---

### Task 0: Order of work

- [ ] **Step 1:** `git fetch origin && git log origin/main --oneline | grep -m1 "#6268"`. If
  #6268 has not merged, stop and report "waiting on #6268". Otherwise `git merge origin/main`.
- [ ] **Step 2:** Confirm on the merged tree: `FilamentSync.weights` exists
  (`grep -n "weights" backend/scadbuddy/core/settings.py`), and note how `pull` and `_book`
  receive the sync setting (`grep -n "sync" backend/scadbuddy/filament/bambuddy_pull.py`).
  Tasks 1 and 9 use that parameter as it is; they never add a second one.

### Task 1: Six directions, the tags rewrite, and the gates

**Files:**
- Modify: `backend/scadbuddy/core/settings.py` (`SyncDirection`, `FilamentSync`, `pulls`, `pushes`), `backend/scadbuddy/core/setting_docs.py` (`filament_sync` doc), `backend/scadbuddy/filament/bambuddy_pull.py` (`scheduled_pull`, `_book`, `_book_usage`, the spool, tag and preset steps of `_update`), `backend/scadbuddy/filament/rfid_push.py` (`push_pending`), `backend/scadbuddy/api/filament.py` (the inline tag try's gate, `import_bambuddy`'s `weights=sync.weights != "off"`, and the descriptions that name `sb_to_bb`) and `api/tray_spools.py` (the inline tag try's gate and its descriptions)
- Create: `backend/scadbuddy/migrations/<ts>_filament_sync_tags_values.sql`
- Test: `backend/tests/api/test_settings.py` (it covers `filament_sync` today; there is no `test_settings_filament_sync.py`), `backend/tests/filament/test_bambuddy_pull.py`, `backend/tests/filament/test_rfid_push.py`, `backend/tests/test_pg_migrations.py`

**Interfaces:**
- Produces: `SyncDirection = Literal["off", "sb_to_bb", "bb_to_sb", "two_way"]`;
  `FilamentSync(spools="bb_to_sb", weights="bb_to_sb", tags="bb_to_sb", presets="bb_to_sb", suppliers="off", locations="off")`, all `SyncDirection`;
  `pulls(d: SyncDirection) -> bool`, `pushes(d: SyncDirection) -> bool` in `core/settings.py`.

- [ ] **Step 1: Failing tests.**
  - `test_filament_sync_defaults_keep_todays_behaviour`: `FilamentSync()` equals the defaults above.
  - `test_filament_sync_takes_every_direction_for_every_type`: a settings PUT with each of the four values on each of the six keys round-trips.
  - `test_the_tags_migration_rewrites_the_old_values`: rows `{"spools":"bb_to_sb","tags":"off"}` → `tags == "bb_to_sb"`; `{"tags":"sb_to_bb"}` → `"two_way"`; a row already `two_way` is unchanged; no row → no row.
  - `test_spools_sb_to_bb_pulls_no_spool_fields`: with `spools` `sb_to_bb` the pull changes no SB spool field and makes no SB spool.
  - `test_weights_alone_books_usage_rows`: `spools` `sb_to_bb`, `weights` `bb_to_sb` → BB's usage rows for a linked spool are booked (`_book_usage` runs under `pulls(spools) or pulls(weights)`, linked spools only).
  - `test_nothing_pulls_skips`: every direction `off` or `sb_to_bb` → `scheduled_pull` returns `"skipped"`, respx sees no `GET /inventory/spools`.
  - `test_presets_off_leaves_sb_presets_alone` and `test_tags_off_binds_no_bb_tag_on_a_linked_spool` (a new BB spool still takes its tags: identity, Decision 6).
  - `test_tags_two_way_pushes` and `test_tags_bb_to_sb_pushes_nothing` (`push_pending`).
  - #6268's `weights` tests still pass with `off`/`bb_to_sb`, plus `test_weights_two_way_books_bb_readings`.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3:** Implement. Every `== "off"` / `!= "sb_to_bb"` check on `filament_sync` becomes `pulls(...)` / `pushes(...)`, `import_bambuddy`'s included. `pulls(spools)` gates only the spool rows (fields, new SB spools), never the whole pull: `scheduled_pull` returns `"skipped"` only when no data type pulls, usage rows book under `pulls(spools) or pulls(weights)`, and Task 9 reads suppliers and locations on their own setting (Decision 16). The migration's body: one `UPDATE settings SET value = jsonb_set(value, '{tags}', '"bb_to_sb"') WHERE name = 'filament_sync' AND value->>'tags' = 'off'`, then the same for `sb_to_bb` → `two_way`. The setting doc lists each data type, the four values, and Decision 16's sentence.
- [ ] **Step 4:** Run; expect PASS. Then ruff, mypy.
- [ ] **Step 5: Commit** `feat(filament): a sync direction per data type (#6265)`.

### Task 2: Record Bambuddy's write routes; the client methods

**Files:**
- Create: `backend/tests/bambuddy/recordings/openapi/sync-routes.json`
- Modify: `backend/tests/bambuddy/conftest.py` (`recorded_schema` reads the new file too), `backend/scadbuddy/bambuddy/client.py`, `backend/scadbuddy/bambuddy/models.py`
- Test: `backend/tests/bambuddy/test_client_sync.py`, `backend/tests/bambuddy/test_recordings.py` (or the test that checks `rfid-routes.json`)

**Interfaces:**
- Produces on `BambuddyClient` (each `Scope.MANAGE_INVENTORY` unless Step 1 records otherwise):
  `create_spool(body: dict[str, Any]) -> Spool`, `update_spool(spool_id: int, body: dict[str, Any]) -> Spool`,
  `archive_spool(spool_id: int) -> Spool`, `restore_spool(spool_id: int) -> Spool`,
  `put_spool_k_profiles(spool_id: int, rows: list[dict[str, Any]]) -> None`,
  `put_spool_presets(spool_id: int, rows: list[dict[str, Any]]) -> None`,
  `put_spool_suppliers(spool_id: int, rows: list[dict[str, Any]]) -> None`,
  `suppliers() -> list[BbSupplier]`, `create_supplier(body) -> BbSupplier`, `update_supplier(id, body) -> BbSupplier`,
  `locations() -> list[BbLocation]`, `create_location(body) -> BbLocation`, `update_location(id, body) -> BbLocation`,
  `create_catalog_entry(name: str, weight: float) -> SpoolCatalogEntry`, `update_catalog_entry(id, name, weight) -> SpoolCatalogEntry`,
  `create_filament(body) -> CatalogFilament`, `update_filament(id, body) -> CatalogFilament`.
  `models.Spool` gains only `note`, `low_stock_threshold_pct`, `suppliers: list[SpoolSupplier]` (all optional, defaults); main already has `location_id`, `archived_at`, `k_profiles`, `last_dried_at`, `weight_locked`, `core_weight_catalog_id`. `SpoolKProfile` (today only `setting_id`) widens to the recorded `SpoolKProfileResponse` fields: `id`, `spool_id`, `printer_id`, `extruder`, `nozzle_diameter`, `nozzle_type`, `k_value`, `name`, `cali_idx`, `setting_id`, `created_at` (all optional, defaults, so rows parsed today still parse). New models `BbSupplier {id, name, website, customer_number, note, updated_at}`, `BbLocation {id, name, identifier, updated_at}`.
  Operation names `spools.create`, `spools.update`, `spools.archive`, `spools.restore`, `spools.k_profiles`, `spools.presets_put`, `spools.suppliers_put`, `suppliers.list|create|update`, `locations.list|create|update`, `catalog.create|update`, `filament_catalog.create|update`.

- [ ] **Step 1: Record.** `GET https://bambuddy.internal.nullreference.io/openapi.json` (no key; 401 → stop and report; any other version is no reason to stop) and record the `info.version` it gives (main already cites `v1.3.2b1-daily.20261113`, past 1.3.1). Write `sync-routes.json` in `rfid-routes.json`'s layout: `components.schemas` for `SpoolCreate`, `SpoolUpdate`, `SpoolResponse`, `SpoolKProfileBase`, `SpoolFilamentPresetBase`, `SpoolSupplierLinkInput`, `SupplierCreate`, `SupplierUpdate`, `SupplierResponse`, `LocationCreate`, `LocationUpdate`, `LocationResponse`, `CatalogEntryCreate`, `CatalogEntryUpdate`, `FilamentCreate`, `FilamentUpdate`, `FilamentResponse`; and `routes`: each route above with its method and its `Permission` and line in Bambuddy's source at the tag of the recorded `info.version` (`backend/app/api/routes/inventory.py`, `filament_catalog.py`), mapped to a ScadBuddy `Scope` as spec §3's table does. Record the version, the method, the date and the commands under a `measured_<YYYY-MM-DD>` key, as `rfid-routes.json` does (`measured_2026-11-17`).
- [ ] **Step 2: Failing tests** (respx, bodies validated with `recorded_schema`): `test_create_spool_sends_a_spool_create`, `test_update_spool_sends_only_the_given_fields`, `test_archive_and_restore_post_to_their_routes`, `test_put_k_profiles_sends_a_list`, `test_supplier_and_location_round_trip`, `test_a_403_names_manage_inventory`, `test_a_404_on_update_is_not_found` (the error carries `bambuddy_status == 404`), `test_a_k_profile_row_parses_its_printer_and_k` (a recorded spool row's `k_profiles[0]` keeps `printer_id`, `extruder`, `nozzle_diameter`, `k_value`), and a recording test that each schema exists and `SpoolUpdate` has no `last_scale_weight`.
- [ ] **Step 3:** Run; FAIL. **Step 4:** Implement the methods over `_send`. Run; PASS.
- [ ] **Step 5: Commit** `feat(bambuddy): spool, supplier, location and catalog writes (#6265)`.

### Task 3: Tables for links, hashes, intents and status

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_bb_sync.sql`, `backend/scadbuddy/filament/bb_sync_models.py`
- Test: `backend/tests/test_pg_migrations.py`, `backend/tests/filament/test_bb_sync_models.py`

**Interfaces:**
- SQL: `filament_bb_links` gains `pushed_fields jsonb NOT NULL DEFAULT '{}'`, `bb_seen jsonb`, `bb_seen_at timestamptz`, `bb_k_profiles jsonb`;
  the existing `filament_bb_links.pushed_hash` stays, unused: the migration's comment says `pushed_fields` supersedes it (never edit the merged `20261114T1301Z_filament_spools.sql`, #491; `filament/models.py` exposes it, so this PR does not drop it);
  `filament_suppliers` gains `bb_supplier_id integer UNIQUE`, `bb_pushed jsonb NOT NULL DEFAULT '{}'`, `bb_seen jsonb`, `bb_seen_at timestamptz`;
  `filament_locations` the same with `bb_location_id`; `filament_insert_types` gains `bb_pushed`, `bb_seen`, `bb_seen_at`;
  every `bb_seen_at` is the time `results` or the pull last wrote that row's `bb_seen`;
  `filament_spool_types` gains `bb_filament_id integer UNIQUE`, `bb_pushed`;
  `filament_bb_creates (kind text CHECK (kind IN ('spool','supplier','location','insert_type','filament')), sb_id uuid, started_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (kind, sb_id))`;
  `filament_bb_push_errors (kind text CHECK (… same kinds …), sb_id uuid, error text NOT NULL, attempts integer NOT NULL DEFAULT 1, at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (kind, sb_id))`;
  `filament_bb_sync_state (id boolean PRIMARY KEY DEFAULT true CHECK (id), instance text, last_pass_at timestamptz, last_ok_at timestamptz, last_error text, counts jsonb NOT NULL DEFAULT '{}')` seeded with one row whose `instance` is null (Task 8 fills it for links made before this row; Task 5 for the first new link).
- Produces in `bb_sync_models.py` (pydantic, `extra="forbid"`):
  `BbSnapshot {instance: str, full: bool, spools: list[dict], spools_complete: bool, suppliers: list[BbSupplier] | None, locations: list[BbLocation] | None, catalog: list[SpoolCatalogEntry] | None, filaments: list[CatalogFilament] | None, read_at: datetime}` (a `None` list means "not read", never "empty"; `spools_complete` is False when any spool row failed `bambuddy_pull.parse_rows`; consumers parse `spools` with `parse_rows`);
  `seen_row(row: Spool) -> dict[str, Any]`: the one shape of every spool `bb_seen`: BB's row restricted to Decision 14's fields, keyed by BB names (`color_name`, `rgba`, `extra_colors`, `label_weight`, `weight_used`, `note`, `low_stock_threshold_pct`, `location_id`, `core_weight`, `last_dried_at`, `slicer_filament`, …), with `rgba`/`extra_colors` in Decision 14's canonical form. `bambuddy_pull.snapshot` stays for the `synced_from_bb` event only;
  `BbWrite {key: str, kind: Kind, sb_id: UUID, op: Literal["create","update","archive","restore","k_profiles","presets","suppliers","probe"], bb_id: int | None, body: dict | list, hashes: dict[str, str]}` (`probe` is Decision 13's `GET spool_row(bb_id)`, with an empty body);
  `BbWriteResult {key: str, ok: bool, bb_id: int | None = None, answer: dict | None = None, error: str | None = None, status: int | None = None}`;
  `SyncPlan {writes: list[BbWrite], deferred: int, instance_changed: bool = False, directions: FilamentSync, interval_s: float}`;
  `PlanRequest {snapshot: BbSnapshot, limit: int = Field(ge=1, le=200)}` (the plan route's body; `full` stays on the snapshot);
  `SyncPassResult {written: int, failed: int, deferred: int, more: bool, instance_changed: bool, interval_s: float, skipped: Literal["no_bambuddy","nothing_pushes"] | None = None, error: str | None = None}` (`error` is an `error_label`; `record_pass` sets `last_error` from it and `last_ok_at` only when it is None, Decision 19);
  `SyncStatus {instance: str | None, last_pass_at, last_ok_at, last_error, counts: dict[str, int], errors: list[SyncError], directions: FilamentSync}`; `SyncError {kind, sb_id, error, attempts, at}`.

- [ ] **Step 1: Failing tests:** the migrations test applies the file; `test_one_sync_state_row` (its `instance` is null); `test_a_none_list_is_not_read` (model round-trip keeps `None` apart from `[]`); `test_seen_row_keeps_only_decision_14_fields` (a recorded spool row → BB-named keys only, `rgba` canonical); `test_a_pass_result_carries_its_error`.
- [ ] **Step 2-4:** Run, implement, run.
- [ ] **Step 5: Commit** `feat(filament): tables for the Bambuddy sync (#6265)`.

### Task 4: The plan: desired values, hashes and "newer wins"

**Files:**
- Create: `backend/scadbuddy/filament/bb_sync_plan.py`
- Test: `backend/tests/filament/test_bb_sync_plan.py`

**Interfaces:**
- Consumes: Task 1 `pulls`, `pushes`; Task 3 models and `seen_row`; `bambuddy_pull.parse_rows`; `colour.normalise_rgba`; `filament.types.effective` (#6261); Task 2's widened `SpoolKProfile`.
- Produces:
  - `field_hash(value: Any) -> str`: SHA-256 of `json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)`, first 16 hex. Hashes and compares take `rgba`/`extra_colors` in the canonical form (both sides through `normalise_rgba`); bodies carry BB's form (`RRGGBBAA`, no `#`; `extra_colors` comma-joined) (Decision 14).
  - `desired_spool(conn: Conn, spool_id: UUID, refs: BbRefs, sync: FilamentSync) -> dict[str, Any]`: the BB field map of Decision 14 for the data types that push; `BbRefs` holds the SB→BB id maps for insert types, locations, suppliers and printers. K rows compare on `printer_id`, `extruder`, `nozzle_diameter`, `k_value` with `filament_bb_links.bb_k_profiles`.
  - `sb_changed_at(conn: Conn, spool_id: UUID, field: str) -> datetime` (Decision 8): the one "SB change time" rule; Task 9's pull imports it.
  - `is_printer_place(location) -> bool`: `printer_id IS NOT NULL` (an AMS unit, a tray, the external spool place, a plain printer place).
  - `decide(*, sb: Any, bb_now: Any, bb_seen: Any, sb_at: datetime, bb_at: datetime | None, direction: SyncDirection) -> bool` (push this field or not).
  - `plan(store: Filament, snap: BbSnapshot, sync: FilamentSync, *, interval_s: float, limit: int = 200) -> SyncPlan`.
- Order inside `plan`: supplier, location, insert type and filament-catalog writes first; then spool creates and updates whose references are all known (others count as `deferred`); then archive/restore; then the per-spool lists. Each write's `key` is `f"{kind}:{sb_id}:{op}"`. A create writes its `filament_bb_creates` intent in `plan`'s transaction (Decision 10). An intent older than `snap.read_at` with a matching unlinked BB row turns into a link via a `BbWrite(op="update")` that carries the found `bb_id`. An item with an open intent younger than `snap.read_at` (a create whose reply was lost earlier in this pass) gets no write and counts `deferred` until a pass reads Bambuddy again (Decision 10).
- The compare value for a full pass: a row's `bb_seen` where its `bb_seen_at` is after `snap.read_at` (`results` updated it in an earlier round of this pass), else the snapshot's row. So round 2 never rewrites a field round 1 wrote.
- A linked spool missing from `snap.spools`: when `snap.spools_complete`, one `BbWrite(op="probe", bb_id=<link's id>)`; else no write. Printer places (`is_printer_place`) are never pushed as BB locations, and a spool in one gets no `location_id` key.

- [ ] **Step 1: Failing tests** (Postgres; SB rows made with the stores; `BbSnapshot` built from `recordings/inventory-spools.json` rows):
  - `test_field_hash_is_order_free`: `field_hash({"a":1,"b":2}) == field_hash({"b":2,"a":1})`, length 16.
  - `test_an_unlinked_spool_is_created_with_its_type_code`: one `create` with `body["material_number"] == type.code`, no `tag_uid`/`tray_uuid` keys, and a `filament_bb_creates` row.
  - `test_an_archived_unlinked_spool_is_not_created`.
  - `test_matching_hashes_skip_the_spool_unless_full`: `full=False` → no write; `full=True` with BB holding another `color_name` under `sb_to_bb` → one `update` with only `color_name`.
  - `test_two_way_skips_a_field_only_bambuddy_changed`: `bb_now != bb_seen`, `sb_at < bb_at` → `decide` is False.
  - `test_two_way_pushes_a_field_sb_changed_later`: `sb_at > bb_at` → True.
  - `test_a_tie_goes_to_bambuddy`: `sb_at == bb_at`, both changed → False.
  - `test_sb_to_bb_pushes_every_difference`.
  - `test_weight_used_is_label_minus_grams_left`: label 1000, grams left 812.34 → `weight_used == 187.7`; grams left 1010 → `0`.
  - `test_a_spool_waits_for_its_new_location`: a spool in an unlinked location gets a location `create`, and the spool's update is deferred (`deferred == 1`).
  - `test_k_values_of_an_unmapped_printer_are_left_out`.
  - `test_a_spool_in_a_printer_place_leaves_location_alone`: for a spool in an AMS unit, a tray, the external spool place and a plain printer place, no `location_id` key.
  - `test_a_printer_place_is_never_pushed_as_a_location`: under `pushes(locations)` no location write names a place with `printer_id`.
  - `test_colours_compare_in_one_form`: a translucent spool (#6654), SB `#AABBCC80`, BB `aabbcc80` → no write with `full=True`; a changed colour's body carries `"AABBCC80"` (no `#`).
  - `test_another_instance_plans_nothing`: state row `instance` differs → `writes == []`, `instance_changed`.
  - `test_a_spool_missing_from_the_listing_keeps_its_link`: `spools_complete=False`, a linked spool absent from `snap.spools` gets no write and its link stays.
  - `test_a_spool_missing_from_a_complete_listing_is_probed`: `spools_complete=True` → one `op == "probe"` write with the link's `bb_id`.
  - `test_a_young_intent_defers_the_spool`: an intent started after `snap.read_at` → no `create`, no adopt, `deferred == 1`.
  - `test_a_later_round_compares_with_the_answer`: `full=True`, the link's `bb_seen_at` after `snap.read_at` and its `bb_seen` equal to SB, the snapshot row still old → no write.
  - `test_the_limit_bounds_the_plan`: 3 unlinked spools, `limit=2` → 2 writes, `deferred == 1`.
  - `test_an_intent_adopts_the_spool_bambuddy_made`: an intent from before `read_at` and an unlinked BB row with the same snapshot and `material_number` → no `create`, one write with that `bb_id`.
  - `test_nothing_pushes_plans_nothing`: every direction `off` or `bb_to_sb` → empty plan.
- [ ] **Step 2-4:** Run, implement, run.
- [ ] **Step 5: Commit** `feat(filament): plan the writes to Bambuddy (#6265)`.

### Task 5: Results: links, hashes, echo and errors

**Files:**
- Create: `backend/scadbuddy/filament/bb_sync_results.py`
- Modify: `backend/scadbuddy/filament/spools.py` (`delete` also deletes the spool's `filament_bb_creates` and `filament_bb_push_errors` rows, kind `spool`; the rule "refused only while a link exists" is already main's code, `spools.py:787-790`, so it needs no change)
- Test: `backend/tests/filament/test_bb_sync_results.py`

**Interfaces:**
- Consumes: Task 3 (`seen_row`, `bb_seen_at`), Task 4 `BbWrite` keys, `rfid_push.enqueue_tag`, `bambuddy_pull.snapshot`, `ledger.record`.
- Produces: `apply_results(store: Filament, plan_writes: Sequence[BbWrite], results: Sequence[BbWriteResult], *, instance: str, now: datetime) -> ResultsReport` (`linked`, `updated`, `failed`, `unlinked` counts). The writes come back from the worker (a trusted service), and the plan is not stored, so `results` validates each write against the links: a result whose key is in no write is ignored and logged; a write's `sb_id` must name an existing SB item of its `kind`; a non-create write's `bb_id` must equal that item's link (`filament_bb_links` for a spool, the `bb_*_id` column for the others); a create's item must be unlinked with an open intent. A write that fails a check is ignored and logged.
- Per result: `ok` create → insert the link (or `bb_*_id` column), delete the intent, set `pushed_fields`/`bb_pushed` to the write's hashes, `bb_seen = seen_row(answer)` and `bb_seen_at = now`, `bb_updated_at` from `answer`, book Decision 9's event for a spool, `enqueue_tag` each open binding of the spool; `ok` update → merge hashes, `bb_seen`/`bb_seen_at`, the event; `ok` probe → keep the link, `bb_seen`/`bb_seen_at`, no event; not ok with `status == 404` on a linked item (a probe's included) → delete the link (Decision 13) and its error row; any other failure → upsert `filament_bb_push_errors` (`attempts + 1`), hashes untouched. Set `filament_bb_sync_state.instance` when it is null and this call linked something (Decision 12); a call that links nothing leaves it null.

- [ ] **Step 1: Failing tests:**
  - `test_a_created_spool_is_linked_with_its_hashes` (link row, `pushed_fields == write.hashes`, intent gone, one `synced_from_bb` event with `data.pushed`).
  - `test_a_new_link_queues_the_spools_tags` (`filament_bb_tag_pending` has its tag).
  - `test_a_pushed_value_is_not_pulled_back`: after `apply_results`, `bambuddy_pull.pull` with the answer row books no reading and no `changes`.
  - `test_a_404_drops_the_link` (an update's 404 and a probe's 404); `test_a_probe_that_finds_the_spool_keeps_the_link`.
  - `test_a_spool_whose_link_was_dropped_can_be_deleted`: a regression test; it passes on main before this task (main's `delete` already has the rule), so it is not a failing-first test.
  - `test_deleting_a_spool_removes_its_intent_and_error_rows`.
  - `test_a_failure_keeps_the_hashes_and_counts_attempts` (two failures → `attempts == 2`; a later success deletes the row).
  - `test_an_unknown_key_is_ignored`; `test_a_write_whose_bb_id_disagrees_with_the_link_is_ignored`; `test_a_create_for_a_linked_item_is_ignored`.
  - `test_the_first_link_records_the_instance`; `test_a_result_that_links_nothing_records_no_instance`.
- [ ] **Step 2-4:** Run, implement, run.
- [ ] **Step 5: Commit** `feat(filament): record what Bambuddy answered (#6265)`.

### Task 6: CRUD routes for the print worker

**Files:**
- Modify: `backend/scadbuddy/api/crud_print.py`, `backend/scadbuddy/api/crud_areas.py` (print area `filament-sync`), `backend/scadbuddy/crud/print.py`, `backend/scadbuddy/worker.py` (`PRINT_CRUD_AREAS`)
- Test: `backend/tests/crud/test_crud_filament_sync.py`

**Interfaces:**
- Routes on `crud_router("print")`, each with `summary=` and `description=`:
  `POST /api/v1/internal/crud/print/filament/sync/plan` body `PlanRequest {snapshot, limit}` → `SyncPlan` (reads `filament_sync` and `spool_overlay_refresh_seconds` itself; passes `limit` to `plan`, so a pass's rounds together stay within Decision 11's 200);
  `POST /api/v1/internal/crud/print/filament/sync/results` body `{instance: str, writes: list[BbWrite], results: list[BbWriteResult]}` → `ResultsReport` (`apply_results` validates every write against the links, Task 5);
  `POST /api/v1/internal/crud/print/filament/sync/pass` body `SyncPassResult` → 204 (writes the state row; `last_ok_at` only when `error` is None).
- Produces: `FilamentSyncApi` protocol (`plan(snap, limit) -> SyncPlan`, `results(instance, writes, results) -> ResultsReport`, `record_pass(result) -> None`, all async); `RemoteFilamentSync(crud: CrudClient)` in `crud/print.py`; `LocalFilamentSync(filament: Filament, settings_store)` in `filament/bb_sync_results.py` for the API's in-process print queue.

- [ ] **Step 1: Failing tests:** `test_plan_needs_the_print_credential` (no credential → 401; the render caller's → 403); `test_plan_route_returns_the_plan`; `test_plan_route_honours_the_limit` (3 unlinked spools, `limit=1` → 1 write); `test_results_route_links`; `test_a_failed_pass_keeps_last_ok_at`; `test_the_print_areas_list_filament_sync`; `test_remote_and_local_give_the_same_plan` (one fixture, both implementations).
- [ ] **Step 2-4:** Run, implement, run.
- [ ] **Step 5: Commit** `feat(filament): CRUD routes for the Bambuddy sync (#6265)`.

### Task 7: The pass on the worker

**Files:**
- Create: `backend/scadbuddy/filament/bb_sync_run.py`
- Modify: `backend/scadbuddy/worker.py` (`build_print_deps` builds `RemoteFilamentSync`; the activity joins the print worker's list), `backend/scadbuddy/main.py` (`_run_print_worker` adds the activity with `LocalFilamentSync`)
- Test: `backend/tests/filament/test_bb_sync_run.py`

**Interfaces:**
- Consumes: Task 2 client methods, Task 6 `FilamentSyncApi`, `client_for`.
- Produces: `run_pass(api: FilamentSyncApi, load: Callable[[], Awaitable[StoredSettings]], *, instance: str, full: bool, heartbeat: Callable[[], None], max_rounds: int = 4) -> SyncPassResult`, and `FilamentSyncActivities(api, load).sync_pass` registered as activity `filament_bb_sync_pass` taking `SyncPassInput {instance: str, full: bool}`.
- Behaviour: no `bambuddy_url` → `skipped="no_bambuddy"`; `instance_for(url) != instance` → `instance_changed=True`, no call. Else read BB once (`spool_rows(include_archived=True)`, `suppliers()`, `locations()`, `spool_catalog()`, `filament_catalog()`; each read that fails becomes `None` in the snapshot, and the spool read failing ends the pass as failed with `SyncPassResult.error = error_label(exc)`; `spools_complete` is False when `parse_rows` leaves any row unparsed), then rounds of `plan(snap, limit=200 − written)` → each write in order → `results`, stopping when a plan has no writes, after `max_rounds`, or after 200 writes (`more=True` when the last plan deferred anything). A `probe` write is `spool_row(bb_id)`: its row is the `answer`, a 404 is a not-ok result with `status == 404`; it counts toward the 200 like a write. One write that fails becomes a not-ok result with `error_label(exc)` and `status`; the rest go on. Heartbeat after each write. `record_pass` at the end, success or not, with `error` set on a failed pass.

- [ ] **Step 1: Failing tests** (respx with recorded bodies; a fake `FilamentSyncApi`):
  - `test_no_bambuddy_skips`; `test_another_instance_calls_nothing` (respx asserts zero calls).
  - `test_a_failed_write_does_not_stop_the_others` (the second of three answers 500 → results ok, not-ok `error == "…:500"`, ok).
  - `test_a_second_round_uses_the_new_location_id` (round 1 creates a location; round 2's spool body has its id).
  - `test_a_failed_supplier_read_is_none_not_empty`.
  - `test_the_write_cap_sets_more`.
  - `test_each_round_asks_for_what_is_left_of_the_cap` (round 1 writes 150 → round 2's `plan` gets `limit == 50`).
  - `test_a_failed_spool_read_records_an_error` (`record_pass` gets `error` set; no `plan` call).
  - `test_an_unparsed_row_marks_the_listing_partial` (`spools_complete is False`).
  - `test_a_probe_404_comes_back_as_not_found` (respx `GET /inventory/spools/{id}` 404 → not-ok, `status == 404`).
  - `test_every_write_heartbeats`.
- [ ] **Step 2-4:** Run, implement, run.
- [ ] **Step 5: Commit** `feat(filament): a sync pass on the print worker (#6265)`.

### Task 8: The workflow, the nudge and the boot start

**Files:**
- Create: `backend/scadbuddy/workflows/filament_sync.py`, `backend/scadbuddy/filament/bb_sync_nudge.py`, `backend/tests/fixtures/filament_sync_histories/v1.json`
- Modify: `backend/scadbuddy/workflows/client.py` (`bambuddy_worker` registers `FilamentBbSync`), `backend/scadbuddy/main.py` (nudger on `AppState`; at boot `adopt_instance`, then a `full=True` nudge once the library worker connects), `backend/scadbuddy/filament/bb_sync_results.py` (`adopt_instance`), `backend/scadbuddy/api/filament.py`, `backend/scadbuddy/api/filament_purchasing.py`, `backend/scadbuddy/api/tray_spools.py` (router dependency), `backend/scadbuddy/api/settings.py` (`put_settings` nudges when `filament_sync` changed)
- Test: `backend/tests/workflows/test_filament_sync.py` (`requires_temporal`), `backend/tests/test_filament_sync_replay.py`, `backend/tests/filament/test_bb_sync_nudge.py`, `backend/tests/api/test_filament_sync_nudge.py`

**Interfaces:**
- Produces:
  - `instance_for(url: str) -> str` (`sha256(url.rstrip("/").lower())[:12]`) and `workflow_id(instance) -> str` (`"filament-bb-sync-" + instance`) in `bb_sync_nudge.py`.
  - `@workflow.defn class FilamentBbSync`: `run(start: SyncStart) -> None` where `SyncStart {instance: str, interval_s: float = 300}`; signal `changed(full: bool = False)`. Loop: wait for a signal or `interval_s` (timer → full pass); `interval_s == 0` means no timer (the setting's documented "0 turns off"): the loop waits for a signal only, and "Sync now" still runs a full pass; after a signal sleep `DEBOUNCE = 2 s` and fold further signals in; run `filament_bb_sync_pass` (start-to-close 15 min, heartbeat 60 s, 3 attempts, backoff 10 s × 2); take `interval_s` from the result; `more` → next pass at once; `instance_changed` or `skipped == "no_bambuddy"` → return; `workflow.info().is_continue_as_new_suggested()` or 200 passes → `continue_as_new` with the current interval, carrying a pending `full`.
  - `FilamentSyncNudger(client_getter, load_settings, task_queue).nudge(*, full: bool = False) -> None`: Decision 17, nothing without `bambuddy_url`.
  - `nudge_after_write` dependency: after a 2xx response to a non-GET request, `nudge()` as a background task; on the `api/filament.py`, `api/filament_purchasing.py` and `api/tray_spools.py` routers. `put_settings` calls `nudge()` when the saved `filament_sync` differs from the stored one. Capture, `sync_ams_weights` and the print worker's CRUD `filament` writes are not nudged (Decision 17).
  - `adopt_instance(store: Filament, instance: str) -> None` in `bb_sync_results.py`: sets `filament_bb_sync_state.instance` when it is null and `filament_bb_links` has a row (links from #6261's pull). `main.py` calls it with `instance_for(bambuddy_url)` at boot, before the first nudge and so before any write (Decision 12).

- [ ] **Step 1: Failing tests:**
  - `test_a_signal_runs_one_debounced_pass` (three signals within 2 s → one activity call, `full=False`).
  - `test_the_timer_runs_a_full_pass`.
  - `test_interval_zero_runs_no_timer` (`interval_s=0`: no pass without a signal; a signal still runs one).
  - `test_more_runs_the_next_pass_at_once`.
  - `test_a_workflow_for_an_old_instance_ends`.
  - `test_continue_as_new_keeps_a_pending_full`.
  - `test_the_history_replays` (`Replayer` over `filament_sync_histories/v1.json`, recorded from the workflow test above once it passes; never re-record it later).
  - `test_nudge_signals_with_start` (fake client: `start_workflow` with `start_signal="changed"`, `id == workflow_id(instance_for(url))`, task queue `bambuddy`); `test_nudge_without_bambuddy_does_nothing`; `test_a_nudge_failure_is_logged_not_raised`.
  - `test_a_filament_write_nudges` (PUT a spool → one nudge) and `test_a_read_or_a_failed_write_does_not_nudge` (GET; a 422).
  - `test_a_tray_spool_write_nudges` (a `/printers` tray-spool PUT → one nudge).
  - `test_saving_filament_sync_nudges` and `test_saving_another_setting_does_not_nudge`.
  - `test_boot_records_the_instance_of_existing_links` and `test_boot_leaves_the_instance_null_without_links` (`adopt_instance`).
- [ ] **Step 2-4:** Run, implement, run. Record the history with the test helper the other `*_replay.py` tests use.
- [ ] **Step 5: Commit** `feat(filament): one sync workflow per Bambuddy, signalled by SB's writes (#6265)`.

### Task 9: The pull, widened

**Files:**
- Modify: `backend/scadbuddy/filament/bambuddy_pull.py`, `backend/scadbuddy/filament/suppliers.py` and `filament/locations.py` (link-aware upserts used by the pull)
- Test: `backend/tests/filament/test_bambuddy_pull.py`, `backend/tests/filament/test_bambuddy_pull_refs.py`

**Interfaces:**
- Consumes: Task 1 gates, Task 3 columns and `seen_row`, Task 2 `suppliers()`, `locations()` and the widened `SpoolKProfile` (`printer_id`, `extruder`, `nozzle_diameter`, `k_value`); main's `Spool.archived_at`, `Spool.k_profiles`, `Spool.location_id`; Task 4 `sb_changed_at` and `is_printer_place`.
- Changes:
  - `read_and_pull` reads suppliers under `pulls(suppliers)` and locations under `pulls(locations)`, whatever `spools` is set to (Decision 16); it takes them from the `FilamentSync` it already receives, never a second settings read.
  - Every "newer change wins" test in the pull (`_update`'s fields, archive, location moves) uses Task 4's `sb_changed_at`, in place of `_update`'s query that counts only `edited` events, for the fields Decision 8 lists. So a type edit or a ledger event counts the same on the push and the pull.
  - `read_and_pull` reads `spool_rows(include_archived=True)`. A linked BB spool whose `archived_at` is set archives the SB spool, and a cleared one restores it, under `pulls(spools)` and Decision 8's rule against the SB spool's newest `archived`/`restored` event. An unlinked archived BB spool makes no SB spool.
  - Every pull writes `filament_bb_links.bb_seen = seen_row(row)` and `bb_seen_at = now` from the row it read (the same shape `results` writes).
  - `pulls(presets)`: BB `k_profiles` → SB spool K values for printers mapped by `bambuddy_printer_id`, by `_bb_presets`' rule (only SB rows that still equal `filament_bb_links.bb_k_profiles` are replaced; a person's rows stay).
  - `pulls(suppliers)`: each BB supplier → the SB supplier with its `bb_supplier_id`, else the unlinked one of the same upper case name (now linked), else a new one; a linked one takes BB's changed fields by Decision 8's rule against `updated_at`. Spool supplier links are not pulled (Decision 15).
  - `pulls(locations)`: each BB location → the SB location with its `bb_location_id`, else an unlinked root location that is not a printer place (`is_printer_place`) with the same upper case name, else a new root location of kind `Shelf`; a linked spool's BB `location_id` change moves the SB spool (`LocationStore.move_on`, actor `bambuddy`) when it changed since `bb_seen` and the spool's newest `moved` event is not later than BB's `updated_at`, and never out of a printer place (an AMS unit, a tray, the external spool place, a plain printer place).
  - A BB spool that an open `filament_bb_creates` intent can claim (Decision 10) is skipped and counted in `PullReport.claimed`.

- [ ] **Step 1: Failing tests:** `test_a_bb_archive_archives_the_sb_spool`, `test_an_sb_restore_after_the_bb_archive_wins`, `test_an_unlinked_archived_bb_spool_is_not_made`, `test_bb_k_profiles_become_sb_k_values`, `test_a_persons_k_value_is_kept`, `test_a_bb_supplier_links_by_name`, `test_a_bb_location_becomes_a_root_shelf`, `test_a_spool_in_a_printer_place_is_not_moved_by_bambuddy` (a tray and the external spool place), `test_suppliers_and_locations_pull_while_spools_is_off`, `test_a_bb_edit_older_than_an_sb_type_edit_is_not_pulled` (an inherited field: the type's `updated_at` after BB's `updated_at` keeps SB's value), `test_the_pull_writes_bb_seen_as_seen_row`, `test_the_pull_leaves_a_spool_an_intent_claims`, `test_push_then_pull_settles` (Task 4 plan + Task 5 results + this pull over one field changed on both sides: after two rounds both sides hold the newer value and a third round writes nothing), and `test_suppliers_off_reads_no_suppliers` (respx: no `GET /inventory/suppliers`).
- [ ] **Step 2-4:** Run, implement, run. Run the whole `tests/filament` folder: #6261-#6268's pull tests must still pass.
- [ ] **Step 5: Commit** `feat(filament): pull archive, K values, suppliers and locations from Bambuddy (#6265)`.

### Task 10: Public routes and agent tools

**Files:**
- Modify: `backend/scadbuddy/api/filament.py`, `agent/src/tools/filament.ts`, `agent/src/sessions/touched.ts`, `agent/src/tools/traySpools.ts` (description text)
- Test: `backend/tests/api/test_filament_sync.py`, `agent/test/filament.test.ts`, `agent/test/coverage.test.ts`

**Interfaces:**
- `GET /api/v1/filament/sync/status` → `SyncStatus` (summary "Bambuddy sync status"; description names the six directions and what `errors` holds).
- `POST /api/v1/filament/sync/run` → 202 `{"started": true}`, or 409 with no Bambuddy configured (summary "Sync with Bambuddy now"; description: a full pass on the print worker, which writes Bambuddy only for the data types set to push).
- Agent tools `filament_sync_status` (read) and `filament_sync_run` (write, in `touched.ts` beside `filament_rfid_reconcile`, approval as the RFID reconcile has it). Every tool description that says "`filament_sync.tags` `sb_to_bb`" now says "tag sync set to push (`sb_to_bb` or `two_way`)".

- [ ] **Step 1: Failing tests:** `test_status_lists_errors_and_directions`, `test_run_nudges_full`, `test_run_without_bambuddy_is_409`; agent: both tools call their routes (msw), `filament_sync_run` is in the touched set, the coverage test passes, and the durable-mode tool list includes both (the test agent/CLAUDE.md names for tools in both modes).
- [ ] **Step 2-4:** Run, implement, `pnpm gen:api`, run.
- [ ] **Step 5: Commit** `feat(filament): sync status and "sync now" routes and tools (#6265)`.

### Task 11: Settings UI

Load `frontend-design:frontend-design` first.

**Files:**
- Modify: `frontend/src/pages/settings/sections/filament.tsx` (`SyncRows`), `frontend/src/pages/settings/values.ts` (`syncParts` → a typed object of six directions, and its inverse), `frontend/src/api/filament.ts` (`getSyncStatus`, `runSync`), `frontend/src/mocks/features/settings.ts` (its `filament_sync` check today 422s `spools` ∉ {off, bb_to_sb}, `tags` ∉ {off, sb_to_bb}, `weights` ∉ {off, bb_to_sb}; it takes the four values on all six keys), `frontend/src/mocks/fixtures.ts` (the seeded `filament_sync` becomes the six-key default of Task 1)
- Create: `frontend/src/mocks/features/filament/sync.ts` (exports `handlers`; `mocks/features.ts` loads every feature by `import.meta.glob('./features/**/*.ts')`, so there is no `index.ts` to edit)
- Test: `frontend/src/pages/settings/sections/filament.test.tsx` (its existing three-key `filament_sync` cases become six-key), `frontend/e2e/filament-sync.spec.ts`

**Interfaces:** one `ChoiceSelect` per data type, labelled "Spools and types", "Weights", "Tags", "Suppliers and prices", "Locations", "Presets and K values", each offering Off, "ScadBuddy → Bambuddy", "Bambuddy → ScadBuddy", "Both ways (newer change wins)"; a help line with Decision 16's sentence; a status block (last pass, last success, the error list with each item's kind, a link to the spool, the error, attempts) and a "Sync now" button that calls `runSync` and re-reads the status. Mock ids are fixed strings, never `crypto.randomUUID`.

- [ ] **Step 1: Failing tests:** `saves every direction in one filament_sync value` (change Weights to Both ways; the PUT body's `filament_sync` equals the six keys with `weights: "two_way"`), `shows a failed spool with its error`, `sync now re-reads the status`, `the mock accepts two_way on every key` (no mock 422); e2e: open Settings → Filament, set Suppliers to "ScadBuddy → Bambuddy", save, reload, the choice is kept (the setting is remembered).
- [ ] **Step 2-4:** Run, implement, run `pnpm lint && pnpm typecheck`.
- [ ] **Step 5: Commit** `feat(filament): choose the Bambuddy sync per data type in Settings (#6265)`.

### Task 12: Docs, whole-branch check and PR

- [ ] **Step 1:** Spec: under §8.2 add "As built in #6265" (Decisions 1, 3-10, 12-16 in one paragraph each, citing this plan, and Decision 17's list of SB writes that do not nudge: capture and `sync_ams_weights` (`capture.py`) and the print worker's CRUD `filament` writes reach Bambuddy on the timer only, against §8.2's "SB events signal it"); in §12 replace the #6264 note's "#6265 widens it" with what was built; correct any line `grep -n "SB to BB is #6263\|read only\|nothing here writes" backend/scadbuddy/filament/*.py docs/superpowers/specs/2026-11-14-filament-management-design.md` finds false now (`bambuddy_pull.py`'s module docstring says "SB to BB is #6263"). `backend/CLAUDE.md`: the layout entry for `filament/bb_sync_*.py` and the workflow, and the print worker's CRUD areas; and its `rfid_push.py` sentences (line 743 on main) "`push_pending` drains it only with `filament_sync.tags` `sb_to_bb`" and "`FilamentSync.tags` (`off` by default)" become `pushes(tags)` (`sb_to_bb` or `two_way`) and `bb_to_sb` by default. `frontend/CLAUDE.md`: the Settings Filament section. `plugins/scadbuddy/skills/filament/SKILL.md`: any sentence that names `sb_to_bb` as the only push value.
- [ ] **Step 2:** File two follow-up issues (linked from the PR): "move the Bambuddy pull's reads to the print worker through a CRUD route" (Decision 5) and "reset the Bambuddy links after the Bambuddy URL changes" (Decision 12).
- [ ] **Step 3:** Run `uv run --frozen pytest tests/filament tests/bambuddy tests/crud/test_crud_filament_sync.py tests/api/test_filament_sync.py tests/api/test_filament_sync_nudge.py tests/workflows/test_filament_sync.py tests/test_filament_sync_replay.py tests/test_spool_refresh_replay.py tests/test_print_replay.py tests/test_pg_migrations.py tests/api/test_openapi.py tests/api/test_routes.py`, ruff, mypy; `cd agent && pnpm test && pnpm typecheck`; `cd frontend && pnpm lint && pnpm typecheck && pnpm test`.
- [ ] **Step 4:** Open the PR `feat(filament): configurable sync with Bambuddy (#6265)`, body `Fixes #6265`, Decisions 1, 2 and 18 in a "Deployment" section (a new workflow type on `bambuddy`: roll `scadbuddy-print` with `Recreate`, README "A release that needs `Recreate`"), the footer. Shepherd it.
- [ ] **Step 5:** Open an eh-homelab/clusters PR that sets `strategy: {type: Recreate}` on `scadbuddy-print` and removes its `rollingUpdate:` block. Link it from this PR's "Deployment" section. Merge it before the release's deploy PR. Once the release has rolled out, open a second clusters PR that restores `RollingUpdate`.

## Pre-flight rulings (2026-11-18)

The pre-flight scan (findings F1-F22, read-only on main after #6650) and the controller's
rulings. Each line: the ruling, then the parts of this plan it changed.

1. F1: `pulls(spools)` gates only the spool rows; suppliers and locations pull on their own setting, and usage rows book under `pulls(spools) or pulls(weights)` for linked spools. Tasks 1, 9.
2. F2: Task 11 edits `mocks/features/settings.ts`, `mocks/fixtures.ts` and the existing `filament.test.tsx` cases, and drops `mocks/features/index.ts` (the glob registers features). Task 11, File structure.
3. F3: Task 12 fixes `backend/CLAUDE.md`'s `tags` sentences ("`off` by default", "drains only with `sb_to_bb`"). Task 12.
4. F4: `SpoolKProfile` widens to the recorded `SpoolKProfileResponse` fields, and `Spool` gains only `note`, `low_stock_threshold_pct`, `suppliers`. Tasks 2, 4, 9.
5. F5: `bb_seen` is `seen_row(row)`: BB's row restricted to Decision 14's fields, BB names, canonical colours; `snapshot` stays for the event only; dict rows parse with `parse_rows`. Decision 9, Tasks 3, 4, 5, 9.
6. F6: the state row's `instance` starts null; the API's `adopt_instance` at boot records it for existing links before any write; `results` sets it only on a call that links something. Decision 12, Tasks 3, 5, 8.
7. F7: `SyncPassResult.error` carries a failed pass's `error_label`; `record_pass` sets `last_error`, and `last_ok_at` only when `error` is None. Decision 19, Tasks 3, 6, 7.
8. F8: `interval_s == 0` means no timer: signals and "Sync now" only. Task 8.
9. F9: an item with an open intent younger than `snap.read_at` is skipped and counted `deferred` until a pass reads Bambuddy again. Decision 10, Task 4.
10. F10: the plan route takes `PlanRequest {snapshot, limit}` and `run_pass` sends `200 − written`. Decision 11, Tasks 3, 6, 7.
11. F11: later rounds compare with `bb_seen` where `results` updated it after `snap.read_at` (`bb_seen_at`), else with the snapshot. Tasks 3, 4, 5, 9.
12. F12: Task 4's `sb_changed_at` is the one "SB change time" rule, and the pull uses it in place of `_update`'s `edited`-only query. Decision 8, Tasks 4, 9.
13. F13: `results` validates each write's `sb_id`/`bb_id` against the links (the worker is a trusted service); the claim "results never trusts a key it did not plan" is dropped. Tasks 5, 6.
14. F14: Task 2 records whatever `info.version` the live instance gives, cites the source at that tag, and stops only on a 401; the recording key is `measured_<date>`. Global Constraints, Task 2.
15. F15: `filament_bb_links.pushed_hash` stays, unused, and the new migration's comment says `pushed_fields` supersedes it. Task 3.
16. F16: a linked spool missing from a complete listing gets one `probe` (`spool_row(id)`); a 404 drops the link, any other answer keeps it; a partial listing still writes nothing. Decision 13, Review Focus 5, Tasks 3, 4, 5, 7.
17. F17: `rgba`/`extra_colors` hash and compare in one canonical form (`normalise_rgba` on both sides) and go to Bambuddy as `RRGGBBAA` without `#`; a translucent spool is tested. Decision 14, Tasks 3, 4.
18. F18: a printer place is a location with `printer_id IS NOT NULL`; it is never pushed as a BB location, never left by a BB move, and a spool in one keeps BB's `location_id` alone. Decision 14, Tasks 4, 9.
19. F19: Task 5 drops the `spools.py` rule change (main has it), keeps its test as a regression test, and makes `delete` remove the spool's intent and error rows. Task 5, File structure.
20. F20: the release rolls `scadbuddy-print` with `Recreate` through a clusters PR merged before the deploy PR, and a second one restores `RollingUpdate`. Decisions 2, 18, Task 12.
21. F21: the nudge also covers `api/tray_spools.py` and a settings save that changes `filament_sync`; capture, AMS weights and the worker's CRUD writes stay on the timer, and Decision 17 and the spec's "As built" say so. Decision 17, Tasks 8, 12, File structure.
22. F22: the settings tests go in `tests/api/test_settings.py`. Task 1.

## Decisions made while building (#6265)

(Filled in by the controller during the build: each ruling with what it costs if wrong.)

1. Task 1: the pull takes a `bambuddy_pull.Pulls` (four booleans: `spools`, `weights`,
   `tags`, `presets`, built by `Pulls.of(sync)`) in place of #6268's `weights: bool`. Why:
   four flags through `_one`, `_update` and `read_and_pull` read better as one value, and
   `pull`'s first positional argument is already named `spools`. Cost: #6268's tests call
   `pull(..., pulls=Pulls(weights=False))`; their assertions are unchanged.
2. Task 1: `scheduled_pull` skips when none of the six data types pulls, `suppliers` and
   `locations` included. Why: F1 says "skipped only when no data type pulls", and Task 9 adds
   their pull to this same pass. Cost: until Task 9, `suppliers` or `locations` alone at
   `bb_to_sb` reads BB's spools and changes nothing.
3. Task 1: BB's row is recorded as seen (the `synced_from_bb` event's `bb`) whatever the
   gates say, so a field BB changed while its data type did not pull is pulled only when BB
   changes it again. Why: recording a partial snapshot would make every later pull see a
   change and write an event each time. Cost: switching `spools` back to `bb_to_sb` does not
   catch up on BB edits made while it was off; under `sb_to_bb` the push (Task 4) makes BB
   match SB anyway.
4. Task 1: with `pulls.spools` False the catalog (insert types) is neither read nor mapped,
   and an unlinked BB spool is left alone (no new spool, no link by tag, no
   `filament_bb_ignored` row). Why: Decision 14 puts insert types under `spools`, and
   Decision 16 says no spool is created or linked. Cost: none known.
5. Task 1: `POST /filament/bambuddy/import` pulls the spools whatever `filament_sync.spools`
   says; weights, tags and presets follow the setting. Why: the import is a person's explicit
   request, and before #6265 it ignored `spools` too. Cost: an import under `spools`
   `sb_to_bb` takes BB's newer field values into SB.
6. Task 1: `last_dried_at` stays booked by `_book` whatever `weights` says (as #6268 left it),
   and `weight_locked` stays a `spools` field on the pull. Why: the brief does not move them,
   and moving them changes #6268's tested behaviour. Cost: Decision 14 lists both under
   `weights`; Task 9 may move them.
7. Task 1: the environment's `SCADBUDDY_FILAMENT_SYNC` is not rewritten (only the stored row
   is). Why: a migration cannot reach the environment. Cost: an install that sets `tags`
   `off` in the environment and has no stored row now binds no BB tag on a linked spool.
8. Task 1: in the frontend only `mocks/fixtures.ts` (the six keys) and two pre-save
   expectations in `filament.test.tsx` change, so `pnpm typecheck` and that file pass. Why:
   the Settings UI is Task 11. Cost: until Task 11, the Filament section still sends three
   keys and maps every `tags` value but `sb_to_bb` to `off`, so a save there writes `tags`
   `off` (now: no tag pulled) and puts `presets`, `suppliers` and `locations` back to their
   defaults. Task 11 must land in the same release.
9. Task 1 (controller ruling, review I1): the Task 1 migration also rewrites a stored
   `filament_sync` row whose `spools` is `off`: `weights` and `presets` become `off`, and
   `tags` stays `sb_to_bb` when it was, else becomes `off`. The `tags` rewrite (`off` to
   `bb_to_sb`, `sb_to_bb` to `two_way`) applies only to rows whose `spools` is not `off`.
   Why: before #6265 a `spools` `off` skipped the whole pull, and the tag push ran only on
   `tags` `sb_to_bb`; Decision 4 says an upgrade changes no behaviour. Cost: a row stored
   with `spools` `off` and the never-valid `tags` `two_way` would take `tags` `off`; #6264
   wrote no such value.
10. Task 1 (controller ruling, review M1): `frontend/src/mocks/fixtures.ts`'s default `tags`
    is `bb_to_sb` now, as the API's default is. Why: a mock that sends a value the backend
    never defaults to tests a state no install has. Cost: none known; the two pre-save
    expectations in `filament.test.tsx` read it.
11. Task 1 (controller ruling, concern 3; replaces item 3): the pull records BB's values as
    seen only for the data types that pull (`bambuddy_pull._seen`); a field of a type that
    does not pull keeps its last recorded value. A linked row BB has not moved is read in
    full when one of its pulled fields differs from the last recorded reading. Why: an
    owner who switches a type on expects BB's current values, not only the ones BB changes
    after the switch. Cost: the first pull after a switch reads such a row in full once.
    The presets already worked this way: the link keeps BB's last `slicer_filament` only
    when presets pull.
12. Task 1 (controller ruling, concern 4): a BB row counts as `updated` in the PullReport
    only when a field of the SB spool changed (a column, its type or insert type, a retag,
    a `bb` reading), a weighing or drying was booked, or a preset changed. Why: a row BB
    moved with no change SB took is no update, and a `synced_from_bb` event with no
    changes said one happened. Cost: a moved row whose BB values SB's later edits all
    outrank records its reading but is not counted.
13. Task 1 (controller ruling, concern 5; replaces item 6): `last_dried_at` (the `dried`
    event) and `weight_locked` are `weights` data, as Decision 14 lists them. With `weights`
    not pulling, `_book` books no drying (on a new spool too), and a linked spool takes no
    lock change from BB; with `weights` pulling they move whatever `spools` says. A new
    spool still takes BB's lock with its first reading. Why: Decision 14. Cost: #6268's
    `weights` `off` now also stops the `dried` events, which it left booked.
14. Task 1 (review fix 1, I-1): a link records BB's values as seen only for the types that pull,
    as an update does (`_seen`). Why: a spool linked while `weights` was off must take BB's
    weight and lock when `weights` is switched on, even when BB's row has not moved. Cost: the
    first pull after the switch reads that row in full once.

## Self-review

- Spec coverage: §8 "one setting per data type" → Task 1; §8.1 rows: types on spools with
  `material_number`, filament-catalog → Task 4; insert types → Task 4 (push) and #6261
  (pull); instance 1:1 links → Tasks 3, 5; presets and K → Tasks 4, 9; suppliers and prices →
  Tasks 4, 9 (Decision 15 for what is not pulled); locations → Tasks 4, 9; tags → Decision 6
  and Task 1; ledger fields → Task 4 (`weights`); invoice → `cost_per_kg` in `spools`;
  matching an unlinked BB spool → #6261's pull, unchanged; sku-settings → Decision 15.
  §8.2 workflow per instance and signals → Task 8; idempotent by hash → Tasks 4, 5; newer
  wins → Tasks 4, 9; consumption counted once → the pull, unchanged, and Decision 9 for
  pushed weights; worker with CRUD routes → Tasks 6, 7. §10 Settings section → Task 11.
- Types: `BbWrite`, `BbWriteResult`, `SyncPlan`, `SyncPassResult`, `FilamentSyncApi`,
  `instance_for`, `workflow_id`, `pulls`, `pushes` are named the same in every task.
- The Review Focus lines each have a named test in their owning task.
