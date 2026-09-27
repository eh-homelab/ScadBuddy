---
name: customize
description: Drive the ScadBuddy customizer for a template - read its parameter schema and presets, choose values, render and inspect the preview, check the plate fit, and keep the result as an output. Use when asked to make, tweak or preview a customized model (a keychain with a name, a box of a given size) on a ScadBuddy instance.
---

# Driving the ScadBuddy customizer

The customizer turns a template's parameters into a render: a colour preview and
a multi-colour 3MF. This skill is the workflow. Sources, relative to the root of
[eh-homelab/ScadBuddy](https://github.com/eh-homelab/ScadBuddy):

- `docs/superpowers/specs/2026-09-22-scadbuddy-design.md` ("main spec"): §5 for
  the customizer, §6 for the render pipeline, §8 for the API.
- `backend/openapi.json`: the exact routes, request bodies and responses. The
  paths below are its paths.
- `docs/superpowers/specs/2026-09-27-ai-integration-design.md` ("AI spec"): §5
  for tools, §8 for tiers and approvals.

## Tools, and the routes behind them

The ScadBuddy MCP tools arrive with issue #251. They are task-shaped rather than
one per route: for example, `render_model` submits a render and waits for it
(AI spec §5.1). Until they exist, and to see what a tool does, the routes below
are what the tools call. Every path is under `/api/v1` and is listed in
`backend/openapi.json`. Use the tool when you have it. Don't call the HTTP routes
yourself unless you have been given a way to.

In the tool names, the harness sees `mcp__scadbuddy__<tool>`, and a Claude Code
install of this plugin sees `mcp__plugin_scadbuddy_scadbuddy__<tool>`
(AI spec §5.1; [plugin MCP tool names](https://code.claude.com/docs/en/plugins/components)).

## 1. Find the template

- `GET /api/v1/models` lists the catalogue. A built-in template's id is
  `builtin:<slug>`, and every `{slug}` route takes it (main spec §4.2 and §8).
- `GET /api/v1/models/{slug}` returns the record: name, description, tags,
  `version` (the current commit) and `origin` (`builtin` or `mine`)
  (main spec §4.2).
- Built-ins are read-only. To change one, duplicate it first with
  `POST /api/v1/models/{slug}/duplicate` (main spec §8).

## 2. Read the schema and the presets

`GET /api/v1/models/{slug}/schema` returns `{title, groups, parameters,
source_sha256}` (`backend/openapi.json`, `CustomizerSchema`). What each
parameter type means (main spec §5.1 and §5.2):

| `type` | Send | Notes |
|---|---|---|
| `slider` | a number within `min`..`max`, on `step` | |
| `number`, `integer` | a number | `integer` when both the step and the initial are whole |
| `string` | a string of at most `maxLength` characters | |
| `boolean` | `true` / `false` | |
| `select` | one option's `value` (not its `name`, which is the label) | |
| `color` | `"#RRGGBB"` | The order of colour parameters is the extruder order (main spec §7). |
| `font` | an installed family, e.g. `"Lobster Two:style=Bold"` | See step 3. |
| `file` | `""`, an uploaded asset's `id`, or one of the parameter's `samples` | See step 3. |

- Groups come in the order they first appear, and `Hidden` parameters are left
  out (main spec §5.1).
- `GET /api/v1/models/{slug}/presets` lists the template's own presets
  (`origin: template`, read-only) and then the saved ones (`origin: mine`). A
  preset holds only the values it sets, so apply it over the defaults
  (`backend/openapi.json`, that route's description).
- To use an older revision, `GET /api/v1/models/{slug}/versions` lists them and
  `GET /api/v1/models/{slug}/versions/{commit}/schema` gives that revision's
  schema (main spec §8).

## 3. Choose values

- **Send only parameters in the schema, with the right types.** A render refuses
  an unknown parameter with a 422 (main spec §6.1).
- **Fonts.** A family that isn't installed silently renders in DejaVu, which
  changes the geometry. `GET /api/v1/fonts` lists the installed families, and
  `GET /api/v1/fonts/catalogue?q=` searches Google Fonts. Installing a family
  (`POST /api/v1/fonts/install`, body `{family}`) downloads it onto the data
  volume (main spec §5.4 and §8; `CLAUDE.md`, section "Verified OpenSCAD facts").
  There is no "Lobster" family, only "Lobster Two" (`CLAUDE.md`, same section).
- **Files.** `POST /api/v1/models/{slug}/assets` (multipart `file`, SVG or PNG,
  at most 8 MiB) stores a picture and returns its `id`. Pass that id as the
  value, never a path. A sample's value is its bare name (main spec §5.5).
- **Colours.** Two colour parameters with the same value share one extruder
  (main spec §7). Say so if the user sets two parts to the same colour and
  expects two filaments.

## 4. Render and inspect

1. `POST /api/v1/models/{slug}/render` with `{params, version?}` answers 202
   with `{job_id, status_url}` (`backend/openapi.json`, `RenderAccepted`;
   main spec §8).
2. `GET /api/v1/jobs/{job_id}` until `status` settles. The job reports `params`,
   `colors`, `parts`, `bbox_mm`, `warnings`, `error`, `log_tail` and
   `preview_url` (`backend/openapi.json`, `JobStatus`).
3. `GET /api/v1/jobs/{job_id}/preview.glb` is the preview mesh, one mesh per
   colour (main spec §6).

The preview **is** the render. The same job produces the GLB and the 3MF that
Generate keeps (main spec §5.3). Read `warnings` out to the user word for word.
The ones that matter:

- A colour that fell back to open parts, or `uncoloured geometry present; parts
  are not closed`. The print will still slice, but those parts aren't closed
  solids (main spec §6.3).
- `OpenSCAD could not open <name>; the model rendered without it`, for a missing
  file parameter (main spec §5.5).
- `plate thumbnail timed out; the 3MF carries no cover image`
  (main spec §6.2.1).

A render has a hard timeout (`SCADBUDDY_RENDER_TIMEOUT`, 120 s by default), and a
job that hits it fails with the log tail (main spec §6.1).

## 5. Check the plate

- `GET /api/v1/plate/fit?x=&y=&z=&model=&colours=` says whether a bounding box
  fits a printer model's plate. Pass `colours` > 1 for a multi-colour model,
  because that needs a prime tower (`backend/openapi.json`, the route's
  parameters). Take `x`, `y` and `z` from the job's `bbox_mm`.
- `GET /api/v1/plate` is the plate for one printer model, and an unknown model
  gets the configured default. `GET /api/v1/plates` lists every plate
  (`backend/openapi.json`).

## 6. Keep the result

- **Generate:** `POST /api/v1/models/{slug}/outputs` keeps a finished job as an
  output (main spec §5.3 and §8). An output is what gets printed (the `print`
  skill).
- `GET /api/v1/models/{slug}/outputs` lists the outputs with their parameters.
  `GET /api/v1/outputs/{output_id}/model.3mf` downloads one, and
  `GET /api/v1/outputs/{output_id}/plates` lists its plates (main spec §8;
  `backend/openapi.json`).
- **Save the values** as a preset with `POST /api/v1/models/{slug}/presets`
  (`{name, params}`). Names are unique per template, ignoring case, and at most
  80 characters (`backend/openapi.json`, that route's description).

## Approvals

Rendering, generating outputs and saving presets are `write` tier: reversible,
and no approval needed. Deleting anything, sending or printing, and changing
settings are `outward`, and always need a human approval in the ScadBuddy UI
(AI spec §8.1 and §8.2). The `print` skill covers the outward half.

## Not yet available

- Driving the user's open ScadBuddy tab (moving sliders they can see) needs the
  browser bridge, issue #254 (AI spec §5.2). Until then, work through the render
  tools and tell the user which values you chose.
- Structured authoring diagnostics and multi-view preview images arrive with
  issue #252.
