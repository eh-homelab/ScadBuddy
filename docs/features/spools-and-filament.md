# Spools and filament

## What it does

The **Filament** section is ScadBuddy's own filament inventory. It covers:

- spools and spool types, with tags, presets and K values;
- catalogs (brands, materials, colours and more);
- suppliers, price sheets, invoices and a shopping list;
- locations with humidity and temperature sensors;
- RFID tags, NTAG labels, and printed QR labels for spools, places, printers, AMS units and slots;
- a printer's K-profiles (pressure advance) through Bambuddy: list, set, batch set, delete and notes;
- a ledger of spool events, with history, metrics and a forecast.

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

Tray changes and print ends are recorded when a printer report is written
(`backend/scadbuddy/filament/capture.py`). They are not a workflow of their own.

A SpoolBuddy scale is tared through Bambuddy (#7155): the spool page's "Scale" block
(`frontend/src/components/filament/ScalePanel.tsx`) asks first, then
`POST /filament/spoolbuddy/devices/{device_id}/tare` (`backend/scadbuddy/filament/scale.py`)
asks Bambuddy to queue the tare. The scale zeroes at the device's next check-in. A device with
another command pending (such as a queued NTAG write) is refused, because a tare would replace
it. The agent's `filament_spoolbuddy_tare` is `outward`. The rulings are in
`docs/superpowers/plans/2026-11-23-spoolbuddy-tare.md`.

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
configure send. After each refresh of the kept presets, a binding that still names a
Bambuddy local preset is moved to a ScadBuddy preset made from the kept copy, and a preset
made in Bambuddy takes a new version when Bambuddy edits it
(`backend/scadbuddy/filament/sb_presets.py`). Until the fetch writes ScadBuddy presets
itself (#7176's second PR), a re-fetch still imports into Bambuddy: at a key whose `3dfp`
preset was already adopted, the new Bambuddy id is adopted as a second, `bambuddy` preset
(plan Decision 6).

A printer's K-profiles (#7154, plan
`docs/superpowers/plans/2026-11-23-printer-kprofiles.md`) are read from the printer through
the Bambuddy printer its registry row links, and shown on its card in Settings → Printers
(closed until opened: each read asks the printer over MQTT). Each write (set, batch set,
delete, a note's set and delete) is an operation on the `bambuddy` queue, so the print
worker makes the call. Set, batch and delete run once: Bambuddy dedupes none of them, and a
write the printer did not acknowledge answers `confirmed: false`. ScadBuddy keeps no copy;
the notes live in Bambuddy, under the keys its own K-Profiles page uses.

Fetching presets is the `ProfileFetch` workflow on the profile-fetch worker, and a 3DFP
search or add is its `TdfpSearch` or `TdfpCreate` workflow (#7025, `filament/tdfp.py`)
([processes.md](../architecture/processes.md#profile-fetch-worker-and-browser)).

## Code

- Routes:
  - `backend/scadbuddy/api/filament.py:220` and `backend/scadbuddy/api/filament_purchasing.py:46` (both under `/filament`);
  - `backend/scadbuddy/api/tray_spools.py:44` (a tray's spool link, and create from slot);
  - `backend/scadbuddy/api/filament_profiles.py:26`;
  - `backend/scadbuddy/api/printer_kprofiles.py:42` (a printer's K-profiles, under `/printers/{id}/kprofiles`).
- UI:
  - the **Filament** nav item (`frontend/src/components/AppShell.tsx:111`);
  - its routes (`frontend/src/App.tsx:119-135`);
  - `frontend/src/pages/filament/FilamentPage.tsx:45`, `SpoolPage.tsx:408`;
  - `frontend/src/components/filament/`;
  - Settings → Printing → Filament (`frontend/src/pages/settings/sections/filament.tsx:684`);
  - the print dialog's picker (`frontend/src/components/FilamentPicker.tsx:358`);
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
- Agent tools: `agent/src/tools/filament.ts`, `filamentPurchasing.ts`, `traySpools.ts`.

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
  `filament_drying_temp_c` and `filament_drying_hours` (#7156: the drying a spool is offered
  when its spool, type, subtype and material set none; empty by default, so none is offered),
  `filament_auto_add_unknown_rfid` (#7156, stored only, on by default: on leaves Bambuddy's own
  auto-add of unknown RFID spools as it is, so Bambuddy may add one and the pull brings it in,
  unless the RFID override mode holds it off; off holds Bambuddy's auto-add off as the override
  mode does, so no spool is made for an unknown tag; the value Bambuddy had goes back only when
  neither holds. ScadBuddy's capture makes no spool from a tag itself),
  `filament_label_base_url` (an https origin of at most 64 characters, live; unset, labels use
  `public_url` reduced to its https origin, then the page's own origin).
- Bambuddy's spool fields (#7149, plan
  `docs/superpowers/plans/2026-11-23-spool-bb-fields.md`,
  migration `20261123T0609Z_filament_spool_bb_fields.sql`):
  - `data_origin` says how the spool's data came in, in Bambuddy's words (`rfid_auto`,
    `rfid_linked`, `nfc_link`, `opentag3d`). It is read only. The pull takes Bambuddy's
    value; a person's tag bind sets `rfid_linked` when the tag's stored kind is a Bambu
    tag, else `nfc_link`; a confirmed NTAG write sets `opentag3d`.
    The push sends it when SB has one, and the tag push sends it to `link-tag`.
  - `added_full` (a person edits it) and `tag_encoded_at` (Bambuddy's `encode_time`, set
    by a confirmed NTAG write) come from Bambuddy on the pull. Bambuddy takes no write of
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
  The agent reads codes with `filament_code_resolve`, `filament_location_get` and
  `filament_printer_labels`.

## Tests

- Backend:
  - `backend/tests/filament/`;
  - `backend/tests/api/test_filament*.py`, `test_tray_spools.py`, `test_print_filaments.py`;
  - the create operations (#7150): `test_filament_spool_create_ops.py`, and create from slot
    in `test_tray_spools.py`;
  - `backend/tests/test_spool_refresh_replay.py`;
  - `backend/tests/profile_fetch/`;
  - `backend/tests/api/test_printer_kprofiles.py`, `backend/tests/bambuddy/test_client_kprofiles.py`.
- Frontend: `frontend/src/components/settings/PrinterKProfiles.test.tsx` (mocks `frontend/src/mocks/features/printerKProfiles.ts`).

## Limits and known issues

- The filament epic is [#6260](https://github.com/eh-homelab/ScadBuddy/issues/6260).
- K-profile writes need Bambuddy's `KPROFILES_CREATE`, `_UPDATE` and `_DELETE`, which no
  Bambuddy API key can hold
  ([`core/auth.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/core/auth.py)
  lines 305-307): while Bambuddy's auth is on, every write is the scope problem naming
  `K-profiles (admin only)`. The assistant has no K-profile tool yet
  ([#7162](https://github.com/eh-homelab/ScadBuddy/issues/7162)).
- Parity with Bambuddy 1.2.6b1 (#6270) is not complete yet: [filament-parity.md](filament-parity.md)
  lists every §11 item, its evidence, and the gap issue for each miss.
- Filament profiles in Postgres, and their follow-ups, are still open:
  [#2800](https://github.com/eh-homelab/ScadBuddy/issues/2800),
  [#2801](https://github.com/eh-homelab/ScadBuddy/issues/2801),
  [#2802](https://github.com/eh-homelab/ScadBuddy/issues/2802).
