# Spools and filament

## What it does

The **Filament** section is ScadBuddy's own filament inventory. It covers:

- spools and spool types, with tags, presets and K values;
- catalogs (brands, materials, colours and more);
- suppliers, price sheets, invoices and a shopping list;
- locations with humidity and temperature sensors;
- RFID tags, NTAG labels, and printed QR labels for spools, places, printers, AMS units and slots;
- a printer's K-profiles (pressure advance) through Bambuddy: list, set, batch set, delete and notes;
- a printer's AMS slots through Bambuddy: configure a slot by hand, its saved slicer preset, refresh
  and reset, load and unload, and the AMS units' labels;
- a ledger of spool events, with history, metrics and a forecast;
- key-value properties on spools and spool types (#7026).

The print dialog picks from these spools (ScadBuddy's own, "SB" spools). Tray changes and
finished prints are recorded against them. The inventory can sync with Bambuddy's in
either direction. A new spool type with a 3dfilamentprofiles.com id can have its slicer
presets fetched per printer model and nozzle. Filament profiles (#717) bundle print
settings for a filament. The design is the
[filament-management spec](../superpowers/specs/2026-11-14-filament-management-design.md).
The repo skill `.claude/skills/filament-intake` covers adding purchased spools.

## How it works

The spools and the ledger live in the `filament_*` tables, written by the API. The print
worker brings in Bambuddy's side:

- `FilamentBbSync` pushes to Bambuddy;
- `FilamentBbPull` reads from it;
- `SensorNotifyPass` sends location-sensor alerts through Bambuddy every minute.

On the API's `library` queue, `SpoolRefresh` runs on a Schedule. It refreshes the kept
presets, runs the pull, re-records any tray changes that were missed, and reconciles RFID
state.

A spool is made in three ways besides one at a time (#7150, plan
`docs/superpowers/plans/2026-11-23-spool-create-ops.md`):

- **Bulk create.** The spool form's "How many spools" (1 to 100) makes that many identical
  spools in one request, `POST /filament/spools/bulk`. Each gets its own number and its own
  `created` event. The place, presets and K values are written in the same transaction. The
  request needs an `Idempotency-Key`; the form sends one per opened form, so a retry or a
  second click makes no more spools. A bulk create never goes into a printer's tray.
- **Copy.** A spool's "Copy" button opens a new spool form filled from
  `GET /filament/spools/{id}/copy`. The copy keeps the type, the spool's own values, its
  note, presets, K values and shelf place. A preset row that a migration made stays such a
  row, so the copy's first type drops it as it would the source's (R-7150-11). The copy
  leaves out the number, tags, tray, weight history, weight lock and whether the source was
  added full (R-7150-10).
- **Create from slot.** "New spool from this tray" makes a spool from what the printer last
  reported in the tray over the LAN: its material, its `#RRGGBBAA` colour and a tagged
  tray's tag. The spool is placed in the tray, and the tray's previous spool moves to no
  place. ScadBuddy builds it from its own report; nothing reaches Bambuddy. The spool has no
  type yet. The route is `POST /printers/{id}/trays/{ams}/{tray}/new-spool` and needs an
  `Idempotency-Key`. The button is on the native print dialog's tray question and on an
  empty AMS tray's place page. The dialog asks about every unanswered native tray, one with
  no shelf candidate too; that question offers only this button and the skip (R-7150-9).

The agent's `filament_spool_upsert` takes `quantity` and `copy_of`, and `create_tray_spool`
makes a spool from a tray. A resend of a copy reads its source again: its key names the
draft it read, so an unchanged source makes nothing twice and a changed one makes a new set
(R-7150-8).

The Spools tab's bulk bar acts on the selected spools (#7151, plan
`docs/superpowers/plans/2026-11-23-spool-bulk-ops.md`):

- **Edit fields.** A dialog lists every field a spool's edit sets, except archived. Each
  ticked field is set on every selected spool; a ticked field left empty clears the spool's
  own value, so it takes its type's. It is one request, `POST /filament/spools/bulk-update`,
  and all or none: when one spool refuses, nothing changes and the reason names that spool.
  Each spool that changed gets its own `edited` event.
- **Archive and Restore.** One request each, `POST /filament/spools/bulk-archive` and
  `/bulk-restore`. The answer lists the spools changed, those already so and the ids that
  name no spool, and the bar reports it once.

An AMS tray's place page that holds a spool has "Take the spool out of this tray"
(`DELETE /printers/{id}/trays/{ams}/{tray}/spool`): the spool moves to no place.

The Spools and Tags tabs find a tag by its UID or tray UUID. A search token of at least 4 hex
digits matches a spool's open tag that holds it, with `:` and `-` left out, so `04:A1:B2`
finds tag `04A1B2…`. The agent's `filament_spool_list` `q` does the same. Its bulk tools are
`filament_spools_bulk_update` and `filament_spools_bulk_archive`.

Tray changes and print ends are recorded when a printer report is written
(`backend/scadbuddy/filament/capture.py`). They are not a workflow of their own.

A type's cost per kg can be worked out on its page (#7152, plan
[`2026-11-23-catalog-cost-stats.md`](../superpowers/plans/2026-11-23-catalog-cost-stats.md)).
The cost calculator sends a purchase's price, tax, shipping, spool count and net weight to
`GET /filament/cost-per-kg` (`backend/scadbuddy/filament/cost_calc.py`). The server does the
sum on exact decimals, in the shop's currency, and writes nothing. "Use as default cost" puts
the result in the type's cost field, and Save writes it. The Metrics tab's "By material
number" section reads `GET /filament/metrics/material-numbers`
(`MetricsStore.material_numbers`). A material number is a type's code, case folded, which
Bambuddy calls `material_number`; archived types of the same code count under it. Each row
gives the types, the spools in stock, their grams and value at cost, and the grams the
range's prints used, counted as the Consumption chart counts them. The agent reads both
with `filament_cost_per_kg` and `filament_material_number_stats`.

Filament presets are ScadBuddy's own (#7176, plan
[2026-11-23](../superpowers/plans/2026-11-23-scadbuddy-filament-presets.md)): the table
`filament_presets`, each document a blob with every version kept. A type or spool names one
by the ref `sb-<id>` in `slicer_filament`. The native slicer reads them from ScadBuddy's
store; Bambuddy knows each one only by its mirror's local preset id
(`EffectivePreset.bambuddy_id`), which its slice path, the spool push and the AMS slot
configure send. ScadBuddy owns the presets, and Bambuddy is at most a mirror target or
the source of an import (#7244, the owner's decision of 2026-11-23): ScadBuddy keeps no
copy of Bambuddy's local presets, and the native slicer, the print dialog and the profile
fill read none. Bambuddy's presets come in three ways only, each reading `GET
/local-presets` itself and writing only `filament_presets`: the scheduled pull's import
while `filament_sync.presets` is `bb_to_sb` or `two_way` (a binding that names a Bambuddy
local preset is moved to a ScadBuddy preset made from it, and a preset made in Bambuddy
takes a new version when Bambuddy edits it); the one-time import, whatever the direction
(`POST /filament/presets/bambuddy-import`, Settings → Printing → Filament's "Import presets
from Bambuddy now", agent `filament_bambuddy_presets_import`); and the API's start, which
adopts every binding that still names a Bambuddy local id (`adopt_at_start`), unless the
links are another Bambuddy's. A binding left naming one is an error the print dialog shows
(`backend/scadbuddy/filament/sb_presets.py`). While the stored setting
`filament_preset_mirror` is on (the default), each preset ScadBuddy owns is copied to
Bambuddy's local presets after a fetch and at the end of each scheduled pull, whatever
`filament_sync` says, rewritten in place under the same Bambuddy id when it changes; a
failed copy is the preset's `mirror_error`
(`backend/scadbuddy/filament/preset_mirror.py`). One mirror runs at a time, a preset's
Bambuddy id is kept with the Bambuddy it belongs to. That id is a Bambuddy link as the
filament sync's are: the write that stores it records its Bambuddy as the links' one when
none is recorded, so a site whose only links are presets has one too. After the Bambuddy
URL changes the mirror waits, as the filament sync does, until the links are reset
(`POST /filament/sync/reset-links`): a reset that finds the same Bambuddy (proved by the
spool links, or, with no spool linked, by the presets: each linked id listed by the new
URL's Bambuddy, none older than Bambuddy's `updated_at` as ScadBuddy last took it: the mirror's last
write, or for a preset imported from Bambuddy, its last import, #7343) moves the ids to the new URL, and
one that deletes the links drops them (the dry run's and the answer's `presets` count), so
each preset is created on the new Bambuddy (on the same Bambuddy, a second copy, which the
reset dialog says);
meanwhile no path hands Bambuddy an id of the old one. A create whose answer was lost is
found above the highest id listed before it, by its name and its document, not made
twice; when a newer version was written since, it is created again and its `mirror_error`
names the copy the lost create left (#7343); one preset's failure, a Bambuddy id another ScadBuddy preset holds included, is its
own `mirror_error` and never stops the rest.

A fetch writes ScadBuddy only: each file is the type's `3dfp` preset of its key, a changed
one a new version with its changed keys in the run's notes, and the type's own nozzle range
wins over 3DFP's (#7142). Fetching presets is the `ProfileFetch` workflow on the
profile-fetch worker, and a 3DFP search or add is its `TdfpSearch` or `TdfpCreate` workflow (#7025, `filament/tdfp.py`)
([processes.md](../architecture/processes.md#profile-fetch-worker-and-browser)).

A printer's K-profiles (#7154, plan
`docs/superpowers/plans/2026-11-23-printer-kprofiles.md`) are read from the printer through
the Bambuddy printer its registry row links, and shown on its card in Settings → Printers
(closed until opened: each read asks the printer over MQTT). Each write (set, batch set,
delete, a note's set and delete) is an operation on the `bambuddy` queue, so the print
worker makes the call. Set, batch and delete run once: Bambuddy dedupes none of them, and a
write the printer did not acknowledge answers `confirmed: false`. ScadBuddy keeps no copy;
the notes live in Bambuddy, under the keys its own K-Profiles page uses.

A printer's AMS slots (#7153, plan `docs/superpowers/plans/2026-11-23-ams-slot-control.md`)
are read from Bambuddy's live status of the printer its registry row links, and shown on its
card in Settings → Printers ("AMS slots", closed until opened). The routes call Bambuddy in
the request. Each write that reaches the printer (configure, reset, refresh, load, unload)
reads Bambuddy's status first and refuses with a 409 and a `code` while the printer prints,
while the AMS is busy, or where the slot is not there; load also refuses an empty slot or the
one already feeding, and refresh refuses while filament is loaded. Configure takes any SB
spool (its values and its type's preset and K value for the slot's nozzle) or the values by
hand. A slot's saved preset and an AMS unit's label live in Bambuddy; a label write also
names ScadBuddy's place of the unit. The page asks before every printer write.

### Weighing breakdown (#7682)

A weighing sent with the scale's reading (`POST /filament/spools/{id}/events`, `kind`
`weighed`, `reading_g` beside `grams`, or alone) keeps what it was made of in the event's
`data`: `reading_g`, `empty_g` (what was taken off for the empty spool), `empty_source`
(`insert`, or `person` when the client took something else off than the spool's effective
empty weight), `empty_basis` and `empty_name` (the insert's, null for `person`), `expected_g`
(the grams left the ledger gave just before the weighing's time; null with no reading before)
and `drift_g` (`grams - expected_g`; negative means the scale found less filament than the
ledger expected). The effective empty weight is the spool's insert type's, else its type's
(`filament/empty_weight.py`); a reading sent alone on a spool with none is refused, 422
`no_empty_weight`, and one under the empty weight 422 `reading_below_empty`. The ledger's rules do not change: the weighing's filament anchors as
before. The spool carries the newest weighing's drift as `last_drift_g` and `drift_notice`
when it is at or over Settings → Filament → "Drift notice at, grams" (10 by default). The
spool page's Weight section says it in a sentence ("Weighed 1180 g on 25 Nov 2026: filament
980 g + spool 200 g (Plastic, placeholder). The ledger expected 992 g. The scale found 12 g
less than the prints accounted for."), the timeline says "reading 1180 g, spool 200 g, 12 g
less than expected", the heading and the spools list carry a Drift mark while it is a notice,
and an added-full spool whose starting amount (its `created` event) is not a reading says
its start gap ("Started at the label weight 1000 g. Bambuddy reported 979.3 g; 20.7 g
unexplained."); a later anchor (a sync, an AMS reading) follows prints and shows none. The spool page, the
phone page and the agent's `filament_spool_add_event` send `reading_g` whenever an empty
weight is known (the phone page sends it alone, so a decimal reading keeps the insert's
name). Design: `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md`
§5.1, §5.2, §5.5.

### A new spool's first weighing under the label weight (#7683)

When a spool that is new by #7280's rule (added full, nothing consumed, grams left at the
label) reads under its label weight, the insert weight check
(`GET /filament/spools/{id}/insert-weight-offer`) answers `reason` `below_label` with
`shortfall_g` (the label less the reading), `resolutions` and `default`. Each resolution
names its filament: `tared` (the scale was tared on the empty spool, so the reading is the
filament), `short` (the spool holds less than its label: the reading less the empty weight,
and the spool stops being added full) and `label_wrong` (the same filament, and the spool's
own label weight becomes it, rounded; the type keeps its own). `short` and `label_wrong`
are offered only when the reading is at least the empty weight. The default is `tared`
within 10 % of the label, else `short`. The weigh form and the phone page show the three
with their numbers ("The reading is 100 g under the label weight. What is up?") with the
default preselected, and the weighing posts `resolution` with `reading_g` alone; the
agent's `filament_spool_add_event` answers the choices (`below_label`, nothing booked)
until it is called again with one. The store checks the choice again under the row locks
(a resolution the check no longer offers is 422 `no_below_label` with the check's
`reason`), books the side effect as an `edited` event before the weighing, and keeps
`resolution` and `shortfall_g` in the event's `data`; a `tared` weighing's `empty_source`
is `tared`, `empty_g` 0. A resolved weighing keeps `expected_g` and records no `drift_g`,
so it raises no Drift mark: the person just explained the gap. The timeline says "scale tared on the spool", "spool short by
100 g" or "label corrected", and the Weight section's sentence reads "filament 900 g (the
scale was tared on the spool)". Design: the same spec, §5.3.

### The phone page (#7273)

`/filament/scan` reads and writes spool tags with the phone's own NFC reader, through Web NFC
(spec `docs/superpowers/specs/2026-11-23-web-nfc-tags-design.md`). It works only in Chrome on
Android, in its own tab: Web NFC refuses a frame, so inside Bambuddy the page shows its address
to open. A tap posts the tag's UID and records to `POST /filament/tags/reads`, which opens the
spool that holds the tag, or offers to bind it or make a spool from what the tag says
(OpenSpool or OpenTag3D). "Write tag" on a spool backs the tag up first, writes the spool's
records in the tag's own format (else the `filament_tag_format` setting), reads the tag back,
and binds it only when the read-back equals what was sent. Every write keeps a backup, which
the page can write back. An agent reads or writes a tag through an NFC job (`nfc_read`,
`nfc_write`): the page shows the job and its records, and nothing runs until a person arms it
and holds a tag. A job waits `timeout_s` for a person to arm it; arming gives the run 60 seconds
(`ARM_SECONDS`) for the tap. Once the page has backed the tag up for a write (`write_begun`), the
job is never cancelled or expired: it ends with the write's result (its read-back, or the
page's error), or, when none comes within 10 minutes of the backup, `failed` with `result.code`
`no_read_back` (the tag may hold the new records, and its backup can restore it). The agent
waits for either.

### Properties (#7026)

Design: [inventory spec §6.3a](../superpowers/specs/2026-11-21-inventory-and-purchasing-design.md), D17.

A spool, a spool type and a catalog row (such as an insert type) can hold properties: free key-value pairs, such as a part number,
barcodes or a lot number. The store is generic and works for any entity, so it is not
specific to filament. A value is a string, a number, a boolean or a list of strings. A key
matches `[a-z][a-z0-9_]{0,63}`. Limits: 200 keys per entity, 2000 characters per string,
50 items per list, 500 characters per item (`backend/scadbuddy/entities/properties.py`).

- Each kind has a schema that documents and types the keys it knows. It never refuses a
  key. The schema is an allowlisted subset of JSON Schema Draft 2020-12 (`TOP_KEYWORDS`,
  `KEY_KEYWORDS`, `ITEM_KEYWORDS`; list items are strings). An `enum` or `const` must fit its
  key, and no bound may cross its pair or a cap. Anything else is 422 `bad_property_schema`, naming the keyword. Values
  already stored are not checked again after a schema change.
- `item` is the exception (#7067). An item's properties follow its item kind's
  `attribute_schema`, merged down the kind tree (a child kind's key wins;
  `backend/scadbuddy/inventory/kinds.py` `item_schema`). An item kind's schema is set on the
  kind, so `GET` and `PUT /entity-kinds/item/property-schema` both answer 409
  `schema_owned_by_item_kind` (`backend/scadbuddy/api/entity_properties.py`).
- The migration seeds the keys `mpn`, `gtin`, `asin`, `manufacturer_url`,
  `empty_spool_weight_g` and `td` for `spool_type`, and `lot_number` for `spool`
  (`backend/scadbuddy/migrations/20261123T0017Z_entity_properties.sql`).
- Routes: `GET`/`PATCH /entities/{ref}/properties` (a key set to `null` is removed; a
  patch applies whole or not at all), `GET /entity-kinds/{kind}/properties` and
  `GET`/`PUT /entity-kinds/{kind}/property-schema`, which answer 409
  `schema_owned_by_item_kind` for `item` (`backend/scadbuddy/api/entity_properties.py`).
- UI: the section "Files, links and properties" of the spool and type pages
  (`frontend/src/components/entities/PropertiesPanel.tsx`; the spool page's anchor
  `#files-and-links` stays), and the same three panels under a catalog row's edit form in the
  Catalog tab (`frontend/src/components/filament/CatalogList.tsx`). A kind's own keys are edited
  from the panel's "Edit the kind's keys" and the Catalog tab's "Property keys"
  (`frontend/src/components/entities/KindKeysForm.tsx`, #7656); an item's are its item kind's,
  on `/inventory/kinds`. A list value has one field per item. A rename or an add onto a key the entity
  already has asks first, since it replaces that key's value.
- Search: the Spools and Types tabs' search also matches property values, beside the hex
  match on a spool's tag UID and tray UUID (#7151)
  (`frontend/src/lib/filamentSearch.ts` `propertyTexts`, loaded by
  `frontend/src/components/entities/api.ts`). The agent's search matches them too
  (`agent/src/tools/filament.ts` loads them, `agent/src/tools/filamentSearch.ts` matches); both use the shared vectors
  `test-fixtures/filament/search-vectors.json`.
- Agent: `entity_property_list`, `entity_property_set`, `entity_property_schema_get` and
  `entity_property_schema_set` (`agent/src/tools/entities.ts`), and a `properties` field on
  `filament_type_upsert` and `filament_spool_upsert` (`agent/src/tools/filament.ts`). An
  upsert that changes only properties skips the row PATCH, so `updated_at` and the
  Bambuddy sync do not change.

## Code

- Routes:
  - `backend/scadbuddy/api/filament.py:265` and `backend/scadbuddy/api/filament_purchasing.py:46` (both under `/filament`);
  - `backend/scadbuddy/api/tray_spools.py:44` (a tray's spool link, and create from slot);
  - `backend/scadbuddy/api/filament_profiles.py:26`;
  - `backend/scadbuddy/api/printer_kprofiles.py:42` (a printer's K-profiles, under `/printers/{id}/kprofiles`);
  - `backend/scadbuddy/api/printer_slots.py:44` (a printer's AMS slots and AMS labels, under `/printers/{id}/ams` and `/printers/{id}/trays`).
- UI:
  - the **Filament** nav item (`frontend/src/components/AppShell.tsx:111`);
  - its routes (`frontend/src/App.tsx:121-138`);
  - `frontend/src/pages/filament/FilamentPage.tsx:45`, `SpoolPage.tsx:418`;
  - `frontend/src/components/filament/`;
  - Settings → Printing → Filament (`frontend/src/pages/settings/sections/filament.tsx:911`);
  - the print dialog's picker (`frontend/src/components/FilamentPicker.tsx:361`);
  - a printer's K-profiles on its card in Settings → Printers (`frontend/src/components/settings/PrinterKProfiles.tsx:83`).
- Core: `backend/scadbuddy/filament/`:
  - the SB spools a print reads: `inventory.py` (`read_sb_spools` at `:957`, `reconcile_trays` at `:453`);
  - `capture.py`, `ledger.py`, `catalog.py`, `types.py`, `locations.py`, `sensors.py`, `invoices.py`, `labels.py`;
  - `backend/scadbuddy/codes/` (code parsing and resolving);
  - `bb_sync_*.py` and `bambuddy_pull.py`;
  - `profile_fetch.py`, `sb_presets.py` (ScadBuddy's presets) and
    `backend/scadbuddy/slicing/own_presets.py` (their refs, as the native slicer reads them);
  - a printer's K-profiles: `backend/scadbuddy/bambuddy/kprofiles.py` (the bodies and the operation kinds, `kprofile_kinds` at `:168`) and `BambuddyClient.set_k_profile` and its siblings in `backend/scadbuddy/bambuddy/client.py`.
- Workflows:
  - `backend/scadbuddy/workflows/spool_refresh.py:104`;
  - `filament_sync.py:68`, `filament_pull.py:43`, `sensor_notify.py:58`.
- Agent tools: `agent/src/tools/filament.ts`, `filamentPurchasing.ts`, `traySpools.ts`, `printerSlots.ts`.

## Data

- Tables: `filament_*` (`backend/scadbuddy/migrations/20261114T1300Z_filament_catalogs.sql`,
  `20261114T1301Z…` and later). These cover the catalogs, spool types, spools,
  `filament_spool_events`, tags and bindings, locations and sensors, suppliers and
  invoices, the Bambuddy sync state and links, `filament_profile_fetches`, and the
  presets `filament_presets` and `filament_preset_versions`
  (`20261123T0711Z_filament_presets.sql`; blobs held by `filament_preset`). The price
  book is `filaments`.
- The old spool overlay and native spool tables were dropped
  (`backend/scadbuddy/migrations/20261118T1235Z_drop_native_spool_tables.sql`).
- Settings: `spool_overlay_refresh_seconds` (the refresh Schedule), `filament_sync`,
  `filament_low_stock_pct`, `filament_default_cost_per_kg`, `profile_fetch_nozzles`,
  `filament_preset_mirror`,
  `filament_drying_temp_c` and `filament_drying_hours` (#7156: the drying a spool is offered
  when its spool, type, subtype and material set none; empty by default, so none is offered),
  `filament_auto_add_unknown_rfid` (#7156, stored only, on by default: on leaves Bambuddy's own
  auto-add of unknown RFID spools as it is, so Bambuddy may add one and the pull brings it in,
  unless the RFID override mode holds it off; off holds Bambuddy's auto-add off as the override
  mode does, so no spool is made for an unknown tag; the value Bambuddy had goes back only when
  neither holds. ScadBuddy's capture makes no spool from a tag itself),
  `filament_label_base_url` (an https origin of at most 64 characters, live; unset, labels use
  `public_url` reduced to its https origin, then the page's own origin),
  `filament_label_template` (#7157, stored only: the label template the labels page and the
  labels PDF use; the 50 x 25 mm roll label by default).
- Bambuddy's spool fields (#7149, plan
  `docs/superpowers/plans/2026-11-23-spool-bb-fields.md`,
  migration `20261123T0609Z_filament_spool_bb_fields.sql`):
  - `data_origin` says how the spool's data came in, in Bambuddy's words (`rfid_auto`,
    `rfid_linked`, `nfc_link`, `opentag3d`), or `openspool`, ScadBuddy's own value for a tag
    the phone page wrote in OpenSpool (Bambuddy has none for that format). It is read only. The pull takes Bambuddy's
    value; a person's tag bind sets `rfid_linked` when the tag's stored kind is a Bambu
    tag, else `nfc_link`; a tag written from the phone page (#7273) sets `openspool` or
    `opentag3d`, the format it wrote.
    The push sends it when SB has one, and the tag push sends it to `link-tag`.
  - `added_full` (a person edits it) and `tag_encoded_at` (Bambuddy's `encode_time`, set
    by a tag written from the phone page) come from Bambuddy on the pull. Bambuddy takes no write of
    either, so they are never pushed. With the baseline below they are kept in the link's
    `bb_seen` (`PULL_SEEN_FIELDS`), and after a push the pull judges them against it, never
    against the push's echo (R-7149-8).
  - The consumed counter is a ledger event, `consumed_reset` (no grams; the grams left
    stay). The spool metrics' `consumed_g` counts the plates after the newest reset, and
    the spool page's "Used" tile shows it, with "Reset the used counter". Reset one spool
    with `POST /filament/spools/{id}/events` (`kind: consumed_reset`), or 1 to 500 spools
    with `POST /filament/spools/reset-consumed` (agent `filament_spool_add_event` and
    `filament_spools_reset_consumed`). Under `filament_sync.weights` the sync
    resets Bambuddy's counter of a linked spool once per SB reset made after the link
    (R-7149-11; op `reset_consumed`, `POST /inventory/spools/{id}/reset-consumed-counter`), and the pull books a Bambuddy
    reset (its `weight_used_baseline` moved) as a `consumed_reset` of source `bb`, which
    is never sent back. BB keeps no reset time, so that event is dated at the newest reading
    that still held the old baseline, or BB's `updated_at` when earlier (R-7149-9). No
    baseline column is kept.
- Codes (#6955): each place has a number and the code `L-<n>`, each spool `S-<n>`. A code
  opens at `/x/<code>` (`frontend/src/pages/CodePage.tsx`; route `GET /api/v1/codes/{code}`,
  `backend/scadbuddy/api/codes.py`). Any case works, with or without the dash. A number is
  never reused, and an archived thing still resolves.
- Printer places: a printer has its own place (`filament_locations.printer_root`, kind
  "Printer"). Its AMS units and external holder hang under it
  (`LocationStore.ensure_printer_on`, `backend/scadbuddy/filament/locations.py`). A printer
  label request makes the place when it is missing, and so does a tray capture whose printer
  has a unit place or external holder with no live parent (`LocationStore.hang_orphans_on`).
- Labels: a place's QR is `<base>/x/L-<n>`. The labels page (`/filament/labels`) prints places
  with `?locations=` and printers with `?printers=` (one printer's from its place page, every
  printer's from the Locations tab). The printer labels first make the place of each AMS unit,
  slot and external holder the printer's latest report lists; a printer with no such report is
  named on the page. Spool labels keep `scadbuddy:spool:<id>`. A place links to its own page,
  `/filament/locations/<id>`.
- Label templates and the PDF (#7157, plan
  `docs/superpowers/plans/2026-11-23-label-templates-pdf.md`): the labels page offers SB's
  50 x 25 mm roll label, Bambuddy's six templates (two box labels, two AMS holder labels and
  the Avery 5160 and L7160 sheets) and a custom size, and saves the choice in
  `filament_label_template`. A sheet template prints on its own grid. "Download PDF" asks
  `POST /api/v1/filament/labels` (`backend/scadbuddy/filament/label_pdf.py`, ReportLab) for
  the same labels at the same template and saves it through `downloadBlob`, so it works
  inside Bambuddy's frame. A place label's PDF needs the label address or an https public
  URL (409 `no_label_base` otherwise). The browser print stays.
  The agent reads codes with `filament_code_resolve`, `filament_location_get` and
  `filament_printer_labels`, and the label sizes with `filament_label_templates`; the PDF
  route has no tool (R-7157-10).

## Tests

- Backend:
  - `backend/tests/filament/`;
  - `backend/tests/api/test_filament*.py`, `test_tray_spools.py`, `test_print_filaments.py`;
  - the create operations (#7150): `test_filament_spool_create_ops.py`, and create from slot
    in `test_tray_spools.py`;
  - bulk edit, archive and restore (#7151): `test_filament_spool_bulk_ops.py`;
  - `backend/tests/test_spool_refresh_replay.py`;
  - `backend/tests/profile_fetch/`;
  - `backend/tests/api/test_printer_kprofiles.py`, `backend/tests/bambuddy/test_client_kprofiles.py`;
  - `backend/tests/api/test_printer_slots.py`, `backend/tests/bambuddy/test_client_slots.py`.
- Frontend: `frontend/src/components/settings/PrinterKProfiles.test.tsx` (mocks `frontend/src/mocks/features/printerKProfiles.ts`),
  `frontend/src/components/settings/PrinterAmsSlots.test.tsx` (mocks `frontend/src/mocks/features/printerAmsSlots.ts`),
  and the AMS unit place's rename in `frontend/src/pages/filament/LocationsTab.test.tsx`;
  the #7151 operations in `components/filament/BulkBar.test.tsx`, `pages/filament/LocationPage.test.tsx`,
  `pages/filament/SpoolsTab.search.test.tsx` and `pages/filament/TagsTab.test.tsx`.
- Agent: `agent/test/printerSlots.test.ts`.

## Limits and known issues

- The filament epic is [#6260](https://github.com/eh-homelab/ScadBuddy/issues/6260).
- K-profile writes need Bambuddy's `KPROFILES_CREATE`, `_UPDATE` and `_DELETE`, which no
  Bambuddy API key can hold
  ([`core/auth.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/core/auth.py)
  lines 305-307): while Bambuddy's auth is on, every write is the scope problem naming
  `K-profiles (admin only)`. The assistant has no K-profile tool yet
  ([#7162](https://github.com/eh-homelab/ScadBuddy/issues/7162)).
- A slot preset's and an AMS label's writes need Bambuddy's `PRINTERS_UPDATE`, which no
  Bambuddy API key can hold
  ([`core/auth.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/core/auth.py)
  line 280): while Bambuddy's auth is on, they are the scope problem naming
  `Printers (admin only)`. Renaming an AMS unit's place on Filament → Locations does not write
  Bambuddy's label.
- A slot write reads the printer's state, then writes: a print started between the two calls
  is not caught by ScadBuddy (Bambuddy's own reset refuses it).
- Parity with Bambuddy 1.2.6b1 (#6270) is not complete yet: [filament-parity.md](filament-parity.md)
  lists every §11 item, its evidence, and the gap issue for each miss.
- Filament profiles in Postgres, and their follow-ups, are still open:
  [#2800](https://github.com/eh-homelab/ScadBuddy/issues/2800),
  [#2801](https://github.com/eh-homelab/ScadBuddy/issues/2801),
  [#2802](https://github.com/eh-homelab/ScadBuddy/issues/2802).
