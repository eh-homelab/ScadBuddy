# The MakerLab protocol: what every MakerWorld tool shares

Make My Sign (`make-my-sign.md`) is one of 26 first-party MakerLab tools and 20 partner
tools. This document is the layer under all of them: one project record, one HTTP API,
one 3MF writer, one autosave hook, one set of WebAssembly libraries, and one way a
tool's document rides inside another tool's. It then compares each piece with what
ScadBuddy has today.

Everything here was read from MakerWorld's web bundle on 2026-10-14 and 2026-10-15
(Next.js build `arlj243Es7etS3BsJ5nsQ`; the chunks are named below) and from captured
responses of a logged-in session (`scratchpad/mw/mms/*.json`). Where a fact comes from
a chunk, the reference is `<chunk>:<line>` in the prettier-formatted copy
(`prettier --parser babel --print-width 120`). Nothing was written to MakerWorld.

## 1. The catalogue: tools, categories and generator ids

**Tool categories** (`_app-de45e9df87af6b5e.js:58743–58769`, an enum keyed both ways).
The number is the `category` of every make the tool saves and the `category` query of
`GET /my/makes`:

| id | tool | id | tool |
|---|---|---|---|
| 1 | makeMySign | 14 | makeMyLantern |
| 2 | pixelPuzzleMaker | 15 | christmasOrnamentMaker |
| 3 | makeMyVase | 16 | shadowBoxMaker |
| 4 | reliefSculptureMaker | 17 | lightboxMaker |
| 5 | ai-scanner | 18 | imageTo3d |
| 6 | parametricModelMaker | 19 | reliefSculptureMakerV2 |
| 7 | imageToKeychain | 20 | flexiToyMaker |
| 8 | makeMyDeskOrganizer | 21 | laserBoxMaker |
| 9 | tripo3D | 22 | chromaCanvas |
| 10 | makeMyLithophane | 23 | printU |
| 11 | printMonMaker | 24 | makeMyShoes |
| 12 | gaussian | 25 | aimia |
| 13 | makeMyStatue | 26 | meshGraffiti |

Captured: the Mahjong sign is `category=1`; the account's keychain list was fetched
with `category=7`, laser box with `category=21`.

**Generator ids** (`2624-7d566f1e0a152179.js`, module 85029), the two-digit codes the
export tracker uses (`VASE 01, STATUE 02, ORNM 03, SIGN 04, MGRAF 05, LTBOX 06, ORGAN
07, RELIEFv2 08, KEYCH 09, FLEXI 10, CHROMA 11, OSCAD 12, PRMON 13, IM3Dv2 14, LITHO 15,
SHOES 16, LSBOX 17, PRINTU 18, SDBOX 19, PIXEL 20, SCAN 21, LANTN 22, AIMO 23`), with the
format codes `3mf 1, stl 2, obj 3, svg 4, lac 5, zip 6, glb 7, img 8` and a per-generator
list of the fields each export records (module 50330). Two things in it matter beyond
analytics:

- `SIGN` and `ORNM` share one field list (`makeId, backdropShape, backdropBorder,
  textCount, iconCount, shapeCount, svgCount, holeCount, vectorizeCount`): the
  Christmas Ornament Maker is the sign editor with another backdrop set.
- `OSCAD` (Parametric Model Maker, category 6) records `designId, exampleId,
  previewCount, isFusion`, and its API is `GET /pmm/template` and
  `GET /scad/gethistoryid` (`_app:2041, 2242`). It is MakerWorld's own OpenSCAD
  customizer. Its editor was not captured (the survey agents ran out before opening
  it); it is the tool to read next, since it is the one closest to ScadBuddy's core.
- A partner tool's id is `MLC_<appid>` (`2624`, module 50330, `/^MLC_.+$/`).

**Partner tools** (20 of the 39 tiles, the "Community" tab): each lives at
`/en/makerlab/community/<appid>` with an editor at `…/<appid>/edit?appid=<appid>`
(captured for `dice-maker, laser-box, laser-studio, polaroid-frame-studio,
snapframes-lite`). The editor page loads Go's `wasm_exec.js` and sits behind a
Cloudflare Turnstile that headless browsers do not pass, so what runs inside was not
seen. The server side they use is the `thirdparty_generator` group (`_app:2441–2490`):
`GET simple_content`, `POST export_record` / `GET export_status` (the "Export" counter
on a tool's page), `POST like`, `GET onboarding/status`, `GET orders`, `GET
<id>/membership` and `PUT`, `GET membership/public_key`, `GET
membership/payment_status`, `POST ticket/verify`. Partner tools do not use the make
record of §2; their projects are their own.

## 2. The project record: a "make"

Every first-party tool keeps a user's project as one row the API calls a *make*
(`GET /api/v1/creation-service/my/make/<id>`, captured for the Mahjong sign):

```json
{
  "id": 85233397,
  "title": "mahjong",
  "cover": "makerworld/makerlab/creation/1/85233397/nameplateCover/20261014_225008_938/cover85233397",
  "coverUrl": "https://or-cloud-makerlab-prod.s3-accelerate.amazonaws.com/…(signed)",
  "content": "{…the tool's document, as one JSON string…}",
  "assets": {
    "svgAssets": [{"name": "Keychain Draft_1774223447148.svg", "url": "https://makerworld.bblmw.com/makerworld/model/20260326/3784251043/aa018a4e9ddd58a2.svg", "objectName": ""}],
    "imageAssets": null
  },
  "instanceId": 2837289,
  "productName": "Make My Sign",
  "categoryName": "Make My Sign",
  "keyChainTemplateId": 0
}
```

- **`content` is an opaque string.** The server stores and returns it; only the tool
  parses it. Each tool's document carries its own `version` tag at the top level:
  `"NamePlate V1.1"` (sign, ornament), `"Vectorize V1.0"` (Image to Keychain, §5). The
  tag is also what the 3MF writer stamps as `MakerLabVersion` (§4).
- **`assets` holds what the document names but does not contain**: SVGs and local
  fonts (`.font`, by PostScript name) under `svgAssets`, images under `imageAssets`.
  Each is `{name, url, objectName}`; the document refers to it by `name`
  (`keyName`). The editor fetches them at load (`b0b707fb:4439–4460`).
- **`instanceId`** links the make to the MakerWorld model instance it was published
  as (the Mahjong make → instance 2837289 of model 2573411). The published design
  carries the tool as a category (`design-service/design/2573411`: categories
  `2002 "Make My Sign"`, `2000 "Generative 3D Model"`), and the instance's
  `lacCustomInfo`.
- **`cover`** is an S3 object name; `coverUrl` is a signed URL for it.
- The cover's upload key says which tool: `bizType: "<toolName>Cover"`
  (`page_makeMySign_edit.js:204`), e.g. `nameplateCover`.

**Templates** are the same shape minus the user: `GET /template?category=1&offset&limit`
→ `{total, hits: [{id, title, titleTranslated, cover, user, useCount, designId,
content}]}`, `GET /template/<id>` → one. `POST /my/makebytemplate/<id>` makes a make
from one.

**User preset** (`GET`/`POST /my/preset`) is one JSON string per tool holding the
editor's settings, versioned the same way: `{"date", "units": "mm", "version":
"NamePlate V1.1", "unitsType": "metric", "showReference": true}`.

## 3. The API (`/api/v1/creation-service`)

The client is one module (`_app-de45e9df87af6b5e.js:1700–2500`). Each endpoint's
request, response and callers, and the diff against ScadBuddy's routes, are in
`creation-service-api.md`. The make and template calls, which every tool shares:

| call | body / params | notes |
|---|---|---|
| `POST /my/make` | `{title, content: "", category}` | create; the sign page creates with empty content, then saves (`page_makeMySign_edit.js:147–151`) |
| `GET /my/make/<id>` | | the record of §2 |
| `PUT /my/make/<id>` | `{content}` or `{cover}` or `{title}` | the autosave (§3.1) |
| `PUT /my/make/<id>/webcontent` | `{content}` | the same, for tools whose page sets `isPutWebContent` |
| `GET`/`PUT /my/make/<id>/additionalinfo` | `{additionalInfo}` | a second document beside `content`, for tools that set `isPutAdditionalInfo` |
| `DELETE /my/make/<id>` | | |
| `POST /my/make/<id>/copy` | | duplicate |
| `GET /my/makes` | `category, offset, limit` | the user's list, per tool (`my/makes?category=1&offset=0&limit=40`) |
| `POST /my/makebytemplate/<templateId>` | | new make from a gallery template |
| `POST /my/createfromshare/<instanceId>` | | new make from a published instance that allows it (`allowReCreation`) |
| `POST /my/createfromdesign/<designId>` | | the same, from a design |
| `GET /template`, `GET /template/<id>` | `category, offset, limit` | the gallery |
| `GET`/`POST /my/preset` | `{preset}` | editor settings |
| `POST /my/make/snapshot` | | a cover snapshot request |
| `POST /my/make/statistics` | | export counts |
| `POST /my/make/securitycheck` | | content moderation of an upload |
| `GET`/`POST /my/make/setting/keepuploadedimages` | | the "keep my images" consent |
| `POST /my/upload/file` | `{fileName, bizType, category}` | returns a presigned upload (`storageInfo.objectName`); the file goes to S3, then the object name is `PUT` as `cover` |
| `GET /my/upload/files`, `DELETE /my/upload/file/<id>` | | the user's uploads |
| `GET`/`POST /my/make/vectorization`, `/vectorization/<id>/{detail,result,resource,slice,additionalInfo,notification}`, `/vectorizationv1/<id>/vertorize`, `/vectorizationv2/<id>/{vertorize,cancel}` | | Image to Keychain's server-side tracing |
| `GET /my/credit`, `/my/credit/detail`, `/makerlab/credit/configs`, `POST /my/credit/terms/accept` | | credits (paid tools) |

Tool-specific groups follow the same pattern (`/pixelate/<id>/…`, `/printmon/…`,
`/relief2/<id>/…`, `/shadowbox/…`, `/statue/<id>/…`, `/shoes/3mf`, `/pmm/template`,
`/scad/gethistoryid`): a make id, then the tool's own verbs.

### 3.1 The autosave hook

One React hook serves every editor (`page_makeMySign_edit.js:130–270`; it takes
`{content, id, uid, toolName, category, isPutWebContent, isPutAdditionalInfo,
isVectorize, permanentFileIdsRef, …}`):

- `saveSerializedData(doc, immediate)` stringifies the document and, unless immediate,
  waits **3 s** of quiet before `PUT {content}`; save status goes
  warning → info → success/error.
- The **cover** is uploaded on the first save and then at most every **30 s**: a
  canvas data URL or blob → `POST /my/upload/file {fileName: "cover<id>", bizType:
  "<tool>Cover", category}` → S3 → `PUT {cover: objectName}`.
- `beforeunload` flushes a pending save.

## 4. The 3MF writer

One class writes every tool's 3MF (`0ab3f289.da6547e6bf4cdcba.js:13620–14400`, the
`parse(groups, title, png, opts)` method; the sign calls it with `makerLabToolName:
"Make My Sign"`, Image to Keychain with `"Image to Keychain"` and `VERSION:
"Vectorize V0.0.2"`, `ec4e7569:35994`). Its options are the whole contract:

```
unit="millimeter", compress=true, flatMode, customOffsetZ, VERSION, scaleSize=1,
makerLabToolName="Make My Sign", region, mlFileId, aigc, nozzle="0.4", layerHeight,
wall_generator="arachne", ironing_type="top", enable_support="0",
support_type="normal(auto)", useMergedObject=false, weldVertices=true, printerType,
projectSetting={}, customProjectSetting=[], wipe_tower_y, separatePlates=true,
autoCenter=true, streamingZip=false, serializedData, svgAssets
```

What it writes:

- **Root model metadata** (`0ab3f289:13860–13900`), in this order: `Application
  BambuStudio-01.07.04.52`, `MakerWorld_M1 <title>`, `MakerLab <tool name>`,
  `MakerLabVersion <document version>`, `DesignRegion` and `MakerLabRegion` (`US`),
  `MakerLabFileId <id>` and `DesignModelId FMI_<id>` (when the export was registered
  with `/my/make/statistics`), `AIGC <json>` (AI tools), `Copyright`, `CreationDate`,
  `Title`. The captured sign 3MF has exactly these.
- **One `<object>` per group**, each mesh a `<part>` named
  `<elementType><index>-<id>` (`Backdrop-57`, `Border-59`, `undefined-54-62`; the
  `undefined` is a vector element with no type set) with `extruder` = the 1-based index
  of its colour in the file's distinct-colour list (`filamentColour`, `el5`). So
  **extruders are assigned by colour, in order of first appearance**, and
  `project_settings.config`'s `filament_colour` lists them.
- Parts are sorted by element type before writing (`el2`: `Backdrop 0, Border 1,
  BaseShape 2, SVG 3, Sticker 4, Text 5`; Keychain adds `Boundary -7, MirrorBackdrop
  6, BoundaryBackdrop 7`), so part order in the file is layer order.
- **Printer profile** by printer and nozzle (`0ab3f289:380–600`, `eA`…): X1 Carbon is
  the default; `printerType` picks A1, A1 mini (180×180, wipe tower y 142.333), P1P,
  P1S, X1E, X2D, A2L, H2D/H2D Pro/H2S/H2C (350×320 or 330×320), P2S; 0.2 mm nozzle
  variants switch to the `0.10mm Standard` process. The plate width used to decide
  whether a sign fits is 300 for the H2 family, 166 for A1 mini, 240 otherwise
  (`el9`), minus a 40 mm wipe margin.
- `Metadata/model_settings.config`, `Metadata/project_settings.config`,
  `Metadata/plate_1.png` and `Auxiliaries/.thumbnails/thumbnail_3mf.png` from the
  2D canvas.
- **`Auxiliaries/Templates/template.json`** = `serializedData` (the make's `content`,
  byte for byte) and **`Auxiliaries/Templates/assets/<name>`** = each `svgAssets`
  entry's bytes, when the user ticks "Include customizable file". This is the only
  place a tool's document leaves MakerWorld.

Nothing in the bundle reads `Auxiliaries/Templates` back in: the editors import from
the API, not from a 3MF. The sign editor's headless export (`b0b707fb:35567`,
`up({json, svgAssets, fontAssets, region, projectName})` and `uf(files)` over
`[*.json, *.svg, *.font]`) is the reverse: files in, 3MF out, with no API.

## 5. Documents nest: Image to Keychain inside Make My Sign

Image to Keychain's document (`"Vectorize V1.0"`, `ec4e7569:20633–20700`, `loadData` /
`exportData`) is:

```
{ defaultSetting: {depth},
  globalResult: {angle, scale, left, top},
  pathResults: [ {colorKey, depth, …} per traced path ],
  paletteMap: [[key, {colorKey, index, children[], fill, red, green, blue, isOrigin,
                      parent?, customColor?, alpha?, createdAt, updatedAt}]],
  colorPathMap: [[key, [path indexes]]],
  secondaryEditColorReplaceMap: [[key, key]],
  backdropInfo: {type: "default"|"shape"|"doubleSide"|"mirror", thickness,
                 width, height, left, top, shape, scaleX, scaleY, borderRadius,
                 colorKey, isAttachmentAdded, attachmentType, attachmentProps{colorKey,…},
                 holeOffset, isBoundaryAdded, boundaryOffset, boundaryThickness,
                 boundaryColorKey, facedownMode},
  groupSelector: [{indexes[], groupName, order}],
  date, version: "Vectorize V1.0" }
```

plus the traced SVG itself (`vectorize.svg` in the 3MF's assets; from
`/vectorization/<id>/result` on the server). `pathResults[i].depth` is per-path height;
facedown clamps it to the plate (`getPathIndexDepth`, `ec4e7569:20970–20985`: whole
thickness, or half for `mirror`).

The "Export to Make My Sign" button opens
`/makerlab/makeMySign/edit/<makeId>?vectorizeID=<id>&from=keychain`
(`ec4e7569`, string table), and the sign's `VectorizeElement` record carries
`props.vectorizeContent`: **the Vectorize V1.0 document as a string**, with the SVG as
an `svgAssets` entry. The Mahjong sign is exactly this: a keychain project embedded in
a sign (`make-my-sign.md` §3.2, VectorizeElement). So a sign loader must already read
the keychain format, and a project document is a tree of tool documents, each with its
own `version`.

## 6. Shared WebAssembly

All tools load the same engines from the CDN, by URL (captured on the sign, keychain and
partner-tool pages): `public-cdn.bblmw.com/content-generator/hb.wasm` (HarfBuzz),
`makerworld.bblmw.com/makerworld/makerlab/content-generator/clipper2z.wasm`
(Clipper2), `…/cdt_wasm_bg.wasm` (constrained Delaunay triangulation),
`…/manifold.wasm?v=2` (Manifold, the booleans in the 3MF step), plus the font files
under `public-cdn.bblmw.com/content-generator/fonts/`. ScadBuddy's `signs/` package uses
the same HarfBuzz (harfbuzzjs) and Clipper2 (clipper2-ts) builds, so text and offsets
match by construction; triangulation and booleans are OpenSCAD's (Manifold) instead.

## 7. Comparison with ScadBuddy

| | MakerLab | ScadBuddy today | Gap |
|---|---|---|---|
| **Project record** | a *make*: `{title, content (opaque JSON string, versioned), assets{svgAssets,imageAssets}, cover, category, instanceId}` | a model (`models/<slug>/model.json` + sources) and its *inputs* (`{v, params, …}`, `render/inputs.py`), saved as presets (`saved_presets.inputs` jsonb) and recorded with every output (`inputs.json`); `assets` table for `// file` uploads | Same split: an opaque, versioned document the tool owns + a side list of binary assets. ScadBuddy's `inputs` is that document; `saved_presets` is the make list. Missing: a `category`-like tool id on an inputs document (today the template slug plays that role), and a cover per saved preset. |
| **Templates / gallery** | `GET /template?category=` → `{content}`; `makebytemplate` | template gallery (#273): a template's own `presets` list in `model.json`, `saved_presets` | Equivalent. |
| **Create from a published thing** | `createfromshare/<instanceId>`, `createfromdesign/<designId>`, gated by `allowReCreation` | fork/upstream (`library/model_operations.py`), mesh import (#2146) | Equivalent at the model level; ScadBuddy has no "open a project someone published as a project" because its outputs do not carry one (next row). |
| **Document inside the 3MF** | `Auxiliaries/Templates/template.json` + `assets/`, and `MakerLab` / `MakerLabVersion` / `MakerLabFileId` root metadata | `ScadBuddy:provenance` root metadata (`render/provenance.py`: `{model, version, output, params, edit_url}`) | ScadBuddy stamps the *parameters* but not the *document*: a pipeline template's full inputs, a mesh model's `scene.json`, a sign's document, and their assets, are not in the file. Adopting `Auxiliaries/Templates/template.json` (+ `assets/`) as ScadBuddy's own carrier, with `ScadBuddy:provenance` naming the document's kind and version, makes every output re-openable as a project and makes a sign's export (`make-my-sign.md` §6) one case of it. Reading it back (`objects3mf.read_objects` sees only meshes today) gives ScadBuddy the import MakerWorld itself lacks. |
| **Part naming and extruders** | parts `<elementType><n>-<id>`, extruder = colour index in order of first appearance, parts sorted by element type | `bambu3mf.py`: one part per colour solid, names from the render, extruder per colour | Same model (one colour per part, extruder per colour). Order of first appearance vs ScadBuddy's own filament order: worth matching when exporting for their editor so filament slots line up. |
| **Printer profiles** | a table per printer × nozzle, plate size picks the printer, 40 mm wipe margin | `plate_profiles.py`, `plate.fit_problem` | Equivalent; their table has H2D/H2S/H2C/A2L/P2S/X2D entries ScadBuddy can check its own against. |
| **Autosave** | 3 s debounce on `content`, cover every 30 s, flush on unload | presets saved explicitly; the Sign Maker's editor (spec §7) adopts their cadence | Adopt for any template-owned editor, not only signs. |
| **Assets** | `name` → `{url, objectName}`; fetched at load; uploads by presigned S3 config, `bizType` names the use | `assets` table (`library/assets.py`), `ClaimStore` for large request bytes, `// file` parameters | Equivalent; ScadBuddy's pointer is `{asset, sha256, media_type}` (sign spec §4). |
| **Nesting** | a document embeds another tool's document (`vectorizeContent`), each with its own `version` | a pipeline's inputs are one object; a mesh model's scene is one file | A loader keyed by `version` (`NamePlate V1.1`, `Vectorize V1.0`, ScadBuddy's own `v`) is the one dispatch point; the sign loader needs the keychain one anyway. |
| **Headless export** | `up({json, svgAssets, fontAssets})` → 3MF, no API | `signs_compile` activity (sign spec §6.2) → `model.scad` → OpenSCAD → 3MF | Same shape: files in, 3MF out, on a worker. |
| **OpenSCAD** | Parametric Model Maker (category 6, `OSCAD`): `/pmm/template`, `/scad/gethistoryid`; editor not captured | the whole product | The next thing to read: how they expose a customizer, what a "pmm template" is, and whether its make `content` is a params document like ScadBuddy's inputs. |
| **Partner tools** | Go WASM apps behind an `appid`, `thirdparty_generator` API for exports, likes, membership | template-owned UIs and pipelines (#423, #425) | Their partner surface is the analogue of ScadBuddy's template UI contract (`template-ui/host.ts`); the API shows what a host owes a tool: an export record, a like, a membership check, a content fetch. |

## 8. What to take

1. **The carrier.** Write `Auxiliaries/Templates/template.json` (the inputs or scene
   document, verbatim) and `Auxiliaries/Templates/assets/<name>` into every output 3MF,
   beside `ScadBuddy:provenance` (which gains the document's kind and version). Read
   them back on import, dispatching by version: `NamePlate V1.1` → the sign loader,
   `Vectorize V1.0` → the keychain loader (needed by the sign loader anyway),
   ScadBuddy's own → reopen the project. Export for MakerWorld's editor is then the
   same writer with `MakerLab` / `MakerLabVersion` metadata set to their values.
2. **The project shape.** Treat `{title, content, assets, category, cover}` as the
   shape of a saved project across every template-owned editor: ScadBuddy's
   `saved_presets` already is `content` (`inputs`); add the cover and the asset list.
3. **The autosave cadence** (3 s content, 30 s cover, flush on unload) for every
   template-owned editor.
4. **Read Parametric Model Maker next.** It is their OpenSCAD tool; its make
   `content`, `/pmm/template` and the customizer's parameter format are the direct
   comparison for ScadBuddy's inputs, and were not reached.

## 9. Not verified

- Partner tool internals (Turnstile).
- Parametric Model Maker, Make My Vase, Lithophane, Pixel Puzzle, Desk Organizer and
  the AI tools' `content` formats: only the sign's and the keychain's were read.
- `/my/make/<id>/additionalinfo` and `/webcontent`: which tools set
  `isPutAdditionalInfo` / `isPutWebContent`, and what the second document holds.
- `design2d` / `instance2d` / `preset/published` in design-service
  (`_app:3288–3296`): a 2D-design publication path that may be how a make's document
  is published beside a model.
