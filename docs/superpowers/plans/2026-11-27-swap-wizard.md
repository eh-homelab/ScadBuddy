# Swap Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **UI work (Tasks 5, 6 and 7):** the implementer loads the `frontend-design:frontend-design`
> skill before writing any UI.

**Goal:** A phone page at `/filament/swap` where the owner names each spool and its new place by a tag tap, a QR scan or a pick by hand, and a move whose readings went stale is refused.

**Architecture:** The page sends the existing `POST /filament/spools/{id}/move`. `SpoolMove` gets one optional field, `expected_location_id`; `homes.move_into` checks it under the spool's row lock and raises `SpoolMovedError`, which the route answers as 409 `move_stale`. A full box keeps the #7167 answers (`box_full`, `replace` with `expected_occupant_id`, `move_conflict`). The page keeps a stack of waiting moves for swaps. Tags go through `lib/webNfc.ts`; a place tag is one NDEF `url` record, written with a new backup reason `place_write`.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, pytest; React 19, vitest, msw, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-27-swap-wizard-design.md` (binding). Issue #7168, epic #7171.

## Global Constraints

- No new route (R1). The only API changes: the `SpoolMove` field `expected_location_id`, the 409 code `move_stale` with `location_id` and `archived`, and the backup reason `place_write`.
- Only the API process touches SQL (root `CLAUDE.md`, "Database access").
- Migrations are new files only: `$(date -u +%Y%m%dT%H%MZ)_filament_place_tag_backups.sql` in `backend/scadbuddy/migrations/`, named with that command when the task runs. Never edit a merged file.
- Never call `makeReadOnly`. Never write a tag without a backup or without a UID.
- No step has a default answer: a spool and a place are always named by a tag, a QR code or a pick by hand (R4).
- No Web NFC and no camera when `nfcSupport()` is `embedded`; the page links to itself in a new tab with `openExternal` (`frontend/src/lib/embed.ts`).
- Do not change `frontend/src/pages/filament/ScanPage.tsx` or `tools/go/core/internal/tagreads` (c4f175d1's, R2).
- Fixed copy: page `<h1>` "Swap spools"; steps "Which spool?" and "Where does it go?"; box choices "Archive S-<n>", "Move S-<n> first", "Choose another place"; cycle choice "Take S-<n> out first"; frame link "Open the swap page in its own tab"; no-NFC note "Tags need Chrome on Android or the ScadBuddy app."; Bambu unknown side "This side of the Bambu tag is on no spool yet. Tap the other side, use the QR code, or pick the spool by hand."
- Tests never depend on runner speed or the date (R9). A race test waits for a lock (`pg_stat_activity.wait_event_type = 'Lock'`), never for a delay.
- Locally run only lint, types and the focused tests named in each task (memory "Offload tests to CI"). Backend tests need a Postgres in Docker on a unique port, as `CLAUDE.local.md` says.
- Simplified Technical English in docs and copy. No counts in docs.

## Review Focus

1. A spool with no place (`location_id` null) read by the page: the move must send `"expected_location_id": null` explicitly, and the server must check it. A missing key means "no check". Task 1 and Task 3 test both.
2. A spool archived by another wizard's replace while this wizard holds it: the archive keeps `location_id`, so only the `archived` check catches it. Task 1 tests it.
3. A tag that a spool holds, tapped on the place step: it must not name a place, and a place write onto it must be refused before anything is written. Tasks 5 and 6 test it.
4. The swap cycle (two full boxes, each spool going into the other's box): the page must take one spool out first, and each later move must carry its new reading (null). Task 4 tests it.
5. The page in the Bambuddy frame: no `NDEFReader` call and no `getUserMedia` call at all, even when the API exists. Task 7 tests it.

---

### Task 1: The stale refusal on the move

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolMove`, near line 842)
- Modify: `backend/scadbuddy/filament/homes.py` (`move_into`, `move`, a new `SpoolMovedError`)
- Modify: `backend/scadbuddy/api/filament.py` (`_refused`, `move_spool` and its description, a constant `MOVE_STALE`)
- Test: `backend/tests/filament/test_homes.py`, `backend/tests/api/test_filament_dry_box.py`

**Interfaces:**
- Produces: `SpoolMove.expected_location_id: UUID | None = None`, checked only when `"expected_location_id" in body.model_fields_set`.
- Produces: `homes.SpoolMovedError(ValueError)` with `.location_id: UUID | None` and `.archived: bool`.
- Produces: `move_into(..., expected_location_id: UUID | None = None, check_location: bool = False)` and the same two keywords on `move`.
- Produces: the route's 409 `{"code": "move_stale", "location_id": "<uuid>" | null, "archived": bool}`.

- [ ] **Step 1: Write the failing tests** in `test_homes.py`:

```python
def test_a_move_whose_spool_left_its_place_is_stale(pg_pool):
    # spool read in shelf A, then moved to shelf B by someone else
    with pytest.raises(homes.SpoolMovedError) as err:
        _move(pg_pool, spool, box, expected_location_id=shelf_a, check_location=True)
    assert err.value.location_id == shelf_b and err.value.archived is False
    assert _row(pg_pool, spool)["location_id"] == shelf_b  # nothing moved

def test_a_spool_read_in_no_place_is_checked_too(pg_pool):
    # expected None, spool now in shelf B -> SpoolMovedError(location_id=shelf_b)

def test_an_archived_spool_is_stale_though_its_place_is_the_same(pg_pool):
    # spool in shelf A, archived; expected shelf A -> SpoolMovedError(archived=True)

def test_a_stale_replace_archives_nothing(pg_pool):
    # box holds O; mover read in A but now in B; replace with expected_occupant_id=O
    # -> SpoolMovedError; O is unarchived and still in the box

def test_no_expected_location_is_todays_move(pg_pool):
    # check_location=False: the spool moves from wherever it is
```

Race tests (D13), in `test_homes.py`, each with two threads and the lock-wait poll of `tests/filament/test_dry_box.py::test_a_move_that_waited_on_a_kind_flip_sees_the_box`:

```python
def test_two_wizards_on_one_spool_the_second_is_stale(pg_pool):
    # both read spool S in A. Wizard 1 runs move_into(S -> B, expected A) in an open
    # transaction; wizard 2 runs move_into(S -> C, expected A) and blocks on S's row;
    # wizard 1 commits. outcome == ["moved", "stale:B"]; S is in B.

def test_two_wizards_on_one_box_the_second_meets_box_full(pg_pool):
    # empty box X; S1 and S2 both read in their shelves. Wizard 1 moves S1 -> X (open
    # transaction); wizard 2 moves S2 -> X with refuse and blocks on X's place lock;
    # wizard 1 commits. Wizard 2 raises BoxFullError naming S1; S2 stays on its shelf.

def test_a_replace_and_a_move_of_the_occupant_race(pg_pool):
    # box X holds O. Wizard 1: replace S -> X, expected_occupant_id O (open transaction,
    # O archived). Wizard 2: move O -> shelf, expected X, blocks on O's row; wizard 1
    # commits. Wizard 2 raises SpoolMovedError(archived=True).
```

API tests in `test_filament_dry_box.py`:

```python
def test_a_stale_move_is_a_409_move_stale(client):
    # body {"location_id": box, "expected_location_id": shelf_a}; spool is in shelf_b
    assert r.status_code == 409
    assert r.json()["code"] == "move_stale"
    assert r.json()["location_id"] == str(shelf_b) and r.json()["archived"] is False

def test_an_explicit_null_expected_location_is_checked(client):
    # body {"location_id": box, "expected_location_id": None}; spool is in shelf_b -> 409 move_stale
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_homes.py tests/api/test_filament_dry_box.py -k "stale or wizards or race or expected_location" -v`
Expected: FAIL (`SpoolMovedError` not defined; 200 instead of 409).

- [ ] **Step 3: Implement**

`move_into` reads `location_id` and `archived_at` in the existing `SELECT … FOR UPDATE` of the wanted spools. With `check_location`, it raises `SpoolMovedError` for `spool_id` when `location_id IS DISTINCT FROM expected_location_id` or `archived_at IS NOT NULL`, right after that lock and before the `answers`, replace and move code. `move` passes both keywords through. The route passes `check_location="expected_location_id" in body.model_fields_set`, adds `SpoolMovedError` to the caught tuple, and `_refused` maps it to `ApiError(409, str(exc), code=MOVE_STALE, location_id=str|None, archived=bool)`. The route's description gets one sentence for the field and the 409; `SpoolMove`'s field comment says the same. The module docstring of `homes.py` names the check.

- [ ] **Step 4: Run the tests to see them pass**

Run: the Step 2 command, then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: PASS, no lint or type error.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/filament/models.py backend/scadbuddy/filament/homes.py backend/scadbuddy/api/filament.py backend/tests/filament/test_homes.py backend/tests/api/test_filament_dry_box.py
git commit -m "feat(filament): a move carries where it read the spool; a stale one is 409 move_stale (#7168)"
```

### Task 2: The `place_write` backup reason

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_filament_place_tag_backups.sql`
- Modify: `backend/scadbuddy/filament/models.py:2212` (`BackupReason`)
- Modify: `agent/src/tools/filament.ts:1326` (the reasons listed in the tool's description)
- Test: `backend/tests/filament/test_tags.py`

**Interfaces:**
- Produces: `BackupReason = Literal["spool_write", "agent_write", "restore", "place_write"]`; `POST /filament/tags/backups` takes `reason: "place_write"`.

- [ ] **Step 1: Write the failing test** `test_a_place_write_backup_is_stored`: `back_up(store, TagBackupCreate(uid=UID, records=[], reason="place_write"), actor="t")` answers a `backup_id`, and the row's `reason` is `place_write`.
- [ ] **Step 2: Run it to see it fail.** Run: `cd backend && uv run --frozen pytest -k place_write_backup -v`. Expected: FAIL (validation error).
- [ ] **Step 3: Implement.** The migration drops and re-adds the CHECK on `filament_tag_backups.reason` (find its name with `\d filament_tag_backups` on the test database; Postgres names an inline CHECK `filament_tag_backups_reason_check`) with the four values. Widen `BackupReason`. Add `place_write` (a place's tag, written by the swap page) to the agent tool's description.
- [ ] **Step 4: Run it to see it pass**, plus `uv run --frozen mypy` and `cd agent && pnpm lint`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): a backup reason for a place's tag (#7168)`.

### Task 3: The frontend move client and the mocks

**Files:**
- Modify: `frontend/src/api/filament.ts:385-402` (`MoveOptions`, `moveSpool`)
- Modify: `frontend/src/lib/tagWrite.ts:57` (`WriteOptions.reason` gets `'place_write'`)
- Modify: `frontend/src/mocks/features/filament/index.ts` (`FIELDS.move` at line 385, the move handler near line 3002)
- Test: `frontend/src/mocks/features/filament/index.test.ts`

**Interfaces:**
- Consumes: Task 1's 409 body, Task 2's reason.
- Produces: `moveSpool(id, locationId, { …, expectedLocationId?: string | null })`. `undefined` omits the key; `null` sends `"expected_location_id": null`.
- Produces: `staleOf(cause: unknown): { locationId: string | null; archived: boolean } | null` in `frontend/src/api/filament.ts`, for a 409 `move_stale`.

- [ ] **Step 1: Write the failing mock tests**

```ts
it('refuses a move whose spool left the place it was read in', async () => {
  // move a seeded spool to shelf B first, then:
  const cause = await moveSpool(id, boxId, { expectedLocationId: shelfA }).catch((e) => e)
  expect(staleOf(cause)).toEqual({ locationId: shelfB, archived: false })
})
it('checks an explicit null expected place', …)   // spool in shelf B, expected null -> stale
it('refuses a move of an archived spool as stale', …) // archived: true
it('keeps today\'s move when no expected place is sent', …)
```

- [ ] **Step 2: Run them to see them fail.** Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/mocks/features/filament/index.test.ts -t "expected place|stale"`. Expected: FAIL.
- [ ] **Step 3: Implement.** The mock adds `'expected_location_id'` to `FIELDS.move` and, when `'expected_location_id' in sent`, answers `problem(409, …, { code: 'move_stale', location_id, archived })` before the `answers` and box checks, as `homes.py` does. `moveSpool` sends the key when `expectedLocationId !== undefined`.
- [ ] **Step 4: Run them to see them pass**, plus `pnpm exec tsc -p tsconfig.app.json --noEmit` and `pnpm lint`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): the move client and mocks carry the spool's read place (#7168)`.

### Task 4: The swap stack

**Files:**
- Create: `frontend/src/lib/swapChain.ts`
- Test: `frontend/src/lib/swapChain.test.ts`

**Interfaces:**
- Produces:

```ts
export interface Held { spool: Spool; readIn: string | null }       // the spool and where it was read
export interface Waiting extends Held { target: string | null }
export interface Chain { current: Held | null; waiting: Waiting[] }   // waiting[0] is the oldest
export type Next =
  | { kind: 'move'; spoolId: string; target: string | null; expectedLocationId: string | null;
      ifOccupied: 'refuse' | 'replace'; expectedOccupantId?: string }
  | { kind: 'name_target' }                                        // ask "Where does it go?"
  | { kind: 'done' }
export function moveFirst(chain: Chain, target: string, occupant: Spool): Chain   // push current with target; occupant read in target
export function takeOutFirst(chain: Chain, waitingSpoolId: string): Next          // the null move of that waiting spool
export function afterMove(chain: Chain, moved: Spool): { chain: Chain; next: Next } // update readIn of moved; pop the next waiting
export function isWaiting(chain: Chain, spoolId: string): boolean
```

- [ ] **Step 1: Write the failing tests**
  - `moveFirst` then `afterMove(occupant moved to shelf)` gives `next` = the waiting spool's move into the box, `expectedLocationId` its `readIn`, `ifOccupied` `'refuse'`.
  - The cycle: S read in A, target B; B holds O; `moveFirst`; O's target A meets S: `isWaiting(chain, S.id)` is true; `takeOutFirst` gives `{ spoolId: S.id, target: null, expectedLocationId: A }`; `afterMove(S in no place)` sets S's `readIn` to null and gives O's move again; `afterMove(O in A)` gives S's move to B with `expectedLocationId: null`.
  - `afterMove` with nothing waiting gives `{ kind: 'done' }` and `current: null`.
- [ ] **Step 2: Run them to see them fail.** Run: `cd frontend && pnpm exec vitest run src/lib/swapChain.test.ts`. Expected: FAIL (module not found).
- [ ] **Step 3: Implement** the pure functions. No React, no fetch.
- [ ] **Step 4: Run them to see them pass.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): the swap wizard's stack of waiting moves (#7168)`.

### Task 5: Naming a place, and the app's Bambu blocks on a spool tap

Load `frontend-design:frontend-design` first.

**Files:**
- Create: `frontend/src/components/filament/identify/CameraReader.tsx` (moved from `IdentifySpool.tsx`: `BarcodeDetectorLike`, `BarcodeDetectorClass`, `detectorClass`, `CameraReader`; no change in behaviour)
- Modify: `frontend/src/components/filament/IdentifySpool.tsx` (imports the move; `TagWay` posts `takeMifare(reading.uid) ?? { uid, tag_type: null, records }`; a `bind_or_create` result whose read was a 4-byte MIFARE UID with no records shows the Bambu unknown-side copy)
- Create: `frontend/src/components/filament/IdentifyPlace.tsx`
- Test: `frontend/src/components/filament/IdentifySpool.test.tsx`, `frontend/src/components/filament/IdentifyPlace.test.tsx`

**Interfaces:**
- Consumes: `postTagRead`, `resolveCode`, `getLocation`, `listLocations`, `decodeScan`, `storeScanImage`, `nfcSupport`, `NfcSession`, `takeMifare`.
- Produces: `IdentifyPlace({ onIdentified: (place: LocationDetail | null) => void; nfc: 'ok' | 'no_api' | 'embedded' })`. `IdentifySpool` gains the same `nfc` prop (default `nfcSupport()`), so the page decides once.

- [ ] **Step 1: Write the failing tests** (msw handlers and `test/ndefStub.ts`):
  - `IdentifyPlace`: a tag read that answers `open_code` with `resolved.kind: 'location'` names that place; one that answers `open_spool` shows "This tag is a spool's tag." and names none; a QR text `https://sb.example/x/L-4` names L-4; a typed `S-12` says it is a spool; an archived place is refused; pick by hand lists no AMS unit's place; changing the way calls `onIdentified(null)`; with `nfc: 'embedded'` neither the tag way nor the camera button renders.
  - `IdentifySpool`: with `window.ScadBuddyNfc.lastMifare` set for the tapped serial, the read posts `mifare_blocks`; without it, the body has no `mifare_blocks`; the existing tests still pass.
- [ ] **Step 2: Run them to see them fail.** Run: `cd frontend && pnpm exec vitest run src/components/filament/IdentifyPlace.test.tsx src/components/filament/IdentifySpool.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement.** `IdentifyPlace` follows `IdentifySpool`'s layout (a `ToggleGroup` of the ways, no way chosen at first) and resolves every text through `resolveCode`, then `getLocation`.
- [ ] **Step 4: Run them to see them pass**, plus `pnpm exec vitest run src/components/filament/HomeReturnDialog.test.tsx`, typecheck and lint. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): name a place by tag, QR or hand; post the app's Bambu blocks (#7168)`.

### Task 6: Writing a place's tag

Load `frontend-design:frontend-design` first.

**Files:**
- Create: `frontend/src/lib/placeTag.ts`
- Create: `frontend/src/components/filament/WritePlaceTag.tsx`
- Test: `frontend/src/lib/placeTag.test.ts`, `frontend/src/components/filament/WritePlaceTag.test.tsx`

**Interfaces:**
- Consumes: `writeWithBackup` (reason `'place_write'`), `locationPayload`, `labelBase` (`components/filament/labelSheet.ts`), the label base as `LabelsPage.tsx` reads it from the settings, `postTagRead`.
- Produces: `placeRecords(url: string): WireRecord[]` (one record, `record_type: 'url'`, `data_hex` the UTF-8 hex of `url`); `writePlaceTag(session: NfcSession, place: LocationDetail, base: string, hooks: RunHooks): Promise<WriteOutcome>`; `WritePlaceTag({ session, place })`.

- [ ] **Step 1: Write the failing tests**
  - `placeRecords('https://sb.example/x/L-4')` is one `url` record whose `data_hex` decodes to that URL.
  - `writePlaceTag`: the stub's tag is backed up with `reason: 'place_write'`, then written with that record, and the outcome is `checked` when the read-back's `postTagRead` answers `open_code` for this place.
  - A tag whose first read answers `open_spool` for S-12 fails with "Unbind it from S-12 first." and the stub saw no `write`.
  - A read-back that names another place is `failed`.
  - The stub's `makeReadOnly` is never called in any test (assert its call count is 0).
- [ ] **Step 2: Run them to see them fail.** Run: `cd frontend && pnpm exec vitest run src/lib/placeTag.test.ts src/components/filament/WritePlaceTag.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement.** `prepare` posts the read and throws for a held tag; `confirm` posts the read-back and checks `resolved.id`. `WritePlaceTag` reuses `WriteFlow`'s steps view from `components/filament/scan/WriteTag.tsx` with label "Place tag".
- [ ] **Step 4: Run them to see them pass**, typecheck and lint. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): write a place's tag as its label URL (#7168)`.

### Task 7: The swap page

Load `frontend-design:frontend-design` first.

**Files:**
- Create: `frontend/src/pages/filament/SwapPage.tsx`
- Modify: `frontend/src/App.tsx` (lazy `SwapPage`, `<Route path="filament/swap" …>` beside `filament/labels`)
- Modify: `frontend/src/pages/filament/SpoolsTab.tsx` (a "Swap spools" link button in the toolbar near line 451)
- Modify: `frontend/e2e/page-titles.spec.ts` (`'/filament/swap'` in `PAGES`)
- Modify: `docs/features/spools-and-filament.md` (a "### The swap wizard (#7168)" section after "The phone page, the hand-held Scan mode")
- Test: `frontend/src/pages/filament/SwapPage.test.tsx`

**Interfaces:**
- Consumes: `IdentifySpool`, `IdentifyPlace` (Task 5), `WriteFlow` for a spool's tag, `WritePlaceTag` (Task 6), `moveSpool`, `staleOf`, `getSpool`, `problemCode` (Task 3), the `swapChain` functions (Task 4), `nfcSupport`, `openExternal`.

- [ ] **Step 1: Write the failing tests** (msw, seeded spools and places):
  - A plain move: name a spool, name a shelf, press "Move"; the request body has `expected_location_id` equal to the spool's `location_id` as read (and `null` for a spool with no place); the log shows "S-<n> → <shelf>".
  - Stale: after the spool step, the test moves that spool in the mock state (another wizard); the move answers `move_stale`; the page shows "S-<n> is now in <place>" and a confirm button, and sent no second move until it is pressed.
  - Archived: the same with an archive; the page shows "S-<n> was archived" and returns to "Which spool?".
  - Full box: `box_full` shows the choices; "Archive S-<o>" sends `replace` with `expected_occupant_id`; "Move S-<o> first" asks "Where does it go?" for the occupant, and after its move the waiting spool's move goes out by itself with its read place.
  - The cycle: two seeded full boxes; S into O's box, "Move first", O into S's box shows "Take S-<n> out first"; the three requests go out in order with `expected_location_id` A, B, then null.
  - `move_conflict` shows "Try again", which resends the same body.
  - Embedded (`window.self !== window.top` stubbed): the page shows "Open the swap page in its own tab", and `NDEFReader` and `navigator.mediaDevices.getUserMedia` stubs are never called.
  - `no_api`: the note "Tags need Chrome on Android or the ScadBuddy app." shows, and the QR and hand ways are there.
- [ ] **Step 2: Run them to see them fail.** Run: `cd frontend && pnpm exec vitest run src/pages/filament/SwapPage.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement.** One column, large touch targets, the current step at the top, the waiting stack under it, the log at the bottom. `nfcSupport()` is read once and passed down. The docs section says what the page does, the stale rule, the tag rules and the frame rule, with links to the spec.
- [ ] **Step 4: Run the tests to see them pass**, then `pnpm exec playwright test e2e/page-titles.spec.ts`, typecheck and lint. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): the swap wizard page (#7168)`.

## Self-review

- Spec coverage: G1 Task 7; G2 Tasks 5 and 7; G3 Tasks 4 and 7; G4 Tasks 1, 3 and 7 (D1-D5); G5 Tasks 2, 6 and 7; G6 Task 7; the tag-read rule Task 5 (D9); D10 Task 5; D11 Task 7; D12 and the Go port need no task.
- Types: `expectedLocationId` (TS) maps to `expected_location_id` (wire, Python); `SpoolMovedError.location_id`/`.archived` map to the 409's `location_id`/`archived` and to `staleOf`'s `locationId`/`archived`. `Next.move` carries the same option names as `moveSpool`.
- Review Focus: item 1 Tasks 1 and 3, item 2 Task 1, item 3 Tasks 5 and 6, item 4 Task 4, item 5 Task 7.
