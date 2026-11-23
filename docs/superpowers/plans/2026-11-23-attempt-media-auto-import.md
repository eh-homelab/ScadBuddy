# Attempt media auto-import — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan story by story. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An order attempt that names a Bambuddy archive gets that archive's latest finish
photo and its timelapse as attachments, with no click, imported once, by every trigger.

**Architecture:** `entity_attachments` gains archive provenance (`archive_id`,
`archive_media`) and a partial unique index, so "already imported" is a row read. One
Temporal workflow per attempt, `AttemptMedia` on the `library` queue (the API's worker),
runs the step `mirror_attempt` now, at +2 min and at +15 min. Three triggers start it (a
hand attempt, a heard print outcome, the archives read); a housekeeping sweep catches the
rest. A new seam call asks Bambuddy to scan the printer for a missing timelapse, behind a
setting. The order page loads each attempt's archive row on open and renders from the
answer.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, Temporal (library worker,
housekeeping), React + TypeScript, TypeScript agent tools.

**Spec:** [`docs/superpowers/specs/2026-11-05-attempt-media-auto-import-design.md`](../specs/2026-11-05-attempt-media-auto-import-design.md)
(approved 2026-11-05, reviewed against main 2026-11-23). The spec is the authority; F1–F14
are its facts table.

## Tracking

Epic: TBD
Stories: TBD

## Global constraints

- Read the repo's `CLAUDE.md` and the area `CLAUDE.md` of each tree you touch
  (`backend/CLAUDE.md`, `frontend/CLAUDE.md`, `agent/CLAUDE.md`).
- Only the CRUD service (the API process) gets SQL. Every write in this plan is in the API:
  its routes, and activities on the `library` queue, whose worker runs in the API
  (`main.py` `_run_library_worker`). Never a new database client, never a write from the
  agent or a worker pod.
- Bytes go to the blob store with a `blob_refs` hold (`orders/attachments.py` `keep`);
  never `bytea`, never base64 in JSON (#2479).
- Migrations: a NEW file `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` in
  `backend/scadbuddy/migrations/`. Never edit a merged one. A migration may depend only on
  files already on main.
- A Bambuddy download is never awaited inside an HTTP request. A route may start the
  workflow (one Temporal call); a failed start is logged, never raised.
- Workflows on the `library` worker are AUTO_UPGRADE through a `Versioned*` subclass in
  `main.LIBRARY_WORKFLOWS`. A change to the commands a workflow issues goes behind
  `workflow.patched`. A new housekeeping sweep goes behind its own patch, with a replay
  history.
- No system actor (Ruling 12). An automatic write names the attempt's `created_by`.
- Tests first (superpowers:test-driven-development): each step below writes the failing
  test before the code. A parallel backend test run uses its own database (NOTIFY
  cross-talk). Use the uv version the Dockerfile pins (`uvx uv@<version>`).
- A UI change loads the `frontend-design` skill first.
- Never `pkill -f`, `killall` or any machine-wide kill. Kill only by pid or process group.
- PRs: conventional-commit title, body `Fixes #N` (the story's issue), cite a source for
  each external fact and say where each measured fact was measured (#259). Never commit
  `backend/openapi.json` or either `schema.d.ts`. No counts of tests or migrations in docs.

## Review focus

1. A replay of the same trigger (three replicas, a page reload, the sweep) downloads a
   medium at most once per attempt and never writes a second row for one
   `(subject, archive_id, archive_media, filename)`. (Stories 3, 4.)
2. A person's own `finished` photo is never replaced or deleted; a second finish photo
   chosen by `name` is still allowed. (Story 3.)
3. A timelapse over `media_upload_max_bytes` does not stop the finish photo. (Story 4.)
4. `POST …/fulfilments` never waits on a download; with Temporal down it still answers 201.
   (Story 4.)
5. The scan runs at most once per attempt per day, never after 14 days, and never with
   `orders_import_timelapse` off. (Story 6.)

## Order and dependencies

```
1 spike ───────────────────────────────┐
2 migration ─► 3 copy_in ─► 4 workflow ─┼─► 6 scan + setting
                             │          │
                             ├─► 5 sweep│
                             └─► 7 read fields + frontend ─► 8 agent + docs
```

Story 1 needs the live Bambuddy and can run at any time. If it is not done, story 6 waits
and stories 2–5, 7 and 8 proceed: the import then copies only what Bambuddy already has,
and the page shows "waiting for Bambuddy to process the timelapse" until a scan exists.
Story 8's text describes the scan only once story 6 has merged.

CI lanes (`.github/scripts/select-lanes.sh`): a change under `backend/` selects
`backend` and `image`; under `frontend/`, `image`; under `agent/`, `agent_durable`. The
`frontend`, `frontend-unit` and `agent` jobs run on every PR. An API schema change also
shows up as the `freshness` job's diff comment and regenerates the Go client.

---

### Story 1: Spike — record Bambuddy's `timelapse/scan` replies

**Goal:** Know what `POST /api/v1/archives/{id}/timelapse/scan` answers, whether its FTP
pull is complete when it returns, and what it matches on (spec §4.3, F5).

**Who:** the owner, or a session with LAN access to the live Bambuddy and its API key
(1Password, read with `op.exe`, never printed). A session without LAN access cannot do it.

**Files:**
- Create: `backend/tests/bambuddy/recordings/timelapse-scan-found.json`,
  `timelapse-scan-none.json` (replies, serials and IPs replaced as the other recordings)
- Modify: `backend/tests/bambuddy/recordings/README.md` (a row per file: request, date,
  Bambuddy version, archive state)
- Modify: `docs/superpowers/specs/2026-11-05-attempt-media-auto-import-design.md` F5 (the
  measured facts, with where and how measured)

**Steps:**
- [ ] Read the live `/openapi.json` for the route's response schema; note the Bambuddy
  version.
- [ ] Pick two finished archives of the H2C: one whose timelapse is on the printer and not
  in Bambuddy (`GET /archives/{id}/printer-media` lists it in `remote_files`), one with none.
- [ ] Call the scan once on each, with a key that has Manage Archives and Control Printer.
  Time each call. Read the archive again after it: is `timelapse_path` set at once?
- [ ] Call it once with a key that lacks Control Printer; record the refusal.
- [ ] Check the match: does the attached file's name and mtime belong to this print
  (compare `printer-media`'s list and the archive's `completed_at`)?
- [ ] Vendor the replies; update F5.

**Acceptance:** two (or three) recordings with a README row each; F5 states the reply's
shape, whether the call blocks until the pull ends, and the match rule. If the match is
loose (a wrong print's file can attach), stop: the owner decides before story 6 ships
the switch on (spec §8.1).

**Lanes:** `backend`, `image` (recordings live under `backend/tests/`).

### Story 2: Migration and model — archive provenance and attempt stamps

**Goal:** `entity_attachments` records which archive and medium a copy came from, a
double insert is impossible, and the attempt can record its scan and its last check
(spec §4.2, §4.3, §4.6).

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_attempt_media_provenance.sql`
- Modify: `backend/scadbuddy/orders/models.py` (`Attachment.archive_id`,
  `.archive_media: Literal["finished_photo", "timelapse"] | None`;
  `Fulfilment.timelapse_scanned_at`, `.media_checked_at`)
- Modify: `backend/scadbuddy/orders/store.py` (`_ATTACHMENT`, `_FULFILMENT`,
  `insert_attachment` takes and writes both columns and raises `ArchiveCopyExists` on the
  unique index; `archive_media_present(fulfilment_id, item_id) -> set[tuple[int, str]]`;
  `stamp_media_checked`, `stamp_timelapse_scanned`)
- Test: `backend/tests/test_pg_migrations.py`, `backend/tests/orders/test_attempt_files.py`

Migration (one file):
- `ALTER TABLE entity_attachments ADD COLUMN archive_id integer, ADD COLUMN archive_media
  text CHECK (archive_media IN ('finished_photo', 'timelapse')), ADD CONSTRAINT
  entity_attachments_archive_pair CHECK ((archive_id IS NULL) = (archive_media IS NULL))`.
- Backfill: rows whose `subject` is `order_item_fulfilment:%` or `order_item:%` and whose
  caption matches `^Attempt [0-9]+, Bambuddy archive ([0-9]+)$`; `archive_media` from
  `filename` (`finish_%` → `finished_photo`, `timelapse-%` → `timelapse`); only the oldest
  row (`created_at, id`) per `(subject, archive_id, archive_media, filename)`.
- `CREATE UNIQUE INDEX entity_attachments_archive_copy ON entity_attachments (subject,
  archive_id, archive_media, filename) WHERE archive_id IS NOT NULL`.
- `ALTER TABLE order_item_fulfilments ADD COLUMN timelapse_scanned_at timestamptz, ADD
  COLUMN media_checked_at timestamptz`.

**Steps:**
- [ ] Failing test in `test_pg_migrations.py`: seed rows as `copy_in` wrote them (an
  attempt's photo and timelapse, an item's pre-#5268 photo, a caption a person edited, two
  rows on one key), migrate, assert the columns, the one-of-two rule, and that the index
  exists.
- [ ] Failing test in `test_attempt_files.py`: a second insert with the same key raises
  `ArchiveCopyExists` and leaves one row and one `blob_refs` hold; a different `filename`
  on the same archive is allowed.
- [ ] Write the migration and the store/model changes; run
  `cd backend && uv run pytest tests/test_pg_migrations.py tests/orders -n auto`.

**Acceptance:** the backfill is exact for the seeded rows; the index refuses a duplicate;
`Fulfilment` and `Attachment` carry the new fields in the API answers.

**Lanes:** `backend`, `image`.

### Story 3: `copy_in` writes provenance, survives a race, names its actor

**Goal:** Every copy, manual or automatic, records its archive and medium; a lost race
answers with the winner's row; the actor is explicit; the audit line says `source`
(spec §4.2, §4.5, §4.6). Also fixes F14.

**Files:**
- Modify: `backend/scadbuddy/orders/archives.py` (`copy_in`: passes `archive_id`,
  `archive_media`, `source`; catches `ArchiveCopyExists` and answers the existing row;
  `by` stays required and is the actor written)
- Modify: `backend/scadbuddy/orders/lifecycle.py` (`add_attachment` takes `archive_id`,
  `archive_media`, `source: str | None`; the `attachment.added` event carries `source`)
- Modify: `backend/scadbuddy/api/orders.py` (`ArchiveCopy.what:
  Literal["finished_photo", "timelapse"]`, F14)
- Test: `backend/tests/orders/test_archives.py`, `backend/tests/api/test_order_print.py`
  (it tests the copy route today)

**Steps:**
- [ ] Failing tests: a manual copy writes `archive_id`/`archive_media`; two concurrent
  `copy_in` of the same photo give one row and both answer it; a copy of a second named
  photo gives a second row; the audit event of an import has `source: "archive"`;
  `POST …/copy` with `what: "file"` is a 422.
- [ ] Implement; run `cd backend && uv run pytest tests/orders tests/api -k "archive or attachment" -n auto`.

**Acceptance:** the tests pass; the manual button behaves as today, plus the columns.

**Lanes:** `backend`, `image`.

### Story 4: `mirror_media`, the `AttemptMedia` workflow, and the three triggers

**Goal:** An attempt that names an archive imports its latest finish photo and its
timelapse with no click, after a hand attempt, a heard outcome, or a read of its archives;
retried at +2 and +15 min (spec §4.1, §4.2, §4.6).

**Files:**
- Modify: `backend/scadbuddy/orders/archives.py` (`mirror_media(lifecycle, integrations,
  blobs, claims, attempt, facts, *, limit)`: for each archive, the latest `finish_` by name
  and the timelapse when `facts.timelapse`, skipping what `archive_media_present` has; each
  medium its own try; a too-large medium logged with size and limit. `mirror_attempt(
  attempt_id) -> StepResult` (`missing: bool`), stamping `media_checked_at`. `mirror_run`
  and `attempt_archives` start the workflow)
- Create: `backend/scadbuddy/workflows/attempt_media.py` (`AttemptMedia`,
  `VersionedAttemptMedia`, AUTO_UPGRADE; input `{fulfilment_id, retries: bool}`; steps at
  0, +2 min, +15 min when `retries`, else once; ends early when a step answers nothing
  missing; activity `attempt_media_step`, `start_to_close` 30 min (a 1 GiB timelapse),
  heartbeat while downloading, retry 3 times)
- Create: `backend/scadbuddy/orders/attempt_media.py` (`MediaStarter` protocol,
  `TemporalMediaStarter` as `orders/storefront/staged_files.py` `TemporalWatcher`: id
  `attempt-media:<fulfilment id>`, `USE_EXISTING`, `ALLOW_DUPLICATE`; a failed start logged)
- Modify: `backend/scadbuddy/main.py` (`LIBRARY_WORKFLOWS`, the library worker's activities)
- Modify: `backend/scadbuddy/api/orders.py` (`_with_archive_facts` starts the workflow with
  retries after the facts, for a hand attempt with archives; the read route passes the
  starter)
- Modify: `backend/scadbuddy/orders/component.py` (the starter on `OrdersComponent`; the
  `on_outcome` lambda)
- Test: `backend/tests/orders/test_archives.py`, `backend/tests/test_attempt_media_workflow.py`
  (new, as `test_staged_file_workflow.py`), `backend/tests/api/` (the fulfilment route)

**Steps:**
- [ ] Failing tests with the `Archives` fake (it counts downloads): a hand attempt imports
  both media; a run attempt's outcome imports both; a second step downloads nothing; an
  item-level copy from before #5268 counts as present; a too-large timelapse still imports
  the photo; without Bambuddy nothing is asked; the latest photo is the last by name, not
  by list order; created_by is the attempt's.
- [ ] Failing workflow tests (time-skipping env): steps at 0, 2 and 15 min; ends after the
  first step that answers nothing missing; a second start joins the running one; a start
  after the end runs again.
- [ ] Failing route test: `POST …/fulfilments` with `archive_id` answers 201 when the
  starter raises; the route never calls `download_archive_media`.
- [ ] Implement; run `cd backend && uv run pytest tests/orders tests/test_attempt_media_workflow.py tests/api -k "fulfil or archive" -n auto`.

**Acceptance:** the tests pass; a deploy with the new workflow type follows the
`StagedFile` precedent (#6133: no `Recreate`, the library build is made current at start).

**Lanes:** `backend`, `image`.

### Story 5: The housekeeping sweep

**Goal:** An attempt whose triggers were missed (Temporal down, a lost outcome, a timelapse
pulled by hand days later) is still imported within the hour, for 14 days (spec §4.6).

**Files:**
- Modify: `backend/scadbuddy/workflows/housekeeping.py` (`ATTEMPT_MEDIA_SWEEP =
  "housekeeping_import_attempt_media"`, `ATTEMPT_MEDIA_PATCH`; in `SWEEPS` and
  `PRUNE_SWEEPS`)
- Modify: `backend/scadbuddy/orders/store.py` (`attempts_missing_media(created_after,
  checked_before)`: attempts with archives (or a finished run), lacking a finished or a
  timelapse row, `media_checked_at` null or older)
- Modify: `backend/scadbuddy/main.py` (the sweep activity, `@activity.defn(name=ATTEMPT_MEDIA_SWEEP)`,
  as `STOREFRONT_DRAFTS_SWEEP`'s), starting one-step `AttemptMedia` runs
- Create: `backend/tests/fixtures/housekeeping_histories/attempt_media.json`, as
  `auto_import.json`
- Test: `backend/tests/test_housekeeping.py`, `backend/tests/test_housekeeping_replay.py`,
  `backend/tests/orders/test_archives.py`

**Steps:**
- [ ] Failing tests: the sweep starts a run for an attempt 3 days old missing its photo;
  not for one checked 10 minutes ago; not for one 15 days old; not for one with both media.
  The replay test replays the old history with the patch.
- [ ] Implement; run `cd backend && uv run pytest tests/test_housekeeping.py tests/test_housekeeping_replay.py tests/orders -n auto`.

**Acceptance:** the tests pass; the sweep calls Bambuddy only through the workflow's step.

**Lanes:** `backend`, `image`.

### Story 6: Scan the printer for a missing timelapse, behind a setting

**Waits for story 1.** Goal: a timelapse that is on the printer and not in Bambuddy is
imported, at most once per attempt per day, never after 14 days, never with the switch off
(spec §4.3).

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py` (`scan_timelapse(archive_id)`, operation
  `archives.timelapse.scan` added to the closed `Operation` set, `Scope.MANAGE_ARCHIVES`,
  the reply model from story 1's recordings)
- Modify: `backend/scadbuddy/orders/integrations.py` (`scan_timelapse` on
  `OrderIntegrations` and `NotLinked`), `backend/scadbuddy/orders/bambuddy.py`
- Modify: `backend/scadbuddy/orders/archives.py` (`mirror_media` scans when
  `facts.timelapse` is false, the setting is on, the attempt is 14 days old or less and
  `timelapse_scanned_at` is null or a day old; stamps it; reads the archive again on the
  next step)
- Modify: `backend/scadbuddy/core/settings.py` (`orders_import_timelapse: bool = True`,
  apply mode `live`), `backend/scadbuddy/core/setting_docs.py` (label and help)
- Modify: `frontend/src/pages/settings/fields.ts`, `frontend/src/pages/settings/sections/orders.tsx`
  (the switch); `frontend/src/mocks/fixtures.ts`
- Test: `backend/tests/bambuddy/` (the client against the recordings),
  `backend/tests/orders/test_archives.py`, `frontend/src/pages/settings/sections/orders.test.tsx`

**Steps:**
- [ ] Failing tests: the client parses both recorded replies and maps the scope refusal;
  a step with no timelapse scans once and stamps; a second step the same day does not
  scan; a 15-day-old attempt does not scan; the switch off never scans; a refused scan is
  logged once and the photo still imports.
- [ ] Load `frontend-design`; failing test for the switch in Settings → Orders.
- [ ] Implement; run the backend tests above and `cd frontend && pnpm exec vitest run src/pages/settings/sections/orders.test.tsx`
  (after `pnpm gen:api`).

**Acceptance:** the tests pass; if story 1 found a loose match, the default is off and the
owner has been asked (spec §8.1).

**Lanes:** `backend`, `image`.

### Story 7: The read tells the truth, and the row loads on page open

**Goal:** The order page shows, after a reload, what each attempt has attached; offers
only what is missing; and says which of the three timelapse states applies (spec §4.4,
owner decision §8.2).

**Files:**
- Modify: `backend/scadbuddy/orders/archives.py` (`ArchiveOffer.finished_attachment_id`,
  `.timelapse_attachment_id`; `AttemptArchives.import_timelapse`)
- Modify: `frontend/src/components/orders/ArchiveCopies.tsx` (load on mount for an attempt
  with archives or a finished run; render from the answer; the **From Bambuddy…** button
  goes; re-read every 10 s, at most six times, while a medium is offered and not attached;
  the states: "Timelapse attached", "waiting for Bambuddy to process the timelapse" (no
  scan yet), "no timelapse on this print" (scanned, none), "no timelapse in Bambuddy; pull
  it from the print's page" (setting off))
- Modify: `frontend/src/components/orders/FulfilmentForm.tsx` (remount the row after a
  hand attempt is recorded)
- Modify: `frontend/src/mocks/features/orders/` (the new fields)
- Test: `backend/tests/orders/test_archives.py`, `frontend/src/components/orders/ArchiveCopies.test.tsx`,
  `FulfilmentForm.test.tsx`

**Steps:**
- [ ] Load the `frontend-design` skill.
- [ ] Failing backend test: the answer carries the ids of the attached rows and the
  setting.
- [ ] Failing frontend tests: loads on mount with no click; "attached" after a reload; the
  copy button shows only for a missing medium; each timelapse state; the re-read stops
  after six reads (fake timers).
- [ ] Implement; run `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/orders`
  and `pnpm typecheck`.

**Acceptance:** the tests pass; the mocks show an attempt in each state.

**Lanes:** `backend`, `image`.

### Story 8: Agent text and docs

**Goal:** The agent and the docs describe the import (spec §4.7, §5 Docs).

**Files:**
- Modify: `agent/src/tools/orders.ts` (`order_item_fulfil`: the import starts after the
  record and `order_attempt_archives` shows it; `order_attempt_archives`: the two ids, the
  scan state, and that a read starts an import; `order_attempt_copy`: for a chosen `name`
  and the fallback)
- Modify: `docs/superpowers/specs/2026-10-18-orders-design.md` §6.2, §8.2 ("imported; the
  copy route remains for a chosen photo and as the fallback"; the hand trigger of #6134),
  Ruling 7 (the scan), Ruling 12 (automatic writes name the attempt's creator)
- Modify: `backend/CLAUDE.md` orders section (the triggers, `AttemptMedia`, the columns,
  the switch, the trap that `GET …/archives` starts an import)
- Modify: `docs/features/orders-and-quotes.md` (one line on attempt media)
- Test: `agent/test/` tool text tests if a snapshot covers these tools (grep the tool names)

**Steps:**
- [ ] Update the text; run `cd agent && pnpm test -- orders` and `pnpm typecheck`.

**Acceptance:** no doc says "offered as one-click copies" for attempt media; the tool
text matches the routes' behaviour.

**Lanes:** `agent_durable` (any `agent/` change); `backend` and `image` for
`backend/CLAUDE.md`.

## Deviations from the spec (for the owner)

None beyond the review of 2026-11-23, which the spec records in its "Reviewed against
main" section.
