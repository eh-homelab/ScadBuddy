# AMS slot control through Bambuddy (#7153)

ScadBuddy (SB) controls a printer's AMS slots through Bambuddy (BB), as BB's own slot
menu does: configure a slot by hand, choose the slot's slicer preset, refresh and reset a
slot, load and unload filament, and read and write the AMS labels. Before this, SB
configured a slot only automatically, and only for an override-tag spool
(`filament/rfid_slots.py`). Gap found by the §11 parity sweep (#6270; spec
`docs/superpowers/specs/2026-11-14-filament-management-design.md` §11, line "AMS:
assignments, configure slot, slot presets, AMS labels, refresh and reset slot, load and
unload"). Epic #6260.

## What BB does, and where it was read

The OpenAPI fixture `backend/tests/fixtures/bambuddy-openapi-1.2.6b1.json` lists every
route below with no parameters or bodies, and no recording holds one. So they were read
from BB's source at tag `v1.3.1`, the version prod runs
(`backend/tests/bambuddy/recordings/README.md`). No call reached a live Bambuddy.

- [`backend/app/api/routes/printers.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/printers.py):
  - `GET /printers/{id}/slot-presets` (line 2678) answers `{global_tray_id: {ams_id,
    tray_id, preset_id, preset_name, tray_info_idx}}`. `PUT
    …/slot-presets/{ams}/{tray}` (line 2733) takes `preset_id`, `preset_name`,
    `preset_source` (default `cloud`) and `tray_info_idx` in the query, and upserts.
    `DELETE …/slot-presets/{ams}/{tray}` (line 2802) deletes it, or nothing. The mapping
    lives in BB's database, not on the printer.
  - `POST /printers/{id}/slots/{ams}/{tray}/configure` (line 2944) takes the slot's values
    in the query and sends `ams_filament_setting` (and `extrusion_cali_sel`) over MQTT. It
    reads no print state.
  - `POST /printers/{id}/ams/{ams}/tray/{tray}/reset` (line 3491) clears the slot, deletes
    its slot preset, and refuses a printing printer with a 409.
  - `GET /printers/{id}/ams-labels` (line 3539) answers `{ams_id: label}`. `PUT
    …/ams-labels/{ams}` (line 3597) takes `{label (1-100), ams_serial (0-50)}` and stores
    the label under the unit's serial (else `p<printer>a<ams>`). `DELETE
    …/ams-labels/{ams}?ams_serial=` (line 3639) deletes both keys.
  - `POST /printers/{id}/ams/{ams}/slot/{slot}/refresh` (line 4480) re-reads the slot's
    RFID. It refuses while filament is loaded (`tray_now` not 255,
    [`bambu_mqtt.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/services/bambu_mqtt.py)
    line 8591) with a 400.
  - `POST /printers/{id}/ams/load?tray_id=&extruder_id=` (line 4762) and `POST
    …/ams/unload?tray_id=` (line 4836). `tray_id` is the global tray: `ams * 4 + slot` for
    AMS 0-3, the unit id for an AMS-HT (128-135), 254 for the external spool or Ext-L, 255
    for Ext-R. `extruder_id` only matters with the Filament Track Switch fitted. An unload
    of a slot no hotend is loaded from is a 409. Neither reads the print state.
- Permissions ([`backend/app/core/auth.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/core/auth.py)):
  `PRINTERS_READ` maps to `can_read_status` (line 92); `PRINTERS_CONTROL` and
  `PRINTERS_AMS_RFID` to `can_control_printer` (lines 150 and 152); `PRINTERS_UPDATE` is
  in `_APIKEY_DENIED_PERMISSIONS` (line 280). So no BB API key can write a slot preset or
  an AMS label while BB's auth is on. Prod's BB had its auth off on 2026-11-17
  (`rfid-routes.json` `measured_2026-11-17`).
- The AMS's own state: `ams_status_main` is 0 when the AMS is idle, 1 while it changes
  filament, 2 while it reads a tag (`bambu_mqtt.py` line 1292). The one recorded 3 (AMS
  assist) is on a `RUNNING` printer (`printer-status.json`), so only 1 and 2 are busy.
  Known limit: Ext-R's global tray, 255, is also `tray_now` for nothing loaded, so Ext-R
  never shows `loaded` and never refuses `already_loaded`. The status's `extruder_slots`
  might tell them apart, but the only recording shows Ext-R's empty form (ams 254, slot
  255), not a loaded one, so this is left to the port (#7276). The print states BB
  counts as active are `PREPARE`, `SLICING`, `RUNNING` and `PAUSE` (`bambu_mqtt.py` line
  70), SB's `printers/lan_models.py` `BUSY_STATES`.

## Rulings

Each is posted on #7153.

- **R-7153-1 (the source).** BB's parameters and bodies are its source at `v1.3.1`, cited
  above, because the repo holds none. Every item of the issue has a BB route in the
  1.2.6b1 fixture, so every item is built here and no item goes to a follow-up.
- **R-7153-2 (which printer and slot).** SB's routes are keyed by SB's registry printer
  (`/printers/{printer_id}/…`), which must be a physical row linked to a BB printer, as
  R-7154-2: 404 for an unknown id, 409 for a virtual printer or an unlinked row. A slot is
  an AMS 0-15 slot 0-3, an AMS-HT 128-135 slot 0, or the external holder 255 slot 0-1
  (`api/tray_spools.py` `_known_slot`). Load and unload take an AMS 0-3, an AMS-HT or the external holder: BB's
  global tray id is `ams * 4 + slot` up to 15, the unit id for an AMS-HT, and 254 or 255
  for the external holder, so an AMS past 3 has none; refresh takes no external holder (it has no RFID reader).
  Both are 422 otherwise.
- **R-7153-3 (the state first).** Every write that reaches the printer (configure,
  reset, refresh, load, unload) reads BB's live status of the printer first (`GET
  /printers/{id}/status`) and refuses with a 409 and a `code`: `printer_offline` (BB is
  not connected to it), `printer_busy` (a print is active: `PREPARE`, `SLICING`,
  `RUNNING`, `PAUSE`), `ams_busy` (`ams_status_main` is anything but 0, 3 or absent: 1 the AMS changes filament, 2 it
  reads a tag, and a value not seen yet is refused too; 3, the AMS assist, is allowed), `no_slot` (the status shows no such unit or slot). Load also refuses
  `tray_empty` (the slot holds no spool) and `already_loaded` (`tray_now` names it);
  refresh refuses `filament_loaded` (`tray_now` is not 255, as BB itself does). On a
  printer with two extruders, load sends BB the extruder the slot feeds; when the status
  does not say which, load refuses `extruder_unknown`. The read
  and the write are two calls, so a print started between them is not caught; BB's reset
  refuses that case itself.
- **R-7153-4 (BB's scope).** The status read and the reads take Read Status. Configure,
  reset, refresh, load and unload take Control Printer. Slot preset and AMS label writes
  take a new admin-only scope, `Scope.ADMIN_PRINTERS` ("Printers (admin only)"): BB's 403
  there is SB's 409 that says no BB API key can change a printer's slot presets or AMS
  labels while BB's auth is on.
- **R-7153-5 (direct calls).** The routes call BB in the request, as the tare (#7155) and
  the NTAG write do, not as operations: no `Idempotency-Key`, no dashboard kind. A repeat
  is safe or refused by R-7153-3: configure, reset, a preset and a label are sets, a
  refresh re-reads, a second load while the first runs is `ams_busy` and after it
  `already_loaded`, and a second unload is BB's 409.
- **R-7153-6 (configure by hand, for any spool).** `POST …/trays/{ams}/{tray}/configure`
  takes either a `spool_id` (any SB spool: its material, colour and temperatures, its
  type's preset for the slot's nozzle and its K value, as `rfid_slots.slot_params`
  builds them for an override tag) or the values by hand (material, colour `#RRGGBB` or
  `#RRGGBBAA`, preset id and name, nozzle temperatures, nozzle diameter, and optional
  K). Values sent beside a `spool_id` replace the spool's. The slot's nozzle is the
  body's `nozzle_diameter`, else the one BB's status names for the extruder the slot
  feeds, else a 409 `no_nozzle`. The answer is what was sent. It links no spool to the tray: the tray
  question and a spool's move still do that.
- **R-7153-7 (slot presets).** The slicer preset a slot shows is BB's slot-preset
  mapping. `PUT …/trays/{ams}/{tray}/preset` saves it (`preset_id`, `preset_name`,
  `preset_source`, and the `tray_info_idx` the slot was configured with), `DELETE`
  clears it. Configure does not save it: the UI saves it after every configure, so a
  refused preset write never hides a configure that went out. It saves the preset the
  configure sent, whether typed by hand or a spool's type preset, and clears the saved
  preset when the configure sent none. For a spool's preset the configure's answer names
  the source (`preset_source`): `local` for a ScadBuddy preset (`sb-<id>`, sent as its
  mirror's id, which `POST /local-presets/` made) or a bare BB local preset id, else
  `cloud`; the UI uses the form's source only for an id typed by hand, and hides that
  choice while "From a spool" is picked. Both touch BB's database only, so they read no
  print state.
- **R-7153-8 (AMS labels, in step with SB's place).** `PUT /printers/{id}/ams/{ams}/label`
  writes BB's label first (with the unit's serial from BB's status), then renames SB's
  place of the unit (`LocationStore.ensure_ams_unit_on`) to the label. A refused BB write
  changes nothing. `DELETE` deletes BB's label and names the place `<printer> <unit>`
  again. Once BB's label is written the answer is 200: when no location kind is marked
  AMS, so the unit has no place, `place_problem` is `no_ams_kind`. A name never clashes:
  a printer's places are outside the name index (`filament_locations_name` is `WHERE
  printer_id IS NULL`), so two units may share a label. The Locations tab offers Edit (no Archive) on a unit's place, which was read only like
  a tray's; renaming it there does not write BB. The
  read shows both, so a place out of step shows.
- **R-7153-9 (the read).** `GET /printers/{id}/ams` answers the units and the external
  holder from BB's status, each slot with its material, colour, preset id and name,
  whether it holds a spool and whether it is loaded, BB's saved slot preset, each unit's
  BB label and SB place, and the printer's state (`connected`, `state`, `printing`,
  `ams_busy`, `tray_now`), so the UI can disable what R-7153-3 would refuse.
- **R-7153-10 (the page and the assistant).** The UI is an "AMS slots" disclosure on a
  linked printer's card in Settings → Printers, beside K-profiles, read only when opened.
  Each write asks first in a dialog that names the printer and the slot. The agent has a
  tool per route; the writes are `outward` (approved first). The routes that write no SB
  row are in `api/filament.py` `NOT_NUDGED`; the label routes rename an SB place, so they
  nudge the sync as a location edit does. The rename of an AMS unit's place gets the test
  it lacked (backend and `LocationsTab.test.tsx`).
- **R-7153-11 (a stopgap).** PR #7252 merges as a stopgap (owner, 2026-11-23): its writes
  go through Bambuddy; the port to ScadBuddy's own MQTT path is #7276.

## Tasks

1. Client: the nine calls, `tray_now` and `ams_status_main` and the unit's serial on the
   status, `Scope.ADMIN_PRINTERS` (`bambuddy/client.py`, `models.py`, `errors.py`). respx
   tests.
2. `bambuddy/slot_control.py` (the guard, the bodies, the configure params) and
   `api/printer_slots.py` (one read, nine writes). respx tests through the API.
3. Frontend: `components/settings/PrinterAmsSlots.tsx` on the printer's card, client
   calls, msw mocks, vitest. The AMS place rename test in `LocationsTab.test.tsx`.
4. Agent: tools in `agent/src/tools/printerSlots.ts`. Docs:
   `docs/features/filament-parity.md`, `docs/features/spools-and-filament.md`.
