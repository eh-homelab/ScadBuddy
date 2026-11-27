# Labels for spool types and inventory items (#7169)

## Goal

A spool type and an inventory item each get a code, as a place (`L-<n>`), a spool (`S-<n>`)
and an item kind (`K-<n>`) have one. A person prints a label for a spool type, an item or an
item kind from its own page or from the Labels page. The label's QR code opens the thing's
page. A typed code, a scanned photo and an NFC tag find it, as they find a place.

Issue: #7169 (epic #7171, under #6260; inventory epic #6949, design row 16). Plan:
`docs/superpowers/plans/2026-11-27-inventory-labels.md`.

## Facts this design uses

All read from the code on main at `105dec6c5` (2026-11-27), unless the line says otherwise.

- A code is one letter, an optional dash and a number from 1 to 2^31 - 1, in any case
  (`backend/scadbuddy/codes/parse.py` `_CODE`, `MAX_NUMBER`). `codes/resolve.py` `PREFIXES`
  maps a letter to a table and a kind: `L` `filament_locations` `location`, `S`
  `filament_spools` `spool`, `K` `item_kinds` `item_kind`. `resolve` reads
  `SELECT id, <name> AS name, archived_at FROM <table> WHERE number = %s`, so a table needs
  the columns `id`, `number` and `archived_at`, and `_NAME` gives the name column (an SQL
  fragment from code, never from input).
- The letters in use are `L`, `S` and `K`. Searched with `grep` over `backend/scadbuddy`,
  `tools/go`, `frontend/src`, `agent/src` (`format_code("…"`, `"X-"+`, `` `X-${ ``, the two
  prefix maps) and over `docs/` (`` `X-<n>` ``). No other letter is used for a code.
- Go core has a copy: `tools/go/core/internal/scan/resolve.go` `prefixes`, with the same
  three entries (`K` since #8310). Its comment says a new prefix in `PREFIXES` is a new entry
  there too. It serves the photo scan (`POST /api/v1/scan/codes`). Python serves
  `GET /api/v1/codes/{code}` (`api/codes.py`) and the NFC tag read
  (`filament/tag_codes.py` `resolve_records`, over `resolve`). Go has no `/codes` route.
- The browser routes every resolved code through one module: `frontend/src/lib/resolvedCode.ts`
  `codeLabel` and `codeTarget`, typed by `Resolved['kind']`. `/x/<code>`
  (`pages/CodePage.tsx`), the scan page (`pages/filament/ScanPage.tsx`) and its
  `components/filament/scan/CodeCard.tsx` all call them. So a new kind there reaches the scan
  page with no edit to `ScanPage.tsx` or to `tools/go/core/internal/tagreads`.
- `filament_spool_types` has no `number` column (`migrations/20261114T1301Z_filament_spools.sql`
  and every later `ALTER TABLE filament_spool_types`). It has `code`, the maker's product
  code, which the type page shows as its heading (`pages/filament/TypePage.tsx`). Only
  `filament/types.py` `SpoolTypeStore.create_on` inserts a row, with the columns of
  `SpoolTypeCreate`. `_TYPE_COLUMNS` is built from `SpoolType`'s fields.
- `items` has no `number` column (`migrations/20261124T0323Z_inventory_core.sql`). Go core is
  the one writer of `items` once PR #8323 merges (`backend/tests/test_no_python_stock_writes.py`
  on its branch `feat/6949-k1-7-docs`, read with `gh api …/contents`).
- `item_kinds.number` came by the item_kinds pattern: a sequence, a backfill by
  `created_at, id`, `setval`, then `DEFAULT nextval`, `NOT NULL` and `UNIQUE`
  (`migrations/20261126T0218Z_inventory_class.sql` step 1a). A sequence's `nextval` never
  gives a value twice, and any number of column defaults can call one sequence
  ([PostgreSQL 18, sequence functions](https://www.postgresql.org/docs/18/functions-sequence.html)).
- `stock_lots` and `stock_units` have no `name` and no `archived_at`
  (`20261124T0323Z_inventory_core.sql`).
- The inventory design (`docs/superpowers/specs/2026-11-21-inventory-and-purchasing-design.md`)
  says: "A spool type is `item:<id>` and a spool is `unit:<id>`, with the ids they have today"
  (owner, 2026-11-25, ruling 5); row 16 puts codes and labels for items, units and lots in
  phase 3b; §11.4 Q1 asks the owner which of items, units and lots get a code. Q1 has no
  answer on #7169 or in the design (read 2026-11-27).
- The label layer (#7157, #7216): `POST /api/v1/filament/labels` (`api/filament.py`
  `render_labels`, body `filament/models.py` `LabelsRequest`) takes `spool_ids` then
  `location_ids`, 1 to `MAX_LABELS` (100) in all. `filament/labels.py` `pdf_labels` turns
  them into `label_pdf.py` `SpoolLabel` and `PlaceLabel`. A `PlaceLabel` is a code, a name
  and a QR payload `<base>/x/<code>`, at most `MAX_PAYLOAD_BYTES` (84). No base is 409
  `no_label_base`. `label_png.py` `png_names` names a non-spool page by its code. The browser
  draws the same place label with `components/filament/Label.tsx` `LocationLabel`. The Labels
  page (`pages/filament/LabelsPage.tsx`) reads `?ids=`, `?locations=` and `?printers=`.
- CUPS print (#7170): the Labels page asks `POST /filament/labels` for a PDF and posts the
  bytes to Go core (`tools/go/core/internal/labelprint`). Core never reads the subjects
  (`docs/superpowers/specs/2026-11-27-cups-label-printing-design.md` R1, R4).
- The route `/filament/labels` has no Go copy (`grep` for `filament/labels` in `tools/go/core`
  finds only `labelprint`'s comments).
- The agent's `filament_code_resolve` (`agent/src/tools/filament.ts`) describes `L-12` and
  `S-142` only.
- PR #8323 (open, session eee8e050), read with `gh api repos/eh-homelab/ScadBuddy/pulls/8323/files`
  on 2026-11-27: it deletes `backend/scadbuddy/inventory/items.py` (item reads move to Go),
  adds `tools/go/core/internal/inventory/store/items.go` and `…/wire/wire.go`, and gives
  `ItemKind` the fields `number` and `code` (`K-<n>`) in `inventory/models.py` and
  `wire.ItemKind`. `wire.Item` has no number. It also changes
  `frontend/src/pages/inventory/ItemPage.tsx`, `KindsPage.tsx`, `pages/inventory/api.ts`,
  `lib/inventoryLinks.ts`, `mocks/features/inventory.ts` and `mocks/features/filament/index.ts`.
  It does not touch `codes/`, `filament/label*.py`, `LabelsPage.tsx`, `Label.tsx`,
  `resolvedCode.ts` or `scan/resolve.go`.

## Decisions

Rulings from the brief (binding):

- **R1.** New routes go in Go. Python changes are edits to existing Python. This design
  extends the existing `POST /filament/labels` body; it adds no route. Only the API and Go
  core hold SQL. A route with a Go copy changes in both: the code resolve has one (the scan's
  `prefixes`), so both change.
- **R2.** Printing reuses #7170's CUPS route and the browser print, unchanged. The label
  layer stays the one layout: the new labels are the existing code label.
- **R3.** A new prefix needs a stable `number` per row, added by a new migration file only,
  with a sequence and a backfill, unique. No prefix letter is used twice.
- **R4.** No change collides with PR #8323. The overlap is listed under "Overlap with PR
  #8323".
- **R5.** UI tasks load `frontend-design:frontend-design`. The mocks serve every new path.
  Each label subject has a Print action on its own page, and the Labels page prints it.
- **R6.** Tests do not depend on runner speed or the date. Locally: lint, types and focused
  tests.
- **R7.** Decide; do not ask.

Decisions of this design (each one is my call):

- **D1. What gets a code.** A spool type gets `T-<n>`. An item gets `I-<n>`. An item kind
  keeps `K-<n>` and gets a printed label (it has a code already, but no label). My call.
  The issue names "spool types and other inventory items". Lots and units are not in this
  design: Q1 (the owner's) is open on them, and neither table has the name and the archived
  state that a resolved code carries, so a code for them needs a model decision that the
  inventory design has not made. A spool is a unit already and keeps `S-<n>`. A follow-up
  issue under #6949 holds lots and units, blocked on Q1.
- **D2. The letters.** `T` for a spool type, `I` for an item. Neither is in use (Facts). My
  call. DejaVu Sans Bold, the label font, draws `I` with no flag and `1` with one, so `I-12`
  does not read as `1-12`.
- **D3. One sequence for both numbers.** A new sequence `item_numbers_seq AS integer` gives
  both `filament_spool_types.number` and `items.number`. My call. Why: the owner ruled that a
  spool type becomes an item with the same id (Facts). With one sequence, a spool type's
  number is never an item's number, so phase 3 can give the type's item row the type's own
  number, and a printed `T-<n>` and the later `I-<n>` name one row with no renumber. A printed
  label cannot be renumbered. The cost is gaps in each table's numbers, which a code allows
  (a number is never reused, so gaps exist already). The sequence is not `OWNED BY` either
  column, so a later drop of one table keeps the other's default.
- **D4. The migration.** One new file, `<UTC timestamp>_label_numbers.sql`. It makes the
  sequence, adds `number integer` to both tables, numbers the spool types first and then the
  items, each by `created_at, id`, sets the sequence past the largest, then sets
  `DEFAULT nextval('item_numbers_seq')`, `NOT NULL` and a `UNIQUE` constraint on each. The
  default fills a new row in the database, so Go's `INSERT INTO items` and Python's
  `create_on` need no change. My call: `integer`, not `bigint` as `item_kinds.number`, because
  a code's number stops at `MAX_NUMBER` and `L` and `S` are `integer`.
- **D5. The name a code resolves to.** An item's and a kind's name is their `name`. A spool
  type's is `concat_ws(' ', code, colour_name)`, such as "10101 Charcoal": the type page's
  heading plus the colour, which a person in the shop can match to the spool. My call. The
  fragment goes in `codes/resolve.py` (`_NAME` becomes the public `NAME_SQL`) and in the Go
  `prefixes` entry. The label prints the same text, read through `NAME_SQL`, so the label,
  `/codes` and the scan always agree.
- **D6. Resolved kinds.** `Resolved.kind` gains `spool_type` and `item`. `codeTarget` opens a
  spool type at `filamentLinks.type(id)` and an item at `inventoryPaths.item(id)`;
  `codeLabel` says "Type T-4" and "Item I-9". A kind still opens its item list. My call.
- **D7. The labels request.** `LabelsRequest` gains `type_ids: list[UUID]`,
  `item_ids: list[UUID]` and `kind_ids: list[str]` (each `max_length=MAX_LABELS`). The count
  of all five lists is 1 to `MAX_LABELS`. The labels come in this order: spools, places,
  types, items, kinds, each list in the order sent. An unknown id is 404, as now. An archived
  row still gets its label, as a place does. My call.
- **D8. One code label.** Each new label is the place label's shape: the code large, the name
  under it in at most two lines, and the QR `<base>/x/<code>`, with the same base,
  `MAX_PAYLOAD_BYTES` check and 409 `no_label_base`. The type `PlaceLabel` becomes
  `CodeLabel` in `label_pdf.py`, and `LocationLabel` becomes `CodeLabel` in `Label.tsx`, since
  they now draw five subjects. Nothing else in the layout changes, so the PDF, the PNG, the
  sheets, the label fit vectors and the CUPS print take the new labels unchanged. My call.
  A spool type's label carries no spool fields (`fields` stays for spool labels only).
- **D9. Where the label rows are read.** `labels.py` reads the code rows itself, with one
  `SELECT id, number, <NAME_SQL> AS name FROM <table> WHERE id = ANY(%s)` per subject, the
  table and fragment taken from `codes/resolve.py`. That is a read by the API, which holds
  SQL; it writes nothing, so `test_no_python_stock_writes` still passes. My call.
- **D10. The read models.** `SpoolType` gains `number: int` (its `code` field is the maker's
  code, so the typed code is formatted in the browser as `T-${number}`, as spools do
  `S-${number}`). `Item` gains `number: int` and `code: str` (`I-<n>`), in Go `wire.Item`, the
  Go read in `store/items.go` and Python `inventory/models.py`, as #8323 gives `ItemKind`. My
  call.
- **D11. Links and pages.** The Labels page takes `?types=`, `?items=` and `?kinds=`, a
  comma list each, as `?locations=`. One code-label mode serves `?locations=`, `?printers=`
  and the three new ones; one of them at a time, as now. The links are
  `filamentLinks.typeLabel(id)`, `itemLabel(id)` and `kindLabel(id)` in
  `lib/filamentLinks.ts`, beside `locationLabel`, because the Labels page is a filament page
  and `lib/inventoryLinks.ts` is pinned by shared vectors with the agent. Print actions: the
  type page header, the item page header and each row of the kind tree. My call.
- **D12. The scan.** No change to `ScanPage.tsx` or `internal/tagreads` (session c4f175d1's).
  The scan opens what `codeTarget` opens, so it opens a type's or an item's page once D6
  lands. An NFC tag with a `/x/T-<n>` URL or a `T-12` text resolves through `resolve`, as
  now. My call: tell c4f175d1 in its channel when the PR opens; there is no scan follow-up.
- **D13. The agent.** `filament_code_resolve`'s description names `T-` and `I-` (and `K-`).
  No new tool: `POST /filament/labels` stays a `coverage.ts` entry. My call.

## Overlap with PR #8323

- No overlap: `codes/`, `api/codes.py`, `filament/label*.py`, `filament/models.py`
  `LabelsRequest`, `api/filament.py` `render_labels`, `scan/resolve.go`, `resolvedCode.ts`,
  `Label.tsx`, `LabelsPage.tsx`, `TypePage.tsx`, `mocks/features/filament/seed.ts`, the
  migration. The work is two PRs. PR A (the numbers, the resolve on both sides with
  `resolvedCode.ts`, and the labels file) starts now and touches none of #8323's files.
- Overlap, so PR B starts after #8323 merges and reads the merged files first: the item
  read (`store/items.go`, `wire.Item`, `inventory/models.py`), the item and kind pages
  (`ItemPage.tsx`, `KindsPage.tsx`, `pages/inventory/api.ts`), and the mocks
  (`mocks/features/inventory.ts`, `mocks/features/filament/index.ts`).
- `items.number` is added by this design's migration while #8323 is open. #8323's Go INSERTs
  name their columns and need no number, because the default fills it (Facts, K1 owner's
  note of 2026-11-27).

## Out of scope

- Codes and labels for lots and units (Q1; D1's follow-up).
- Writing a type's or an item's code to an NTAG (design row 17).
- A spool type's spool fields on its label; label fields for an item's properties
  (row 16 names them; a later change to `LabelOutput`).
- Phase 3's fold of spool types into items (D3 only keeps it open).
