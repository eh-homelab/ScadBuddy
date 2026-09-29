---
name: print
description: Print a ScadBuddy output through Bambuddy - read the dialog's choices, pick a spool per slot, the nozzle size, quality and plate, options and project, run it with the user's approval, and follow its progress. Use when asked to print, send, queue or slice a generated model, or to explain why a print was refused or failed.
---

# Printing a ScadBuddy output through Bambuddy

ScadBuddy renders, and Bambuddy prints. Bambuddy owns printers, spools, presets,
the queue and projects. ScadBuddy "stores no second copy of printer, spool,
preset or project state" and "reimplements no decision Bambuddy already makes"
(`docs/superpowers/specs/2026-09-24-print-flow-design.md`, introduction). Follow
the same rule: report Bambuddy's answers, and don't second-guess them.

Sources, relative to the root of
[eh-homelab/ScadBuddy](https://github.com/eh-homelab/ScadBuddy):

- `docs/superpowers/specs/2026-09-27-spool-first-print-design.md` ("spool-first
  spec"): §2 for the flow, §3 for what each step reads, §4 for how presets are
  derived, §4.5 for errors versus warnings, §6 for what Bambuddy decides. It
  supersedes the print-flow spec's §2 rule that a pipeline is never bypassed.
- `docs/superpowers/specs/2026-09-24-print-flow-design.md` ("print-flow spec"):
  §3 to §5 for filaments, §6 for progress, §7 for projects.
- `docs/superpowers/specs/2026-09-27-ai-integration-design.md` ("AI spec"): §8 for
  tiers and approvals, §11 for analyzers.
- `docs/superpowers/specs/2026-09-22-scadbuddy-design.md` ("main spec"): §7 for
  Bambuddy integration and extruder order.
- `backend/openapi.json` for every route named below (all under `/api/v1`).

The ScadBuddy MCP tools arrive with issue #251. A print tool wraps the run
behind a single approval (AI spec §5.1): read the choices, then run with them.
The routes below are what it calls. Use the tool when you have it. There is no
pipeline or eligibility step in the print dialog any more (spool-first spec §0).
The `/api/v1/print/library` routes (#313, printing a file already in Bambuddy's
library) have no agent tool yet — that's a #313 follow-up.

## Approvals come first

Sending, printing and deleting are `outward`. **Outward actions always need a
human approval in the ScadBuddy UI, in every auth mode**, including `disabled`
(AI spec §8.1 and §8.2).

- In the in-app harness, the session waits in `waiting_approval` while the UI
  shows a confirmation card (AI spec §8.2).
- Over `/mcp` (external clients such as Claude Code), an outward tool is two
  steps. `prepare` returns a pending action id and a summary. `confirm` completes
  only after the user approves in the UI (AI spec §8.2).

So before you prepare a print, tell the user what will happen: which printer, how
many copies, the spool for each slot, the nozzle size, quality and plate, and any
warnings (§3 below). Never
say a print started until progress (§6 below) says so. Don't retry a denied
action with different wording.

## 1. Start from an output

You print an **output**, which is a render kept with Generate
(`POST /api/v1/models/{slug}/outputs`, main spec §8). If there isn't one yet, use
the `customize` skill first. `GET /api/v1/outputs/{output_id}/plates` lists its
plates, and ScadBuddy's own renders always have one plate (`backend/openapi.json`,
that route's description).

## 2. Assemble the request

The print dialog reads everything it offers in one call,
`GET /api/v1/print/outputs/{output_id}/choices?printer_id=` (`ChoicesView` in
`backend/openapi.json`; spool-first spec §3): the printers, the installed nozzles
and the four nozzle sizes, the quality tiers and processes for the size, the
plates with the one the printer's last print used, the model's remembered
choices, and the filament step (slots, spools and a suggestion).

The run body is `PrintRunRequest` in `backend/openapi.json`: `printer_id`,
`filament_plan`, `choices`, `plate_id` / `all_plates`, `options`, `project_id`
and `copies`. `choices` is `PrintChoices`: `nozzles` (one size for the job),
`tier` (`fine` / `standard` / `draft`) or a `process_name`, `bed_type`, and any
per-slot `filament_overrides` (spool-first spec §2).

| Step | Read with | Notes |
|---|---|---|
| Everything above | `GET /api/v1/print/outputs/{output_id}/choices?printer_id=` | One read for the whole dialog (spool-first spec §3). |
| Filaments | `GET /api/v1/print/outputs/{output_id}/filaments?printer_id=&plate_id=` | The plate's slots and the whole spool inventory, already joined (print-flow spec §3). `all_plates=true` answers for every plate at once. |
| Plate | `choices.bed_type`, `plate_id` / `all_plates` | Preselected from the printer's last print, then the plate remembered with `PUT /api/v1/print/printers/{printer_id}/bed-type` (spool-first spec §4.4). |
| Remembered choices | `PUT /api/v1/print/models/{slug}/choices` | The printer and spools last chosen for this model (`backend/openapi.json`). |
| Options | `GET /api/v1/settings/print-options` | The sparse `PrintOptions` overlay (#88; `backend/openapi.json`, `PrintOptions`). |
| Project | `GET /api/v1/print/projects` | Bambuddy's projects, with their library folders (print-flow spec §7). |

### The filament plan

A `FilamentPlan` is one chosen spool per plate slot (print-flow spec §1). Slot
order is extruder order, which is the order of the model's colour parameters
(main spec §7).

- The route's opening selection matches the same material first, then the
  nearest colour, preferring loaded spools and never offering one spool twice
  (print-flow spec §5). Present it as a suggestion. Every slot stays the user's
  choice.
- **Traps in the data** (print-flow spec §3):
  - `used_grams: 0` means *unknown*, not zero.
  - `remain: -1` means *unknown*, not empty.
  - `/inventory/assignments` covers every printer, but `inventory-remain` covers
    one. The route does this join for you, so don't redo it.
- **The only warnings** (print-flow spec §4). None of them blocks a run:
  1. The spool isn't loaded: name where it is (`storage_location`, or the other
     printer).
  2. Not enough filament: `remaining_g < used_grams × copies`, and only when the
     grams are known.
  3. A slot has nothing chosen.
- ScadBuddy sends **no `ams_mapping`**. Bambuddy's scheduler computes it against
  the printer it actually dispatches to (print-flow spec §4). Don't invent one.

## 3. The run: slice, then queue

There is one submit, `POST /api/v1/print/outputs/{output_id}/run`. ScadBuddy
derives the printer, process and per-slot filament presets from `choices` and the
chosen spools (spool-first spec §4), slices through Bambuddy, waits for the slice
job, then `POST /queue/` on the one printer the dialog is scoped to. No pipeline
runs on this path.

The route answers **202** with a `PrintRun` (`status: "running"`) and slices and
queues in the background, because the slices outlast the proxies in front.
Follow `GET /api/v1/print/runs/{run_id}` until `status` is `succeeded` (its
`result` is the `PrintRunResult` below) or `failed` (its `error` carries the
`type`, `status` and `detail` of the refusal). The body's optional `request_id`
names one deliberate print: send a new one per print and the same one on a
retry of it. The same request (same `request_id`) for the same output again
answers **200** with that run (`repeated: true`) while it is in flight, or for
ten minutes after it succeeded or failed with `may_have_queued: true`, and queues
nothing more; a failed run that never tried to queue holds nothing, so repeating
it tries again. A new `request_id` with
the same choices is a new print (`backend/openapi.json`; #470). `print_output`
makes a new one per call, and re-sends that call's POST with the same id when no
answer from ScadBuddy arrived. If it still reports no answer, or names a run it
started but could not read, follow that run or check Bambuddy's queue rather
than calling `print_output` again. A `failed` run with `may_have_queued: true` had already
tried to queue (a queue call that timed out, or a later plate failing after an
earlier one was queued): tell the user to check Bambuddy's queue, and do not
print again until they have, since another print would be a second one.

- **Errors** are a 422 before anything is sliced, and name the slot or setting:
  mixed nozzle sizes, or a slot with no filament preset for the nozzle
  (spool-first spec §4.5). A slot error needs the uploaded file, so it arrives
  as the run's `failed` `error` with that 422, not as the POST's answer.
- **Warnings** come back in the result and never block: spool not loaded, nozzle
  not installed, High Flow slicing as Standard, a Generic filament preset
  fallback, or a plate that differs from the last print (spool-first spec §4.5).
  Tell the user about the ones you can predict before they approve.

Which AMS tray and extruder each spool feeds, and which rack nozzle is used,
stay Bambuddy's and the printer's decisions (spool-first spec §6).

The simpler **send** path, `POST /api/v1/outputs/{output_id}/send` with
`{mode: "library"}`, only uploads the 3MF to the library folder and attaches the
edit link. It never slices or queues; `mode: "queue"` is refused with a 422
(`backend/openapi.json`, `SendRequest`; spool-first spec §0, the #312 note). To
print, use the run above. Sending is outward too.

## 4. After the run: the result

A `succeeded` run's `result`, a `PrintRunResult`, reports `route` (always `slice_queue`), `slice_job_id`,
`queue_item_ids`, `library_file_id`, `copies`, `bambuddy_url` and `warnings`
(`backend/openapi.json`). Give the user the `bambuddy_url` and every warning.

## 5. Projects

A project is Bambuddy's (print-flow spec §7). `POST /api/v1/print/projects`
creates one, or links an existing one, together with its library folder. Attaching
queue entries and archives is a separate call,
`POST /api/v1/print/outputs/{output_id}/project`. Call it once the ids exist:
queue entries appear once a plate is queued, and archives only
after the print finishes. Calling it again later is how archives land on the
project (print-flow spec §7). The project routes need Bambuddy's **Manage
Projects** scope, which the key may not have (print-flow spec §7).

## 6. Follow progress

`GET /api/v1/print/outputs/{output_id}/progress` follows whichever route ran
(print-flow spec §6; `backend/openapi.json`, `PrintProgress`):

- `null` means the output was never printed. That is an answer, not an error.
- `settled` says when to stop. An all-plates print reads "Slicing…" until one of
  its plates has a queue entry, and a plate can fail before it is ever queued.
- Show Bambuddy's `error_message` **word for word**, next to the `fix` the route
  suggests (change the mapping, retry). Don't paraphrase it
  (print-flow spec §6).

## 7. Errors that name a scope

Every Bambuddy call declares the API-key scope it needs, so a 401 or 403 names
the missing scope (`CLAUDE.md`, section "Bambuddy iframe facts";
`backend/scadbuddy/bambuddy/errors.py`). Sending needs **Manage Library**,
printing also needs **Manage Queue**, and listing printers needs **Read Status**
(main spec §7).
Tell the user which scope to add to the key in Bambuddy. The key itself never
leaves the server (`CLAUDE.md`, section "Bambuddy iframe facts").

## 8. Print analyzers (coming with #284)

Analyzers inspect a print before it runs and propose **diffs against the base
request** that this flow already builds. They never propose a profile of their
own (AI spec §11; issue #284). The run already slices then queues, so an
accepted diff lands on that same request:

1. Filament-level settings go in `filament_overrides` on the queue item.
2. Process-level settings become a derived local preset.
3. The 3MF's `project_settings.config` is used only if it is shown to beat the
   resolved process preset. Main spec §3 measured the preset winning.

Each of these is on AI spec §3.2's "to verify" list, so don't rely on any of them
until a PR verifies it. Analyzer sessions can read and propose, but they can't
send or print (issue #284, "Agent-mode analyzers"). Every proposed value needs a
source. A value without one is the analyzer's judgement, and is never applied
automatically (AI spec §2, D10).
