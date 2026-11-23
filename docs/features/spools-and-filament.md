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

Tray changes and print ends are recorded when a printer report is written
(`backend/scadbuddy/filament/capture.py`). They are not a workflow of their own.

Filament presets are ScadBuddy's own (#7176, plan
[2026-11-23](../superpowers/plans/2026-11-23-scadbuddy-filament-presets.md)): the table
`filament_presets`, each document a blob with every version kept. A type or spool names one
by the ref `sb-<id>` in `slicer_filament`. The native slicer reads them from ScadBuddy's
store; Bambuddy knows each one only by its mirror's local preset id
(`EffectivePreset.bambuddy_id`), which its slice path, the spool push and the AMS slot
configure send. After each refresh of the kept presets, a binding that still names a
Bambuddy local preset is moved to a ScadBuddy preset made from the kept copy, and a preset
made in Bambuddy takes a new version when Bambuddy edits it
(`backend/scadbuddy/filament/sb_presets.py`). While the stored setting
`filament_preset_mirror` is on (the default), each preset ScadBuddy owns is copied to
Bambuddy's local presets after a fetch and after each refresh, rewritten in place under the
same Bambuddy id when it changes; a failed copy is the preset's `mirror_error`
(`backend/scadbuddy/filament/preset_mirror.py`).

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

## Code

- Routes:
  - `backend/scadbuddy/api/filament.py:173` and `backend/scadbuddy/api/filament_purchasing.py:46` (both under `/filament`);
  - `backend/scadbuddy/api/tray_spools.py:36` (a tray's spool link);
  - `backend/scadbuddy/api/filament_profiles.py:26`;
  - `backend/scadbuddy/api/printer_kprofiles.py:42` (a printer's K-profiles, under `/printers/{id}/kprofiles`).
- UI:
  - the **Filament** nav item (`frontend/src/components/AppShell.tsx:111`);
  - its routes (`frontend/src/App.tsx:119-135`);
  - `frontend/src/pages/filament/FilamentPage.tsx:45`, `SpoolPage.tsx:330`;
  - `frontend/src/components/filament/`;
  - Settings → Printing → Filament (`frontend/src/pages/settings/sections/filament.tsx:684`);
  - the print dialog's picker (`frontend/src/components/FilamentPicker.tsx:358`);
  - a printer's K-profiles on its card in Settings → Printers (`frontend/src/components/settings/PrinterKProfiles.tsx:83`).
- Core: `backend/scadbuddy/filament/`:
  - the SB spools a print reads: `inventory.py` (`read_sb_spools` at `:901`, `reconcile_trays` at `:448`);
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
  `filament_preset_mirror`,
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
