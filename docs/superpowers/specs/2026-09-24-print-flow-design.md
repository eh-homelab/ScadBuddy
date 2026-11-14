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
| Plate | `bed_type` (sliced with; remembered per printer), `plate_id` / `all_plates` | #83 |
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
  └── a filament plan, or queue-level         → POST /library/files/{id}/slice   (the
      options                                    pipeline's OWN presets + bed type,
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

> Superseded for the print picker by `2026-09-27-spool-first-print-design.md` §0; the
> one-click send bar still runs the configured pipeline until #312.

**What actually escalates, as shipped.** Only a **filament plan** (`run_for_output`) or
**queue-level print options** (`_queue_send`, #88) take the second route. A `printer_id`
on its own does not: it scopes the picker and binds the queue item *once a plan already
forced the queue route*, and is ignored otherwise. A `project_id` on its own does not
either — a project changes which library folder the 3MF uploads into, which needs no
queue item. Anything else in the table above that reads like a trigger is the eventual
three-issue design, not a path to go looking for in the code today.

Escalating is visible in the dialog before the click ("Bambuddy will slice this for
*printer*, then queue it"), because it changes which printer the copies land on: a
class-targeted pipeline fans out, a queued item does not.

**As built: the request picks the path (#5352).** A run reaches its printer through
Bambuddy's queue or over the LAN (`PrintRunResult.path`). Before #5352 only the stored
backends and the printer row chose it (`printers/hot_path.py`, `print_run.run_path`).
`PrintRunRequest.path` now lets the caller choose for one print:

- Omitted: the choice as before. The run key does not change (`runs.run_key` leaves the
  field out when it is not sent).
- `bambuddy`: Bambuddy's queue, even where the run would go over the LAN.
  `run_route(..., asked=)` and `ensured_route` return a route that is not native. They read
  no LAN report, so a stale or missing report refuses nothing. `prepare_run` does not read
  the path again. The printer must link a Bambuddy printer, or the run is refused as on
  Bambuddy's path today (`_not_on_bambuddy`). The run then takes a project and every
  `PrintOptions` field. The calibrations (`bed_levelling`, `flow_cali`,
  `nozzle_offset_cali`, `vibration_cali`) and `timelapse` go to `POST /queue/` as before
  (`dispatch.enqueue_plate`). `QueueItemCreate` already names each of them as Bambuddy's
  `PrintQueueItemCreate` does (recorded in
  `backend/tests/bambuddy/recordings/openapi/scadbuddy-routes.json`). No field is new.
- `native`: the LAN. If the run would not go over the LAN, the check and the run refuse it
  (`print_run.LAN_PATH_REFUSAL`, 422). They never send it through Bambuddy instead.

The LAN start (`printers/lan.py` `project_file_command`, line 173) has no field for
`bed_levelling`, `nozzle_offset_cali` or `vibration_cali`, so the printer applies its own
default. It always sends `flow_cali: false` (`lan_models.ProjectFile.flow_cali`, which
`print_activities._command` never sets). So a native run refuses only a value the LAN
cannot honour, with a 422 (`options.lan_calibration_refusal`, `LAN_CALIBRATIONS`):
`flow_cali` `"on"` or `"auto"`, `bed_levelling` or `nozzle_offset_cali` `"on"` or `"off"`,
and `vibration_cali` either way (a plain bool, with no "the printer decides"). What the
LAN does passes: `flow_cali` `"off"`, and `"auto"` for bed levelling and nozzle offset.
The dialog offers only those values on a native route, with a hint. Before #5352 the LAN
start dropped them without a word. `timelapse` and `layer_inspect` are LAN fields and still pass. A remembered option
(global, printer or model scope) is not judged: it stays a Bambuddy queue option, as
before. The refusal is in `prepare_run`, in the dialog's check (`check_print`,
`asked_path_refusal`) and in the run's `print_check` activity, before anything is
recorded.

The refusal's problem type is `options.LAN_CALIBRATION_PROBLEM` (#5602). Its extension
`bambuddy_linked` says whether the row links a Bambuddy printer
(`print_run.lan_calibration_problem`). Its text names the Settings > Printers control,
which the dialog shows as it is. For a caller of the API or the agent, on a row that links
a Bambuddy printer, the print route adds one sentence, `LAN_CALIBRATION_API_HINT`: send
`path: "bambuddy"` for this one print (`api/printing.py` `_answered`). A row that links
none refuses that path too (`_not_on_bambuddy`), so its refusal names no path. The route
tells the two callers apart with `asked_by_api`: an agent's call names its author
(`core/authorship.py`), and the page sends `X-ScadBuddy-Request: 1` on every request
(`frontend/src/lib/requestHeader.ts`). An order print's routes record that as the
operation's `api`, because the print starts later, with no request. The background slice
keeps the plain text. When the route turns native, the dialog shows which values it
cleared ("Flow calibration On was cleared: the LAN cannot set it."). If the user then
picks another printer that is not native, each value comes back, unless the user set that
option again in the meantime. The same printer that reads as not native for a moment
puts nothing back (`frontend/src/lib/useLanCleared.ts`). A flow's
approval line for a `path: "native"` call on a printer that prints through Bambuddy
says "Refused" (`flows/print_binding.py` `bind_call`).

Every reader that takes the request judges the route as the run does. The dialog's
checks (`POST /print/outputs/{id}/check`, `POST /print/library/{id}/check`) pass `path` to
`dialog_route`: with `bambuddy` it answers `None`, so the check builds a client and is
Bambuddy's check. The background slice (`preview_slice`) and a quote's slice
(`SliceQuoteRequest.path`, `orders/operations.py` `PreviewSlicer`) pass it to
`slice_route`, so a `bambuddy` print finds the slice made on its own route. The analyzers
(`AnalysisRequest.path`) pass it to `run_route`, and a flow's approval line
(`flows/print_binding.py`) names Bambuddy for a call that asks for it. No workflow code changed, so the replays need no patch. The tier is
unchanged: `print_output` and `order_item_print` stay `outward`. The agent's
`print_output` and `order_item_print` take `path`, and `order_item_print` takes `options`.
The print dialog has no control for it yet.

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

### A native run's tray question (#4504)

On a native (LAN) run (#3818, plan `2026-10-18-hot-path-without-bambuddy.md` D1) none of
the reads above happen. The answer is `source: "trays"`: the printer's filled trays
from its last LAN report, with the spool providers' cached records laid over them
(`spools/store.py` `SpoolStore.overlay`). Each filled untagged tray the overlay finds no
spool for gets a `trays` question. Its `candidates` name rows of the answer's `shelf`
(`FilamentOptions.shelf`), not of `spools`:

- The shelf is the fresh provider records no tray holds (`store.py` `_shelf`). A
  record is left out when it or its physical spool carries an RFID key, when a tray of
  this printer shows its spool, or when a person's link or any of its spool's records'
  provider `location` puts the spool on another printer. A link or placement on this
  printer holds only through a tray the report shows and the overlay resolved to that
  spool (#4511, #4515). It is a Postgres read in the overlay's own read, of records
  the background refresh cached (`workflows/spool_refresh.py`), so the dialog never
  calls Bambuddy or any other provider. `tests/bambuddy/test_trays_first.py` and
  `tests/api/test_tray_spools.py` check this inside `forbid_bambuddy`.
- A tray's candidates are the shelf rows of the same material family within
  `CLOSE_COLOUR`, most filament first and empty last (`bambuddy/trays.py`
  `close_spools`, shared with the Bambuddy path's `unknown_trays`). A tray with none is
  not asked about, because it is a choice of its own already.
- Shelf rows are never print choices. That is why they are not in `spools`, which
  `suggest`, `check` and the resolver match a plan against. Their `spool_id` is
  positional and valid within that one answer only. "Yes" links the row by its
  `physical_spool_id`, else one of its `records` (`{provider, record_id}`), through
  `PUT /api/v1/printers/{id}/trays/{ams_id}/{tray_id}/spool` (`api/tray_spools.py`).
  That writes `tray_spool_links` and nothing in any provider.

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

**A template's own print settings (#770).** A template may declare `print_settings` in
its `model.json`: Bambu Studio process keys and their values (prime tower, support, brim,
or any other process key). Since #2998 there is no allowlist: any key and any value is
taken, a number or a boolean is stored as the string a Bambu config stores (`true` is
`"1"`), and a list is joined with commas (`library/catalogue.py` `_print_setting_text`);
the slicer judges them. They are read as the template's `model.json` has them *now*, not
as they were at render time, and they go two places:

- **Every slice.** They become `SlicePlan.process_overrides`
  (`bambuddy/dispatch.py`) and so `SliceRequest.process_overrides`, which Bambuddy
  writes over the chosen process preset for that slice. They are part of
  `SliceRequest.preset_key`, so a sliced copy made before the template changed them is
  not reused for one after; a template without any keeps the key it always had.
- **Every saved 3MF** (`OutputStore.create`, `render/provenance.py` `stamp`). They are
  written into `project_settings.config` over the process when the output is saved, and
  their keys are listed, `;`-joined, in the process entry (the first) of
  `different_settings_to_system`, so Bambu Studio shows them as edits to the system
  process instead of a new preset. A download serves the saved file byte for byte
  (#2652), so it carries the settings the template had when the output was saved; it
  no longer re-plates for the default printer or asks Bambuddy for preset names.

### Any process setting (#2998)

Every Bambu Studio process setting can be set on a print, through one generic field.
The owner decided on 2026-11-14 (#2998) that every setting is allowed by default: no
allowlist, no named field per setting, and no check that refuses a key or a value. The
slicer is the only judge.

- **The field.** `process_overrides: {<Bambu Studio process key>: value}` on the print
  run's request (`PrintRunRequest`: an output's and a library file's run, a batch step,
  an order item's print, which extends it) and on the slice quote's
  (`SliceQuoteRequest`). A value is a string, a number, a boolean or a list of them (a
  per-extruder option), and is kept as a Bambu config stores it
  (`bambuddy/process_overrides.py` `normalised_overrides`: `true` is `"1"`, `0.2` is
  `"0.2"`, keys sorted; an empty map is no field, so a run's key is the one it had
  before).
- **The order.** The plan's `process_overrides` (`print_run.py` `plan_run`) are the
  filament profiles' (#717), then the template's `print_settings`, then the named
  shorthands (`supports`, `prime_tower`, `print_sequence`), then the request's
  `process_overrides`. So an explicit override wins over all of them. The plan goes into
  `SliceRequest.process_overrides` (Bambuddy writes it over the process JSON it slices
  with) or the native slicer's `slicing/presets.py` `apply_overrides`, and from there into
  the sliced 3MF. A list over a single value is written as it is (Bambu Studio's JSON
  loader reads it joined with commas), where `apply_overrides` refused it before.
- **Checks.** Bambu Studio's option definitions (`PrintConfigDef`) are compiled into its
  binary; neither the image nor the repo holds them as data. So the only check is
  against the resolved process preset's own values: a key it has no value for, or a
  list over one value, is a `process-override-unchecked` warning on the dialog's check
  and the run (`process_overrides.override_warnings`), never a refusal.
- **Host-side settings are stripped.** "Allow everything" covers what the slicer does with
  the print, not commands or files on the machine that slices (the native slicer's pod,
  Bambuddy's slicer sidecar). `process_overrides.HOST_SIDE_KEYS` lists every option of
  Bambu Studio v02.08.02.61's `PrintConfigDef` that runs a command, names a file or path,
  or reaches a print host: `post_process` (runs scripts on the exported G-code),
  `filename_format`, `bed_custom_texture`, `bed_custom_model`, and the print host's
  options (`print_host`, `print_host_webui`, `printhost_*`, `host_type`), with any other
  `printhost_`/`print_host` key. They are dropped and logged where a setting comes in
  (`normalised_overrides`, `PartSettings`' extra keys, a template's `print_settings`), and
  again where the slice is built (`slicing/presets.py` `build_presets`, from the plan's
  overrides and from every preset document, beside `strip_credentials`;
  `dispatch.slice_request` for Bambuddy). A request that named one has it in
  `dropped_settings` (no schema, not in the run key), and its check and run say so in a
  `host-setting-dropped` warning. Custom G-code (`*_gcode`) stays allowed: it changes
  what the printer does and runs nothing on our servers.
- **The slicer's refusal.** A slice that fails is the run's failure, as before. When its
  text names one of the plan's overrides as a whole word, the run's error is a 422,
  `https://scadbuddy.dev/problems/slicer-setting-refused`, with the key as the extension
  `key` (`workflows/problems.py` `setting_refused`, from `PrintRunWorkflow._slice`). The
  wording of Bambuddy's and Bambu Studio's refusals has not been measured, so a refusal
  that does not name the key stays the plain slice failure.
- **The run key and the record.** `process_overrides` is part of `run_key` when sent.
  The run's result records them (`PrintRunResult.process_overrides`), and
  `GET /prints/{archive_id}` lists them, so a reprint sends the same.
- **Quotes.** A slice quote slices with them and records them in its print setup (the
  `choices` column, `quote.py` `print_setup`, `SETUP_FIELDS`; no migration). An item's
  print that sends none prints with the quote's; one that sends others is
  `requote_needed` (`printing.py` `QUOTED_SETUP`), as for the other setup fields.
- **Remembered.** `ModelPrintChoices.process_overrides` is stored in the settings jsonb
  with the model's or file's other choices, left out of the entry when none are set.
- **Reading the values.** `POST /print/process-values` (`api/process_values.py`, body
  `{choices}`) answers the resolved process preset and every key and value of it,
  flattened, from Bambuddy's `GET /slicer/preset-values` or, on `slicer_backend`
  `native`, the image's profiles. The dialog's "Slicer settings" editor lists them, and
  any other key can be typed.

### Settings per part (#5571)

A print can set process settings on the parts of one filament slot: more top layers on
raised letters, say. The print run's request and the slice quote's take
`part_settings`, a map from the slot (`slot_id`, from 1, as `filament_plan` numbers
it) to any Bambu Studio keys (`bambuddy/part_settings.py` `PartSettings`, open since
#2998). The six the dialog first offered stay named fields, so a request that sets them
keys as before: `top_shell_layers`, `bottom_shell_layers`, `wall_loops`,
`sparse_infill_density` (a number is written `N%`), `sparse_infill_pattern` and
`ironing_type`. No key, value or range is refused (#2998); each is written as a Bambu
config stores it, and the slicer judges it. Slot 0 is still a 422 from the request's
validation. A slot that no printed part of
the file is made in is a 422 from `prepare_run` (`part_settings_refusal`, which reads the
file's `model_settings.config` with `printed_part_slots`), so the dialog's check lists
it too. A slot only painted on another part's faces is among them: it has no part of its
own to carry the settings. An output and a library file take them (#5628), on either
library; a sliced file prints as it was sliced, so it is refused (422,
`print_run.PART_SETTINGS_SLICED`). `part_settings_refusal` reads the source's one fetch of
the 3MF (`fetched_3mf`, which the run's other reads share) and opens its archive once
for the layout and the parts (`render/bambu3mf.py` `part_settings_slots`, #5637).

A library file's `model_settings.config` is untrusted, so it is read in chunks up to
`MAX_MODEL_SETTINGS_BYTES` (4 MiB; the largest MakerWorld fixture's is 231,209 bytes,
`2178-find-100-mini-frogs-spot-search-game.3mf`) and its slots are read with
ElementTree. One past the cap, not UTF-8, not well-formed, or with a DOCTYPE takes no
settings per part. Its refusal names that file (422, `send.PART_SETTINGS_UNWRITABLE`,
#5646): the file is laid out, but its parts' settings file cannot be read or written. The
writer rewrites the text in place with patterns that stay linear in the file: a tag is
`[^<>]*`, and a body is a possessive run that stops at the next `<part`, `<object` or
`</object>`. The patterns of #5571 scanned to the end of the file from each unclosed
`<part`: 20,000 of them took 63 s, measured on the dev machine on 2026-11-07. After the
write the result is read back with ElementTree and compared with the file before it,
part by part (`_check_written`). A printed part of a slot that lacks its settings, or a
part that took another slot's (a file written in a way the patterns do not read, such as
an `extruder` tag in a comment), is the same 422, so the copy's key never names settings
its bytes lack and no part is sliced with another slot's settings.

They are written into the laid-out copy, not the process. `send._laid_out_for` adds
each slot's settings to every printed `<part>` of that slot in
`Metadata/model_settings.config` (`render/bambu3mf.py` `set_part_settings`), as
`<metadata key="top_shell_layers" value="8"/>`, after the layout. A part's slot is its
`extruder` metadata, else its object's: in a library file, the filament its author gave
the part. The writer reads an object's `extruder` from its own metadata outside its
parts, before them or after, as ElementTree reads the object's children (#5646). A key the part states already (a Bambu Studio file can) is replaced by the
request's; the part's other keys stay. A negative part or a modifier gets none. A
self-closing `<part …/>` becomes an open and a close tag around them. A painted part
takes the settings of the slot it is made in, not those of its painted faces. In an
author's file they are written even when its layout falls back to the file as it is
(`ArchiveTooLargeError`). A 3MF nothing can be written into (`layout_of` `as_is`: past
the size caps, sliced, or with no `project_settings.config`) prints as it is, so it
cannot carry them: `part_settings_refusal` refuses it before anything is uploaded (422,
`send.PART_SETTINGS_AS_IS`), and `_laid_out_for` refuses a target with settings for one
too. A library file with no 3MF to lay out (past `MAX_DOWNLOAD_BYTES`, or an STL that
holds no mesh) is refused the same way. So the copy's key never names settings its bytes
lack. The copy's `Target.key` gets `&parts:` and a digest of the settings, before the
colours, so a copy with settings is never reused for one without, nor the other way
round; a copy without settings keeps the key it had. `plan_run` builds the copy for the
run, the dialog's background slice and the quote alike. A library file with settings is
always copied (`print_source.file_to_print`): in Bambuddy's library the file itself is
printed only when nothing would change it and no settings are sent, and a native file
is copied whenever it has a 3MF to lay out. A slice quote records the
settings it sliced with (`order_item_quotes.part_settings`, `part_settings.dumped`). An
item's print that sends none prints with its quote's (`orders/operations.py`
`print_body`, from what `print_check` returns), so it slices the bytes the quote priced;
one that sends others is `requote_needed` (`printing.py` `_same_part_settings`).
`PrintRunRequest.part_settings` is in `run_key` only when it sets something, so every
older run keeps its key.

The dialog sets them (#5627). In the filament step, each slot row of an output or a
library file (#5628) has a "Settings for this colour" disclosure
(`components/print/PartSettingsDisclosure.tsx`). A sliced file has none. Since #2998 it is
the same editor as the whole print's "Slicer settings"
(`components/print/SlicerSettingsEditor.tsx`): every key of the resolved process preset
(`POST /print/process-values`, read when it opens), searchable by key, with a "Changed
only" filter and a row to add any other key by name; a typed value is sent as text, and
nothing is refused or clamped. Each row shows the slot's default: what the slot prints with
when the request sets nothing. That is the process's value, with a template's
`part_print_settings` for the slot (#5626) laid over it, per key, as the run lays the
request over the template's (`run_part_settings`). Once a slot's disclosure is open, the
dialog's check asks for the first six keys' defaults
(`?process_values=true` on `POST …/check`). The check reads the process's values with the
preset's other values (`PrintCheck.process_part_settings`,
`part_settings.process_part_settings`; a fractional density is rounded to a whole
percent, and named in `PrintCheck.process_inexact`, so that whole percent typed is still
sent), and gives, for each slot the template sets on the copy
(`template_part_settings`), the process's values with the template's over them
(`PrintCheck.slot_part_settings`). A check that does not ask reads nothing more, since the
read can be one more Bambuddy call. The values are `None` when the preset cannot be read;
the rows then show no default. They stay off until a check that asked for the choices as they
stand has answered. The request's own `part_settings` do not change the process, so the
dialog asks only until the values are read for the other choices as they stand
(`lib/usePrintCheck.ts` `processKey`, #5686): a settled edit of a setting checks again
without reading the preset again. An answer whose values are `None` (the preset could not
be read, or the check refused before it read the process) is not a read: the next check
asks again. The rows still turn on for it, with no default, so the person can type a value. A
process preset edited in Bambuddy while the dialog is open is read again only after a
change of printer, nozzle or other choice, or when the dialog opens again. A re-check that fails keeps the values already read for
those choices, so the rows stay on; a change of printer, nozzle or other choice drops
them, and another process's values are never shown. A value set back to the slot's default, or reset, is unset, and only
what is set is sent (`lib/partSettings.ts` `sentPartSettings`, `withSetting`,
`slotBaseline`; a value compares as Bambu writes it, so `"8"` and `8` are one). So on a
slot whose template sets 5 top shell layers over the process's 3, typing 3 sends 3. A remembered or quoted value equal to the slot's default is dropped once
the defaults are read (`withoutSeededDefaults`); a value the user set in the dialog is never
dropped. The check,
the background slice, the run, a batch step and the quote send the same field
(`sliceOverrides`), so the check lists a slot the file lacks. A print remembers them with
the model's or file's other choices (`ModelPrintChoices.part_settings`, left out of the
entry when none are set). They are kept per model or file only, never at the print options'
global or printer scope: a slot number names one file's colour. A quote opens on the item's
newest slice quote's settings. An item's print opens on the accepted quote's (a test print
on the newest slice quote's) and sends the quote's own settings, so its body matches the
background slice's. Other settings would print what was not priced, so Print is held while
the dialog's settings differ from them. An item print from the API that sends none, or `{}`,
prints with the quote's (`print_body` reads `{}` as none sent). The run's result
records them (`PrintRunResult.part_settings`), and `GET /prints/{archive_id}` lists them as
`part_settings`, found through the queue item's send and its run. A print started over
the LAN has no queue item, so its run is found through the start it became (#5661,
`print_history._run_by_file`). Bambuddy archives a print it did not queue under the name of
the file the printer printed (`services/archive.py` `archive_print`,
[maziggy/bambuddy@2ce0d93](https://github.com/maziggy/bambuddy/blob/2ce0d93451c50516f9a3bacbc80c80e14fbdcc3e/backend/app/services/archive.py)).
That name is the one the run uploaded (`scadbuddy-<run id[:8]>-p<plate>[-c<copy>][-<attempt>].3mf`,
`workflows/printing.py` `_start_lan`), and it names the start. The first read of a link
found by content hash whose file name has that form looks for the start among the
runs of the link's own subject whose result holds it, whatever the run's status (#5733;
today only a run that succeeded holds a result). When exactly one start of one run has that
name, the read records it (`lan_print_archives`: archive, run, start, plate) and lists
that run's settings. Later reads follow the record (`print_history._named_run`), also once
Bambuddy has deleted the archive, and list none once the run is pruned or the archive
belongs to another subject. A send or a recorded start is read while the archive is
fetched; only the file name waits for the archive (#5733). Any other case (no such name, no such start, two runs that share the name)
records nothing and lists none: the link is never a guess. A LAN print reaches `/prints`
only through a content hash link. A native run uploads its slice straight to the printer,
so no file in Bambuddy's library has its hash, and the queue path's scan
(`linking.link_subject_by_hash`) never finds it (#5724). Bambuddy computes an archive's
`content_hash` as the SHA-256 of the 3MF it downloads from the printer (`archive_print`,
`compute_file_hash`, same commit), and that file holds the bytes the run uploaded
(`printers/print_activities.py` `lan_upload`, from the slice's blob). The native library
row of the same slice holds the SHA-256 of the same blob (`library_files.sha256`, written
by `print_sliced_upload`). So the first page of `GET /prints` looks for each subject
with a LAN start in the last 14 days that no archive names yet
(`PrintRunStore.lan_unnamed`, over `lan_print_archives`): it scans Bambuddy's archives
of the starts' days for the hashes of the subject's slices, native and Bambuddy's
(`linking.link_lan_starts`), records each match as a `content_hash` link, and names the
start from the archive's file name as above. At most four subjects are scanned per
request, and one subject at most once per process every 10 minutes
(`linking.LAN_SCANS_PER_CALL`, `LAN_SCAN_INTERVAL`). The read skips the subjects this
process scanned within those 10 minutes, so newer subjects cannot keep an older one from
its scan (#5749). It reads only the runs made at most 14 days before the window
(`runs.LAN_RUN_SPAN`), so a start of a run made more than 28 days ago is not looked for. The
scan asks Bambuddy only for its own slices' hashes, never for a native slice's id. A
start whose print never reached Bambuddy is looked for until its 14 days end, or until its run is 28 days old, whichever comes first. Every answer gives a slot's settings
with only what is set (`part_settings.parts_set_only`, on the read models: the run result,
the print record, the remembered choices and the check), so a reprint can send them again as
they are. A request's own dump keeps every key, so `run_key` is what it was before #5627.

Bambu Studio honours them. Its 3MF loader reads every `<part>` metadata key that is not
one of its own (name, subtype, source, matrix …) into the volume's config
(`src/libslic3r/Format/bbs_3mf.cpp` lines 5464-5471 in `_generate_volumes_new`, tag
v02.08.02.61, commit `5b34346`), and each volume's region is sliced with that config over
its object's, over the process (`src/libslic3r/PrintObject.cpp` lines 3105-3122,
`region_config_from_model_volume`: the print's region config, then the object's config,
then the volume's, then a height range's). So a part's setting wins over its object's
value for the same key, and a height range modifier in the file
(`layer_config_ranges`) wins over both on its layers. The CLI
replaces an object's config only for `--orient` (`src/BambuStudio.cpp` line 5216), which
ScadBuddy never passes.

Measured on 2026-11-07 with the native slicer image
`ghcr.io/eh-homelab/scadbuddy-slicer:sha-5de2c80` (Bambu Studio 02.08.02.61). The input was
a two-colour file from ScadBuddy's own writer (`write_bambu_3mf`): slot 1 a 40 x 40 x 4 mm
base, slot 2 a 20 x 20 x 6 mm block on it (z 4 to 10). It was laid out with
`replate_3mf` for the H2C, then copied once more with `with_part_settings` and slot 2's
`top_shell_layers` 8. Each file was sliced with the CLI's argv as the slicer worker builds
it (`workers/rust/crates/sb-slicer/src/runner.rs`, without `--pipe`): `--load-settings`
with the flattened `Bambu Lab H2C 0.4 nozzle` and `0.20mm Standard @BBL H2C` (whose
`top_shell_layers` is 5), `--load-filaments` with `Bambu PLA Basic @BBL H2C` twice,
`--filament-colour`, `--arrange 0 --slice 1 --export-3mf`. The G-code's `; Z_HEIGHT:` and
`; FEATURE:` lines were then counted per layer. Above z 4 only the block prints (and the
prime tower). Without the setting the block's top shell was a `Bridge` layer at z 9.2 and
`Internal solid infill`/`Top surface` on 4 layers (z 9.4 to 10.0): 5 layers. With it, the
`Bridge` moved to z 8.6 and the solid layers were 7 (z 8.8 to 10.0): 8 layers. The base's
solid layers (z 4 and below) were 6 in both slices. Bambuddy's slicer runs the same
02.08.02.61 CLI on the uploaded copy, but it was not measured with part settings.

A library file (#5628) was measured the same way on the same day and image. The input was
the same two-colour file made an author's (`print_settings_id` `0.20mm Standard @BBL
H2C`, so `layout_of` says `author`), its object stating `top_shell_layers` 6 and slot 2's
part its own 9. `_laid_out_for` laid it out for the H2C (`state_nozzles`, not re-placed),
once without settings and once with slot 2's `top_shell_layers` 7, which replaced the
part's 9 in the copy. Without the setting the base's top shell was 6 layers (z 3.0 to
4.0, `Bridge` then solid) and the block's 9 (z 8.4 to 10.0). With it the base kept 6 and
the block had 7 (z 8.8 to 10.0). So Bambu Studio reads an author's object and part keys,
and the request's value is the one that slices. A first try with the object at 2 and the
part at 3 sliced 5 layers for the base and for the block; a request of 8 on slot 2 gave
the block 8 and left the base at 5. The process's `top_shell_thickness` (1.0 mm, 5 layers
at 0.2 mm) sets a minimum, so a `top_shell_layers` below it changes nothing, from the
object, a part or a request.

A template declares its own (#5626) in `model.json` as `part_print_settings`, beside
`print_settings`, with the same keys (`PartSettings`: any key since #2998). Only a value
that is no setting at all (an object, say) fails the load (`InvalidModelMetaError`), and
a print of the template is then refused (409), as for a bad `print_settings`.
The dialog's background slice and the slice quote refuse it too, with the same 409, so
neither shows or prices a copy without the template's settings that the run would then
refuse (#5649). The dialog's check (`POST /print/outputs/{id}/check`) puts that refusal
first in its `errors`, so the person sees why before Print.
A template names the part by its `// color` parameter, never by slot:
`{"letter_color": {"top_shell_layers": 8}}`. The decision: a slot is a position in the
colours a render used (`layout.colours`, slot N is colour N-1). It moves when two colour
parameters take one value (they become one slot) or when a colour's parts are not drawn.
A colour parameter keeps naming the same parts whatever colours are chosen. The key must
be an OpenSCAD identifier (`COLOUR_PARAMETER_PATTERN`). `Catalogue.part_print_settings`
also refuses a key that is not a `// color` parameter of `model.scad`
(`render/schema.py` `find_annotations`), naming it, so a typo never sets nothing in
silence. A template with a pipeline (its params need not be `model.scad`'s) or with no
`model.scad` only logs a warning.

They are read as `model.json` has them now, as `print_settings` are (#770):
`Catalogue.part_print_settings`, from `LocalOutputs.naming`, which the print worker
reaches through the internal naming route. The naming turns them into the output's slots
(`OutputNaming.part_settings`, `part_settings.by_colour`): each parameter's value in the
output's `params.json` (else the template's default from its cached schema, for an
output from before #4774) is matched, by `colour_hex`, to the slot of that colour in the
output's `colors`. A parameter with no value, or whose colour no slot is, sets nothing;
nor does a colour past slot 64 (`MAX_SLOT`, the bound of `SlotId`, #5649).
Two parameters of one colour share its slot; for a key both set, the first declared
wins. An arranged output (`OutputStore.arranged_from` not empty) takes none: its parts
come from other outputs or library files, it has no params, and it may be filed under
any template, so a default colour's slot could be another file's part. The run (`SourceSpec.part_settings`, from the check activity), the background
slice and the quote (`OutputSource.part_settings`) all reach `plan_run`, where
`run_part_settings` lays the request's `part_settings` over them, per key
(`part_settings.laid_under`). The template's go only where a copy can carry them: not on
an `as_is` 3MF, and only on slots a printed part is made in. So a template's settings
never refuse a run; only the request's are refused (`part_settings_refusal`). They are
not in `run_key`, as `print_settings` are not; the copy's `Target.key` digests what the
copy carries, so a change to them makes a new copy. A slice quote records only the
request's settings (`order_item_quotes.part_settings`); a print reads the template's as
they are at that time, as with `print_settings`.

The print dialog's rows (#5627, above) show each slot's default, the template's
settings laid over the process's values (`PrintCheck.slot_part_settings`), and send only
what the user changes from it. The template's still lie under that request in
`run_part_settings`. A run records only the request's settings
(`PrintRunResult.part_settings`).

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
   has reported grams. Unknown grams decline to judge rather than guessing. For all
   plates, each slot's grams are summed across the plates before this check (#198): a
   plate with unknown grams adds nothing, so the total is unknown only when every
   plate's is.

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

A project is Bambuddy's, and ScadBuddy keeps no second one. There is no ScadBuddy
project entity, no project membership and no project history view: which prints and
files belong to a project is on the project's own page, and a second answer here would
go stale. What is persisted is only defaults for the next filing: `last_project_id`
(where a print dialog with no project of its own opens, and what a run that names none
is filed under), and since #1660 `model_projects`, the project Generate last filed each
model (and preset) into, which the Customize page's picker opens on and Generate asks
before leaving. Both are remembered choices, forgettable in Settings.

- **Create** → `POST /api/v1/projects/` then `POST /api/v1/library/folders/` with
  `project_id` set, which is the pairing Bambuddy's own UI makes.
- **Link** → `GET /projects/` plus `GET /library/folders/by-project/{id}`; a project
  that already has a folder keeps it, so linking twice does not leave two.
- **Send** → the project's folder replaces `settings.library_folder_id` for that send,
  which is what puts the 3MF on the project's page. A project with no folder yet gets
  one created and linked first (as **Link** does) — never the inbox fallback, where the
  copy would be superseded. An output keeps one library copy
  per (folder, target) (#316): a project that has none gets a new copy in its folder,
  even when the output was uploaded before a project was chosen. A copy is never
  moved — a project's file is its record of what it printed, and its slices and
  archives stay where it is — and only a copy in the inbox (`library_folder_id`) is
  ever deleted, when a newer inbox copy supersedes it.
- **Attach** → `POST /projects/{id}/add-queue` and `/add-archives`.

Attaching is a **separate call**, not part of the run: a pipeline run's
`jobs[].queue_entry_id` is null when the 202 returns, and an archive only exists once a
print has finished. Attaching at run time would attach nothing on one route and half on
the other, so `POST /print/outputs/{id}/project` is called from the progress read (#89)
once the ids are known, and again when the archives appear. On the queue route
`project_id` also rides on the queue item itself, so there is no window in which the
entry exists unfiled.

Scope: the project routes map to `Manage Projects`, which the current key may not
carry — the settings page's connection test reports the missing scope by name rather
than the picker failing at submit.

## 8. Test strategy

Backend tests drive `respx` from recordings taken by `GET` only, per
`tests/bambuddy/recordings/README.md`; serials, IPs, access codes, RFID `tag_uid` and
`tray_uuid` are redacted. Shapes that need a `POST` to observe (a slice job result, a
queue-item create, a `PipelineRunResponse` with `jobs[]`) are built from Bambuddy's
`openapi.json` inline, as the existing tests already do. The frontend gets `vitest`
coverage of the opening selection, the filters and both warnings — including that they
are recomputed for a hand-picked spool, not dropped — and one Playwright spec drives
the dialog end to end against `msw`.
