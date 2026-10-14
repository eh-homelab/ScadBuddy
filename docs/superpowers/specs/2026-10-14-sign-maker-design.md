# Sign Maker: a template maker with Make My Sign parity (#3468)

Status: design. The user approved the direction in conversation on 2026-10-14 and asked
for the spec to land as its own PR, followed by an implementation plan.
Related: spec 2026-09-27 (template-owned UIs and pipelines), spec 2026-10-11 (mesh
models, whose scene → generated `model.scad` pattern this follows), #2146.

## 1. Goal

MakerWorld's MakerLab **Make My Sign** (<https://makerworld.com/en/makerlab/makeMySign>)
is a layered 2D editor for anything flat: name plates, badges, door hangers, coasters.
It offers text, emoji/icons, shapes, SVGs, a vectorized image, holes, a base plate with
a border, per-element thickness and a facedown mode. ScadBuddy gets the same editor
with full feature parity. ScadBuddy's version is a **template maker**:

- **The editor is the built-in template `sign-maker`.** It has a page-slot UI, and its
  preview and Generate work like any template's.
- **"Save as template" turns the current sign into a new, ordinary template**
  (preset → template). The new template has no fork or upstream link. It holds
  `sign.json`, a generated `model.scad`, its layer SVGs and its fonts. Its texts,
  colours, heights and plate size are customizer parameters, so it is customised
  like any template. It also opens back in the Sign Maker for layout edits, and it can
  be ejected to plain source as a mesh model can.
- **A Make My Sign project imports 1:1.** The driving case is the Mahjong coaster,
  <https://makerworld.com/en/models/2573411> ("This model is created by Make My Sign").
  Importing it gives a template that renders the same sign and is geared to that
  design: its parameters are the sign's own.
- **The geometry work runs server-side, in TypeScript.** This replaces MakerWorld's
  in-browser processing: importing, normalising and compiling a sign are Temporal
  activities served by a TypeScript worker, so MakerWorld's logic ports with the
  least translation (§6). The browser editor reuses the same TypeScript modules for
  its instant 2D view.

The rest of MakerLab was surveyed so that the template-maker contract defined here
(§11) also fits the tools that come after this one.

Out of scope:
- Laser output: SVG/DXF cut files.
- MakerLab's AI generators, which need hosted models (§11).
- Writing back to MakerWorld.
- Publishing to MakerWorld.

## 2. Measured facts this rests on

### 2.1 OpenSCAD, in `openscad/openscad:dev.2026-10-12` (the pinned base, `--backend=Manifold`)

Measured 2026-10-14 from the session's scratchpad. The verify scripts (§12) re-measure
each fact on the pinned image.

- **`import()` of an SVG by element id works without experimental flags.**
  `linear_extrude(1) import("a.svg", id="p2")` rendered only that path: 12 triangles,
  one box. The layer files in §5 can therefore be one SVG per layer, or one SVG with
  ids.
- **Reading JSON needs an experimental flag.** `d = import("d.json")` with no flag
  prints `WARNING: Experimental builtin function 'import' is not enabled` and leaves
  `d` undef. With `--enable=import-function` it printed `ECHO: 99, "MAHJONG", 1`.
  ScadBuddy's runner passes no `--enable` flags, and adding a global experimental
  flag would change every render. **So the compiler emits plain `.scad` and never
  reads JSON at render time.**
- **A template can ship its own font.** With `use <Lobster-Regular.ttf>` beside the
  source, `text("Mahjong", size=10, font="Lobster")` rendered with X from 0.93 to
  48.36 mm. Without the `use`, the same text silently fell back to DejaVu Sans (X from
  1.14 to 52.38 mm), with no warning. So a generated template carries its fonts as
  sibling files and `use`s each one (§5.3). This agrees with CLAUDE.md's verified fact
  that a missing family silently falls back.
- **Binary files in a model directory reach the render worker** (spec 2026-10-11 §2,
  `store/archive.py` `pack_dir`). Layer SVGs and fonts need no new transport.

### 2.2 Temporal

- **Standalone activities work on the pinned dev server, not in production.** The
  pinned `temporalio` 1.34.0 (`backend/uv.lock`) has `Client.start_activity` /
  `Client.execute_activity`: an activity started from a client with no workflow. On the
  CLI that the image pins (1.9.1, Server 1.32.0), with default settings, a Python worker
  answered `execute_activity("echo", "hi", task_queue=…)` with `echo:hi`. Production
  runs **Server 1.28.4** (eh-homelab/clusters
  `applications/scadbuddy/temporal-cluster.yaml`), the newest line the operator
  supports (`>= 1.14.0 < 1.29.0`, quoted there). So standalone activities are not
  available in production yet (§6.3).
- **Cross-language activities from a workflow need nothing new.** A Python workflow
  can schedule an activity by name on another task queue, and a TypeScript worker on
  that queue serves it. The agent already runs a TypeScript Temporal worker
  (`agent/src/temporal/worker.ts`, `@temporalio/*` 1.24.0).

### 2.3 Make My Sign, measured in the logged-in editor and its shipped bundle

The full reverse-engineered format and feature inventory is Appendix A. These are the
facts the design depends on:

- **A project is one JSON document.** Every autosave sends `PUT
  /api/v1/creation-service/my/make/<id>` with `{"content": "<JSON string>"}`. The
  content is `{data: [element…], thickness: {backdropHeight, textHeight, shapeHeight,
  svgHeight, holeHeight}, globalStatus: {flatMode, nonePlateMode}, date, version:
  "NamePlate V1.1"}`. Elements are Fabric.js 5.3.0 object JSON wrapped by type:
  `BackdropElement`, `TextElement`, `BaseShapeElement`, `IconElement`,
  `SvgImageElement`, `SvgShapeElement`, `VectorizeElement` and `BaseHoleElement`.
  `HookElement` is legacy: the editor drops it when loading. Appendix A has every field.
- **The editor stack, from the bundle:**
  - Fabric.js 5.3.0 for the canvas;
  - paper.js for SVG import and booleans;
  - Clipper2 (WebAssembly) for offsets;
  - HarfBuzz (WebAssembly) for text shaping and glyph outlines;
  - three.js (SVGLoader, a patched ExtrudeGeometry);
  - manifold-3d (WebAssembly) for triangulation and hole cuts;
  - fflate for the 3MF.

  All of the processing runs in the browser.
- **Text and emoji are glyph outlines shaped by HarfBuzz,** from fonts on a public CDN
  (`public-cdn.bblmw.com/content-generator/fonts/<file>`, 19 families, Appendix A.3).
  Emoji are monochrome glyphs of `NotoEmoji-Bold.ttf`. OpenSCAD's `text()` also shapes
  with HarfBuzz and FreeType. So a `text()` with the same font file reproduces both,
  and both stay live parameters (§5.3).
- **The owner can read a project back with `GET /api/v1/creation-service/my/make/<id>`.**
  It returns `{…, content, assets: {svgAssets: [{name, url, objectName}]}, instanceId}`.
- **A vectorized image's geometry is not in `content`.** It is a public SVG at
  `assets.svgAssets[].url`, fetched without authentication. The Mahjong make's SVG has
  129 filled `<path>`s in a 1080×1080 viewBox. `vectorizeContent.pathResults[i]` maps
  to the i-th path, and `colorPathMap` gives `{colour: [path indices]}`.
- **Local files stay in the browser.** Locally imported SVGs, custom SVG plate shapes
  and locally imported fonts live only in that browser's IndexedDB. The content holds
  a key for each, and the editor never uploads them. An import gets them from
  `make.assets.svgAssets` (`.svg` and `.font` entries, which the editor downloads into
  that store when it loads a make), or from an exported 3MF (below). Without either,
  it must ask the user for the file.
- **Cloning a published design:** the model page's Customize button sends `POST
  /api/v1/creation-service/my/createfromshare/<instanceId>`, which copies the design
  into a new make on the server. No public read of a design's content was found.
- **The exported 3MF carries the project itself.** Download → "Download 3mf" with
  "Include customizable file in 3mf (including SVG)" checked (the default) wrote
  `Auxiliaries/Templates/template.json`, which is the same NamePlate V1.1 content, and
  `Auxiliaries/Templates/assets/<keyName>.svg` (the vectorized image), beside the
  meshes. Measured on make 85233397, 2026-10-14. That export:
  - is a Bambu Studio project with `Application: BambuStudio-01.07.04.52`,
    `MakerLab: Make My Sign` and `MakerLabVersion: NamePlate V1.1` metadata;
  - has one object of 31 parts, one per island of each colour, named `Backdrop-<n>`,
    `Border-<n>` and `<element>-<n>`, each with the extruder of its colour;
  - lists filaments in the order plate, border, then each element colour
    (`filament_colour` `#FFFFFF, #000000, #050504, #E7232C, #34A54F, #1860AE`);
  - is set up for the A1 mini (`printer_model`), `0.20mm Standard @BBL A1M`,
    `ironing_type: top`.

  It also measures the geometry rules:
  - **Scale:** 5 px = 1 mm (the 495 px circle is 99.0 mm, X and Y ±49.5).
  - **Facedown inlays from the plate's top face:** the plate spans z 0–3
    (`backdropHeight` 3); the border is an inlay 0.4 mm deep (z 2.6–3.0) inside the
    outline (offset 0); the vector parts span z 2.0–3.0, their element's
    `data.depth` 1, not `svgHeight`.
  - **The build item is flipped for facedown printing:**
    `transform="-1 0 0 0 1 0 0 0 -1 90 70 3"`, X mirrored and Z inverted, so the face
    is on the bed.
- **The editor autosaves on every change and again on unload.** It also caches the
  project in localStorage under `<uid>-<makeId>-content_gen.nameplate: saved data`,
  and that cache wins over the server copy when the page loads. Measured by accident
  on 2026-10-14: a stray text element added to make 85233397 survived a server-side
  restore until the cache was rewritten. An importer must never drive the editor.

## 3. What gets built

| Piece | Where | Language |
|---|---|---|
| Sign document `sign.json` v1, its schema and validation | `signs/src/doc/` | TS (schema exported as JSON Schema for Python) |
| MakerWorld importer (NamePlate V1.1 → `sign.json`) | `signs/src/import/` | TS |
| Normaliser (shapes, emoji, SVG, vectorize → layer SVGs in mm; text metrics) | `signs/src/normalise/` | TS |
| Compiler (`sign.json` → `model.scad` + `layers/` + `fonts/` manifest) | `signs/src/compile/` | TS |
| The `signs` Temporal worker (activities over the three above) | `signs/src/worker.ts`, Dockerfile target `signs` | TS |
| The Sign Maker template (`models/sign-maker`: UI, pipeline, `model.scad` shim) | `models/sign-maker/` | JS (bundled from `signs/src/editor/`), Python pipeline |
| Operations: create a template from a sign, edit, eject, import | `backend/scadbuddy/signs/` | Python |
| `ctx.render(..., files=)` overlay and `ctx.service(...)` (§7.2) | `backend/scadbuddy/workflows/` | Python |
| Agent tools | `agent/src/tools/signs.ts` | TS |

## 4. The sign document (`sign.json`, version 1)

The source of truth for a sign, as `scene.json` is for a mesh model. It is a superset of
NamePlate V1.1 in ScadBuddy's own terms: **millimetres, not canvas pixels; colours as
hex; every element with its own height range**. Everything an imported project had is
kept, so a round trip back to NamePlate V1.1 would be possible (it is not built, §1).

The example is the Mahjong coaster as imported: its plate, border and vector art,
with the depths §2.3 measured in facedown. A text and a hole are added to show those
types; the coaster itself has neither.

```json
{
  "version": 1,
  "generated_sha256": "…",
  "source": {"kind": "makerworld_make", "make_id": 85233397, "instance_id": 2837289,
             "model_url": "https://makerworld.com/en/models/2573411", "format": "NamePlate V1.1"},
  "units": {"display": "mm"},
  "bed": {"printer": "A1 mini", "w": 180, "h": 180},
  "facedown": true,
  "thickness": {"plate": 3, "text": 0.6, "shape": 0.4, "svg": 0.4, "hole": 1},
  "plate": {"shape": "circle", "w": 99, "h": 99, "colour": "#FFFFFF",
            "border": {"enabled": true, "weight": "medium", "width": 2.0, "offset": 0, "colour": "#000000"},
            "svg": null},
  "elements": [
    {"id": "e1", "type": "vector", "name": "Mahjong art", "layers": [
       {"colour": "#050504", "svg": "layers/e1-0.svg"}, {"colour": "#E7232C", "svg": "layers/e1-1.svg"}],
     "transform": {"x": 0, "y": 8, "w": 90.5, "h": 60.4, "rotate": 0, "flip_x": false},
     "depth": 1, "lock": false},
    {"id": "e2", "type": "text", "text": "MAHJONG", "font": {"family": "Roboto", "file": "fonts/Roboto-Bold.ttf", "weight": 700, "italic": false},
     "size": 14, "align": "center", "line_height": 1.16, "letter_spacing": 0, "curve": null, "colour": "#000000",
     "transform": {"x": 0, "y": -15, "rotate": 0, "flip_x": false},
     "expose": {"text": "title_text", "colour": "title_colour"}},
    {"id": "h1", "type": "hole", "kind": "inside", "position": "single_top", "size": "small", "d": 5,
     "transform": {"x": 0, "y": 42}}
  ],
  "parameters": {"plate_colour": {"from": "plate.colour"}, "plate_height": {"from": "thickness.plate", "min": 0.6, "max": 10}}
}
```

- **Element types:**
  - `text` (straight or curved, `curve: {radius, direction}`)
  - `icon` (an emoji: MakerWorld's `IconElement`)
  - `shape` (circle, triangle, rectangle, pentagram: `BaseShapeElement` and
    `SvgShapeElement`)
  - `svg` (an uploaded SVG; each fill colour is a layer: `SvgImageElement`)
  - `vector` (a vectorized image, one layer per colour and depth: `VectorizeElement`)
  - `hole` (`inside` | `boundary` | `back`, in MakerWorld's sizes and positions:
    `BaseHoleElement`)

  Appendix A gives each type's fields, ranges and defaults and its MakerWorld
  counterpart.
- **Coordinates:** the origin is the plate's centre, X right, Y up, in mm. Rotation is
  degrees counter-clockwise. The importer converts Fabric pixels with the editor's own
  scale (Appendix A.2).
- **Heights,** as MakerWorld stores them: one thickness per element type
  (`thickness`, MakerWorld's `backdropHeight`, `textHeight`, `shapeHeight`,
  `svgHeight`, `holeHeight`; icons use `text`, the border uses `shape`). An element
  may override its own with `depth` (MakerWorld's `data.depth`). Each element's z range
  is derived when the sign is compiled, never stored, so changing a thickness moves
  every element of that type.
  - **Facedown** (`facedown: true`, MakerWorld's `flatMode`) sinks every element into
    the plate: its top is flush with the plate's top and the sign prints face down.
  - **No plate** (`plate.shape: "none"`, MakerWorld's `nonePlateMode`) leaves only the
    elements.
- **Colour:** any hex colour; no fixed palette (release notes 1.3.0). Elements with the
  same colour print with the same filament. The colour list is derived from the
  document, never stored separately.
- **Exposing parameters:** an element field becomes a customizer parameter of the
  generated template through `expose` (as in spec 2026-10-11 §3.2). The compiler
  writes `name = value; // annotation` and uses it. "Save as template" exposes by
  default:
  - each straight text's string and colour;
  - every colour;
  - the plate's size, height and border;
  - each element type's height.

  A name must be unique and a valid identifier.
- **Caps** (`signs/src/doc/caps.ts`, mirrored in Python):
  - 200 elements;
  - 64 layers per element;
  - 2 MiB per layer SVG and 16 MiB of layers per sign;
  - 8 fonts;
  - text of 500 characters.

  The JSON is validated against the schema on every write, in TypeScript and in Python
  (the schema is generated from the TypeScript types into
  `backend/scadbuddy/signs/sign.schema.json`; a freshness test compares the two).

## 5. The compiler

`compile(sign) → {source: string, files: {path: bytes}, parameters: [...]}`, a pure and
deterministic function in `signs/src/compile/`. It emits a plain `.scad`, so the
render, versions, Arrange and print paths see an ordinary template and need no change.

### 5.1 Output layout

```
models/<slug>/
  model.json      # name, description, tags, "sign": {"version": 1}, plus the source's provenance and licence
  sign.json       # source of truth (§4)
  model.scad      # GENERATED from sign.json; committed
  layers/*.svg    # one closed, filled SVG per (element, colour), in mm, normalised
  fonts/*.ttf     # every font the sign's text uses
  thumbnail.png
```

### 5.2 `model.scad` shape

```scad
// Generated from sign.json by ScadBuddy Sign Maker; edit the sign, or eject to edit this file.
use <fonts/Roboto-Bold.ttf>
title_text = "MAHJONG"; // text
title_colour = "#000000"; // color
plate_colour = "#FFFFFF"; // color
border_colour = "#000000"; // color
plate_height = 3; // [0.6:0.2:10]
…
module plate_outline() { circle(d = 99, $fn = 256); }   // or a shape's path data, or import("layers/plate.svg")
module border_ring() { difference() { offset(r = -border_offset) plate_outline(); offset(r = -(border_offset + border_w)) plate_outline(); } }
module e1_0() { import("layers/e1-0.svg"); }
module e2() { text(title_text, size = 14, font = "Roboto:style=Bold", halign = "center", spacing = 1); }
module cutters() { translate([0, 42, -1]) cylinder(d = 5, h = plate_height + 2, $fn = 64); }  // inside holes; back holes from z 0

// Facedown: inlays sit in pockets cut from the top face; the whole sign is then turned face down.
translate([0, 0, plate_height]) rotate([0, 180, 0]) {
  color(plate_colour) difference() {
    linear_extrude(plate_height) plate_outline();
    translate([0, 0, plate_height - 0.4]) linear_extrude(1) border_ring();
    translate([0, 8, plate_height - 1]) linear_extrude(2) e1_0();
    translate([0, -15, plate_height - 0.6]) linear_extrude(1) e2();
    cutters();
  }
  color(border_colour) difference() { translate([0, 0, plate_height - 0.4]) linear_extrude(0.4) border_ring(); cutters(); }
  color("#050504") difference() { translate([0, 8, plate_height - 1]) linear_extrude(1) e1_0(); cutters(); }
  color(title_colour) difference() { translate([0, -15, plate_height - 0.6]) linear_extrude(0.6) e2(); cutters(); }
}
```

These rules keep every colour attributed and every volume single-coloured:

- **Each colour solid is its own `color()` block.** `render/solids.py`'s per-colour
  re-render, which shadows `color`, then yields one closed solid per colour, as
  CLAUDE.md requires.
- **Holes cut inside each `color()` block.** The difference wraps each coloured solid,
  as the mesh-models compiler does (spec 2026-10-11 §3.3), because a cutter outside
  every `color()` gives its faces the default material.
- **Overlaps resolve by z-order.** Where two elements overlap, the upper one (later in
  `elements`) owns the shared volume; the lower one is `difference()`d by the upper
  one's footprint over the shared height. No volume ever has two colours.
- **Facedown inlays every element from the plate's top face down to its own depth,**
  clamped to the plate's height, as MakerWorld clamps it (`min(backdropHeight, h)`,
  Appendix A.3).
  The plate is `difference()`d by the element footprints over that depth, so each
  element sits flush in its pocket. **This deliberately differs from MakerWorld:** its
  3MF leaves the plate whole and lets overlapping parts share volume, so the slicer's
  part order decides the colour there. OpenSCAD's per-colour solids must not overlap
  (CLAUDE.md), so ScadBuddy cuts. The printed result is the same. The whole sign is then turned face down:
  `translate([0, 0, plate_height]) rotate([0, 180, 0])` is the same matrix as
  MakerWorld's build transform (§2.3), so the face prints on the bed with no slicer
  step. The border inlays the same way, to its own depth (§2.3 measured 0.4 mm).
- **Without facedown, elements stand on the plate's top face** (z from
  `thickness.plate` to `thickness.plate` plus the element's depth).

### 5.3 Text

- **Straight text compiles to `text()`,** with the font file shipped and `use`d (§2.1),
  so its string stays a live parameter. The compiler measures the layout with
  harfbuzzjs (the HarfBuzz WebAssembly build MakerWorld itself shapes with) from the
  same font file, mirroring Fabric's Textbox:
  - wrapping at the box width;
  - `lineHeight × fontSize` between lines;
  - `charSpacing / 1000` em of extra spacing;
  - left, centre or right alignment.

  It emits one `text()` per line at the measured baseline. A wrapped line break is
  fixed when the sign is compiled. Each line's string is its own parameter
  (`<name>_line<N>`) when a text wraps; an unwrapped text is one parameter.
- **Curved text compiles to one `text()` per glyph,** each translated and rotated
  along the arc using advances measured when the sign is compiled. A curved text's
  string is not exposed, since changing it needs new advances. It is edited in the
  Sign Maker, and the generated template's README says so.
- **Bold and italic** select the family's own face file, as MakerWorld does. A
  variable font (NotoSans, NotoSansSC) gets the weight on its variation axis: 600
  normal, 900 bold. MakerWorld offers bold and italic only for the families that have
  those faces (Appendix A.3), and so does the Sign Maker, so nothing is synthesised.
  OpenSCAD reaches a variable font's weight by its named instance
  (`font = "Noto Sans SC:style=Black"`). That a named instance renders at the weight
  MakerWorld uses is measured in the verify script (§12); a font with no matching
  instance ships the static face file instead.
- **Braille fonts** (three in MakerWorld) are fonts like any other.
- **Icons (emoji)** compile to `text("<emoji>", font = "Noto Emoji:style=Bold")`, with
  `NotoEmoji-Bold.ttf` shipped and `use`d, which is the glyph MakerWorld outlines.
  The emoji is then a live parameter too. The picker uses the same unicode-emoji-json
  catalogue and groups (Appendix A.1).
- **Fonts for imports** come from the same CDN files MakerWorld uses
  (`public-cdn.bblmw.com/content-generator/fonts/`, an allowlisted host for
  `signs_fetch_assets`), or from the project's `.font` assets for a local font.
  - Each font's licence is recorded in the template's `model.json`.
  - A font whose licence the catalogue does not list as redistributable (the
    "stylized" group, Appendix A.3) is fetched for rendering but kept out of the
    generated template's committed files. The template then names the font, and
    that file goes to the ScadBuddy font install path (`library/font_operations.py`)
    instead.
  - A missing local font falls back to NotoSansSC, as MakerWorld's
    `onFontMissing` does, with a warning naming it.

### 5.4 Shapes, icons, SVG and vector layers

The normaliser (§6) turns each into closed, filled outlines in mm, one SVG per colour:

- It flattens transforms, converts strokes to fills with clipper, and resolves the
  fill rule.
- It drops anything with zero area.
- It writes `<svg width="Wmm" height="Hmm" viewBox="0 0 W H">`.

The compiler then `import()`s each file, so OpenSCAD never sees a stroke, a clip path
or a CSS style.

### 5.5 Holes, border, plate

- **Plate outlines** (Appendix A.1):
  - None;
  - Circle, an ellipse once scaled;
  - Rectangle, with a corner radius;
  - eight outline shapes: Star, Heart, Cat, NumberPlate, Bone, ClosedHanger,
    OpenHanger, Triangle;
  - Custom, from an SVG.

  The eight outline shapes ship in `signs/src/shapes/` as path data. Their geometry is
  reproduced from the editor's own outlines (Appendix A.1), so an imported plate
  matches. The PR that adds them names this provenance for review.
- **The border is a ring inside the plate,** as MakerWorld draws it: `offset(r =
  -o) outline` minus `offset(r = -(o + w)) outline`. Here `o` is `borderOffset` and
  `w` the weight: None 0, Light 1, Medium 2, Bold 4 mm. Clipper's round joins become
  OpenSCAD's `offset(r=)`. The ring stands `shapeHeight` on the plate, or is inlaid
  that deep in facedown (§2.3 measured 0.4 mm).
- **Holes** (Appendix A.1):
  - `inside`: Ø5 or Ø10 mm, cut through the plate and through every element above it.
  - `back`: a keyhole slot, 6×7.6 or 12×13.6 mm, cut 2 or 3 mm up from the underside.
    The editor warns when the plate is thinner than the cut + 1 mm.
  - `boundary`: a tab outside the outline, 8×9 or 18×26 mm, in the plate's colour
    and height, with its hole.
  - Positions are single top, single top-left or double top, from per-shape tables.
    A hole the user has moved keeps its saved transform (hole record 2.0). A hole not
    fully inside the plate is drawn red and left out of the 3D, as in MakerWorld.

### 5.6 Determinism and the hand-edit check

`sign.json` records `generated_sha256`, the hash of the text the compiler last wrote, as
mesh models do (spec 2026-10-11 §5). An edit is refused with 409 "model.scad was edited
by hand; eject the sign to keep editing it as source" when the hash differs. A newer
compiler emitting different text never reads an existing sign as hand-edited: the
check compares the stored hash, never a fresh compile.

## 6. TypeScript on the server: the `signs` worker

### 6.1 The package

`signs/` is a new pnpm package (Node 24, as the other two; corepack). Its dependencies
are pure JS or WebAssembly, so it bundles for both Node and the browser:

- `harfbuzzjs` (shaping and glyph metrics; WebAssembly, runs in Node and the browser)
- `clipper2-js` (offsets, booleans, stroke-to-fill)
- an SVG path parser
- `@temporalio/*` (worker only, pinned to the agent's exact version)
- `fabric@5.3.0` in the editor bundle only

It has no native addons. Its modules are `doc/`, `import/`, `normalise/`, `compile/`, `editor/` (§7.1) and
`worker.ts`. It builds two bundles:

- `dist/worker.js`, for the `signs` image target;
- `models/sign-maker/ui/editor.js`, the template UI. It is committed, because a template's
  `ui/` is served as files. The `freshness` job rebuilds it and fails on a diff, as it
  does for the msw worker.

CI gets a `signs` job (lint, typecheck, test, build), and `ci.yml`'s `CI Summary` needs
it. Node is pinned in the same places as the other Node stages.

### 6.2 Activities

Served on the task queue `signs` (`SCADBUDDY_TEMPORAL_TASK_QUEUE_SIGNS`) by one
unversioned worker:

| Activity | In | Out |
|---|---|---|
| `signs_import` | `{kind: "makerworld_make" \| "makerworld_template", content, assets, plate_svg?}` (blob pointers for large bytes) | `{sign, files: {path: Blob}, warnings}` |
| `signs_normalise` | `{sign, files}` (an edited sign whose new uploads are raw) | `{sign, files, warnings}` |
| `signs_compile` | `{sign, files}` | `{source, files, parameters, sha256}` |
| `signs_fetch_assets` | `{urls}` (public `makerworld.bblmw.com` SVG asset URLs only; allowlisted host, capped size, no redirects off-host) | `{files: {name: Blob}}` |

Rules for the activities:

- **Bytes move as blob pointers, never inline in payloads.** Arguments and results are
  JSON. Each large file is `{name, mediaType}` plus a blob key in the content store (the
  CLAUDE.md rule against base64 images in JSON, `lint-base64-images.sh`). The worker
  reads and writes blobs through the backend's internal blob routes, the same
  `SCADBUDDY_API_INTERNAL_URL` channel the print worker uses
  (`bambuddy/output_reader.py` `RemoteOutputs`).
- **No credentials beyond the API's internal key.** The worker holds no Bambuddy key,
  no KEK and no Claude credential. Its only outward reach is the
  `makerworld.bblmw.com` asset fetch.
- **Deterministic:** the same input gives the same bytes, which `signs_compile`'s
  tests pin.

### 6.3 How Python reaches it

**From a workflow** (every phase-1 path): `workflow.execute_activity("signs_compile",
…, task_queue=signs_queue)`. Both `TemplatePipeline` (the Sign Maker's preview, §7.2)
and `OperationWorkflow` (create, edit and import, §8) are workflows already.

**Standalone, from the API with no workflow** (#3468 follow-up): `Client.execute_activity`
on the same queue, for calls that need no record, e.g. validating an import before
showing its preview. The dev server and the pinned SDK support it (§2.2). Production's
Server 1.28.4 does not, and the operator caps the server below 1.29. Phase 1 therefore
routes every call through a workflow. A follow-up issue in eh-homelab/clusters tracks
moving Temporal to a server that supports standalone activities. After that, the
standalone path is a one-function change (`backend/scadbuddy/signs/service.py`
`call(name, arg)` picks the client or the workflow call), behind a setting.

### 6.4 Deployment and dev

- **Production:** the Dockerfile target `signs` (Node 24 slim plus `dist/worker.js`)
  runs as its own Deployment. That needs a clusters PR adding the Deployment and its
  internal-API secret, filed with the implementation.
- **Dev:** with `SCADBUDDY_TEMPORAL_WORKER_INPROCESS=true` the API starts
  `node signs/dist/worker.js` as a child process when Node is on `PATH`, so a
  one-process dev run works. Without Node, the Sign Maker reports "the signs worker is
  not running" instead of hanging.
- **Tests:** tests marked `requires_signs` skip unless Node and a built
  `signs/dist/worker.js` exist. The CI `backend-pytest` shards build it.

## 7. The Sign Maker template (`models/sign-maker`)

### 7.1 The editor UI

`ui/editor.js`, `{"ui": {"module": "ui/editor.js", "slot": "page", "api": 1}}`, is a
full-page editor reproducing Make My Sign's layout (Appendix A.7):

- **Top toolbar:** Text, Emoji, Shape, SVG, Hole, Plate, Thickness, plus Import (§9).
- **Left rail:** select, pan, copy, paste, undo, redo, delete.
- **Top right:** 2D/3D, zoom, settings (units metric/imperial and mm/cm, show plate
  reference), Generate, Save as template.
- **A properties panel** for the selected element or tool.
- **A printer/bed selector** showing the bed outline.

Behaviour:

- **The canvas** is Fabric.js 5.3.0 over the document. It shows a live 2D view of the
  same normalised layers the server compiles, with no server round trip per drag.
- **3D** is the existing `<sb-preview>` of the last render.
- **Generate** is `<sb-generate>`.
- **Editing:** multi-select (Shift-click, Ctrl+A), align and distribute, layer order,
  flip, rotation with snap, arrow-key nudge, numeric size input, lock, undo/redo
  (command stack: add, modify, remove, batch remove).
- **State:** the document is the template's UI state, in `inputs.sign` (scalars only
  under `params`, so the 64 KB inputs cap holds). Uploaded bytes go to the asset store
  (`library/assets.py`), and the document names them by asset id, never inline.
- **Host API additions,** all minor (spec 2026-09-27 §8.1): `host.assets.upload(file)
  → {id, url}` and `host.signs.saveAsTemplate({name, description, tags}) → {slug}`. A
  template other than the Sign Maker cannot call the second: the route checks that
  the template is a template maker, with `"maker": true` in `model.json`.

### 7.2 Preview and Generate

The Sign Maker's `pipeline/pipeline.py`:

```python
async def run(ctx, inputs):
    compiled = await ctx.service("signs_compile", {"sign": inputs["sign"]})
    part = await ctx.render("model.scad", files=compiled["files"])
    await ctx.output(plates=[ctx.plate_of([part])], name=inputs["sign"].get("name"))
```

This adds two generic `ctx` primitives (spec 2026-09-27 §5.2, minor additions):

- **`ctx.service(name, arg)`** runs a host service activity: a TypeScript activity on
  its own queue, from an allowlist in `workflows/services.py`. Phase 1's list is the
  `signs_*` activities. Template code cannot name an arbitrary activity or queue.
- **`ctx.render(file, files=…, **params)`** overlays files onto the revision's snapshot
  for this piece. The compiled `model.scad` replaces the shim's, and `layers/` and
  `fonts/` are added. The piece key includes each overlay file's digest, so
  identical signs dedupe as identical parameters do. Overlay files are blob
  pointers, capped at the §4 sizes.

Rendering, colour splitting, solids, plates and printing are unchanged.

## 8. A sign becomes a template

These are `OperationKind`s on the `library` queue (`backend/scadbuddy/signs/operations.py`,
exported as `OPERATION_KINDS`), run through `run_operation`.

A kind's `run` is itself an activity (`workflows/operation.py`), so it cannot call
another one. Instead, `OperationKind` gains `service: ServiceCall | None`:
- `OperationWorkflow` runs the named host-service activity (§7.2's allowlist) on its
  own queue after the check and before the run, as it already runs a kind's
  `prelude` on another queue (`operation.py`, `PRELUDE_PATCH`).
- The run then receives the service's result as `checked["service"]`.
- This sits behind `workflow.patched("operation-service-3468")`, so histories
  recorded before it replay unchanged.

### 8.1 `sign_template_create`

Saves a sign as a new template. Its request is `{name, description?, tags?, sign}`, or
`{…, source_output_id}`. The run:

1. normalise and compile (§6.2);
2. write `model.json`, `sign.json`, `model.scad`, `layers/` and `fonts/`;
3. render a thumbnail;
4. make one git commit.

The new template has no `upstream` and no fork relation. It is not a duplicate of
`sign-maker`. It carries no `ui/` of its own (§8.2). The slug comes from the name, as
model creation does today, and a slug already taken is refused.

### 8.2 Editing a generated template

- **Customize** works like any template: the generated form shows the exposed
  parameters, and presets, outputs and printing are unchanged.
- **"Edit in Sign Maker"** (a button shown for a model whose `model.json` carries
  `sign`) opens the Sign Maker's editor on that model's `sign.json`.
- **"Save" there runs `sign_edit`,** which takes the edited sign, recompiles it and
  commits. The hand-edit check is §5.6. Undo is version history and restore.
- **Exposed names belong to the element,** as in mesh models. Removing one while a
  saved preset names it is refused with 409, unless `drop_from_presets: true`.
- **`sign_eject`** deletes `sign.json` in one commit. The model stays and becomes plain
  source that imports `layers/` and `uses` `fonts/`.

### 8.3 Agent and flows

The agent gets tools, each with a `defineTool` and a `coverage.ts` entry:

- `sign_get`
- `sign_create_template` (a sign or an import)
- `sign_edit` (a list of edits: add, move, set, remove, expose)
- `sign_eject`
- `sign_import`

All are local library writes, and none is `outward`. Flows reach them through
`tool(name, args)`. The plugin's `authoring` skill gains a Sign Maker section citing
this spec.

## 9. Import (1:1)

`sign_import` is an `OperationKind`; the Sign Maker's Import button and the agent call
it. It accepts:

- **A Make My Sign 3MF** (the primary path). This is what the editor's Download writes
  with "Include customizable file" on (§2.3): `Auxiliaries/Templates/template.json`
  plus its assets. It needs no MakerWorld access at all, and it is also how a 3MF
  already in the Bambuddy library imports (`library_file_id`, read as Arrange reads
  library files). A published model's own 3MF, from a model created by Make My Sign,
  is expected to carry the same files; that is checked against the Mahjong model when
  the fixtures are collected (§12). A 3MF without `template.json` is refused with
  "this 3MF has no Make My Sign project; open it as a mesh model instead".
- **The make JSON**, the body of `GET /api/v1/creation-service/my/make/<id>`. The user
  saves it from a browser logged in to MakerWorld; the import dialog shows how. The
  server cannot read a user's makes, since it holds no MakerWorld session and never
  will.
- **A MakerWorld template id** (the 12 gallery templates). `GET
  /api/v1/creation-service/template/<id>` returns the full content. It was read logged in
  (2026-10-14), and whether it answers without a session is unchecked. The 12 are test
  fixtures only (§9's fidelity check), since they are other people's designs. The
  Sign Maker's own starter gallery is signs authored for ScadBuddy, under
  `models/sign-maker/starters/`. A user who wants a MakerWorld template imports its
  JSON or its 3MF like a make's.
- **A custom plate SVG**, when the content uses one (it is only in the user's browser,
  §2.3).

What happens:

1. **Assets.** From a 3MF they are its `Auxiliaries/Templates/assets/`. From make JSON,
   `signs_fetch_assets` fetches every `assets.svgAssets[].url` (public, allowlisted
   host, size-capped).
2. `signs_import` maps every element type to `sign.json` (Appendix A gives the mapping
   field by field):
   - Fabric pixels become mm.
   - Fabric transforms (scale, angle, flip, origin) are flattened.
   - A `VectorizeElement`'s paths are grouped by `colorPathMap`, with `paletteMap`
     entries marked deleted merged into their parent colour.
   - Fonts resolve as §5.3 says: MakerWorld's catalogue file, a `.font` asset, or the
     NotoSansSC fallback with a warning.
   - Hole records 1.0 recompute their position from the plate; records 2.0 keep their
     saved transform. `HookElement` is dropped with a warning, as MakerWorld drops it.
   - Legacy content that is a bare array is read as `data`.
3. The result opens in the Sign Maker as an unsaved sign, or becomes a template
   directly (`sign_template_create` with the import as its source). `source` records
   the make, instance and model URL. The template's `model.json` records the
   MakerWorld licence when the content states one (the Mahjong model is a Standard
   Digital File License: personal use only, which the README repeats).

**Fidelity check:** for a 1:1 import, the compiled sign's footprint per colour matches
MakerWorld's own exported 3MF for the same project. Footprint means area and bounding
box in XY, within 1 % of area and 0.2 mm of bounds. §2.3 and Appendix A.3 give MakerWorld's side.
The Mahjong make's export (`mahjong.3mf`, 2.9 MB, collected 2026-10-14) and the 12
gallery templates are fixtures: their JSON, their SVG assets and MakerWorld's 3MF for
each go under `test-fixtures/makerworld/sign/` (Git
LFS, `manifest.json` entries, `requires_fixtures`).

## 10. Errors

- **A sign that fails validation** is a 422 naming the JSON pointer of the bad field.
- **A sign past a cap** (§4) is a 422 naming the cap.
- **An import with an element type it does not know** (a future MakerWorld format)
  imports everything else and reports each dropped element by type and index in the
  operation's `warnings`, shown in the import dialog before saving. It is never
  silently dropped.
- **A font that is neither in MakerWorld's catalogue nor among the project's assets**
  falls back to NotoSansSC, as MakerWorld does (§5.3), and the import's `warnings`
  name it.
- **The signs worker down:** the Sign Maker shows "the signs worker is not running"
  after a 30 s `schedule_to_start` timeout, and the operation fails with that text.

## 11. MakerLab survey and the template-maker contract

The survey of MakerLab's 39 tools (Appendix B) sorts them into families. Each family
names the host primitives it needs:

| Family | Tools (examples) | Primitives |
|---|---|---|
| Layered 2D signs | Make My Sign, Image to Keychain, Lightbox Maker, Christmas Ornament Maker, Polaroid Frame, Laser Studio (3D-printable parts) | the `signs` document and compiler (this spec); image → colour-quantised vector layers (Image to Keychain: 4/8/12 colours, contour outlines, export to Make My Sign) |
| Height maps | Make My Lithophane, Lithophane fans, Relief Sculpture, PlakHaus, Shadowbox, Make My Lantern | image → height map → mesh (a pipeline activity), with wrapping onto a curved surface |
| Parametric | Parametric Model Maker, ParamaCraft, Bento3D, Desk Organizer, Vase, LumHaus, Folding Box | what ScadBuddy templates already are; plus a grid/cell editor and a profile-curve editor as template UIs |
| Codes and data | QRFun, Star Map, Song Card, Keycap Legend, Stamp Maker, Clicker | a generator activity (QR, a star catalogue) feeding a sign-like document |
| Mesh painting | Mesh Graffiti | ScadBuddy mesh models' paint (spec 2026-10-11 §7) |
| AI | Image to 3D, PrintU, Make My Statue | out of scope; a resulting mesh can open as a mesh model |

The contract this spec defines for any template maker:
- a document in the UI's state;
- host services in TypeScript on Temporal;
- `ctx.service` and `ctx.render(files=)`;
- an operation that writes a new ordinary template with the document as its source of
  truth and a generated `model.scad`.

Image to Keychain is the next maker: it reuses `vector` elements and adds one service
(`signs_vectorize`).

## 12. Testing

- **`signs/` (vitest):**
  - schema validation;
  - importer golden tests: each fixture's NamePlate JSON → `sign.json`, snapshot;
  - normaliser tests on strokes, transforms and fill rules;
  - the compiler's golden `.scad` per fixture;
  - text layout against Fabric's own measurement of the same font, run in jsdom with
    `fabric` 5.3.0;
  - activity tests on a Temporal dev server (`requires_temporal`).
- **Backend:**
  - `ctx.service` allowlist refusal;
  - `ctx.render(files=)` piece keys: the same files dedupe, a changed file misses;
  - the operations: create, edit with the hand-edit 409, eject, import warnings, the
    preset 409;
  - all on Postgres and Temporal as `tests/api` does, with `requires_signs`.
- **OpenSCAD facts** (`models/sign-maker/verify.sh`, the `models` job): §2.1's three
  facts re-measured on the pinned image. On top of that:
  - every colour has a closed solid;
  - no geometry is uncoloured;
  - facedown leaves the top face flush (z range per part);
  - each hole type removes the volume it should;
  - the Mahjong fixture's per-colour footprints match MakerWorld's 3MF (§9).
- **Frontend:**
  - the editor module's unit tests;
  - a mocked e2e: open the Sign Maker, add a text, Generate, Save as template, open
    the new template's form;
  - `e2e/real-backend.spec.ts` gains the same against a container.

## 13. Phases

1. **`signs/` package:** schema, normaliser, compiler; golden tests; the `signs`
   worker and Dockerfile target; the CI job.
2. **Backend:** `ctx.service`, `ctx.render(files=)`, the sign operations, the
   in-process dev child; the agent tools.
3. **The Sign Maker template:** the editor with full parity (Appendix A's checklist),
   preview and Generate, and Save as template.
4. **Import:** make JSON, templates and fixtures (the Mahjong coaster first); the
   fidelity measurement.
5. **Follow-ups filed as issues:** standalone activities once production Temporal
   supports them (§6.3); the clusters Deployment; Image to Keychain as the next maker
   (§11).

## Appendix A. Make My Sign, reverse-engineered

Read from the shipped bundle (MakerWorld build `arlj243Es7etS3BsJ5nsQ`, captured
2026-10-14), and checked against the live editor, the 12 gallery templates
(`GET /template/15..26`) and the Mahjong make's 3MF export (§2.3). Everything was read
from code unless it is marked **INFERRED**. "B" is the editor chunk
(`static/chunks/b0b707fb.05a1eeeeedd6a707.js`, one scope-hoisted module), and "E" is
the edit page (`pages/makerlab/makeMySign/edit/[id]-f97cf818d5000b7d.js`). Line
numbers are into the prettier-formatted chunks.

The live captures confirmed these figures independently:
- 5 px = 1 mm;
- Mahjong facedown z ranges: plate 0–3, border 2.6–3.0, vector 2.0–3.0;
- the 180° Y build transform;
- the extruder order (plate, border, then elements);
- holes stored as `{holeType: "out_single_top", holeSize: 20}`;
- icons stored as `{content: "♿", …}`;
- fonts named by family, such as `站酷快乐` (ZCOOL KuaiLe).

The gallery templates use these element types: BaseShapeElement 63, IconElement 37,
TextElement 32, Backdrop 12, BaseHoleElement 4.

### A.1 Content schema ("NamePlate V1.1")

The make's `content` field is a JSON string. It is built in `aO.changeSerializedData` (B:11032):

```js
this._serializeData = { data: n /* element records, canvas z-order (back→front) */,
  thickness: this._thickness, globalStatus: this._globalStatus, date: Date.now(), version: nx /* "NamePlate V1.1" */ };
```

- `data`: the element records, sorted by their index in fabric's `canvas._objects`. The first record is the bottom-most, and the backdrop is always sent to the back.
- `thickness` (`aw`, B:11015), in mm. The defaults are `{backdropHeight:1, textHeight:0.6, shapeHeight:0.4, svgHeight:0.4, holeHeight:1}`.
- `globalStatus` (`ak`). The defaults are `{flatMode:false, nonePlateMode:false}`.
  - `flatMode` is called "Facedown Mode" in the UI. Its description reads: "All the elements will be embedded into base plate to make a flat surface. The sign will face down to the print plate…" (B:34969).
  - `nonePlateMode` is forced on when the backdrop shape is None (B:29519). While it is on, flatMode is disabled.
- `date` is epoch ms. `version` is always `"NamePlate V1.1"`. No other top-level version string occurs in the code.

The working copy is also mirrored to `localStorage["<uid>-<makeId>-content_gen.nameplate: saved data"]`.

### Thickness keys (`ng`, B:4211)

This maps a render type to its `thickness` key:

| Render type | `thickness` key |
| --- | --- |
| text, icon | `textHeight` |
| shape, border | `shapeHeight` |
| backdrop | `backdropHeight` |
| svg | `svgHeight` |
| hole | `holeHeight` |

Note that the border uses `shapeHeight`. `holeHeight` is stored but never drives any geometry; see §3.

Per-element depth override: `fabricObj.data.depth` (or `props.data.depth`). The element's `preProcessBeforeSerialize` always writes `data:{depth}` (B:8890). When `depth > 0` it replaces the type's default thickness (`customDepth`).

### Element records

Every element is a fabric object in the canvas coordinate system (§2), plus wrapper fields. Element type ids live in `iE` (B:8708): None 0, Backdrop 1, Text 2, Icon 3, BaseShape 4, SvgShape 5, BoundaryHole 6, InsideHole 7, BackHole 8, SvgImage 9, Group 10, Vectorize 11.

The deserializer table `o7` (B:16398) maps type strings to classes: `BackdropElement, BaseShapeElement, SvgShapeElement, IconElement, TextElement, BaseHoleElement, SvgImageElement, VectorizeElement`. `HookElement` is filtered out on load and never created, so it is legacy (§6).

#### BackdropElement (`iI`, B:8936)

```json
{"version":"1.0","type":"BackdropElement","shape":<n7>,"group":{...}}
```

Plate shapes (`n7`, B:7780):

| Value | Name |
| --- | --- |
| -1 | None |
| 0 | Circle |
| 1 | Rectangle |
| 2 | Star |
| 3 | Heart |
| 4 | Cat |
| 5 | NumberPlate |
| 6 | Bone |
| 7 | ClosedHanger |
| 8 | OpenHanger |
| 9 | Triangle |
| 10 | Custom |

The `group` object depends on the shape:

- **Circle** (`iC`): `{outlineEnabled, borderOffset, borderWidth, borderColor, backdropColor, mainObj: <fabric Circle JSON>}`. A new circle has radius 250 px (Ø100 mm). The radius is `max(w,h)/2`, and `scaleX`/`scaleY` give an ellipse.
- **Rectangle** (`im`): the same fields with `mainObj: <fabric Rect JSON>`. A new rectangle is 750×500 px (150×100 mm) with `rx=ry=50` (corner radius 10 mm). The corner radius is `mainObj.rx`, and the UI slider runs from 0 to 100 mm.
- **SVG shapes 2–10** (`ig`): `{outlineEnabled, borderOffset, borderWidth, borderColor, backdropColor, shape, shapeUrl, width, height}`.
  - `width` and `height` are fabric group px.
  - The SVG is looked up again from `ic.get(shape)` on load. Shapes 2–9 are inline data URIs, extracted to the bundled backdrop outlines (viewBox 595.28×841.89, except Triangle which is 100×86.6).
  - A new shape is 750 px wide, or 750 px tall when the SVG is taller than it is wide (`id`, B:8311).
  - **Custom (10)** reads its SVG from the per-make svgStore under the key `"CUSTOM_SVG_SHAPE"`. This is "Import SVG as Base Plate".
- **None** (`ip`): `{outlineEnabled:false, …, mainObj:<invisible fabric Object JSON>}`.

Defaults from the `is` base class (B:7968): `_outlineEnabled=true, _borderOffset=10, _borderWidth=10, backdropColor="#FFFFFF", borderColor="#000000"`.

Border weights (`dV`, B:30744). Widths are px; divide by 5 for mm:

| Weight | px | mm |
| --- | --- | --- |
| None | 0 | 0 |
| Light | 5 | 1 |
| Medium | 10 | 2 |
| Bold | 20 | 4 |

Choosing None turns `borderEnabled` off. The border offset slider runs from 0 to 100 mm (stored ×5).

Because the backdrop is centred on the origin (`left=-w/2, top=-h/2`), the canvas origin is the plate centre.

A new project gets a Rectangle backdrop (`initBackdrop`, B:16649). If a backdrop fails to load, it is replaced by a Rectangle and `onBackdropError` fires.

#### TextElement (`av`, B:10626)

```json
{"version":"1.0","type":"TextElement","fabricObj":{...}}
```

`fabricObj` is either a fabric **Textbox** (straight text) or a fabric **Text** with `path` (curved text). The saved JSON drops `path` and keeps `curvePoint {x,y}`; the arc is rebuilt on load (`an(curvePoint, textWidth)`).

- Class defaults: `left:-100, top:-15, fontSize:36, width:200, fill:"#353535", fontFamily:"times new roman", textAlign:"left"`.
- The "Add text" button calls `addText("Input Text", {fontFamily:"NotoSansSC", fontWeight:600})` (B:32158).
- `",AdobeNotDef"` is appended to `fontFamily` as a tofu fallback. Only the first comma-separated family is used for geometry.
- Variable fonts (NotoSans, NotoSansSC; `nI`) store a numeric weight: normal 600, bold 900. Other fonts use `"bold"` / `"italic"`.

Curve presets (`nd`, B:4220): UpwardCurve 0, DownwardCurve 1, CircularCurve 2, None 3. `ai()` (B:10380) computes the initial `curvePoint`:
- **Up:** `(-√2·r/2, -√2·r/2)` with `r = 2·w/π`.
- **Down:** the same, with +y.
- **Circular:** `(0, w/π - 22)`.

`aA()` builds an SVG arc through the start point (B:10262). A draggable "curveController" changes `curvePoint`. Curved text cannot be resized from its side handles.

#### IconElement — "Sticker" / emoji (`i0`, B:9975)

```json
{"version":"1.0","type":"IconElement","content":"<emoji string>","originScaleX","originScaleY","fabricObj":{... no path}}
```

The emoji glyph is rebuilt from the **NotoEmoji-Bold.ttf** outline at fontSize 50 via HarfBuzz (`n3(content,"NotoEmoji",50,1,1,0)`). It is single-colour, filled with `fabricObj.fill`, default `#353535`.

The picker data `ey` is the unicode-emoji-json dataset (`{slug, group}` keyed by char). VS16 is rewritten to VS15 for text presentation. Some codepoints are excluded (`gT`). The tabs are recent, people, animals_and_nature, food_and_drink, activity, travel_and_places, objects, flags (B:22289), and the search keywords are `g_` (B:22849).

#### BaseShapeElement (`iQ`, B:9075)

```json
{"version":"1.0","type":"BaseShapeElement","shape":0|1|2,"fabricObj":{...}}
```

`ib`: Circle 0 (fabric Circle radius 80), Triangle 1 (80×80), Rectangle 2 (80×80). All default to fill `#178F51` at `left/top -40`.

#### SvgShapeElement (`i4`, B:10186)

```json
{"version":"1.0","type":"SvgShapeElement","shape":0,"fabricObj":{... no path}}
```

`i9`: Pentagram 0 is the only shape. Its 36×36 inline SVG (B:10249) is regrouped by `groupSVGElements`. The default fill is `#E03D19` at `left/top -18`.

#### SvgImageElement (`i3`, B:10064)

```json
{"version":"1.0","type":"SvgImageElement","keyName":"<name>_<Date.now()>","props":{fabric props + optional fill + data.depth}}
```

- The SVG text itself is **not** in the content. It lives in the per-make IndexedDB store (§4) under `keyName`.
- The SVG loads as a fabric Group of its paths. When the group is wider than 450 px it is scaled to `900/(2·width)`.
- A `fill` in `props` recolours every child; with no `fill` the original colours are kept.
- An SVG is rejected if it contains `<clipPath` (`oK.checkSvgCompatible`, B:16156).

#### VectorizeElement (`ax extends i3`, B:10913)

This is an "Image to Keychain" import. The record has the same shape as SvgImageElement, and `props.vectorizeContent` is a JSON string (§4).

#### BaseHoleElement (`o3` / `iH` subclasses, B:9362–9975, 16318)

```json
{"version":"2.0","type":"BaseHoleElement","holeSize":<iP>,"holeType":<iw>,"fabricObj":{fabric JSON}}
```

Version `"1.0"` records have no saved geometry; positions are recomputed from the backdrop (§6).

Hole types (`iw`, B:9248). Each style has three positions: `single_top`, `single_top_left` and `double_top`. `none` adds nothing.

| Style (`iz`) | UI name | Types | Class |
| --- | --- | --- | --- |
| `"inside"` | Inside / "Inside Plate" | `in_single_top`, `in_single_top_left`, `in_double_top` | `i1` |
| `"outside"` | "Boundary" / "On the boundary" | `out_single_top`, `out_single_top_left`, `out_double_top` | `iG` |
| `"back"` | "On the back" | `back_single_top`, `back_single_top_left`, `back_double_top` | `iJ` |

Sizes (`iP`) are in px; 5 px = 1 mm:

| Size key | Value | Geometry |
| --- | --- | --- |
| `inSm` | 12.5 | fabric Circle, radius = holeSize px → Ø5 mm |
| `inBm` | 25 | Ø10 mm |
| `backSm` | 15 | the bundled hole outline (15 px), 30×38 px → 6×7.6 mm keyhole slot. Cut depth `aS[15]=2` mm |
| `backBm` | 30 | the 30 px outline, 60×68 px → 12×13.6 mm. Cut depth `aS[30]=3` mm |
| `outSm` | 20 | the 20 px outline, 40×45 px → 8×9 mm loop tab, filled with the backdrop colour |
| `outBm` | 45 | the 45 px outline, 90×130 px → 18×26 mm |

Positions are tables keyed by backdrop shape: `iW` single top, `iZ` double, `iV` top-left (inside and back); `i_`, `iN`, `iM` for outside.
- Example: Rectangle inside double is `left = -w/2 + 20 + 0.2928·cornerRadius`, `top = -h/2 + 20 + 0.2928·cr`. Its mirror twin uses `originX:"right"` and `left = -left`.
- Outside holes stick out of the plate edge by `height - 40` px. The top-left variant is rotated 315°.

Holes can be moved freely (v1.3). Lock (`isHoleLock`) and Hide (`isHoleHidden`) live in View Options. A hole that is not fully inside the plate turns red (`#d26d5d`), gets `isHoleInPlate=false` (`iD`), and is **excluded from 3D** (`aj`). Holes cannot be copied.

Back-hole warning: `aP()` sets `backHoleOverflow = max(aS[size]) >= backdropHeight`. The UI then prompts "Suggest {{thickness}}mm or above" and can set `backdropHeight = depth + 1`.

### `preset` (user settings, not per make)

The editor reads it from `GET /my/preset` (`{preset:"<json>"}`) and saves it with `POST /my/preset {preset}`. Captured value:

```json
{"date":…, "units":"mm", "version":"NamePlate V1.1", "unitsType":"metric", "showReference":true}
```

- `units` is `mm`, `cm` or `inch`. `unitsType` is `metric` or `imperial`.
- `showReference` toggles the reference/size labels.
- A copy is kept in `localStorage["<uid>-<id>-content_gen.nameplate: user setting"]`.

### A.2 Units and coordinates

- **5 canvas px = 1 mm.** `oz = {mm:5, cm:50, inch:127}` px per unit (B:14134). The size inputs use `cN(px,unit) = px/c_.get(unit)` (B:20929).
- The canvas origin (0,0) is the **plate centre**; fabric uses y-down. The viewport pans so the origin sits at the canvas centre (`absolutePan(-w/2,-h/2)`).
- In 3D, every SVG is exported as `<g transform="scale(1,-1)">` (`nM`, B:7079). Meshes are positioned at `(left, -top, z)` under a group scaled `(0.2, 0.2, 1)`, so XY is px→mm and Z is already mm (B:33418).
- The printer bed only chooses the print profile and size hints. `eX`/`eF` (B:291):

  | Key | UI label | Bed (mm) |
  | --- | --- | --- |
  | A1M | A1 Mini | 180×180 |
  | X1 | X/P/A1 | 256×256 |
  | H2D | H2D(Pro)/H2C | 300×320 |
  | H2S | H2S | 340×320 |
  | A2L | A2L | 330×320 |

  The printer list with model codes is also at B:291 (A1 N2S, A1M N1, … H2C O1C2). In the 3MF, only A1 mini and X1C profiles are emitted (§3d).
- Zoom: wheel factor `0.999^deltaY`, clamped 0.01–20 (B:16690). The view is saved under `localStorage "canvas-transform-state"` (`o8`).
- Rotation snaps to 45° with a 5° threshold. Skew is locked.

### A.3 Geometry pipeline

#### A.3a 2D outlines

- **Text** (`n5`, B:7689): HarfBuzz shapes each line (or each character, for curved text, using fabric `__charBounds` and its angle).
  - `glyphToPath` produces the SVG path in font units. It is scaled by `fontSize/upem` and wrapped in `rotate(360-angle) scale(scaleX·1000, -scaleY·1000)`.
  - The 3D step then applies `scaleX/Y = 0.001`. The ×1000 round trip keeps precision.
  - Fonts load from `https://public-cdn.bblmw.<com|cn>/content-generator/fonts/<file>` (`nF`, B:7232). HarfBuzz itself is `https://makerworld.bblmw.<com|cn>/makerworld/makerlab/content-generator/hb2.wasm`.
- **Font catalogue** (`nh` groups and `nX` files, B:4226/7190):

  | Group | Family | Files |
  | --- | --- | --- |
  | sans_serif | NotoSansSC | `NotoSansSC-VariableFont_wght.ttf` |
  | sans_serif | NotoSans | `NotoSans-VariableFont_wdth,wght.ttf`, italic `NotoSans-Italic-VariableFont_wdth,wght.ttf` |
  | sans_serif | Roboto | `Roboto-{Regular,Bold,Italic,BoldItalic}.ttf` |
  | sans_serif | SmileySans | `SmileySans-Oblique.ttf` |
  | sans_serif | Bagnard Sans | `BagnardSans.otf` |
  | serif | Merriweather | `Merriweather-{Regular,Bold,Italic,BoldItalic}.ttf` |
  | serif | Wigners Friend | `WignersFriendRoman.ttf` |
  | handwritten | Mali | `Mali-{SemiBold,Bold,SemiBoldItalic,BoldItalic}.ttf` |
  | handwritten | Pacifico | `Pacifico-Regular.ttf` |
  | handwritten | 站酷快乐 | `ZCOOLKuaiLe-Regular.ttf` |
  | handwritten | 站酷小薇 | `ZCOOLXiaoWei-Regular.ttf` |
  | stylized | Runy Tunes | `RunyTunesRevisitedNF.ttf` |
  | stylized | Kalmansk | `Kalmansk-51WVB.otf` |
  | stylized | Radio | `radio.ttf` |
  | stylized | Bloom | `Bloom-Regular.otf` |
  | stylized | LeFun | `LeFunFont-Regular.otf` |
  | accessibility | CalHendersonBraille | `Braille.ttf` |
  | accessibility | AliHealthFontBraille | `AlibabaHealthFont20BRAILLE-A-01.ttf` |
  | accessibility | AliHealthFontPro | `AlibabaHealthFont20PRO-A-01.ttf` |
  | (hidden) | AdobeNotDef | `AND-Regular.ttf` |
  | (hidden) | NotoEmoji | `NotoEmoji-Bold.ttf` |
  | (hidden) | times.ttf | `times.ttf` |

  - Bold: NotoSansSC, NotoSans, Roboto, Merriweather, Mali. Italic: NotoSans, Roboto, Merriweather, Mali.
  - Local fonts (v1.2, `.ttf/.otf/.woff/.woff2`) are stored in a per-user IndexedDB font store (`tR`) by PostScript name. They are used by name in `fontFamily`.
- **Shapes, SVGs, Vectorize:** `nM(fabricObj)` is fabric `toSVG()` under `scale(1,-1)`.
- **Backdrop:** `exportMainSvg()` of the plate shape.
- **Border** (B:7935): the ring between two inward Clipper offsets of the plate outline:

  ```js
  let i = await ia(e, -t),          // inflate(plate, -borderOffset)        (Clipper2 InflatePathsD, JoinType.Round, EndType.Polygon, precision 2)
      a = await ia(e, -t - A),      // inflate(plate, -(borderOffset+borderWidth))
      r = i.exclude(a)              // paper.js boolean → ring
  ```

  So the border sits **inside** the plate, `borderOffset` px from the edge and `borderWidth` px wide. Curves are flattened at 0.01 before Clipper (`n8`). Clipper2's wasm is `…/makerworld/makerlab/content-generator/` (`locateFile`).

#### A.3b 2D → 3D (`h_` renderer, B:33400–33830; `nN`, B:7090)

Each item from `getThreeDeeRenderDataList()` (B:11372) is `{svg, height, offsetZ, left, top, scaleX?, scaleY?, useNaiveWay, renderType, elementType, customDepth, customDepthValue, pathResults?}`.

Processing steps:
1. A three.js `SVGLoader` copy (`oK`) parses each SVG path into shapes.
2. Each shape is extruded with `nV`, a fork of ExtrudeGeometry: `depth=height`, `steps 1`, no bevel, `curveSegments` 12 (50 for `<circle>`).
3. `useManifold:true` triangulates through manifold `CrossSection.ofPolygons(...,"NonZero")` + `triangulate`.
4. Each mesh keeps the path's own fill colour (`MeshStandardMaterial`, roughness 0.5, metalness 0.2). `elementType` is stored in `userData`.

Z placement (`getActualObjectPose` and `prepareModels`):

- **Normal mode:** the backdrop sits at z 0–`backdropHeight`. The border, text, stickers, shapes, SVGs and vectorize parts all start at `offsetZ = backdropHeight` and stand `height` tall (element depth or type thickness).
- **nonePlateMode:** `backdropHeight` becomes 0, so every element starts at z = 0.
- **flatMode** (facedown), for non-backdrop, non-hole items: `height = min(backdropHeight, h)`, `offsetZ = backdropHeight - height`. Each element is **embedded flush with the plate's top face**.
  - The backdrop is not cut. The overlapping volumes are separate parts, and the slicer's part ordering decides colour. **INFERRED:** later parts override earlier ones in Bambu Studio.
  - The 3MF item is then rotated 180° about Y so the face prints on the bed (§3d).
- **Vectorize:** `mergeSameColorDepthMesh` unions every path of the same colour and depth (paper `unite`, then one SVG per group). Per-path depth comes from `pathResults[i].depth`, else `defaultSetting.depth`. In flatMode everything uses the clamped `height`. "transparent" paths are skipped.
- **Order:** items are sorted by canvas z-index, and the mesh `polygonOffsetUnits = -10·index` (display only).

Holes (`classifyModels` + `cutHoles`):

- **InsideHole and BackHole are cutters.** Every Backdrop, BoundaryHole and other model whose bbox meets a cutter has it subtracted with manifold `subtract`.
- **Inside hole depth:** `backdropHeight + 1` in flatMode. Otherwise it is `maxElementHeight + backdropHeight + 1`, starting at z 0. It goes through everything, including text that overlaps it.
- **Back hole:** an extrusion `aS[size]` (2 or 3 mm) tall from z 0, so it is a blind keyhole slot in the underside.
- **BoundaryHole** is a solid tab, extruded to `backdropHeight` in the backdrop colour (it follows backdrop colour changes) and treated like the backdrop.
- **Thickness change** (`changeThickness`): when `backdropHeight` changes, every non-hole element's `offsetZ` is set to the new value, and BoundaryHole `height` is set to `backdropHeight`.

#### A.3c Colour → filament

Colours come from fabric fills: element `fill`, SVG child fills, vectorize palette colours, `backdropColor` and `borderColor`. `usedColors` collects them upper-cased, and `getColorCount()` counts the distinct ones.

#### A.3d 3MF writer (`hV.parse`, B:33006; `export3mf` B:33771)

- **Mesh order:** top-level groups are sorted by `hL` order: Backdrop 0, Border 1, BaseShape 2, SVG 3, Sticker 4, Text 5.
- **Filaments:** each mesh's material colour `#RRGGBB` maps to an extruder number, assigned in order of first appearance (`filamentColour`).
  - `project_settings.filament_colour` is that list. Each `filament_*` array is padded to the colour count by repeating element 0.
- **Profile:**
  - Default `hT` is X1C: `printable_area 0x0..256x256`, `printable_height 250`, bed_exclude 18×28, `print_settings_id "0.20mm Standard @BBL X1C"`, `printer_settings_id "Bambu Lab X1 Carbon 0.4 nozzle"`, `printer_model "Bambu Lab X1 Carbon"`, `curr_bed_type "Textured PEI Plate"`, `filament_settings_id ["Bambu PLA Basic @BBL P1P"]`, `ironing_type "top"`, `wall_generator "arachne"`, `wall_loops 2`, `sparse_infill_density 15%`, `layer_height 0.2`, wipe tower (15, 220).
  - `hW` is A1 mini (180×180×180, `@BBL A1M`, wipe tower (15, 142.333)). It is chosen when `bboxW ≤ 180 && bboxH + u ≤ 180`, where `u = 40` mm for multicolour (wipe-tower margin) and 0 otherwise.
- **Placement:** `transform = "<rot> <c-x> <c-(y+u/2)> <z>"`, with `c = 128` for X1C and 90 otherwise.
  - `rot` is identity, or `"-1 0 0 0 1 0 0 0 -1"` (180° about Y) when flatMode is on.
  - `z` = model depth in flatMode, else 0.
- **Files:**

  | File | Contents |
  | --- | --- |
  | `3D/3dmodel.model` | Metadata: Application `BambuStudio-01.07.04.52`, `MakerWorld_M1`=title, `MakerLab`="Make My Sign", `MakerLabVersion`="NamePlate V1.1", `DesignRegion`/`MakerLabRegion`, `MakerLabFileId`, `DesignModelId`=`FMI_<id>`, Title, CreationDate. One object with `<components>` pointing at `/3D/Objects/object-<id>.model`, one per mesh. Build `item printable=1`. |
  | `3D/Objects/object-<id>.model` | One `<object type="model"><mesh>` per three.js mesh, with vertices baked by parent position and scale. |
  | `3D/_rels/3dmodel.model.rels` | Relationships. |
  | `Metadata/model_settings.config` | Object `name=<title>.3mf`, `extruder 1`, `thumbnail_file Metadata/plate_1.png`. One `<part subtype="normal_part">` per mesh, named `<elementType>[-<groupId>]-<meshId>` with `extruder=<colour index>`. One plate (`plater_id 1`) and an assemble item. |
  | `Metadata/project_settings.config` | JSON of the chosen profile. |
  | `Metadata/plate_1.png` and `Auxiliaries/.thumbnails/thumbnail_3mf.png` | The **2D canvas screenshot** (`canvasUrl`). |
  | `Auxiliaries/Templates/template.json` and `Auxiliaries/Templates/assets/*` | Only when exporting with `shareTemplate`: the full content JSON plus `<keyName>.svg`, `CUSTOM_SVG_SHAPE.svg` and `<postscriptName>.font`. **This is a self-contained project carrier, so a 1:1 import can read it.** |

  The zip comment is "created by BambuLab".
- **STL export:** three's STLExporter in binary. The 80-byte header is `ML <region> SIGN <mlFileId>` (`td`).
- **Export bookkeeping:** every export first registers a file (`AT({generatorId:"SIGN", format, extra:{makeId, backdropShape, backdropBorder, textCount, iconCount, shapeCount, svgCount, holeCount, vectorizeCount}})` → `{region, mlFileId}`) and earns MakerLab credit. In China there is also a censorship check (`POST /my/make/securitycheck`).
- **Headless exporter (B:35571 `up`/`uf`/`uv`):** takes `.json` + `.svg` + `.font` files and produces `nameplate.3mf` with a readonly renderer. That is the same input shape as `template.json` + assets.

### A.4 Vectorize, Keychain Draft and SVG storage

- **Per-make SVG store** (`nw`, B:4349):
  - IndexedDB `"<uid>-<makeId>-content_gen.nameplate:SVG_DB"`, version 1, object store `svgs`, keyPath `keyName`, records `{keyName, content:<svg text>}`.
  - Reference counts live in `localStorage "<uid>-<makeId>-content_gen.nameplate: cached svg"` as `{version:"V1.0", rc:{keyName:count}}`. `prune()` deletes zero-count entries.
  - Duplicating a make copies both (E module 34254), and deleting clears them.
- **Server assets:** `make.assets = {svgAssets:[{name, url, objectName}], imageAssets}`. On load, `nO` (B:4438) downloads every `*.svg` asset into the store under its name minus `.svg`, and every `*.font` asset into the font store.
  - Captured example: `"Keychain Draft_1774223447148.svg"` at `https://makerworld.bblmw.com/makerworld/model/…svg`.
  - **The editor never uploads locally imported SVGs or fonts**, so they exist only in that browser's IndexedDB. Import must fall back to `assets.svgAssets` or a 3MF's `Auxiliaries/Templates`. **INFERRED:** a make with a local-only SVG has no server copy of it.
- **Importing from Image to Keychain:**
  - The list comes from `GET /my/makes?category=<imageToKeychain>&limit=30`.
  - The detail comes from `GET /vectorization/{id}/resource` → `{title, assets.svgAssets[0].url, additionalInfo|content}`.
  - The SVG is then added with `addSvgImage(title, svg, {vectorizeContent})`. The URL parameter `?vectorizeID=<id>` does the same automatically.
- **vectorizeContent** (class `to`, B:1832; `exportData`):

  ```json
  {"pathResults":[{"colorKey":"<key>","depth"?:mm}],          // index = SVG path order (paths with clipMask skipped)
   "defaultSetting":{"depth":1},                               // base thickness, mm
   "globalResult":{"left","top","scale","angle"},              // initial placement (scale→scaleX/Y)
   "paletteMap":{ "<colorKey>":{colorKey, red,green,blue (0..1), alpha, fill, index, isOrigin, isPaletteHidden, deleted, parent?, customColor?{red,green,blue}, children[]} },
   "colorPathMap":{ "<colorKey>":[pathIdx...] },
   "secondaryEditColorReplaceMap":{ "rgb(r, g, b)":"<new css colour>" }}
  ```

  A path's colour is resolved in this order:
  1. `pathResults[i].colorKey` → paletteMap entry. A `customColor` overrides it, and `deleted` entries are ignored.
  2. Otherwise the path fill → paletteMap → `parent`.
  3. The `rgb(...)` string is then passed through `secondaryEditColorReplaceMap`, which holds this editor's recolours (`ChangeVectorizeColor`).
  4. `alpha 0` means transparent and is not built.

  Palette indices are assigned by sorting visible entries by HSL. A hidden `"transparent"` entry is always added. `hasMixedDepth` is true when any path depth differs from `defaultSetting.depth`.

### A.5 API endpoints

The base is `https://makerworld.com/api/v1/creation-service` (confirmed from captured URLs). The `category` for Make My Sign is `1`.

| Method | Path | Use |
|---|---|---|
| GET | `/my/make/{id}` | Load the make `{id,title,cover,coverUrl,content(str),assets{svgAssets,imageAssets},instanceId,productName,categoryName,keyChainTemplateId}`. The page does it server-side (`__N_SSP`) and also calls it to re-read the title. |
| PUT | `/my/make/{id}` | Autosave `{content:"<json>"}`, rename `{title}`, set cover `{cover:<objectName>}`. |
| POST | `/my/make` | "Create from Blank" `{category:1, cover:"", title, content:""}` → `{id}`. |
| POST | `/my/makebytemplate/{templateId}` | "Use template" `{cover:"", title}` → `{id}`. |
| POST | `/my/make/{id}/copy` | Duplicate → `{id}`. The IndexedDB SVGs are copied client-side. |
| DELETE | `/my/make/{id}` | Delete `{id}`. Local storage and IndexedDB are cleared. |
| GET | `/make/{id}/upload?fileName=…&bizType=…&category=1` | Presigned upload config `{presignedUrl,url,fileId,storageInfo.objectName}`. Covers use bizType `nameplateCover` and fileName `cover{id}`; screenshots use `nameplateScreenshot`. |
| PUT | `<presignedUrl>` | S3 upload (`Dy`, bizType makerlab). |
| POST | `/common/permanent_files` | `{makeId,fileIds,categoryId}`, generic save hook. Not used by sign. |
| GET | `/template?offset=0&limit=20&category=1` | Template gallery `{total,hits[{id,title,cover,user,useCount,designId,content}]}`. |
| GET | `/template/{id}` | Template content (`getTemplateData` → `.content`). |
| GET | `/my/makes?offset&limit&category` | List makes (used for the Keychain import list). |
| GET | `/vectorization/{id}/resource` | Keychain make detail for import. |
| GET / POST | `/my/preset` | User settings. |
| POST | `/my/make/securitycheck` | `{text:[…], image:[url]}` → `{isPass}`, China only. |
| POST | `/my/make/statistics` | `{makeId, category:"SIGN"}`, print statistics. |
| POST | `/my/createfromdesign/{designId}` | Exists in the client. **INFERRED** to be "Customize" from a model page, which creates a make from a published design's embedded template. It is not referenced in the sign chunks. |
| — | `downloadByShare` | `d.FBP(instanceId, {type:"download"})`, a design-instance download counter (a different service module). |

Autosave timing (E module 38314):
- The content PUT is debounced 3 s after a change.
- The cover upload happens on the first save, then at most every 30 s, and on unload. `beforeunload` flushes both.
- Ctrl/Cmd+S saves immediately.

### A.6 Versions and migrations

- **Top level:** if the content is a bare array (pre-wrapper format), it is used as `data` (`deserializeData`: `n = e.data ? e.data : e || []`). Missing `thickness` or `globalStatus` keys fall back to the defaults by merging.
- **Element records** are all `"version":"1.0"`, except holes.
  - Holes `"1.0"`: the position is recomputed from `holeType`/`holeSize` against the current backdrop (`createHole` without data).
  - Holes `"2.0"`: the saved fabric `left/top/angle/scale/origin` are applied.
- **HookElement** and holes are filtered out of the main pass, and **HookElement is dropped entirely**. **INFERRED:** it is the old hanging hook, replaced by Boundary holes; the old UI images are `signHook.webp` and `signHolder*.webp` at B:17925.
- **Missing font:** `onFontMissing` fires and the text is rewritten to `fontFamily:"NotoSansSC,AdobeNotDef"`.
- **Variable-font weight** in old records with `"bold"`/`"normal"` is converted to 900/600 (`setupPreProcess`).
- **Release notes in E:** 0.3.0 (multi-select, Braille, arrow keys, adaptive A1 mini profile, triangle plate, top-left hole); later per-element thickness, size inputs, remove plate, alignment, bold/italic; 1.0.0 facedown and curved text; 1.1.0 Keychain import; 1.2.0 local fonts; 1.3.0 base plate, drilling and colour upgrades (free holes, hole V2).

### A.7 Editing features

- **Undo/redo** (`o6`, B:16405): command stack. Commands `ag`: AddElement, ModifyElement, ChangeText, RemoveElement, ChangeHoles, ZoomCanvas, ChangeThickness, ChangePropertyWithTime (merges rapid changes), MultiChangeProperty, BatchRemoveElement, ChangeTextType, MoveElementLevelUp/Down, ChangeVectorizeColor. Every executed command re-serializes the content.
- **Shortcuts** (B:21951; Mod is Cmd on Mac, Ctrl elsewhere):

  | Keys | Action |
  | --- | --- |
  | Mod+Z | Undo |
  | Mod+Y | Redo |
  | Backspace, Del | Delete |
  | Mod+C | Copy |
  | Mod+V | Paste |
  | Mod+S | Save |
  | Mod+A | Select all |
  | Arrow keys | Nudge 1 px (0.2 mm), as a ChangePropertyWithTime command |
  | Shift+click | Multi-select |

  Text editing registers the same keys so they act on the text instead.
- **Copy/paste/duplicate:** `clone()` offsets the copy by `+width/2` in x. The backdrop and holes cannot be copied.
- **Layers:** bring forward/back and front/back (`MoveElementLevelUp/Down`). Text and icons stay above shapes and SVGs (`moveTo(len - textCount)`), and holes are brought to the front.
- **Snapping** (`oq`, B:14296): smart guides with a 4 px/zoom margin, line `#F68066` at 0.75 px. They snap to other objects' edges and centres while moving and scaling.
- **Multi-element alignment** for groups (left/centre/right/top/middle/bottom; the exact set is **INFERRED** from the "align" tracking at B:21241) and text alignment.
- **Lock:** holes only (Lock Holes, Hide Holes). The backdrop group cannot be moved; it only scales from the centre.
- **Size inputs:** W/H in the chosen unit with optional ratio lock (`cX`). Thickness per type has a slider (0..? mm). Per-element thickness uses `data.depth`.
- **Views:**
  - The 2D fabric canvas with a "reference" size overlay (`showReference`).
  - The 3D three.js view: OrbitControls, camera (0,-128,256), grid 256, background `#444444`.
  - Export as 3MF or STL, or "Open in Bambu Studio" through the `wx.postMessage` bridge (`homepage_makerlab_open_3mf_binary`) when inside BBL-Slicer.
- **Units:** mm, cm, inch (metric/imperial).

### A.9 Parity checklist

- [ ] Content JSON `{data, thickness, globalStatus, date, version:"NamePlate V1.1"}`. Also accept a bare-array legacy format.
- [ ] Coordinates: 5 px/mm, origin at plate centre, y-down. Export Y-flip.
- [ ] Backdrop shapes −1..10:
  - [ ] Circle with ellipse scaling
  - [ ] Rectangle with rx corner radius
  - [ ] The 8 bundled SVG shapes (the bundled backdrop outlines)
  - [ ] Custom SVG plate (`CUSTOM_SVG_SHAPE`)
  - [ ] None, which sets nonePlateMode
- [ ] Border: inward Clipper round-join offset ring (offset, width). Weights 0/5/10/20 px. Own colour. Height `shapeHeight` on top of the plate.
- [ ] Text:
  - [ ] HarfBuzz shaping
  - [ ] The 19-family catalogue with bold/italic availability
  - [ ] Variable weights 600/900
  - [ ] Multi-line Textbox with align
  - [ ] Curved text (up/down/circular, draggable `curvePoint`, per-char placement)
  - [ ] AdobeNotDef fallback
  - [ ] Local font upload, stored as `.font` by PostScript name
- [ ] Emoji stickers: NotoEmoji-Bold glyph outlines (monochrome), unicode-emoji-json catalogue, VS15.
- [ ] Basic shapes: circle r80, triangle 80, rect 80, default `#178F51`. Pentagram SVG shape, `#E03D19`.
- [ ] SVG import: fabric group of paths, auto-scale to 450 px max, reject `<clipPath`, optional single-colour override, per-path colours kept.
- [ ] Vectorize (Keychain) import: `vectorizeContent` palette, depth per path, colour replacement, merge by colour+depth.
- [ ] Holes:
  - [ ] Inside: Ø5/Ø10 through-cut
  - [ ] Back: keyhole slot 6×7.6 / 12×13.6 mm, 2/3 mm blind cut from the underside
  - [ ] Boundary: tab 8×9 / 18×26 mm in plate colour
  - [ ] single / single-top-left / double placement tables per shape
  - [ ] Freely movable; out-of-plate holes excluded
  - [ ] Lock and hide
  - [ ] Back-hole thickness warning (≥ depth+1)
- [ ] Thickness per type (defaults 1/0.6/0.4/0.4/1) plus per-element `data.depth`.
- [ ] Z stacking: elements on top of the plate. nonePlateMode at z 0. flatMode (facedown): embedded flush, clamped to plate thickness, printed upside down (rotate 180° Y).
- [ ] Inside/back holes subtract from all meshes they touch (manifold).
- [ ] 3MF:
  - [ ] One object, one part per mesh, extruder per distinct colour
  - [ ] Bambu metadata, `model_settings` and `project_settings` (A1 mini vs X1C by size + 40 mm wipe margin)
  - [ ] Thumbnails from the 2D canvas
  - [ ] Optional `Auxiliaries/Templates/template.json` + assets
- [ ] STL export.
- [ ] Import sources:
  - [ ] Make content + `assets.svgAssets` (`.svg` and `.font`)
  - [ ] Template content
  - [ ] 3MF `Auxiliaries/Templates`
  - [ ] Hole V1 → recompute; V2 → saved transform
  - [ ] Drop HookElement
  - [ ] Missing-font fallback
- [ ] Editor:
  - [ ] Undo/redo command stack
  - [ ] Copy/paste/duplicate (+w/2 offset)
  - [ ] Select-all and multi-select
  - [ ] Arrow-key nudge
  - [ ] Delete
  - [ ] Save shortcut
  - [ ] Layer order (text/icons above shapes)
  - [ ] Smart guides
  - [ ] 45° rotation snap, no skew
  - [ ] Align tools
  - [ ] Size inputs with ratio lock
  - [ ] Units mm/cm/inch
  - [ ] Reference overlay
  - [ ] Zoom 0.01–20 at cursor, pan
  - [ ] 2D/3D view
- [ ] Persistence: debounced autosave (3 s), cover thumbnail, rename, duplicate, delete, user preset (units, showReference).

## Appendix B. MakerLab catalogue

The index at <https://makerworld.com/en/makerlab> was scraped on 2026-10-14, logged in.
It has 39 tiles: 19 first-party and 20 "From Partner". The partner tools are exactly the
Community tab, at `/en/makerlab/community/<slug>`. The tabs are All, AI Generators,
Single-Color Friendly, Multi-Color Friendly, Laser & Cut, Experiments and Community.
Cloudflare's interactive check stopped the headless survey before most editors
opened, so a family below is assigned from the tool's own description and landing
page unless "opened" says otherwise. The survey is to be completed in a visible
browser before the next maker is designed (§13 phase 5).

| Tool | Tabs | Family (§11) | Notes |
|---|---|---|---|
| Make My Sign | Multi | Layered 2D | opened; this spec |
| Image to Keychain | Multi | Layered 2D + vectorize | landing page and release notes: 4/8/12-colour matching, mirror and facedown, contour outline (width, thickness, colour), circle/hexagon crops, export to Make My Sign; 50 projects |
| Lightbox Maker | Multi | Layered 2D | images, text |
| Christmas Ornament Maker | Multi | Layered 2D | 2D spinning ornaments |
| Flexi Toy Maker | Multi | Image → articulated | image to flexi print-in-place |
| Pixel Puzzle Maker | Single | Codes/data (pixel grid) | ScadBuddy has `pixel-art-mosaic` |
| Chroma Canvas | Multi | Height map (colour layers) | image to filament painting |
| Make My Lithophane | Single, Multi | Height map | |
| Lithophane Folding Fan / Round Fan | Community | Height map on a curved surface | partner |
| Relief Sculpture Maker | AI, Single | Height map | |
| PlakHaus | Community | Height map | partner, with paid packs |
| Shadowbox Maker | Multi, Experiments | Height map (depth layers) | |
| Make My Lantern | AI, Single, Experiments | Height map on a cylinder | one image ≤ 10 MB |
| Parametric Model Maker | Single | Parametric | MakerWorld's OpenSCAD-style customizer gallery |
| ParamaCraft lite, Bento3D, LumHaus, Folding Box Generator | Community | Parametric | Bento3D: cell-level layout editor; LumHaus: lamp profile, 3MF/STL; Folding Box: 12 styles, SVG/DXF/flat 3MF |
| Make My Desk Organizer, Make My Vase | Single | Parametric | |
| Stamp Maker, QRFun, Keycap Legend Generator, Clicker Generator, Song Card Generator, Star Map Generator, Personal Memory Card, OpenMe Card Maker | Community | Codes/data | QRFun vs ScadBuddy `wifi-qr-plaque` |
| Polaroid Frame Generator, SnapFrames Lite | Community | Layered 2D / Parametric | SnapFrames splits frames larger than the bed |
| Dice Studio | Community | Parametric + per-face layered 2D | d4–d20, per-face text/emoji/SVG; beyond ScadBuddy `dice` (d6) |
| Laser Box Generator, Laser Studio, Laser Box Maker | Laser & Cut / Community | out of scope (SVG/DXF) | Laser Studio's batch-of-names and QR ideas carry over |
| Mesh Graffiti | Multi | Mesh painting | |
| Image to 3D Model, PrintU, Make My Statue | AI | out of scope | 24–500 credits per generation (`makerlab/credit/configs`) |
