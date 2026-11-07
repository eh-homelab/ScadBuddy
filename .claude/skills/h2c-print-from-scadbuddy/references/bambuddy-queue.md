# Bambuddy's print queue, as used from here

Base: `https://bambuddy.internal.nullreference.io/api/v1`. No API key is checked.

## Upload a sliced 3MF

```
curl -F "file=@<file>.3mf;type=model/3mf" "$B/library/files?generate_stl_thumbnails=false"
```

Answers `{"id": <library file id>, "file_type": "gcode.3mf", ...}` when the 3MF holds
gcode. A 3MF without `Metadata/plate_N.gcode` uploads as `3mf` and the queue would
slice it with Bambuddy's own presets, which is not what you verified.

## Queue it

```
POST /queue/
{"printer_id": 1, "library_file_id": <id>, "plate_id": 1,
 "ams_mapping": [7, 14, 15], "use_ams": true,
 "bed_levelling": "on", "flow_cali": "on", "nozzle_offset_cali": "on",
 "vibration_cali": true, "layer_inspect": true, "timelapse": true,
 "quantity": 1, "require_previous_success": false}
```

Answers the item: `id`, `status: pending`, `position`, and the options echoed. `timelapse: true` is the `--timelapse` case; `queue_sliced.sh` sends `false` by default. It makes the printer record a video. It does not reach the archive by itself: after the print, pull it (the print's page, or `POST /prints/{archive_id}/timelapse/pull`) before an order attempt copies it. Off, there is nothing to pull. The
tri-state options are `"off" | "on" | "auto"`; on the wire Bambuddy sends each as a
bool plus an int (`bed_leveling` + `auto_bed_leveling`, `flow_cali` + `extrude_cali_flag`,
`nozzle_offset_cali` int, dual-nozzle only), the same two-field shape Bambu Studio
sends, with non-zero `project_id`/`task_id`/`subtask_id`.

## Read it

- `GET /queue/<id>`: `status` (`pending`, `printing`, `completed`, `cancelled`, `failed`), `started_at`, `completed_at`, `archive_id`.
- `GET /queue/?printer_id=1`: everything, including hundreds of old cancelled rows. Filter on status.
- `POST /queue/<id>/start`: only on `pending`; the scheduler usually beats you to it.
- `POST /queue/printer/1/resume`: resumes a queue Bambuddy paused after a failure. Not needed for a user stop.

## Scheduler rules that matter

- Dispatches only when the printer is idle (`FINISH`, `FAILED`, `IDLE`), not held for a plate clear, and no item is `printing` on it.
- Skips `manual_start: true` items.
- `queue_shortest_first: true` orders by estimated time, not by position: a 3-tag plate goes before a 9-tag plate queued earlier.
- `require_plate_clear: true` holds after every finish; confirm with `POST /printers/1/clear-plate` or the UI.
- Log: `kubectl -n bambuddy logs deploy/bambuddy | grep print_scheduler` shows "unavailable, not idle", "reserved, selected for dispatch" and `PRINT COMMAND: printer=1, file=...`.

## Other endpoints used

- `POST /printers/1/print/stop`: stop the running job.
- `POST /printers/1/hms/clear`: sends `clean_print_error`; does not lift `FAILED`.
- `POST /printers/1/clear-plate`: releases the hold.
- `GET /printers/1`: `ip_address`, `serial_number`, `access_code`, `model`, `nozzle_count`.
- `GET /inventory/spools`: Bambuddy spool records (positive ids; the LAN path does not take them).
