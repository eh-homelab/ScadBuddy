# Spools and filament

## What it does

The **Filament** section is ScadBuddy's own filament inventory. It covers:

- spools and spool types, with tags, presets and K values;
- catalogs (brands, materials, colours and more);
- suppliers, price sheets, invoices and a shopping list;
- locations with humidity and temperature sensors;
- RFID tags, NTAG labels, and printed QR labels for spools, places, printers, AMS units and slots;
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
  note, presets, K values and shelf place. It leaves out the number, tags, tray, weight
  history and weight lock.
- **Create from slot.** "New spool from this tray" makes a spool from what the printer last
  reported in the tray over the LAN: its material, its `#RRGGBBAA` colour and a tagged
  tray's tag. The spool is placed in the tray, and the tray's previous spool moves to no
  place. ScadBuddy builds it from its own report; nothing reaches Bambuddy. The spool has no
  type yet. The route is `POST /printers/{id}/trays/{ams}/{tray}/new-spool` and needs an
  `Idempotency-Key`. The button is on the native print dialog's tray question and on an
  empty AMS tray's place page.

The agent's `filament_spool_upsert` takes `quantity` and `copy_of`, and `create_tray_spool`
makes a spool from a tray.

Tray changes and print ends are recorded when a printer report is written
(`backend/scadbuddy/filament/capture.py`). They are not a workflow of their own.

Fetching presets is the `ProfileFetch` workflow on the profile-fetch worker, and a 3DFP
search or add is its `TdfpSearch` or `TdfpCreate` workflow (#7025, `filament/tdfp.py`)
([processes.md](../architecture/processes.md#profile-fetch-worker-and-browser)).

## Code

- Routes:
  - `backend/scadbuddy/api/filament.py:214` and `backend/scadbuddy/api/filament_purchasing.py:46` (both under `/filament`);
  - `backend/scadbuddy/api/tray_spools.py:44` (a tray's spool link, and create from slot);
  - `backend/scadbuddy/api/filament_profiles.py:26`.
- UI:
  - the **Filament** nav item (`frontend/src/components/AppShell.tsx:111`);
  - its routes (`frontend/src/App.tsx:119-135`);
  - `frontend/src/pages/filament/FilamentPage.tsx:45`, `SpoolPage.tsx:380`;
  - `frontend/src/components/filament/`;
  - Settings → Printing → Filament (`frontend/src/pages/settings/sections/filament.tsx:684`);
  - the print dialog's picker (`frontend/src/components/FilamentPicker.tsx:358`).
- Core: `backend/scadbuddy/filament/`:
  - the SB spools a print reads: `inventory.py` (`read_sb_spools` at `:957`, `reconcile_trays` at `:453`);
  - `capture.py`, `ledger.py`, `catalog.py`, `types.py`, `locations.py`, `sensors.py`, `invoices.py`, `labels.py`;
  - `backend/scadbuddy/codes/` (code parsing and resolving);
  - `bb_sync_*.py` and `bambuddy_pull.py`;
  - `profile_fetch.py`.
- Workflows:
  - `backend/scadbuddy/workflows/spool_refresh.py:104`;
  - `filament_sync.py:68`, `filament_pull.py:43`, `sensor_notify.py:58`.
- Agent tools: `agent/src/tools/filament.ts`, `filamentPurchasing.ts`, `traySpools.ts`.

## Data

- Tables: `filament_*` (`backend/scadbuddy/migrations/20261114T1300Z_filament_catalogs.sql`,
  `20261114T1301Z…` and later). These cover the catalogs, spool types, spools,
  `filament_spool_events`, tags and bindings, locations and sensors, suppliers and
  invoices, the Bambuddy sync state and links, and `filament_profile_fetches`. The price
  book is `filaments`.
- The old spool overlay and native spool tables were dropped
  (`backend/scadbuddy/migrations/20261118T1235Z_drop_native_spool_tables.sql`).
- Settings: `spool_overlay_refresh_seconds` (the refresh Schedule), `filament_sync`,
  `filament_low_stock_pct`, `filament_default_cost_per_kg`, `profile_fetch_nozzles`,
  `filament_label_base_url` (an https origin of at most 64 characters, live; unset, labels use
  `public_url` reduced to its https origin, then the page's own origin).
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
  - `backend/tests/profile_fetch/`.

## Limits and known issues

- The filament epic is [#6260](https://github.com/eh-homelab/ScadBuddy/issues/6260).
- Parity with Bambuddy 1.2.6b1 (#6270) is not complete yet: [filament-parity.md](filament-parity.md)
  lists every §11 item, its evidence, and the gap issue for each miss.
- Filament profiles in Postgres, and their follow-ups, are still open:
  [#2800](https://github.com/eh-homelab/ScadBuddy/issues/2800),
  [#2801](https://github.com/eh-homelab/ScadBuddy/issues/2801),
  [#2802](https://github.com/eh-homelab/ScadBuddy/issues/2802).
