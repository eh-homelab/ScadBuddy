# MakerLab's `creation-service` API, endpoint by endpoint, against ScadBuddy's

`makerlab-protocol.md` names the API the MakerLab tools share. This document reverses
each endpoint of the make/template/asset family: the request a tool sends, the fields
of the response it reads, what the server does with it, and which ScadBuddy operation
is the counterpart. §4 is the diff.

Method: the client module in `_app-de45e9df87af6b5e.js` (lines 1690–2500 of the
prettier copy) defines 166 creation-service calls as `(args) => service({url, method,
data|params})`; its export table maps each minified name (e.g. `yg7`) to its endpoint.
Every call site of those names across the captured chunks (`scratchpad/mw/api-callsites.txt`,
73 sites) was read for the body it builds and the response fields it destructures. The
transport is `29909-a9165aa8186031b0.js` modules 81462/81787/50771. Response shapes
marked *captured* come from recorded responses of a logged-in session
(`scratchpad/mw/mms/*.json`, 2026-10-14); the rest are what the client reads and no
more. Endpoints with no captured call site are listed with their verb and path only.

## 1. Transport

- **URL:** `/api` + `/v1/creation-service` + path (`createService`, 29909:81462; the
  same table maps `design-service`, `user-service`, `point-service`, …). Query
  parameters are `URLSearchParams(params)`. Bodies are JSON (`Content-Type:
  application/json`), or `FormData` passed through.
- **Auth:** the browser session cookie, same origin. A CSRF token is fetched from
  `/api/csrf` and sent as `x-bbl-csrf-token` (`_app:41076`, `46178`). No bearer token
  on these calls.
- **Client headers** on every call (`_app:4711`): `X-BBL-Client-Type: web`,
  `X-BBL-Client-Version: 00.00.00.01`, `X-BBL-App-Source: makerworld`,
  `X-BBL-Client-Name: MakerWorld`. Query parameters matching `x-bbl-mock-*` are copied
  into headers (a dev hook, `_app:4707`).
- **Tracking:** when a referral (`ref_`) is in the page, it is injected into every GET's
  params and every JSON body (`_app:4722–4735`).
- **Response:** the JSON body as is, no envelope (`createMutator` returns `S.data`,
  29909:81787). Server-side rendering caches GETs for 60 s; the browser never caches.
- **Errors:** a non-2xx becomes `ResponseError {status, headers, body, request}`
  (29909:19553); a transport failure `NetworkError`. The body is `{code, error}`
  (`_app:5962–5985`: `P.body.code`, `P.body.error`). The global `window.onApiRespond`
  handler (`_app:6027`) shows `body.error` in a snackbar (or a permission dialog for
  the account-ban codes) **unless** the call passed an options callback returning
  `{suppressError: false}`, and then rethrows to the caller. `window.onRetry` can
  solve a captcha challenge once and repeat the call with the captcha token header
  (`_app:5987–5999`).
- The Cloudflare challenge in front of `makerworld.com` is separate from all of this:
  a headless browser never reaches the API.

## 2. The make family

A *make* is one saved project of one tool; `category` is the tool (makerlab-protocol §1).
Every call below is `creation-service`; paths are relative to it.

| Endpoint | Request | Response (fields read) | What it does | Callers (chunk:line) |
|---|---|---|---|---|
| `POST /my/make` | `{category: 1, cover: "", title: "<i18n default>", content: ""}` | `{id}` | Creates an empty make; the editor then navigates to `/makerlab/<tool>/edit/<id>?from=makerlab`. Keychain creates a *sign* make this way for "Export to Make My Sign" (`createNameplateProject`). | `pages_makerlab_makeMySign:679, 783`; `pages_…imageToKeychain_edit:729` |
| `GET /my/make/<id>` | | *captured:* `{id, title, cover, coverUrl, content, assets: {svgAssets: [{name, url, objectName}], imageAssets}, instanceId, productName, categoryName, keyChainTemplateId}` | The record. The editor page gets it server-side as `nameplateDetail` (`page_makeMySign_edit:673`); at run time it is re-read only to roll back a failed title save (`:725`). | `page_makeMySign_edit:725`; keychain `:540` |
| `PUT /my/make/<id>` | one of `{content}`, `{title}`, `{cover: <objectName>}`, `{asset: {svgAssets: [{name, url: "", objectName}]}}`, or `{asset: {svgAssets: []}, content: "[]"}` (keychain reset) | nothing read | Partial update; each field independently. `content` is the autosave (3 s debounce, makerlab-protocol §3.1). `cover` is an S3 object name from §3. `asset.svgAssets` replaces the asset list. | `page_makeMySign_edit:189, 693`; keychain `:242, 534, 669, 694` |
| `PUT /my/make/<id>/webcontent` | `{content}` | | The same save for a tool whose page sets `isPutWebContent` (none of the captured ones). | hook only |
| `GET`/`PUT /my/make/<id>/additionalinfo` | `{additionalInfo: <string>}` | | A second document on the make, for tools with `isPutAdditionalInfo`. | hook only |
| `DELETE /my/make/<id>` | body `{id}` | | Delete; the list card and the editor's menu. | `86645:472`; `page_makeMySign_edit:744`; keychain `:553` |
| `POST /my/make/<id>/copy` | `{}` | `{id}` | Duplicate; opens the copy in a new tab. | `86645:485`; `page_makeMySign_edit:734` |
| `GET /my/makes` | `?category=1&offset=0&limit=30` | `{hits: [{id, title, cover, lastUpdateTime, …}], total}` | The user's list for one tool, newest first. The sign editor lists *keychain* makes (`category: 7`) for its import dialog, and the keychain editor lists sign makes. | `pages_makerlab_makeMySign:759`; `page_makeMySign_edit:640`; keychain `:493` |
| `POST /my/makebytemplate/<templateId>` | `{cover: "", title}` | `{id}` | New make from a gallery template. | `pages_…makeMySign_template:159` |
| `POST /my/createfromshare/<instanceId>` | | | New make from a published instance whose design allows it (`allowReCreation` on `design-service/design/<id>`). Not captured. | the model page (not captured) |
| `POST /my/createfromdesign/<designId>` | | | The same from a design. Not captured. | |
| `POST /my/make/snapshot` | `{makeId, errorCode?}` | | Asks the server for a cover snapshot (keychain, after a failed client screenshot). | keychain `:691` |
| `POST /my/make/statistics` | `{makeId, category?}` | `{region, mlFileId}` | **Registers an export.** The response is what the 3MF writer stamps as `MakerLabRegion` / `MakerLabFileId` / `DesignModelId FMI_<id>` (makerlab-protocol §4). Called once per download, wrapped in the generator tracker (`generatorId`, `format`). | `page_makeMySign_edit:942`; keychain `:779` |
| `POST /my/make/securitycheck` | `{text, image?: [url]}` | `{isPass}` | Content moderation. The sign calls it with the new title and the screenshot URL before a title save or a share. | `page_makeMySign_edit:714` |
| `POST /my/make/pointtask` | | | Points reward for a make action. Not captured. | |
| `GET`/`POST /my/make/setting/keepuploadedimages` | | | The "keep my uploaded images" consent. | hook only |
| `GET`/`POST /my/make/removebackground` | `POST {makeId, imageObject, category}` → `{objectKey}`; `GET ?objectKey=` → `{status: "success"\|"failed", removeBackgroundPic}` | | Background removal as a polled job (2 s), on an uploaded image. | keychain `:145, 156` (the `removeBg` hook, module 28113) |

## 3. Templates, presets, uploads

| Endpoint | Request | Response | What it does | Callers |
|---|---|---|---|---|
| `GET /template` | `?category=1&offset=0&limit=20` | *captured:* `{total, hits: [{id, title, titleTranslated, cover, user: {uid, name, avatar, …}, useCount, designId, content}]}` | The gallery for one tool. `content` is a full document; `designId` links the template's published model. | `page_makeMySign_edit:637` |
| `GET /template/<id>` | | *captured:* `{id, title, cover, user, useCount, content}` | One template; the editor's "start from template" reads `.content`. | `page_makeMySign_edit:643` |
| `POST /my/preset` | `{preset: <JSON string>}` | | The tool's editor settings for this user (units, reference overlay, …), one string, versioned like a document. Read server-side as `userPreset.preset`. | `page_makeMySign_edit:757` |
| `GET /make/<id>/upload` | `?fileName=cover<id>&bizType=nameplateCover&category=1[&extraHeader]` | `{fileId, presignedUrl, url, storageInfo: {objectName}}` | **Upload ticket.** The client then `PUT`s the bytes to `presignedUrl` (S3; `Content-Type` set, no disposition; keychain `:86–110`, module 41930 → 90791 `Dy`) and refers to the file by `storageInfo.objectName` in a later `PUT /my/make/<id>` (`cover`, `asset.svgAssets[].objectName`) or `imageObject`. `bizType` names the use: `nameplateCover`, `nameplateScreenshot`, `vectorizeCover`, `vectorizeoriginalpic`, `common_v2`. | `page_makeMySign_edit:204, 693`; keychain `:145, 629` |
| `PUT /make/<id>/upload/<fileId>` | | | Finalises or renames an upload. Not captured. | |
| `POST /common/permanent_files` | `{makeId, fileIds: [fileId…], categoryId}` | | Pins uploads to the make by slot index (`permanentFileIdsRef[i] = fileId`), so they outlive the upload bucket's expiry. | `page_makeMySign_edit:230` |
| `PUT /common/permanent_files/delta` | | | The incremental form. Not captured. | |
| `POST /my/upload/file`, `GET /my/upload/files`, `PUT`/`DELETE /my/upload/file/<id>` | | | The user's standalone upload list (fonts, SVGs). Not captured. | |
| `GET`/`POST`/`PUT`/`DELETE /my/filament`, `/my/colorpalette` | | | The user's filament list and saved palettes (the colour picker's "My Filaments"). Not captured. | |
| `GET /my/customize/favourite`, `/recent` | | | Favourites and recents of the *customizer* (parametric models). Not captured. | |
| `GET /pmm/template`, `GET /scad/gethistoryid` | | | Parametric Model Maker (OpenSCAD). Not captured. | |

## 4. Image to Keychain's own group (`/vectorization`)

The keychain make is **not** edited through `content`: its document (`Vectorize V1.0`)
is saved with `PUT /vectorization/<id>/additionalInfo {additionalInfo}` (the autosave
hook with `isVectorize`), its `content` stays `"[]"`, and its traced SVG is an asset
named `vectorize` (keychain `:669`). The record is read with `GET /vectorization/<id>/detail`
→ `{id, title, assets, instanceId, additionalInfo, keyChainStatus ("generated" when
done), keyChainTemplateId, userImage: {url}, vectorizationV1: {status, image,
imageObjectName, startTime, averageTime}, vectorizationV2: {status, image, isAdapted,
jobWait, …}}` (`:496, 569, 634`).

| Endpoint | Request | Response | What |
|---|---|---|---|
| `POST /vectorizationv2/<id>/vertorize` | `{imageObjectName, ncluster: 16}` | | Start the server trace (v2: colour-quantised, 16 clusters). |
| `GET /vectorizationv2/<id>/vertorize` | | `{jobWait, status, imageObjectName, image, isAdapted, startTime, averageTime}` | Poll it. |
| `POST /vectorizationv2/<id>/cancel` | `{}` | | Cancel. |
| `POST /vectorizationv1/<id>/vertorize` | `{imageObjectName, params: "processing.max_colors=16;output.curves.allowed.circular_arc=false;…"}` | | The v1 tracer (vtracer-style key=value options, `:459`). |
| `GET /vectorizationv1/<id>/vertorize` | | status | Poll. |
| `POST /vectorization/<id>/result` | `{version}` | | Records which trace the user accepted. |
| `GET /vectorization/<id>/resource`, `/slice`, `POST /slice`, `/slice/clear`, `/notification` | | | Slicing a keychain server-side for mobile print, and its push notification. Not captured. |

So MakerWorld keeps **two** per-tool document slots on a make, `content` and
`additionalInfo`, and a tool picks one; the sign uses the first, the keychain the second.

## 5. The diff against ScadBuddy

ScadBuddy's side is `backend/openapi.json` (exported 2026-10-15, 240 operations). The
nearest counterpart of a *make* is a saved preset of a template plus the outputs
rendered from it; of a *template*, a template's own `presets`.

| Concern | MakerLab | ScadBuddy | Difference |
|---|---|---|---|
| Identity | A make id, global; `category` says which tool. | A preset id under a model slug (`/models/{slug}/presets/{id}`); an output id, global, that names its `slug`. | Theirs is "my projects, filtered by tool"; ours is "this template's presets". A cross-template "my projects" list has no route. |
| Create | `POST /my/make {category, cover, title, content}` → `{id}`; empty content first, document follows. | `POST /models/{slug}/presets {name, description, tags, params, inputs}` → `ParamPreset {id, name, description, tags, params, inputs, origin, updated_at}`. | Ours takes the document on create and types it (`inputs` is checked by `render/inputs.py`); theirs is opaque. Ours has no cover on a preset. |
| Update | `PUT /my/make/<id>` with any one of `content` / `title` / `cover` / `asset`. | `PATCH /models/{slug}/presets/{id} {name?, description?, tags?, params?, inputs?}`. | Same partial-update shape. Missing on ours: `cover`, an asset list. |
| Second document | `additionalinfo` beside `content`. | one `inputs` object. | Ours would nest (`inputs.<tool>`), not add a slot. |
| Read | `GET /my/make/<id>` returns the document inline. | `GET /models/{slug}/presets` (array, document inline), `GET /outputs/{id}` (`OutputDetail.inputs`). | Equivalent. |
| List | `GET /my/makes?category&offset&limit` → `{hits, total}`, newest first. | `GET /models/{slug}/presets` → array, unpaged; `GET /models/{slug}/outputs`. | Ours is unpaged and per model. |
| Duplicate | `POST /my/make/<id>/copy {}` → `{id}`. | `POST /models/{slug}/presets/{id}/duplicate`. | Equivalent. |
| Delete | `DELETE /my/make/<id>` (body `{id}`). | `DELETE /models/{slug}/presets/{id}`. | Equivalent. |
| Templates | `GET /template?category` → `{hits: [{id, title, cover, user, useCount, designId, content}]}`; `POST /my/makebytemplate/<id> {cover, title}`. | A template's `presets` in `model.json` (`ModelPatch.presets`), read-only; the gallery (#273). | Ours has no `useCount`, no author, no cover per preset. |
| From a published thing | `POST /my/createfromshare/<instanceId>` (gated by the design's `allowReCreation`). | `POST /models/import {url}`, `POST /models/from-mesh {library_file_id \| output_id}`. | Ours imports the *model*; theirs copies the *project document* of a published make. The 3MF carrier (makerlab-protocol §8) is how ours gets the latter. |
| Cover | Client renders a PNG, `GET /make/<id>/upload` → presigned S3 `PUT`, then `PUT /my/make/<id> {cover: objectName}`; refreshed every 30 s. | Thumbnails are rendered server-side (`render/thumbnail.py`) for outputs and the model; `PUT /models/{slug}/thumbnail` (multipart) and `PUT /outputs/{id}/thumbnail`. | Ours never asks the client for a cover; a preset has none. |
| Uploads | Presigned S3 ticket per file (`bizType` names the use), referenced by `objectName`; `POST /common/permanent_files` pins them. | `POST /models/{slug}/assets` (multipart) → `AssetMeta {id, kind, name, size, width, height}`; `POST /models/{slug}/assets/fetch {url}`; bytes through the API, kept alive by use (`assets` table, #591). | Direct-to-storage vs through the API; object name vs asset id. Equivalent otherwise. |
| The 3MF | Built **in the browser** by the shared writer; the server only registers the export (`POST /my/make/statistics` → `{region, mlFileId}` stamped into the file). | Built **on the server**: `POST /models/{slug}/render {inputs}` → job → `POST /models/{slug}/outputs {job_id, index, inputs, name}` → `GET /outputs/{id}/model.3mf`; provenance stamped by `render/provenance.py`. | Opposite split. Theirs has no server copy of the file; ours has no client writer. The `mlFileId` is their equivalent of our `ScadBuddy:provenance` `output` id. |
| Send to printer | Bambu Studio / Handy open the downloaded file; mobile slicing via `/vectorization/<id>/slice`. | `POST /outputs/{id}/send`, `POST /outputs/{id}/project-file` (Bambuddy). | Theirs has no printer in the loop; ours does. |
| Moderation | `POST /my/make/securitycheck {text, image}` → `{isPass}` on titles and screenshots. | none | n/a for a self-hosted tool. |
| User settings | `POST /my/preset {preset: "<json>"}`, one string per tool. | none server-side. | Ours would be a settings row per template, if wanted. |
| Errors | `{code, error}` body, `ResponseError{status, body}`, global snackbar unless `suppressError: false`. | RFC 7807-style problem bodies (`workflows/problems.py`), `command()` retries with `Idempotency-Key` and follows 202. | Theirs has no idempotency key and no long-running operation shape; a save is a plain PUT. |

What the diff says to build, in order of what the Sign Maker needs:

1. **A cover and an asset list on a saved preset** (`saved_presets`: `cover`, `assets`),
   and the opaque-document slot beside `inputs` for a template-owned editor's own
   format (`inputs.sign` in the sign spec §4 is that slot).
2. **A paged "my projects" list across templates** (`GET /presets?template&offset&limit`),
   which is the make list; today the panel would have to walk every model.
3. **Copy a published project** = read `Auxiliaries/Templates/template.json` from a 3MF
   (makerlab-protocol §8); there is no API to add, only the loader.
4. The rest (presigned uploads, server snapshots, moderation, user preset strings) is
   not worth copying: the API-side upload and the server-rendered thumbnails are the
   simpler design for a self-hosted system.

## 6. Not verified

- `createfromshare` / `createfromdesign` bodies and responses (the model page chunk
  was not captured).
- The `additionalinfo` GET's shape; the `/my/upload/*` list's shape; `/my/filament` and
  `/my/colorpalette` records.
- `POST /my/make/statistics`: whether it increments a public export count or only
  mints the file id.
- Anything under `/pmm` and `/scad` (Parametric Model Maker).
