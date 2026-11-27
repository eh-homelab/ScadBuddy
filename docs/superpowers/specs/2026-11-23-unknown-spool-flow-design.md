# Unknown spool flow

Issue #7143. Slice 2 of the spool onboarding epic
[#7171](https://github.com/eh-homelab/ScadBuddy/issues/7171) (under the filament epic #6260),
after the printer and AMS labels (#6955, `2026-11-22-printer-location-labels-design.md`). Approved by the owner on
2026-11-23.

## Intent

When the owner loads a spool that ScadBuddy does not know, ScadBuddy must say so at once
and make it easy to tell it which spool it is. Most of the time the spool already exists
(it came from an invoice), so the common path is "link it". Until the owner answers,
ScadBuddy must not print from that printer, because its grams and costs would go to no spool.

## What happens today (measured on origin/main b7a47581b, 2026-11-23)

- Capture (`filament/capture.py` `reconcile`) asks `FilamentInventory.holder_on`
  (`filament/inventory.py`) which spool holds a loaded tray: an open tag binding for the
  tray's `tray_uuid`, then its `tag_uid`; else the spool placed in that tray's place whose
  `tray_fingerprint` (material and colour) matches. A loaded tray that matches nothing is
  ignored: no event, no row, no notice.
- A tray is loaded when `bambuddy/trays.py` `_filled` says so (a `tray_type`, `exists`,
  and the spool-loaded state bits).
- The print dialog's `tray_questions` asks about an untagged empty-handed tray only when
  close shelf candidates exist; a tagged tray that matches nothing is skipped.
- `prepare_run` (`bambuddy/print_run.py`) refuses a run before upload with
  `RunRefusalError` (422); "Slot N has no spool chosen" is one such refusal.
  `PrintActivities.lan_print` probes the printer right before a LAN start.
- "Needs you" (`frontend/src/lib/needsYou.ts`) lists agent requests, flow grants and
  pairings; it has no filament source.
- A spool's `label_weight_g` is nullable and overrides its type's. There is no "first
  weight" rule. Invoice confirm (`InvoiceStore.confirm`) makes spools with
  `grams_left = label_g` and no place.
- Tray links: `FilamentInventory.link` / `unlink` (`PUT/DELETE
  /printers/{id}/trays/{ams}/{tray}/spool`).

## Goals

1. An unknown tray is recorded the moment capture sees it, and cleared when the tray
   empties or a spool is matched to it.
2. An unknown tray with an RFID tag (`tray_uuid` or `tag_uid`) gets a spool made from the
   tag and linked at once. The owner is told; nothing is blocked.
3. An unknown tray without RFID shows as a "Needs you" item and in the bell, naming the
   printer and slot ("Unknown spool in H2C AMS-A slot 2").
4. The owner resolves it by a typed code (`S-142`, any case, dash optional, as
   `codes/parse.py`), a scanned spool label, or a pick list. The pick list puts spools with
   no place first (invoice spools waiting to be loaded), ordered by how well material and
   colour match the tray. "Add new" opens the invoice upload; the spools it makes then
   appear in the pick list.
5. The resolve step takes the spool's net weight, prefilled from the AMS remain % times the
   label weight when the tray reports one, typed otherwise. If it differs from the type's
   label weight, it becomes the spool's own `label_weight_g`. Either way it is booked as the
   spool's first weight reading.
6. While a printer has an unresolved unknown tray, ScadBuddy's own print paths refuse
   prints to that printer: the print dialog says why, `prepare_run` refuses with 422
   before any upload, and the LAN worker refuses at its last check before start.
7. The agent can list unknown trays and resolve one.

## Non-goals

- Prints started in Bambuddy or on the printer are not blocked (owner, 2026-11-22: "only
  ScadBuddy's queue").
- Dry boxes, a spool's home, waste on archive (slice 3).
- The scan/tap swap wizard and Web NFC (slice 4). Scanning here is the phone camera opening
  a label's `/x/S-<n>` URL or the owner typing the code.
- Push notifications to a closed browser. The item shows in the app; push can come later.
- A tray loaded between `prepare_run` and the LAN start, when the worker's CRUD read of the
  block fails at `lan_print`: the start goes on (logged). `prepare_run` checked moments
  before, and a failed read must not stop every print; such a print books its grams to no
  spool.

## Anti-goals

- A tray that ScadBuddy can match must never be reported unknown. A false block of the
  printer is worse than a late notice.
- Never guess silently: a spool without RFID is never linked by a heuristic alone.

## Constraints

- Only the API touches the database (#5295). Capture already runs inside the API's report
  write (`printers/reports.py`, in a savepoint). The LAN worker reads the block through a
  CRUD route with a service credential.
- New migration files only; event kinds stay as they are unless a new kind is needed (the
  CHECK on `filament_spool_events.kind` is dropped and re-added in the same file).
- Every capture write stays inside capture's savepoint; a failure in the unknown-tray step
  must never fail the report write.

## Approaches

1. **A table of unknown trays, written by capture (chosen).** One row per printer, AMS and
   tray while it is unknown, with what the tray reported. Capture inserts or clears it on
   each reconcile. The UI, the agent and the print block all read one place. Costs: a
   table and a migration.
2. **Compute unknown trays on read** from the latest report and the holders. No new
   state, but every print check and every bell refresh would re-run the match over every
   report, and an item would have no stable id to resolve against.
3. **Ledger events only** (an `unknown_loaded` event). Events attach to spools; an unknown
   tray has no spool, so the event has no home.

## Design

### Data

`filament_unknown_trays`: `id`, `printer_id` (FK printers, cascade), `ams_id`, `tray_id`,
`tray_uuid`, `tag_uid`, `material`, `colour_rgba`, `remain_pct` (nullable),
`first_seen_at`, `seen_at`. Unique `(printer_id, ams_id, tray_id)`. A row exists while the
tray is loaded and unmatched; resolving, dismissing (D10) or emptying deletes it. Three
cases keep a row as it is: a tray that is loading, an AMS unit the report no longer shows
(the owner can dismiss its rows), and a tray whose lock is busy (D11). The row's
`seen_at` and reported fields are the resolve's guard.

### Capture

In `reconcile`, for each loaded tray with no holder:
- RFID present: make a spool of the tag's type (the existing type match by material and
  colour; with no match, a spool with no type, as the Bambuddy pull makes, see D6), link it to the tray
  (`FilamentInventory.link`), place it, and book `created` and `loaded`. No row.
- No RFID: upsert the row (refresh `seen_at` and the reported fields).
For each tray with a holder, or not loaded: delete its row if one exists.

### API

- `GET /filament/unknown-trays`: the rows, each with the printer's name, the slot's
  place label and code, and up to 10 ranked candidates (unplaced spools first, then by
  material and colour distance).
- `POST /filament/unknown-trays/{id}/resolve` `{spool_id | code, net_weight_g,
  seen_at}`: refused 409 `tray_changed` when the row's `seen_at` differs or the row is
  gone; 404 for an unknown code. In one transaction: link the spool to the tray, place it,
  book the weight (see Goals 5), delete the row.
- CRUD route for the worker (see D7): `GET /api/v1/internal/crud/print/filament/printers/{id}/unknown-trays`
  (service credential), answering the count.

### Print block

- `prepare_run`: refuse `RunRefusalError` "<printer> has an unknown spool in <slot>:
  link it on the Filament page first" when the printer has a row.
- `_native_check` / `check_print`: the same as advice, so the dialog shows it before Print.
- `PrintActivities.lan_print`: read the CRUD route before the start; refuse the same way.

### UI

- `NeedsYouItem` kind `unknown_spool`, read from `GET /filament/unknown-trays` on the
  same cadence as the other sources; it opens the resolve dialog.
- Resolve dialog: the slot, what the tray reports (material, colour swatch), a code field
  that also accepts a scanned `/x/S-<n>` URL, the ranked pick list, the net weight field,
  "Add from an invoice" (opens the Invoices tab upload). It is also reachable from the
  slot's place page and the Locations tab.
- The print dialog shows the block with a link to the resolve dialog.

### Agent

`filament_unknown_trays` (read) and `filament_unknown_tray_resolve` (write tier).

## Decisions made while planning (2026-11-23)

- D1. A row's `seen_at` moves only when that row's reported fields change. Capture runs on
  any tray change of the printer, so a swap in another slot must not refuse a resolve.
- D2. The external holder (ams 255) gets no row: it has no presence state, so a type set on
  it with no spool would block the printer for good.
- D3. `remain_pct` is reported only for trays with RFID keys (`trays._remain`), and those
  are made automatically, so the net-weight prefill seldom fires; the owner types the
  weight. `_remain` is not changed.
- D4. The net weight sets the spool's `label_weight_g` only for a spool with no reading,
  use or load yet (a new spool); a weighed event is always booked. Otherwise a half-used
  spool's grams would become its label weight.
- D5. "The owner is told" about a spool made from a tag: the list route answers
  `made_from_tags` (the last 24 h) and the app shows each one once as a toast.
- D6. "A new type when none matches" was wrong about the Bambuddy pull: it makes a spool
  with no type, and so does this (the type is set later, as for any untyped spool).
- D7. The worker's CRUD path sits under `/print`, as every print-worker route does:
  `/api/v1/internal/crud/print/filament/printers/{id}/unknown-trays`.
- D8. An RFID tray still gets a (blocking) row in two cases: when making or linking its
  spool fails, and when no location kind is flagged `ams` (there is no place to put the
  spool). Goal 2's "nothing is blocked" holds whenever the spool can be made and placed.
- D9. A tray that Bambuddy assigns to a linked, unarchived ScadBuddy spool that agrees
  with it is matched, not unknown (the anti-goal). The print picker already reads that
  assignment (`bambuddy/filaments.py` `read_inventory`). Two changes keep it from
  blocking: the pull's `FilamentInventory.reconcile_trays` (#6713) also places such a
  spool into an untagged tray that holds no placed spool, and clears the tray's row in
  the same transaction; and on the Bambuddy path, `prepare_run` leaves out a row whose
  tray the run's own inventory read gives an SB spool. The native path has no Bambuddy
  read, so there a row blocks until the next pull places the spool.
- D10. A row can be dismissed (`POST /filament/unknown-trays/{id}/dismiss`, 204). It is for
  a row of an AMS unit that was taken off. If the tray is still loaded and unmatched, the
  next capture writes the row again.
- D11. Capture takes the tray's try-lock before it clears a row too, not only before it
  writes one, and leaves the row when the lock is busy. A resolve holds the row and then
  locks spools, while the report write already holds spool locks; taking the tray lock
  first means the two never wait on each other in opposite orders.
- After deploy, every loaded untagged tray with no linked spool blocks its printer once
  capture or the backstop sees it. That is the intent, apart from the trays D9 matches.

## Left to the builder

- The ranking's exact colour distance (reuse what search or the colour catalog uses).
- How the type match for an unknown RFID tag reuses the existing pull or invoice code.
- The polling cadence of the Needs-you source.
- Test split between capture, the store, the routes, the print checks and the dialog.

## As built (#7143)

Read from the code at commit bdb40d8bb of the branch `feat/7143-unknown-spool`. Where this
section and the text above differ, this section is what the code does.

- **Table.** `filament_unknown_trays` (`20261126T1936Z_filament_unknown_trays.sql`). `seen_at`
  moves only when a reported field changes (D1). No row for the external holder (D2).
  `remain_pct` is stored and null in practice (D3).
- **Capture.** `capture.note_unknown` runs in the report write's savepoint and in the backstop. An
  RFID tray gets a spool (`match_tray`, else a spool with material and colour only; `created` event
  `data.by` `rfid_capture`). An untagged tray gets a row and its place. A busy tray lock leaves the
  tray to the next report (D11).
- **Routes.** Go, area `unknown-trays` (`tools/go/core/internal/unknowntrays`): the list, with up to 10
  ranked candidates and `made_from_tags` (D5, 24 hours), and dismiss. The rank key: spools with no
  place, then the reported material, then the nearest colour, then the number.
- **Resolve.** The write is Python's Temporal activity `filament_resolve_unknown_tray`
  (`backend/scadbuddy/filament/unknown_resolve.py`) on the `library` queue, served by the API process
  (G5). It answers `{id, number, code, location_id}`, publishes no event (G8), and keeps the lock
  order in Python (G9). The label weight changes only for a first weight (D4). There is no Go spool
  writer. **The Go resolve route is not built yet.** It waits for a Temporal client in Go
  (`internal/temporalx`). Until then, the dialog's and the tool's resolve get a 404 and cannot resolve a
  tray. The existing link paths still clear the row (`link_on`: the print dialog's tray question,
  the place page's "New spool from this tray", the agent's `link_tray_spool`). The rest of the
  flow works.
- **Block.** `prepare_run` (and so the check, `PrintCheck.unknown_trays`) and `lan_print` refuse a
  run with 422 `unknown_spool_loaded`. The print worker reads a CRUD route of caller `print`
  (area `unknown-trays`). A missing route lets the run go on (G3), so the worker-side block works only
  once `SCADBUDDY_CORE_AREAS` names the area.
- **Owner.** A "Needs you" source (read every 15 s), one dialog, four entry points (Needs you,
  Filament → Locations, a place's page, the print dialog) and the tools `filament_unknown_trays` and
  `filament_unknown_tray_resolve`.
- **Limits.** A print started in Bambuddy or on the printer is not blocked. A row whose unit the
  report no longer shows stays until that unit returns; deleting or merging the printer drops it.
