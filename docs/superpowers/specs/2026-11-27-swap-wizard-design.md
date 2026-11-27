# Swap wizard: scan or tap each spool and its new place (#7168)

Issue #7168, slice 4 of spool onboarding (epic #7171, under #6260). The owner's rulings and the
split with session c4f175d1 are under "Decisions". The plan is
`docs/superpowers/plans/2026-11-27-swap-wizard.md`.

## Intent

The owner moves spools between dry boxes, shelves and trays by hand. Today each move is a
form on the spool's page. The swap wizard is one phone page: the person names a spool, then
names its new place, and the page moves it. Then the next spool, and the next. Each spool and
each place is named by a tag tap, a QR scan of its label or a pick from a list. The page never
picks one by itself.

## Goals

- G1. A phone-first page at `/filament/swap`. It repeats two steps: "Which spool?" and "Where
  does it go?". A log under the steps lists each move done in this visit.
- G2. Each step accepts a tag tap (where Web NFC works), a QR scan of a ScadBuddy label, or a
  pick from a list. No step has a default answer (R4).
- G3. A move into a full dry box asks the person what to do: archive the box's spool, move that
  spool first, or choose another place. "Move it first" makes the box's spool the next spool
  to place, and the first spool waits for it (a swap).
- G4. Two wizards at the same time never race. Each move carries what its steps read. The
  server refuses a move whose reading no longer holds, and the page shows what changed.
- G5. The page writes reusable NTAG215 tags for a spool or a place through Web NFC. It never
  locks a tag.
- G6. Web NFC and the camera run only on ScadBuddy's own origin, top level. In the Bambuddy
  frame the page offers a link that opens it in a new tab.

## Non-goals

- `/scan` and the Bambu tag decode. They belong to c4f175d1 (#7905). The wizard does not
  change `ScanPage.tsx` or `tools/go/core/internal/tagreads`.
- A shared reader component for `/scan` and the wizard. c4f175d1 extracts one when `/scan`
  lands, and moves the wizard to it (not this issue).
- A lookup by tray UUID from the browser. It comes with c4f175d1's decode route.
- A Go port. Inventory K4 ports the move, with the field this design adds, unchanged.
- A batch move or an undo.

## Anti-goals

- A move of a spool the person did not name, or into a place the person did not name.
- An archive of a spool the person did not see (the rule of #7167, P28 and P29).
- A call to `makeReadOnly`, a tag write without a backup, or a write without a UID.
- Web NFC or the camera inside the Bambuddy iframe.

## Facts this design rests on

- **The move already refuses a stale target.** `homes.move_into`
  (`backend/scadbuddy/filament/homes.py`) locks the spools it writes, then the target place
  (`occupant_on`, `FOR NO KEY UPDATE`). A `refuse` move into a box that holds another spool is
  409 `box_full` with `occupant_id`. A `replace` names the occupant it expects
  (`expected_occupant_id`, required, else 422 `expected_occupant_required`); another occupant
  is 409 `box_full`, and nothing is archived. An occupant that left before the lock is not
  archived. A deadlock is 409 `move_conflict`. Read in the code on 2026-11-27.
- **The move does not check the spool.** `move_into` moves the spool from wherever it is now.
  It also moves an archived spool. An archive keeps `location_id` and sets `archived_at`
  only (`FilamentSpoolStore.archive`, the `UPDATE filament_spools SET archived_at = …`
  statement in `filament/spools.py`). Read in the code on 2026-11-27.
- **A label's QR text.** A spool label holds `scadbuddy:spool:<id>`. A place label holds
  `<label base>/x/L-<n>` (`filament/labels.py`, `frontend/src/components/filament/labelSheet.ts`
  `labelPayload`, `locationPayload`). Go's `scan/resolve.go` and Python's
  `GET /api/v1/codes/{code}` resolve both. Read in the code on 2026-11-27.
- **A tag's NDEF records.** `POST /filament/tags/reads` finds the spool that holds a tag by its
  UID. When no spool holds it, it reads a `url` record whose path ends `/x/<code>`, or a `text`
  record that is a code or `scadbuddy:spool:<id>` (`filament/tag_codes.py`), and answers
  `next` `open_code` with `resolved` (spec `2026-11-23-web-nfc-tags-design.md`, "As built,
  2026-11-25"). A spool's tag is written with OpenSpool or OpenTag3D records by
  `POST /spools/{id}/tag-records` and bound by `POST /spools/{id}/tag-written`.
- **Web NFC reads a Bambu tag's UID only.** A Bambu tag is MIFARE Classic. Web NFC gives its
  serial number but none of its blocks: the owner's phone scan on prod on 2026-11-25 made a tag
  of kind `other` with no records (spec `2026-11-26-bambu-tag-scan-design.md`, "Facts"). Web
  NFC handles NDEF only (https://w3c.github.io/web-nfc/). The lookup stores a phone's 4-byte
  UID as the AMS's `<uid>00000100` (#8056, `clean_tag_uid`), so a phone tap finds a spool whose
  tag the AMS saw on that side. A Bambu spool has two tags with different UIDs; the side the
  AMS never read has no row.
- **The ScadBuddy Android app** installs `NDEFReader` backed by Android NFC, NTAG writes
  included. For a MIFARE Classic tap it also puts the blocks in
  `window.ScadBuddyNfc.lastMifare`; `frontend/src/lib/nativeMifare.ts` `takeMifare(serial)`
  takes them once, and a read that carries `mifare_blocks` is decoded in Go
  (`tools/go/core/internal/tagreads`, #8161), which finds the spool by the tray UUID of block 9,
  so either side finds it. The app keeps navigation on ScadBuddy's own origin (spec
  `2026-11-26-bambu-tag-scan-design.md`, "The app (G5)"). Read in the code on 2026-11-27.
- **`IdentifySpool`** (`frontend/src/components/filament/IdentifySpool.tsx`, #7167) already
  names a spool by a tag, a QR code (camera through `BarcodeDetector`, else a photo through
  core's `POST /api/v1/scan/codes`, #8155) or a pick by hand. It forgets the spool when the
  person changes the way. `HomeReturnDialog` uses it.
- **`nfcSupport()`** (`frontend/src/lib/webNfc.ts`) answers `embedded` in a frame, `no_api`
  without `NDEFReader`, else `ok`. `NfcSession.write` always overwrites and never makes a tag
  read-only. `lib/tagWrite.ts` `writeWithBackup` reads the tag, backs it up
  (`POST /tags/backups`), writes, reads back and confirms. Its backup `reason` is one of
  `spool_write`, `agent_write`, `restore`; the table's CHECK holds the same three
  (`20261124T0258Z_filament_tag_backups.sql`).
- **`BarcodeDetector`** is not in every browser
  (https://developer.mozilla.org/en-US/docs/Web/API/BarcodeDetector). `IdentifySpool` falls back
  to the photo route (P23 of #7167).
- **Bambuddy's frame** has no `allow-downloads` and is not known to grant NFC; deep links use
  `window.open(…, '_blank')` (root `CLAUDE.md`, "Bambuddy iframe facts").

## Design

### What a step carries, and the stale refusal (R5)

A move is one `POST /filament/spools/{id}/move`. It carries what its two steps read:

- The spool step reads the spool (`Spool`: its id and its `location_id`). The move sends
  `expected_location_id`: the place the spool was in when the person named it, or `null` for
  no place.
- The place step reads the place. A move into it sends `if_occupied: refuse`, which already
  means "I read this box as empty" (409 `box_full` otherwise). When the person chose to archive
  the box's spool, the move sends `if_occupied: replace` and `expected_occupant_id`: the spool
  that the 409 or the place's page named. That is the existing rule of #7167.

The new rule. `SpoolMove` gets one optional field, `expected_location_id: UUID | None`. When
the body has the key (null included), `move_into` checks the moved spool's row as it locks it.
When the spool's `location_id` differs, or the spool is archived, the move is refused before
anything is archived or moved: `SpoolMovedError`, which the route answers as 409 with `code`
`move_stale`, `location_id` (where the spool is now, or null) and `archived` (true or false).
A body without the key keeps today's behaviour, so other callers do not change.

The page does not send `updated_at`. A weight reading or an AMS report changes `updated_at`,
and that is no reason to refuse a move.

After `move_stale` the page re-reads the spool (`GET /spools/{id}`), shows the change ("S-12 is
now in Shelf 2" or "S-12 was archived"), and asks the person to confirm the step again. It never
sends the move again by itself. An archived spool cannot move, so after `archived` true the page goes
back to "Which spool?". After `box_full` it shows the box's spool and the choices
(G3). After `move_conflict` it offers "Try again", which sends the same move with the same
readings.

Two wizards on one box: both read it empty, both send `refuse`. The box's place lock orders
them. The first moves in; the second gets `box_full` naming the first's spool. Two wizards on
one spool: both read it in box A. The first moves it to B. The second still carries
`expected_location_id` A, and gets `move_stale` with `location_id` B. A replace that archived a
spool makes any other wizard's move of that spool `move_stale` with `archived` true.

### The swap chain (G3)

The page keeps a stack of waiting moves. Each entry is a spool, the place it was read in, and
the target the person named for it.

1. The person names spool S and target T. The page sends the move.
2. `box_full` naming O. The page shows O and three choices: "Archive O" (a replace with
   `expected_occupant_id` O), "Move O first", "Choose another place".
3. "Move O first" pushes (S, T) on the stack. O becomes the current spool, read in T. The
   person names O's target.
4. Each move that succeeds pops the stack: the waiting spool's move is sent again, now that its
   target is empty. Its `expected_location_id` is the place it was read in.
5. A cycle: O's target is the box of a waiting spool W, so the move gets `box_full` naming W.
   The page offers "Take W out first". It moves W to no place (`location_id` null,
   `expected_location_id` W's place), then sends O's move again, and then W's move when its
   turn comes, with `expected_location_id` null.

The steps are separate requests. If one fails in the middle, each spool is in a real place
(or in none), the log says so, and the page shows the stack that is still waiting. "Clear"
drops the stack; the person can pick up any spool again.

### Naming a spool, naming a place (R4)

- The spool step is `IdentifySpool` as it is, with one change: its tag way posts the app's
  MIFARE blocks with the read when `takeMifare(reading.uid)` has them, so the Go route decodes
  a Bambu tag in the app. In a plain browser it posts the UID and the NDEF records, as now.
- The place step is a new `IdentifyPlace` with the same three ways:
  - Tag: `POST /tags/reads`; a read that answers `open_code` with `resolved.kind` `location`
    names the place. A read that names a spool says "This tag is a spool's tag".
  - QR code: the camera or a photo, as `IdentifySpool` does it. The text is a place label's
    URL or a typed code (`L-4`), resolved by `GET /codes/{code}`.
  - Pick by hand: the unarchived places that are not an AMS unit's place, searchable by name
    and code.
- An archived place, an AMS unit's place and a spool code are refused with a sentence.

### Which tags the page reads, and how

- An NTAG215 (NDEF) tag that ScadBuddy wrote: the page reads it through `webNfc.ts`, in Chrome
  on Android and in the app.
- A Bambu tag (MIFARE Classic), in Chrome on Android: the page reads its UID only. It names the
  spool when that UID is known (that side was seen by the AMS, or bound by hand). Else the page
  says "This side of the Bambu tag is on no spool yet. Tap the other side, use the QR code, or
  pick the spool by hand."
- A Bambu tag in the app: the read also carries the app's blocks, so either side names the
  spool by its tray UUID.
- No hand-off to `scadbuddy://scan`. The app opens ScadBuddy pages on their own origin, so the
  wizard runs inside the app as it is.

### Tag writing (R6)

- A spool's tag: after the spool step, "Write tag" runs the existing spool write
  (`WriteFlow` from `components/filament/scan/WriteTag.tsx`, OpenSpool or OpenTag3D records,
  backed up first, read back, then bound). The wizard adds nothing to that flow.
- A place's tag: after the place step, "Write tag" writes one NDEF `url` record, the place
  label's own URL `<label base>/x/L-<n>`. A tap, a QR scan and Android's own launch of the URL
  then all name the same place. It runs `writeWithBackup` with a new backup reason,
  `place_write`. `prepare` refuses a tag that a spool holds ("Unbind it from S-12 first").
  `confirm` posts the read-back as a read and checks that it names this place. Nothing is
  bound: a place has no binding table.
- The label base is the one the labels page uses: `filament_label_base_url`, else
  `public_url`, else the page's own origin (`LabelsPage.tsx`, `labelBase`).
- Never `makeReadOnly`. A tag stays reusable: a later write overwrites it, backed up first.

### Own origin (R7)

- `nfcSupport()` `embedded`: the page shows no tag or camera way. It shows "Open the swap page
  in its own tab" (`openExternal(window.location.href)`, `lib/embed.ts`). QR by photo and pick
  by hand stay. The camera is not offered in the frame.
- `no_api` (iOS, desktop): no tag way. The page says "Tags need Chrome on Android or the
  ScadBuddy app." QR and pick by hand stay.

### Where the page is linked

- Route `/filament/swap`, page `SwapPage.tsx`, `<h1>` "Swap spools".
- A "Swap spools" button on the Spools tab's toolbar.

## Decisions

Binding rulings, restated:

- R1 (owner, 2026-11-26). The wizard is Python, on the existing spool move, with a stale-step
  refusal. Inventory K4 ports it to Go unchanged. A brand-new route would be Go. This design
  adds no route: one optional field on `SpoolMove` and one 409 code.
- R2 (split with c4f175d1, fleet DM #244, 2026-11-26). `/scan` and the Bambu decode are
  c4f175d1's. The wizard is its own page and route. It reads tags through `webNfc.ts`; in the
  app, a native polyfill backs it, NTAG215 writes included. A shared reader comes later, from
  c4f175d1. Corrected by the code (c4f175d1's report of 2026-11-27, checked): a Bambu tap in a
  browser gives the UID only, and the UID names the spool only for a side the AMS saw; in the
  app the read carries the MIFARE blocks, and the Go route finds the spool by tray UUID. See
  "Which tags the page reads".
- R3 (#7167, PR #8270). The wizard reuses `move` and its answers (`box_full`, `replace`,
  `expected_occupant_id`, `move_conflict`). It re-implements none of them.
- R4 (owner, on #7167). Every step needs a selection: a tag, a QR code or a pick by hand.
- R5. Each step carries what it read; a stale step is refused (above).
- R6. Reusable NTAG215 tags through Web NFC; never `makeReadOnly`; feature-detect.
- R7. Own origin, top level; in the frame, a link to a new tab.
- R8. Phone-first; `frontend-design:frontend-design` in every UI task; mocks cover every server
  path, `move_stale` included; vitest; the page-titles e2e lists the new route.
- R9. Tests do not depend on runner speed or the date.

My calls:

- D1 (my call). The spool step carries `location_id` and the archived state, not `updated_at`.
  A weight or AMS update must not make a step stale.
- D2 (my call). The field is `expected_location_id`, checked only when the key is in the body
  (`model_fields_set`), so null means "it was in no place". Other callers do not change.
- D3 (my call). The refusal is 409 `move_stale` with `location_id` and `archived`. It is
  checked under the spool's row lock, before `answers`, the replace and the move.
- D4 (my call). The target's reading is the existing one: `refuse` means "empty", and
  `replace` names `expected_occupant_id`. No new target field.
- D5 (my call). After `move_stale` the page re-reads and asks again. It never resends by
  itself. `move_conflict` offers "Try again" with the same readings.
- D6 (my call). The swap chain is a stack on the page, not a server batch. Each step is a
  separate move; a failure in the middle leaves every spool in a real place or in none, and
  the log says which.
- D7 (my call). A place tag is one NDEF `url` record, the place label's URL. It is the format
  that `tag_codes.py` already reads, and the phone's own NFC launch opens the same place.
- D8 (my call). Place writes take a new backup reason, `place_write` (a new migration widens
  the CHECK). A place tag is never bound; its confirm is a read that names the place.
- D9 (my call). `IdentifySpool`'s tag way posts `takeMifare` blocks when the app has them.
  `HomeReturnDialog` gains the same in-app Bambu lookup.
- D10 (my call). `CameraReader` and `detectorClass` move out of `IdentifySpool.tsx` into
  `components/filament/identify/CameraReader.tsx`, so `IdentifyPlace` shares them. No change in
  behaviour.
- D11 (my call). Route `/filament/swap`, linked from the Spools tab. Not under `/scan`.
- D12 (my call). The agent's `filament_spool_move` tool does not get the new field in this
  issue. An agent reads and moves in one turn; the page is where minutes pass between steps.
- D13 (my call). The race tests in Python hold one transaction open and wait for the second
  session to block on a lock. They wait through `tests/filament/lock_support.py` `waits_for`
  (the #7167 helper: `pg_stat_activity` with `pg_blocking_pids`), so no test depends on how
  fast the runner is.

## Risks

- A Bambu spool's second side reads as an unknown tag in a browser. The page says what to do;
  the app or the QR code works.
- A swap stack that fails in the middle leaves a spool in no place. The log and the stack show
  it, and the person moves it again.
- The OpenAPI diff adds a field and a code; the `freshness` job shows it on the PR.
