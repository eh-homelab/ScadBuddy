# One print flow: filaments, options, project, run tracking

Design for #87 (filament and nozzle mapping, as an inventory picker), #89 (run
tracking) and #79 (projects), built as one dialog rather than three. Written
2026-09-24 against the live Bambuddy **1.2.5.5**; every shape below was read off
that instance with a `GET`, or off its `openapi.json`.

ScadBuddy stores no second copy of printer, spool, preset or project state. Every
list in the dialog is a read of Bambuddy's own objects, and the one thing ScadBuddy
persists is provenance — which Bambuddy ids a send produced.

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

**`ams_mapping`, `filament_overrides` and `required_filament_types` exist only on
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
                                                 ams_mapping, filament_overrides,
                                                 required_filament_types, plate_id,
                                                 options, project_id
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

One aggregation route, `GET /api/v1/print/outputs/{id}/filaments?printer_id&pipeline_id`,
so the browser makes one call and no join happens client-side:

| Datum | Source |
|---|---|
| Plate slots: index, colour, type, **grams for this print** | `GET /library/files/{file_id}/filament-requirements` (falls back to the output's own `colors` when the 3MF has not been sliced — an unsliced upload answers `used_grams: 0`, which is *unknown*, not zero) |
| Spool inventory: material, subtype, colour name + `rgba`, brand, `label_weight`, `weight_used`, `slicer_filament`, `storage_location` | `GET /api/v1/inventory/spools` |
| Which spool is loaded where | `GET /api/v1/inventory/assignments` → `printer_id`, `ams_id`, `tray_id` |
| Remaining grams per loaded slot, **and the extruder that slot feeds** | `GET /api/v1/printers/{id}/inventory-remain` → `slot_materials[]` |
| Live tray state and temperature window per loaded slot | `GET /api/v1/printers/{id}/status` → `ams[].tray[]`, `vt_tray[]` |
| Nozzles, nozzle rack, AMS→inlet switching | the same status: `nozzles[]`, `nozzle_rack[]`, `ams_switch_inlet` |
| The `slicer_filament` to slice a spool with, per printer model **and nozzle diameter** | `GET /api/v1/inventory/spools/{id}/filament-presets` |

Remaining is `label_weight - weight_used` for a spool on the shelf, and
`inventory-remain.slot_materials[].remaining_g` for a loaded one — that is Bambuddy's
own reconciliation of the AMS against the inventory, and it wins where both exist.

Two traps worth stating because they are easy to get backwards:

- **An AMS `id` is the printer's numbering, not a list index** (the recorded H2C
  reports `[0, 1, 128, 2]`, the AMS-HT at `128`), and `ams_switch_inlet` keys them as
  **strings**. `ams_mapping` is `ams_id * 4 + tray_id`, `128` for the AMS-HT, and the
  external spool is `vt_tray`, not an AMS at all.
- **`remain: -1` means unknown, not empty.** An untagged spool reports it, and a
  "0 g left" warning built on it would fire on every third-party spool.

## 4. Compatibility rules — derived, never listed

No hand-kept material table. Every rule is a comparison of data Bambuddy already
holds, so it stays right as filaments are added:

1. **Temperature window.** Intersect `[nozzle_temp_min, nozzle_temp_max]` over the
   spools chosen for one extruder. Empty intersection → warning. The window comes
   from the AMS tray for a loaded spool, then the spool row, then the spool's filament
   preset. *This is the PLA-with-PETG rule*: 190–230 against 230–260 does not
   intersect, so the warning falls out of the data instead of out of a list.
2. **Material mismatch** is reported as the material names behind rule 1, not judged
   separately.
3. **Unloaded spool** — allowed. The plan records the spool, and the dialog says
   "load *Bambu PETG Basic — Misty Blue* into AMS 0 slot 2" using its last known
   assignment, or "into any free slot" when it has none. It never fails silently.
4. **Not enough filament**: `remaining_g < used_grams × copies`, only when the slice
   has reported grams. Unknown grams say so rather than guessing.
5. **Preset nozzle mismatch**: the pipeline's process preset names its diameter
   ("… H2C 0.4 nozzle"); the spool's filament presets are keyed by `nozzle_diameter`;
   the live nozzle is `status.nozzles[extruder].nozzle_diameter` (a **string** there,
   a float on the queue route). Any disagreement → warning, naming all three.
6. **Reachability**: a slot prints on the extruder Bambuddy reports for it
   (`inventory-remain.slot_materials[].extruder`), and `ams_switch_inlet` says which
   inlet each AMS is switched to. A chosen spool whose AMS is switched to the other
   inlet is unreachable for that slot — a warning, with the AMS and inlet named. The
   inlet→extruder correspondence is *read*, not assumed.

Warnings are advisory and never disable Run; #86's `force` already exists for the
issues Bambuddy itself raises, and these are ScadBuddy's own.

## 5. Auto-match, so the common case needs no clicks

Per slot, in order: exact `slicer_filament` match → colour distance within a small
threshold on the same material → same material and subtype. Loaded spools sort first
and win ties. A slot with no candidate is left empty and blocks Run with a reason.

Filters, all client-side over the one payload: material, subtype, brand, colour
search, loaded-only, and "enough for this print".

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
