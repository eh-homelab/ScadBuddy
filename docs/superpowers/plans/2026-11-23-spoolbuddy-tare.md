# SpoolBuddy scale tare (#7155)

A gap of the §11 parity sweep (#6270, `docs/features/filament-parity.md`; spec
`docs/superpowers/specs/2026-11-14-filament-management-design.md` §11, epic #6260). ScadBuddy (SB)
reads the SpoolBuddy scale's weight through Bambuddy (BB), but cannot tare the scale. This plan
adds the tare, as BB does it.

## What BB does

BB's routes are in `backend/tests/fixtures/bambuddy-openapi-1.2.6b1.json` (operation ids only, no
bodies). The bodies come from BB's source at tag v1.3.1, the tag the repo's other SpoolBuddy calls
cite (`backend/scadbuddy/bambuddy/client.py` `spoolbuddy_devices`, `queue_tag_write`). There is no
`v1.2.6b1` tag in maziggy/bambuddy (read with `gh api repos/maziggy/bambuddy/git/refs/tags` on
2026-11-23).

- `POST /api/v1/spoolbuddy/devices/{device_id}/calibration/tare`
  ([routes/spoolbuddy.py#L1305](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/spoolbuddy.py#L1305)):
  no body. It sets the device's `pending_command` to `"tare"` and answers
  `{"status": "ok", "message": "Tare command queued"}`; 404 `Device not registered`. Permission
  `INVENTORY_UPDATE` (L1309). The daemon reads the command at its next heartbeat (L308-L395), zeroes
  the scale, and reports the raw offset to `set-tare`.
- `POST /api/v1/spoolbuddy/devices/{device_id}/calibration/set-tare`
  ([L1322](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/spoolbuddy.py#L1322)):
  body `SetTareRequest {tare_offset: int}`
  ([schemas/spoolbuddy.py#L140](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/schemas/spoolbuddy.py#L140)),
  the daemon's report of the raw ADC offset after a tare. It stores it, sets `last_calibrated_at`,
  and answers `CalibrationResponse {tare_offset, calibration_factor}`.
- A device has one `pending_command` slot. A queued NTAG write is `"write_tag"` (L943) and stays
  there until the write result (heartbeat L344-L362). A tare overwrites the slot (L1317).

## Rulings

Each ruling is also a comment on #7155:
[R-7155-1](https://github.com/eh-homelab/ScadBuddy/issues/7155#issuecomment-7097843959),
[R-7155-2](https://github.com/eh-homelab/ScadBuddy/issues/7155#issuecomment-7097845043),
[R-7155-3](https://github.com/eh-homelab/ScadBuddy/issues/7155#issuecomment-7097846158),
[R-7155-4](https://github.com/eh-homelab/ScadBuddy/issues/7155#issuecomment-7097847557),
[R-7155-5](https://github.com/eh-homelab/ScadBuddy/issues/7155#issuecomment-7097848662),
[R-7155-6](https://github.com/eh-homelab/ScadBuddy/issues/7155#issuecomment-7097849603).

- **R-7155-1.** SB calls `calibration/tare` only. A tare is a request to the device: BB queues it,
  and the scale zeroes at the device's next heartbeat. SB answers that it is queued, not that the
  scale is zeroed.
- **R-7155-2.** SB never calls `set-tare`. It is the daemon's callback with a raw ADC value. A
  value from a person or the agent would move the scale's zero to a number nobody measured. No
  client method, route or tool sends it.
- **R-7155-3.** BB's scope is "Manage Inventory" (`INVENTORY_UPDATE`, which BB maps to
  `can_manage_inventory`), the scope of the NTAG write and cancel. The client declares
  `Scope.MANAGE_INVENTORY`, so a 403 names it.
- **R-7155-4.** SB reads the device list first and refuses a tare that BB would take wrongly: 404
  `no_device` for a device BB does not list, 409 `no_scale` for one with no scale, and 409
  `device_busy` for one with another pending command. A tare over a queued `write_tag` would drop
  that write with no error. A tare already pending is queued again (no harm).
- **R-7155-5.** Who may tare. The browser route is `POST /filament/spoolbuddy/devices/{device_id}/tare`,
  in the same class as the NTAG write (any caller of the API). `device_id` takes the NTAG routes'
  pattern. It writes no SB row, so it does not nudge the sync (`NOT_NUDGED`). The agent's tool
  `filament_spoolbuddy_tare` is `outward` (approved first), the tier of
  `filament_spool_ntag_write` and `filament_spool_ntag_cancel`, the other tools that act on a
  SpoolBuddy device through BB. A tare moves a physical device's zero outside SB, so `outward` is
  the more secure tier than `write`.
- **R-7155-6.** Where. The spool page, in a "Scale" block under the tag panels, on the same page as
  the timeline that shows the scale's `weighed` events. It lists the devices with a scale and has
  "Tare scale", which asks first ("Take everything off the scale first"). The answer says the tare
  is queued; a refusal says why.

## Tasks

1. `SpoolBuddyDevice` gets `has_scale`, `scale_ok` and `last_calibrated_at` (all in BB's
   `DeviceResponse`). `BambuddyClient.tare_scale(device_id)`, operation `spoolbuddy.tare`.
   Tests: respx, `tests/bambuddy/test_client_rfid.py`, `test_recordings_rfid.py` (the route and
   its scope added to the recording's source-read `routes`).
2. `filament/scale.py` `tare` (R-7155-4) and the route; tests `tests/filament/test_scale.py`,
   `tests/api/test_filament.py`.
3. Agent tool, tests in `agent/test/`.
4. Frontend: `api/filament.ts` `tareScale`, the msw mock in `mocks/features/filament/rfid.ts`,
   `components/filament/ScalePanel.tsx` with a vitest test, the spool page.
5. Docs: the parity row to MATCH, `docs/features/spools-and-filament.md`.
