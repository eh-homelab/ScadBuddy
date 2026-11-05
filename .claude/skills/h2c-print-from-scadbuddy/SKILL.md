---
name: h2c-print-from-scadbuddy
description: Print a ScadBuddy output or library file on the Bambu H2C (Bambuddy printer 1, "3DP-31B-598") and see it through to a cleared bed. Covers the LAN print_output recipe with negative tray ids, the Bambuddy-queue route for when bed levelling, flow dynamics, nozzle offset or vibration calibration must be forced on, verifying the AMS mapping over MQTT, watching progress, plate-clear holds between plates, stopping and restarting, and what to do when the reply is lost, the printer answers ERROR STATE, or progress reads null. Use this whenever the user says print, send it, reprint, queue it in Bambuddy, stop it, "is it done", "is it mapped", "turn on flow calibration", or asks why a ScadBuddy print did not start, even when they do not name the H2C or ScadBuddy.
---

# Printing a ScadBuddy file on the H2C

The H2C is reached two ways, and they are not interchangeable. ScadBuddy's own
`print_output` talks to the printer over the LAN: fast, no queue, no approval,
calibrations left to the printer. Bambuddy's print queue talks to the same printer
with the full Bambu Studio job options and a plate-clear hold between jobs. Most
of the trouble in this area comes from using one path and expecting the other's
behaviour, so decide the path first.

## Fixed facts for this estate

- ScadBuddy: `https://scadbuddy.internal.nullreference.io/api/v1`, MCP server `scadbuddy`.
- Bambuddy: `https://bambuddy.internal.nullreference.io/api/v1`. No API key is checked on this instance.
- Printer: Bambuddy printer id `1`, name `3DP-31B-598`, model H2C, two 0.4 mm High Flow nozzles plus a hotend rack, four AMS units (A to D) and an AMS-HT.
- ScadBuddy registry printer `a94c3a42-66d5-432a-b819-4f7df1079f50` links Bambuddy printer 1 **and** is listed as a LAN printer. Every `print_output` aimed at it, whether by `printer` or by `printer_id: 1`, takes the LAN path. ScadBuddy cannot queue into Bambuddy for this printer at all.
- MQTT straight to the printer: port 8883, user `bblp`, access code and IP from `GET /printers/1`. `scripts/printer_report.py` does this for you.
- Tray ids and spool positions move. Read them live from `get_print_choices` or `get_print_filaments` for the file; never reuse a number from an older session.

## Pick the path

| You need | Path |
|---|---|
| Print now, printer idle, calibrations left to the printer's "auto" | LAN, section 1 |
| Bed levelling, flow dynamics, nozzle offset or vibration calibration **forced on** | Bambuddy queue, section 3. The LAN path hardcodes them and ignores `options` |
| Several plates, each started once the bed is cleared | Bambuddy queue. LAN has no queue; a send to a busy printer is refused |
| The printer sits in `FAILED` after a stop | Bambuddy queue, section 4 |

## 1. LAN path

```
print_output{
  library_file_id: <id>            # or output_id
  printer: "a94c3a42-66d5-432a-b819-4f7df1079f50"
  plate_id: 1
  filament_plan: {slots: [{slot_id: 1, spool_id: -20}, ...]}   # NEGATIVE tray ids
  nozzles: [{size: "0.4", flow: "high_flow"}, {size: "0.4", flow: "high_flow"}]
  tier: "standard", bed_type: "Textured PEI Plate"
  prime_tower: {enabled: true, no_sparse_layers: false}
}
```

- Slot ids are the tray ids `get_print_choices` lists, negative. A positive Bambuddy spool id answers `Slot N has no spool chosen` (HTTP 422).
- Never put the AMS-HT tray (`-2049`, HT-A) in the plan. ScadBuddy sends a flat `ams_mapping` the firmware turns into `65535` (unmapped) for an HT, and the print pauses at that filament's first load (HMS `0703_8012`). Move that colour to a regular AMS slot.
- `no_sparse_layers: true` makes Bambu Studio demand 96 mm between the tower and any object taller than 4 mm. A full plate cannot give that; leave it `false` unless the plate is small. The `h2c-plate-layout` skill has the geometry.
- The call blocks on the whole run (plan, slice, upload, start). The MCP client backgrounds it after about two minutes; wait for the notification.

**After the send, verify, do not trust.** Within a minute of the job starting, run
`uv run --with paho-mqtt scripts/printer_report.py` and read `mapping`: one entry per
slot, encoded `ams_id * 256 + slot`, none equal to `65535`.
`scripts/ams_mapping.py decode 259 770 771` names the trays. Then start
`scripts/watch_printer.sh` in the background; it prints state transitions and exits
when the job ends.

The job's name on the printer is `scadbuddy-<first 8 of run id>-p<plate>`. Bambuddy's
`GET /printers/1/status` shows it as `subtask_name`, with `state`, `layer_num`,
`total_layers`, `progress`, `stg_cur_name` and `awaiting_plate_clear`.

**If the reply never comes back** (transport dropped, HTTP 500 "Workflow Task in failed
state", "did not answer"), the run usually still happened: the workflow is durable, only
the HTTP reply died. Do not resend on the strength of `get_print_progress` reading
`null`; that read lied twice in one night (an API rollout and a FollowPrint that
settled on the previous job's FINISH). Check the printer's `subtask_name` first, then
the run's Temporal history with the `scadbuddy-run-forensics` skill. A resend while
the printer is busy is refused with `ERROR STATE` and prints nothing, so the cost of a
mistaken resend is only time, but the cost of not checking is a confused order record.

## 2. Reading the printer

`references/printer-signals.md` lists every field and code seen on this printer. The
short version:

- `state`: `PREPARE` and `RUNNING` are live; `FINISH`, `FAILED` and `IDLE` are not. A stopped job reports `FAILED`.
- Prep before layer 1 takes 10 to 17 minutes (homing, levelling, calibrations). A 12-tag keychain plate takes about 4.5 h, 9 tags 3.5 h, 3 tags 1.6 h.
- HMS `0x20005`, `0x20006` sit on this printer permanently and `0x4004` ("device busy") appears for a moment after a send; none of them is a problem.
- A camera frame is `get_printer_camera{printer_id: 1}`; send it to the user with SendUserFile at FINISH and before any send that assumes an empty bed.

## 3. Bambuddy queue path

Bambuddy's queue takes a **sliced** 3MF (`Metadata/plate_1.gcode` inside). ScadBuddy
holds the sliced file in its native library after every run, succeeded or refused:

1. If the LAN run succeeded, its result has `sliced_library_file_id`. If the printer was busy, run the LAN `print_output` anyway: it slices, stores the sliced file, then is refused `ERROR STATE`. The sliced id is in the run's Temporal history, the `print_sliced_upload` activity result (`scadbuddy-run-forensics`). The sliced files do not show in the library listing; `GET /print/library/<id>/file` serves them.
2. `scripts/queue_sliced.sh <sliced id> <name> '<flat mapping>'` downloads the file, checks it holds gcode, uploads it to Bambuddy's library and queues it on printer 1 with all four calibrations on. Build the flat mapping with `scripts/ams_mapping.py encode -20 -51 -52` (`ams_id * 4 + slot`, 0-based).
3. Watch the queue item: `GET /queue/<id>` goes `pending`, `printing`, `completed`. Bambuddy's job name is the library filename stem.

How the scheduler behaves, so the order does not surprise you:

- Items with `manual_start: true` are skipped by auto-dispatch. A long tail of such items ahead of yours does not block you.
- With `queue_shortest_first` on (it is), the shortest **estimate** goes first, whatever the positions say.
- `require_plate_clear` is on. After every finish the printer status shows `awaiting_plate_clear: true` and nothing dispatches until someone confirms, in the Bambuddy UI or with `POST /printers/1/clear-plate`. Look at the camera before confirming; this hold is the only thing stopping a print onto a full bed.
- The dispatch log is in the Bambuddy pod: `kubectl -n bambuddy logs deploy/bambuddy | grep print_scheduler`.

The queue option names are `bed_levelling`, `flow_cali`, `nozzle_offset_cali` (`"off" | "on" | "auto"`), `vibration_cali`, `layer_inspect`, `timelapse` (booleans). "auto" still queues the stage and lets the printer skip it at run time, so the stage list on the printer looks the same either way; the forced runs here took about five minutes longer in prep.

## 4. Stopping, and starting again

`POST /printers/1/print/stop` stops within seconds. The printer then sits in `FAILED`
with `print_error 0x0300400C`, and stays there: `POST /printers/1/hms/clear`, the MQTT
`clean_print_error` (even with the code and subtask id) and the `uiop` dialog close all
answer success and change nothing. A `project_file` sent by hand with zero
`project_id`/`task_id`/`subtask_id`, which is what ScadBuddy also sends, was refused
`ERROR STATE` three times in that state. Bambuddy's queue dispatch, which sends
non-zero ids, was accepted a minute later. So after a stop, queue the next job in
Bambuddy; do not fight the state.

## Troubleshooting

| Symptom | Meaning | Do |
|---|---|---|
| `Slot N has no spool chosen` (422) | positive spool ids on a LAN printer | use negative tray ids |
| `refused the print: ERROR STATE` (502) | printer busy, or `FAILED` after a stop | wait for FINISH and a cleared bed; after a stop use the queue |
| HMS `0703_8012` at a filament load, `mapping` has `65535` | HT tray in the plan | stop, re-plan with a regular AMS slot |
| `relative position of the model and the prime tower` (422 from the slicer) | `no_sparse_layers: true` with objects inside 96 mm of the tower | set it `false` or shrink the plate |
| HTTP 500 `Workflow Task in failed state`, transport dropped | reply lost, run probably alive | printer status, then forensics; no blind resend |
| `get_print_progress` is `null` for a running job | ScadBuddy restarted or FollowPrint settled early | trust the printer, not the record |
| `ScadBuddy failed unexpectedly while preparing this print` | usually a heartbeat timeout on `print_sliced_upload` | rerun; the slice is cached by preset key |
| Queue item stays `pending` with the printer idle | plate-clear hold, or an item already printing | check `awaiting_plate_clear`, confirm the plate |

## Scripts

- `scripts/printer_report.py [out.json]`: one MQTT `pushall`, prints state, job, error, mapping and stage list. Needs `paho-mqtt`: `uv run --with paho-mqtt scripts/printer_report.py`.
- `scripts/ams_mapping.py encode|decode|check`: tray id to flat mapping, firmware mapping to tray names, unmapped check on a saved report.
- `scripts/watch_printer.sh [previous job name]`: waits through a hold for a new job, reads its mapping, polls to the end. Run in the background.
- `scripts/queue_sliced.sh <sliced id> <name> '<mapping>' [--cali auto] [--timelapse]`: ScadBuddy sliced file into Bambuddy's queue with calibrations forced on. Pass `--timelapse` for a print that will be an order attempt: the attempt's timelapse comes from the archive, and an archive of a print queued without it has none to pull.

Related skills: `h2c-plate-layout` (where things go on the bed), `scadbuddy-run-forensics` (what a run did).
