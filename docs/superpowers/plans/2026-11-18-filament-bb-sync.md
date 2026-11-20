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
  `filament_spool_types` gains `bb_filament_id integer UNIQUE`, `bb_pushed` (and, from Task 5, `bb_seen`, `bb_seen_at`: decision 76);
  `filament_bb_creates (kind text CHECK (kind IN ('spool','supplier','location','insert_type','filament')), sb_id uuid, started_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (kind, sb_id))` (and, from Task 5, `sent jsonb`: decision 85);
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

**Rulings carried from Task 4 (#6265)** (build decisions 59-61):
- R-T4-10: a non-create write on an unlinked item is valid only when the item has an open
  intent and no other SB row links that `bb_id`. On `ok` it links the item, as a create
  does, and closes the intent.
- R-T4-11: store `bb_k_profiles` whenever `bb_seen` is stored, from the same full answer
  (`[]` when the answer has none; a GET when the answer is not a full row, R-T3-6). Store
  `bb_seen` for suppliers, locations and insert types as `json_form` of their readable
  fields.
- R-T4-12: check the recordings for how Bambuddy stores `last_dried_at` (microseconds),
  `quoted_price_per_kg` (rounding) and an empty `note`, `subtype` and `color_name` (`""` or
  null), so that `seen_value` takes Bambuddy's form for each and no field flaps.
- From R-T4-1's build (decision 50): the error upsert sets `at = now()` on every failure,
  and keeps the intent of a refused create. The plan reads an error row newer than
  `snap.read_at` as "refused in this pass".
- R-T4-16 is replaced by R-T4-22 (decision 71): `plan` seeds a missing `bb_seen` in Task 4.
  Task 5 has nothing to build for it.
- R-T4-25 (decision 76): filament catalog entries get `bb_seen jsonb` and
  `bb_seen_at timestamptz` in this branch's unmerged migration
  (`20261118T2112Z_filament_bb_sync.sql`; #491 allows an edit while it is unmerged).
  `results` stores their seen copy as it does for the other kinds (R-T4-11, merged by
  R-T4-28), and Task 4's seeding (`_SEEN_TABLES`, `rows`) gains `filament`, marks
  included (R-T4-27). Else `two_way` freezes them (decision 53's cost).
- R-T4-27 (decision 74): `bb_pushed` of a supplier, location, insert type (and filament
  entry, after R-T4-25) holds the reserved key `unseen` (`bb_sync_plan.UNSEEN_KEY`) beside
  the field hashes. `results` merges a write's hashes into `bb_pushed` key by key (`||`),
  keeps `unseen` as it is, and never reads it as a field.
- R-T4-28 (decision 75): stored answers are merged, not swapped. When `results` stores a
  write's answer, it merges it into `bb_seen` field by field: a field goes in only when
  this write sent it, or when its answer value equals SB's compare value. Every other
  field keeps its old seen value, or stays out when it was unseen, and its mark in
  `bb_pushed["unseen"]` stays. The same holds for a spool link's `bb_seen` (the answer of
  a write that did not send a field does not make that field seen). `bb_seen_at` moves to
  the answer's time. Required test (the re-review's scenario 1):
  `test_a_parent_rename_never_pushes_the_childs_identifier`: a location under `two_way`,
  SB identifier `A1`, Bambuddy identifier `B7`, seeded by a full pass. The parent is
  renamed in SB, so the plan pushes only the child's name. `apply_results` stores the
  answer (`identifier` `B7`). The child's `bb_seen` has no `identifier`, and the next
  full pass (and every later one) plans no write that carries `identifier`.
- R-T4-32 (decision 82): an adoption by name of a row a person made has a body without
  SB's differing fields, and its hashes leave them out; the plan has already stored their
  marks in `bb_pushed["unseen"]` of the unlinked SB row (the key is there even when empty).
  `results` links the row (its intent says it is an adoption), merges the write's hashes
  with `||` (keeping `unseen`), and stores the answer under R-T4-28 with no old copy: a
  differing field stays out of `bb_seen`. Test: an adoption of BB location `B7` by SB
  location `A1` under `two_way`, then `apply_results`; the next full pass plans no write
  that carries `identifier`.

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
  - **Carried from Task 4: R-T4-33 (decision 83).** `SyncPassResult.conflicts` is the largest `SyncPlan.conflicts` of the pass's rounds (every round plans from one snapshot, so a sum would count a field once per round). Test: `test_conflicts_are_counted_once_per_pass` (two rounds each with `conflicts == 1` → the result has `conflicts == 1`).
- [ ] **Step 2-4:** Run, implement, run.
- [ ] **Step 5: Commit** `feat(filament): a sync pass on the print worker (#6265)`.

**Carried from Task 4 (#6265):** `more` reads `SyncPlan.deferred` only. It counts the items
that wait for a write planned earlier in the pass, and the items whose non-retry write the
cap cut (decision 64); a retry the cap cut is not counted, so retries alone never set `more`.
`SyncPlan.failed` and `SyncPlan.blocked` never set it (decision 50). Add
`SyncPlan.unmapped_k` to the pass counts (decision 58).
R-T4-21 (decision 70): Task 7 runs at most 3 passes back to back on `more`. It then waits
for the normal interval, whatever `deferred` says, and logs once that it hit the limit.
Test: a fake `FilamentSyncApi` whose `plan` always defers gives 3 passes, then the wait.
(Changed by decision 108: the activity runs one pass and passes `more` through; this bound
moves to Task 8.)
**Carried from Task 5: R-T5-2 (decision 93).** A list `PUT`'s result carries `answer =
{"k_profiles": rows}` or `{"suppliers": rows}` (Bambuddy's answered list; the preset
`PUT` needs none), so `results` stores that field with no `GET`. A probe's answer is the
`spool_row` it read. Test: `test_a_list_put_answer_names_its_field`.
**Carried from Task 6: R-T6-5 (decision 107).** Each plan round's `SyncPlan.pass_id` goes back in
`results(..., pass_id)` for that round's results, resends included.

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

**Carried from Task 7: R-T7-1 (decision 108).** The activity `filament_bb_sync_pass` runs one
pass and returns `more` unchanged. The workflow runs the next pass at once on `more`, at most
3 passes back to back (R-T4-21, decision 70); it then waits for the normal interval, whatever
`deferred` says, and logs once that it hit the limit. Test: an activity stub whose result
always has `more` gives 3 passes, then the wait. R-T7-3 (decision 110): the workflow passes
its `interval_s` in `SyncPassInput`.

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
  - Every pull writes `filament_bb_links.bb_seen = seen_row(row)` and `bb_seen_at = now` from the row it read (the same shape `results` writes), under R-T4-28's merge rule below.
  - **Carried from Task 4: R-T4-28 (decision 75).** A pull's `bb_seen` write is a merge, not a swap. A field the pull took into SB goes in (SB now holds it). A field the pull did not take goes in only when Bambuddy's value equals SB's compare value. Every other field keeps its old seen value, or stays out when it was unseen, and its mark in `bb_pushed["unseen"]` stays. This holds for spool links, suppliers and locations. Test: `test_a_pull_that_does_not_take_a_field_leaves_it_unseen` (a seeded location whose BB identifier differs; a pull that takes no identifier leaves `identifier` out of `bb_seen`, and the next plan pushes no identifier).
  - **Carried from Task 4: R-T4-33 (decision 83).** For suppliers and locations (and every row kind the pull takes), when SB and Bambuddy both changed the same field since `bb_seen` (both moved off the seen value; or, for a field the seen copy leaves out, both hashes moved off the mark in `bb_pushed["unseen"]`), the pull takes nothing for that field, whatever `updated_at` says. SB's row time cannot show when SB changed one field, so neither side's change is proven older. The field keeps SB's value, stays out of the merged `bb_seen` (R-T4-28), and is counted in `PullReport.conflicts`. Test: `test_a_field_both_sides_changed_is_not_pulled` (a location seen as `A1`, SB `C3`, Bambuddy `B7` with an `updated_at` after SB's: SB keeps `C3`, `conflicts == 1`).
  - `pulls(presets)`: BB `k_profiles` → SB spool K values for printers mapped by `bambuddy_printer_id`, by `_bb_presets`' rule (only SB rows that still equal `filament_bb_links.bb_k_profiles` are replaced; a person's rows stay).
  - `pulls(suppliers)`: each BB supplier → the SB supplier with its `bb_supplier_id`, else the unlinked one of the same upper case name (now linked), else a new one; a linked one takes BB's changed fields by Decision 8's rule against `updated_at`. Spool supplier links are not pulled (Decision 15).
  - `pulls(locations)`: each BB location → the SB location with its `bb_location_id`, else an unlinked root location that is not a printer place (`is_printer_place`) with the same upper case name, else a new root location of kind `Shelf`; a linked spool's BB `location_id` change moves the SB spool (`LocationStore.move_on`, actor `bambuddy`) when it changed since `bb_seen` and the spool's newest `moved` event is not later than BB's `updated_at`, and never out of a printer place (an AMS unit, a tray, the external spool place, a plain printer place).
  - A BB spool that an open `filament_bb_creates` intent can claim (Decision 10) is skipped and counted in `PullReport.claimed`.
  - **Carried from Task 5: R-T5-5 (decision 96).** The push's `synced_from_bb` event holds `snapshot(answer)` whole, so a person's Bambuddy edit to a field SB's write did not send is hidden from the event's `before`. The pull reads `filament_bb_links.bb_seen` (merged under R-T4-28, where such a field stays out or keeps its old value) for those fields, so it can take that edit by its own rule. Test: `test_a_persons_edit_the_push_did_not_send_is_pulled`.

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
15. Task 2: the live Bambuddy gave `info.version` `1.3.1` on 2026-11-18 (`GET /openapi.json`, no
    key), so `sync-routes.json` and the new client docstrings cite the source at tag `v1.3.1`
    (commit d3be761), not `v1.3.2b1-daily.20261113`. Why: F14. Cost: none known; the routes and
    lines are the ones that version serves.
16. Task 2: the filament catalog's writes (`create_filament`, `update_filament`) take a new
    `Scope.ADMIN_FILAMENTS` ("Filament catalog (admin only)"), not Manage Inventory, and a 403
    on one says that no API key can make it. Why: `FILAMENTS_CREATE`/`FILAMENTS_UPDATE` are
    absent from `_APIKEY_SCOPE_BY_PERMISSION` and listed in `_APIKEY_DENIED_PERMISSIONS`
    (`core/auth.py` lines 90 and 300-301, v1.3.1). Cost: the filament-catalog push works only
    while Bambuddy's auth is off (prod runs with it off); Task 4 must treat that 409 as a
    per-item error.
17. Task 2: `suppliers()` and `locations()` take Read Status, as `spool_row` does. Why: their
    routes need `INVENTORY_READ` (`inventory.py` lines 896, 700), which maps to
    `can_read_status`. Cost: none known.
18. Task 2: `create_catalog_entry` and `update_catalog_entry` keep `weight: float` and send
    `round(weight)`. Why: `CatalogEntryCreate`/`CatalogEntryUpdate.weight` is an integer in the
    recording. Cost: a fractional SB weight reaches BB rounded to whole grams.
19. Task 2: `test_a_k_profile_row_parses_its_printer_and_k` puts a K-profile built from the
    recorded `SpoolKProfileResponse` into a recorded spool row. Why: no live spool had a
    K-profile (0 of 106, `GET /api/v1/inventory/spools` on 2026-11-18). Cost: the field names
    are checked against the recording; the values are not measured.
20. Task 2: sent bodies are validated with `jsonschema` against the recorded schema
    (`conftest.assert_recorded_body`: types, required fields, nested refs), beyond `shaped`'s
    name check; the recording test is a new `test_recordings_sync.py`. Why: the brief asks for
    bodies validated against the recorded schema. Cost: none known.
21. Task 2: `SpoolSupplier` (a spool's nested supplier link) requires only `supplier_id`, as
    Bambuddy's `SpoolSupplierResponse` does with `id` and `supplier_name` beside it. Why: a link
    with no supplier names nothing; the other fields default so a sparse row parses. Cost: a
    link row with no `supplier_id` fails the whole spool's parse.
22. Task 3 (Task 2 review minor): the agent's `BambuddyScope` union (`agent/src/tools/registry.ts`)
    names `'Filament catalog (admin only)'`, the label of `Scope.ADMIN_FILAMENTS`. Why: the type
    says it lists the backend's `Scope` labels, and a 403 on a filament-catalog write carries that
    label. Cost: none known. Decision 29 added `'Manage Inventory'`, which was missing from
    the union on main too.
23. Task 3: `seen_row` keeps `archived_at` (the archive state), the `weights`, `presets` and
    `locations` fields and the spool's `suppliers` (each link restricted to what `PUT …/suppliers`
    sends, sorted by `supplier_id`), but not `k_profiles`. Why: Decision 14 lists them; the brief
    gives the K-profiles their own column, `filament_bb_links.bb_k_profiles`. Cost: a consumer that
    compares K values reads that column, not `bb_seen`.
24. Task 3: in `bb_seen`, `extra_colors` is a list of `#RRGGBBAA` (as `snapshot`'s
    `extra_colours`), and a value `normalise_rgba` refuses reads as `None` (as the pull reads it).
    Times are ISO text (`model_dump(mode="json")`). Why: F17's one canonical form for compare and
    hash, and `bb_seen` is jsonb. Cost: Task 4 sends `extra_colors` to Bambuddy comma-joined
    without `#`, so it converts at the edge.
25. Task 3: the new tables' `kind` and `sb_id` key columns carry no `NOT NULL`, as the brief's
    SQL gives them; a primary key makes them not null anyway. `Kind` and `Op` are module-level
    `Literal`s in `bb_sync_models.py`. Why: the brief's values verbatim. Cost: none known.

26. Task 3 (ruling R-T3-1): the colour helpers are shared. `_rgba` and `_colours` moved to
    `filament/colour.py` as public `canonical_rgba` and `canonical_colours`; `bambuddy_pull`
    and `bb_sync_models` import them. Why: F17 needs one canonical colour form, two copies can
    drift, and `colour.py` avoids the import cycle Task 9 would make. Cost: one small move.
27. Task 3 (R-T3-2): both sides compare in one JSON form. `bb_sync_models.seen_value(field,
    value)` writes SB's Decimal, datetime and int values as `seen_row` stores them (`19.99`,
    `"...Z"` text, `0.0`). Task 4 runs SB's desired values through it before every compare and
    hash. The archive state is compared as `archived_at is None` on both sides, never as
    timestamps. Why: otherwise every full pass rewrites the price and the drying time, and
    `two_way` always sees a difference. Cost: an extra helper.
28. Task 3 (R-T3-3): `test_seen_row_keeps_only_decision_14_fields` asserts a literal set of the
    25 Decision 14 names, not `SEEN_FIELDS`. Why: a test that compares the code with its own
    constant cannot catch a dropped field.
29. Task 3 (R-T3-4): `'Manage Inventory'` is added to the agent's `BambuddyScope` union beside
    the filament catalog label (this replaces the "left" note in item 22). Why: the gap is older
    than this PR, but the fix is one line in a line this PR already edits.
30. (changed by 89) Task 3 (R-T3-5): Task 5 cleans the `filament_bb_push_errors` and `filament_bb_creates` rows
    of every kind (spool, supplier, location, insert type) when the SB row they name is deleted
    or archived, not only kind `spool`. Why: otherwise a deleted supplier's error shows in
    `SyncStatus.errors` forever.
31. (changed by 87) Task 3 (R-T3-6): before Task 5 stores `seen_row(answer)` for `archive`, `restore`,
    `PUT .../suppliers` and `PUT .../k-profiles`, it checks in Task 2's recordings that each
    answer is a full `SpoolResponse`. Where an answer is not, Task 5 reads the spool with a
    GET (`GET` only) and stores that.
32. Task 3 (R-T3-7): `json_form(value)` in `bb_sync_models.py` is the one recursive JSON form of a
    `bb_seen` value: a Decimal is a float, a datetime is UTC ISO text ending in `Z` (a naive one
    counts as UTC), lists and dicts go item by item. `seen_row` and `seen_value` both use it,
    `rgba` and `extra_colors` also go through `canonical_rgba` and `canonical_colours`, and
    `seen_value("suppliers", ...)` keeps what `seen_row` keeps (the `PUT .../suppliers` fields,
    sorted by `supplier_id`). Why: the stored form and SB's form cannot differ (I-1, M-1, M-2 of
    the Task 3 re-review). Cost: a table test builds one BB row and its SB-side values.
33. Task 3 (R-T3-8): Decision 22's note says Decision 29 added `Manage Inventory`. Why: the old
    note said it was left out, which was false once item 29 landed.
34. Task 3 (R-T3-9): `json_form` also turns a UUID into text, a date (not a datetime) into ISO
    text and an Enum into its value, recursively. A whole number in a money or weight field
    (`cost_per_kg`, `label_weight`, `core_weight`, `weight_used`, a supplier link's
    `quoted_price_per_kg`; the constant `_FLOAT_FIELDS`) is a float on both sides.
    `seen_value("suppliers", None)` is `[]`, and a link that lacks a PUT key gets it as `None`,
    so it matches the full-key form `seen_row` stores (sorted by `supplier_id`). Why: M-1 to M-3
    of the Task 3 re-review 2. Cost: none; the table test now uses SB's real forms
    (`extra_colors` as upper-case `#RRGGBBAA` strings, an int price).

35. (changed by 50 and 68) Task 4: `desired_spool` gives a `Pending(kind, sb_id)` for a row Bambuddy has no id for
    (`BbRefs` holds the ids). The spool waits (`deferred`) only while that row's create is
    planned in this round, or it has an open intent and no `filament_bb_push_errors` row;
    otherwise the field is left out (the supplier list as a whole). Why: a reference that
    will not come (Bambuddy refused the row, or its listing was not read) must not defer
    the spool on every pass and keep `more` set. Cost: a spool may reach Bambuddy once
    without that field, and takes it on a later pass.
36. Task 4: a field SB has no value for that Bambuddy cannot take empty is left out:
    `label_weight` with no label weight, `core_weight` and `core_weight_catalog_id` with no
    insert type, `low_stock_threshold_pct` outside 1-99 (SB allows 0-100; `SpoolUpdate`
    1-99), `weight_used` with no label weight or no grams left. Why: the recorded schema.
    Cost: Bambuddy keeps its own value then; SB's 0 % and 100 % never reach it.
37. (changed by 52) Task 4: a create carries the fields of every data type that pushes, not only
    `spools`', except `last_dried_at` (`SpoolCreate` has none) and the three lists (they
    need the BB id). Why: a new BB spool starts with SB's weight and place, not full and
    nowhere. Cost: the drying time and the lists arrive one round later.
38. Task 4: a `PATCH` that sends `weight_used` always sends `weight_locked` too, with its
    hash. Why: Bambuddy locks the weight otherwise (`write_semantics.spool_update_partial`).
    Cost: none known.
39. (changed by 78 and 81) Task 4: the K-profiles compare in `k_form` (`printer_id`, `extruder`,
    `nozzle_diameter`, `k_value` to 4 decimals, sorted) with the snapshot row's
    `k_profiles`, or `bb_k_profiles` when `bb_seen_at` is after `read_at`; the last seen
    value under `two_way` is `bb_k_profiles`. The preset list (`PUT …/filament-presets`)
    compares by its hash only, in both kinds of pass: no BB spool row carries it. Why: F11
    and the recorded `SpoolResponse`. Cost: a preset list edited in Bambuddy is not
    repaired, and the first pass under `pushes(presets)` puts `[]` once for every linked
    spool with no preset.
40. (changed by 53, 55 and 77) Task 4: an archived SB spool gets its archive write only (phase "archive/restore"), no
    field or list write. `decide` with no BB `updated_at` (catalog and filament entries, or
    a row `results` stored this pass) lets SB's change win. Why: an archived spool is off
    the lists; there is no BB time to compare. Cost: a field edited on an archived spool
    reaches Bambuddy only after a restore.
41. (changed by 82 and 85) Task 4: suppliers, locations, insert types and filament catalog entries compare field
    by field with BB's listing (suppliers `name`, `website`, `customer_number`, `note`;
    locations `name`, `identifier`; catalog `name`, `weight`; filament `name`, `type`,
    `brand`, `color_hex` upper case, `density`; the rest of a `FilamentCreate` by hash).
    An unlinked SB row adopts BB's unlinked row of the same upper case name, intent or
    not, and records an intent when it has none; the write is an `update` with that
    `bb_id`. Spools adopt only through an old intent (Decision 10). Why: Bambuddy answers
    409 for a second supplier or location of one name, so a create would fail on every
    pass. Cost: a BB row of the same name that a person made becomes SB's. Task 5 must take
    an `update` on an unlinked item with an open intent as an adoption, for spools too.
42. (changed by 62) Task 4: a linked supplier, location, catalog or filament entry missing from BB's
    listing gets no write; only spools are probed (Decision 13). Why: the brief's probe is
    `spool_row`. Cost: such a row stays linked to a row Bambuddy deleted until a later
    task handles it.
43. Task 4: what is pushed of each kind: every unarchived insert type (`name`,
    `weight` = `empty_weight_g`); every unarchived place that is no printer place, named by
    its path; one filament catalog entry per unarchived type, named by its `code` (unique
    in SB), `type` its material, `color_hex` the first 7 of `rgba`, temperatures the type's
    own, `cost_per_kg` only when set. Why: Decision 14 names the rows, not their bodies.
    Cost: a filament entry's name is a product code, not a display name.
44. (changed by 50, 85 and 97) Task 4: a spool's adoption matches `bambuddy_pull.snapshot` (tags left out, SB sends
    none) of BB's row with that of a `BbSpool` built from the create body, and BB's raw
    `created_at` (which `BbSpool` drops) must be no earlier than the intent less 5 s when
    the row has one. An old intent while the listing is partial waits (`deferred`). The
    adoption's hashes are all desired fields'; its body only what differs (often `{}`).
    Why: Decision 10; the clocks agree within 0.25 s; the row to adopt may be the
    unparsed one. Cost: none known.
45. Task 4: `sb_changed_at`: a value is inherited when the spool's own column is null and
    it has a type, or when only the type gives it (`material_number`, presets, K values,
    suppliers); then the type's `updated_at` and the newest `edited` event naming
    `type_id` count too. `weight_used` takes the newest event with a reading or a delta;
    `suppliers` also the type's products' and price points' `updated_at`. A catalog row's
    rename and an insert type's weight edit do not count. Why: Decision 8 names edits,
    ledger events and the type. Cost: a brand renamed in SB under `two_way` loses to a BB
    edit of that field made after the spool's own last change.
46. Task 4: `slicer_filament` is the effective preset for the model of the printer whose
    place holds the spool (`upper(btrim(model))`), its first nozzle by text, else the first
    preset by model and nozzle. The supplier list has one link per supplier (its first
    priced product by SKU, else its first by SKU) with the newest `price_per_kg`
    (no currency conversion); the supplier of the spool's invoice is the purchase source,
    linked alone with no SKU and price when it sells no product of the type. Why: Bambuddy
    refuses a supplier twice in one `PUT`. Cost: a second product of one supplier is not
    shown in Bambuddy.
47. (changed by 58 and 69) Task 4: the limit cuts in the plan's order; an item any of whose writes is cut counts
    `deferred` once, and only a kept create (or adoption) writes its intent. A create made
    again over an old intent moves its `started_at` to now. The count of K values left out
    for an unmapped printer (Decision 14's `unmapped_k`) is not in `SyncPlan` (Task 3's
    model has no field). Why: the brief's limit test; the model is Task 3's. Cost: Task 7 or
    10 adds the count if the status should show it.
48. Task 4: an event-driven pass skips a field whose hash matches `pushed_fields` and
    compares the rest with BB's row through `decide`; a field BB's row does not carry
    compares by hash in both kinds of pass. `plan` is empty when none of `spools`,
    `weights`, `presets`, `suppliers`, `locations` pushes (`tags` is the drain's). Why:
    Decision 7, Decision 6. Cost: none known.
49. Task 4 (R-T3-11): `test_bb_sync_models.py` uses `bb_sync_plan.field_hash` in place of its
    test-local `_hash`. Why: one hash, tested where it is made. Cost: none.

50. Task 4 (R-T4-1, review C-1): `deferred` means only "this item waits for a write
    planned earlier in this same pass", which a later round of this pass can clear. Three
    cases are not `deferred`. A create that Bambuddy refused (it has a
    `filament_bb_push_errors` row) counts `failed`: an error row newer than `snap.read_at`
    (refused in this pass) gets no write for the rest of the pass, whatever the op, and a
    create refused in an earlier pass is tried once on a full pass only. A spool held by a
    partial listing counts `blocked`: an old intent, or a linked spool that is missing
    from the listing and has no fresh `bb_seen`. A spool whose reference has an open
    intent while this pass did not read that reference's listing counts `blocked`, and
    gets no write. `SyncPlan` gains `failed` and `blocked` (defaults 0). Neither sets
    `more`. A young intent with an error row from an earlier pass is a lost answer and
    stays `deferred`. Why: a deferral that never clears made the sync run passes back to
    back with no wait. Cost: a refused create waits for the next full pass (the timer or
    "Sync now"), not the next event pass.
51. Task 4 (R-T4-2, concern 8): every write of an item that holds a
    `filament_bb_push_errors` row from an earlier pass goes in a last phase, after the
    spools' lists. Why: failing catalog creates must not spend the cap ahead of spools.
    Cost: a supplier or location that failed once reaches Bambuddy after the spools that
    name it. A spool goes without the reference in round 1 (an item with an error row is
    not in flight), so it takes the id one round later when the pass has a round left,
    else one pass later (R-T4-23).
52. Task 4 (R-T4-3, review I-1): `linked_spool` checks for a fresh `bb_seen` (`bb_seen_at`
    after `snap.read_at`) before it probes. A spool written earlier in this pass is planned
    from that answer and never probed, so decision 37's "one round later" holds. Why: the
    probe of every new spool in every later round spent the cap. Cost: none known.
53. (changed by 65, which 71 replaces, and by 76 and 84) Task 4 (R-T4-4, review M-1): a link with `bb_seen` NULL (a legacy link) treats "last
    seen" as unknown (`bb_sync_plan.UNSEEN`, which equals no value). Under `two_way` the
    newer side wins by time: SB's change time against Bambuddy's row time. When Bambuddy's
    time is unknown, Bambuddy wins and nothing is pushed. The same holds for a supplier,
    location, insert type or filament entry with no `bb_seen`. Why: SB must never
    overwrite a value without knowing it is newer. Cost: under `two_way`, a linked catalog
    or filament entry with no `bb_seen` (Bambuddy gives those rows no time) takes no SB
    change until a write stores `bb_seen`. Decision 71 seeds `bb_seen` for spool links,
    suppliers, locations and insert types. Filament entries had no `bb_seen` column and kept
    this cost until Task 5, which gives them one and seeds them too (decision 76).
54. Task 4 (R-T4-5, review M-2): a fresh `bb_k_profiles` of NULL is unknown, not an empty
    list. No K write is planned for that spool until a full read gives the list; the
    `before` value of a NULL `bb_k_profiles` is `UNSEEN`. Why: F11. Cost: none known.
55. Task 4 (R-T4-6, review M-3): the archive state is read from the spool row whatever
    `spools` pushes. An archived SB spool gets no field or list write (decision 40). Why:
    the old check read it from the desired values, which hold it only under
    `pushes(spools)`. Cost: none known.
56. Task 4 (R-T4-7, reviews M-4 and M-5): new tests cover decision 44's `created_at`
    window (a decoy that differs only by time), decision 38's `weight_locked` partner and
    decision 35's refused reference. Both after-the-code tests were seen to fail with
    their rule removed. The link-count assertion that could not fail is replaced by
    `blocked == 1`. Why: an untested rule can break unseen. Cost: none.
57. (changed by 69 and 71) Task 4 (R-T4-8, review M-6): a renamed linked location, supplier or insert type whose
    new name an unlinked Bambuddy row already holds gets no `update`. The plan inserts one
    `filament_bb_push_errors` row with error `name_taken` (`bb_sync_plan.NAME_TAKEN`) and
    leaves an existing row as it is, so `attempts` does not grow and no 409s repeat. The
    item counts `failed`. This is the one write `plan` makes besides the create intents.
    Why: a 409 on every full pass. Cost: the item's other changed fields wait until the
    name no longer clashes; the Settings UI (Task 11) shows the label as "a Bambuddy row
    already has this name".
58. Task 4 (R-T4-9, concern 4): `SyncPlan` gains `unmapped_k: int = 0`, the number of K
    values that `desired_spool` left out because their printer has no
    `bambuddy_printer_id` (decision 14), summed over the spools that the round planned.
    `desired_and_unmapped` returns the values and the count; `desired_spool` keeps its
    signature. Task 7 adds the count to the pass counts. Why: decision 14 requires the
    count. Cost: none.
59. Task 4 (R-T4-10, concern 1; carried to Task 5): a non-create write on an unlinked item
    is valid only when the item has an open intent and no other SB row links that
    `bb_id`. On `ok` it links the item, as a create does, and closes the intent.
60. Task 4 (R-T4-11, concern 3; carried to Task 5): Task 5 stores `bb_k_profiles` whenever
    it stores `bb_seen`, from the same full answer (`[]` when the answer has none; a GET
    when the answer is not a full row, R-T3-6). It stores `bb_seen` for suppliers,
    locations and insert types as `json_form` of their readable fields.
61. Task 4 (R-T4-12, precision; carried to Task 5): Task 5 checks the recordings for how
    Bambuddy stores `last_dried_at` (microseconds), `quoted_price_per_kg` (rounding) and an
    empty `note`, `subtype` and `color_name` (`""` or null). `seen_value` then takes
    Bambuddy's form for each, so no field flaps.
62. (changed by 88) Task 4 (R-T4-13, concern 5): linked non-spool rows that Bambuddy deleted are follow-up
    #6688. Task 5 drops a link on any 404 on a linked item.
63. (changed by 78 and 81) Task 4 (R-T4-14, concerns 6 and 7): the one-time `PUT []` per linked spool with no
    preset is accepted. Tests written after the code are accepted, given decision 56. The
    commit title stays, because the squash title decides.
64. Task 4 (R-T4-15, fix round 2): `more` counts only non-retry work the cap cut. `take` does
    not add a retry (an item with a `filament_bb_push_errors` row from an earlier pass) to
    `deferred` when the cap cuts it. Retries alone never set `more`. Why: more than 200
    refused creates made every pass run the next at once. Cost (R-T4-23): phase 4 keeps a
    fixed order (kinds, then the upper-case name or the spool number). A retry the cap cut
    waits for a pass in which fewer than `limit` writes come before it. While the first
    `limit` retries keep failing, the retries after them get no request, and their error
    rows keep their old reasons.
65. (replaced by 71) Task 4 (R-T4-16; carried to Task 5): a linked row with a NULL `bb_seen` gets `bb_seen`
    seeded from the full snapshot's current Bambuddy value by the bookkeeping of `results`,
    with no Bambuddy write. SB changes then push by the hash rule. Why: catalog and filament
    rows have no Bambuddy time, and R-T4-4 would freeze them. Cost: none.
66. Task 4 (R-T4-17): the error row of `name_taken` is sync bookkeeping, like the create
    intents, not SB domain data. Task 11 labels it "a Bambuddy row already has this name".
67. Task 4 (R-T4-18): "refused in this pass, no more writes this pass" applies to every
    write type. Task 5 sets `filament_bb_push_errors.at = now()` on every failure (carried).
68. Task 4 (R-T4-19, re-review I-1): a spool whose reference (a place or an insert type) is
    archived and still has an open intent counts `blocked`, not `deferred`. `rows` records
    the archived items of each kind it reads. Archived suppliers never become a reference
    (`_supplier_links` leaves them out). Why: `rows` never plans an archived row, so the
    intent never closed and the spool was `deferred` on every pass, which kept `more` set.
    Cost: the spool gets no write in the pass that closes the intent (decision 69), and
    goes without that field from the next pass on (decision 35).
69. (changed by 76) Task 4 (R-T4-20, re-review I-1): `plan` closes an open intent (`filament_bb_creates`)
    in two cases. First, `rows` meets the intent's item archived, whatever the intent's
    age. Second, a full pass newer than the intent finds no Bambuddy row it made: for a
    supplier, location, insert type or filament catalog entry no unlinked row of the same
    upper-case name (changed by 76), for a
    spool no adoptable row in a complete listing. The item is then planned again as a fresh
    create when it is eligible, and a kept create writes a new intent. An event pass keeps
    the intent. The planner keeps a closed archived intent in memory for the rest of the
    plan, so decision 68 applies in that pass. Why: an intent that no write can answer must
    not hold a spool, and a stale `started_at` would widen the adoption window of decision
    44. Cost: a lost answer of an archived row is found again by name (decision 41) when
    the row is restored, not by its intent.
70. Task 4 (R-T4-21; carried to Task 7): Task 7 runs at most 3 passes back to back on
    `more`. It then waits for the normal interval, whatever `deferred` says, and logs once
    that it hit the limit. Why: decisions 68 and 69 close the known loops, and this bound
    closes the unknown ones. Cost: work left over after 3 passes waits one interval.
71. (changed by 74, 76, 77, 78, 83 and 101) Task 4 (R-T4-22, re-review I-2; replaces 65): on a full
    pass, `plan` seeds the missing
    seen copy of a linked row whose `bb_seen` is NULL: a spool link (`filament_bb_links`),
    a supplier, a location or an insert type. It sets `bb_seen` from the snapshot's current
    Bambuddy value and `bb_seen_at = snap.read_at`. This is sync bookkeeping, like the
    intents, and no Bambuddy write. The copy holds only the fields whose Bambuddy value is
    SB's value (and a link's `bb_k_profiles` only when the K-profiles agree). A field that
    differs stays out of the copy, so it is `UNSEEN`. A field that agrees at seeding is
    pushed by the seen rule after a later SB change. Under `two_way` the rule for an
    `UNSEEN` field depends on the kind:
    - Spools (each field has its own SB change time, `sb_changed_at`): decision 53's rule
      when Bambuddy's row has a time (SB's change strictly after it). With no time, it
      pushes only when SB changed that field after `bb_seen_at`
      (`sb_changed_at > bb_seen_at`; `decide` takes `seen_at`).
    - Suppliers, locations and insert types (one SB change time per row, `updated_at`,
      which any column or a parent's move sets): seeding records SB's and Bambuddy's
      hashes of the field in `bb_pushed["unseen"]` (decision 74). The field pushes only
      when SB's hash differs from the recorded one, that is, when SB really changed that
      field. It then wins when Bambuddy's hash is still the recorded one; when Bambuddy
      changed it too, only a Bambuddy time SB's row time is strictly after lets it win.
      The row time alone never pushes it, also not in the seeding pass.
    - Filament catalog entries: as suppliers, locations and insert types since Task 5
      (R-T4-25, decision 76); before it they had no seen copy, so no mark, and no `UNSEEN`
      field pushed under `two_way`.
    Why: Task 5's `results` has no snapshot, and a blind push would break R-T4-4. Cost: an
    event pass does not seed.
72. Task 4 (R-T4-23): decisions 51, 53 and 64 are corrected. 51 says "one round later",
    53 is marked as changed by 65 (which 71 replaces), and 64 states what a cut retry
    waits for.
73. (changed by 74 for suppliers, locations and insert types) Task 4 (R-T4-22, build
    reading): under `sb_to_bb` the direction's rule (decision 8)
    pushes every difference, whatever was seen. So a field that differs at seeding is
    pushed in the seeding pass, and seeding changes no `sb_to_bb` push. Under `two_way`, a
    field that differs at seeding is pushed in that pass only when Bambuddy's row has a
    time and SB's change is strictly after it (decision 53): that push is not blind. Why:
    the ruling's reason is R-T4-4 ("never overwrite a value without knowing it is newer"),
    and both cases know. Cost: none known.
74. (changed by 76 for filament entries, and by 83) Task 4 (R-T4-27, re-review 2 I-1): suppliers, locations and insert types have one SB
    change time per row, so it never shows that SB changed one field. At seeding, `plan`
    records each field the seen copy leaves out in `bb_pushed` under the reserved key
    `unseen` (`bb_sync_plan.UNSEEN_KEY`): `{field: {"sb": <SB's field_hash>, "bb":
    <Bambuddy's field_hash>}}`. Under `two_way` such a field pushes only when SB's current
    hash differs from `sb` (`_row_wins`); a field with no mark does not push. A field in
    the seen copy pushes only when SB's value is not the seen one; when Bambuddy's value
    moved off the seen one too, only a known Bambuddy row time SB's row time is strictly
    after lets SB win. Spools keep their per-field rule (decision 71). The `bb` hash is a
    build addition to the ruling's `{field: sb_hash}`: without it, a seeded field Bambuddy
    changed again (no time) would be pushed over a value of unknown age. No pushed field is
    named `unseen` (`test_the_reserved_key_is_no_field_name`). Why: a rename in SB pushed
    the seeded identifier `A1` over Bambuddy's `B7` (re-review 2, measured). Tests:
    `test_a_rename_leaves_a_seeded_differing_identifier_alone` (no Bambuddy time, and an
    older one), `test_an_sb_identifier_change_after_seeding_is_pushed`,
    `test_a_seeded_field_bambuddy_changed_again_is_not_pushed_blind`,
    `test_a_seen_field_bambuddy_changed_is_not_pushed_for_another_sb_change`. Cost: under
    `two_way` a field both sides changed is decided by row times, and SB's row time can be
    later than its field change. A supplier or location with no seen copy (an event pass
    before the first full pass) pushes no changed field until a full pass seeds it. This
    narrows decision 73 for these kinds: no push in the seeding pass, Bambuddy time or not.
75. Task 4 (R-T4-28, re-review 2 I-2; carried to Tasks 5 and 9): stored answers are merged
    into `bb_seen`, not swapped. A field goes in only when the write sent it (the pull: took
    it), or when its answer value equals SB's compare value; every other field keeps its
    old seen value, or stays out when unseen, and its mark stays. Why: a whole answer
    makes a field the write did not send "seen", and the next pass pushes SB's older value
    (the re-review's scenarios 1 and 2). Cost: none known. Task 5's required test is
    scenario 1.
76. Task 4 (R-T4-29): decision 69 names filament catalog entries; Task 5's carry list gains
    R-T4-25 (their `bb_seen` columns and seeding). Decision 53's "keep this cost" lasts only
    until Task 5.
77. (changed by 80) Task 4 (fix round 4, the whole-plan check): under `two_way`, `decide` no longer lets SB
    win when Bambuddy's value moved off the seen one and Bambuddy's row has no time
    (catalog and filament entries, a supplier or location row without `updated_at`). Its
    age is unknown, so SB's value is not pushed over it. A value `results` stored in this
    pass still compares as seen, so decision 40's case of a fresh answer is unchanged.
    Why: the core rule, "never overwrite a value of unknown or newer age". Test:
    `test_a_value_bambuddy_changed_with_no_time_is_not_overwritten`. Cost: under `two_way`
    such a field keeps Bambuddy's value until SB takes it (Task 9's pull, where it pulls)
    or a later SB change finds Bambuddy's value unmoved.
78. (changed by 81) Task 4 (fix round 4, the whole-plan check; changes 63 for `two_way`): Bambuddy's spool
    rows do not carry the preset list, so a legacy link does not know Bambuddy's list.
    Under `presets` = `two_way` a link that never pushed the list and has no seen copy
    plans no preset write, and seeding stores SB's hash of the list in `pushed_fields`, so
    the list pushes when SB changes it. A link SB created (its seen copy stored by
    `results`) pushes the list as before, because Bambuddy made it empty. `sb_to_bb` keeps
    decision 63. Test: `test_a_legacy_link_does_not_put_an_empty_preset_list_under_two_way`.
    Cost: a preset list SB holds and Bambuddy lacks at seeding reaches Bambuddy only after
    SB changes it.
79. (changed by 80) Task 4 (fix round 4, the whole-plan check): a copy `results` stored in this pass
    (`bb_seen_at` after `read_at`) is read over the snapshot's row, field by field, for
    every kind and for spools. Under R-T4-28 it holds only the fields its write sent; a
    field it lacks takes the snapshot's value, not "missing" (which compared by hash and
    pushed SB's value blind). Test: `test_a_fresh_seen_copy_without_a_field_reads_the_snapshot`.
    Cost: none known.
80. Task 4 (R-T4-30, re-review 3 I-A; replaces 79's rule): decision 79's premise is false.
    Under R-T4-28 a copy `results` stored in this pass also keeps the old seen value of
    every field its write did not send and its answer did not match. So the overlay takes
    a stored value over the snapshot's value only when it equals SB's current compare
    value (the write sent it and SB has not changed it since, or the answer matched SB).
    Every other field reads the snapshot's value: rows with no row time (`bb_now`),
    spools with the answer's time (`linked_spool`). The K-profiles follow the same rule;
    a spool absent from the snapshot whose stored K-profiles are not SB's plans no K write
    in that round. Decision 77's "a value `results` stored in this pass still compares as
    seen" now holds only for such a value. Why: the overlay read the old seen value as
    "Bambuddy now", so round 2 pushed SB's older value over Bambuddy's newer one
    (re-review 3's scenario). Tests:
    `test_round_two_reads_bambuddys_newer_value_over_an_old_seen_one` (rows),
    `test_round_two_reads_a_spools_newer_note_over_an_old_seen_one` (spools). Cost: a field
    whose stored value is stale is not pushed until the next pass, and for a row kind that
    round counts it in `conflicts` (decision 83) when SB changed it again after the write.
81. Task 4 (R-T4-31, re-review 3 I-B; changes 39, 63 and 78): under `presets` = `two_way`
    the preset-list `PUT` is gated on "`filament_presets` is not in `pushed_fields`" alone,
    not on `bb_seen` being NULL. Such a link plans no preset write, on any pass, and a
    full pass records SB's hash in `pushed_fields` (bookkeeping), so the list pushes when
    SB changes it. A create (and the adoption of a lost create's spool) carries the hash
    of `[]` in its hashes when `presets` pushes, because Bambuddy starts a new spool with
    an empty list. `seed_spool` no longer records the hash; the gate does. Why: a link
    seeded while `presets` was `off` and then switched to `two_way`, or a link the pull
    made (Task 9), still sent a blind `PUT`. Decision 78's "a link SB created (its seen
    copy stored by `results`) pushes the list" treated "has a seen copy" as "SB created
    it", which is false. Build choice: the `[]` hash is recorded only when `presets`
    pushes at creation; a spool created while `presets` was off then has no hash, and the
    gate covers it after a switch. Tests:
    `test_a_seeded_link_does_not_put_its_preset_list_after_a_switch`,
    `test_a_create_records_bambuddys_empty_preset_list`. Cost: a link's first preset push
    waits until SB changes its presets.
82. (changed by 85) Task 4 (R-T4-32, re-review 3 open path a; changes 41): under `two_way`, adopting by name
    a Bambuddy row a person made (no intent) sends none of SB's differing readable fields;
    the adoption body holds only what already agrees (often `{}`), and its hashes leave the
    differing fields out. The plan records their seeding marks
    (`bb_pushed["unseen"]` `{field: {"sb": …, "bb": …}}`, decision 74) on the SB row, as
    seeding does, so the link then acts as a seeded one. Decision 41's accepted cost ("a
    BB row of the same name that a person made becomes SB's") is withdrawn. An adoption
    with an intent (SB's lost create) still sends SB's values; `sb_to_bb` keeps pushing
    every difference (decision 73). Build addition: the adoption records an intent so
    `results` can link it (decision 41), and a lost answer's retry would then find that
    intent and push. So the marks are stored even when empty, and an unlinked row whose
    `bb_pushed` holds `unseen` (`_Item.marked`) keeps the person's path whatever intent
    it has. Why: the binding constraint (no blind push), and the adopted row's values are
    the person's. Tests: `test_adopting_a_row_a_person_made_pushes_no_differing_field`
    (both paths, and a lost answer's retry),
    `test_a_person_made_row_with_no_difference_stays_the_persons`. Cost: SB's differing
    values reach Bambuddy only after SB changes them again.
83. Task 4 (R-T4-33, re-review 3 open path b; changes 71 and 74; carried to Tasks 7 and 9):
    under `two_way`, when SB and Bambuddy both changed the same field of a supplier,
    location, insert type or filament entry (a seen field: both moved off the seen value;
    an `UNSEEN` field with a mark: both hashes moved off the mark's), `_row_wins` returns
    False whatever the row times. Nothing is pushed, and the plan counts the field in
    `SyncPlan.conflicts`. `SyncPassResult.conflicts` carries it (Task 7). Task 9's pull
    applies the same rule: nothing is pulled. Why: a row's `updated_at` cannot show when
    SB changed one field (any column or a parent's move sets it), so it cannot prove SB's
    change newer, and the constraint forbids overwriting a change not proven older.
    Re-review 3 measured a case where SB's value was provably older. Tests:
    `test_a_field_both_sides_changed_is_a_conflict`, and
    `test_a_seeded_field_bambuddy_changed_again_is_not_pushed_blind` (its second assertion
    now expects no write and one conflict). Cost: such a field stays different until a
    person edits it on one side; the count makes this visible.

84. Task 5 (R-T4-34, re-review 4 M-1): the row-time plumbing that nothing read is gone.
    `rows` passes no SB time and no Bambuddy time to `changed` for suppliers, locations,
    insert types and filament entries; `bb_now` returns the compare values only; `_Item`
    has no `sb_at`, and the item queries read no `updated_at`. `changed`'s `sb_at` and
    `bb_at` are a spool's (they default to none). Why: every finding gets fixed, nits
    included, and a dead input invites a later blind push. Cost: none.
85. Task 5 (R-T4-35, re-review 4 note; changes 41, 44 and 82): under `two_way`, the
    adoption of a row SB's own create made (its answer was lost) pushes only the fields
    where Bambuddy's row still holds what the create sent. `filament_bb_creates.sent jsonb`
    (this branch's unmerged migration, #491) holds the create's field hashes; `take`
    writes it with the intent, and again when a create is made over an old intent. A
    differing field whose Bambuddy hash is not the sent one is a person's later edit: it
    is not in the body or the hashes. A row kind records its seeding marks (decision 74);
    a spool's field stays out of `bb_seen` (`results`' merge, R-T4-28), so it is `UNSEEN`
    and decided by the times (decision 71). A held `weight_used` holds `weight_locked`
    back, and the other way round. An intent with no `sent`, and a field the create did not
    send (`last_dried_at`), hold the field back. `sb_to_bb` keeps pushing every difference
    (decision 73). Why: the binding constraint; this closes the last blind-push path.
    Tests: `test_a_person_edit_of_a_row_sb_made_is_not_pushed`,
    `test_an_sb_change_after_a_lost_create_is_pushed`,
    `test_a_lost_spool_create_pushes_only_what_bambuddy_kept` (both cases), and
    `test_adopting_a_row_a_person_made_pushes_no_differing_field[sb-made]` now stores the
    sent hashes. Cost: a field Bambuddy normalised on create (trimmed, cased) is held until
    SB changes it again; after a lost create, a spool's drying time waits for the next
    drying. A row whose adoption held a field is marked (`_Item.marked`), so the retry of
    that adoption's lost answer takes the person's path and holds every differing field
    (liveness only).
86. Task 5 (build): a create's answer is stored whole as the seen copy (a spool's
    `bb_k_profiles` too, `[]` when Bambuddy answers none); every other write's answer is
    merged (R-T4-28). Why: Bambuddy made the row from SB's body in that one call, so a
    field the create did not send holds Bambuddy's default, never a person's edit; and
    decision 37's "the drying time and the lists arrive one round later" needs that
    default as seen. Cost: none known.
87. Task 5 (build; changes 31): `results` makes no `GET`. `archive` and `restore` answer a
    full `SpoolResponse` (`sync-routes.json`). The list `PUT`s answer their list only, and
    under R-T4-28 only the field the write sent is stored, so the list is enough: the
    worker passes `answer = {"k_profiles": rows}` or `{"suppliers": rows}` (or the whole
    spool row), and Task 7 must build it so. The preset `PUT` stores its hash only. A list
    write books no event and leaves `bb_updated_at` as it is; so does a probe. Why: the
    API process has no Bambuddy client in `results`, and the answer holds what is needed.
    Cost: none known.
88. Task 5 (build; changes 62): a 404 drops a link on `update`, `archive`, `restore` and
    `probe` only. A list `PUT`'s 404 is a failure (an error row): `PUT …/suppliers` also
    answers 404 for a supplier Bambuddy does not know (`write_semantics.replace_all_lists`).
    Why: dropping a live spool's link makes the plan create it again (a duplicate). Cost: a
    spool deleted in Bambuddy whose list `PUT` met the 404 is unlinked one pass later, by
    its probe.
89. Task 5 (build; changes 30): archiving a supplier, location, insert type
    (`CatalogStore.update` of `insert-types`) or spool type deletes its intent and its error
    row (`bb_sync_models.forget_sync`); archiving a spool deletes its error row only, and
    `FilamentSpoolStore.delete` deletes both. Why: a spool's intent lets a restore adopt the
    row its lost create made (Decision 10); without it the restore creates a duplicate, and
    Task 9's pull could take the orphan row as a new SB spool. The rows' intents close in
    the plan anyway (decision 69). Cost: an archived spool's intent stays until it is
    restored or deleted.
90. Task 5 (build; Decision 9 as written): the push's `synced_from_bb` event carries
    `snapshot(answer)` whole for every spool write that changes the row, not a merge. Why:
    a merged `bb` would let the pull take a person's Bambuddy edit stamped with the
    `updated_at` SB's own write gave the row, over an SB value that may be newer (the
    binding constraint). Cost: a person's Bambuddy edit of a field the push did not send,
    made before the push, is hidden from the pull until Bambuddy changes the field again;
    the push keeps it `UNSEEN` (R-T4-28), so neither side overwrites the other. Task 9's
    pull, which reads `bb_seen`, can take it.
91. (changed by the Task 5 review's I-1) Task 5 (build): `results` ignores every result while the state row's instance is set
    and is not the call's (Decision 12); an ignored result and an `ok` probe count nothing
    in `ResultsReport` (a probe writes nothing to Bambuddy, so it also keeps the item's
    error row; test `test_a_probe_that_finds_the_spool_keeps_the_link`); an answer must parse as the write's kind and carry its Bambuddy id.
    SB's compare value for the merge is computed for every data type. The event's actor is
    `system:bambuddy-sync`. R-T4-12, measured with `GET /api/v1/inventory/spools` on the
    live Bambuddy on 2026-11-20 (no key, 106 rows): `last_dried_at` is naive ISO text with
    microseconds (38 rows), which `json_form` keeps; `quoted_price_per_kg` is a plain
    number (2 decimals in the 4 priced links of 5, one null); `note`, `subtype` and
    `color_name` are text or null, never `""`. So `seen_value` needs no change.

92. Task 5 (R-T5-1, concern 1): decision 89 is accepted. Archiving a spool keeps its intent
    and drops its error row. Why: restoring an archived spool must not make a duplicate in
    Bambuddy. Cost: an archived spool's intent row lingers.
93. Task 5 (R-T5-2, concern 2; carried to Task 7): decision 87 is accepted. `results`
    makes no `GET`, and a list `PUT`'s answer is stored for its one field. Task 7 sends
    `{"k_profiles": rows}` or `{"suppliers": rows}` as the answer. Cost: none known.
94. Task 5 (R-T5-3, concern 3): decision 88 is accepted. A list `PUT`'s 404 does not
    unlink; the probe (#6688's path) unlinks a truly deleted spool one pass later. Cost:
    one wasted pass.
95. Task 5 (R-T5-4, concern 4): decision 86 is accepted. A create's answer is stored whole.
96. Task 5 (R-T5-5, concern 5; carried to Task 9): decision 90 is accepted. The echo
    event's `bb` is `snapshot(answer)` whole, as Decision 9 says. Task 9's pull reads
    `bb_seen` for the fields SB did not send.
97. (changed by 100) Task 5 (R-T5-6, concern 6; changes 44): the adoption of a spool after a lost create
    matches Bambuddy's rows against the intent's stored `sent` hashes (decision 85), in the
    fields behind `snapshot`'s values (`bb_sync_plan._ADOPT_FIELDS`, tags aside) that the
    create sent, and against SB's current body only when the intent has no `sent`. The
    `created_at` window stays. Why: an SB edit made between the lost create and the retry
    must not make a duplicate. Test:
    `test_an_sb_edit_after_a_lost_create_adopts_instead_of_creating_again` (seen to fail
    first: it planned a second create). Cost: none beyond the test.
98. Task 5 (R-T5-7, concern 7): decision 85's liveness costs are accepted under the
    binding constraint (no blind push).
99. Task 5 (R-T5-8, concern 8): noted. R-T4-12 stands unchanged; the live GET-only read
    was taken on 2026-11-20 (decision 91).

100. Task 5 (R-T5-9, review M-1; changes 97): `adoptable` accepts a field whose Bambuddy
     hash is the intent's `sent` hash or the hash of SB's current compare value. The
     second is what an adoption sent before its own answer was lost too, so a double-lost
     answer still adopts the row and makes no duplicate. Test:
     `test_a_double_lost_answer_still_adopts` (seen to fail first: it planned a create).
     Cost: none known.
101. Task 5 (R-T5-10, review M-2; changes 71): `filament_bb_links.bb_seeded boolean NOT
     NULL DEFAULT false` (this branch's unmerged migration) says the link holds a full seen
     copy: set by seeding and by any answer that is a whole spool row (a create, an
     adoption, an update, archive, restore or probe), never by a list `PUT`'s one-field
     answer. A full pass seeds every link whose `bb_seeded` is false, merging the seeded
     fields over what the copy holds and keeping its later `bb_seen_at`. Why: a list `PUT`
     on a link with no seen copy stored `{"suppliers": …}`, and seeding then never ran
     (it read "`bb_seen` is not NULL" as "seeded"). Test:
     `test_a_list_put_on_an_unseeded_link_leaves_it_to_seeding` (seen to fail first).
     Cost: none known.
102. Task 5 (R-T5-11, review M-3): one `apply_results` call reads each row kind's desired
     values and the spool references (`BbRefs`) once (`bb_sync_results._Reads`), not once
     per result; a link the call makes is added to the references it holds. Test:
     `test_results_read_each_kinds_rows_once` counts the reads with `monkeypatch` (not a
     SQL query count, which would break on any unrelated query); seen to fail first (3
     location reads, 2 reference reads). Cost: a person's SB edit made during the call is
     not seen by the call's later results; it only decides whether an unsent field of
     the answer is stored as seen, so no push goes blind.
103. Task 6 (R-T6-1, review M-1; superseded by R-T6-5): `/results` keeps no Idempotency-Key.
    A failed result sent again after a lost answer must not raise the item's attempts again.
    The first fix was a 30 s window, replaced by R-T6-5's exact pass identity (decision 107).
104. Task 6 (R-T6-2, review M-2): no code change. A skipped pass with no error sets
    `last_ok_at`, as Decision 19 says. The status UI must not read it as "last sync".
105. Task 6 (R-T6-3, review M-3): `test_remote_and_local_give_the_same_plan` compares a
    non-empty results answer (a refused write: `ResultsReport(failed=1)`) from both
    implementations.
106. Task 6 (R-T6-4, review M-4): the area test in `test_crud_filament.py` checks membership
    only, and its docstring no longer says "appended last". The tail asserts of the later
    areas check the order.
107. Task 6 (R-T6-5, re-review): the plan answer carries a `pass_id` (`SyncPlan.pass_id`,
    made by `LocalFilamentSync.plan` for each plan round: a server-made `uuid4().hex`), and
    `ResultsRequest.pass_id` and `apply_results(..., pass_id=None)` carry it back.
    `filament_bb_push_errors.last_pass_id text` (in the unmerged migration
    `20261118T2112Z_filament_bb_sync.sql`) holds the round that last wrote the row. A failed
    result of a round whose id the row holds adds no attempt and keeps `at`; another round
    with the same error adds one, with no time window; a `None` id is a round of its own.
    Task 7 sends the plan's `pass_id` with that round's results. Tests:
    `test_results_of_one_pass_sent_again_add_one_attempt`,
    `test_a_resent_failed_result_is_one_attempt` (local and remote agree). Cost: none known;
    the report of a resent ok create still answers `linked=0`.
108. Task 7 (R-T7-1; changes 70, carried to Task 8): the activity `filament_bb_sync_pass`
    runs exactly one pass and returns its `SyncPassResult` with `more` as the pass left it.
    The bound of decision 70 (at most 3 passes back to back on `more`, then the normal
    interval, logged once) and "`more` → next pass at once" are the workflow's (Task 8).
    Test: `test_one_activity_call_is_one_pass_and_keeps_more`. Why: a workflow sees each
    pass and can count them; an activity that loops hides them. Cost: none.
109. Task 7 (R-T7-2): a pass whose configured Bambuddy is another instance than the
    workflow's records nothing (`record_pass` is not called) and returns
    `instance_changed=True`. Why: the state row belongs to the configured instance, whose
    own workflow records its passes. Cost: none.
110. Task 7 (R-T7-3): `run_pass` and `SyncPassInput` take `interval_s` (default 300). A pass
    that made no plan (skipped, or a failed spool read) reports it back unchanged; Task 8
    passes it from settings. Why: `spool_overlay_refresh_seconds` is not a `StoredSettings`
    field. Cost: none.
111. Task 7 (R-T7-4): `bb_sync_nudge.py` exists from Task 7 with `instance_for` only; Task 8
    adds the rest of it. Cost: none.
112. Task 7 (R-T7-5): `put_spool_k_profiles`, `put_spool_presets` and `put_spool_suppliers`
    return Bambuddy's answered list (`_rows`), not `None`, so a list `PUT`'s result carries
    it (decision 93). Cost: none; no caller used the old `None`.
113. Task 7 (R-T7-6): `PUT …/inventory/catalog/{id}` takes the name and the weight both. An
    insert type update that sends one of them takes the other from this pass's snapshot of
    the catalog; an entry the pass did not read is a not-ok `internal` result. Cost: an
    entry Bambuddy changed after the read gets the read value back for that field.
114. Task 7 (R-T7-7, review finding 1): a pass whose plan answers `instance_changed` (SB's
    links belong to another Bambuddy) records nothing, as decision 109's pass does, and
    returns `instance_changed=True`. Test: `test_a_plan_for_another_instance_stops_the_pass`.
    Why: `last_ok_at` must not move for a pass that synced nothing. Cost: the status keeps
    the last real pass until Task 8's workflow ends.
115. Task 7 (R-T7-8, review finding 5): `BambuddyClient._rows` turns a body that is not JSON
    into the same 502 `ApiError` as a body that is not a list, so a spool listing that is
    not JSON fails the pass with `error_label(exc)` and `record_pass` gets that error. Test:
    `test_a_spool_listing_that_is_not_json_fails_the_pass`. Cost: none.
116. Task 7 (R-T7-9, review findings 2-4 and 6): tests for a probe counting toward the cap,
    a weight-only catalog update and an unread catalog entry, `_maybe`'s `"invalid"` path,
    and failed location, catalog and filament reads; `written` counts with an explicit
    `+= 1`. Cost: none.
117. Task 7 (R-T7-10, re-review; changes 115): both 502s of `BambuddyClient._rows` (a body
    that is not JSON, a body that is not a JSON array) carry the problem type
    `BAD_BODY_PROBLEM` (`https://scadbuddy.dev/problems/bambuddy-bad-body`,
    `bambuddy/errors.py`), so `error_label` names what happened. Like the client's other
    wrong-shape answers (`REJECTED_PROBLEM` on a model that does not validate), they set no
    `bambuddy_status`: Bambuddy's status was a success. The callers of a `_rows` read with
    an `except ValueError` of their own: `bambuddy_pull.read_catalog` and
    `try_assignments` caught a JSONDecodeError there; they now log a `BAD_BODY_PROBLEM` as
    "an answer of another shape" in their `except ApiError`, and their `except ValueError`
    keeps the pydantic ValidationError. Every other such caller (print_import, choices,
    archive_cache, printers_now, send, print_run, print_history) also catches `ApiError`
    with the same handling, so no change. Tests: `test_a_listing_body_of_the_wrong_kind_is_a_bad_body`,
    `test_a_spool_listing_that_is_not_json_fails_the_pass`,
    `test_a_garbled_catalog_read_is_none_and_logged`. Cost: none.
118. Task 8: `FilamentBbSync` is registered on `bambuddy_worker` (the print worker, and the
    API's in-process print queue), the worker and queue that register Task 7's
    `filament_bb_sync_pass`; that worker is AUTO_UPGRADE (`print_deployment`). The nudge
    starts it on `temporal_task_queue_bambuddy`. Cost: none.
119. Task 8: `SYNC_PASS_ACTIVITY`, `SyncPassInput` and `DEFAULT_INTERVAL_S` moved from
    `bb_sync_run.py` to `bb_sync_models.py`. Why: `workflows/client.py` imports the workflow,
    and importing `bb_sync_run` from it closes a cycle (`bambuddy_pull` →
    `filament/component.py` → `api/deps.py` → `workflows/client.py`). Cost: none;
    `bb_sync_run` imports them back.
120. Task 8: `SyncStart`, `FILAMENT_BB_SYNC_WORKFLOW` and `CHANGED_SIGNAL` live in
    `bb_sync_nudge.py`, which the workflow imports (the nudge cannot import the workflow's
    module, for the same cycle). `SyncStart` has `pending` and `full` (both default False)
    to carry a signal the run has not served across a continue-as-new, a plain one too.
    Why: with `interval_s` 0 a lost plain signal would wait for the next write. Cost: none.
121. Task 8: the nudger's `load_settings` is `SettingsStore.read` over `bambuddy_url` and
    `spool_overlay_refresh_seconds` (a new workflow's interval). `nudge_after_write` calls
    `FilamentSyncNudger.nudge_soon` (a task the nudger holds until it ends) after its
    `yield`. Why: on FastAPI 0.162 a yield dependency's exit runs after the response, and a
    `BackgroundTasks` task added there never runs (measured on this tree with a test app,
    2026-11-20). A route that raises (a 422 of its body too) reaches the dependency as the
    exception, so it nudges nothing. Cost: a route that returned a non-2xx response without
    raising would nudge; none of the three routers has one.
122. Task 8: a pass whose 3 attempts all failed is logged (`ActivityError`) and the workflow
    waits for the next signal or the timer; it never fails the workflow. The pass after
    `more` keeps the first pass's `full`. Cost: none.
123. Task 8: `put_settings` compares `snapshot.runtime.filament_sync` (an env-seeded field;
    `StoredSettings` has none) with the value read before the save, only when the patch
    sets or resets `filament_sync`.
124. Task 8: at boot `_adopt_bb_instance_logged` runs `adopt_instance` after the components
    start and before the workers and the first request; a failure is logged, never raised.
    `_run_library_worker` starts `nudge(full=True)` once its client connects. The api
    tests' autouse `filament_nudges` (`tests/api/conftest.py`) records `nudge_soon` and
    silences `nudge`, as the api tests keep the spool refresh off (#4180): a pass would read
    Bambuddy beside a test's mocked reads.
125. Task 8 (R-T8-2, controller ruling on concern 1): `run_pass` reads the stored
    `filament_sync` through `FilamentSyncApi.directions` (`LocalFilamentSync` reads the
    settings; `RemoteFilamentSync` the new CRUD route `GET …/print/filament/sync/directions`,
    area `filament-sync`, since the print worker's settings carry no `filament_sync`). After
    the URL and instance checks, a pass in which no data type pushes (`pushes_anything`,
    `bb_sync_models.py`) records the pass as `skipped="nothing_pushes"` and returns before
    any Bambuddy read. The workflow ends on any `skipped`. `FilamentSyncNudger.nudge` reads
    `filament_sync` too and starts nothing when nothing pushes, as with no `bambuddy_url`.
    A settings save that turns a direction on nudges (decision 123), which starts the
    workflow again. Tests: `test_nothing_pushing_skips_before_any_bambuddy_read`,
    `test_a_workflow_for_an_old_instance_ends[nothing-pushes]`,
    `test_a_nudge_with_nothing_pushing_does_nothing`,
    `test_turning_a_direction_on_starts_the_workflow`,
    `test_the_directions_route_answers_the_stored_directions`. Why: by default nothing
    pushes, and a workflow would read Bambuddy's five listings every interval for nothing.
    Cost: one more CRUD call per pass.
126. Task 8 (R-T8-3, review spec finding; keeps Decision 17): `POST
    /filament/spools/sync-ams-weights` does not nudge. `nudge_after_write` skips the routes
    named in `api/filament.py` `NOT_NUDGED` (`sync_ams_weights`, the route's name in
    `request.scope["route"]`); the route stays on the `/filament` router. Test:
    `test_the_ams_weights_sync_does_not_nudge` (that POST nudges nothing, a spool create on
    the same router nudges once).
127. Task 8 (R-T8-4, review Important 1; changes 125): on a `skipped` result the workflow
    returns only when no `changed` signal is pending. With one pending (a save that turned
    a direction on while the pass found nothing pushing), it goes back to its wait, which
    debounces and runs the next pass. Test: `test_a_signal_during_a_skipped_pass_runs_one_more`
    (`Passes.during` signals during a `nothing_pushes` pass; a second pass runs in the same
    run). The code has never run on prod, so `v1` still replays (its passes are never
    skipped) and is not re-recorded; no `workflow.patched`.
128. Task 10: `SyncStatus.skipped` is `skip_reason(bambuddy_url, filament_sync)`
    (`bb_sync_models.py`) of the settings now, typed as the pass's own `SkipReason`
    (`no_bambuddy`, `nothing_pushes`); the nudge (`FilamentSyncNudger.nudge`) uses the same
    function. It is not a stored column. Why: the nudge starts no workflow in either case, so
    no pass records it; with the default settings (nothing pushes) no pass has ever run, and
    with `interval_s` 0 a running workflow never learns that nothing pushes. Cost: none.
    Test: `test_status_says_why_no_pass_runs`.
129. Task 10: `POST /filament/sync/run` answers 409 with `code` `no_bambuddy` (the brief's)
    and also with `code` `nothing_pushes`: the nudge would start nothing, and a 202
    `{"started": true}` would be false. Test: `test_run_with_nothing_pushing_is_409`.
130. Task 10: the route awaits `nudge(full=True)` (at most `NUDGE_TIMEOUT`, 5 s). A failed
    signal is logged by the nudge and the route still answers 202; the timer's full pass
    makes up for it (Decision 7). The route is in `NOT_NUDGED`, so `nudge_after_write` adds
    no plain nudge after it. Test: `test_run_nudges_full`.
131. Task 10: `filament_sync_run` is `outward` (gated, approved first) as
    `filament_rfid_reconcile` is, and in `TOUCHES_NOTHING` beside it;
    `filament_sync_status` is `read`.
132. Task 10 (R-T10-1, concern 1): decision 128 is accepted as built. `skipped` is
    `skip_reason` of the settings now.
133. Task 10 (R-T10-2, concern 2; changes 129 and 130): "Sync now" never answers 202 when
    nothing started. `FilamentSyncNudger.nudge(full=, raise_errors=False)` answers the
    `SkipReason` it stopped on (else `None`), and with `raise_errors` raises a failed
    settings read, start or signal (or its 5 s timeout) instead of logging it. The run
    route calls `nudge(full=True, raise_errors=True)` and no longer reads the settings
    itself: the 409s come from the nudge's answer, and a raise is a 503 whose detail ends in
    `rfid_push.failure_label(exc)` (`error_label` for an `ApiError`, else `internal`; it was
    `_label`), never the message. The write nudge (`nudge_soon`) stays log-only. Tests:
    `test_run_that_cannot_reach_temporal_is_503`, `test_raise_errors_raises_and_logs_nothing`,
    `test_a_nudge_answers_why_it_started_nothing`.
134. Task 10 (R-T10-3, concern 4): a `put_settings` that changes `filament_sync` nudges when
    the new value pushes; when it pushes nothing and the old value did, it calls
    `FilamentSyncNudger.signal_running_soon`: a `changed` signal to the configured
    instance's workflow, never a start. The workflow's next pass is `nothing_pushes` and it
    ends (R-T8-4), also with `interval_s` 0. A workflow that does not exist (`NOT_FOUND`)
    or no `bambuddy_url` is nothing; another failure is logged. The api tests' autouse
    `filament_signals` records it. Tests: `test_a_save_that_stops_every_push_signals_only`,
    `test_signal_running_signals_and_never_starts`,
    `test_signal_running_with_no_workflow_is_quiet`,
    `test_a_save_that_stops_every_push_ends_a_running_workflow`.
135. Task 11: the six directions are one form string, the six parts joined by `|` in
    `SYNC_KINDS` order (`values.ts` `syncParts`, `syncValue`); a missing or unknown part reads
    `off`. The mock keeps a saved `filament_sync` in `sessionStorage`, so a reload keeps it.
    Cost: none known; the server always sends all six keys.
136. Task 11 (R-T11-1, concern 4): each `SyncError` carries a `label` the API resolves: a
    spool's `#N` (the spool list's name for it), a spool type's code (the Bambuddy
    filament's name, Decision 14), any other row's name; null when the item is gone. One
    query per kind present (`bb_sync_results._labels`), never one per row. The UI shows the
    label, and `shortId(sb_id)` only when it is null. The route's and
    `filament_sync_status`'s descriptions name it. Tests:
    `test_status_labels_each_error_row`, `names an item that is gone by its id`. Cost: none
    known.
137. Task 11 (R-T11-2): the pass "Sync now" asks for runs in the background, so after a 202
    the page reads the status every 3 s (`SYNC_POLL_MS`), for at most 60 s
    (`SYNC_POLL_LIMIT_MS`, 20 reads), until `last_pass_at` differs from its value before
    the click; then it shows the new pass and "Sync finished.". On the limit it says "The
    pass is still running. Check back later." Unmounting stops the reads, and "Sync now" is
    disabled while they run. The mock's pass lands on the second status read after the
    run, never inside the POST, so a test sees the reads. Tests: `sync now re-reads the
    status every 3 s until the pass lands`, `says the pass is still running after 60 s with
    no new pass`, `stops reading the status when the page goes away` (fake timers). Cost: up
    to 20 extra status reads per click, each a read of two SB tables.
138. Task 9 (R-T9-0, controller ruling; changes the brief): where the brief and #6707
    differ, #6707 wins. An unlinked archived BB spool makes an archived SB spool
    (`_create_archived`), with its usage rows booked, and it stays archived. Test:
    `test_an_unlinked_archived_bb_spool_is_made_archived_with_its_usage`, in place of the
    brief's `test_an_unlinked_archived_bb_spool_is_not_made`. Why: #6707 merged to main
    first and imports used-up spools with their history. Cost: none known.
139. Task 9 (R-T9-0b, controller ruling): in `bambuddy_pull.py` each `has_key(...)` that a
    key lookup or `resolve_or_add` followed is one `catalog.lookup_key(conn, kind, name,
    material=...)` (`_resolve`), as `types.py` `_by_key` does: a name with no key is
    skipped, a found row is used, and only a missing row goes to `resolve_or_add`. The
    behaviour is the same. `_catalog_id` stays for `csv_import.py`. Why: one query in place
    of two per name. Cost: none known.
140. Task 9 (R-T9-1, controller ruling): the archived create runs only under
    `pulls(spools)`, as every create does. Test: the first `read_and_pull` of
    `test_an_unlinked_archived_bb_spool_is_made_archived_with_its_usage` (`spools` off
    makes nothing).
141. Task 9 (R-T9-2, controller ruling): an archived spool the pull creates takes no
    weighing, drying, preset or K value. Test: the same test (no `weighed` event, no own
    preset, no K value).
142. Task 9: the pull's "newer change wins" (`_sb_newer`) is `sb_changed_at`, but the
    spool's `created_at`, that rule's fallback, is no SB change on the pull. Why: a spool
    the pull made took BB's values, and a link keeps the SB spool's own values and records
    BB's as seen, so only a change after it counts. With the fallback, a BB edit stamped
    before the spool row was made could never reach it. Cost: none known; on the push the
    fallback stays.
143. Task 9: a linked spool takes BB's archive state only when BB's state moved off
    `bb_seen.archived_at`. With no seen archive state a BB archive is taken by the time
    rule, and a BB restore is not: a BB row that is live and newer than an SB archive does
    not show that Bambuddy restored it. BB's change time is the later of `updated_at` and
    `archived_at`. The pull archives and restores through `update_on` (actor `bambuddy`,
    the event at now). Tests: `test_a_bb_archive_archives_the_sb_spool`,
    `test_an_sb_restore_after_the_bb_archive_wins`,
    `test_an_sb_archive_is_not_undone_by_a_bb_row_that_was_never_archived`. Cost: a link
    seen before Task 9 with an SB archive and a BB restore stays archived until Bambuddy
    archives and restores again.
144. Task 9: `Pulls` gains `suppliers` and `locations`, `False` by default as in
    `FilamentSync`, so `EVERY` (renamed in its comment to "the defaults") reads no new
    route. `read_and_pull` reads `GET /inventory/suppliers` only under `pulls.suppliers`
    and `GET /inventory/locations` only under `pulls.locations`; each read and each row
    pull fails alone, logged.
145. Task 9: a supplier or location field with neither a seen value nor a mark (a row
    linked by its name) follows the rows' `updated_at`: SB's row strictly after BB's keeps
    SB's value; a tie, or no BB time, takes BB's. A field kept this way gets its seeding
    marks in `bb_pushed["unseen"]` (R-T4-27), so a later pull or push moves it only on a
    real change. A link by name does not move `updated_at`. A nested place takes a BB name
    only when the name keeps its parents' path (`` / ``); a name another SB row holds is
    not written, logged. An archived SB row is left alone. Tests:
    `test_a_bb_supplier_links_by_name`, `test_a_field_only_bambuddy_changed_is_pulled`.
146. Task 9: K values are pulled for a spool with a type too (presets are not, F27): a key
    whose effective value (the type's) is already BB's gets no row. `bb_k_profiles` takes
    BB's whole list whenever it changed and presets pull. Tests:
    `test_bb_k_profiles_become_sb_k_values`, `test_a_persons_k_value_is_kept`.
147. Task 9: after the push's echo event, a field moved when it is off `bb_seen` or absent
    from it. When the pull takes nothing, it books no event, so the echo stays the last
    reading. Cost: while a field stays unseen, each pull reads that row in full again and
    keeps SB's value by the same rule.
148. Task 9: `BbSpool` gains `created_at` (`SpoolResponse.created_at`, `sync-routes.json`
    v1.3.1). The pull finds a claimed BB spool with `bb_sync_plan.claims`, which is
    `_Planner.adoptable`'s rule, from `claim_form` of the intent spool's desired values
    under every direction. Test: `test_the_pull_leaves_a_spool_an_intent_claims`.
149. Task 9: the seen copy of a spool the pull made or linked takes a field only when BB's
    value is SB's compare value; only `_update` names what it took. Cost: a catalog name SB
    keeps under another spelling (BB `DUAL-COLOR`, SB `Dual Color`) stays unseen on a new
    spool until a change of it.

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
