# Spools and filament

## What it does

The print dialog lists spools: those Bambuddy knows, and ScadBuddy's own spools. Trays
can be linked to a spool, used grams are recorded per print, and filament types carry
presets and k-values. The filament inventory (catalogs, spools, tags, locations, an event
ledger) can pull from Bambuddy. Filament profiles (#717) bundle print settings for a
filament.

## How it works

A spool overlay merges provider records (Bambuddy at priority 100, ScadBuddy's own at 10,
`backend/scadbuddy/spools/spool_providers.py:1-20`) and is refreshed by the `SpoolRefresh`
workflow on a Schedule. The newer filament model (#6261) lives in the `filament_*`
tables; the older native spool tables were copied into it and are not dropped yet. The
design is the [filament-management spec](../superpowers/specs/2026-11-14-filament-management-design.md).
For adding purchased spools see the repo skill `.claude/skills/filament-intake`.

## Code

- Routes: `backend/scadbuddy/api/filament.py:45` (`/filament`), `backend/scadbuddy/api/native_spools.py:30`,
  `backend/scadbuddy/api/tray_spools.py:34`, `backend/scadbuddy/api/filament_profiles.py:26`.
- Overlay: `backend/scadbuddy/spools/` (`overlay.py`, `providers.py`, `native.py`,
  `activities.py:80-107`); `backend/scadbuddy/workflows/spool_refresh.py:69`.
- Filament: `backend/scadbuddy/filament/` (`catalog.py`, `types.py`, `spools.py`,
  `ledger.py`, `locations.py`, `bambuddy_pull.py`).
- UI: `frontend/src/components/FilamentPicker.tsx:240`,
  `frontend/src/components/settings/NativeSpools.tsx:84`,
  `frontend/src/pages/settings/SpoolInventoryPanel.tsx:43`.
- Agent tools: `agent/src/tools/filament.ts`, `nativeSpools.ts`, `traySpools.ts`.

## Data

- Tables: `physical_spools`, `spool_provider_records`, `spool_record_links`,
  `tray_spool_links`, `native_spools`, `native_spool_uses`, `filament_*`
  (`backend/scadbuddy/migrations/20261114T1300Z_filament_catalogs.sql` and `…1301Z…`).
- Setting: `spool_overlay_refresh_seconds`.

## Tests

- Backend: `backend/tests/spools/`, `backend/tests/filament/`,
  `backend/tests/api/test_native_spools.py`, `test_filament*.py`, `test_tray_spools.py`,
  `backend/tests/test_spool_refresh_replay.py`.

## Limits and known issues

- No UI calls `/filament/*` yet: the Filament section is
  [#6262](https://github.com/eh-homelab/ScadBuddy/issues/6262).
- Capture from tray changes and prints
  ([#6263](https://github.com/eh-homelab/ScadBuddy/issues/6263)), RFID
  ([#6264](https://github.com/eh-homelab/ScadBuddy/issues/6264)), sync with Bambuddy
  ([#6265](https://github.com/eh-homelab/ScadBuddy/issues/6265)) and dropping the old
  tables ([#6348](https://github.com/eh-homelab/ScadBuddy/issues/6348)) are open; the epic is
  [#6260](https://github.com/eh-homelab/ScadBuddy/issues/6260).
- Filament profiles in Postgres and their follow-ups:
  [#2800](https://github.com/eh-homelab/ScadBuddy/issues/2800),
  [#2801](https://github.com/eh-homelab/ScadBuddy/issues/2801),
  [#2802](https://github.com/eh-homelab/ScadBuddy/issues/2802).
