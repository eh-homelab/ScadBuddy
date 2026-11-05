# Printer signals on the H2C

Two sources: Bambuddy's `GET /printers/1/status` (polled, cheap, enough for
watching) and the printer's own MQTT report (`scripts/printer_report.py`, authoritative
for mapping and stages).

## Bambuddy status fields

| Field | Values seen | Notes |
|---|---|---|
| `state` | `IDLE`, `PREPARE`, `RUNNING`, `PAUSE`, `FINISH`, `FAILED` | a stopped or cancelled job is `FAILED` |
| `subtask_name` | `scadbuddy-<run8>-p1` (LAN), library filename stem (queue) | the previous job's name persists until the next starts |
| `layer_num` / `total_layers` | 0 during prep | 22 layers for a 4.4 mm keychain at 0.2 mm |
| `progress` | 0..100 | jumps at filament changes |
| `stg_cur_name` | `Preparing`, `Homing toolhead`, `Printing`, `Changing filament`, `None` | |
| `remaining_time` | minutes | rough |
| `awaiting_plate_clear` | bool | Bambuddy's hold after a finish when `require_plate_clear` is on |
| `hms_errors[].code` | `0x20005`, `0x20006` always present; `0x4004` briefly after a send | ignore these three |
| `print_error` | usually `None` here even when the printer reports one | read MQTT for the real code |

## MQTT report fields (pushall)

| Field | Meaning |
|---|---|
| `gcode_state` | same vocabulary as `state` above |
| `print_error` | int; `0` clean; `0x0300400C` (50348044) after a user stop; `83902468` seen during a normal start |
| `mapping` | one int per filament slot: `ams_id * 256 + slot` (0-based); `65535` = unmapped |
| `stg` | the stage ids the job queued; `stg_cur` the current one (`-1` idle) |
| `subtask_name`, `mc_percent`, `layer_num`, `total_layer_num` | as above |
| `ams.tray_now` | `255` when no tray is loaded |

Stage ids seen on this printer, named from Bambu's public stage table (ha-bambulab's
`CURRENT_STAGE_IDS`, Bambu Studio's `get_stage_string`; both end at 77):

| id | stage |
|---|---|
| 1 | auto bed levelling |
| 3 | XY mech sweep (vibration compensation) |
| 4 | changing filament |
| 8 | calibrating extrusion (flow dynamics) |
| 13 | homing toolhead |
| 14 | cleaning nozzle tip |
| 29 | cooling chamber |
| 39 | nozzle offset calibration |
| 54 | waiting for heatbed temperature |
| 72 | hotend type detection |
| 74 | heatbed foreign-object detection |
| 79 | newer H2-series firmware stage, no public name |

A stage in `stg` is queued, not necessarily run: under `auto` the printer drops a
calibration it did recently. The list looked identical for the auto and forced jobs
here; the forced jobs spent about five minutes longer before layer 1.

## Tray and mapping arithmetic

- Tray id as ScadBuddy's LAN path takes it: `-(ams_id * 16 + slot_1based)`. AMS-A slot 4 is `-4`, AMS-B slot 4 is `-20`, AMS-D slots 1..4 are `-49..-52`. HT units start at ams id 128, so HT-A slot 1 is `-2049`.
- Flat `ams_mapping` entry as ScadBuddy and Bambuddy send it: `ams_id * 4 + slot_0based` (AMS-B slot 4 is `7`, AMS-D slot 3 is `14`).
- Firmware's echo in `mapping`: `ams_id * 256 + slot_0based` (`259`, `770`).

`scripts/ams_mapping.py` converts between the three and flags `65535`.

## Timings seen (cap-keychain plates, 0.2 mm, 3 colours)

| Plate | Prep to layer 1 | Total |
|---|---|---|
| 12 tags, LAN (auto calibrations) | 10.5 min | 4 h 40 |
| 12 tags, queue (forced calibrations) | 15.5 min | 4 h 45 |
| 9 tags | 11 min | 3 h 30 |
| 3 tags | 15 min | 1 h 40 |
