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
  `sign.json` (the MakerWorld document in ScadBuddy's envelope, §4), a generated
  `model.scad`, its layer SVGs and its fonts. Its texts,
  colours, heights and plate size are customizer parameters, so it is customised
  like any template. It also opens back in the Sign Maker for layout edits, and it can
  be ejected to plain source as a mesh model can.
- **A Make My Sign project imports 1:1.** The driving case is the Mahjong coaster,
  <https://makerworld.com/en/models/2573411> ("This model is created by Make My Sign").
  Importing it gives a template that renders the same sign and is geared to that
  design: its parameters are the sign's own. **The sign document is MakerWorld's own
  format, stored verbatim** (§4), so an import is a copy, not a translation, and a
  sign can be written back out as a 3MF that MakerWorld's editor opens (§9.3).
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
- Writing to MakerWorld's API (a sign leaves ScadBuddy as a 3MF file, §9.3).
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

The full reverse-engineered format, geometry semantics, 3MF layout and HTTP API are
in **the format reference**, `docs/makerworld/make-my-sign.md`, with captured example
documents under `docs/makerworld/examples/`. These are the facts the design depends on:

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
  (`public-cdn.bblmw.com/content-generator/fonts/<file>`, 19 families, reference §7).
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
| Sign document `sign.json`: the NamePlate V1.1 document in ScadBuddy's envelope, its types (generated from the format reference §3), schema and validation | `signs/src/doc/` | TS (schema exported as JSON Schema for Python) |
| Importer (make JSON, template JSON or 3MF → `sign.json` plus assets) and exporter (`sign.json` → a MakerWorld-compatible 3MF project) | `signs/src/import/`, `signs/src/export/` | TS |
| Normaliser (shapes, emoji, SVG, vectorize → layer SVGs in mm; text metrics) | `signs/src/normalise/` | TS |
| Compiler (`sign.json` → `model.scad` + `layers/` + `fonts/` manifest) | `signs/src/compile/` | TS |
| The `signs` Temporal worker (activities over the three above) | `signs/src/worker.ts`, Dockerfile target `signs` | TS |
| The Sign Maker template (`models/sign-maker`: UI, pipeline, `model.scad` shim) | `models/sign-maker/` | JS (bundled from `signs/src/editor/`), Python pipeline |
| Operations: create a template from a sign, edit, eject, import | `backend/scadbuddy/signs/` | Python |
| `ctx.render(..., files=)` overlay and `ctx.service(...)` (§7.2) | `backend/scadbuddy/workflows/` | Python |
| Agent tools | `agent/src/tools/signs.ts` | TS |

## 4. The sign document (`sign.json`, envelope version 1)

The source of truth for a sign, as `scene.json` is for a mesh model. It is **MakerWorld's
own "NamePlate V1.1" document, stored verbatim**, inside a small ScadBuddy envelope. The
format reference (`docs/makerworld/make-my-sign.md`) is the authority on the MakerWorld
half: every element type, field, unit and default, and what the geometry does with each.
This section defines only the envelope and the rules around it.

Why verbatim, not a translation: MakerWorld's editor rebuilds every record from its
Fabric.js objects on each save (reference §11), so a document can only round-trip if
ScadBuddy keeps it exactly as the editor writes it. Keeping it also makes import a copy,
makes the gallery templates usable as they are, and lets the Sign Maker's canvas load
the same Fabric object shapes without a mapping layer.

```json
{
  "version": 1,
  "makerworld": {
    "data": [
      {"type": "BackdropElement", "version": "1.0", "shape": 0,
       "group": {"mainObj": {"type": "circle", "left": -247.5, "top": -247.5, "radius": 247.5, "fill": "#FFFFFF", …},
                 "backdropColor": "#FFFFFF", "borderColor": "#000000", "borderWidth": 10, "borderOffset": 0, "outlineEnabled": true}},
      {"type": "VectorizeElement", "version": "1.0", "keyName": "Keychain Draft_1774223447148",
       "props": {"left": -206.66, "top": -165.95, "width": 905.25, "height": 604.18, "scaleX": 0.4566, "scaleY": 0.4844,
                 "angle": 0, "flipX": false, "flipY": false, "data": {"depth": 1}, "vectorizeContent": "{…}"}}
    ],
    "thickness": {"backdropHeight": 3, "textHeight": 0.6, "shapeHeight": 0.4, "svgHeight": 0.4, "holeHeight": 1},
    "globalStatus": {"flatMode": true, "nonePlateMode": false},
    "date": 1760000000000,
    "version": "NamePlate V1.1"
  },
  "scadbuddy": {
    "source": {"kind": "makerworld_3mf", "make_id": 85233397, "instance_id": 2837289,
               "model_url": "https://makerworld.com/en/models/2573411", "imported_at": "2026-10-14T15:19:50Z"},
    "assets": {
      "Keychain Draft_1774223447148": {"asset": "a_01HX…", "sha256": "…", "media_type": "image/svg+xml"}
    },
    "elements": {"1": {"overlap": "occlude"}},
    "expose": {"plate_colour": {"from": "backdrop.backdropColor"}, "plate_height": {"from": "thickness.backdropHeight", "min": 0.6, "max": 10}},
    "generated_sha256": "…"
  }
}
```

That is the Mahjong coaster as imported from its 3MF (`docs/makerworld/examples/3mf-template.json`
is the `makerworld` half, byte for byte).

- **`makerworld`** is the document as received or as the Sign Maker's canvas serialises
  it: canvas px (5 px = 1 mm), Y down, Fabric.js 5.3.0 object JSON, element records in
  z-order. ScadBuddy never rewrites a field it does not own. Values the editor
  regenerates on load (a textbox's `width`/`height`, a path's bbox, a curved text's
  `path`, an icon's outline; reference §3.2) are regenerated here too and never read as
  truth.
- **`scadbuddy.source`**: where the sign came from: `makerworld_make`,
  `makerworld_3mf`, `makerworld_template` or `blank`, with the ids known.
- **`scadbuddy.assets`**: the bytes the document names but does not hold. Keyed by the
  record's `keyName` (an `SvgImageElement` or `VectorizeElement`), `CUSTOM_SVG_SHAPE`
  (a custom plate) or `font:<postscriptName>` (a local font), each a pointer into the
  asset store (`library/assets.py`): `{asset, sha256, media_type}`. Never inline bytes.
- **`scadbuddy.elements`**: per-element settings the format has no slot for, keyed by
  the record's index in `data` (records carry no id). Today one key: `overlap:
  "occlude" | "stack"` for `SvgImageElement` and `VectorizeElement`, default `occlude`
  (§5.2). An import sets `occlude`. The canvas re-keys this map when it reorders
  `data`.
- **`scadbuddy.expose`**: which values become customizer parameters of a generated
  template (as spec 2026-10-11 §3.2 exposes scene values): a parameter name → a path
  into `makerworld` (`backdrop.backdropColor`, `thickness.backdropHeight`,
  `data[2].fabricObj.text`, `data[2].fabricObj.fill`) with optional `min`/`max`. "Save
  as template" exposes by default: each straight text's string and colour, every
  colour, the plate's size, height and border, and each type's thickness. A name must
  be unique and a valid identifier.
- **`scadbuddy.generated_sha256`**: the hand-edit check (§5.6).
- **Coordinates and units** are MakerWorld's. The compiler, the normaliser and the
  canvas all read px and Y down and convert at the boundary (reference §4). Nothing
  stores mm.
- **Colour:** any hex colour; no fixed palette. Elements with the same colour print
  with the same filament. The colour list is derived from the document, never stored.
- **Caps** (`signs/src/doc/caps.ts`, mirrored in Python):
  - 200 element records;
  - 64 colour layers per element;
  - 2 MiB per asset SVG and 16 MiB of assets per sign;
  - 8 fonts;
  - text of 500 characters.
- **Validation:** `makerworld` is checked against a schema generated from the
  reference's field tables (one TypeScript type per element record, `signs/src/doc/`),
  strict about the fields that drive geometry and permissive about Fabric's cosmetic
  ones, so a record from a newer MakerWorld build with an extra Fabric key still loads.
  An unknown `type` is refused with its index (§10). The envelope is checked strictly.
  Both are exported as JSON Schema to `backend/scadbuddy/signs/sign.schema.json` for the
  Python side; a freshness test compares the two.
- **Legacy content:** a bare array is read as `data`; missing `thickness` or
  `globalStatus` keys take the defaults (reference §9). The stored `makerworld` is the
  normalised form, so an export always writes the full shape.

## 5. The compiler

`compile(sign) → {source: string, files: {path: bytes}, parameters: [...]}`, a pure and
deterministic function in `signs/src/compile/`. It emits a plain `.scad`, so the
render, versions, Arrange and print paths see an ordinary template and need no change.

### 5.1 Output layout

```
models/<slug>/
  model.json      # name, description, tags, "sign": {"version": 1}, plus the source's provenance and licence
  sign.json       # source of truth (§4): the MakerWorld document plus the envelope
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
  // e2 overlaps e1 and is later in `elements`, so e1's block gives up e2's footprint
  color("#050504") difference() { translate([0, 8, plate_height - 1]) linear_extrude(1) e1_0(); translate([0, -15, plate_height - 1]) linear_extrude(1) e2(); cutters(); }
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
  `data`) owns the shared volume; the lower one is `difference()`d by the upper
  one's footprint over the shared height. No volume ever has two colours.
- **Colours inside one SVG or vector element follow its `scadbuddy.elements[i].overlap` setting:**
  - `occlude` (the default, as MakerWorld draws it): a later colour is cut out of the
    earlier ones, so each point of the element shows one colour.
  - `stack`: each colour keeps its whole outline, and the colours are stacked in paint
    order at successive heights within the element's depth. Where they overlap, the
    print has thin layers of each filament, and the layers blend. Each height band
    still holds one colour, so no volume has two.
- **Facedown inlays every element from the plate's top face down to its own depth,**
  clamped to the plate's height, as MakerWorld clamps it (`min(backdropHeight, h)`,
  reference §5.1).
  The plate is `difference()`d by the element footprints over that depth, so each
  element sits flush in its pocket. **This deliberately differs from MakerWorld:** its
  3MF leaves the plate whole and lets overlapping parts share volume, so the slicer's
  part order decides the colour there. OpenSCAD's per-colour solids must not overlap
  (CLAUDE.md), so ScadBuddy cuts. The printed result is the same. The whole sign is then turned face down:
  `translate([0, 0, plate_height]) rotate([0, 180, 0])` is the same matrix as
  MakerWorld's build transform (§2.3), so the face prints on the bed with no slicer
  step. The border inlays the same way, to its own depth (§2.3 measured 0.4 mm).
- **Without facedown, elements stand on the plate's top face** (z from
  `backdropHeight` to `backdropHeight` plus the element's depth).

### 5.3 Text

- **Straight text compiles to `text()`,** with the font file shipped and `use`d (§2.1),
  so its string stays a live parameter. The compiler measures the layout with
  harfbuzzjs (the HarfBuzz WebAssembly build MakerWorld itself shapes with) from the
  same font file, mirroring Fabric's Textbox:
  - wrapping at the box width;
  - `lineHeight × fontSize × 1.13` between lines (Fabric's `_fontSizeMult`);
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
  those faces (reference §7), and so does the Sign Maker, so nothing is synthesised.
  OpenSCAD reaches a variable font's weight by its named instance
  (`font = "Noto Sans SC:style=Black"`). That a named instance renders at the weight
  MakerWorld uses is measured in the verify script (§12); a font with no matching
  instance ships the static face file instead.
- **Braille fonts** (three in MakerWorld) are fonts like any other.
- **Icons (emoji)** compile to `text("<emoji>", font = "Noto Emoji:style=Bold")`, with
  `NotoEmoji-Bold.ttf` shipped and `use`d, which is the glyph MakerWorld outlines.
  The emoji is then a live parameter too. The picker uses the same unicode-emoji-json
  catalogue and groups (reference §3.2).
- **Fonts for imports** come from the same CDN files MakerWorld uses
  (`public-cdn.bblmw.com/content-generator/fonts/`, one of the two hosts on
  `signs_fetch_assets`' allowlist, §6.2), or from the project's `.font` assets for a
  local font.
  - Licences are decided per file, not per group. `signs/fonts/licences.json` lists
    each catalogue file (reference §7) with its licence and a link to where it was
    checked. Only a file listed there as redistributable (for example OFL-1.1 or
    Apache-2.0) is committed with a generated template, and each committed font's
    licence is recorded in the template's `model.json`.
  - Every other file is fetched for rendering but kept out of the committed files.
    The template names the font, and the file goes to the ScadBuddy font install
    path (`library/font_operations.py`) instead. That covers anything unchecked or
    not redistributable, such as the stylized group, the Alibaba Health and
    CalHenderson Braille files until their licences are checked, and the hidden
    `times.ttf` (Fabric's `"times new roman"` default), which is never committed.
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

- **Plate outlines** (reference §3.2, BackdropElement):
  - None;
  - Circle, an ellipse once scaled;
  - Rectangle, with a corner radius;
  - eight outline shapes: Star, Heart, Cat, NumberPlate, Bone, ClosedHanger,
    OpenHanger, Triangle;
  - Custom, from an SVG.

  The eight outline shapes ship in `signs/src/shapes/` as path data. Their geometry is
  reproduced from the editor's own outlines, so an imported plate
  matches. The PR that adds them names this provenance for review.
- **The border is a ring inside the plate,** as MakerWorld draws it: `offset(r =
  -o) outline` minus `offset(r = -(o + w)) outline`. Here `o` is `borderOffset` and
  `w` the weight: None 0, Light 1, Medium 2, Bold 4 mm. Clipper's round joins become
  OpenSCAD's `offset(r=)`. The ring stands `shapeHeight` on the plate, or is inlaid
  that deep in facedown (§2.3 measured 0.4 mm).
- **Holes** (reference §3.2, BaseHoleElement, and §5.6):
  - `inside`: Ø5 or Ø10 mm, cut through the plate and through every element above it.
  - `back`: a keyhole slot, 6×7.6 or 12×13.6 mm, cut 2 or 3 mm up from the underside.
    The editor warns when the plate is thinner than the cut + 1 mm.
  - `boundary`: a tab outside the outline, 8×9 or 18×26 mm, in the plate's colour
    and height, with its hole.
  - Positions are single top, single top-left or double top, from per-shape tables.
    A hole the user has moved keeps its saved transform (hole record 2.0). A hole not
    fully inside the plate is drawn red and left out of the 3D, as in MakerWorld.

### 5.6 Determinism and the hand-edit check

`sign.json` records `scadbuddy.generated_sha256`, the hash of the text the compiler last wrote, as
mesh models do (spec 2026-10-11 §5). An edit is refused with 409 "model.scad was edited
by hand; eject the sign to keep editing it as source" when the hash differs. A newer
compiler emitting different text never reads an existing sign as hand-edited: the
check compares the stored hash, never a fresh compile.

## 6. TypeScript on the server: the `signs` worker

### 6.1 The package

`signs/` is a new pnpm package (Node 24, as the other two; corepack). Its dependencies
are pure JS or WebAssembly, so it bundles for both Node and the browser:

- `harfbuzzjs` (shaping and glyph metrics; WebAssembly, runs in Node and the browser)
- `clipper2-ts` (offsets, booleans, stroke-to-fill; `clipper2-js` was measured wrong on
  offsets and point-in-polygon and replaced)
- `@xmldom/xmldom` (the SVG normaliser's parser, external entities refused)
- `@temporalio/*` (worker only, pinned to the agent's exact version)
- `fabric@5.3.0` in the editor bundle only

It has no native addons. Its modules are `doc/`, `import/`, `export/`, `geom/`, `shapes/`,
`normalise/`, `text/`, `compile/`, `editor/` (§7.1) and `worker.ts`. It builds two bundles:

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
| `signs_import` | `{kind: "makerworld_make" \| "makerworld_3mf" \| "makerworld_template", document, files}` (the make JSON, a 3MF's `template.json`, or a template's content; the SVGs and fonts as blob pointers) | `{sign, files, warnings}` (the envelope of §4, every `keyName` resolved) |
| `signs_export` | `{sign, files}` | `{template_json, files: {"assets/<keyName>.svg" \| "assets/<psName>.font": Blob}}`, the `Auxiliaries/Templates/` half of a MakerWorld 3MF (§9.3) |
| `signs_normalise` | `{sign, files}` (an edited sign whose new uploads are raw) | `{sign, files, warnings}` |
| `signs_compile` | `{sign, files}` (raw uploads allowed: it runs `signs_normalise`'s step first) | `{sign, source, files, parameters, sha256, warnings}` (`sign` is the normalised document) |
| `signs_fetch_assets` | `{urls}` (public MakerWorld asset URLs only: `makerworld.bblmw.com` for SVG assets and `public-cdn.bblmw.com/content-generator/fonts/` for fonts; capped size, no redirects off-host) | `{files: {name: Blob}}` |

Rules for the activities:

- **Bytes move as blob pointers, never inline in payloads.** Arguments and results are
  JSON. Each large file is `{name, mediaType}` plus a blob key in the content store (the
  CLAUDE.md rule against base64 images in JSON, `lint-base64-images.sh`). The worker
  reads and writes blobs through the backend's internal blob routes, the same
  `SCADBUDDY_API_INTERNAL_URL` channel the print worker uses
  (`bambuddy/output_reader.py` `RemoteOutputs`).
- **No credentials beyond the API's internal key.** The worker holds no Bambuddy key,
  no KEK and no Claude credential. Its only outward reach is `signs_fetch_assets`,
  to the two hosts its row names: `makerworld.bblmw.com` (SVG assets) and
  `public-cdn.bblmw.com` (fonts, under `/content-generator/fonts/` only).
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
full-page editor reproducing Make My Sign's layout (reference §2 and the checklist in Appendix A):

- **Top toolbar:** Text, Emoji, Shape, SVG, Hole, Plate, Thickness, plus Import (§9).
- **Left rail:** select, pan, copy, paste, undo, redo, delete.
- **Top right:** 2D/3D, zoom, settings (units metric/imperial and mm/cm, show plate
  reference), Generate, Save as template.
- **A properties panel** for the selected element or tool.
- **A printer/bed selector** showing the bed outline.

Behaviour:

- **The canvas** is Fabric.js 5.3.0 over the document: it loads `makerworld.data`
  directly, record by record, with the same object shapes MakerWorld's editor uses
  (reference §3.2), and serialises back the same way. It shows a live 2D view of the
  same normalised layers the server compiles, with no server round trip per drag.
- **3D** is the existing `<sb-preview>` of the last render.
- **Generate** is `<sb-generate>`.
- **Editing:** multi-select (Shift-click, Ctrl+A), align and distribute, layer order,
  flip, rotation with snap, arrow-key nudge, numeric size input, lock, undo/redo
  (command stack: add, modify, remove, batch remove).
- **State:** the document is not stored inline. `render/inputs.py` measures all of
  `inputs` against `MAX_INPUTS_BYTES` (64 KB), and §4's caps allow a sign well past
  that. So the UI uploads `sign.json` to the asset store (`library/assets.py`) on each
  change (debounced), and `inputs.sign` holds only the pointer
  `{"asset": <id>, "sha256": <hex>, "name": "sign.json", "mediaType": "application/json"}`.
  The exposed parameters stay scalars under `params`. Uploaded SVGs and fonts go
  to the asset store the same way, and the document names them by asset id, never
  inline.
- **Host API additions,** all minor (spec 2026-09-27 §8.1): `host.assets.upload(file)
  → {id, url}` and `host.signs.saveAsTemplate({name, description, tags}) → {slug}`. A
  template other than the Sign Maker cannot call the second: the route checks that
  the template is a template maker, with `"maker": true` in `model.json`. That flag
  is a template's claim about itself, so it is not a security boundary. It only
  keeps other templates' UIs from offering the action. The route's authority is the
  same as any library write (`model_create`), which the browser user already holds.

### 7.2 Preview and Generate

The Sign Maker's `pipeline/pipeline.py`:

```python
async def run(ctx, inputs):
    # inputs["sign"] is an asset pointer (§7.1); signs_compile reads it, normalises
    # any raw uploads, and compiles, so the preview needs no separate normalise call.
    compiled = await ctx.service("signs_compile", {"sign": inputs["sign"]})
    part = await ctx.render("model.scad", files=compiled["files"])
    await ctx.output(plates=[ctx.plate_of([part])], name=compiled["sign"].get("name"))
```

This adds two generic `ctx` primitives (spec 2026-09-27 §5.2, minor additions):

- **`ctx.service(name, arg)`** runs a host service activity: a TypeScript activity on
  its own queue, from an allowlist in `workflows/services.py`. Phase 1's list is
  exactly `signs_normalise` and `signs_compile`, which are pure functions of their
  input and reach no network. `signs_import` and `signs_fetch_assets` are not on it:
  only the `sign_import` operation (§8) calls them. Template code cannot name an
  arbitrary activity or queue.
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
2. write `model.json`, `sign.json` (the envelope, with its `assets` copied into the
   model directory and re-pointed), `model.scad`, `layers/` and `fonts/`;
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

## 9. Import and export (1:1)

### 9.1 Sources

`sign_import` is an `OperationKind`; the Sign Maker's Import button and the agent call
it. It accepts:

- **A Make My Sign 3MF** (the primary path). This is what the editor's Download writes
  with "Include customizable file" on (§2.3, reference §6): `Auxiliaries/Templates/template.json`
  plus its `assets/`. It needs no MakerWorld access at all, and it is also how a 3MF
  already in the Bambuddy library imports (`library_file_id`, read as Arrange reads
  library files). A published model's own 3MF, from a model created by Make My Sign,
  is expected to carry the same files; that is checked against the Mahjong model when
  the fixtures are collected (§12). A 3MF without `template.json` is refused with
  "this 3MF has no Make My Sign project; open it as a mesh model instead".
- **The make JSON**, the body of `GET /api/v1/creation-service/my/make/<id>`. The user
  saves it from a browser logged in to MakerWorld; the import dialog shows how. The
  server cannot read a user's makes, since it holds no MakerWorld session and never
  will. Its `assets.svgAssets[]` are fetched by `signs_fetch_assets`.
- **A MakerWorld template's content** (`GET /api/v1/creation-service/template/<id>`),
  saved the same way. The 12 gallery templates are test fixtures only, since they are
  other people's designs. The Sign Maker's own starter gallery is signs authored for
  ScadBuddy, under `models/sign-maker/starters/`, in the same format.
- **The files the document names but the source lacks:** a custom plate SVG, a locally
  imported SVG or font that was never uploaded (§2.3). The import reports each missing
  `keyName` and asks for the file; the sign opens with the element marked missing
  until it is supplied.

### 9.2 What happens

1. **The document is copied, not mapped.** `makerworld` is the `content` string parsed
   (or `template.json`), with only the loader's normalisation applied: a bare array
   becomes `data`, missing `thickness`/`globalStatus` keys take the defaults,
   `HookElement` records are dropped with a warning, as MakerWorld drops them
   (reference §9). Nothing else is touched: px stay px, Fabric fields stay as written.
2. **Assets resolve.** Every `keyName`, `CUSTOM_SVG_SHAPE` and local font named in the
   document is matched to a file from the source (a 3MF's `assets/`, a make's
   `svgAssets`, or the user's upload), stored in the asset store and pointed at from
   `scadbuddy.assets`. Catalogue fonts resolve as §5.3 says.
3. **Validation** (§4) refuses an unknown record `type` by index; a record with a
   known type and an unexpected extra field loads (§10).
4. `scadbuddy.source` records the make, instance and model URL; `scadbuddy.elements`
   gets `occlude` for each SVG and vector record; `expose` gets the defaults of §4.
5. The result opens in the Sign Maker as an unsaved sign, or becomes a template
   directly (`sign_template_create` with the import as its source). The template's
   `model.json` records the MakerWorld licence when the content states one (the
   Mahjong model is a Standard Digital File License: personal use only, which the
   README repeats).

**Fidelity check:** for a 1:1 import, the compiled sign's footprint per colour matches
MakerWorld's own exported 3MF for the same project. Footprint means area and bounding
box in XY, within 1 % of area and 0.2 mm of bounds. §2.3 and the reference §5 give
MakerWorld's side. The Mahjong make's export (`mahjong.3mf`, 2.9 MB, collected
2026-10-14) and the 12 gallery templates are fixtures: their JSON, their SVG assets and
MakerWorld's 3MF for each go under `test-fixtures/makerworld/sign/` (Git LFS,
`manifest.json` entries, `requires_fixtures`).

**Round trip check:** importing any fixture and exporting it again (§9.3) gives a
`template.json` byte-identical to the source, and the same `assets/` set.

### 9.3 Export

A sign leaves ScadBuddy as a MakerWorld-compatible project: `signs_export` writes
`makerworld` as `Auxiliaries/Templates/template.json` and the `scadbuddy.assets` as
`Auxiliaries/Templates/assets/<keyName>.svg` and `<postscriptName>.font`, which is
exactly what MakerWorld's own headless exporter and editor read (reference §6). The
output's `Download` gains "Download as Make My Sign project (3MF)", which packs those
files into the rendered 3MF ScadBuddy already writes, beside the meshes. Opening that
file's project in MakerWorld's editor is the parity test for the editor (§12).

## 10. Errors

- **A sign that fails validation** is a 422 naming the JSON pointer of the bad field.
- **A sign past a cap** (§4) is a 422 naming the cap.
- **An import with an element `type` it does not know** (a future MakerWorld format)
  imports everything else and reports each dropped record by type and index in the
  operation's `warnings`, shown in the import dialog before saving. It is never
  silently dropped. A known type with an unexpected Fabric field loads and keeps the
  field (§4).
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
  - importer tests: each fixture's content → `sign.json`, with `makerworld` equal to
    the source after loader normalisation, and the §9.2 round trip byte-identical;
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
  - a manual parity check, recorded in the PR: a sign exported from the Sign Maker
    (§9.3) opens in MakerWorld's editor and looks the same.
  - `e2e/real-backend.spec.ts` gains the same against a container.

## 13. Phases

1. **`signs/` package:** the document types from the format reference, the loader,
   normaliser, text layout and compiler reading the MakerWorld document; golden tests;
   the `signs` worker and Dockerfile target; the CI job.
2. **Backend:** `ctx.service`, `ctx.render(files=)`, the sign operations, the
   in-process dev child; the agent tools.
3. **The Sign Maker template:** the editor with full parity (Appendix A),
   preview and Generate, and Save as template.
4. **Import and export:** the 3MF and make JSON paths, the fixtures (the Mahjong
   coaster first), the fidelity measurement, the round trip and the 3MF export.
5. **Follow-ups filed as issues:** standalone activities once production Temporal
   supports them (§6.3); the clusters Deployment; Image to Keychain as the next maker
   (§11).

## Appendix A. Make My Sign parity checklist

The reverse-engineered format, geometry, 3MF and API that used to be here are now the
format reference, `docs/makerworld/make-my-sign.md`, with the captured documents under
`docs/makerworld/examples/`. This appendix keeps only the checklist the Sign Maker is
measured against.

### A.1 Parity checklist

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
  - [ ] Boundary: tab 8×16 / 18×26 mm in plate colour (the editor's Fabric extent; the SVG declares 45 px)
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
