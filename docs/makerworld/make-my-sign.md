# MakerWorld "Make My Sign": document format and protocol

A reference for the document MakerWorld's Make My Sign editor stores, the geometry
it builds from it, the 3MF it exports and the HTTP API behind it. ScadBuddy's Sign
Maker (#3468) stores this document verbatim as its own sign document, imports a
make or an exported 3MF without a mapping step, and writes it back out.

## 1. Provenance

Everything here was read from the shipped editor bundle, MakerWorld build
`arlj243Es7etS3BsJ5nsQ`, captured 2026-10-14, and checked against:

- the live editor in a browser session (network capture of every request the edit
  page makes);
- the document of the author's own make 85233397 ("mahjong", `examples/make-85233397.content.json`);
- the 12 gallery templates, `GET /template/15` … `/template/26`
  (`examples/template-26.content.json` is one of them);
- the 3MF the editor exported for make 85233397, with "Include customizable file in
  3mf" on (`examples/3mf-template.json` is its `Auxiliaries/Templates/template.json`);
- one record of each element type as the gallery stores it (`examples/element-examples.json`).

Citations: `B:<line>` is the editor chunk `static/chunks/b0b707fb.05a1eeeeedd6a707.js`
(one scope-hoisted module), `E:<module>` the edit page
`pages/makerlab/makeMySign/edit/[id]-f97cf818d5000b7d.js`, both after prettier
formatting; `fabric:<line>` is Fabric.js 5.3.0 in chunk `a4069263`. `captured:` names a
file under `examples/`. A fact that could not be read from code is marked
**INFERRED**.

The bundle is not redistributed here. Line numbers drift with every MakerWorld deploy;
the identifiers (`aO`, `iI`, …) are minified names and drift too, but the string
literals quoted beside them do not.

## 2. A make

A *make* is one sign project on the server: `{id, title, cover, coverUrl, content,
assets, instanceId, productName, categoryName, keyChainTemplateId}` (`GET
/my/make/{id}`, §8). `content` is a JSON **string** holding the document of §3.
`assets.svgAssets[]` and `imageAssets[]` are files the server holds for it
(`{name, url, objectName}`); the editor never uploads a locally imported SVG or font
there (§7), so a make can depend on files that exist only in one browser.

Where the document lives while editing (B:11032, B:4349, E:38314):

| Store | Key | Holds |
| --- | --- | --- |
| Server | `PUT /my/make/{id} {content}` | The document, autosaved 3 s after a change; on `beforeunload`; on Mod+S. |
| `localStorage` | `<uid>-<makeId>-content_gen.nameplate: saved data` | A mirror of the last serialised document. |
| `localStorage` | `<uid>-<makeId>-content_gen.nameplate: cached svg` | `{version:"V1.0", rc:{<keyName>: <refcount>}}` for the SVG store. |
| IndexedDB | `<uid>-<makeId>-content_gen.nameplate:SVG_DB`, store `svgs`, keyPath `keyName` | `{keyName, content:<svg text>}` for every imported SVG, the custom plate and vectorize imports. |
| IndexedDB | per-user font store | Locally uploaded fonts by PostScript name (v1.2). |
| `localStorage` | `<uid>-<id>-content_gen.nameplate: user setting` | A copy of the user preset (§3.6). |
| `localStorage` | `canvas-transform-state` | The viewport zoom and pan. |

The cover image (`bizType nameplateCover`, file `cover<id>`) is uploaded on the first
save, then at most every 30 s, and on unload (E:38314). Deleting a make clears the
local stores; duplicating one (`POST /my/make/{id}/copy`) copies the IndexedDB entries
client-side (E:34254).

Elements are held in the editor as Fabric.js objects on one canvas; the document is
rebuilt from them on every executed command (`changeSerializedData`, B:11032). That has
a consequence for anyone extending the format, in §11.

## 3. The document ("NamePlate V1.1")

### 3.1 Top level

Built by `aO.changeSerializedData` (B:11032):

```json
{
  "data": [ <element record>, … ],
  "thickness": { "backdropHeight": 1, "textHeight": 0.6, "shapeHeight": 0.4, "svgHeight": 0.4, "holeHeight": 1 },
  "globalStatus": { "flatMode": false, "nonePlateMode": false },
  "date": 1760000000000,
  "version": "NamePlate V1.1"
}
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `data` | array | `[]` | Element records in canvas z-order, back to front. The backdrop is always first (B:11060 sorts by `canvas._objects` index). |
| `thickness` | object, mm | `{backdropHeight:1, textHeight:0.6, shapeHeight:0.4, svgHeight:0.4, holeHeight:1}` (B:11015) | One height per render type, §5.1. `holeHeight` is stored and never read. |
| `globalStatus.flatMode` | bool | `false` | "Facedown Mode": every element is sunk into the plate's top face and the sign prints face down (B:34969, §5.5). |
| `globalStatus.nonePlateMode` | bool | `false` | Forced on while the backdrop shape is None (B:29519); disables flatMode. |
| `date` | number | `Date.now()` | Epoch ms of the last serialisation. |
| `version` | string | `"NamePlate V1.1"` | The only version string in the code. |

Loader rules (`deserializeData`, B:16970): a bare array is accepted as `data`;
missing `thickness` or `globalStatus` keys are filled from the defaults by object
spread (so `examples/template-26.content.json`, which has no `globalStatus`, loads).
`null` records are skipped. `BaseHoleElement` records are loaded in a second pass and
`HookElement` records are dropped (§9).

The gallery's 12 templates use, in total: BaseShapeElement 63, IconElement 37,
TextElement 32, BackdropElement 12, BaseHoleElement 4 (captured: `GET /template/15..26`).

### 3.2 Element records

Each record is `{version, type, …type fields…}` plus, for everything but the backdrop
and v1 holes, a Fabric.js object serialised by Fabric's `toObject()` (Fabric 5.3.0,
`"version": "5.3.0"` inside it). The type ids in code are `iE` (B:8708): None 0,
Backdrop 1, Text 2, Icon 3, BaseShape 4, SvgShape 5, BoundaryHole 6, InsideHole 7,
BackHole 8, SvgImage 9, Group 10, Vectorize 11. The loader table `o7` (B:16398) maps
the `type` string to a class.

#### The Fabric object

These keys appear in every `fabricObj` / `mainObj` / `props` (captured:
`element-examples.json`). The ones that matter for geometry are marked; the rest are
Fabric's defaults, written by `toObject()` and ignored on load unless noted.

| Key | Type | Geometry | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | Fabric class: `circle`, `rect`, `triangle`, `textbox`, `text`, `path`, `group`, `object`. |
| `left`, `top` | px | yes | Position of the `originX`/`originY` corner, canvas px, Y down, origin at the plate centre (§4). |
| `width`, `height` | px | yes | Unscaled size. For `path` and `group` it is the path bbox. |
| `scaleX`, `scaleY` | number | yes | Applied about the origin corner. Resizing in the editor changes these, not `width`/`height`. |
| `angle` | degrees | yes | Clockwise on screen (Y down). |
| `flipX`, `flipY` | bool | yes | Mirror. |
| `originX`, `originY` | `left`/`center`/`right`, `top`/`center`/`bottom` | yes | Which corner `left`/`top` names. Normally `left`/`top`; a mirrored double hole uses `originX:"right"` (§5.6). |
| `skewX`, `skewY` | degrees | no | Always 0; skewing is locked in the editor. |
| `fill` | CSS colour | yes | The element's colour, and so its filament (§5.7). |
| `stroke`, `strokeWidth`, `strokeLineCap`, `strokeLineJoin`, `strokeMiterLimit`, `strokeDashArray`, `strokeDashOffset`, `strokeUniform` | | no | Never drawn in 3D. `strokeWidth` is still 1 on most objects (Fabric's default) and 0 on the backdrop, which matters for `getScaledWidth()` in curved text (§5.3). |
| `opacity`, `visible`, `shadow`, `backgroundColor`, `fillRule`, `paintFirst`, `globalCompositeOperation` | | no | Fabric defaults. |
| `version` | `"5.3.0"` | no | Fabric's own version stamp. |
| `data` | object | yes | Custom: `{depth: <mm> \| undefined}`, the per-element height override (§5.1). Rewritten to exactly that on every save (B:8904). |
| `rx`, `ry` | px | yes | `rect` only: corner radii. |
| `radius`, `startAngle`, `endAngle` | | yes | `circle` only. |

Derived values that an importer must not trust: `width`/`height` of a `textbox` (Fabric
recomputes them from the text and font on load), `width`/`height` of a `path` or
`group` (recomputed from the path data), and everything the editor regenerates from
other fields: a curved text's `path` (from `curvePoint`), an icon's outline (from
`content`), a backdrop SVG shape's outline (from `shape`).

#### BackdropElement (`iI`, B:8936)

```json
{"version":"1.0", "type":"BackdropElement", "shape": <int>, "group": {…}}
```

| `shape` | Name | `group` form |
| --- | --- | --- |
| -1 | None | `{outlineEnabled:false, borderOffset, borderWidth, borderColor, backdropColor, mainObj:<invisible fabric object>}` |
| 0 | Circle | `{outlineEnabled, borderOffset, borderWidth, borderColor, backdropColor, mainObj:<fabric circle>}` |
| 1 | Rectangle | the same with `mainObj:<fabric rect>` |
| 2 | Star | `{outlineEnabled, borderOffset, borderWidth, borderColor, backdropColor, shape, shapeUrl, width, height}` |
| 3 | Heart | ″ |
| 4 | Cat | ″ |
| 5 | NumberPlate | ″ |
| 6 | Bone | ″ |
| 7 | ClosedHanger | ″ |
| 8 | OpenHanger | ″ |
| 9 | Triangle | ″ |
| 10 | Custom | ″, the SVG read from the SVG store under key `CUSTOM_SVG_SHAPE` |

Values (`n7`, B:7780; defaults from the `is` base class, B:7968):

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `outlineEnabled` | bool | `true` | The border ring is drawn. Choosing weight None turns it off. |
| `borderOffset` | px | 10 | Distance from the plate edge inward to the ring's outer edge. UI slider 0–100 mm. |
| `borderWidth` | px | 10 | Ring width: None 0, Light 5, Medium 10, Bold 20 (`dV`, B:30744). |
| `borderColor` | hex | `#000000` | Ring colour. |
| `backdropColor` | hex | `#FFFFFF` | Plate colour; also `mainObj.fill`. |
| `mainObj` | fabric object | | Circle: `radius`, new = 250 px (Ø100 mm); an ellipse is `scaleX ≠ scaleY`. Rectangle: new = 750×500 px with `rx = ry = 50` (10 mm corners); the UI corner slider is 0–100 mm. Always `left = -w/2, top = -h/2`, which is what puts the canvas origin at the plate centre. |
| `shape`, `shapeUrl` | int, data URI | | Shapes 2–10 only. `shapeUrl` is the inline SVG the editor was built with; on load the SVG is looked up again from the bundle by `shape` (B:8311), so `shapeUrl` is informational. Shapes 2–9 have viewBox 595.28×841.89 (Triangle 100×86.6). |
| `width`, `height` | px | | Shapes 2–10: the fabric group's size. A new shape is 750 px on its longer side. |

A new project gets a Rectangle (`initBackdrop`, B:16649). A backdrop that fails to
load is replaced by a Rectangle and `onBackdropError` fires.

#### TextElement (`av`, B:10626)

```json
{"version":"1.0", "type":"TextElement", "fabricObj": {…textbox or text…}}
```

`fabricObj.type` is `textbox` for straight text and `text` for curved text. Keys
beyond the common Fabric ones:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `text` | string | `"Input Text"` (the Add Text button, B:32158) | Lines separated by `\n`. |
| `fontFamily` | string | `"times new roman"` (class default); the button adds with `NotoSansSC` | `"<family>,AdobeNotDef"`: only the part before the first comma is used for geometry; `AdobeNotDef` is the tofu fallback (§7). A local font is named by its PostScript name. |
| `fontSize` | px | 36 | Em size in canvas px (÷5 = mm). |
| `fontWeight` | `"normal"` / `"bold"` / number | `"normal"` | Variable fonts (NotoSans, NotoSansSC) store 600 or 900; others `"bold"`. Old records with `"bold"`/`"normal"` on a variable font are converted to 900/600 on load. |
| `fontStyle` | `"normal"` / `"italic"` | `"normal"` | |
| `textAlign` | `left` / `center` / `right` | `"left"` | Within `width`. |
| `width` | px | 200 | The Textbox wrap width; words wrap at spaces (Fabric `_wrapLine`, fabric:13270). Fabric widens the box to its widest word. |
| `lineHeight` | number | 1.16 | Line advance is `fontSize × lineHeight × 1.13` (Fabric `_fontSizeMult`, fabric:11390). |
| `charSpacing` | 1/1000 em | 0 | Letter spacing, `fontSize × charSpacing / 1000` per character (fabric:11594). |
| `curvePoint` | `{x, y}` px | absent | Curved text only: the arc's control point relative to the text (§5.3). `path` is dropped on save and rebuilt from this on load (`an(curvePoint, textWidth)`). |
| `minWidth`, `splitByGrapheme`, `styles`, `direction`, `pathSide`, `pathAlign`, `pathStartOffset`, `underline`, `overline`, `linethrough`, `textBackgroundColor` | | | Fabric defaults, unused. |

#### IconElement, "Sticker" (`i0`, B:9975)

```json
{"version":"1.0", "type":"IconElement", "content":"♿", "originScaleX":0.0244140625, "originScaleY":0.0244140625, "fabricObj": {…path…}}
```

| Key | Meaning |
| --- | --- |
| `content` | The emoji string. VS16 (U+FE0F) is rewritten to VS15 for text presentation. The picker is the unicode-emoji-json dataset (`ey`, B:22289), some code points excluded (`gT`). |
| `originScaleX/Y` | `50 / upem` of NotoEmoji-Bold (2048): the glyph is outlined by HarfBuzz at font size 50 (`n3(content, "NotoEmoji", 50, …)`) and this is the scale from font units. |
| `fabricObj` | A `path` of `width`/`height` ≈ 2080 (font units), `scaleX/Y` the on-canvas scale, `fill` the colour (default `#353535`). The path data itself is not saved; it is rebuilt from `content`. |

#### BaseShapeElement (`iQ`, B:9075)

```json
{"version":"1.0", "type":"BaseShapeElement", "shape": 0|1|2, "fabricObj": {…}}
```

`shape` (`ib`): 0 Circle (fabric `circle`, radius 80), 1 Triangle (fabric `triangle`
80×80), 2 Rectangle (fabric `rect` 80×80, `rx`/`ry` 0). New shapes are at `left/top
-40` with fill `#178F51`.

#### SvgShapeElement (`i4`, B:10186)

```json
{"version":"1.0", "type":"SvgShapeElement", "shape": 0, "fabricObj": {…}}
```

`shape` 0 is the only value: the Pentagram, a 36×36 inline SVG (B:10249) loaded as a
group; default fill `#E03D19`, `left/top -18`. The path data is rebuilt on load.

#### SvgImageElement (`i3`, B:10064)

```json
{"version":"1.0", "type":"SvgImageElement", "keyName":"<name>_<Date.now()>", "props": {…fabric props…, "fill"?: "<colour>", "data": {"depth": …}}}
```

| Key | Meaning |
| --- | --- |
| `keyName` | The SVG store key (§2). **The SVG text is not in the document.** On load it comes from IndexedDB, or from `make.assets.svgAssets[]` by `name` minus `.svg` (`nO`, B:4438), or from a 3MF's `Auxiliaries/Templates/assets/<keyName>.svg` (§6). |
| `props` | The Fabric properties of the group (`left`, `top`, `scaleX`, `scaleY`, `angle`, `flipX`, `flipY`, `width`, `height`, `data.depth`). |
| `props.fill` | Optional. When present every child path is recoloured to it; absent, each path keeps its own fill. |

An SVG wider than 450 px on import is scaled to `900 / (2 × width)` (B:10064). An SVG
containing `<clipPath` is refused (`checkSvgCompatible`, B:16156).

#### VectorizeElement (`ax`, B:10913)

An "Image to Keychain" import. The record is an SvgImageElement plus
`props.vectorizeContent`, a JSON **string** (captured:
`make-85233397.content.json`):

```json
{
  "defaultSetting": { "depth": 1 },
  "globalResult": { "left": 82.06, "top": 213.66, "scale": 0.553, "angle": 0 },
  "pathResults": [ { "colorKey": "<key>", "depth"?: <mm> }, … ],
  "paletteMap": { "<key>": { "colorKey", "index", "children": [], "fill": "rgb(5, 5, 4)", "red", "green", "blue", "alpha"?, "isOrigin", "isPaletteHidden"?, "deleted"?, "parent"?, "customColor"?: {"red","green","blue"} } },
  "colorPathMap": { "<key>": [ <path index>, … ] },
  "secondaryEditColorReplaceMap": { "rgb(r, g, b)": "<css colour>" }
}
```

| Key | Meaning |
| --- | --- |
| `pathResults[i]` | One per `<path>` in the SVG, in document order (paths with a clip mask skipped). `colorKey` names a palette entry (`""` = use the path's own fill); `depth` overrides `defaultSetting.depth` for that path. |
| `paletteMap` | Keyed by `"{ red: r, green: g, blue: b }"` with components 0–1. `index` is the palette slot (sorted by HSL); `parent`/`children` record merges; `deleted` entries are ignored; `customColor` overrides the colour; a hidden `"transparent"` entry is always present. |
| `secondaryEditColorReplaceMap` | This editor's recolours, applied last. |
| `globalResult` | The placement the import arrived with; `props` holds the current one. |

Colour resolution for a path (`to`, B:1832): `pathResults[i].colorKey` → palette
entry (`customColor` wins, `deleted` ignored); else the path's fill → palette →
`parent`; then `secondaryEditColorReplaceMap`; `alpha 0` is not built.

#### BaseHoleElement (`o3`/`iH`, B:9362–9975)

Version `"1.0"` records carry no geometry; the hole is re-created from `holeType` and
`holeSize` against the current backdrop (captured: `element-examples.json`). Version
`"2.0"` (editor 1.3.0, "free holes") adds the Fabric object:

```json
{"version":"2.0", "type":"BaseHoleElement", "holeSize": 20, "holeType": "out_single_top", "fabricObj": {…}}
```

| `holeType` | Style | Placement |
| --- | --- | --- |
| `in_single_top`, `in_single_top_left`, `in_double_top` | `inside`: a through hole | §5.6 |
| `out_single_top`, `out_single_top_left`, `out_double_top` | `outside` ("Boundary"): a solid tab on the edge | §5.6 |
| `back_single_top`, `back_single_top_left`, `back_double_top` | `back`: a blind keyhole slot in the underside | §5.6 |
| `none` | | no hole |

| `holeSize` (px) | Key | Geometry |
| --- | --- | --- |
| 12.5 | `inSm` | circle r 12.5 px → Ø5 mm |
| 25 | `inBm` | Ø10 mm |
| 15 | `backSm` | the bundled keyhole outline, 30×38 px → 6×7.6 mm, cut 2 mm deep (`aS[15]`) |
| 30 | `backBm` | 60×68 px → 12×13.6 mm, cut 3 mm deep (`aS[30]`) |
| 20 | `outSm` | the bundled tab outline, Fabric extent 40×80 px → 8×16 mm (the SVG declares 45 px tall; measured in signs/src/shapes/holes.ts) |
| 45 | `outBm` | 90×130 px → 18×26 mm |

A hole not fully inside the plate is drawn red, gets `isHoleInPlate=false` and is left
out of the 3D build (`iD`, `aj`). Holes cannot be copied. Lock and Hide (`isHoleLock`,
`isHoleHidden`) are view options, not document state. `aP()` warns when a back hole is
as deep as the plate (`backHoleOverflow`), suggesting `backdropHeight = depth + 1`.

#### HookElement (legacy)

Filtered out on load and never created (B:16970). **INFERRED:** the pre-1.3 hanging
hook, replaced by Boundary holes; the old UI images are `signHook.webp` and
`signHolder*.webp` (B:17925).

### 3.3 Per-element depth

`fabricObj.data.depth` (or `props.data.depth`), in mm. `preProcessBeforeSerialize`
writes `data: {depth}` on every save (B:8904). When `depth > 0` it replaces the type's
thickness (`customDepth`); `undefined` means use the type's.

### 3.4 Thickness keys (`ng`, B:4211)

| Render type | `thickness` key |
| --- | --- |
| text, icon | `textHeight` |
| shape, **border** | `shapeHeight` |
| backdrop | `backdropHeight` |
| svg, vectorize | `svgHeight` |
| hole | `holeHeight` (stored, never used) |

### 3.5 Default document

A new make is `content: ""` on the server; the editor materialises the defaults: a
Rectangle backdrop (§3.2), the thickness and globalStatus defaults of §3.1, no other
elements.

### 3.6 The user preset (not per make)

`GET /my/preset` → `{preset: "<json>"}`, saved with `POST /my/preset {preset}`:

```json
{"date": …, "units": "mm", "version": "NamePlate V1.1", "unitsType": "metric", "showReference": true}
```

`units` is `mm`, `cm` or `inch`; `unitsType` `metric` or `imperial`; `showReference`
toggles the size labels. It does not affect the document, which is always px.

## 4. Units and coordinates

- **5 canvas px = 1 mm.** `oz = {mm: 5, cm: 50, inch: 127}` px per unit (B:14134).
- The canvas origin is the **plate centre**, because the backdrop is placed at
  `left = -w/2, top = -h/2`. Fabric is **Y down**; `angle` is clockwise on screen.
- The 3D build flips Y: every element's SVG is exported inside
  `<g transform="scale(1,-1)">` (`nM`, B:7079), and meshes are placed at
  `(left, -top, z)` under a group scaled `(0.2, 0.2, 1)` (B:33418), so XY is px→mm and
  Z is already mm.
- Fabric's transform order for an object is: scale about the origin corner, then
  rotate about it, then translate to `(left, top)`. `flipX`/`flipY` negate the scale.

Worked example (captured: `element-examples.json`, BaseShapeElement): `left -138.92,
top -201.93, width 80, height 80, scaleX 3.43, scaleY 4.62, originX left, originY top`
is a rectangle whose top-left corner is at (-27.78 mm, +40.39 mm) in a Y-up mm frame
(x = -138.92 / 5; y = -(-201.93) / 5), 54.88 mm wide (80 × 3.43 / 5) and 73.92 mm tall.

The printer list only chooses the 3MF print profile and size hints (`eX`/`eF`,
B:291): A1M 180×180, X1 256×256, H2D 300×320, H2S 340×320, A2L 330×320 mm.

## 5. Geometry semantics

What the editor's 3D build (`h_`, B:33400–33830; `nN`, B:7090) does with each field.
Each element yields `{svg, height, offsetZ, left, top, scaleX?, scaleY?, renderType,
elementType, customDepth, customDepthValue, pathResults?}` (`getThreeDeeRenderDataList`,
B:11372); the SVG is parsed by a copy of three's SVGLoader, extruded (`depth = height`,
no bevel, 12 curve segments, 50 for `<circle>`), and triangulated through manifold
(`CrossSection.ofPolygons(…, "NonZero")`).

### 5.1 Heights

| Mode | Backdrop | Every other element |
| --- | --- | --- |
| normal | z 0 → `backdropHeight` | starts at `offsetZ = backdropHeight`, stands `h` tall, where `h` = `data.depth` if > 0 else the type's thickness (§3.4) |
| `nonePlateMode` | none | `backdropHeight` is 0, so everything starts at z 0 |
| `flatMode` | z 0 → `backdropHeight`, **not cut** | `height = min(backdropHeight, h)`, `offsetZ = backdropHeight − height`: sunk flush with the top face. The overlapping volumes are separate 3MF parts; the slicer's part order decides the colour where they overlap (**INFERRED**: later parts win in Bambu Studio). The whole item is then rotated 180° about Y (§6). |

The border uses `shapeHeight` and sits on the plate like a shape. Changing
`backdropHeight` moves every element's `offsetZ` and resizes Boundary tabs
(`changeThickness`).

### 5.2 Plate and border

- Circle/Rectangle: the Fabric shape's outline (`exportMainSvg`). Shapes 2–9: the
  bundled SVG outline fitted to `width × height`. Custom: the stored SVG.
- Border (B:7935): `ring = inflate(plate, −borderOffset) − inflate(plate, −(borderOffset + borderWidth))`,
  Clipper2 `InflatePathsD` with round joins, curves flattened at 0.01 first (`n8`). So
  the ring lies inside the plate, `borderOffset` px from the edge, `borderWidth` wide,
  `shapeHeight` tall, in `borderColor`.

### 5.3 Text

- Straight text: HarfBuzz shapes each line (`n5`, B:7689) in the font named by
  `fontFamily` (§7); `fontSize` px is the em; lines are laid out as Fabric does
  (`textAlign` within the box, advance `fontSize × lineHeight × 1.13`). The 3D outline
  ignores `charSpacing` (B:7723 shapes each line as one run); only the on-screen text
  spreads. The path is emitted in font units and wrapped in
  `rotate(360 − angle) scale(scaleX·1000, −scaleY·1000)`; the 3D step then scales by
  0.001 (precision round trip).
- Curved text: Fabric `Text` on a `path`. Presets (`nd`, B:4220): Up 0, Down 1,
  Circular 2, None 3. The initial `curvePoint` (`ai`, B:10380), with `w` the text's
  `getScaledWidth()` (that is `(width + strokeWidth) × scaleX`, fabric:7514):
  - Up: `(−√2·r/2, −√2·r/2)`, `r = 2w/π`;
  - Down: `(−√2·r/2, +√2·r/2)`;
  - Circular: `(0, w/π − 22)`.

  `aA()` (B:10262) builds the arc through the start point: `R0 = (x² + y²) / 2|y|`,
  the sweep angle clamped to [1e-4°, 360°], `R = w / angle`. The user drags the
  "curveController" to move `curvePoint`. Each character is placed on the arc on its
  own (no kerning across characters). Curved text cannot be resized from its side
  handles.
- Missing font: `onFontMissing` fires and `fontFamily` becomes `"NotoSansSC,AdobeNotDef"`.

### 5.4 Icons, shapes, SVGs

- Icon: the NotoEmoji-Bold glyph outline, one colour (`fill`).
- BaseShape, SvgShape: Fabric `toSVG()` of the object.
- SvgImage: the stored SVG as a Fabric group; `props.fill` recolours every path, else
  each path keeps its fill. Every path is its own mesh with its own colour.
- Vectorize: `mergeSameColorDepthMesh` unions every path of the same resolved colour
  and depth (paper.js `unite`) into one mesh; per-path depth from `pathResults[i].depth`
  else `defaultSetting.depth`; in flatMode all use the clamped height; `"transparent"`
  paths are skipped.

### 5.5 Z-order and overlap

Elements are built in canvas z-order (`data` order). Nothing is cut against anything
except holes; overlapping elements are overlapping parts, and in the exported 3MF the
slicer resolves them by part order (§6). Text and icons are always kept above shapes
and SVGs in the canvas (`moveTo(len − textCount)`), holes above everything.

### 5.6 Holes

- Inside holes and back holes are **cutters** (`classifyModels`, `cutHoles`): every
  backdrop, boundary tab and element whose bbox meets one has it subtracted (manifold
  `subtract`).
  - Inside: from z 0, `backdropHeight + 1` tall in flatMode, else
    `maxElementHeight + backdropHeight + 1`: through everything, text included.
  - Back: the keyhole outline extruded `aS[size]` (2 or 3 mm) from z 0: a blind slot
    in the underside.
- Boundary tabs are solid, extruded to `backdropHeight` in `backdropColor`, treated
  as part of the backdrop.
- Preset positions are tables keyed by backdrop shape (`iW` single top, `iZ` double,
  `iV` top-left for inside and back; `i_`, `iN`, `iM` for outside; B:9543–9636). For
  example a Rectangle inside double is at `left = −w/2 + 20 + 0.2928·rx`,
  `top = −h/2 + 20 + 0.2928·rx`, its twin at `originX:"right", left = −left`. Boundary
  tabs stand out of the edge by `height − 40` px; the top-left variants are rotated
  315°. None, ClosedHanger and OpenHanger have no preset positions. (ScadBuddy's copy
  of the tables is `signs/src/shapes/holes.ts`.)

### 5.7 Colour → filament

Colours are Fabric fills upper-cased: `backdropColor`, `borderColor`, each element's
`fill`, each SVG path's fill, each vectorize palette colour. `usedColors` collects the
distinct set; the 3MF assigns extruders in order of first appearance (§6).

## 6. The exported 3MF

Written by `hV.parse` / `export3mf` (B:33006, B:33771). From the captured export of
make 85233397 (A1 mini profile, 6 colours):

| File | Contents |
| --- | --- |
| `[Content_Types].xml` | Defaults for `rels`, `model`, `png`, `gcode`. |
| `3D/3dmodel.model` | `<model unit="millimeter">` with metadata `Application = BambuStudio-01.07.04.52`, `MakerWorld_M1 = <title>`, `MakerLab = Make My Sign`, `MakerLabVersion = NamePlate V1.1`, `DesignRegion`/`MakerLabRegion = US`, `MakerLabFileId`, `DesignModelId = FMI_<fileId>`, `Title`, `CreationDate`. One object with `<components>` naming every mesh in `/3D/Objects/object-<id>.model`. One build `<item>` with the transform below. |
| `3D/Objects/object-<id>.model` | One `<object type="model"><mesh>` per mesh, vertices baked. |
| `Metadata/model_settings.config` | The object `name = <title>.3mf`, `thumbnail_file = Metadata/plate_1.png`; one `<part subtype="normal_part">` per mesh named `<elementType>-<meshId>` (`Backdrop-57`, `Border-59`) or `<elementType>-<groupId>-<meshId>` (vectorize parts come out as `undefined-54-62`), each with `extruder = <colour index, 1-based>`; one plate, one assemble item. |
| `Metadata/project_settings.config` | The print profile as JSON: `filament_colour` = the colour list in first-appearance order (captured: `["#FFFFFF","#000000","#050504","#E7232C","#34A54F","#1860AE"]`), every `filament_*` array padded to that length by repeating element 0, `printer_model`, `printer_settings_id`, `print_settings_id`, `printable_area`, `printable_height`, `curr_bed_type = Textured PEI Plate`, `ironing_type = top`, `layer_height = 0.2`, `wall_loops = 2`, `sparse_infill_density = 15%`, `wall_generator = arachne`, `wipe_tower_x/y`. |
| `Metadata/plate_1.png`, `Auxiliaries/.thumbnails/thumbnail_3mf.png` | The **2D canvas screenshot**, not a render. |
| `Auxiliaries/Templates/template.json` | Only with "Include customizable file in 3mf (including SVG)" (on by default): the document of §3, byte-identical to the make's `content` (captured: `3mf-template.json` = `make-85233397.content.json`). |
| `Auxiliaries/Templates/assets/<keyName>.svg`, `CUSTOM_SVG_SHAPE.svg`, `<postscriptName>.font` | Every SVG the document references and every local font. This makes the 3MF a self-contained project: it is the one place a make's locally imported files can be recovered from. |

Profiles (B:33006): the default `hT` is X1C (256×256×250, `0.20mm Standard @BBL X1C`,
`Bambu Lab X1 Carbon 0.4 nozzle`, wipe tower (15, 220)); `hW` is A1 mini
(180×180×180, `@BBL A1M`, wipe tower (15, 142.333)), chosen when the sign's bbox width
≤ 180 and height + u ≤ 180, where u = 40 mm for a multicolour sign (wipe-tower room)
and 0 otherwise.

Placement: `transform = "<rot> <c − x> <c − (y + u/2)> <z>"` with c = 128 (X1C) or
90 (A1 mini). `rot` is the identity, or `-1 0 0 0 1 0 0 0 -1` (180° about Y) in
flatMode, with `z` = the model's depth so the face lands on the bed (captured:
`-1 0 0 0 1 0 0 0 -1 90 70 3`). Mesh order is Backdrop, Border, BaseShape, SVG,
Sticker, Text (`hL`).

STL export is three's binary STLExporter with the 80-byte header
`ML <region> SIGN <mlFileId>` (`td`). Every export first registers a file
(`AT({generatorId: "SIGN", format, extra: {makeId, backdropShape, backdropBorder,
textCount, iconCount, shapeCount, svgCount, holeCount, vectorizeCount}})` →
`{region, mlFileId}`). A headless exporter (B:35571) takes `.json` + `.svg` + `.font`
files and produces `nameplate.3mf`: the same input shape as `Auxiliaries/Templates`.

## 7. Fonts

Catalogue (`nh` groups and `nX` files, B:4226, B:7190), served from
`https://public-cdn.bblmw.<com|cn>/content-generator/fonts/<file>` (`nF`, B:7232):

| Group | Family (`fontFamily`) | Files | Bold | Italic |
| --- | --- | --- | --- | --- |
| sans_serif | NotoSansSC | `NotoSansSC-VariableFont_wght.ttf` | 900 (variable) | |
| sans_serif | NotoSans | `NotoSans-VariableFont_wdth,wght.ttf`, `NotoSans-Italic-VariableFont_wdth,wght.ttf` | 900 (variable) | yes |
| sans_serif | Roboto | `Roboto-{Regular,Bold,Italic,BoldItalic}.ttf` | yes | yes |
| sans_serif | SmileySans | `SmileySans-Oblique.ttf` | | |
| sans_serif | Bagnard Sans | `BagnardSans.otf` | | |
| serif | Merriweather | `Merriweather-{Regular,Bold,Italic,BoldItalic}.ttf` | yes | yes |
| serif | Wigners Friend | `WignersFriendRoman.ttf` | | |
| handwritten | Mali | `Mali-{SemiBold,Bold,SemiBoldItalic,BoldItalic}.ttf` | yes | yes |
| handwritten | Pacifico | `Pacifico-Regular.ttf` | | |
| handwritten | 站酷快乐 | `ZCOOLKuaiLe-Regular.ttf` | | |
| handwritten | 站酷小薇 | `ZCOOLXiaoWei-Regular.ttf` | | |
| stylized | Runy Tunes | `RunyTunesRevisitedNF.ttf` | | |
| stylized | Kalmansk | `Kalmansk-51WVB.otf` | | |
| stylized | Radio | `radio.ttf` | | |
| stylized | Bloom | `Bloom-Regular.otf` | | |
| stylized | LeFun | `LeFunFont-Regular.otf` | | |
| accessibility | CalHendersonBraille | `Braille.ttf` | | |
| accessibility | AliHealthFontBraille | `AlibabaHealthFont20BRAILLE-A-01.ttf` | | |
| accessibility | AliHealthFontPro | `AlibabaHealthFont20PRO-A-01.ttf` | | |
| hidden | AdobeNotDef | `AND-Regular.ttf` | | |
| hidden | NotoEmoji | `NotoEmoji-Bold.ttf` | | |
| hidden | times.ttf | `times.ttf` | | |

Variable weights: normal 600, bold 900 (`nI`, B:4288). HarfBuzz is loaded from
`https://makerworld.bblmw.<com|cn>/makerworld/makerlab/content-generator/hb2.wasm`.
Local fonts (editor 1.2, `.ttf/.otf/.woff/.woff2`) go into the per-user IndexedDB font
store by PostScript name and are referenced by that name in `fontFamily`; a 3MF export
carries them as `assets/<postscriptName>.font`.

## 8. HTTP API

Base `https://makerworld.com/api/v1/creation-service`; Make My Sign is `category=1`
(captured). Requests carry the site's session cookies; no separate token was seen.

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| GET | `/my/make/{id}` | | `{id, title, cover, coverUrl, content: "<json string>", assets: {svgAssets: [{name, url, objectName}], imageAssets: […]}, instanceId, productName, categoryName, keyChainTemplateId}`. The edit page also fetches it server-side (`__N_SSP`). |
| PUT | `/my/make/{id}` | `{content: "<json string>"}` (autosave), or `{title}`, or `{cover: <objectName>}` | `{}` |
| POST | `/my/make` | `{category: 1, cover: "", title, content: ""}` ("Create from Blank") | `{id}` |
| POST | `/my/makebytemplate/{templateId}` | `{cover: "", title}` ("Use template") | `{id}` |
| POST | `/my/make/{id}/copy` | | `{id}` (the SVG store is copied client-side) |
| DELETE | `/my/make/{id}` | | |
| GET | `/make/{id}/upload?fileName=…&bizType=…&category=1` | `bizType` `nameplateCover` (file `cover<id>`) or `nameplateScreenshot` | `{presignedUrl, url, fileId, storageInfo: {objectName}}` |
| PUT | `<presignedUrl>` | the bytes | S3 |
| GET | `/template?offset=0&limit=20&category=1` | | `{total, hits: [{id, title, cover, user, useCount, designId, content}]}` |
| GET | `/template/{id}` | | `{…, content: "<json string>"}` |
| GET | `/my/makes?offset&limit&category` | | the user's makes (the Keychain import list) |
| GET | `/vectorization/{id}/resource` | | `{title, assets: {svgAssets: [{url}]}, additionalInfo \| content}` (a Keychain make) |
| GET / POST | `/my/preset` | `{preset: "<json>"}` | §3.6 |
| POST | `/my/make/securitycheck` | `{text: […], image: [url]}` | `{isPass}` (China region only) |
| POST | `/my/make/statistics` | `{makeId, category: "SIGN"}` | print statistics |
| POST | `/common/permanent_files` | `{makeId, fileIds, categoryId}` | present in the client, not used by sign |
| POST | `/my/createfromdesign/{designId}` | | present in the client; **INFERRED** to be "Customize" from a published model page. Not referenced in the sign chunks. |

Importing from Image to Keychain: the editor lists `GET /my/makes?category=<keychain>`,
reads `GET /vectorization/{id}/resource`, downloads the SVG from `assets.svgAssets[0].url`
and adds it with `addSvgImage(title, svg, {vectorizeContent})`. `?vectorizeID=<id>` on
the edit URL does the same on load.

## 9. Versions and migrations

Applied on load (`deserializeData`, B:16970; `setupPreProcess`):

| Rule | Effect |
| --- | --- |
| Content is a bare array | used as `data`; `thickness` and `globalStatus` default |
| `thickness` / `globalStatus` keys missing | filled from the defaults (spread) |
| Element `version` | always `"1.0"` except holes |
| Hole `"1.0"` | no geometry saved; position recomputed from `holeType`/`holeSize` and the current backdrop |
| Hole `"2.0"` | the saved `fabricObj` (`left`, `top`, `angle`, `scaleX/Y`, `originX/Y`) is applied |
| `HookElement` | dropped |
| Backdrop fails to load | replaced by a Rectangle; `onBackdropError` |
| Font cannot load | `fontFamily = "NotoSansSC,AdobeNotDef"`; `onFontMissing` |
| Variable font with `"bold"` / `"normal"` | converted to 900 / 600 |

Editor history (release notes in E): 0.3.0 multi-select, Braille, arrow keys, adaptive
A1 mini profile, triangle plate, top-left hole; then per-element thickness, size
inputs, remove plate, alignment, bold/italic; 1.0.0 facedown and curved text; 1.1.0
Keychain import; 1.2.0 local fonts; 1.3.0 base plate, drilling and colour upgrades
(free holes, hole v2).

## 10. Gallery templates

A template is a make whose `content` is served from `GET /template/{id}` and copied
into a new make by `POST /my/makebytemplate/{id}`. The 12 at the time of capture
(ids 15–26) are rectangles and shape plates with text and stickers; none uses an
SvgImage, a Vectorize or a v2 hole, and the older ones have no `globalStatus`
(`examples/template-26.content.json`: an OpenHanger plate with a rectangle, two text
boxes and a ✔️ sticker, all `#ffffff` on `#353535`).

## 11. What a ScadBuddy document adds, and where it must live

The editor rebuilds every record on save: the top level is
`{data, thickness, globalStatus, date, version}` (B:11032), each record is
`{version, type, …, fabricObj: preProcessBeforeSerialize({…})}` (B:9085, B:9442,
B:9989), `fabricObj` is Fabric's own `toObject()` (known keys only), and
`fabricObj.data` is overwritten with `{depth}` (B:8904). So **no key ScadBuddy adds
anywhere inside the document survives a load-and-save in MakerWorld's editor.** That
is read from the serialiser, not tested live; a live round trip would confirm it.

What ScadBuddy needs that the format has no slot for:

| Need | Why |
| --- | --- |
| Where an SVG's or font's bytes are | `keyName` and a PostScript name point at a browser store; ScadBuddy holds them as assets. |
| Per-element overlap mode (`occlude` / `stack`) | MakerWorld leaves overlaps to the slicer; OpenSCAD colour solids may not overlap (spec §5.2). |
| Which values a saved template exposes as customizer parameters | "Save as template" (spec §8). |
| A sign's own identity: source make id, template id, import date | Provenance. |

So ScadBuddy stores the MakerWorld document **verbatim and untouched** beside its own
state, in an envelope:

```json
{
  "makerworld": { …the §3 document, exactly as received or as the canvas serialises it… },
  "scadbuddy": {
    "v": 1,
    "source": { "kind": "makerworld_make" | "makerworld_3mf" | "makerworld_template" | "blank", "make_id"?, "template_id"?, "imported_at"? },
    "assets": { "<keyName>": { "asset": "<ScadBuddy asset id>", "sha256": "…" }, "fonts/<postscriptName>": { … } },
    "elements": { "<index in data>": { "overlap": "occlude" | "stack" } },
    "expose": { … }
  }
}
```

Rules that follow from §11's first paragraph:

- Import takes `content` (or `template.json`) into `makerworld` byte for byte, and
  resolves every `keyName` and local font to an asset. Export writes `makerworld` back
  as `content` / `template.json` and the assets as `assets/<keyName>.svg` and
  `<postscriptName>.font`, which MakerWorld's own loader reads (§6).
- `scadbuddy.elements` is keyed by position in `data`, since records carry no id.
  A reorder in the canvas re-keys it; the canvas is ScadBuddy's own, so it can.
- Everything the editor regenerates on load (§3.2, "derived values") is regenerated
  by ScadBuddy too and never read as truth: a textbox's `width`/`height`, a path's
  bbox, a curved text's `path`, an icon's outline.
- The Fabric canvas in ScadBuddy's editor loads `makerworld.data` directly with the
  same object shapes, so a document edited in ScadBuddy still opens in MakerWorld, and
  one edited in MakerWorld still opens here.
