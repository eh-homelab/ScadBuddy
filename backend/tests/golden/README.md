# Golden fixtures

Two kinds of golden live here.

- `two_boxes/` — the 3MF writer's unit golden. Two boxes built in Python, no
  OpenSCAD involved, compared byte for byte by `tests/test_bambu3mf.py`.
- `name-keychain-*/` — integration goldens for the example models in `models/`.
  Each one is a real render through the production pipeline, recorded by
  `tests/test_golden_models.py`.

Regenerate the model goldens with `./regenerate.sh`; `two_boxes` regenerates
with `SCADBUDDY_UPDATE_GOLDEN=1 pytest tests/test_bambu3mf.py`.

## Why the model goldens are not bytes

`openscad/openscad:dev` is a rolling nightly. The Dockerfile asserts the exact
build (`OPENSCAD_VERSION`) so a swap cannot happen silently, but it does happen
— and when it does, the tessellation of `text()`, `offset()` and `circle()`
moves. Byte-comparing a rendered mesh would turn every such bump into a golden
regeneration with no way to tell a real regression from a retessellation.

So the meshes themselves are not stored. `3D/Objects/object_*.model` is 270 KB
and 640 KB for the Reagan keychain; what is recorded instead is a
`signature.json` of facts that survive retessellation, compared under three
rules:

| Section | Compared | Holds |
|---|---|---|
| `exact` | equality | part names, colours, extruder order, watertightness, connected-component count, Euler number, the archive's entry list, the preview's geometry names and material colours, the job's `colors` and `warnings` |
| `lengths_mm` | ±0.1 mm | every bounding box: the job's, each 3MF part's, each preview geometry's (Y-up, so it also pins the GLB axis flip) |
| `magnitudes` | ±1 % | each part's volume and surface area |

Vertex and triangle counts are deliberately absent: they are the thing that
drifts. Topology is pinned instead — `bodies` (the letters are one connected
piece, which the weld in the model exists to guarantee) and `euler_number`
(genus: the Reagan base is 0 because the keyring hole makes it a torus, the
`hole=false` base is 2 because it has no hole). Those are invariants of the
shape, not of how it was triangulated.

## The cover images are pinned by name, not by bytes

`Metadata/plate_1.png` and its three companions appear in `archive_entries`, so
losing one fails a golden. Their pixels do not: the renderer is a pure function
of the mesh, but a recorded PNG would turn every lighting or framing tweak into
a binary diff nobody can review, and the same retessellation that moves the mesh
moves the image. `tests/test_thumbnail.py` pins them as properties instead —
size, transparent background, and that a two-colour model really does show two
colours, which is the whole point of them.

## The two files that are stored verbatim

`3D/3dmodel.model` and `Metadata/model_settings.config` — the Bambu Studio
plumbing issue #42 asks for a diff of. Both are ~1 KB, both are written by this
repo rather than by OpenSCAD, and both are deterministic (the UUIDs are `uuid5`
of the model name; the zip timestamps are pinned).

They are compared after two normalisations:

- **`<metadata>` entries the golden does not carry are dropped.** Adding a
  metadata key is a forward-compatible change, so it does not have to land
  together with a golden regeneration. Changing or removing one still fails.
- **The build item's `transform` is rounded to whole millimetres.** The plate
  offset is computed from the mesh bounds, so its floats move with the
  tessellation. Position on the plate is unit-tested exactly in
  `tests/test_bambu3mf.py`; here the geometry is pinned by `lengths_mm`.

## Fonts

The keychain asks for `Lobster Two`. Without `fonts-lobstertwo` OpenSCAD
substitutes DejaVu Sans **silently** and every measurement here becomes
meaningless, so the test skips when `fc-list` does not report the family rather
than recording the wrong numbers. That is also why the goldens have to be
regenerated inside the image.
