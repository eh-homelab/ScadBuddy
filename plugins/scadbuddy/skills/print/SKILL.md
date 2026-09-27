---
name: print
description: Print a ScadBuddy output through Bambuddy - choose a pipeline, check eligibility, plan filaments per slot, pick the plate, options and project, run it with the user's approval, and follow its progress. Use when asked to print, send, queue or slice a generated model, or to explain why a print was refused or failed.
---

# Printing a ScadBuddy output through Bambuddy

ScadBuddy renders, and Bambuddy prints. Bambuddy owns printers, spools, presets,
the queue and projects. ScadBuddy "stores no second copy of printer, spool,
preset or project state" and "reimplements no decision Bambuddy already makes"
(`docs/superpowers/specs/2026-09-24-print-flow-design.md`, introduction). Follow
the same rule: report Bambuddy's answers, and don't second-guess them.

Sources, relative to the root of
[eh-homelab/ScadBuddy](https://github.com/eh-homelab/ScadBuddy):

- `docs/superpowers/specs/2026-09-24-print-flow-design.md` ("print-flow spec"):
  §1 for the request, §2 for the route, §3 to §5 for filaments, §6 for progress,
  §7 for projects.
- `docs/superpowers/specs/2026-09-27-ai-integration-design.md` ("AI spec"): §8 for
  tiers and approvals, §11 for analyzers.
- `docs/superpowers/specs/2026-09-22-scadbuddy-design.md` ("main spec"): §7 for
  Bambuddy integration and extruder order.
- `backend/openapi.json` for every route named below (all under `/api/v1`).

The ScadBuddy MCP tools arrive with issue #251. A print tool wraps
"eligibility → send → run behind a single approval" (AI spec §5.1). The routes
below are what it calls. Use the tool when you have it.

## Approvals come first

Sending, printing and deleting are `outward`. **Outward actions always need a
human approval in the ScadBuddy UI, in every auth mode**, including `disabled`
(AI spec §8.1 and §8.2).

- In the in-app harness, the session waits in `waiting_approval` while the UI
  shows a confirmation card (AI spec §8.2).
- Over `/mcp` (external clients such as Claude Code), an outward tool is two
  steps. `prepare` returns a pending action id and a summary. `confirm` completes
  only after the user approves in the UI (AI spec §8.2).

So before you prepare a print, tell the user what will happen: which pipeline or
printer, how many copies, the filament plan, and which route (§3 below). Never
say a print started until progress (§6 below) says so. Don't retry a denied
action with different wording.

## 1. Start from an output

You print an **output**, which is a render kept with Generate
(`POST /api/v1/models/{slug}/outputs`, main spec §8). If there isn't one yet, use
the `customize` skill first. `GET /api/v1/outputs/{output_id}/plates` lists its
plates, and ScadBuddy's own renders always have one plate (`backend/openapi.json`,
that route's description).

## 2. Assemble the request

The print dialog builds one `PrintRequest` from independent steps
(print-flow spec §1). The request body is `PrintRunRequest` in
`backend/openapi.json`: `pipeline_id`, `printer_id`, `filament_plan`,
`bed_type`, `plate_id` / `all_plates`, `options`, `project_id`, `copies` and
`force`.

| Step | Read with | Notes |
|---|---|---|
| Pipeline | `GET /api/v1/print/models/{slug}/pipelines` | Includes this model's default. Without `pipeline_id` a run uses the model's default, then the global one (`backend/openapi.json`, `POST /print/outputs/{output_id}/run`). |
| Eligibility | `POST /api/v1/print/outputs/{output_id}/eligibility` | Bambuddy judges a *library file*, so this **uploads the 3MF to Bambuddy** if it isn't there yet (`backend/openapi.json`). Whether a printer can run the job is Bambuddy's answer, not ours (print-flow spec §4). |
| Filaments | `GET /api/v1/print/outputs/{output_id}/filaments?printer_id=&plate_id=` | The plate's slots and the whole spool inventory, already joined (print-flow spec §3). |
| Plate | `bed_type`, `plate_id` / `all_plates` | The bed type last used on a printer is remembered with `PUT /api/v1/print/printers/{printer_id}/bed-type` (`backend/openapi.json`). |
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

## 3. Which route runs: pipeline, or slice then queue

There is one submit, `POST /api/v1/print/outputs/{output_id}/run`, and the
backend picks the route from what the request asks for (print-flow spec §2):

- **`route="pipeline"`**: `POST /slicer-pipelines/{id}/run` on Bambuddy. A
  pipeline run carries only `source_library_file_id` / `source_archive_id` /
  `copies` / `force`: no printer, no mapping, no options.
- **`route="slice_queue"`**: slice with the pipeline's own presets and bed type,
  with filament presets swapped per slot, wait for the slice job, then
  `POST /queue/` with `printer_id`, `filament_overrides`,
  `required_filament_types`, `plate_id`, `options` and `project_id`.

**What escalates to slice-then-queue:** only a filament plan or queue-level print
options. A `printer_id` alone doesn't, and neither does a `project_id` alone
(print-flow spec §2, "What actually escalates, as shipped").

Tell the user before they approve, because it changes where the copies land. A
class-targeted pipeline fans out across printers, but a queued item goes to one
(print-flow spec §2): "Bambuddy will slice this for *printer*, then queue it."

**`force`.** A blocking eligibility issue comes back as Bambuddy's 409, whose
body is passed through as `bambuddy_body`. `force: true` runs anyway, and
Bambuddy records `eligibility_overridden` (`backend/openapi.json`, the run
route's description). Only set `force` when the user has read that refusal and
asked to override it.

The simpler **send** path, `POST /api/v1/outputs/{output_id}/send` with
`{mode: "library" | "queue", copies, options}`, uploads to the library folder
and, in `queue` mode, slices and queues it (`backend/openapi.json`; main spec
§7). It is outward too.

## 4. After the run: the result

`PrintRunResult` reports `route`, `run`, `slice_job_id`, `queue_item_ids`,
`library_file_id`, `bambuddy_url` and `warnings` (`backend/openapi.json`;
print-flow spec §2). Give the user the `bambuddy_url`.

## 5. Projects

A project is Bambuddy's (print-flow spec §7). `POST /api/v1/print/projects`
creates one, or links an existing one, together with its library folder. Attaching
queue entries and archives is a separate call,
`POST /api/v1/print/outputs/{output_id}/project`. Call it once the ids exist:
queue entries appear after a pipeline run's background task, and archives only
after the print finishes. Calling it again later is how archives land on the
project (print-flow spec §7). The project routes need Bambuddy's **Manage
Projects** scope, which the key may not have (print-flow spec §7).

## 6. Follow progress

`GET /api/v1/print/outputs/{output_id}/progress` follows whichever route ran
(print-flow spec §6; `backend/openapi.json`, `PrintProgress`):

- `null` means the output was never printed. That is an answer, not an error.
- `settled` says when to stop. On a failed pipeline run, **`status` and the copy
  counters don't move**, so don't read them as "still printing". The run's
  `completed_at` is what settles it (print-flow spec §6).
- Show Bambuddy's `error_message` **word for word**, next to the `fix` the route
  suggests (re-check eligibility, change the mapping, retry). Don't paraphrase it
  (print-flow spec §6).

## 7. Errors that name a scope

Every Bambuddy call declares the API-key scope it needs, so a 401 or 403 names
the missing scope (`CLAUDE.md`, section "Bambuddy iframe facts";
`backend/scadbuddy/bambuddy/errors.py`). Sending needs **Manage Library** and
**Manage Queue**, and listing printers needs **Read Status** (main spec §7).
Tell the user which scope to add to the key in Bambuddy. The key itself never
leaves the server (`CLAUDE.md`, section "Bambuddy iframe facts").

## 8. Print analyzers (coming with #284)

Analyzers inspect a print before it runs and propose **diffs against the base
request** that this flow already builds. They never propose a profile of their
own (AI spec §11; issue #284). Accepting a diff forces the slice-then-queue
route:

1. Filament-level settings go in `filament_overrides` on the queue item.
2. Process-level settings become a derived local preset.
3. The 3MF's `project_settings.config` is used only if it is shown to beat the
   pipeline's preset. Main spec §3 measured the preset winning.

Each of these is on AI spec §3.2's "to verify" list, so don't rely on any of them
until a PR verifies it. Analyzer sessions can read and propose, but they can't
send or print (issue #284, "Agent-mode analyzers"). Every proposed value needs a
source. A value without one is the analyzer's judgement, and is never applied
automatically (AI spec §2, D10).
