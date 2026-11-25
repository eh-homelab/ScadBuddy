# Storefront listings on pipeline templates (#5701)

Date: 2026-11-17. Status: draft. The owner answered its questions on 2026-11-18 (§12),
and this text applies the answers. Nothing is built before the owner approves it. The storefront session (scadify) reviews it too, because its #6581 changes
the slice price at the same time (§7.3).

Builds on:

- the template pipelines spec, [2026-09-27](2026-09-27-template-pipelines-design.md)
  §4.3 (inputs), §5 (pipelines) and §8.2 (inputs versions);
- the storefront spec, [2026-11-02](2026-11-02-storefront-design.md) §5.3-§5.4;
- "Sell on storefront", [2026-11-13 easy listing](2026-11-13-storefront-easy-listing-design.md);
- the customize page and its price, [2026-11-13 customize UX](2026-11-13-storefront-customize-ux-design.md) §5.4, §5.6.

## 1. Why

When this was written, publish refused a template with a pipeline, with 422
`listing_pipeline_unsupported` (PR 2 replaced it, §11).
The reason is in the issue: a pipeline takes all of `inputs`, not only `params`. A
template's own UI keeps state outside `params` (the dollhouse's `house`), and the
pipeline renders other things from it. `ListingInput` has no place for that state, so a
listing would render with UI state that the shop never saw.

The owner asked on 2026-11-17 for pipeline listings to be supported. This design says
how, for a first version in which the customer never changes the pipeline's own inputs.

## 2. Facts it rests on

Each fact names where and how it was measured. "Read" means a read of the code on
`origin/main` 41e438599, on 2026-11-17.

1. **One real template has a pipeline: `dollhouse-kit`.** `models/dollhouse-kit/model.json`
   declares `"ui": {"module": "ui/index.js", "slot": "page", "api": 1}` and
   `"pipeline": {"module": "pipeline/pipeline.py", "api": 1}`. `models/maze-puzzle` has a
   `ui/` (slot `panel`) and no pipeline, and its UI writes nothing outside `params` (no
   `inputs.set` or `bind` in `models/maze-puzzle/ui/index.js`). No other directory in
   `models/` has a `pipeline/` or `ui/`. Measured with `ls models/*/` and a read of each
   `model.json`.
2. **The dollhouse's inputs.** `pipeline/pipeline.py` has `INPUTS_VERSION = 1`. `run`
   reads `inputs["house"]` (`cols` 1-4, `rows` 1-3, `storeys` 1-3, `windows` 0-14,
   `LIMITS`; it clamps any other value, `clamp_house`) and `inputs["params"]` (the
   style, passed to `model.scad` for each piece, less the keys the pipeline sets itself:
   `SHAPE` = `piece`, `course`, `width_units`, `depth_units`, `preview`). `migrate(inputs, 0)`
   adds a clamped `house`. The pipeline calls `ctx.output` once: one multi-plate 3MF, a
   `bom` and `assembly.svg`. `pipeline/verify-inputs.json` holds two inputs at `v` 1.
   Read.
3. **Prod's dollhouse outputs.** `builtin:dollhouse-kit` has 9 saved outputs. Every one
   has `inputs.v` 0 and an empty `bom`, and the newest is from 2026-10-10. One has a
   `house` (`{"cols": 3, "rows": 1, "storeys": 2, "windows": 1}`). So no saved output on
   prod comes from the whole-house pipeline at `v` 1. Measured with the ScadBuddy MCP
   tool `list_outputs` (`slug` `builtin:dollhouse-kit`), 2026-11-17.
4. **No template declares an input schema today.** `PipelineDeclaration`
   (`backend/scadbuddy/library/catalogue.py`) has only `module` and `api`. Pydantic
   ignores other keys there, so a new optional key breaks no template. Read.
5. **A closed JSON Schema subset exists already, for flows.** `backend/scadbuddy/flows/inputs.py`
   checks a flow's `inputs.json` (its schema check moved to `core/input_schema.py`, #5701) (scalars and arrays of scalars at the top level,
   `additionalProperties: false`, size and pattern limits) with `jsonschema`, which is a
   backend dependency (`backend/pyproject.toml`, `jsonschema>=4.23`).
   `frontend/src/components/flows/StartRun.tsx` draws a form from it. Read.
6. **The API reads `INPUTS_VERSION` without running template code.**
   `library/pipelines.py` `inputs_version_of` parses `pipeline.py` with `ast`. Template
   Python runs only on the render worker: a render in `TemplatePipeline`, and a migration
   in the `MigrateInputs` workflow that `render/submit.py` `RenderService.migrate_inputs`
   starts. Read.
7. **Most of the render path takes whole inputs already.**
   - `ListingSnapshot.pipeline` exists (`orders/storefront/wire.py`). It was "always
     false for now"; since PR 2 it is true for a pipeline template.
   - `renders.py` `start_render` and `prepare.py` pass
     `whole_inputs=listing.snapshot.pipeline` to `RenderService.submit_answer`.
   - `reference.py` `render_key_of(..., pipeline=)` keys a pipeline render on its whole
     inputs.
   - `rendering.py` `output_for_reference` sees a pipeline reference by its render key
     and submits it with `whole_inputs`. The order's render, its quote and the storefront
     slice run all use it.
   - The one place that dropped the pipeline's inputs was `inputs.py`
     `listing_reference`: it built `{"params": …, "v": 0}`. PR 2 changed it (§7.1).

   Read.
8. **A pipeline job's result is its first output.** `workflows/pipelines.py` records
   `result=ctx.outputs[0].result, outputs=ctx.outputs`, and `Job.outputs` lists every
   output (`render/job_models.py`). A pipeline may call `ctx.output` more than once
   (pipelines spec §5.2). Each output's `JobResult` is built by `workflows/outputs.py`
   (the end of `build_output`, which `pipeline_activities.py` `write_output` runs):
   `model_3mf` is the one multi-plate 3MF of the output (every plate,
   `write_plates_3mf`), and `colors` is the same `colours` list that writer is given, so
   it lists the whole output's colours in that 3MF's slot order. `preview_glb` holds the
   first plate only (`write_glb(list(plates[0].parts), …)`). Read.
9. **The slice price.** `slice_price.py` `price_variant` renders the variant through
   `output_for_reference`, with `source=STOREFRONT`, so the output stays out of Recent
   renders (#6371, merged in 8de2907a9). It slices every plate through
   `quote.py` `slice_and_price`. `renders.py` `render_estimate` gives no price when the
   render's slug or commit is no longer the listing's. Read.
10. **Sell on storefront reads `params` only.** `frontend/src/components/storefront/sellSource.ts`
    takes `job.params` or `output.params`, and the agent's `storefront_listing_draft`
    takes `output.params` (`agent/src/tools/storefront.ts`). Both drop `house`. Read.
11. **Listings pin a git commit.** `ListingInput.commit` must match `LISTING_COMMIT`
    (`^[0-9a-f]{40}$`, `wire.py`). Publish captures the schema and the source snapshot,
    and holds the snapshot in `blob_refs` while the listing is published
    (`listings.py`, module notes). Read.

12. **A storefront slice reads one output of a render.** Read on `origin/main`
    71227b21d, 2026-11-18:
    - `orders/rendering.py` `output_for_reference` saves the job with `"index": 0`, so
      it saves only the first output. `library/outputs.py` `OutputStore.create` saves
      output `index` of `job.outputs`, else `job.result`.
    - `library/outputs.py` `storefront_output_id(render_key)` gives one output id per
      render key.
    - `slice_price.py` `model_digest(job_id, path)` keeps each digest by job id only.
    - `store/cache.py` `materialize_result(blobs, result)` fetches the files of one
      `JobResult`. `renders.py` `_fetch` and `orders/operations.py`
      `storefront_slice_run`'s `rendered_sha256` call it with `job.result`.
    - `orders/quote.py` `slice_and_price` takes a `start` callable that gives a list of
      started plates. It follows every plate, reads each with `read_plate`, then prices
      all of them in one `price_plates` call. `Slicer.start(output_id, ...)` starts every
      plate of one output.
    - `orders/pricing.py` `price` sums grams per slot over all plates, adds one `setup`
      line per plate, one `labour` line, one `markup` line and one `rounding` line. So
      one call over all plates prices labour and rounding once. Two calls, summed, would
      count them twice.
    - `slice_price.py` `SlicePrices.claim` takes one place of
      `storefront_slices_concurrent` per price key. One `storefront_slice_price`
      operation runs per claimed key.
13. **A back-office revision keeps the other inputs.** `frontend/src/components/orders/printSetups.ts`
    `inputsToSend` sends `{...inputs, params: ...}`, so it keeps every key but `params`.
    `DesignInputs.tsx` does not edit a template with a `ui` at all (`OWN_UI_TEXT`). Read
    on 71227b21d.

Not measured: how long a whole dollhouse takes to render. Prod has no `v` 1 output
(fact 3). §10.4 measures it before go-live.

## 3. Decisions in short

1. `ListingInput` gains `inputs`: the pipeline's own state, without `params` and `v`. The
   customer's bounded values stay in `params`, as today. The two never mix.
2. A pipeline template declares the shape of its inputs in a JSON Schema file,
   `model.json` `pipeline.inputs`. It is the flows' closed subset plus one level of
   objects. Publish checks the listing's inputs against it. A pipeline template with no
   such file cannot be listed (422 `listing_inputs_undeclared`). This code replaces
   `listing_pipeline_unsupported`.
3. Publish brings older inputs up to the commit's `INPUTS_VERSION` with the template's
   own `migrate`, on the render worker, as the Customize page does. The API never runs
   template Python.
4. The customer never changes `inputs` in this version. The customer's values reach only
   `inputs.params`, by construction.
5. The renders, the price and the order's render take whole inputs:
   `listing_reference` builds them from the snapshot. Everything after it works already
   (fact 7).
6. A render whose job made more than one output gets one price over all its outputs
   (§7.4). A render whose fixed inputs are no longer the listing's gets no firm price,
   and the shop prices it by hand.
7. The shop sets the inputs in the template's own UI, opened in a dialog from the
   listing editor. A form drawn from the input schema is the fallback.
8. A listing keeps its commit, its inputs and its snapshot. A template change reaches a
   listing only when the shop republishes it at a new commit.
9. A template names the parameters its pipeline sets itself, in `model.json`
   `pipeline.sets_params`. The editor hides them, and publish refuses a bound on one
   (§6.1).

## 4. ListingInput and publish (question 1)

### 4.1 The template declares its inputs

`model.json`:

```json
"pipeline": {"module": "pipeline/pipeline.py", "api": 1, "inputs": "pipeline/inputs.json"}
```

`inputs` is a path under the template directory, with the same pattern rule as `module`.
The file is a JSON Schema in a closed subset:

- The flows' subset (`core/input_schema.py`, moved out of `flows/inputs.py` by #5701): the top level is
  `{"type": "object", "properties": {...}, "required": [...], "additionalProperties": false}`,
  and each property is a `string`, `integer`, `number`, `boolean`, or an `array` of one
  of those, with `title`, `description`, `default` and the type's own limits.
- Plus one level of `object`: a top-level property may be an object of scalar
  properties, with `additionalProperties: false` and its own `required`. The dollhouse's
  `house` needs this.
- The top level must not declare `params` or `v`. Those belong to the host.
- At most 16 KiB, as `core/input_schema.py` `MAX_SCHEMA_BYTES`. A `pattern` has the flows'
  limits (`MAX_PATTERNED_LENGTH`, the pattern time budget), because the API runs it.

The subset's checker moves out of `flows/inputs.py` into one shared module, which flows
and listings both call. There is no second copy.

The dollhouse's file, `models/dollhouse-kit/pipeline/inputs.json`:

```json
{
  "type": "object",
  "properties": {
    "house": {
      "type": "object",
      "title": "House",
      "properties": {
        "cols": {"type": "integer", "title": "Modules wide", "minimum": 1, "maximum": 4},
        "rows": {"type": "integer", "title": "Modules deep", "minimum": 1, "maximum": 3},
        "storeys": {"type": "integer", "title": "Storeys", "minimum": 1, "maximum": 3},
        "windows": {"type": "integer", "title": "Windows per storey", "minimum": 0, "maximum": 14}
      },
      "required": ["cols", "rows", "storeys", "windows"],
      "additionalProperties": false
    }
  },
  "required": ["house"],
  "additionalProperties": false
}
```

The titles are the UI's `HOUSE_LABELS`. A template test keeps the bounds equal to
`pipeline.py` `LIMITS`, and checks that every case of `verify-inputs.json` passes the
schema.

### 4.2 The wire

`ListingInput` (`orders/storefront/wire.py`) gains two fields:

```python
#: A pipeline template's own inputs (spec 2026-09-27 §4.3), without ``params`` and
#: ``v``: the shop sets them, and no customer changes them (#5701). Empty for a
#: template with no pipeline.
inputs: dict[str, JsonValue] = Field(default_factory=dict)
#: The inputs version ``inputs`` were made at (``inputs.v`` of the output or render
#: they came from). None: the commit's own ``INPUTS_VERSION``.
inputs_v: int | None = Field(default=None, ge=0, le=2**31 - 1)
```

`ListingSnapshot` gains what publish settled, which every render reads:

```python
#: The pipeline's inputs as publish checked them, at ``inputs_version``. Read this,
#: never the row's ``inputs``, for what a published listing renders with.
inputs: dict[str, JsonValue] = Field(default_factory=dict)
#: The commit's ``INPUTS_VERSION`` (0 for a template with no pipeline).
inputs_version: int = 0
```

`pipeline` is now true for a pipeline template, so its docstring ("Always false for
now: publish refuses a pipeline template") is rewritten in the same PR.

A library-file listing (#6710) has no pipeline. `ListingInput` refuses `inputs` and
`inputs_v` on it as it refuses its other template fields (422 `listing_source_invalid`).

Storage: one new migration adds `storefront_listings.inputs jsonb NOT NULL DEFAULT '{}'`
and `inputs_v integer` (null allowed). The snapshot is in its jsonb column already, so it
needs no column. The serialised `inputs` is at most 16 KiB. A row written before the
migration reads as `{}` and null, and a snapshot without the new keys as `{}` and 0.

### 4.3 What publish checks

`_commit_facts` stops refusing a pipeline. For a pipeline template it also reads, at the
commit, without running template code (`orders/storefront/pipeline_inputs.py`, where the
checks below are too):

- `INPUTS_VERSION`, with `inputs_version_of` (fact 6);
- the declared input schema, as data (`json.loads`).

Then `_publish` checks, in this order (first fault wins), before the checks it makes
today. "Stored" is the row's `inputs` and `inputs_v` as the shop saved them, before any
migration; "migrated" is the migration's result, or the stored inputs when no migration
runs:

| Order | Fault | Checked on | Status and code | `name` |
| --- | --- | --- | --- | --- |
| 1 | `inputs` is not empty, and the template has no pipeline | stored | 422 `listing_inputs_unexpected` | `inputs` |
| 2 | A pipeline template with no `pipeline.inputs`, or a file that is missing, too large or outside the subset | the commit | 422 `listing_inputs_undeclared` | `inputs` |
| 3 | `inputs` has a `params` or `v` key | stored, before the migration | 422 `listing_inputs_reserved` | `inputs.params` or `inputs.v` |
| 4 | `inputs` is larger than 16 KiB, or holds what a row cannot (NaN or an infinity, a NUL, a lone surrogate) | stored, before the migration | 422 `listing_inputs_invalid`, `reason` `too_large` or `unstorable` | `inputs` |
| 5 | `inputs_v` is above the commit's `INPUTS_VERSION` | stored | 422 `listing_inputs_invalid`, `reason` `newer` | `inputs` |
| 6 | The template's `migrate` refuses the inputs (only when `inputs_v` is below the version) | stored | 422 `listing_inputs_migration_failed`, `detail` the template's message | `inputs` |
| 7 | A value fails the schema | migrated | 422 `listing_inputs_invalid`, `reason` the failed keyword (`minimum`, `required`, `type`, `additional`, …) | the value's path, `inputs.house.cols`. A missing or an extra key is named itself (`required`, `additional`), not its object |
| 8 | `params` offers a parameter in `pipeline.sets_params`, or locks one at another value than the default (§6.1) | the commit | 422 `listing_param_pipeline_set` | `params.<name>` |

The migrated inputs need no second reserved-key check: publish keeps only their keys
other than `params` and `v` (below). Their size is checked again with row 7, since a
migration can add keys (`reason` `too_large`). PR 2's tests check this order.

A `model.json` whose `pipeline` the catalogue cannot read is still a pipeline
(`declares_pipeline`), and gives row 2, as does a schema file that is a link out of the
template or one over 64 KiB as written.

The codes follow the `listing_*` style, and each carries `name`, so the editor shows it
under the field it names (§5.4). `listing_pipeline_unsupported` is gone: no route answers
it. A page built before the change maps any unknown code to the page's general error,
as today (`listingDraft.ts` `listingRefusal` answers null for it).

**Migration.** When `inputs_v` is below the commit's `INPUTS_VERSION`, publish calls
`RenderService.migrate_inputs(slug, {**inputs, "params": settled_params, "v": inputs_v},
version=commit)`. That runs the template's `migrate` in `MigrateInputs` on the render
worker (fact 6), as the Customize page does when it opens an older output. Publish keeps
the result's keys other than `params` and `v`. It does not follow a change the migration
makes to `params`: the listing's bounds and locks stay the shop's. A 413, 503 or 504 from
the migration passes through unchanged; nothing is published, and the shop tries again.

The `params` checks stay as they are: the bounds and the locks are checked against
`model.scad`'s schema (`checked_fixed`, `_check_bound`). That is all a listing can bound
(the issue's reason). A pipeline whose `params` are not `model.scad`'s parameters can
still be listed, but only with every parameter locked at the schema's defaults.

Publish then stores `inputs` (migrated and checked) and `inputs_version` in the snapshot.
The row's own `inputs` and `inputs_v` stay as the shop saved them; they are the next
publish's input, as `fixed` is today.

Save (`POST` and `PUT /storefront/listings`) checks only the shape: the reserved keys,
the size and what a row can hold (rows 3 and 4). It does not read the commit, as it does
not today. A `PUT` replaces the row, so the editor sends back the `inputs` and `inputs_v`
the listing has (PR 2, `listingDraft.ts` `draftOf`). PR 6 sets them.

## 5. The back-office editor (question 2)

### 5.1 Where the inputs come from

Two ways in, one for each starting point:

1. **Sell on storefront** (easy listing spec §3). The shop designs the house on the
   dollhouse's own page, presses Generate, then Generate's menu "Sell on storefront…".
   `sellSource.ts` reads the job or the output, and for a pipeline template it now takes
   `inputs` less `params` and `v` as the draft's `inputs`, and `inputs.v` as `inputs_v`.
   The smart defaults (`defaultsFor`) run over `inputs.params`, as they run over
   `params` today. This is the main path, and it reuses the template's UI with no new UI
   at all.
2. **"Set up the design" in the editor.** For a new listing, a version change, or a
   change of mind, the editor opens the template's own UI in a dialog (§5.3).

### 5.2 The Design block

The editor's "Design" line (easy listing spec §5) grows into a block for a pipeline
template. It stays at the top, because the inputs decide everything below them:

```
┌ Design ──────────────────────────────────────────────────────────┐
│ Dollhouse Kit, version 3f9a2c1 (current)               [Change]  │
│                                                                  │
│ 3 × 1 house, 2 storeys, 63 pieces  (an example)                  │
│ Set by you. Customers cannot change it.     [Set up the design]  │
│                                                                  │
│ ! Storeys: at most 3.                     (a publish refusal)    │
└──────────────────────────────────────────────────────────────────┘
┌ Product ─────────────┐  ┌ Preview (as the customer sees it) ─────┐
│ ...                  │  │                                         │
```

- The summary line is the template UI's own `describe()` text (host API §4.3), read
  while the dialog is open and kept on the draft. When there is none, the editor writes
  the schema's titles and values: "House: Modules wide 3, Modules deep 1, Storeys 2,
  Windows per storey 1".
- "Set by you. Customers cannot change it." says the rule of this version once, in plain
  words. No JSON is shown.
- A refusal whose `name` starts with `inputs` shows here, in the block, with the schema's
  title for the path ("Storeys: at most 3."), never the raw path.
- A template with no pipeline shows the Design line as today.

### 5.3 The "Set up the design" dialog

A large dialog (the page slot needs the room) that mounts the template's UI with
`TemplateUi` (`frontend/src/template-ui/TemplateUi.tsx`) at the listing's commit. The
host (`template-ui/host.ts`) is built over the draft, not over the Customize page:

| Host call | In the dialog |
| --- | --- |
| `inputs.get` | `{...draft.inputs, params: the listing's locked values and defaults, v}` |
| `inputs.set` | changes the dialog's working copy only |
| `schema` | the schema at the listing's commit |
| `files.url` | the template's files at the listing's commit |
| `generate` | a back-office render of the working copy, for `<sb-preview>`; it saves no output |
| `openPrint` | does nothing |
| `presets.list`, `presets.load` | the template's presets, to start from one |
| `presets.save` | refused: a listing is not a preset |

The dialog ends with two buttons: "Use this design" and "Cancel". "Use this design"
writes the working copy back:

- every key but `params` and `v` becomes `draft.inputs`, and `v` becomes `inputs_v`;
- each value in `params` becomes the locked value of a locked parameter, or the starting
  value of an offered one (`fittedDefault`, as the editor's "Starts at" does). The
  bounds and the colours on offer do not change.

If the module fails to mount (the host falls back to a form for any template, §4.2 of
the pipelines spec), the dialog draws a form from the input schema instead, with the
flows' `StartRun` controls (fact 5). A pipeline template with no UI gets the same form.

Visual rules, from the frontend-design pass: the dialog is the app's own (its tokens,
its type, its buttons), and the template's UI inside it keeps the look its shadow root
gives it. The one strong element is the template's UI itself; the chrome around it is
quiet: a title ("Set up the design"), the two buttons, nothing else. A phone gets the
dialog full screen, as the dollhouse's own one-column layout expects.

### 5.4 Other editor changes

- `listingDraft.ts` gains `inputs` and `inputs_v` on the draft; `listingInput` sends
  them; `savedKey` includes them, so a change to the inputs is an unsaved change.
- `listingRefusal` gains kind `inputs` for the five new `listing_inputs_*` codes, with
  `name` and `reason`. `listing_param_pipeline_set` shows under the parameter it names,
  as the other `params.<name>` refusals do.
- Editing the inputs of a published listing unpublishes it on save, as any edit does.
  The page's note ("Saving a draft takes this listing off the store…") covers it.
- The live preview (`previewListing`) shows a pipeline listing's settings as today. The
  inputs do not show to the customer.

## 6. Customer bounds (question 3)

This version is fixed-only. The customer changes no input.

- The public page and the edge send only `values` (`RenderBody`, `AddListingItem`).
  `listing_values` checks each one against `listing.params`, so a value reaches only
  `inputs.params`. `listing_reference` writes the snapshot's inputs around them. No code
  path copies a customer value into `inputs`, so the rule holds by construction, not by
  a check.
- The shop may offer any `model.scad` parameter, as on any listing. The smart defaults
  stay: colours and free text offered, the geometry locked.
- A pipeline may ignore a parameter that the listing offers (the dollhouse sets `piece`,
  `course`, `width_units`, `depth_units` and `preview` itself, fact 2). The customer then
  changes nothing by changing it. For the dollhouse this does not arise. `preview` sits
  under `/* [Hidden] */` (`models/dollhouse-kit/model.scad` lines 288-291), and
  `shownParams` leaves the `Hidden` group out of a listing entirely. The dollhouse's own
  UI hides the other four (`DRIVEN`, group `Piece`), and the smart defaults lock them,
  because they are geometry. §6.1 makes this general.

A later version could let the customer change an input within bounds the shop sets.
§13.1 says what this design keeps free for it.

### 6.1 Parameters the pipeline sets (owner, decision 5)

A template names the parameters its pipeline sets itself, in its `model.json`:

```json
"pipeline": {"module": "pipeline/pipeline.py", "api": 1, "inputs": "pipeline/inputs.json",
             "sets_params": ["piece", "course", "width_units", "depth_units", "preview"]}
```

- `PipelineDeclaration` (`library/catalogue.py`) gains
  `sets_params: list[str] = Field(default_factory=list, max_length=200)`, each name with
  the pattern of a parameter name. An old `model.json` without it still loads (fact 4).
- The dollhouse's list is `pipeline.py` `SHAPE`. A template test keeps the two equal.
- `RevisionSchema` (`api/versions.py`, the answer of
  `GET /models/{slug}/versions/{version}/schema`, which the editor reads through
  `api.getSchema(slug, commit)`) gains `pipeline_params: list[str] = []`. It is read from
  `model.json` at that revision, as `_revision_ui` reads `ui` there.
- The editor hides them: `listingDraft.ts` `shownParams(schema)` leaves out each name in
  `schema.pipeline_params`, as it leaves out the `Hidden` group. A hidden parameter is
  never offered and never locked by the shop, so it renders at the schema's default,
  which the pipeline replaces. The agent's `storefrontDraft.ts` `defaultsFor` leaves them
  out too, and the shared vectors (§9) carry a case for it.
- Publish refuses a listing that offers one of them, or locks one at a value that is not
  the schema's default: 422 `listing_param_pipeline_set`, `name` `params.<name>`. Publish
  reads the list at the commit in `_commit_facts`, with the declared input schema. This
  is row 8 of §4.3's table, after the inputs checks.

## 7. Rendering (question 4)

Every step runs in the CRUD service (today's API process), or on a worker through
Temporal. The edge gets no new route and no new data. No new SQL client, grant or table
access is added (#5295). Template Python runs only on the render worker, as today.

### 7.1 The reference

`inputs.py` `listing_reference` builds:

```python
inputs = {
    **snapshot.inputs,
    "params": with_defaults(snapshot.schema_, values),
    "v": snapshot.inputs_version,
}
```

and its render key with `render_key_of(..., pipeline=snapshot.pipeline)`, as now. For a
template with no pipeline, `snapshot.inputs` is empty and `inputs_version` is 0, so the
result is byte for byte what it is today. A test pins that.

From this one change:

- **The customer's preview.** `start_render` submits `reference.inputs` with
  `whole_inputs=True`. `TemplatePipeline` runs the pipeline at the listing's commit.
  Visitors who choose the same values share one render, keyed on the whole inputs.
- **Prepared colours.** `prepare.py` builds each combination through `listing_reference`,
  so a prepared render has the visitor's render key.
- **The cart and the order.** The draft item's revision holds the reference's whole
  inputs. The order's render and its quote go through `output_for_reference`, which sees
  the pipeline by its render key (fact 7) and submits whole inputs. Those outputs keep
  `source` `back_office` (#6371): they are the shop's.

### 7.2 What changes in the price checks

`renders.py` `estimate_of` gives no price (`none`, "We price this design by hand") also
in the case below. `render_estimate` (the visitor's read) and `prepare.py` `warm` (#6922,
at publish and after Prepare colours) both ask it, so the rule goes there and not into
`render_estimate` alone:

- the job's inputs other than `params` differ from the listing's `snapshot.inputs`, or
  its `v` from `inputs_version`. That is the shop republishing other inputs at the same
  commit. It extends today's slug-and-commit check (fact 9). The comparison is deep
  equality of the parsed JSON values (Python `==` on the dicts), never of their text:
  a value read from `jsonb` comes back with its keys reordered (backend `CLAUDE.md`,
  #6302), and a text comparison would take a correct firm price away.

`line_prices.py` `price_of` applies the same rule. It is the one caller of `firm_price`,
for the add and for `reprice` at submit, so a cart line never keeps a firm price for a
design the listing no longer sells.

**The instant read (#6921), a requirement of PR 3.** `renders.py` `instant_variant`, the
cart add without a render (`_priced_variant`) and the preview file (`variant_preview`)
find a `done` row by `variant_key`. The key holds the listing's `updated_at`, which each
save and publish changes, so a republish with other inputs is another key and finds no
old row (PR 2 has the test, with a row planted on the old key). But `output_of` compares
the output's slug and commit only, and `variant_preview` reads the listing as it is now:
an old key's URL on a listing republished with other inputs at the same commit would
still serve the old house. In PR 2 no row has a pipeline's `variant_key`, since only
`estimate_of` leads to one, so none of the three is reached. PR 3 makes `output_of`
compare the output's inputs other than `params`, and its `v`, with the snapshot's (deep
equality, as above), and tests that a republish with other inputs serves
no old price and no old preview. The `variant_key` of a template with no pipeline must not
change (PR 2 pins one by its hex).

A job with more than one output is priced over all its outputs (§7.4), not refused and
not priced by hand (owner, decision 4).

**The slots of a pipeline render.** `renders.py` `variant_of` takes the slots from
`job.result.colors` and the price key's digest from `job.result.model_3mf`, and the
slice slices the output that `output_for_reference` saves from that job. For a pipeline
job with one output, `result` is that output's: `model_3mf` is the whole kit's 3MF, every
plate, and `colors` lists that 3MF's colours in its slot order (fact 8). So the key, the
slot filaments and the slice all describe the same file. PR 3 tests this (§10.1),
because a per-piece list would give a wrong price with no error. A job with several
outputs reads the kit's slots and digest instead (§7.4).

**No promise of a price.** A visitor's line may still be priced by hand on any visit, as
for every listing: after #6581 a variant has no price while the profile printer's status
cannot be read (`printer_opening` gives no status, `STATUS_UNREAD`; no such printer gives
None), a slot's type with no
preset for the nozzle gives none (`NO_TYPE_PRESET`), and a slice that fails gives none
either. A whole dollhouse slices for longer than a keychain, so a status blip during the
wait is more likely to cost that visitor the firm price. The design adds no retry of its
own for this.

**The preview shows the first plate.** `preview_glb` is the first plate only (fact 8),
so a kit's preview shows part of the kit. The storefront shows it as it is; a kit-wide
preview is not in this design.

### 7.3 The slice price

The slice run itself needs no pipeline-specific change. `price_variant` renders the
reference through `output_for_reference`, which already takes whole inputs and marks the
output `source=storefront` (#6371). It slices every plate of the multi-plate 3MF through
`slice_and_price`.

The slice request for a pipeline variant is built as any variant's is after scadify's
slice-price fix, #6581 (issue #6574, merged as bd5889e76), never by a third copy. The
names below are read from `main` after that merge:

- `bambuddy/choices.py` `opening_choices(remembered: ModelPrintChoices | None, status:
  PrinterStatus | None, bed_type: str) -> PrintChoices`, the one builder of the
  dialog's opening choices, which replaced `slice_price.py` `storefront_choices`;
- its helpers `dialog_bed_type(...)` and
  `async printer_opening(settings, printers, printer) -> PrinterOpening | None`, where
  `PrinterOpening` holds `status`, `last`, `printer` and `printer_id`, and the plate is
  `opening.bed_type(settings)`, read at each price;
- in the storefront, `slice_price.py` `plan_variant(..., printer: PrinterOpening,
  type_presets: Mapping[UUID, Sequence[EffectivePreset]])`, with the presets read by
  `read_type_presets` (`filament_spool_type_presets`, #6262);
- `slice_profile.py` `slice_request(profile, choices)`, which sends no spools and takes
  no slot materials. The shop slice profile maps each material to a filament type id
  (`slot_types`), and each slot gets that type's presets (`slot_presets`). The slots go
  as `SliceVariant.slot_presets`, then to `resolve(..., slot_presets=)`. The 3MF gives
  the colour.

`plan_variant` gives no price when the printer's status cannot be read
(`STATUS_UNREAD`) or when a slot's type has no preset for the H2C and the chosen nozzle
(`NO_TYPE_PRESET`). The price key is at `KEY_VERSION` 4 (#6710 moved it from 3: a
file listing's key has a subject and its setup).

A pipeline variant goes through `plan_variant` and `slice_request` unchanged, with the
`PrinterOpening` of the profile's printer. The remembered choices `opening_choices`
takes are read by the listing's slug, as for any listing. #6581 also changed `price_key`; this design
adds no field to the key, because the 3MF's sha256 already differs when the inputs
differ. For several outputs, the key's `model` is the kit digest of §7.4. Publish's `check_sliceable` (`listings.py`, 422
`listing_material_unsliceable`) applies to a pipeline listing as it is: after #6581 it
refuses a material with no filament type in the shop slice profile (its text says
"no filament type for"). #6581 is merged, so
the work that touches `slice_price.py` or `line_prices.py` can start once this design is
approved (§11).

### 7.4 A render with several outputs (owner, decision 4)

A pipeline may call `ctx.output` more than once (fact 8). Publish cannot know that
before a render. The owner chose one price over every output, not a refusal and not a
price by hand. The rule: **the kit is every output of the one job, sliced in one
operation and priced in one `price_plates` call.** A job with one output (and a job with
no `outputs`, a template with no pipeline) gives byte for byte today's key, slots and
price. All of this runs in the CRUD service and on the `library` queue, as today
(#5295).

**The kit's slots.** A new `slice_price.py` `kit_colours(job) -> list[str]` gives
`job.result.colors` when `job.outputs` has at most one entry. Otherwise it gives the
colours of `job.outputs[0].result.colors`, then each colour of the later outputs that is
not in the list yet, compared casefolded, in output order. A slot of the kit is an index
in that list. One colour is one slot across the kit, so one colour gets one filament, as
`renders.py` `_filament_for` already maps by colour, not by slot. The first output's
slot numbers do not change. `renders.py` `variant_of`, `store_render` (its `colors`) and
the `RenderColours` reader all take `kit_colours(job)` in place of `job.result.colors`,
so the visitor's slot choices, the slot filaments and the key use one slot list.

For each output `k`, `slot_map(output_colours, kit) -> dict[int, int]` maps the
output's own slot (from 0) to its kit slot. The slice of output `k` gets, as its
`slot_presets`, the kit slot's presets under the output's own slot id
(`{local + 1: variant.slot_presets[kit + 1]}`), because `resolver.resolve` reads slot ids
of the file it slices. `SliceVariant` keeps `slot_filaments` and `slot_presets` by kit
slot. It gains no field: the run reads each output's colours from the outputs it saved.

**The kit's digest, and the key.** `price_key` does not change and `KEY_VERSION` stays 4.
Its `model` is a new `kit_digest(job, root) -> str`:

- at most one output: `model_digest(job.id, root / job.result.model_3mf)`, as today;
- several: the sha256 of the JSON text `["kit", d0, d1, …]` (sorted keys, no spaces),
  where `dk` is the sha256 of output `k`'s `model_3mf`.

`model_digest` keeps each digest by a cache key, not by job id only (fact 12): `job.id`
for the first output, as today, and `f"{job.id}:{k}"` for output `k` > 0. Without that,
output 1 would read output 0's kept digest. A visitor who changes a colour gets another
key through `filaments`, as today. A change to any output's 3MF gives another key.

**The files.** `materialize_result` runs once per output's `JobResult` before any digest
or slice: `renders.py` `_fetch` and `storefront_slice_run`'s digest read fetch every
output's files. A job whose later output's files cannot be fetched is `files_lost`, as
for the first.

**The render and the saved outputs.** A new `orders/rendering.py`
`outputs_for_reference(reference, ..., saved_as: Callable[[int], str], source=...)
-> list[RenderedOutput]` renders the reference once (the same submit and wait as
`output_for_reference`). It then saves each entry of `job.outputs` with `"index": k`
under `saved_as(k)`, in index order, and reuses an output already saved there while it
`reference_matches`. `output_for_reference` stays as it is for the order's render and
the quote. `storefront_output_id(render_key, index=0)` keeps today's id for index 0. For
a later output it appends `:{index}` to the uuid5 name string, not to the id: the id is
`uuid5(NAMESPACE_URL, f"scadbuddy:storefront_output:{render_key}:{index}").hex`
(`library/outputs.py` `storefront_output_id`). `library/output_index.py`
`saved_by_storefront_run` recognises only the index-0 id. That matters only for an
output saved before the `source=storefront` mark (#6371); a later output always carries
the mark. The slice run passes
`lambda k: storefront_output_id(reference.render_key, k)`. Each saved output keeps
`source=storefront` (#6371).

**One slice operation.** `price_variant` calls `outputs_for_reference`. Its
`rendered_sha256` check takes the list: every output must come from one job (`job_id`),
and `kit_digest` of that job must equal `variant.model_sha256`, else
`GEOMETRY_DIFFERS`. Then it calls `slice_and_price` once:

- `start` starts every output's plates, output after output
  (`run.slicer.start(output_k, variant.slice, subject, slot_presets=presets_k)`), and
  gives the plates in one list;
- `slice_and_price` gains one optional keyword,
  `place: Callable[[int, Plate], Plate] | None = None`. It is called with each plate's
  position in that list after `read_plate`, before `priced_for`. The storefront passes
  one that renumbers the plate (output 0's plates keep 1 … n0, output 1's are n0 + 1 …)
  and moves each `PlateFilament.slot` to its kit slot. A back-office quote passes none,
  so its plates do not change;
- `priced_for` runs `sliced_slots(plates, range(len(filaments)))` over the kit slots, as
  today;
- `price_plates` prices all plates of all outputs once: grams per kit slot, one setup
  line per plate, one labour line, one markup and one rounding (fact 12). A price
  summed from one call per output would count labour and rounding once per output.

`SlicePrices.finish` writes the row as today. Its `plates` are the renumbered plates, and
`grams` is keyed by kit slot, so `line_prices.py` `_plates` hands job cost the whole
kit.

**A slice that fails.** Any output's failure is a failure of the one `slice_and_price`
call: a plate that fails (`_follow`), a file that cannot be read (`read_plate`), a kit
slot at 0 g (`sliced_slots`), or a render that fails or is cancelled
(`outputs_for_reference`, a 409). `price_variant` then writes `failed` for the variant's
key, with the reason (`_failed`), and the variant has no price. No partial price is ever
written: the row is `done` only after every output's plates are priced. A passing
failure (`_passing`) is raised as `SliceUnavailableError`, and the operation tries again,
as today.

**The cost: one render, one slice (owner, decision 3, option (a)).** A visitor's render
of a pipeline listing is one render job, whatever its number of outputs or pieces, for
`storefront_renders_per_minute`, `…_per_day` and `storefront_render_concurrency`. A
variant's slice is one place of `storefront_slices_concurrent`: one price key, one
`SlicePrices.claim`, one `storefront_slice_price` operation, whatever its number of
outputs or plates. "One" is the variant's key. That is the rule for a multi-plate output
today, so a kit adds no new counting. The whole kit's slice runs under the one
operation's `QUOTE_TIMEOUT` (`LOST_AFTER` is built on it). §10.4 measures a whole
dollhouse slice against it.

**The order.** The order's render still saves and attaches output 0 to the item
(`output_for_reference`), because `OrderItem` holds one `output_id`. The customer's firm
price covers the whole kit, and the price row's plates go to the item's job cost. The
other outputs of an ordered kit are found by the job: each saved output records its
`job_id`, and the job lists every output. An order page that renders and prints every
output of an item is part of this build (PR 5, §11). **No customer pays for an output the
order cannot make:** until PR 5 lands, a render with more than one output still answers
`none`, so such a kit is priced by hand. PR 4 builds the kit price, and PR 5 turns it on
together with the order's every-output render and print. The dollhouse makes one output
(fact 2), so no listed template meets this today.

## 8. Template changes (question 5)

A listing pins a commit (fact 11). Here that means:

- **A published listing never moves.** Its snapshot holds the schema, the inputs at the
  commit's `INPUTS_VERSION`, and the key of the source snapshot, which includes
  `pipeline/`. The worker loads the pipeline from that revision (pipelines spec §8.3), so
  a later edit of `pipeline.py`, `inputs.json` or `model.scad` changes nothing for the
  customer until the shop publishes again.
- **An order never moves.** Its item's reference holds the commit and the whole inputs,
  `v` included. A re-render months later runs the same pipeline on the same inputs.
- **Republishing at a newer commit.** "Change" picks the new version and keeps
  `inputs` and `inputs_v`. Publish then migrates them with the new commit's `migrate`
  (from `inputs_v` to the new `INPUTS_VERSION`) and checks them against the new commit's
  schema. A key the new schema dropped is refused (`listing_inputs_invalid`, reason
  `additional`, under the Design block) unless the template's `migrate` drops it. A
  pipeline removed at the new commit, with inputs set, is `listing_inputs_unexpected`;
  the shop clears them with "Set up the design". A pipeline added at the new commit with
  no inputs set yet is checked like any other: an empty `inputs` fails a schema with a
  `required` key.
- **A deleted template.** Publish refuses it (`listing_reference`, as today). A listing
  already published keeps rendering from its held snapshot.
- **The row's `inputs_v` is never rewritten by publish.** So the next publish migrates
  from what the shop saved, and the result does not depend on how many times it ran.

## 9. The assistant and the shared vectors

- `storefront_listing_draft` (`agent/src/tools/storefront.ts`): with `output_id` of a
  pipeline template, it takes the output's `inputs` less `params` and `v` as `inputs`,
  and `inputs.v` as `inputs_v`, and runs the smart defaults over `inputs.params`. With
  `{slug, values}` it sets no inputs; the answer then says that the design needs setting
  up in the editor, with the editor link. `storefront_listing_publish` is unchanged: its
  refusals carry the new codes.
- `storefrontDraft.ts` (the agent's copy of `defaultsFor`) and `listingDraft.ts` both
  gain the split of a source's inputs into `inputs`, `inputs_v` and the values. The rule
  goes into `test-fixtures/storefront/sell-draft-vectors.json` as new cases (`source`
  with whole inputs; expected `input.inputs` and `input.inputs_v`), so the two copies
  stay equal.
- The authoring skill (`plugins/scadbuddy/skills/authoring`) gains the `pipeline.inputs`
  declaration and its subset, with its sources cited as `lint-plugin.sh` requires.

## 10. Test plan (question 6)

### 10.1 Backend

- The shared schema subset: the flows' cases still pass; one level of object passes;
  two levels, `params` or `v` at the top level, and an object without
  `additionalProperties: false` are refused.
- `catalogue.py`: `pipeline.inputs` and `pipeline.sets_params` are read; an old
  `model.json` without them still loads. `RevisionSchema` carries `pipeline_params` at
  the revision.
- Publish, §6.1: an offered parameter in `sets_params` is
  `listing_param_pipeline_set` with `name` `params.piece`; one locked at its default
  passes; one locked at another value is refused.
- Publish (`tests/orders/storefront/test_listings.py`, `tests/api/test_storefront_listings.py`):
  each new code with its `name` and `reason`; the old `listing_pipeline_unsupported` test
  becomes the `listing_inputs_undeclared` test; `inputs_v` below the version calls
  `migrate_inputs` once (a fake `RenderService`) and stores its result less `params`;
  above the version is refused; a migration error is `listing_inputs_migration_failed`
  with the template's message; the snapshot holds `inputs`, `inputs_version` and
  `pipeline: true`.
- `listing_reference`: a pipeline listing gives whole inputs with `v` and a whole-inputs
  render key; a listing with no pipeline gives exactly today's reference. The existing
  pin that it equals `resolve_reference` (`tests/api/test_storefront_public.py`) gets a
  pipeline case.
- A visitor's `values` with a key named like an input (`house`) is refused
  `input_outside_listing`, and never reaches `inputs`.
- PR 2: a pipeline render starts no slice and gets `none` through every path: the
  estimate, the warming at publish and after Prepare colours (with
  `storefront_warm_prices` on, no `storefront_slice_prices` row), the instant read, the
  add and the submit; also when a `done` row of the same 3MF and filaments exists. A firm
  line whose listing is republished as a pipeline is told at submit (409
  `prices_changed`, `by_hand`).
- PR 3: `estimate_of` and `price_of`: `none` for a job whose inputs other than `params`
  differ from the snapshot; the instant read and the preview of §7.2.
- PR 3, the slots of a pipeline render: a fixture pipeline that packs two pieces of
  different colours onto two plates. The job's `result.colors` lists every colour of
  both plates in the 3MF's slot order, `result.model_3mf` is the file the slice run
  reads, and `variant_of` gives one slot filament per colour of the whole kit.
- §7.4, several outputs (`tests/orders/storefront/test_slice_price.py`, with a fake
  `Slicer` and a job of two outputs):
  - `kit_colours`: one output gives `job.result.colors` unchanged; two outputs
    `["#FF0000", "#00FF00"]` and `["#00ff00", "#0000FF"]` give
    `["#FF0000", "#00FF00", "#0000FF"]`; `slot_map` of the second is `{0: 1, 1: 2}`.
  - `kit_digest`: one output equals `model_digest` of its 3MF (today's key, pinned);
    two outputs give the `["kit", d0, d1]` digest; the digest of output 1 is not
    output 0's kept digest (the cache key bug).
  - `price_key` of a one-output job is equal to the key before this change (a pinned
    hex value), so no stored price moves.
  - `price_variant` over two outputs: `Slicer.start` is called once per output, each with
    its own `slot_presets` by local slot id; the row is `done` with plates 1 … n0 + n1,
    `grams` by kit slot, and one `labour` and one `rounding` line in `lines`.
  - A plate of the second output that fails, and a kit slot at 0 g, each give a
    `failed` row and no price; no row is ever `done` with part of the kit.
  - Outputs saved from two jobs give `GEOMETRY_DIFFERS`.
  - Until PR 5: the estimate route still answers `none` for a job of two outputs.
- PR 5, the order of a kit item (`tests/orders/`): an item from a two-output render
  attaches both outputs by `job_id`; its job cost meets every kit slot; the order page
  offers a print of each output; the estimate route now answers the kit price.
  - `slice_and_price` with no `place` gives the plates as today (a back-office quote
    test, unchanged).
  - One claim: a two-output variant takes one place of `storefront_slices_concurrent`;
    with `limit=1`, a second variant's read answers `pending`.
- `outputs_for_reference`: saves every output under `storefront_output_id(key, k)`,
  index 0 under today's id, and reuses saved outputs that match on a second call with
  no new render.
- One `requires_temporal` API test with a small pipeline fixture template (fake
  openscad, as `tests/api` renders): publish, a visitor's render through the storefront
  render route, then the order item's render, both with the listing's inputs.

### 10.2 Template

- `models/dollhouse-kit`: `inputs.json` passes the subset; its bounds equal `LIMITS`;
  both cases of `verify-inputs.json` pass it; `pipeline.sets_params` equals `SHAPE`. `verify.sh` runs this in the `models` job.

### 10.3 Frontend and agent

- `listingDraft.test.ts` and the agent's tool test read the new vector cases.
- `sellSource` takes a job's and an output's inputs.
- `shownParams` leaves out the `pipeline_params` of a revision schema.
- `ListingEditor.test.tsx`: the Design block's summary; a refusal with `name`
  `inputs.house.storeys` shows under the block with the title "Storeys"; "Set up the
  design" mounts a fake template module, and "Use this design" writes `inputs`,
  `inputs_v` and the locked values; a module that throws shows the schema form.
- A Playwright run on the msw mocks: Sell on storefront from the dollhouse's page, the
  editor with the Design block, publish.

### 10.4 Before go-live (on prod, by hand)

- Measure a whole dollhouse render at the smallest house (1 × 1, 1 storey) and at the
  default house (2 × 1, 1 storey), from the job's `started_at` and `finished_at`. Record
  where and how in the go-live PR. The Sell note warns above 100 s (easy listing spec §4).
- Measure the slice of the same two houses (the `storefront_slice_price` operation's
  start and end), and check it ends well inside `QUOTE_TIMEOUT` (§7.4).
- List one dollhouse, render it as a visitor, wait for its price, add it, submit, and
  render the order item. Check the order's output has `source` `back_office`, and the
  slice run's output `storefront`.

## 11. PR split

Each PR is a reviewable step, and a pipeline listing stays refused or priced by hand
until the step that makes it safe.

1. **`feat(templates): a pipeline declares its inputs`** (backend, models, plugin).
   The shared schema subset out of `flows/inputs.py`; `PipelineDeclaration.inputs` and
   `sets_params`; `RevisionSchema.pipeline_params`; the dollhouse's `inputs.json`, its
   `sets_params` and its template test; the authoring skill. No storefront change.
2. **`feat(storefront): list a pipeline template with fixed inputs`** (backend). The
   migration (`inputs`, `inputs_v`); the wire fields; publish's checks and migration;
   `listing_param_pipeline_set` (§6.1); the snapshot and its `pipeline` docstring;
   `listing_reference`; `estimate_of` answers `none` for every pipeline render in this PR,
   so nothing is priced yet and no slice starts: `render_estimate`, and since #6922
   `prepare.py` `warm` at publish and after Prepare colours, both ask it. Retires
   `listing_pipeline_unsupported`, with every copy of the code: the docstrings in
   `api/storefront.py` and `orders/storefront/listings.py`, and the test mock in
   `frontend/src/pages/StorefrontListingPage.test.tsx`. The back-office msw mock
   `frontend/src/mocks/features/storefront.ts` fills the new fields, which
   `pnpm typecheck` needs, and `listingDraft.ts` `draftOf` carries a listing's `inputs`
   and `inputs_v`, so a save in the editor keeps them. It does not touch `slice_price.py`, and touches `line_prices.py`
   in one line: `price_of`, the one caller of `firm_price` (the add and `reprice`), gives
   None for a listing whose snapshot is a pipeline. The first draft left that file alone,
   because no `done` row could exist for a pipeline variant. That was false: `price_key`
   holds no slug and no inputs, so the price of the same 3MF and filaments, made before
   the template had a pipeline or under another listing, has the key. Each pipeline line
   is therefore "priced by hand" at the add and at submit. The instant read goes by
   `variant_key`, which only the estimate's path writes, and `listing_last` is read for
   file listings only, so `estimate_of`'s answer covers both. Between this PR and PR 3 a
   customer of a pipeline listing reads "We price this design by hand", so none is
   published on prod before PR 3 (a kit with several outputs: before PR 5).
3. **`feat(storefront): price a pipeline listing`** (backend). The rule of §7.2 in
   `estimate_of` and `price_of` in place of PR 2's two `pipeline` answers (the inputs
   compared by deep equality of the parsed values, not by key order), `output_of` and the
   instant read (§7.2), the slice request through #6581's `plan_variant`
   and `slice_request` (§7.3), and the slice-price test for a pipeline variant. It
   builds on #6581, which is merged (bd5889e76). A job with more than one output still
   answers `none` in this PR, so a kit is priced by hand until PR 5.
4. **`feat(storefront): price every output of a pipeline render`** (backend). §7.4:
   `kit_colours`, `slot_map`, `kit_digest` and the `model_digest` cache key;
   `outputs_for_reference` and `storefront_output_id(..., index)`; the `place` keyword of
   `slice_and_price`; `price_variant` over every output; `variant_of`, `store_render` and
   `RenderColours` on the kit's slots; every output's files fetched; §10.1's several-output
   tests, run through the price path directly. PR 3's `none` for several outputs stays in
   the estimate route. After 3.
5. **`feat(orders): render and print every output of a kit item`** (backend and frontend).
   The order's render saves and attaches every output of the item (by `job_id`), its job
   cost meets every kit slot, and the order page prints each output. This PR removes PR
   3's `none` for several outputs, so the kit price goes live only with it. After 4.
6. **`feat(storefront): set a listing's design inputs`** (frontend). The Design block,
   the "Set up the design" dialog (owner, decision 1), the schema form fallback, Sell's
   inputs, the refusal mapping, `shownParams` over `pipeline_params`, the mocks and the
   Playwright run. It can go up in parallel with 3 to 5, after 2.
7. **`feat(agent): draft a pipeline listing`** (agent, shared vectors). After 2. The
   frontend's vector cases land in 6 or 7, whichever is first; the other reads them.

Then the prod checks of §10.4, as a comment on #5701.

## 12. Decisions (owner, 2026-11-18)

The owner answered the questions of the first draft in
[a comment on PR #6577](https://github.com/eh-homelab/ScadBuddy/pull/6577#issuecomment-6971189245).
This text applies each answer.

1. **The editor: yes.** "Set up the design" (the template's UI in a dialog, §5.3) is in
   the first version, in PR 6. Sell on storefront stays the main path (§5.1).
2. **The schema file: yes.** A pipeline template must declare `pipeline.inputs` before
   it can be listed (§4.1). Without it, publish answers 422 `listing_inputs_undeclared`.
3. **Render and slice cost: option (a).** A pipeline render counts as one render job,
   and a kit's slice as one slice, as today (§7.4, "The cost"). The facts behind the
   question: one render fans out to one openscad run per entry of `pipeline.py`
   `house_pieces`, 11 or 12 for a one-storey house and 14 or 15 for two or three
   storeys (measured on 2026-11-17 by calling `house_pieces` for every house within
   `LIMITS`). A kit's slice holds one place of `storefront_slices_concurrent` for longer
   than a keychain's, and other visitors' prices wait while it runs. That is accepted
   while kits are few, because a variant is sliced once and its price is kept by key.
4. **Several outputs: price all files.** Publish does not refuse a pipeline that can
   make several outputs, and such a render is not priced by hand. One price covers
   every output of the render (§7.4). PR 4 builds it, and PR 5 turns it on with the
   order's every-output render and print, so no customer pays for an output the order
   cannot make.
5. **Parameters the pipeline sets: yes.** The template names them in `model.json`
   `pipeline.sets_params`, and the editor hides them (§6.1).
6. **A customer who changes an input: later.** A follow-up issue, but this design keeps
   room for it (§13.1).
7. **A back-office revision of a storefront pipeline item: later.** A follow-up issue,
   but this design keeps room for it (§13.2).

**A listing can name a `builtin:` slug.** Prod's dollhouse outputs are of
`builtin:dollhouse-kit` (fact 3). PR 2's API test
(`tests/api/test_storefront_pipeline_listings.py`
`test_a_listing_names_a_builtin_template`) saves, publishes and renders a listing of
`builtin:<slug>` at the version the catalogue gives for it, with its inputs. No change was
needed.

## 13. Room for the follow-ups (decisions 6 and 7)

Neither follow-up needs a migration of what this design stores. Each adds fields; no
stored value changes its meaning.

### 13.1 Customer-editable inputs

- **The stored inputs stay the whole design.** `ListingInput.inputs` and
  `ListingSnapshot.inputs` hold every value the pipeline reads (§4.2). A later
  customer value only replaces one of them at its path. So a listing published now
  stays valid, and it becomes a listing with no editable input.
- **Bounds go in a field of their own.** The follow-up adds
  `ListingInput.input_bounds: dict[str, ParamBound]` (`wire.py` `NumberBound`,
  `ChoiceBound`, `TextBound`), keyed by the input's path under `inputs`, without the
  `inputs.` prefix (`"house.cols"`). Its column defaults to `{}`, so every row of this
  design reads as "no bound". The subset of §4.1 allows at most one level of objects, so
  a path has one or two names. The schema gives each path its type and limits, so
  publish can check a bound against it as `_check_bound` checks a parameter's.
- **The paths are already the refusal names.** §4.3 names a fault by
  `inputs.<path>`. The same path keys a bound, its refusal and the editor's field.
- **The customer's values stay apart.** `RenderBody.values` and `AddListingItem` keep
  parameter names only, and `listing_values` keeps refusing any other key
  (`input_outside_listing`). The follow-up adds its own `input_values` field, checked
  against `input_bounds`, and `listing_reference` writes those values over
  `snapshot.inputs` before `params` and `v`. The reference keeps the shape of §7.1, and
  the render key stays the whole inputs (`render_key_of(..., pipeline=True)`).
- **The price needs no change.** The key hashes the kit's 3MF (§7.3, §7.4), so a
  customer's input change is a new key.
- **The public page.** `PublicListing` gains an `inputs: dict[str, PublicParam]` next to
  `params`. The `Public*` bound shapes exist already.

### 13.2 A back-office revision of a pipeline item

- **The item already holds the whole design.** Its revision's reference holds the
  commit and the whole inputs, `v` included (§7.1, §8), and its render key is the
  whole-inputs key. A new revision with other inputs is a new reference of the same
  shape.
- **A revision today keeps the other inputs.** `inputsToSend` writes only `params`
  over the item's inputs (fact 13). `DesignInputs.tsx` does not edit a template with a
  `ui` (the dollhouse has one), so no back-office edit today changes a pipeline item's
  `house`.
- **The dialog is reusable.** The host of §5.3 is built over a source of inputs, not
  over the listing draft. The follow-up mounts the same dialog over the item's
  revision: `inputs.get` reads the revision's inputs, and "Use this design" sends
  `{...inputs, params}` as a new revision through the route `DesignInputs` uses now.
  The item's `v` stays the commit's `INPUTS_VERSION`, because publish migrated it.
- **The price.** A revised item is a new reference. Its quote goes through
  `output_for_reference`, which already submits whole inputs (fact 7).

## 14. Out of scope

- A customer who changes an input (decision 6, §13.1): a follow-up issue.
- A back-office revision of a pipeline item's inputs (decision 7, §13.2): a follow-up
  issue.
- Showing the pipeline's `bom` or extra files (`assembly.svg`) to the customer.
- A storefront page change: the public site shows a pipeline listing as any other.
