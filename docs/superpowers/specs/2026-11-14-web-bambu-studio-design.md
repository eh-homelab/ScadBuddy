# Web Bambu Studio: full plate, object and slicer control with a better UX

Epic: #6272. Plan: `docs/superpowers/plans/2026-11-14-web-bambu-studio.md`.

> "Ultimately we want to have a web-based Bambu Studio essentially, but with a better UX."
> (owner, 2026-11-14)

## 0. Sources and how they were read

- Bambu Studio rules are cited at tag
  [`v02.08.02.61`](https://github.com/bambulab/BambuStudio/tree/v02.08.02.61). That is
  the version ScadBuddy's native slicer pins (`Dockerfile` `ARG BAMBU_STUDIO_VERSION=02.08.02.61`).
  The files were fetched with `gh api repos/bambulab/BambuStudio/contents/<path>?ref=v02.08.02.61`
  on 2026-11-14. Each rule below links the file and the lines. Bambuddy's own slicer can
  run another version; a rule that moves between versions is re-read when the pin moves.
- The `master` of the same day (`930fe938`) has the same by-object rules at other line
  numbers. One difference: `master` computes the skirt distance with the skirt flow
  (`get_real_skirt_dist(print.config(), print.skirt_flow())`); the tag does not.
- The owner's decisions are in the issues, each dated 2026-11-14: #2998 (any slicer
  setting), #6253 (per-object and per-part settings, the viewer, the capability model),
  #6246 (the by-object rules), #6245 (a template plate is one object), #6219 (remembered
  settings). Where this spec says "the owner decided", the issue comment is the source.
- Where no decision exists, this spec makes the call and lists it in §11 ("Assumptions").

## 1. Intent

ScadBuddy makes and sends multi-colour 3MFs. Today a person who wants to change how a
plate prints must open Bambu Studio on a desktop: move an object, split it, give one
object more walls, turn off the prime tower, order the objects for a by-object print.
The goal is to do all of that in ScadBuddy, in the browser, and to do it better than
Bambu Studio does.

Who uses it:

- the owner and staff in the back office (print dialog, order items, Arrange);
- customers on the storefront, with a small, safe subset;
- the assistant, through tools that do what the UI does.

Why now: ORD-2026-2 (the menorah, 2026-11-14) needed per-object layout for a by-object
print, a prime tower turned off, a skirt setting, and a support-interface filament.
ScadBuddy could set none of them (#2998, #6245, #6246).

### 1.1 Goals

1. Any slicer setting, for the whole print, for one object, or for one part. No allowlist.
2. One plate viewer and editor that selects and edits objects and parts like Bambu Studio.
3. Arrange and the checks follow Bambu Studio's own rules, so a plate that ScadBuddy
   calls good slices without a conflict.
4. Each object on a plate is its own 3MF object, with a stable id.
5. Every choice a person makes in a print or quote dialog is remembered.
6. The assistant can do everything the UI can, with the same ids and the same checks.

### 1.2 Non-goals

- A slicer in the browser. Slicing stays on the server (Bambuddy or the native slicer,
  spec `2026-10-16-own-store-slicer-rust-workers-design.md`).
- G-code preview beyond what `components/print/ToolpathScene.tsx` shows today.
- Mesh modelling (sculpt, boolean, text). Mesh models have their own scene editor
  (`backend/scadbuddy/meshmodel/`, tool `edit_scene`). This epic edits placement,
  grouping and settings of what a render or a file already holds.
- Printer and filament preset editing. Filament profiles are epic #717.
- Bambu Studio's Prepare-tab features that are not about a plate: calibration wizards,
  the device tab, MakerWorld.

### 1.3 Anti-goals (a failure even if it works)

- A setting the slicer accepts but ScadBuddy refuses. The owner decided that no key and
  no value is refused (#2998, "Decision, refined"). A warning is the most a check may do.
- A check that passes a plate the slicer then refuses for a by-object conflict.
- A capability that the browser turns on and that lets a role write what it may not.
  The server checks permissions on every save (#6253, "The host decides").
- An edit that changes the source output. Edits make a new output (#6253, "Selection
  edits everything").
- A UI that is Bambu Studio's dialog copied. Copy its rules and its names, not its
  clutter (§10).

## 2. What exists today

| Area | Today | Code |
|---|---|---|
| Whole-print settings | Named fields (`prime_tower`, `supports`, `print_sequence`) and an allowlist for template `print_settings`. #2998's PR (in progress, another worktree) adds a generic `process_overrides`, a searchable settings list from the resolved process (`POST /api/v1/print/process-values`), free keys, and the agent tool `get_process_values`. It has no option metadata: labels, sections, types and ranges are compiled into the Bambu Studio binary and are not in the image. | `backend/scadbuddy/library/catalogue.py` `PRINT_SETTING_VALUES`; `bambuddy/dispatch.py` `SliceRequest.process_overrides`; `slicing/presets.py` `apply_overrides` |
| Per-part settings | Keyed by filament slot, a fixed list of keys (#5571, #5627). | `bambuddy/part_settings.py`; `render/bambu3mf.py` `set_part_settings`; `components/print/PartSettingsDisclosure.tsx` |
| Objects in a 3MF | One object per declared object (`echo(objects = N)`) or per placed copy of an Arrange layout (#956). A template plate with no declaration is one object (#6245). | `workflows/outputs.py`; spec `2026-10-13-declared-objects-design.md` |
| Layout | `Layout` → `LayoutPlate` → `Placed(piece_key, x, y, rot, recolour)`. Quarter turns only. | `workflows/models.py`; packer `workflows/arrange.py` |
| By-object check | Bounding boxes and `extruder_clearance_radius` (49 mm on the H2C). Bambu Studio reads `extruder_clearance_max_radius` (96 mm) and convex hulls. | `bambuddy/clearance.py`; `render/plate_profiles.py` `CLEARANCES` |
| Viewers | `components/Preview.tsx` (customizer, storefront, library, print history), `components/print/PlateScene.tsx` (print dialog), `components/print/ToolpathScene.tsx`. All view only. No selection. | three.js with `@react-three/fiber` |
| Remembered choices | `ModelPrintChoices`: printer, filament plan, nozzles, tier, process, print sequence, part settings. #6219 lists 16 gaps. | `library/settings_store.py:299`; `api/settings.py` `RememberedChoices` |
| Agent | `arrange`, `get_output_plates`, `get_plate`, `check_plate_fit`, `print_output`, `remember_model_print_choices`, and others. No tool edits a plate. | `agent/src/tools/arrange.ts`, `agent/src/tools/outputs.ts` |

## 3. Approaches considered

### 3.1 How the editor is built

- **A. One shared React viewer on three.js, edits as an operation list, the server
  writes the 3MF (chosen).** It grows from the viewers that exist. The browser sends a
  list of operations; the server applies them to a plate document and writes a new
  output. The same operations are the agent's tool input. Cost: the gizmos
  (move/rotate/scale, lay on face) are ours to build. `@react-three/drei` has
  `TransformControls` and `PivotControls`, which cover most of it.
- **B. Run Bambu Studio or OrcaSlicer in the browser (WebAssembly or a remote desktop).**
  Rejected. Bambu Studio's GUI is wxWidgets; it does not build for the web. A remote
  desktop gives Bambu Studio's UX, which is what the owner wants to beat.
- **C. Edit through the agent only, view-only UI.** Rejected. The owner asked for full
  editing by clicking (#6253).

### 3.2 What an edit changes

- **A. A plate document (JSON) is the source of truth; the 3MF is written from it
  (chosen).** The document holds objects, parts, transforms, plates, order, settings and
  groups, with stable ids. It extends `Layout`. A 3MF from outside (a library file, a
  Bambu Studio project) is read into a document first.
- **B. Edit the 3MF in place.** Rejected. Ids would be the 3MF's object numbers, which
  change on every write, and each operation would parse and rewrite XML.

### 3.3 Settings metadata

- **A. Generate a catalogue from Bambu Studio's source at the pinned tag, at build time
  (chosen).** Parse `PrintConfig.cpp` (`def = this->add(...)` blocks), `Tab.cpp`
  (pages, groups, order, wiki paths) and `Preset.cpp` (`s_Preset_print_options`). The
  owner asked for this on #2998 (2026-10-24 comment).
- **B. Ask the slicer binary.** Rejected. The CLI has no command that prints the option
  definitions.
- **C. Write the metadata by hand.** Rejected. It drifts from the slicer.

## 4. The viewer and editor component

### 4.1 One component, the host decides (owner, #6253)

One component, `PlateEditor` (frontend `components/plate/`), replaces the three viewers
over time. The host page passes `capabilities`. Every capability is off unless the host
turns it on. The component renders only the controls it has.

```ts
type Capabilities = {
  select?: 'objects' | 'parts' | 'both' | false
  multiSelect?: boolean
  transform?: { move?: boolean; rotate?: boolean; scale?: boolean; layOnFace?: boolean; dropToBed?: boolean; toPlate?: boolean }
  split?: { toObjects?: boolean; toParts?: boolean }
  merge?: boolean
  delete?: boolean
  duplicate?: boolean
  filament?: boolean            // change a part's filament slot
  recolour?: boolean            // storefront: pick a colour from the allowed list
  settings?: 'object' | 'part' | 'both' | false
  assemblies?: boolean
  order?: boolean               // reorder objects (print order for by object)
  layers?: boolean              // the cut-at-height slider that PlateScene has today
  toolpath?: boolean            // the G-code view that components/print/ToolpathScene.tsx has today
}
```

| Host | Capabilities |
|---|---|
| Print dialog, order item, Arrange (back office) | all |
| Customizer page (back office) | view, `select: 'both'`, `layers` |
| Storefront customize and order pages | view, `select: 'parts'`, `recolour` where the listing allows it |
| Print history, a customer's quote link, library file details | view and `select` only |

The server enforces the same split. A save goes through a route that checks the
caller's role (§4.5). The storefront edge exposes only the recolour route it has today.

### 4.2 Selection, as in Bambu Studio

Bambu Studio selects by instance (an object) or by volume (a part)
([`Selection.cpp` `set_mode`, `add_object`, `add_instance`, `add_volume`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/Selection.cpp#L191-L439)).
ScadBuddy does the same:

- Click selects an object. A second click on a selected object selects the part under
  the pointer. Alt-click selects a part directly. Shift-click adds or removes.
  A drag on empty space draws a box that selects the objects in it.
- An object list beside the viewport shows plates, objects, parts and groups, in print
  order. Selection in the list and in the viewport is one state.
- An object or part with its own settings shows a marker in the list and in the
  viewport, as Bambu Studio's settings icon does (#6253).
- The selection and the ids it names are what the agent tools take (§9).

### 4.3 Edits

Each edit is one operation on the plate document (§6). The browser applies it at once
for display and keeps an undo stack. Save sends the list.

| Operation | Bambu Studio counterpart |
|---|---|
| `move`, `rotate`, `scale` (uniform or per axis), with typed values and snapping | the Move, Rotate, Scale gizmos |
| `lay_on_face` (click a face) | the Lay on face gizmo (`src/slic3r/GUI/Gizmos/GLGizmoFlatten.cpp`) |
| `drop_to_bed` | the drop-to-bed action, done after every transform |
| `to_plate` | moving an object to another plate |
| `delete`, `duplicate` (n copies) | `ObjectList::del_subobject_item` ([`GUI_ObjectList.cpp#L2827`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/GUI_ObjectList.cpp#L2827)), clone |
| `split_to_objects`, `split_to_parts` (by connected shells) | `ObjectList::split` ([`#L3085`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/GUI_ObjectList.cpp#L3085)) |
| `merge` (objects into one object of parts) | `ObjectList::merge` ([`#L3142`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/GUI_ObjectList.cpp#L3142)) |
| `set_filament` (a part's slot) | the part's extruder column in the object list |
| `set_settings` / `clear_settings` (object or part, any keys) | "Add settings" in the object list ([`ObjectList::add_category_to_settings_from_selection`, `#L2133`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/GUI_ObjectList.cpp#L2133)) |
| `group`, `ungroup` (parts into an object, objects into an assembly) | the object list's assemblies (`ASSEMBLE_TAG` in [`bbs_3mf.cpp#L253`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Format/bbs_3mf.cpp#L253)) |
| `reorder` (print order) | dragging objects in the object list; the order is the print order for by object (§5.2) |
| `arrange` (this plate, or the selection, the rest pinned) | Arrange; pinning is #2330 |

### 4.4 Live checks

While a person drags, the viewer shows Bambu Studio's clearance halos and colours a
conflict red (#3772). The halos come from the same rules module as the server check
(§5): the rules are pure geometry, and the plan ports them to
TypeScript with one set of test vectors run by both sides (as `workers/rust/vectors`
does for the slicer). The server check is the one that counts.

### 4.5 Save and permissions

- Save posts the operation list to `POST /api/v1/plate-edits` with a `source`: an
  output id or a library file id. The server reads the source's plate document (a library
  file is read from its 3MF), replays the operations, runs the checks (warnings only),
  writes a new output (`kind='arranged'`), and returns its id. The source does not change
  (#6253).
- The route needs the back-office role that `POST /outputs/arrange` needs. Storefront
  recolour keeps its own route on the storefront edge. A capability the browser turns on
  does not open a route.
- An order item gets a new revision only when the edit starts from that order item (the
  host passes `order_item_id`). A save from any other page never changes an order, even
  when an order item points at the source output (orders spec
  `2026-10-18-orders-design.md`).

## 5. Arrange and validation, by Bambu Studio's rules

One module, `backend/scadbuddy/render/plate_rules.py`, holds the rules. Arrange, the print
check (`bambuddy/clearance.py`), the editor's save, and the TypeScript halos use it. Each
rule names its source. The rules are warnings in the dialog and the agent's result; the
slicer stays the judge, as `bambuddy/clearance.py` already says.

### 5.1 Printer data

`CLEARANCES` in `render/plate_profiles.py` grows to hold, for each printer, the values the
slicer reads: `extruder_clearance_max_radius`, `extruder_clearance_radius`,
`extruder_clearance_height_to_rod`, `extruder_clearance_height_to_lid`,
`extruder_clearance_dist_to_rod`, `nozzle_height`, `printable_height`, `bed_exclude_area`
and `wrapping_exclude_area`. They are generated from the machine profiles in the slicer
profile tree the image already ships (`slicing/profiles.py`, `/opt/slicer-profiles`), not
typed by hand. H2C values, from
[`Bambu Lab H2C 0.4 nozzle.json#L37-L50`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/resources/profiles/BBL/machine/Bambu%20Lab%20H2C%200.4%20nozzle.json#L37-L50):
max radius 96, radius 49, to rod 47.4, to lid 201, dist to rod 50; wrapping exclusion area
x 145–251, y 310–326
([`#L407-L412`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/resources/profiles/BBL/machine/Bambu%20Lab%20H2C%200.4%20nozzle.json#L407-L412)).

### 5.2 By object (owner, #6246)

All from `Print::sequential_print_clearance_valid`
([`Print.cpp#L597`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L597)).

1. **Hull clearance.** Each object's 2D convex hull grows by `obj_distance` with round
   joins; no two grown hulls may meet
   ([`#L676-L681`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L676-L681)).
   `obj_distance = 0.5 * extruder_clearance_max_radius - 0.1`, or
   `0.5 * MAX_OUTER_NOZZLE_RADIUS - 0.1` when every object is shorter than
   `nozzle_height`
   ([`#L634-L636`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L634-L636)).
   In a by-object print the skirt adds: `max(obj_distance + 0.5 * skirt, skirt)`
   ([`#L637-L643`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L637-L643)).
   So the skirt settings (`skirt_loops`, `skirt_distance`) are inputs to Arrange.
2. **Print order is the object-list order.** The sort by position is `#if 0`'d out
   ([`#L747`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L747));
   objects sort by `object_index`
   ([`#L840-L842`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L840-L842)).
   ScadBuddy writes the objects into the 3MF in the plate document's order.
3. **Rod and lid.** Each object's Y range grows by `extruder_clearance_dist_to_rod / 2`
   ([`#L881-L886`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L881-L886)).
   For object k in print order, the height limit is `printable_height` when k prints
   last, else `extruder_clearance_height_to_lid`
   ([`#L894`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L894)),
   and `extruder_clearance_height_to_rod` when any object printed after k overlaps its Y
   band
   ([`#L907-L917`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L907-L917)).
4. **Exclusion areas.** No object's hull (not grown) may meet `bed_exclude_area`
   ([`#L687-L695`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L687-L695)).
   `Print::validate` runs the by-object check or the by-layer check below, never both
   ([`#L1346-L1358`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L1346-L1358)).
   So in a by-object print the slicer does not check `wrapping_exclude_area`. ScadBuddy
   still warns for it in both modes, because the area is where the H2C's wrapping detection
   works (§11, assumption 11).
   In a by-layer print, no hull may meet `bed_exclude_area`, nor `wrapping_exclude_area`
   when wrapping detection is on
   ([`layered_print_cleareance_valid`, `#L944-L995`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L944-L995)).

Arrange's by-object goal applies the same numbers as Bambu Studio's own Arrange:
`min_obj_distance = max(min_obj_distance, cleareance_radius)` (that field is
`extruder_clearance_max_radius`), or `MAX_OUTER_NOZZLE_RADIUS` when all objects are short,
plus the skirt
([`Arrange.cpp#L91-L111`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Arrange.cpp#L91-L111);
[`ArrangeJob.cpp#L913-L915`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/Jobs/ArrangeJob.cpp#L913-L915)).
On top of that it:

- gives each object taller than the rod its own Y band among all objects printed after it;
- puts short objects first in the order, and the tallest last;
- keeps out of the exclusion areas;
- splits objects across plates when the rules need it;
- writes the order into the 3MF.

ORD-2026-2 is the acceptance case: 10 candles of 65.5–83.5 mm, flames and plug, base and
cradle, on the H2C, at most 5 tall candles a plate (#6246, owner's comment).

### 5.3 The prime tower

`Print::has_wipe_tower` turns the tower on when `enable_prime_tower` is true and any of:
wrapping detection is on with an exclusion area, timelapse is on, or more than one filament
is used and spiral mode is off
([`Print.cpp#L3229-L3241`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Print.cpp#L3229-L3241)).
So a one-colour by-object plate with timelapse still gets a tower unless the print sets
`enable_prime_tower` false. The tower's footprint, and Arrange reserving it, is #3770.
The rules module asks the tower model from #3770 whether the plate has a tower.

### 5.4 Where checks run

| Moment | Check | Result |
|---|---|---|
| Drag in the editor | TypeScript port of §5.2 and §5.3 | halos, red conflict |
| Save of edits, Arrange result, print or quote dialog | `plate_rules.py` | structured warnings with the object ids and the rule's source link |
| Slice | the slicer | a refusal comes back as a 422 that names the key or object (#2998) |

## 6. Multi-plate and object identity

### 6.1 The plate document

`Layout` grows into a plate document. Its new fields default so a stored `Layout` still
reads.

```text
PlateDocument
  plates[]:  { id, name, objects: [object id, ...] in print order, settings? }
  objects{}: { id, name, transform (3x4), parts: [part id ...], settings{}, group? }
  parts{}:   { id, mesh (blob pointer), volume type, filament slot, transform (3x4, relative), settings{}, provenance? }
  groups{}:  { id, name, objects[] }          # assemblies
  process_overrides{}                         # whole-print keys (#2998)
```

- Ids are short strings, stable across edits. A new object (duplicate, split) gets a new
  id; the others keep theirs. The 3MF carries them in `model_settings.config` object and
  part metadata (a `scadbuddy_id` key next to Bambu Studio's keys;
  [`bbs_3mf.cpp` `OBJECT_TAG`, `PART_TAG`, `METADATA_TAG`, `#L200-L253`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Format/bbs_3mf.cpp#L200-L253)).
- Each part's mesh is a blob of its own. A split or a merge writes new mesh blobs and
  new part ids. A save never re-derives meshes from the source 3MF, so replay is cheap
  and the browser, the rules and the writer read the same meshes. `provenance` (the
  `piece_key` and colour of a rendered part) is kept where it exists, for re-render.
- The document is stored as a blob beside the output (`{name, mediaType}` pointer, never
  inline bytes; repo rule #2479).
- `Placed.x, y, rot` become a full transform. Quarter turns stay what the packer makes.
- A 3MF that ScadBuddy did not write (a library file, a Bambu Studio project) is read into
  a document by `render/objects3mf.py`. Today it reads objects, parts and their extruders.
  It drops modifiers and support blockers and enforcers, and it merges the build items
  that place one object the same way up. Plan Task 3 extends it to keep volume types,
  instances and setting metadata.

### 6.2 A template plate is objects, not one object (#6245)

- With `echo(objects = N)` the template's declaration wins (spec
  `2026-10-13-declared-objects-design.md` §1).
- Without one, the writer splits each template plate into objects, for every print, not
  only by-object ones: object identity is what the editor, the settings and the agent
  name. The split finds the connected shells of each colour's mesh, then joins shells of
  any colour that touch or are closer than `GAP_MM` (5 mm, `workflows/packing.py`) into
  one object, so a letter's dot stays with its letter. The PR runs the template checks
  over `models/` to show what changes. This
  changes the declared-objects spec's "a template with no declaration writes exactly
  what it does today"; that spec rejected splitting to avoid the dot case, which the gap
  rule covers.
- The person can fix a wrong guess with `split_to_objects` and `merge` (§4.3).
- Until this ships, a by-object print of a plate with one multi-colour object warns
  (#6245's second option), so ORD-2026-2's 57-hour slice cannot happen silently.

## 7. Settings: whole print, per object, per part

### 7.1 One generic path (owner, #2998)

- Any process key, on any print, quote, item print, library-file print and batch step,
  through `process_overrides: {key: value}`. The named fields stay as shorthands.
- No key or value is refused. A check may warn, with Bambu Studio's type or range. The
  slicer's refusal returns as a 422 that names the key.
- #2998's PR ships the path, the list of resolved values and the agent tool
  `get_process_values`.

### 7.2 The settings catalogue (new)

A build step reads Bambu Studio's source at the pinned tag and writes
`settings-catalogue.json`. For each key: label, tooltip, type, unit (`sidetext`),
min/max, enum values and labels, `mode` (Simple, Advanced, Develop), page, group, order
and wiki path. Sources:

- option definitions: `PrintConfigDef::init_fff_params` in `PrintConfig.cpp`, for example
  [`print_sequence`, `#L1797`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/PrintConfig.cpp#L1797),
  [`wipe_tower_no_sparse_layers`, `#L5367`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/PrintConfig.cpp#L5367);
- pages, groups and wiki paths: `TabPrint::build` in `Tab.cpp`
  ([`#L3193`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/Tab.cpp#L3193)).
  This confirms the guess on #2998: the wiki path is the second argument of
  `append_single_option_line`, for example `"parameter/prime-tower"`
  ([`#L3477-L3478`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/Tab.cpp#L3477-L3478));
- which keys are process keys: `s_Preset_print_options` in `Preset.cpp`
  ([`#L1013`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/Preset.cpp#L1013)).

The settings list from #2998 gains labels, sections, help links and a "Develop mode"
badge from it. An unknown key still works.

### 7.3 Per object and per part (owner, #6253)

- Any key, on an object or a part, written to that object's or part's metadata in
  `model_settings.config`. `bambu3mf.py` `set_part_settings` already writes part
  metadata for a fixed list (#5571); it opens to any key.
- Bambu Studio offers, for a part, the region keys (`PrintRegionConfig`), and for an
  object, the region and object keys (`PrintObjectConfig`)
  ([`GUI_Factories.cpp` `SettingsFactory::get_options`, `#L118-L135`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/GUI_Factories.cpp#L118-L135)).
  The slicer builds an object's config from those keys only
  ([`PrintApply.cpp#L1719-L1728`](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/PrintApply.cpp#L1719-L1728)).
  So a whole-print key (for example `enable_prime_tower`) on an object likely has no
  effect. ScadBuddy writes it anyway and shows a warning: "Bambu Studio applies this
  key to the whole print only". The catalogue (§7.2) records each key's scope, so the
  warning needs no list of its own.
- Today's slot-keyed `part_settings` stays. It means "every part in this filament slot".
  An object's or part's own setting wins over it.
- The same settings editor serves the print, an object and a part. It opens on the
  selection.

### 7.4 Precedence

From lowest to highest: process preset → template `print_settings` → remembered
`process_overrides` → this print's `process_overrides` → slot `part_settings` → object
settings → part settings. The editor shows, for each key, the value in effect and which
level set it. In Bambu Studio's 3MF a part's metadata wins over its object's, so the
writer writes the merged slot-and-part values on each part and the object values on the
object; that keeps this order.

## 8. Remembered settings (#6219)

- Every choice in the print, batch-print and quote dialogs is remembered (owner, #6219).
  The 16 gaps in #6219's body are the work list. Colour match is #6218 / PR #6220: on by
  default, remembered per model or library file.
- Whole-print `process_overrides` are remembered with the model's choices (#2998).
- Object and part settings belong to the plate document of an output, not to the
  remembered choices: an object id means one output's object. A template's declared
  object k is the exception: settings set on declared object k are remembered per model
  and object number, as slot settings are per slot.
- A quote records its full print setup, edits included, so the print that follows slices
  the same file (#2998).

## 9. Agent tools at parity

The assistant names the same ids as the UI.

| Tool | Does | Routes |
|---|---|---|
| `get_plate_document` | plates, objects, parts, ids, transforms, filaments, settings, groups, order, and the current warnings | `GET /api/v1/outputs/{id}/plate-document`, `GET /api/v1/library/files/{id}/plate-document` |
| `edit_plate` | the operation list of §4.3, saved as a new output; returns its id and the warnings | `POST /api/v1/plate-edits` |
| `check_plate` | §5's rules for a document and print settings, without saving | `POST /api/v1/plate-rules/check` |
| `search_slicer_settings` | the catalogue (§7.2): by key, label, section; with the value in effect for a print | `GET /api/v1/print/settings-catalogue` |
| `arrange` (existing) | gains the by-object goal, `process_overrides`, and pinned objects | `POST /api/v1/outputs/arrange` |
| `get_process_values` (#2998) | resolved whole-print values | `POST /api/v1/print/process-values` |
| `print_output` and the print tools (existing) | take `process_overrides` (#2998) and print a saved document's output | existing |

`edit_plate` is a write that makes a new output and changes nothing outward, so it is not
a pending action. Printing stays one.

## 10. UX: where we beat Bambu Studio

1. **One searchable settings list for every level.** Search by key, label or section,
   "changed only", and "where is this set" (§7.4). Bambu Studio splits settings across
   tabs, hides Develop-mode keys, and adds per-object settings through a category menu.
2. **The rule says why.** A conflict names the objects, the rule in plain words, the
   numbers ("candle 3 is 83.5 mm; the rod clears 47.4 mm; candle 7 prints after it in the
   same Y band"), and a fix button ("reorder", "move to plate 3", "re-arrange"). Bambu
   Studio says "X is too tall, and collisions will be caused."
3. **Live checks as you drag** (#3772), with the server as the final judge.
4. **Undo and redo** for every edit, and a save that never destroys: each save is a new
   output, and history keeps the old one.
5. **Remembered by default.** The next print of the same model opens as the last one was.
6. **The assistant does the same work.** "Put the tall candles on their own plate and
   print by object" is one request.
7. **Fewer modes.** No Simple / Advanced / Develop switch. All keys are present; the
   catalogue's `mode` only sorts and badges them.
8. **Works in the Bambuddy frame and on a tablet.** Touch: tap selects, long-press adds
   to the selection, two fingers orbit. Phone width is view and select only.

## 11. Assumptions (calls made without an owner answer)

1. Rules are cited at the native slicer's pinned tag `v02.08.02.61`, not `master`.
2. The rules module is ported to TypeScript for live halos, with shared test vectors; the
   server result is the one that counts.
3. A template plate with no declaration splits by connected shells, with shells closer
   than 5 mm (`GAP_MM`) kept together (§6.2). Until then a by-object print of a
   one-object multi-colour plate warns.
4. A whole-print key set on an object or part is written and warned about, not refused.
5. Object and part settings live in the output's plate document, not in remembered
   choices, except for a template's declared object numbers (§8).
6. `edit_plate` is not a pending action: it makes a new output and sends nothing outward.
7. The settings catalogue is generated in the Docker build from the source at the pinned
   tag, and checked in CI when the pin moves.
8. The plate document is a blob beside the output, not a Postgres column (repo rule:
   bytes in the blob store).
9. The save route needs the same role as `POST /outputs/arrange`; the storefront keeps
   its recolour route only.
10. Phone width gets view and select only; editing needs a pointer or a tablet.
11. The wrapping exclusion area is warned about in by-object prints too, though the slicer
    checks it only by layer (§5.2 rule 4). The owner's #6246 comment lists it for Arrange.
12. Edits can start from a library file as well as an output; the route takes either
    source (§4.5).
13. An order item gets a new revision only from an edit started on that order item.
14. Part meshes are blobs of their own in the plate document (§6.1).
15. The template split applies to every print, by layer too (§6.2).
16. Milestone 1 (ORD-2026-2, no editor) ships before the editor (§12).

## 12. Milestones

1. **ORD-2026-2 prints by object from ScadBuddy.** The rules module, the print check and
   Arrange's by-object goal (#6246), the template split (#6245), `process_overrides`
   (#2998), and remembered choices (#6219). No editor yet.
2. **The editor in the back office.** The plate document, the operations, `PlateEditor`,
   transforms, object operations, per-object and per-part settings (#6253), the agent's
   plate tools.
3. **Everywhere else.** Halos (#3772), pinning (#2330), assemblies, the catalogue in the
   settings list, storefront and read-only hosts.

The epic is done when milestone 3 ships and the ORD-2026-2 end-to-end test passes.

## 13. Left to the builder

- Gizmo library choice inside `@react-three/drei`, keyboard shortcuts, snapping steps.
- How the object list renders large plates (virtualise past a few hundred rows).
- The id format and the `scadbuddy_id` metadata key name.
- The parser for `PrintConfig.cpp`: a regex pass over `def = this->add(...)` blocks is
  enough if its test fixes the count of keys at the pinned tag.

## 14. Related issues

Children are in epic #6272. Background, closed: #84 (print workflow epic), #836 (rack
nozzle choice), #956 (declared objects and the by-object pre-check), #4625 (machine rate),
#5571 (per-part settings, fixed list), #4673 (line-item versions). Related open epics:
#717 (filament profiles), #2182 (print as easily as a page). Related open issues: #1026
(colours from filaments), #385 (multi-plate acceptance).
