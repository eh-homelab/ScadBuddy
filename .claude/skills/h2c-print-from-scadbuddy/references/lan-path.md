# What ScadBuddy's LAN send actually does

From `backend/scadbuddy/printers/print_activities.py` and `printers/lan.py`, and the
Temporal histories of the November 2026 keychain runs.

## The PrintRun workflow

`print_check` → `print_insert` → `print_plan` (copies the library file; result holds
the copy's `library_file_id`) → update answered to the client (`status: running`) →
`slice_run` (task queue `library`; result `slice_id`, `preset_key`) →
`print_sliced_upload` (result: the sliced native library file id) →
`print_start_enqueue` → `lan_upload` (FTPS to the printer; result `remote_name`,
`md5`, `bytes`, `filament_count`, `rack`) → `lan_print` (MQTT `project_file`; result
`subtask_name`, `started_at`) → `print_record` → `print_finish` → `print_succeed` /
`print_fail` → child `FollowPrint` (`follow-print-<subject>`).

The slice is cached by `preset_key` (printer preset, process, filament presets, plate,
bed type, prime tower settings, spool ids), so a rerun with the same choices slices in
seconds.

## The project_file command it sends

```
url: ftp://<remote_name>, param: Metadata/plate_<n>.gcode, md5,
subtask_name: scadbuddy-<run8>-p<n>, use_ams, ams_mapping: [flat],
bed_type: "auto", flow_cali: false, timelapse: <input>, layer_inspect: true,
project_id/profile_id/task_id/subtask_id: "0", nozzle_mapping: [rack picks] (optional)
```

So: no `bed_leveling`, `vibration_cali` or `nozzle_offset_cali` keys (the printer
applies its defaults), `flow_cali` always false, and the `options` block of
`print_output` is not consulted on this path. `ProjectFile` in `printers/lan_models.py`
is where a fix would go.

## Things the run record gets wrong

- `get_print_progress` for the file read `null` for a run that was printing, after the API pods rolled during the call.
- `FollowPrint` for that run settled in five seconds: it read the previous job's `FINISH` under the new name before the printer switched. The native follow keys on `subtask_name` and `gcode_state`, and the printer reports the old state under the new name for a moment (#4935 is the same window on the error side).
- Four workflow tasks timed out at 10 s each while the run was healthy; that is Temporal persistence slowness and the reason a client sees HTTP 500 "Workflow Task in failed state" for a run that completes.

## Refusals from the printer

`_started` raises `<printer> refused the print: <reason>` from the `project_file` ack.
`ERROR STATE` was the reason both when a job was running and when the printer sat in
`FAILED` after a stop.
