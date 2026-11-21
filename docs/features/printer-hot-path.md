# Printers and the native hot path

## What it does

ScadBuddy keeps its own printer registry (one identity per serial), synced from Bambuddy
or added by hand, including virtual printers. A printer with a LAN link (its access code)
can be printed to directly, with no Bambuddy call in the path: ScadBuddy slices, uploads
over FTPS, starts the job over MQTT with the AMS and rack mapping, and follows progress
from the printer's own reports. Several plates or copies run in sequence, optionally
waiting for the plate to be cleared. See README
["The native hot path"](../../README.md#the-native-hot-path-3818).

## How it works

`print_check` decides per run whether the path is native: the slicer and library are
native, the store is not local, and the printer has a LAN link and a fresh report. The
sequence is in [workflows.md "Print to the H2C"](../architecture/workflows.md#print-to-the-h2c).
Only the API opens a printer's sealed access code; the print worker gets it over a CRUD
route. `PrinterWatch` keeps one MQTT connection per printer and writes its reports.

## Code

- Decision: `backend/scadbuddy/printers/hot_path.py:22-34`,
  `backend/scadbuddy/bambuddy/print_run.py:2159-2170` (`run_path`), `:2315` (`run_route`).
- LAN client: `backend/scadbuddy/printers/lan.py` (ports `:65-67`, `project_file_command`
  `:174`), `ams_mapping.py:92-139`, `nozzle_mapping.py`.
- Activities: `backend/scadbuddy/printers/print_activities.py` (`lan_upload` `:264`,
  `lan_print` `:307`, `lan_wait` `:432`).
- Workflows: `backend/scadbuddy/workflows/printing.py:584` (`PrintRun`), `:1136`
  (`PrintLanSequence`), `printer_watch.py:79`, `follow.py:70`.
- Registry routes: `backend/scadbuddy/api/printers.py:62` (LAN link `:469`, Bambuddy sync
  `:173`, virtual printers `:214-348`); UI `frontend/src/components/settings/Printers.tsx:40`,
  `VirtualPrinters.tsx:121`, `frontend/src/components/print/LanRunControls.tsx:26`.
- Rack (H2C hotend rack): `backend/scadbuddy/rack/rank.py`, `rack/usage.py`;
  `PUT /print/printers/{id}/rack-algorithm` (`backend/scadbuddy/api/printing.py:528`).

## Data

- Tables: `printers` (sealed access code, pinned certificate, `preferred_path`),
  `printer_reports`, `rack_nozzle_seen`, `rack_nozzle_picks`, `rack_nozzle_prints`
  (`backend/scadbuddy/migrations/20261018T1659Z_printers.sql`).
- Settings: `lan_require_plate_clear`, `printer_rack_algorithms`, `default_printer`.

## Tests

- Backend: `backend/tests/printers/` (lan, lan_ftps, lan_start, ams_mapping,
  nozzle_mapping, watch, a fake printer), `backend/tests/api/test_hot_path_native.py`,
  `test_print_rack.py`, `backend/tests/test_print_plate_clear.py`.
- e2e: `frontend/e2e/hot-path.spec.ts`, `rack.spec.ts`, `printers.spec.ts`,
  `hotend-usage.spec.ts`.
- Real stack: `e2e-stack/` with a fake printer and a Bambuddy recorder that fails every
  call, run by `hot-path-e2e.yml` ([`e2e-stack/CLAUDE.md`](../../e2e-stack/CLAUDE.md),
  `frontend/e2e/real-hot-path.spec.ts`).

## Limits and known issues

- The LAN AMS mapping is "not yet measured on a printer"
  (`backend/scadbuddy/printers/ams_mapping.py:34-36`;
  [#4978](https://github.com/eh-homelab/ScadBuddy/issues/4978)); `ams_mapping2` is computed
  but not sent (`backend/scadbuddy/printers/lan.py:174-199`).
- Transfer URLs are not narrowed to a run's own sliced file
  ([#6122](https://github.com/eh-homelab/ScadBuddy/issues/6122),
  [#6179](https://github.com/eh-homelab/ScadBuddy/issues/6179)).
- Whether the print worker still needs the Bambuddy key:
  [#6189](https://github.com/eh-homelab/ScadBuddy/issues/6189). The epic is
  [#3818](https://github.com/eh-homelab/ScadBuddy/issues/3818).
