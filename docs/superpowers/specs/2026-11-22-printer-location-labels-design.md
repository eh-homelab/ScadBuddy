# Printer and AMS labels with short codes

Date: 2026-11-22. Status: short design, approved by the owner on 2026-11-22.
Issue: #6955. Parent: the filament epic #6260 (spec `2026-11-14-filament-management-design.md` §5.6, §7
"Labels"). Related: the inventory epic #6949.

## Intent

The owner wants a label on the outside of each printer, each AMS unit and each AMS slot,
now. A phone scan of a label opens that place in ScadBuddy. Later work (an unknown-spool
prompt, a scan-or-tap swap wizard, home dry boxes, waste on archive, NFC tags) starts from
these labels, so a label printed now must stay valid.

## Goals

- A short code for every location, typed by hand when a scan is not possible: `L-12`.
- Each printer is a location, with its AMS units and slots under it.
- The QR holds a full URL, `<label base URL>/x/L-12`, so the phone camera opens it with no app.
- The URL's host is a setting that the owner keeps stable (the SSO address). Changing the
  setting changes only labels printed after the change.
- A `/x/<code>` page resolves a code and opens the thing it names.
- The labels page prints a printer's labels: the printer, each AMS unit, each slot and the
  external holder.

## Non-goals (this piece)

- Spool labels keep their current QR (`scadbuddy:spool:<id>`, plan 2026-11-17 Decision 12).
- Direct ZPL printing to a network label printer. Printing stays in the browser.
- The scan page, NFC, the wizard, home locations, waste, unknown-tray prompts and print blocking.
- Codes for types, parts and other items (the resolver is built to take them later).

## Anti-goals

- A printed label that stops working when ScadBuddy's internal address changes.
- A code that is reused for another thing after an archive.

## Constraints

- Locations are `filament_locations`. AMS units and trays are made by
  `LocationStore.ensure_ams_on` / `ensure_ams_unit_on` (`backend/scadbuddy/filament/locations.py`),
  keyed by printer, AMS and tray, never by name.
- Spools already have a typed number: `filament_spools.number`, an identity column.
- The labels page is `frontend/src/pages/filament/LabelsPage.tsx`, with `labelSheet.ts`
  (50 x 25 mm, QR at level M, a quiet zone of 4 modules) and the `roll` and `sheet` layouts.
- New data crosses services only through CRUD routes (root CLAUDE.md "Database access").
  This piece reads and writes only through the API's own routes.
- Migrations: one new file per change; never edit a merged one.

## Approaches

1. **Codes and URL labels in ScadBuddy, printed through the browser (chosen).** It is small,
   needs no new hardware, and the labels stay valid when ZPL printing comes.
2. Print labels only after the label printer and ZPL exist. Rejected: the owner wants the
   labels now.
3. Keep host-less QR codes (`scadbuddy:location:<id>`). Rejected: the phone camera cannot
   open them, and the owner chose a stable URL.

## Design

1. **Location numbers.** A migration adds `number integer GENERATED ALWAYS AS IDENTITY UNIQUE`
   to `filament_locations`, as spools have. Existing rows get numbers. The display form is
   `L-<number>`.
2. **Printer locations.** A new seeded location kind `printer` (flagged neither dry storage
   nor AMS). `ensure_printer_on(conn, printer_id)` makes the printer's root place, named
   as the printer, idempotent and safe under a race, like `ensure_ams_unit_on`. AMS unit
   places hang under it; an existing unit place with no parent is moved under it. The
   external holder's tray place hangs under the printer place. A printer with no place
   gets one when a label is asked for and when capture next runs.
   As built: the root is the row with `printer_root` true, and the kind is the one with the
   flag `filament_location_kinds.printer` (seeded on "Printer"), not found by name. One
   unarchived root per printer (unique index). A "beside the printer" place is not a root. A
   unit place whose parent is empty or archived is moved under the root. In the printer merge
   a root clashes only with a root; a root only the merged printer has moves to the kept
   printer and takes its name (#7081). The labels route calls `ensure_printer_on`. So does capture
   (`filament/locations.py` `_printer_parent`; a missing printer kind leaves the place with no
   parent and never fails a capture): when it makes an AMS unit or the external holder, and on
   each tray capture (`filament/capture.py` `reconcile`, a tray change or the refresh's
   backstop) whose printer has a unit place or external holder with no live parent
   (`LocationStore.hang_orphans_on`). So an install made before printer places gets its root on
   its next tray capture. A printer with no AMS unit or external holder place yet gets its place
   only from the labels route.
3. **Code format.** `<prefix>-<number>`, prefixes upper case. The parser accepts any case,
   with or without the dash: `l12`, `L12`, `L-12`. This piece registers `L` (location) and
   `S` (spool). A registry maps a prefix to a lookup and a page, so later prefixes (types,
   parts, entities from #6949) add a row, not a new format.
4. **Resolver.** `GET /api/v1/codes/{code}` answers `{kind, id, code, name, archived}`, or
   404. An archived thing still resolves, marked archived. Numbers are never reused.
5. **`/x/:code` page.** It calls the resolver and opens the thing: a spool opens
   `filament/spools/:id`; a location opens a new location page.
6. **Location page** `filament/locations/:id`: name, code, kind, the path (printer → AMS →
   slot), the children, the spools placed here with grams left, and for a printer place a
   link to the printer. For an AMS slot, the spool in it.
7. **Setting.** `filament_label_base_url` in Settings → Printing → Filament: an https origin,
   checked like `storefront_public_url`. Unset, it falls back to `public_url`, then to the
   page's own origin. The labels page shows which one it uses.
   As built: `public_url` is reduced to its https origin (a path is dropped; an http scheme, or no host,
   gives `None`) in `filament/labels.py` `label_base_url` and in the frontend `httpsOrigin`.
   A label payload over 84 bytes is refused, so a long origin cannot reach a QR.
8. **Labels page, "Printers" mode.** Pick one printer or all. One label each for the printer,
   each AMS unit, each slot and the external holder, in tree order. Each label: the code in
   large type, the name ("H2C AMS-A slot 3"), and the QR of `<base>/x/L-<n>`. The same
   50 x 25 mm size and layouts as spool labels. The QR payload is longer than the spool
   payload, so its version and module size are checked against the 50 x 25 mm label, and
   the module stays at least 0.5 mm.
   As built: the labels route first makes the place of each AMS unit, slot and external holder
   the printer's latest report (`printer_reports`) lists, so a slot that never held a spool
   gets a label. A printer with no report that lists a unit or a holder gets no invented slot;
   the answer's `no_report` names it and the page says so. A printer's own place page links
   "Print this printer's labels" (`?printers=<printer_id>`); the Locations tab links every
   printer (`?printers=all`).
9. **Agent.** A tool to resolve a code, and the location page's read, named like the
   existing filament tools.

## Left to the builder

- The exact layout of the location page at phone width.
- Whether the location page also becomes the target of the Locations tab's rows.
  As built: it does. `filamentLinks.location` and the agent's `location` `ui_path` are
  `/filament/locations/<id>` (`test-fixtures/filament/ui-paths.json`); the spools in a place
  are `filamentLinks.locationSpools` (`/filament/spools?place=<id>`).
- Test split between the migration, the store, the resolver and the page.

## Relation to other work (checked 2026-11-22 against main at the merge in this branch)

- **Inventory epic #6949** (spec `2026-11-21-inventory-and-purchasing-design.md`). Its D3
  keeps `filament_locations` as the place tree for all stock, so a printer's own place and
  its `L-<n>` code serve lots and units too. Phase 1 (#6961) registers `location` as an
  entity kind (`entities/registry.py`); the place page shows its files and links panels, as
  the spool, type and supplier pages do. Phase 4 makes printers units; a printer unit links
  to its printer, and its place stays the `printer_root` row. Items, lots and units will
  want codes and labels of their own: each is a new prefix in `codes/resolve.py`
  `PREFIXES`, not a second resolver.
- **Properties #7026.** A scanned GTIN or lot number is a property search, not a code:
  `/x/<code>` resolves only ScadBuddy's own prefixed numbers.
- **Dry bins** stay `filament_locations` rows (kind "Dry box"); a bin gets an `L-<n>`
  label from the labels page's `?locations=` mode today.
