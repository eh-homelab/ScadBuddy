# Mesh models: open a 3MF/STL as an editable scene (#2146)

Status: design, approved in conversation 2026-10-11; implementation plan to follow.
Related: #959 (mesh assets), #1252 (meshes into Bambuddy), #2330 (hand placement in
Arrange, shares the gizmos of §8).

## 1. Goal

Anything a user would do to a mesh in Bambu Studio's Prepare view, the assistant (and
the UI) can do in ScadBuddy: open a mesh, cut it, join the parts with a peg and socket,
reorient for strength, add or subtract shapes, recolour, and then render, save, arrange
and print it like any template.

The driving case is Bambuddy library file #688, `2 Carrot Garden Print by Object.3mf`:
the leaf tuft snaps at the neck because layer lines run across it. The fix is to cut
the tuft off at the neck, give the body a keyed socket and the tuft a peg, and print the
tuft lying flat. The same need came up for `Articulated Pumpkin for AMS attached
300percent.3mf` (a stem).

Out of scope: hand placement on Arrange's plates (#2330), writing to the Bambuddy
library (nothing is uploaded or overwritten there unless the user sends an output, as
today), mesh repair beyond what reading already refuses, sculpting or vertex edits.

## 2. Measured facts this rests on

- **OpenSCAD imports a 3MF and cuts it.** Measured 2026-10-11 in `scadbuddy:ci-test`
  (OpenSCAD 2026.09.23, `--backend=Manifold`): a two-object 3MF written by OpenSCAD
  (`color("red") cube(10)` and a blue sphere), then `difference(){ import("src.3mf");
  translate([0,0,5]) cube(100); }` exported to 3MF with `Status: NoError`, 264 facets;
  the same with `import("src.stl")`. The imported file's `basematerials` colours reach
  the output (`#FF0000`, `#0000FF` bases present). That image is an older nightly than
  the pinned `dev.2026-10-05` (2026.10.05); `backend/tests/test_mesh_models_openscad.py`
  re-measured it on the pinned image on 2026-10-12 (a colourless core 3MF in `color()`, see
  spec 2026-09-22 §3).
- **Library 3MFs are already read faithfully.** `render/objects3mf.py` `read_objects`
  returns each object as closed meshes per colour (`ReadObject.parts`, `ColourPart`),
  with Bambu Studio per-part extruders, `basematerials` colours, and Bambu `paint_color`
  painting carried as `ColourPart.paint` (#1965). It refuses sliced files, PrusaSlicer
  painting and painted-and-mirrored parts, carries a negative part as
  `ColourPart.negative` (#2337; a mesh model cuts it out of the object's printed parts),
  and bounds work by `ReadBudget`.
- **File 688 is painted.** `render/paint.py` `MAX_CODE_DIGITS`'s comment: a two-colour
  Bambu Studio project, 13,873 painted faces. So paint is part of the acceptance case,
  not an edge case (§7).
- **The writer already writes paint.** `render/bambu3mf.py` writes each part's
  `paint_color` codes (`part.paint.codes`) per face.
- **Binary files in a model directory reach the render worker.** The revision snapshot
  is a git export packed by `store/archive.py` `pack_dir` (every regular file but
  dotfiles and symlinks), and `render/provenance.py` `source_version` hashes every file
  in the directory but the sidecars (its comment names a swapped STL). What is missing is
  a write path: `PUT /models/{slug}/files/{name}` accepts only `*.scad` text
  (`api/model_files.py`).
- **Library files are already fetched and split into pieces once per hash.**
  `bambuddy/library_objects.py` reads a library file through `LibrarySource.fetch_3mf`
  (an STL wrapped by `bambu3mf.stl_3mf`), writes each object as a blob piece keyed by
  the file's sha256 and the object's index, and keeps the object list under
  `cache/library-objects/`. Downloads are capped at `MAX_DOWNLOAD_BYTES`
  (`bambuddy/print_source.py`, equal to `bambu3mf.MAX_UNCOMPRESSED_BYTES`, 256 MiB).

## 3. Representation

A mesh model is an ordinary model directory with three extra things:

```
models/<slug>/
  model.json            # as today, plus "mesh_source" (the provenance; kept after an eject)
  model.scad            # GENERATED from scene.json; committed
  scene.json            # the source of truth
  meshes/<object>-<part>.3mf   # one closed mesh per (object, colour part); never edited
  meshes/<object>-<part>.paint.json   # that part's paint codes, when painted (§7)
```

A model is a mesh model while it has a `scene.json` (`meshmodel/operations.py`
`read_scene`); `model.json` carries no `scene` flag, only `mesh_source`
(`library/catalogue.py` `ModelMeta`).

`model.scad` is compiled from `scene.json` by a pure function, so the render, version,
Arrange and print paths see a plain `.scad` template and need no change. The agent and
the UI both edit `scene.json`, only through `scene_edit` (§5).

### 3.1 `scene.json` (version 1)

```json
{
  "version": 1,
  "generated_sha256": "…",
  "source": {"kind": "bambuddy_library", "file_id": 688,
             "name": "2 Carrot Garden Print by Object.3mf", "sha256": "…"},
  "objects": [
    {"id": "o1", "name": "Carrot 1",
     "parts": [{"id": "p1", "mesh": "meshes/o1-p1.3mf", "colour": "#F08A24", "painted": true}],
     "ops": [],
     "transform": {"translate": [0, 0, 0], "rotate": [0, 0, 0], "scale": [1, 1, 1]}}
  ]
}
```

- `source.kind` is `bambuddy_library`, `upload` or `output` (with `output_id`).
- An object is its parts (each a mesh and its colour), then its `ops` in order, in the
  mesh's own frame, then its `transform` (rotate in degrees, X then Y then Z, as
  OpenSCAD's `rotate([x,y,z])`).
- Object and part ids are stable across edits; a new object gets the next free id.
- Caps (`meshmodel/scene.py`): 200 objects, 64 parts and 100 ops an object, 16 cuts an
  object (`MAX_CUTS`: `section.cut_points` is cubic in them), a name of 200 characters.

### 3.2 Ops

| op | fields | compiles to |
|---|---|---|
| `cut` | `point`, `normal`, `keep: above\|below` | `intersection()` with a half-space: a cube larger than the object's bounding box, aligned to the plane |
| `joint` | `at: [x,y,z]`, `axis`, `diameter`, `depth`, `clearance`, `key: none\|flat\|hex`, `role: peg\|socket` | peg: `union()` with the keyed cylinder; socket: `difference()` with it grown by `clearance` radially and at the bottom |
| `add` | `primitive: cube\|cylinder\|sphere`, `size`, `transform`, `colour?` | the shape is first `difference()`d from every other piece, then added as a piece in `colour` (or the object's first printed part's), so no volume has two colours |
| `subtract` | as `add` | `difference()` |
| `custom` | `scad` (text) | the snippet, given the geometry so far as `children()` |

An op's numeric field may be written `{"expose": "name", "value": v}`: the compiler
emits a customizer parameter `name = v;` (with the op's range as its annotation) and uses
it, so customizing a mesh model works like any template (e.g. `cut_height`,
`clearance`). An exposed name must be unique and a valid identifier.

### 3.3 The compiler

`scadbuddy/meshmodel/compile.py`: `compile_scene(scene) -> str`, pure and
deterministic. Output: a header line `// Generated from scene.json by ScadBuddy; edit
the scene, or eject (scene_eject) to edit this file`, the exposed parameters, one
`module <id>() { … }` per object, and at the end each object placed with
`multmatrix(<transform>)`. Each part is `color("<colour>") import("meshes/…3mf");`.
A part's mesh file is written with no `basematerials` and no colour of its own (one
object, one mesh): OpenSCAD keeps the innermost colour (§2 measured an imported 3MF's
`basematerials` reaching the output), so a mesh that carried one would ignore the outer
`color()` and `set_colour`. With the colour only in `color()`, `render/solids.py`'s
per-colour re-render (which shadows `color`) still yields one closed solid per colour.

An object compiles per piece (`compile.py` `_modules`, `_apply`): one `color(c)
<statement>;` per colour piece, starting with one per printed part. An op that removes
geometry (a cut's half-space, a socket, a `subtract`, the negative parts) wraps every
piece; a `custom` op is given each piece on its own as its `children()`; an op that adds
geometry (a peg, an `add`) appends a piece, and an `add` is first cut out of every other
piece. So every cutter sits inside its piece's `color()`: in OpenSCAD 2026.10.05's
Manifold 3MF a face takes the colour of the solid that made it, and a cutter outside
every `color()` made the cut faces the default material (measured in the image,
`backend/tests/test_mesh_models_openscad.py`). The geometry is that of wrapping the
union, since (A ∪ B) − C = (A − C) ∪ (B − C). The compiled `model.scad` is capped at
8 MiB (`meshmodel/operations.py` `MAX_SOURCE_BYTES`; a 422).

## 4. Opening a mesh (`model_from_mesh`)

An `OperationKind` on the `library` queue (§6), exported with `model_kinds`.

Sources:

- **Bambuddy library file** (`library_file_id`): `bambuddy/library_objects.py`'s read
  (once per sha256; the same refusals Arrange gives: sliced, past the cap, unreadable),
  so a file Arrange has listed is neither downloaded nor parsed again.
- **Upload**: `POST /api/v1/models/from-mesh`, multipart, streamed into the claim store
  with `ClaimStore.hold_file` as media uploads are; `.3mf` and `.stl` only (the formats
  `read_objects` and `bambu3mf.stl_3mf` read); at most `MAX_DOWNLOAD_BYTES`. An STL
  becomes one object of one part in the default colour. OBJ is out of phase 1: nothing
  reads it today, and its groups and `.mtl` materials need rules of their own (#959).
- **Output** (`output_id`): the output's own 3MF on the volume.

Run: read the objects (`read_objects` under the operator's read budget, `budget_of`, as
Arrange reads; a file past the scene's caps, 200 objects with copies counted or 64 parts
an object, is a 422 naming the cap, and a longer name is cut to 200 characters), write each part as
`meshes/<object>-<part>.3mf` (and its `.paint.json` when painted) in the frame
`read_objects` gives it, and build `scene.json`: an object read with `count` copies
becomes that many scene objects sharing its meshes, and the objects start in a row
along X with 5 mm gaps, their printed parts resting on Z=0 (a negative part may reach
below the bed). Then compile `model.scad`, write `model.json` with `mesh_source`, the
provenance (source kind, id, name, sha256, and the licence when the source states one),
one git commit. A painted part's paint is kept beside its mesh but not carried into
renders or prints until §7 (phase 3); its object's note says so. `meshes/` as a whole is held to the same
cap. Nothing is written to Bambuddy.

`meshes/` is the only place binary files are written; `PUT …/files/{name}` stays
`.scad`-only.

## 5. Editing (`scene_edit`, `scene_eject`)

`scene_edit` is one `OperationKind` taking a list of **edits**; all apply or none do,
in one git commit (so undo is the existing version history and restore).

Check: the model has a scene; the sha256 of `model.scad` equals the scene's
`generated_sha256` (else 409 "model.scad was edited by hand; eject the scene to keep
editing it as source"). The hash is of the text the compiler last wrote, recorded in
`scene.json` with every write, never a fresh `compile_scene` of the stored scene: a
later release whose compiler emits different text must not read every existing mesh
model as hand-edited. Each edit recompiles with the current compiler and records the
new hash; and each edit validates against the scene (ids exist, numbers in range).

Edits (what callers send; they expand into ops):

| edit | effect |
|---|---|
| `transform {object, translate?, rotate?, scale?}` | sets the object's transform |
| `split_at_plane {object, point, normal, joint?}` | the plane is in world coordinates. With `M` the transform's linear part (object frame to world) and `t` its translation, the point maps as `M⁻¹(p − t)` and the normal as `Mᵀn`, renormalised (a plane's normal transforms by the inverse-transpose, which matters once the scale is non-uniform). The object becomes two (`<id>` keeps `cut keep:below`, a new one `cut keep:above`), both keeping the parent's ops. With `joint {diameter?, depth, clearance, key, peg_on: above\|below}`: the section at the plane is measured with trimesh at edit time; the peg sits at the section's centroid and the diameter defaults to `1.2 * room` (60 % of the circle about the centroid), `room` being the distance from the centroid to the section's nearest edge; refused unless `room >= diameter / 2 + clearance + 0.8` mm (two 0.4 mm walls), or for a hex key `room >= (diameter / 2 + clearance) / cos 30° + 0.8`, its corners standing past its flats (`edits.py`), and refused for a ring (the centroid outside the section) and past 16 cuts. The measured position and size are stored in the ops, so compiling stays pure |
| `lay_flat {object, normal \| "largest_flat"}` | `normal` is a world-space direction (the viewer sends the normal of the face clicked on the preview; no face index, since the preview is the rendered mesh after ops and its triangles change with each render). The object is rotated so that direction points to −Z, the rotation written into the transform and the object dropped to Z=0. `"largest_flat"` picks the largest coplanar facet of the object's part meshes after its ops' cuts (trimesh `facets`), which is what a cut face usually is |
| `add`, `subtract {object, primitive, size, transform}` | appends the op; `add` takes an optional `colour` (above) |
| `custom {object, scad}` | appends the op |
| `set_colour {object, part, colour}` | a part's colour |
| `delete`, `duplicate`, `rename {object, …}` | as named |
| `remove_op {object, op}` | removes the op at that index |
| `expose {object, op, field, name}` | makes an op field a customizer parameter; `unexpose` reverses it |

Joints stay whole (review of #2388): a peg takes the colour of the part the cut goes
through most, so it prints with the piece it stands on; a coloured `add` is not a part,
so its colour never colours a peg, even where it fills most of the section. A parameter may not move a cut
that carries a joint, size a joint, or scale a jointed half, and `transform` may not
change a jointed half's scale: the other half's cut and the joint's root are numbers of
their own, so one side would move without the other, and a joint's size would skip the
fit check. Scale before splitting. `lay_flat`'s drop to the bed counts what ops add (a
peg, an `add`), and `largest_flat` never picks a cut face carrying a peg, which would
put the peg into the bed. The peg's fit is checked once, when the split is made: a later
edit to an earlier op that changes the section there (an `add` across the plane, a
parameter on an earlier cut) is not checked again, and the render is the check.

Exposed parameters and presets: an exposed name belongs to the op, not to the object's
id or name, so `rename` and `transform` never change it. An edit that removes one
(`unexpose`, deleting its op or object) is refused (409) while a preset names it, since a
render naming an unknown parameter fails (`render/runner.py` `UnknownParameterError`).
A saved preset (`saved_presets`) is released by `drop_from_presets: true`, which removes
that key from it; the model's own `model.json` presets are read-only and are never
dropped, so that 409 stands until they are edited (`operations.py` `edit_run`). Outputs keep the parameters they were rendered with, at their own revision.

Measuring (sections, inscribed circles, faces) runs in `asyncio.to_thread` under a
`ReadBudget`, with the kind's `run_timeout` sized for the cap: 10 minutes, a library pin's
(`PIN_TIMEOUT`), under the 15 minutes the browser and the agent follow a 202. The commit
runs to its end (`operations.kinds.to_thread_to_end`), so a run past its timeout whose
commit lands still announces it.

`scene_eject`: deletes `scene.json` and clears `scene` in `model.json`, one commit. The
model is then an ordinary template whose source imports `meshes/`.

## 6. Temporal, operations, flows and pipelines

No new workflow type and no new queue.

- `model_from_mesh`, `scene_edit` and `scene_eject` are `OperationKind`s with
  `queue="library"`, run by `OperationWorkflow` (check, insert, run, finish), exported
  from `library/operations.py` through `model_kinds`. The library worker runs in the API
  process, which holds the data volume and the Bambuddy client. Routes run them through
  `api/operations.py` `run_operation` (`Idempotency-Key`, 202 past the deadline,
  `GET /operations/{id}`), so the browser's and the agent's `command()` work unchanged.
  Uploads go by claim check (`operations/claims.py`).
- Rendering is the existing `TemplatePipeline`: a mesh model is a template whose saved
  revision changed; the snapshot carries `meshes/` to the render worker.
- Paint (§7) is a pipeline step, the only render-time difference.
- Agent tools (`agent/src/tools/`, one `defineTool` each, with `coverage.ts` entries):
  `create_model_from_mesh`, `get_scene`, `edit_scene` (a list of edits), `eject_scene`;
  `analyze_geometry` gains `weak_joints` (§9). All are local library writes: no
  Bambuddy write, so none is `outward`. Flows reach them through `tool(name, args)`
  (#2257), durable sessions as `agent-tools` activities, with no new host function.
- The plugin's `authoring` skill gains a mesh-model section (open, inspect, edit loop),
  citing this spec.

## 7. Paint

OpenSCAD drops painting: its output triangles carry only material indices. A painted
part's paint is carried over after the render, by a step in the default pipeline that
runs only when the model has a scene with a painted part:

1. Read the source part's mesh and `.paint.json` (codes per source face), and the
   object's ops and transform, from the revision.
2. For each output triangle of that part's colour solid, transform its centroid back
   into the source mesh's frame and find the nearest source face (trimesh proximity).
   - The output triangle is the same triangle (all three vertices within 1e-4 mm of the
     source face's): it takes the source code verbatim, so painting finer than the
     triangle survives.
   - Otherwise (a cut face, a retriangulated face, an added primitive): if the nearest
     source face is within 0.01 mm and its code is one colour, it takes that colour; else
     the part's own colour (code empty).
3. The codes become the part's `Paint`, which the 3MF writer already writes.

The step is an activity on the render queue (`pipeline_activities.py`), run between the
pieces and `pack`. Its fidelity is measured in §10 (share of painted faces carried
verbatim on an op-free and on a cut object). Where it cannot carry paint (a part mirrored
by a negative scale) `get_scene` and the edit answer warn, and the part renders in its
own colour.

## 8. UI (phase 4)

A mesh model's Customize page shows a scene panel beside the preview: objects and parts
(rename, colour, delete, duplicate), each object's ops (remove one, edit its fields),
and gizmos on the preview:

- move, rotate, scale (three.js `TransformControls`);
- a cut plane, dragged and rotated, with a joint toggle and its fields;
- lay on face: click a face.

While dragging, the preview draws each part's mesh (GLB from `meshes/`, served by the
template-file route) with the edited transform on the client; letting go sends one
`scene_edit`, and the render confirms. The gizmo components live in
`frontend/src/components/scene/` so #2330's plate view uses them. The generated form
still shows exposed parameters.

## 9. Weak joints (`analyze_geometry`)

`render/geometry.py` gains `weak_joints`: for each part, sections at every layer height
(0.2 mm default) along print Z; a section whose area is below 25 % of the larger of the
sections 2 mm below and above, with at least 1 cm³ of the part's volume above it, is reported
with its Z, area, and a suggested `split_at_plane` (the plane, and a joint sized to the
section). The agent's carrot loop: `analyze_geometry` → `edit_scene` with the suggested
split → `lay_flat` the tuft → render → `analyze_geometry` again.

## 10. Testing

- Unit: `compile_scene` golden files per op; every edit, including world-to-object plane
  mapping under rotation and non-uniform scale (the normal by `Mᵀ`); `lay_flat` by
  normal and `largest_flat` after a cut; the preset refusal and `drop_from_presets`;
  peg sizing and its refusal; `expose` naming; the hand-edit 409 (by `generated_sha256`), and no 409 when only the compiler's output
  changed.
- Paint transfer: on a synthetic painted mesh, an op-free object keeps 100 % of its
  codes verbatim; a cut object keeps every uncut face's code verbatim and gives cut faces
  the part's own colour.
- API (`requires_postgres`, `requires_temporal`): `model_from_mesh` from an upload and
  from a mocked library file (the recorded Bambuddy client); `scene_edit` and
  `scene_eject` through `run_operation`; claim release.
- `requires_openscad` (the image): a 3MF made with OpenSCAD is opened, split with a
  keyed joint, rendered; both parts are watertight, the peg's diameter is the socket's
  less `2*clearance`, and colours survive; a `set_colour` edit changes the rendered
  part's colour (the mesh files carry none, §3.3). This re-measures §2's first fact on the
  pinned image.
- Bundled template `models/mesh-joint-demo/` with `verify.sh`: a small two-colour mesh
  made with OpenSCAD in the verify run (never a user's library file), split, jointed,
  laid flat, rendered.
- Agent: tool tests for the four tools; an eval scenario "the leaves of this carrot
  snap off; fix it" on the demo mesh, scored on the edits made and the render.
- Frontend (phase 4): vitest for the scene panel and edit dispatch; a Playwright e2e on
  msw for a drag that sends one `scene_edit`.

## 11. Phases

1. Open as editable model: `meshes/` write path, `scene.json`, compiler (cut, joint,
   add, subtract, custom), `model_from_mesh`, `create_model_from_mesh`, `get_scene`.
   Acceptance: a library 3MF opens and renders the same geometry and colours (paint by
   phase 3).
2. Editing: `scene_edit` with every edit of §5, `scene_eject`, `edit_scene`,
   `eject_scene`, the demo template. Acceptance: the carrot split, jointed and laid flat
   by the agent, saved and printed.
3. Paint transfer (§7) and `weak_joints` (§9).
4. Scene editor UI (§8).

Each phase is its own PR; the plan covers phases 1 and 2 first.
