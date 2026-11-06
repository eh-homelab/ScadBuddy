---
name: scadbuddy-run-forensics
description: Find out what a ScadBuddy print, slice or order operation actually did by reading its Temporal workflow history, instead of trusting the HTTP reply or the run record. Answers whether a run whose reply was lost reached the printer, which sliced library file it produced, why an activity failed (ERROR STATE refusal, heartbeat timeout on the sliced upload, workflow task timeouts, "no spool chosen"), and which spools and trays a LAN send used. Use it whenever a ScadBuddy MCP call times out or its transport drops, the API answers HTTP 500 "Workflow Task in failed state" or "did not answer", get_print_progress reads null while the printer is clearly busy, or before resending any print, and for any "did it actually go", "what happened to my run", "which file did it make" question.
---

# ScadBuddy run forensics

ScadBuddy runs every command as a Temporal workflow in namespace `scadbuddy`
(cluster namespace `bambuddy`). The workflow is durable; the HTTP reply is not. An
API pod rollout, an MCP transport drop or a slow Temporal database all lose the reply
while the run carries on, and ScadBuddy's own run records (`get_print_progress`,
`get_print_run`) have been wrong in exactly those moments. The history is the one
record that cannot be stale, so read it before deciding a run failed, and always before
resending a print.

## Reach the history

`scripts/temporal_pf.sh` port-forwards the Temporal UI service to `127.0.0.1:18234`
and waits until it answers. Then:

- `scripts/print_runs.py list [--since 2026-11-04T05:00:00Z] [--prefix print-]`: recent workflows with status, subject, the sliced file id and the last activity.
- `scripts/print_runs.py show <workflow id or prefix>`: the activity timeline with inputs and results decoded, failures with their messages.
- `scripts/print_runs.py show --file history.json`: the same from a saved history (the UI's `/history` JSON).

The raw API, when the script does not fit:
`GET /api/v1/namespaces/scadbuddy/workflows?query=<visibility query>` (for example
`StartTime > "2026-11-04T05:00:00Z" AND WorkflowId STARTS_WITH "print-"`) and
`GET /api/v1/namespaces/scadbuddy/workflows/<id>/history`. Payloads are base64 JSON
under `input.payloads[].data`, `result.payloads[].data` and `outcome.success`.

## Workflow families

| Id | Type | What it is |
|---|---|---|
| `print-<sha>` | `PrintRun` | one print_output / library run; task queue `bambuddy` |
| `follow-print-<subject>` | `FollowPrint` | child that watches the printer for a LAN start |
| `slice-<file id>-<hash>` | slice | the slicer activity's own workflow, task queue `library` |
| `op-<kind>-<sha>` | operation | renders, output saves, quotes (`op-output_create-…`, `op.order_item_quote.*`) |
| `scadbuddy-prune-library-<ts>` | schedule | housekeeping; ignore |

A run id (`3291f613…`) is not the workflow id; the subject (`library:1000000050`,
`output:<id>`) is in the workflow input and in the update outcome, and the run id is in
the outcome's `run.id`. Search by time and prefix, then match the subject.

## What a PrintRun's activities tell you

`references/printrun-activities.md` has the full table. The ones you will look for:

- `print_plan` result: the library copy made for the run (`library_file_id`).
- `slice_run` result: `slice_id`, `preset_key` (what was sliced with what; a rerun with the same key is cached).
- `print_sliced_upload` result: **the sliced native library file id**. This is the number `h2c-print-from-scadbuddy` needs for the Bambuddy queue route.
- `lan_upload` input/result: `remote_name` on the printer, `md5`, `bytes`, `rack` picks.
- `lan_print` input: the whole LAN plan, including each slot's tray, Bambuddy spool record id and physical spool id, `bed_type`, `timelapse`, `layer_inspect`. Its result: `subtask_name` and `started_at`, which is the moment the printer took the job. Its failure: the printer's refusal reason.
- `WORKFLOW_EXECUTION_COMPLETED` result: the run as the client would have received it, `status` and `error.detail` included.

## Reading failures

| In the history | Meaning | Next |
|---|---|---|
| `WORKFLOW_TASK_TIMED_OUT`, several in a row | Temporal persistence slow; the client got HTTP 500 but the run continues | wait; read the printer |
| `lan_print` failed `refused the print: ERROR STATE` | printer busy or `FAILED` after a stop; nothing printed; the sliced file still exists | use it with the queue route |
| `print_sliced_upload` `ACTIVITY_TASK_TIMED_OUT` "activity Heartbeat timeout" | the blob store stalled; the slice itself completed | rerun; it slices from cache |
| `print_plan` failed `Slot N has no spool chosen` | positive spool ids on a LAN printer | negative tray ids |
| `FollowPrint` completed `"settled"` seconds after the start | it read the previous job's FINISH under the new name | the printer is the truth; progress records for this run are void |
| No `print-` workflow for your subject in the window | the request never reached a workflow | safe to send again |

## Logs, when the history is not enough

`kubectl -n bambuddy logs deploy/scadbuddy -c scadbuddy --since=15m` for the API;
pods `-l app.kubernetes.io/name=scadbuddy`; the print worker's identity in a failure
is `scadbuddy-print-<hash>`. A rollout shows as fresh pod start times and
`could not make this build current yet` warnings; every HTTP reply open across it is
lost.
