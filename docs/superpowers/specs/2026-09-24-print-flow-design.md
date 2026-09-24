# One print flow: filaments, options, project, run tracking

Design for #87 (filament and nozzle mapping, as an inventory picker), #89 (run
tracking) and #79 (projects), built as one dialog rather than three. Written
2026-09-24 against the live Bambuddy **1.2.5.5**; every shape below was read off
that instance with a `GET`, or off its `openapi.json`.

ScadBuddy renders OpenSCAD and sends the result to Bambuddy. Bambuddy owns printing,
inventory, projects and tracking. So ScadBuddy stores no second copy of printer,
spool, preset or project state, and — the harder half of the same rule — it
**reimplements no decision Bambuddy already makes**. Every list in the dialog is a
read of Bambuddy's own objects, every judgement is Bambuddy's own, and the one thing
ScadBuddy persists is provenance: which Bambuddy ids a send produced.

## 1. The state model

One `PrintRequest`, assembled by the print dialog and submitted once:

| Step | Owns | Issue |
|---|---|---|
| Pipeline | `pipeline_id`, the eligibility report | #86 (merged) |
| Printer | `printer_id` — scoping, and binding on the queue route | #86 / #87 |
| Filaments | a `FilamentPlan`: one chosen spool per plate slot | **#87** |
| Plate | `plate_id` | #83 (reserved, not built here) |
| Options | the sparse `PrintOptions` overlay | #88 |
| Project | `project_id` + the folder sends land in | **#79** |
| Copies | `copies` / `quantity` | #86 |
| *after submit* | run / slice job / queue entries | **#89** |

The steps are independent reads and one dependent write. Nothing but the filament
step needs the printer, and nothing but the submit needs all of them.

## 2. The single request, and how the route is chosen

**`filament_overrides` and `required_filament_types` exist only on
`PrintQueueItemCreate`.** `PipelineRunCreateRequest` carries exactly
`source_library_file_id` / `source_archive_id` / `copies` / `force` — no printer, no
mapping, no options. A pipeline run's background task then creates each copy's queue
entry from Bambuddy's own defaults (verified against the pod's
`app/api/routes/pipeline_runs.py` for #88), so there is nothing to plumb through and
nothing to `PATCH` afterwards — `jobs[].queue_entry_id` is still null when the 202
returns.

So the dialog submits one request, `POST /api/v1/print/outputs/{id}/run`, and the
**backend** picks the route from what the request actually asks for:

```
run(pipeline_id, copies, force, printer_id?, filament_plan?, options?, project_id?)
  │
  ├── nothing set that a run cannot express   → POST /slicer-pipelines/{id}/run
  │                                             route="pipeline"
  └── a filament plan, a pinned printer,      → POST /library/files/{id}/slice   (the
      queue-level options or a project           pipeline's OWN presets + bed type,
                                                 filament presets swapped per slot)
                                                GET  /slice-jobs/{id} until finished
                                                POST /queue/  with printer_id,
                                                 filament_overrides,
                                                 required_filament_types, plate_id,
                                                 options, project_id
                                                 (NO ams_mapping — see §4)
                                                route="slice_queue"
```

This is the same escalation #88 introduced for the send bar, extended to the picker
path and given a name. The pipeline is never bypassed — the slice borrows *its*
`printer_preset`, `process_preset`, `bed_type` and target, so "which pipeline" still
means what it meant. `PrintRunResult` gains `route`, `slice_job_id` and
`queue_item_ids` so both routes report through one shape.

Escalating is visible in the dialog before the click ("Bambuddy will slice this for
*printer*, then queue it"), because it changes which printer the copies land on: a
class-targeted pipeline fans out, a queued item does not.

## 3. What each step reads

One aggregation route, `GET /api/v1/print/outputs/{id}/filaments?printer_id&plate_id`,
so the browser makes one call and no join happens client-side:

| Datum | Source |
|---|---|
| Plate slots: index, colour, type, **grams for this print** | `GET /library/files/{file_id}/filament-requirements` (falls back to the output's own `colors` when the 3MF has not been sliced — an unsliced upload answers `used_grams: 0`, which is *unknown*, not zero) |
| Spool inventory: material, subtype, colour name + `rgba`, brand, `label_weight`, `weight_used`, `slicer_filament`, `storage_location` | `GET /api/v1/inventory/spools` |
| Which spool is loaded where | `GET /api/v1/inventory/assignments` → `printer_id`, `ams_id`, `tray_id` |
| Remaining grams per loaded slot | `GET /api/v1/printers/{id}/inventory-remain` → `slot_materials[]` |
| The printer's name, for the "loaded elsewhere" label | `GET /api/v1/printers/{id}` |

That is the whole list, and it is short on purpose. The picker does **not** read
`/printers/{id}/status` (tray temperatures, nozzle diameters, `ams_switch_inlet`) or
`/inventory/spools/{id}/filament-presets`: nothing here decodes any of them, because
nothing here decides anything they would inform. A test asserts those two are never
called, so the claim cannot rot.

Remaining is `label_weight - weight_used` for a spool on the shelf, and
`inventory-remain.slot_materials[].remaining_g` for a loaded one — that is Bambuddy's
own reconciliation of the AMS against the inventory, and it wins where both exist.

Three traps worth stating because they are easy to get backwards:

- **`used_grams: 0` means unknown.** Read as a real weight, the "not enough filament"
  warning never fires; read as "needs nothing", every spool looks sufficient.
- **`remain: -1` means unknown, not empty.** An untagged spool reports it, and a
  "0 g left" warning built on it would fire on every third-party spool.
- **`/inventory/assignments` covers every printer; `inventory-remain` covers one.**
  Joining them on `(ams_id, tray_id)` alone hands a spool sitting in printer B's
  AMS 0 slot 1 printer A's remaining weight — a different filament's.

## 4. What ScadBuddy sends, and what Bambuddy decides

**ScadBuddy sends no `ams_mapping`.** Bambuddy's scheduler computes one itself
whenever a queue item carries none — `_compute_ams_mapping_for_printer`, read off the
running pod — against the printer it is *actually* dispatching to. That step already
handles the Filament Track Switch (a switcher routes any AMS slot to either extruder,
so the per-nozzle filter must not apply; its issue #2186), the AMS-HT's own id space
and the external `vt_tray` feeds. A second copy here would be a worse one, and it
would drift the first time Bambuddy learns a new machine.

What is sent instead is the pair its scheduler actually matches on, in the shape its
own 3MF parser produces:

- `filament_overrides`: `{slot_id, type, color, used_grams, force_color_match?}` per
  slot. `force_color_match` is **opt-in** — on a class-targeted item, insisting can
  leave the job unschedulable.
- `required_filament_types`: the materials of the chosen spools.

For the slice itself, a spool names its own preset (`slicer_filament`, e.g. `GFG00`),
so that is the one used. Where the spool names none, or names one Bambuddy's preset
catalogue cannot look up, the **pipeline's own** filament preset stays — sending an id
Bambuddy cannot resolve fails the slice naming a preset nobody chose.

### The two warnings

There is no compatibility rules engine, and no material table. Whether two filaments
can share a plate, whether a printer can run the job, whether a slot can reach the
extruder it slices for — those are Bambuddy's questions, and its eligibility report
(#86) answers them in the same dialog. ScadBuddy adds only what falls straight out of
the data it has already fetched:

1. **The spool is not loaded** — allowed, not a refusal. "Load *Bambu PETG Basic
   Misty Blue* into the printer", naming its `storage_location` when it has one, or
   naming the other printer when it is loaded in one.
2. **Not enough filament**: `remaining_g < used_grams × copies`, only when the slice
   has reported grams. Unknown grams decline to judge rather than guessing.

Plus "this slot has nothing chosen", which is the one case that really is incomplete.
All three are advisory and none disables Run.

They are computed **in the browser** against the plan on screen, by the same two rules
(`checkPlan` in `lib/filaments.ts`). The server computes them too, for its own opening
selection — but that answer stops being true the moment a slot is changed, which is
the entire point of a picker.

## 5. The opening selection, so the common case needs no clicks

Per slot: same material (when the plate declares one), then nearest colour within a
small threshold. Loaded spools sort first and win ties, and a spool already taken by
an earlier slot is not offered again. A slot with no candidate opens empty and says
so.

This fills the picker in; it decides nothing. Every slot stays editable, and which
tray the print is finally drawn from is still Bambuddy's answer.

Display order: loaded in *this* printer, then loaded anywhere, then the shelf; most
remaining first within each band. Filters, all client-side over the one payload:
material, subtype, brand, colour search, loaded-only, and "enough for this print".

## 6. Run tracking (#89)

After submit, one ScadBuddy route polls whichever route ran and normalises both into
a `PrintProgress`:

- `route="pipeline"` → `GET /api/v1/pipeline-runs/{run_id}` (the single-run read;
  `/slicer-pipelines/{id}/runs` is a list and is not needed). Reports `status`,
  `copies_completed` / `_failed`, `error_message`, and `jobs[]` with
  `assigned_printer_name`, `queue_entry_id`, `status`, `error_message`.

  **`status` and the copy counters both lie on a failed run**, so `completed_at` is
  what settles the poll. A real run recorded on 2026-09-24
  (`recordings/pipeline-run.json`) reports `status: "in_progress"` and
  `copies_in_progress: 1` while also carrying `completed_at` and
  `error_message: "Slice failed: …"`; neither the status nor the counters ever move.
  The fix is likewise chosen by *where* it failed — `slice_job_id` set with
  `sliced_library_file_id` still null is the slicer's refusal, a failed copy with a
  `queue_entry_id` was refused at the queue, one without never matched a printer — so
  none of it depends on parsing Bambuddy's wording.
- `route="slice_queue"` → `GET /slice-jobs/{id}` until finished, then
  `GET /queue/{item_id}` for `status`, `waiting_reason`, `error_message`.

Polling stops when every copy is queued, failed or cancelled. Bambuddy's own text is
shown verbatim next to the fix that applies (re-check eligibility / change the
mapping / retry), never paraphrased. `OutputMeta` already records
`library_file_id`, `pipeline_run_id` and `queue_item_id`; it gains the job and queue
entry ids so History can deep-link each one.

## 7. Projects (#79)

A project is Bambuddy's, not a second one here. Create → `POST /api/v1/projects/`
then `POST /api/v1/library/folders/` with `project_id` set. Link → `GET /projects/`
plus `GET /library/folders/by-project/{id}`. The picker's project choice replaces
`settings.library_folder_id` for that send, so the 3MF uploads into the project's
folder; afterwards the produced archives go to `POST /projects/{id}/add-archives` and
the queue entries to `POST /projects/{id}/add-queue`. On the queue route `project_id`
also rides on the queue item itself, so there is no window in which the entry exists
unfiled.

Attaching is a **separate call**, not part of the run: a pipeline run's
`jobs[].queue_entry_id` is null when the 202 returns, and an archive only exists once a
print has finished. Attaching at run time would attach nothing on one route and half on
the other, so `POST /print/outputs/{id}/project` is called once the ids are known and
again when the archives appear.

Scope: the project routes map to `Manage Projects`, which the current key may not
carry — the settings page's connection test reports the missing scope by name rather
than the picker failing at submit.

## 8. Test strategy

Backend tests drive `respx` from recordings taken by `GET` only, per
`tests/bambuddy/recordings/README.md`; serials, IPs, access codes, RFID `tag_uid` and
`tray_uuid` are redacted. Shapes that need a `POST` to observe (a slice job result, a
queue-item create, a `PipelineRunResponse` with `jobs[]`) are built from Bambuddy's
`openapi.json` inline, as the existing tests already do. The frontend gets `vitest`
coverage of the matcher, the filters and every warning rule, and one Playwright spec
drives the dialog end to end against `msw`.
