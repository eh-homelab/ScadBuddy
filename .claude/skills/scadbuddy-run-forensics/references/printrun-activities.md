# PrintRun activities, in order

| Activity | Queue | Input of note | Result of note | Seen failures |
|---|---|---|---|---|
| `print_check` | bambuddy | subject, printer, plan | | |
| `print_insert` | bambuddy | | run row created (`created_at`) | |
| `print_plan` | bambuddy | choices, spools | `library_file_id` of the run's copy; `prepared.printer_status` | `Slot N has no spool chosen` |
| (update outcome) | | | `run.id`, `status: running` answered to the client | |
| `slice_run` | library | the copy, presets | `slice_id`, `preset_key` | slicer refusals (422 text, e.g. the prime-tower rule) |
| `print_sliced_upload` | library | sliced blob key | the sliced native library file id (an int) | heartbeat timeout |
| `print_start_enqueue` | bambuddy | | | |
| `lan_upload` | bambuddy | `run_id`, `sliced.blob_key`, `remote_name`, `plate_id` | `remote_name`, `md5`, `bytes`, `filament_count`, `rack` | FTPS errors |
| `lan_print` | bambuddy | `uploaded`, `plan`, `spools` (tray, brand, colour, `records.bambuddy.record_id`, `physical_spool_id`, `label`), `bed_type`, `timelapse`, `layer_inspect`, `rack_algorithm` | `subtask_name`, `started_at`, `warnings`, `plate_id` | `refused the print: <reason>` (non-retryable `PrintFailed`) |
| `print_record` | bambuddy | | | |
| `print_finish` | bambuddy | | | |
| `print_succeed` / `print_fail` | bambuddy | | the final run object | |
| child `FollowPrint` | bambuddy-follow | subject | `"settled"` | settles early on the previous job's state |

The Bambuddy-queue route (a printer that is not LAN-linked) replaces `lan_upload` and
`lan_print` with a library upload and a queue insert; this estate's H2C never takes it.

## Decoding

Every payload is `{"metadata": {"encoding": "json/plain" (base64)}, "data": <base64 JSON>}`.
`scripts/print_runs.py` decodes them. By hand:

```python
import base64, json
json.loads(base64.b64decode(event["activityTaskCompletedEventAttributes"]["result"]["payloads"][0]["data"]))
```

Event types to read: `WORKFLOW_EXECUTION_STARTED` (input), `WORKFLOW_EXECUTION_UPDATE_COMPLETED`
(the early answer), `ACTIVITY_TASK_SCHEDULED` (type and input), `ACTIVITY_TASK_COMPLETED`
(result), `ACTIVITY_TASK_FAILED` (`failure.message`, `failure.applicationFailureInfo.details`),
`ACTIVITY_TASK_TIMED_OUT` (`failure.cause.message`), `WORKFLOW_TASK_TIMED_OUT`,
`START_CHILD_WORKFLOW_EXECUTION_INITIATED`, `WORKFLOW_EXECUTION_COMPLETED` (result).
