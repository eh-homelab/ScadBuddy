# Spool create operations: bulk create, copy, create from slot (#7150)

Gap found by the §11 parity sweep (#6270, `docs/features/filament-parity.md`) against
Bambuddy (BB) 1.2.6b1. Spec: `docs/superpowers/specs/2026-11-14-filament-management-design.md`
§11 ("Operations: create, bulk create, copy, create from slot, …"). Epic #6260.

## Rulings

- **R-7150-1 Bulk create is its own route, idempotent.** `POST /filament/spools/bulk` takes
  `{spool, quantity, location_id, presets, k_values}` and makes `quantity` (1 to 100) identical
  spools in one transaction. Each spool gets its own number (the identity column) and its own
  `created` event. The route needs an `Idempotency-Key` (428 without one), keyed under the
  person's name in the `filament` ledger, as the CSV import does (`api/filament.py`
  `import_spools`): a retry answers the first reply and writes nothing; the key with another
  body is a 422. `POST /filament/spools` stays as it is. The spool form sends every new spool
  through the bulk route, with one key per opened form, so a double click makes no second set.
- **R-7150-2 The bulk body carries the form's whole new spool.** The place, the spool's own
  presets and its own K values are written in the same transaction, so a bulk of N is never
  half made and the form needs no N-by-3 follow-up requests. The place may not be a printer's
  tray (422): a tray holds one spool, and the tray's own link puts it there.
- **R-7150-3 Copy is a read: the new-spool draft of an existing spool.**
  `GET /filament/spools/{id}/copy` answers the bulk body without `quantity`: the source's type,
  its own overrides, its note, its own presets and K values, and its place unless that place
  is a printer's tray. It leaves out what stays unique to the first spool: number, tags, the
  tray place, the ledger (so no `grams_left`), and the weight lock (a fact about its weight
  history). An archived spool can be copied (a reorder). The person edits the draft in the
  spool form and creates through R-7150-1; nothing is written by the copy itself.
- **R-7150-4 Create from slot is built by ScadBuddy from its own LAN report, never through
  Bambuddy.** SB holds the spools now, and BB's `POST /inventory/spools/from-slot` would write
  BB's inventory, not SB's. The route is
  `POST /printers/{printer_id}/trays/{ams_id}/{tray_id}/new-spool`, beside the tray link
  (`api/tray_spools.py`), and reads the tray from the printer's latest stored report as the
  link does. It needs an `Idempotency-Key` (428 without one): a lost reply retried would
  otherwise make a second spool and move the first out of the tray.
- **R-7150-5 What a slot gives the spool.** The material is the tray's `tray_type`, resolved by
  key against every material name and alias, and added when no row has it (as the BB pull and
  the CSV import add one). The colour is the tray's `#RRGGBBAA`, alpha kept. A tray's RFID keys
  become the spool's tag. The spool is placed in the tray in the same transaction as it is
  made (`FilamentInventory.link`'s steps), and the tray's previous holder moves to no place.
  No type, label weight or grams are guessed: the report gives none of them as SB reads it,
  so the spool shows "needs a type" and the type proposals group it. 409 when the printer has
  not reported the tray filled, when the tray's tag is already on an unarchived spool (that
  spool is the tray's), or for a Bambuddy virtual printer.
- **R-7150-6 Where a person reaches create from slot.** On the native print path's tray
  question (the print dialog, where SB shows a printer's untagged trays from its report), and
  on an AMS tray's place page under Filament > Locations when the tray holds no spool. The
  Bambuddy path's tray question keeps its link to Bambuddy's inventory: that path names a BB
  printer id, not a registry printer.
- **R-7150-7 The agent gets the same options.** `filament_spool_upsert` takes `quantity` and
  `copy_of` (it then sends the bulk route, with the call's idempotency key; `fields` overlay
  the copy's draft). A new tool `create_tray_spool` calls the create-from-slot route. All
  three are write tier, as the other spool and tray tools are.
- **R-7150-8 A copy's key names the draft it read.** A resend of `filament_spool_upsert`
  with `copy_of` reads `GET …/copy` again. Its bulk `Idempotency-Key` is the sha256 of the
  call's key and the draft's JSON. A resend with the source unchanged sends the same key, so
  it makes nothing twice. A resend after the source changed sends a new key, so it makes a new
  set from the changed source, never a 422 for a key sent with another body. The tool's
  description says so. A plain bulk add (no `copy_of`) keeps the call's key.
- **R-7150-9 The native print dialog asks about every unanswered tray.** A native tray with no
  shelf candidate is still asked about. Its question offers only "New spool from this tray"
  and the skip ("Print from the tray as it is"). A tray question is keyed by its printer too,
  so one printer's error and `Idempotency-Key` never carry over to the next printer.
- **R-7150-10 A copy does not carry `added_full`.** Whether a spool was added full is a fact
  of the source spool's own history, as its ledger and weight lock are. `copy` leaves it out
  of the draft, so the copy's `added_full` is unset (`null`).
- **R-7150-11 A copy keeps each preset row's origin.** The draft's preset rows
  (`SpoolDraftPreset`) carry `migrated`. A source's `migrated` row stays `migrated` on the
  copy, so the copy's first type drops it as it would the source's (`presets.drop_migrated`).
  The spool form sends a copied row back `migrated` only while its values are unchanged, as
  `set_presets` keeps a row sent back unchanged; a row the person edited is theirs.
- **R-7150-12 Bulk preset refusals match `PUT …/presets`.** A bulk create whose preset row names an
  `sb-<id>` that does not exist is `PresetUnknownError` (`names_preset`), as the presets PUT
  answers it; any other foreign key is `CatalogRefError`.

## Tasks

1. Backend models: `SpoolDraft`, `SpoolBulkCreate` (`filament/models.py`).
2. Backend store: `FilamentSpoolStore.bulk_on`, `copy`, presets and K values on a connection;
   `FilamentInventory.create_in_tray_on` (`filament/spools.py`, `filament/inventory.py`).
3. Routes: bulk and copy (`api/filament.py`), new-spool (`api/tray_spools.py`), with tests
   (`tests/api/test_filament_spool_create_ops.py`, `tests/api/test_tray_spools.py`).
4. Frontend: client calls, msw mocks, spool form quantity and copy, the two from-slot buttons,
   vitest tests.
5. Agent: the upsert options and `create_tray_spool`, with tests.
6. Docs: `docs/features/spools-and-filament.md`; parity rows to MATCH.
