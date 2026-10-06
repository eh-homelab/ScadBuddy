# Spool-first printing: choose spools, nozzles, quality and plate; derive the profiles

Written 2026-09-27 against the live Bambuddy **1.2.5.5** and the H2C `3DP-31B-598`
(printer id 1). Every fact marked *measured* was read off that instance on that date.

## 0. What this changes, and what it supersedes

Today the print dialog starts from a **pipeline**: a saved Bambuddy recipe of printer
preset, process preset, filament presets and bed type. Spools are chosen afterwards and
only swap the filament presets. To print 0.2 mm at 0.08 mm on PETG you first need a
pipeline for exactly that combination; there were 16 of them on 2026-09-27, most of
which differ only in nozzle size, layer height and material.

This design turns the dialog around. You choose what you actually think about —
**spools, nozzles, quality, plate** — and ScadBuddy derives every preset from those
choices.

**This supersedes one rule of `2026-09-24-print-flow-design.md` §2: "The pipeline is
never bypassed — the slice borrows *its* `printer_preset`, `process_preset`, `bed_type`
and target."** From this design on, the printer preset, process preset and bed type come
from the resolver (§4), and the target is always the printer the dialog is scoped to.
Everything else in that spec stands: ScadBuddy stores no second copy of Bambuddy's
state, sends no `ams_mapping`, and reimplements no decision Bambuddy already makes.

Pipelines are removed from ScadBuddy's print dialog. The pipelines themselves stay in
Bambuddy untouched; ScadBuddy neither deletes nor edits them.

**The one-click send bar and Settings' default pipeline are out of scope and stay
pipeline-based (amendment 2, confirmed with the user before Task 3).** `SendDialog`
(`POST /outputs/{id}/send`) and the per-model/global default pipeline in Settings are
untouched by this project — this spec's scope is the print picker dialog only. One
exception from the final review: the send bar now runs the global pipeline only. A
stored per-model pipeline is kept but no longer read, because the routes that showed
and changed it went with the picker.
Converting the send bar to spool-first is tracked as its own follow-up,
eh-homelab/ScadBuddy#312.

**Superseded by #312 (2026-09-28): the send bar no longer queues.** `POST
/outputs/{id}/send` only uploads the 3MF to the library, laid out for the Settings
printer, and attaches the edit link. Its queue mode, copies and print options are gone,
and so are Settings' default pipeline, the raw slicer-preset settings, the pipeline
progress route and ScadBuddy's Bambuddy pipeline client calls. The print dialog's run
(§4) is the only path that prints. The amendment 2 paragraph above describes the state
before #312.

## 1. Scope

This is project 1 of 3:

1. **Spool-first print flow** (this spec), wired into ScadBuddy models.
2. **Print any Bambuddy library file** — a file list plus a plate picker (#83) feeding
   the same flow. Separate spec.
3. **Plate composer** — N objects from Z files on one plate. Uses the process default
   print sequence (by layer); no by-object option. Separate spec.

The resolver (§4) takes "a library file plus its plate slots", so projects 2 and 3 reuse
it unchanged.

Out of scope: choosing AMS trays or which extruder a spool feeds (Bambuddy's, §6),
managing the queue, and editing presets.

## 2. The flow

One dialog, in the place the current one lives. Every step follows one pattern:
**offer everything you own, mark what is on the printer, warn rather than block, and
let Advanced override.**

**Simple mode shows only what the user has to choose (#768):** the printer, when there
is more than one; the spools; which plate, for a multi-plate file; and Print, beside the
Checks. Nozzles, quality, plate type, print options, project and copies are Advanced
steps. In Simple mode they still send their defaults: the nozzle size and tier this
model last printed with, else 0.4 mm and Standard; the plate type from the printer's
last print, else the remembered or default one (§4.4); the remembered copies; and the
project the page passed in (the Customize page's own project choice), else the last
project printed to (`last_project_id`), as the Library page's prints use.

| Step | Simple mode | Advanced mode adds |
|---|---|---|
| 1. Plate slots | One row per color in the model — for "All plates", every color any plate uses — each pre-matched to the nearest spool (the existing `suggest`). | — |
| 2. Spools | Any active spool from inventory. Loaded spools are marked with printer + AMS slot (and the side they feed) and listed first. An unloaded spool is allowed and warned: "Jade White PLA isn't loaded — load it before this prints." | Override the filament preset per slot, from the presets compatible with the chosen nozzle size. |
| 3. Nozzles | Not shown: the remembered size, else 0.4 mm. | One nozzle size for the job, both sides — a single radio group, not a per-side choice (§5 test 3 withdrew the per-side size). All four sizes (0.2 / 0.4 / 0.6 / 0.8) are offered; the ones installed in the rack are marked; picking one that isn't installed warns "No 0.6 mm nozzle is installed. Install one before this prints." Standard or High Flow per side. Bambuddy has no High Flow presets (§5 test 1), so choosing High Flow slices as Standard and the step says so: "Bambuddy slices this as Standard flow; High Flow presets aren't supported by Bambuddy yet." (bambuddy#3176). |
| 4. Quality | Not shown: the remembered tier, else Standard. | Fine / Standard / Draft (§4.2), or Bambu's full H2C process list for the chosen size. |
| 5. Plate | Not shown: preselected from the printer's last print (§4.4). | Every H2C plate type. |
| 6. Print options, project, copies | Not shown: Bambuddy's defaults, the page's project (else the last one printed to), the remembered copies. | Bambuddy's queue-item options with Bambuddy's defaults, exactly as `PrintOptionsDisclosure` shows them today (#88); the project; copies. Queue behavior — manual start, waiting for filament — is Bambuddy's. |
| 7. Print | Slice through Bambuddy, then queue. | — |

## 3. What each step reads

| Datum | Source | Notes |
|---|---|---|
| Plate slots | `GET /library/files/{id}/filament-requirements`, falling back to the output's own colors | unchanged from 2026-09-24 §3 |
| Spools, loaded state, remaining | `/inventory/spools`, `/inventory/assignments`, `/printers/{id}/inventory-remain` | unchanged |
| Per-spool presets | `GET /inventory/spools/{id}/filament-presets` → one row per `(printer_model, nozzle_diameter)` | already read since #161 |
| Installed nozzles | `GET /printers/{id}/status` → `nozzles[]` and `nozzle_rack[]` (`nozzle_type`, `nozzle_diameter`) | new; the 2026-09-24 spec deliberately did not read `/status` — this design needs it |
| Presets | `GET /slicer/presets`, `GET /local-presets/` | unchanged |
| Last plate used | `GET /archives/` → newest entry with this `printer_id` and a status of `completed`, `cancelled` or `failed` → `bed_type` | new |

**Measured 2026-09-27 — the rack.** `nozzle_rack[]` held ids 0, 1, 16, 18–21:
`HS00` 0.2 (id 16); `HS01` 0.4 (ids 0, 19, 21); `HH01` 0.4 (ids 1, 18, 20). The second
letter of `nozzle_type` is the flow type (`S` standard, `H` high flow); the first is the
material (`H` hardened steel). That decoding is inferred from the codes present, not read
from a Bambu source; the resolver keys only on the second letter. Which of `nozzles[0]`
/ `nozzles[1]` is left and which is right is **not verified**; the design does not depend
on it (§6). The user also owns 0.6 and 0.8 nozzles that were not in the rack.

**Measured 2026-09-27 — the printer does not report its plate.**
`plate_detection_enabled` is false, `/status` has no plate field and `current_plate_id`
is null. #190 covers using a reported plate once Bambuddy provides one.

## 4. The resolver

`backend/scadbuddy/bambuddy/resolver.py`. A pure function: callers fetch the data in §3
and pass it in, so it makes no network calls and is unit-tested entirely from
recordings.

```
resolve(options, plan, choices, catalogue, spool_presets) -> Resolved
```

- `options: FilamentOptions` — the plate's slots and the spool inventory behind them.
- `plan: FilamentPlan` — which spool the caller picked for each slot.
- `choices: PrintChoices` — the dialog's own choices: nozzles, tier, process name, bed
  type and any Advanced filament overrides.
- `catalogue: _Catalogue` — every printer, process and filament preset Bambuddy has,
  cloud and standard tier alike.
- `spool_presets: dict[int, list[SpoolFilamentPreset]]` — each spool's own presets by
  `(printer_model, nozzle_diameter)`, keyed by spool id.

It returns a `Resolved`: the printer, process and per-slot filament presets, the plate,
and the warnings and errors (§4.5). It reuses what exists rather than re-deriving it:
each spool's own preset for a nozzle size (#161, `spool_presets` above) and the same
per-AMS-slot indexing `slice_filament_presets` used.

### 4.1 Printer preset — as shipped, after §5's tests 1 and 3 both failed

The original design below this line planned a local, ScadBuddy-authored printer preset
for High Flow and a possible mixed-size override. §5's live tests found Bambuddy rejects
**every** `source: "local"` printer preset outright, before slicing: `400 "The selected
printer is not compatible with the process preset in the 3mf."` — for a plain
passthrough preset, one with a `default_nozzle_volume_type` override, one with an
explicit `compatible_printers`, and one created via `POST /local-presets/import`. That
isolated the failure to the local-preset tier itself, not to the HF override or to mixed
sizes, so both are handled without ever naming a printer preset ScadBuddy invented
(see §5's Results):

| Nozzles | Printer preset |
|---|---|
| Any flow, any single size | Always Bambu's own: `printer_preset_name()` returns `Bambu Lab H2C <nozzles[0].size> nozzle` regardless of flow — 0.2 → `GM042`, 0.4 → `GM041`, 0.6 → `GM043`, 0.8 → `GM044`. **No local or `ScadBuddy ·` printer preset is ever created.** |
| Either side High Flow | Same Bambu preset as above. The flow goes in the 3MF instead, each side's as chosen (`nozzle_volume_type`, #484); before that this added an `hf-unsupported` warning, since Bambuddy has no High Flow presets (bambuddy#3176). |
| Mixed sizes | Always a `mixed-sizes` **error** ("The two nozzles are different sizes. Bambuddy can't slice mixed nozzle sizes yet."), surfaced as a 422 before slicing. There is no Advanced override — the earlier plan to keep one behind a "firmware may refuse" warning is withdrawn, since the 400 is Bambuddy's local-preset tier refusing the request outright, not the firmware; an override would only ever error. The UI reflects this: nozzle size is one radio group setting both sides, in both Simple and Advanced mode (§2 step 3). |

Why a printer preset was the plan at all: on the H2C the flow type is a **printer**
setting (`default_nozzle_volume_type` in `fdm_bbl_3dp_002_common`, per extruder, default
`["Standard","Standard"]`), not a process or filament setting — *measured* from Bambu
Studio's bundled profiles in the `bambuddy-slicer` image. `SliceRequest` has
`process_overrides` but no printer overrides, so a preset looked like the only route that
needed no upstream change; it turned out Bambuddy's API rejects that route entirely for
locally-authored printer presets, independent of what they override.

### 4.2 Process preset

Simple mode uses this table; ★ marks Bambu's own default for the size. On 0.6 and 0.8
Bambu's default is its thickest profile, so it is Draft there.

| Nozzle | Fine | Standard | Draft |
|---|---|---|---|
| 0.2 | 0.08mm High Quality (`GP243`) | 0.10mm Standard ★ (`GP245`) | 0.12mm Balanced Quality (`GP246`) |
| 0.4 | 0.12mm High Quality (`GP247`) | 0.20mm Standard ★ (`GP252`) | 0.24mm Standard (`GP255`) |
| 0.6 | 0.18mm Balanced Quality (`GP250`) | 0.24mm Balanced Quality (`GP163`) | 0.30mm Standard ★ (`GP256`) |
| 0.8 | 0.24mm Balanced Quality (`GP253`) | 0.32mm Balanced Quality (`GP164`) | 0.40mm Standard ★ (`GP258`) |

Ids *measured* from the `setting_id` of each `process/*@BBL H2C*.json` in the slicer
image, and each is `compatible_printers: ["Bambu Lab H2C <size> nozzle"]`. The table is
data in `resolver.py`; a Bambu Studio update that renames a profile fails a test that
checks every id resolves in the recorded catalogue. Advanced mode lists every process
compatible with the chosen size (including `GP244` 0.08mm HQ on 0.4 and the Balanced
Strength profiles), preselecting the tier's.

### 4.3 Filament preset per slot

In order:

1. An Advanced override for the slot.
2. The spool's own preset for `(H2C, size)` from `/inventory/spools/{id}/filament-presets`
   — what the filament intake writes, and what #161 already uses.
3. Bambu's Generic profile for the spool's material at that size
   (`Generic <material> @BBL H2C <size> nozzle`, or `Generic <material> @BBL H2C` for
   0.6/0.8), from the catalogue.
4. Otherwise a **slot error**, e.g. TPU at 0.2, where Bambu ships no profile. The user
   picks one in Advanced mode.

`filament_colours` carries each spool's `rgba`, so the sliced file records what will
actually print (Bambuddy #2977).

**Wire format, as shipped.** Bambuddy's slice expects a per-AMS-slot array indexed by
`slot_id - 1`, not a compact list over only the slots the plate uses — a plate using only
slot 2 sends a length-2 array with that spool's preset at index 1, mirroring the old
`slice_filament_presets`. A slot the plate doesn't use is padded with the first resolved
slot's preset (never invented, since Bambuddy can't look up a preset id that isn't real);
if nothing resolved at all, every used slot already carries a slot error and the request
is rejected (422) before this array is ever built.

**HF and filament presets.** An H2C filament profile holds one value per nozzle variant
(`filament_extruder_variant`: `Direct Drive Standard`, `Direct Drive High Flow`, and on
0.4/0.6 `Direct Drive E3D High Flow`). Bambu's system and Generic profiles carry every
variant. 3dfilamentprofiles exports declared only `Direct Drive Standard`; on 2026-09-26
the 72 imported ones in this Bambuddy were widened to all variants, copying the Standard
values (the same values Bambu's Generic profiles use for every variant). §5 test 4
checks the slicer uses them.

**Extruder per filament (#469, #768, #797).** The run refuses nothing on the mounted
nozzles, and warns of one thing only (below).
From #538 until #768 it refused, before upload, a size neither mounted nozzle (nor a
rack spare) had, a multi-color print when only one side had the size, and a spool on
the side with another size, and it warned when a side was unreported or a mounted
nozzle of the size was High Flow. Those rested on the premise that the slicer spreads
a multi-color print across both extruders (queue item 108 paused with HMS 05FE8053,
"the left nozzle is not matched", with no spare 0.2 in the rack). Measured by the
maintainer's test print, 2026-09-29: a two-color print sliced for 0.2 mm printed
through the one 0.2 mm nozzle while the other extruder had a different size fitted. The
printer handles its nozzles itself, and the H2C swaps hotends from its rack (§6), so
those refusals and warnings are gone, from the run, the check before Print (#755) and
the dialog alike. One warning stays, by the owner's rulings on #772 and #797: when a
side the slice may use has a nozzle of the chosen size mounted in the other flow than
the one sliced there (`nozzle_type` `HH01` is High Flow), the run and the check before
Print carry an `hf-mounted` warning (#723; queue item 149 paused on a High Flow nozzle
sliced as Standard). Since #484 the slice states each side's flow as chosen, and it is
offered only a side with a nozzle of that flow when one side alone has one (#834), so
the warning is left for the one side offered when no side has the flow, either side
when the slicer chooses, and the rack side until a rack pick swaps on a hotend of the
flow (#1238). A library file prints as its author left it: it is offered no side, and
it is taken as Standard on both, whatever flow is chosen, as are its rack preview and a
manual rack pick (the flow it states is not read). The warning is advisory only, never
a refusal, and it changes nothing the run sends, since a print may be set up before its
nozzle is fitted. An unreadable printer status gives no warning rather than assuming a
side. The dialog shows `hf-mounted` in Simple and Advanced mode alike, and it never holds
Print; the nozzle step's own note (`not-installed`) stays Advanced only.

That print was sliced in desktop Bambu Studio 02.08.02.61 ("Name Keychain (H2C)",
project Raegan): printer `Bambu Lab H2C 0.2 nozzle`, process `0.08mm High Quality @BBL
H2C 0.2 nozzle`, `Bambu PLA Basic @BBL H2C 0.2 nozzle` for the second filament,
`nozzle_diameter` `["0.2","0.2"]`, Standard flow on both sides, and `filament_map`
`["1","1"]` under `"Auto For Flush"`. The run's presets for a Fine 0.2 run are the
same, and the print run's upload writes the same project-level map
(`bambu3mf.one_extruder_map`).

**Which extruders the slicer may use (#834).** That `filament_map` is not where the
filaments went. `project_settings.config` keeps the map it was given; the slicer's
actual grouping is in `slice_info.config` (`filament_maps`, and one `<nozzle
extruder_id>` per group). Archive 36 (the test print) says `filament_maps` `"2 2"` and
one nozzle, `extruder_id="2"`: every filament on the slicer's second extruder. Queue
item 159, sliced through Bambuddy with the same presets, says `"2 1"` and two nozzles:
filament 2 on the slicer's first extruder, whose G-code pre-heats the left (`M104 T1`).
It paused with HMS 05FE8053. Both files state `nozzle_diameter` `["0.2","0.2"]`, so the
printer does not check an extruder the file leaves unused. It checks the ones the slice
prints on.

The slicer's extruder order is not the printer's. The H2C preset's
`physical_extruder_map` `["1","0"]` makes the slicer's extruder 1 the left (physical 1)
and its extruder 2 the right (physical 0). Upstream Bambuddy maps dispatches through
the same table (0 right, 1 left), and its `extract_nozzle_mapping_from_3mf` gives item
159 `{1: 0, 2: 1}` (slot 2 on the left) and archive 36 `{1: 0, 2: 0}`. So `filament_map`
"1" is the left, and `one_extruder_map` names the left. It is inert either way: under
"Auto For Flush" the slicer does its own grouping.

What the grouping follows is `extruder_nozzle_stats`, the nozzles each extruder has, in
the slicer's order. Bambu Studio writes it from the printer (archive 36:
`["Standard#0|High Flow#0","Standard#1"]`, so no 0.2 on the left). The headless slicer
falls back to the preset's `["Standard#1","Standard#6"]`, so both sides look like they
have the size. Measured against the deployed slicer (`bambu-studio-api:bambuddy-1.2.5.6`,
2026-09-30) on a ScadBuddy 3MF: `["Standard#0","Standard#1"]` in the 3MF's
`project_settings.config` puts every filament on extruder 2 (`"2 2"`, one nozzle, no
second-extruder pre-heat). `["Standard#1","Standard#0"]` puts them on extruder 1, and
`["Standard#0","Standard#0"]` fails the slice ("No valid nozzle found"). The run's
upload now writes it (`extruders.slicer_nozzle_stats`), and so does Generate's project
file, so a print on the same printer still reuses that file. It is written only when
exactly one side has a nozzle of the size: the one mounted, or for the right, a spare in
the rack. A standard nozzle is preferred to a High Flow one. When both sides or neither
has the size, or the status cannot be read, the file is left as it was, and nothing is
refused (#768). #791's download keeps `nozzle_diameter` `[size, size]`. Bambu Studio
writes the same, and archive 36 printed with it.

Not done: several hotends of one size on the rack side, one per color. `Standard#6` on
the right gives two groups on extruder 2, and each group needs a rack position picked at
dispatch (upstream #1784). `status.nozzle_rack` lists the spares, but ScadBuddy picks no
position (§6).

What stays is a label. Extruders are physical: 0 is the right (main), 1 the left, and
`status.nozzles` is indexed the same way. The filament step marks each loaded spool
with the side it feeds, and lists the mounted nozzles, as information only:

- **With the Filament Track Switch** (`fila_switch.installed`; printer 1 has one), the
  switch routes any AMS to either nozzle (user ruling, 2026-09-28). A spool's side, from
  `ams_switch_inlet` (inlet A left, B right, as upstream `fts_routing.py`), is shown
  only as "rests on L/R".
- **Without the switch**, each AMS is wired to one side: the external holder's tray
  (assignment `ams_id` 255, tray 0 left, tray 1 right), else `ams_extruder_map`, where
  anything but 0 or 1 is unknown, and the badge is left off.
- The `not-installed` warning (§4.5) still says when no hotend of the chosen size is
  anywhere in the rack, mounted or spare. It is advisory, never a 422.

### 4.4 Plate

Preselect order (amendment 4): the printer's last print's `bed_type` (§3) → ScadBuddy's
own remembered plate for this printer (`printer_bed_types`, #83) → `Textured PEI Plate`.
Shown as "Last print used: Textured PEI" — a guess, never "on the printer" — only when
the first source won; otherwise it's shown as the remembered or default plate with no
such claim. Choosing a different plate than the last print shows "The H2C's last print
used Textured PEI. Swap to Cool Plate before this starts." **In the dialog only:**
Bambuddy queue items have no note field (*measured*: `PrintQueueItemCreate` has no
note/comment/description property).

**Plate-compatibility warnings, from a filament's per-plate bed temperature, are
dropped from this project (amendment 1, confirmed with the user before Task 3).**
The original plan read each filament profile's `cool_plate_temp`, `eng_plate_temp`,
`hot_plate_temp`, `textured_plate_temp` and `supertack_plate_temp` — 0 meaning Bambu
does not support that plate for that filament — from `GET /slicer/preset-values`. That
route answers `400 "Only the 'process' slot is supported"` for `slot=filament`
(*measured* 2026-09-27); asking with `slot=process` and a filament id happens to
resolve cloud presets, but returns no plate temperatures for standard-tier presets and
does not resolve local ones at all. Building on that would mean depending on an
accident of the cloud-tier response shape, so it isn't built. The preselect-from-last-
print plus swap reminder above is everything §4.4 ships; there is no
filament/plate-temperature warning.

### 4.5 Errors versus warnings

- **Errors** block the Print button (422) and name the slot or setting: no filament
  preset for a slot (§4.3.4); mixed nozzle sizes (§4.1 — there is no override).
- **Warnings** show and allow printing: spool not loaded; nozzle not installed; a
  nozzle of the chosen size mounted in the other flow on a side the slice may use
  (`hf-mounted`, §4.3 — shown in Simple and Advanced mode alike, and never blocks
  Print); no size-specific preset for a spool, falling back to Bambu's Generic (§4.3);
  plate differs from the last print (§4.4 — there is no plate/filament-temperature
  warning).

### 4.6 Template print settings (#770)

The resolver picks the process preset; the template may still say how it prints best.
A template's `print_settings` (its `model.json`, keys and values allowlisted by
`PRINT_SETTING_VALUES` in `library/catalogue.py`) go on every slice as
`SlicePlan.process_overrides`, sent as the `SliceRequest`'s `process_overrides` over the
resolved process preset, and are part of its `preset_key`. They are not a choice in the
dialog. A downloaded 3MF gets the same settings in `project_settings.config`, listed in
`different_settings_to_system` as edits to the system process. The print-flow spec
(`2026-09-24-print-flow-design.md` §4) has the details.

## 5. Unknowns to test before building on them

Run against the live Bambuddy before the implementation that depends on each. Results
are written back into this section with the date.

| # | Question | Test | If it fails |
|---|---|---|---|
| 1 | Does Bambuddy's slicer honor an HF local printer preset? | Create one (H2C 0.4, left HF), slice a small file, read the sliced 3MF's project settings for `nozzle_volume_type` and the HF filament values. | HF becomes Advanced-only, labelled "Bambuddy may slice as Standard"; filed upstream. |
| 2 | Does the printer accept that file on an HF nozzle without the mismatch warning of Bambuddy #3136? | Queue it with manual start and read the printer's response. Needs the printer idle and the user present; asked for first. Nothing prints. | As 1. |
| 3 | Can one job mix nozzle sizes? | Slice with 0.2 on one side and 0.4 on the other. | Advanced keeps it with the warning; the resolver surfaces the slicer's error. |
| 4 | Are the widened 3DFP presets' HF values used? | Slice with a 3DFP-preset spool on an HF nozzle and read the resolved filament settings. | Fall back to the Generic profile for that material on HF, with a warning. |

### Results

| # | Result | Measured value | Date |
|---|---|---|---|
| 1 | **Fail** | Every slice attempt using a `source: "local"` printer preset — created via `POST /local-presets/` (plain passthrough, with `default_nozzle_volume_type` override, with an explicit `compatible_printers`, with explicit `printer_model`/`nozzle_diameter`), via the deprecated `printer_preset_id` field, and via `POST /local-presets/import` — was rejected before slicing with `400 "The selected printer is not compatible with the process preset in the 3mf."` A baseline slice of the same file/process/filament combination using the `standard`-tier id `"Bambu Lab H2C 0.4 nozzle"` succeeded (`library_file_id: 104`), isolating the failure to the local printer-preset tier itself rather than to the HF override, the file, or the process/filament choice. No `nozzle_volume_type` / `filament_max_volumetric_speed` values were ever produced. | 2026-09-27 |
| 2 | Not run — moot: test 1 showed Bambuddy rejects local printer presets before slicing, so there is no HF file to queue. | — | 2026-09-27 |
| 3 | **Fail (same root cause as test 1)** | A second local preset (`nozzle_diameter: ["0.2", "0.4"]`) hit the identical `400 "The selected printer is not compatible with the process preset in the 3mf."` before any mixed-size-specific behavior could be observed. | 2026-09-27 |
| 4 | **Fail (same root cause as test 1)** | HF preset + local filament preset id `16` (`Insignia PLA+/Pro -- Other -- WHITE (3DFP Ks5G96TQn) @H2C`) hit the identical `400` error before `filament_max_volumetric_speed` could be read. | 2026-09-27 |

### Acceptance (after deploy)

This ran against the deployed build `sha-54d56ea`, which includes #335. The scripted check
is `name-keychain`, 0.2 + Fine, two Bambu PLA Basic spools, and manual start. It passed.
The follow-up real prints failed at the printer because of #469.

| Check | Result | Measured value | Date |
|---|---|---|---|
| 0.2 printer preset on the queue item | **Pass** | Queue item 104's sliced file: printer `Bambu Lab H2C 0.2 nozzle` | 2026-09-28 |
| Fine tier resolves to `0.08mm High Quality` | **Pass** | Process `0.08mm High Quality @BBL H2C 0.2 nozzle`, layer height 0.08 | 2026-09-28 |
| Both spools' colors | **Pass** | Slots 1/2 = `#9D432C` / `#00B1B7`, both on `Bambu PLA Basic @BBL H2C 0.2 nozzle` | 2026-09-28 |
| Manual start honored | **Pass** | `manual_start: true`, status `pending`, `started_at: null` | 2026-09-28 |
| A real print starts (queue item 108, silk, 0.2 standard, auto start) | **Fail → #469** | The printer paused at layer 0 with HMS `05FE8053`: "The left nozzle is not matched with slicing file." The slice sets both extruders to the chosen size. This H2C has 0.2 on the right and 0.4 on the left. | 2026-09-28 |

Also found:
- #470: the run POST can outlive the 60 s ingress timeout. The client gets a 504 while the item is still queued, so a retry double-queues.
- #476: the sliced plate thumbnail shows the model's authored colors, not the chosen spools'. The sliced `filament_colour` is correct.

### Acceptance after #538 (2026-09-29)

Rerun against the deployed builds `sha-74f634f` and later, which include #538's refusals and hotend-rack handling. The printer was fitted with a 0.4 mm nozzle on each side and held one spare 0.2 mm hotend in its rack. Each run used a `name-keychain` output, Fine, and manual start, and the user started each print on the printer.

| Check | Result | Measured value |
|---|---|---|
| Two colors at 0.2 are refused before upload | **Pass** | 422, nothing uploaded: "This printer has a 0.4 mm nozzle on the right and 0.4 mm on the left, and one spare 0.2 mm hotend in the rack… Fit a 0.2 mm nozzle on both sides, or print in one color." |
| One color at 0.2 uses the rack's spare | **Pass** | Queue item 150: sliced at 0.2, `0.08mm High Quality`. At start the printer reported 0.2 on the right and 0.4 on the left, swapped in from the rack, and completed 03:55 to 04:43 UTC with no HMS. |
| One color at 0.4 prints | **Pass** | Queue item 151: sliced at 0.4, `0.12mm High Quality`, completed 03:24 to 03:49 UTC. |
| Two colors at 0.4 print | **Fail** | Queue item 149: sliced at 0.4, 0.12 mm layers, colors `#BECF00` / `#00B1B7`, `manual_start: true`. Queued 02:13 UTC; as a manual start it waited until the user started it, at 13:15 UTC, and paused at layer 0 with HMS `05FE8053`, "The left nozzle is not matched with slicing file." Both sides were 0.4 mm, but the right was standard (`HS01`) and the left High Flow (`HH01`), and both were sliced as standard. #538's refusal compares size only; tracked in #723. |

This closed #469 for sides that differ in size: that case (queue item 108) was refused before upload (withdrawn by #768, below), and the rack swap that #538 assumed is confirmed. Sides that match in size but differ in type still pause at layer 0 (item 149), so the acceptance does not pass until #723 lands and a two-color print is rerun.

Also found:
- Each run POST still outlives the 60 s ingress timeout, giving a 504 while the item queues (#470). Every run above was checked on Bambuddy's queue rather than retried.
- A one-color print needs a one-color output. Two plate slots are two filaments to the slicer even on the same spool, so the multi-color refusal applies (by design).

### The maintainer's test print (2026-09-29, #768)

A two-color print sliced for 0.2 mm printed through the one 0.2 mm nozzle, while the other extruder had a different size fitted. #538's multi-color refusal (first row of the table above) refused exactly that, so the run no longer refuses anything on the mounted nozzles; only the advisory High Flow warning remains (§4.3, #797).

### Acceptance after #840 (2026-10-01)

This is the two-color rerun that the #538 acceptance was waiting on. It ran against a build that includes #840, which tells the slicer the only side with the chosen nozzle (`extruder_nozzle_stats`, §4.3). The printer had a Standard 0.2 mm nozzle on the right and 0.4 mm on the left. The job was a `name-keychain` output ("Reagan"), Fine, on two Bambu PLA Basic spools. It was queued as a manual start through the Print dialog, and the user started it on the printer.

| Check | Result | Measured value |
|---|---|---|
| 0.2 printer preset on the queue item | **Pass** | Queue item 160's sliced file: printer `Bambu Lab H2C 0.2 nozzle` |
| Fine tier resolves to `0.08mm High Quality` | **Pass** | Process `0.08mm High Quality @BBL H2C 0.2 nozzle`, layer height 0.08 |
| Both spools' colors | **Pass** | `#00B1B7` / `#EC008C`, both `Bambu PLA Basic @BBL H2C 0.2 nozzle` |
| Only the side with the nozzle is sliced | **Pass** | One nozzle group, `extruder_id="2"` (the right), 0.2 Standard; `filament_maps` `2 2`; `extruder_nozzle_stats` `["Standard#0","Standard#1"]`; no pre-heat of the left |
| A two-color print completes | **Pass** | Started 01:49:57 and completed 03:08:27 UTC with no HMS, 6.88 g. [Print 84](https://scadbuddy.internal.nullreference.io/prints/84) in ScadBuddy; [finish photo](https://scadbuddy.internal.nullreference.io/api/v1/prints/84/photos/finish_20260930_230831_4f4c887b.jpg) and [plate thumbnail](https://scadbuddy.internal.nullreference.io/api/v1/prints/84/plates/1/thumbnail), both in the chosen colors. The times are Bambuddy's queue `started_at`/`completed_at`, in UTC. The finish photo's filename is stamped in local time (EDT, UTC−4), so `20260930_230831` is the same 03:08 UTC finish |

The run before it, queue item 159, was sliced before #840 and paused at layer 0 with HMS `05FE8053`, "The left nozzle is not matched with slicing file." The slicer's "Auto For Flush" grouping had split the filaments across both sides. It was cancelled, and 160 is that print resliced. This closes the acceptance: the spool-first flow queues, slices and completes a two-color print on this printer.

The user passed it with notes, from nine photos (in the template's media on [`name-keychain`](https://scadbuddy.internal.nullreference.io/m/builtin:name-keychain), and copied with metadata stripped to [`media/2026-10-01-acceptance-160/`](media/2026-10-01-acceptance-160/)). Colors and letter edges are correct, and nothing dragged across the letters, which was the defect in earlier runs. The underside and edges are clean. What remains is cosmetic and comes from slicer tuning, not from the flow:
- a few fine strings in the counters of `e` and across the key-ring hole;
- faint diagonal scuffs and small zits on the letters' top surface.

## 6. What Bambuddy decides, and ScadBuddy does not

- **Which AMS tray and which extruder each spool feeds.** Bambuddy's scheduler computes
  it at dispatch (`_compute_ams_mapping_for_printer`) when a queue item has no
  `ams_mapping`, which is what ScadBuddy sends. *Measured*: no endpoint previews that
  mapping (`check-eligibility` is pipeline-only and reports issues, not placements). A
  placement preview is not requested upstream for now; ScadBuddy shows only facts it can
  read (loaded or not, installed or not).
- **Which rack nozzle is used.** Superseded by
  `2026-10-01-rack-nozzle-selection-design.md` (#836): ScadBuddy ranks the H2C's rack per
  sliced filament group and sends `nozzle_rack_choice`, keyed by group id, on every
  slice-and-queue print; "Let Bambuddy pick" in Advanced mode restores Bambuddy's own
  choice. The slicer still decides which extruders the file prints on; ScadBuddy only
  tells it which sides have the size (`extruder_nozzle_stats`, §4.3, #834).
- **Queue behavior.** Manual start, waiting for filament, order: Bambuddy's options,
  Bambuddy's defaults.

## 7. What changes in the code

**New**

- `bambuddy/resolver.py` — §4, including the tier table. **Not built:**
  `bambuddy/printer_presets.py` (find-or-create the HF printer presets) — §5 tests 1–2
  both failed, so there is no local printer preset for the resolver to find or create;
  §4.1 covers what ships instead.
- `GET /print/outputs/{id}/choices` — one aggregation for the dialog: installed nozzles,
  tiers and processes per size, plate types plus last plate, spools (the existing
  filament options). **Printer choice (amendment 3).** Without a pipeline there is no
  target, so this route also picks the printer: the model's remembered printer, else
  `settings.printer_id`, else the first active printer. Only one printer exists on the
  live instance today, so the dialog's control is a plain select rather than anything
  that has needed to handle a real choice yet.

**Changed**

- `run_for_output` takes `{spool plan, nozzles, quality, plate, options, advanced}` and
  always takes the slice-then-queue route. The `route="pipeline"` branch and
  `PipelineRunCreateRequest` use are removed from the print path.
- `PrintPicker.tsx` becomes the steps of §2 with a Simple/Advanced toggle, reusing
  `FilamentPicker` and `PrintOptionsDisclosure`. **Per-model memory, as shipped:** the
  chosen printer, spool plan, nozzles, tier and process name are remembered per model
  (`ModelPrintChoices`), the same way print options and copies already were; plate type
  is remembered per **printer**, not per model (`printer_bed_types`), since the plate is
  a property of what's on the bed, not of the model being printed. Reopening the dialog
  restores those choices, and the dialog opens in Advanced mode if the remembered choice
  is a named process or a High Flow flow — either one would otherwise be applied unseen
  from Simple mode.

**Removed from the dialog**

- Pipeline selection (#86): `NewPipelineForm`, the per-model default pipeline, and the
  pipeline eligibility check. Their API routes are removed with them.

**Issues**

- #84 (epic) gets a note that the print flow no longer starts from a pipeline, pointing
  here. #83 (plate index) and #190 (reported plate) are linked, not absorbed.

## 8. Test strategy

- **Resolver unit tests**, from recordings: every size × tier; HF on each side and both
  (the `hf-unsupported` warning, never a local preset); mixed sizes (always the
  `mixed-sizes` error, §4.1); preset fallback to Generic and the refusal (TPU at 0.2);
  the plate-differs warning; every tier id resolves in the recorded catalogue. New
  recordings: `printer-status` with `nozzle_rack`, `archives`, `slicer-presets` with the
  H2C processes.
- **API tests**: `test_print*.py` move from pipeline-based requests to choice-based; the
  `/choices` route gets its own.
- **Frontend**: Vitest for `PrintPicker` in both modes (warnings shown, errors disable
  Print); Playwright against msw mocks for the whole dialog.
- **Live**: §5's four tests, recorded here before merging.
