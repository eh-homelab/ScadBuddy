# Storefront listings on pipeline templates (#5701)

Date: 2026-11-17. Status: draft, for the owner's approval. Nothing is built before the
owner approves it. The storefront session (scadify) reviews it too, because its #6581 changes
the slice price at the same time (§7.3).

Builds on:

- the template pipelines spec, [2026-09-27](2026-09-27-template-pipelines-design.md)
  §4.3 (inputs), §5 (pipelines) and §8.2 (inputs versions);
- the storefront spec, [2026-11-02](2026-11-02-storefront-design.md) §5.3-§5.4;
- "Sell on storefront", [2026-11-13 easy listing](2026-11-13-storefront-easy-listing-design.md);
- the customize page and its price, [2026-11-13 customize UX](2026-11-13-storefront-customize-ux-design.md) §5.4, §5.6.

## 1. Why

Today publish refuses a template with a pipeline, with 422 `listing_pipeline_unsupported`.
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
   checks a flow's `inputs.json` (scalars and arrays of scalars at the top level,
   `additionalProperties: false`, size and pattern limits) with `jsonschema`, which is a
   backend dependency (`backend/pyproject.toml`, `jsonschema>=4.23`).
   `frontend/src/components/flows/StartRun.tsx` draws a form from it. Read.
6. **The API reads `INPUTS_VERSION` without running template code.**
   `library/pipelines.py` `inputs_version_of` parses `pipeline.py` with `ast`. Template
   Python runs only on the render worker: a render in `TemplatePipeline`, and a migration
   in the `MigrateInputs` workflow that `render/submit.py` `RenderService.migrate_inputs`
   starts. Read.
7. **Most of the render path takes whole inputs already.**
   - `ListingSnapshot.pipeline` exists, and is "always false for now"
     (`orders/storefront/wire.py`).
   - `renders.py` `start_render` and `prepare.py` pass
     `whole_inputs=listing.snapshot.pipeline` to `RenderService.submit_answer`.
   - `reference.py` `render_key_of(..., pipeline=)` keys a pipeline render on its whole
     inputs.
   - `rendering.py` `output_for_reference` sees a pipeline reference by its render key
     and submits it with `whole_inputs`. The order's render, its quote and the storefront
     slice run all use it.
   - The one place that drops the pipeline's inputs is `inputs.py` `listing_reference`:
     it builds `{"params": …, "v": 0}`.

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
6. A render whose job made more than one output, or whose fixed inputs are no longer the
   listing's, gets no firm price. The shop prices it by hand.
7. The shop sets the inputs in the template's own UI, opened in a dialog from the
   listing editor. A form drawn from the input schema is the fallback.
8. A listing keeps its commit, its inputs and its snapshot. A template change reaches a
   listing only when the shop republishes it at a new commit.

## 4. ListingInput and publish (question 1)

### 4.1 The template declares its inputs

`model.json`:

```json
"pipeline": {"module": "pipeline/pipeline.py", "api": 1, "inputs": "pipeline/inputs.json"}
```

`inputs` is a path under the template directory, with the same pattern rule as `module`.
The file is a JSON Schema in a closed subset:

- The flows' subset (`flows/inputs.py`): the top level is
  `{"type": "object", "properties": {...}, "required": [...], "additionalProperties": false}`,
  and each property is a `string`, `integer`, `number`, `boolean`, or an `array` of one
  of those, with `title`, `description`, `default` and the type's own limits.
- Plus one level of `object`: a top-level property may be an object of scalar
  properties, with `additionalProperties: false` and its own `required`. The dollhouse's
  `house` needs this.
- The top level must not declare `params` or `v`. Those belong to the host.
- At most 16 KiB, as `flows/inputs.py` `MAX_SCHEMA_BYTES`. A `pattern` has the flows'
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
inputs_v: int | None = Field(default=None, ge=0)
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

Storage: one new migration adds `storefront_listings.inputs jsonb NOT NULL DEFAULT '{}'`
and `inputs_v integer` (null allowed). The snapshot is in its jsonb column already, so it
needs no column. The serialised `inputs` is at most 16 KiB.

### 4.3 What publish checks

`_commit_facts` stops refusing a pipeline. For a pipeline template it also reads, at the
commit, without running template code:

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
| 4 | `inputs` is larger than 16 KiB | stored, before the migration | 422 `listing_inputs_invalid`, `reason` `too_large` | `inputs` |
| 5 | `inputs_v` is above the commit's `INPUTS_VERSION` | stored | 422 `listing_inputs_invalid`, `reason` `newer` | `inputs` |
| 6 | The template's `migrate` refuses the inputs (only when `inputs_v` is below the version) | stored | 422 `listing_inputs_migration_failed`, `detail` the template's message | `inputs` |
| 7 | A value fails the schema | migrated | 422 `listing_inputs_invalid`, `reason` the failed keyword (`minimum`, `required`, `type`, `additional`, …) | the value's path, `inputs.house.cols` |

The migrated inputs need no second reserved-key check: publish keeps only their keys
other than `params` and `v` (below). Their size is checked again with row 7, since a
migration can add keys (`reason` `too_large`). PR 2's tests check this order.

The codes follow the `listing_*` style, and each carries `name`, so the editor shows it
under the field it names (§5.4). `listing_pipeline_unsupported` goes: no route answers
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

Save (`POST` and `PUT /storefront/listings`) checks only the shape: the reserved keys and
the size. It does not read the commit, as it does not today.

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
- `listingRefusal` gains kind `inputs` for the five new codes, with `name` and `reason`.
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
  because they are geometry. A general fix (a template naming the parameters its pipeline
  sets) is left to the owner (§12, question 5).

A later version could let the customer change an input within bounds the shop sets
(`input_bounds`, keyed by path, with the same `number` and `choice` bound kinds). The
schema in §4.1 is enough to check those bounds. It is not in this design.

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

`renders.py` `render_estimate` gives no price (`none`, "We price this design by hand")
also when:

- the job's inputs other than `params` differ from the listing's `snapshot.inputs`, or
  its `v` from `inputs_version`. That is the shop republishing other inputs at the same
  commit. It extends today's slug-and-commit check (fact 9). The comparison is deep
  equality of the parsed JSON values (Python `==` on the dicts), never of their text:
  a value read from `jsonb` comes back with its keys reordered (backend `CLAUDE.md`,
  #6302), and a text comparison would take a correct firm price away.
- the job has more than one output (`Job.outputs`, fact 8). The 3MF of the first output
  is not the whole product, so a firm price from it would be wrong.

`line_prices.py` `reprice` applies the same two rules at submit, so a cart line never
keeps a firm price for a design the listing no longer sells.

**The slots of a pipeline render.** `renders.py` `variant_of` takes the slots from
`job.result.colors` and the price key's digest from `job.result.model_3mf`, and the
slice slices the output that `output_for_reference` saves from that job. For a pipeline
job with one output, `result` is that output's: `model_3mf` is the whole kit's 3MF, every
plate, and `colors` lists that 3MF's colours in its slot order (fact 8). So the key, the
slot filaments and the slice all describe the same file. PR 3 tests this (§10.1),
because a per-piece list would give a wrong price with no error.

**No promise of a price.** A visitor's line may still be priced by hand on any visit, as
for every listing: after #6581 a variant has no price while the profile printer's status
cannot be read (`printer_opening` answers None, `STATUS_UNREAD`), a slot's type with no
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
(`NO_TYPE_PRESET`). The price key is at `KEY_VERSION` 3.

A pipeline variant goes through `plan_variant` and `slice_request` unchanged, with the
`PrinterOpening` of the profile's printer. The remembered choices `opening_choices`
takes are read by the listing's slug, as for any listing. #6581 also changed `price_key`; this design
adds nothing to the key, because the 3MF's sha256 already differs when the inputs
differ. Publish's `check_sliceable` (`listings.py`, 422
`listing_material_unsliceable`) applies to a pipeline listing as it is: after #6581 it
refuses a material with no filament type in the shop slice profile (its text says
"no filament type for"). #6581 is merged, so
the work that touches `slice_price.py` or `line_prices.py` can start once this design is
approved (§11).

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
- `catalogue.py`: `pipeline.inputs` is read; an old `model.json` without it still loads.
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
- `render_estimate` and `reprice`: `none` for a job whose inputs other than `params`
  differ from the snapshot, and for a job with two outputs.
- PR 3, the slots of a pipeline render: a fixture pipeline that packs two pieces of
  different colours onto two plates. The job's `result.colors` lists every colour of
  both plates in the 3MF's slot order, `result.model_3mf` is the file the slice run
  reads, and `variant_of` gives one slot filament per colour of the whole kit.
- One `requires_temporal` API test with a small pipeline fixture template (fake
  openscad, as `tests/api` renders): publish, a visitor's render through the storefront
  render route, then the order item's render, both with the listing's inputs.

### 10.2 Template

- `models/dollhouse-kit`: `inputs.json` passes the subset; its bounds equal `LIMITS`;
  both cases of `verify-inputs.json` pass it. `verify.sh` runs this in the `models` job.

### 10.3 Frontend and agent

- `listingDraft.test.ts` and the agent's tool test read the new vector cases.
- `sellSource` takes a job's and an output's inputs.
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
- List one dollhouse, render it as a visitor, wait for its price, add it, submit, and
  render the order item. Check the order's output has `source` `back_office`, and the
  slice run's output `storefront`.

## 11. PR split

Each PR is a reviewable step, and a pipeline listing stays refused or priced by hand
until the step that makes it safe.

1. **`feat(templates): a pipeline declares its inputs`** (backend, models, plugin).
   The shared schema subset out of `flows/inputs.py`; `PipelineDeclaration.inputs`; the
   dollhouse's `inputs.json` and its template test; the authoring skill. No storefront
   change.
2. **`feat(storefront): list a pipeline template with fixed inputs`** (backend). The
   migration (`inputs`, `inputs_v`); the wire fields; publish's checks and migration;
   the snapshot and its `pipeline` docstring (`wire.py` says "Always false for now";
   it becomes "true for a pipeline template"); `listing_reference`; `render_estimate`
   answers `none` for every pipeline render in this PR, so nothing is priced yet.
   Retires `listing_pipeline_unsupported`, with every copy of the code: the docstrings
   in `api/storefront.py` (line 181) and `orders/storefront/listings.py` (lines 11, 893
   and 1016), and the mock in `frontend/src/pages/StorefrontListingPage.test.tsx`
   (line 187). It touches neither `slice_price.py` nor
   `line_prices.py`. That is safe for the cart: a line is firm only when the add finds a
   `done` price row for its key (`slice_price.py` `firm_price`, which starts no slice),
   only the estimate read starts a slice, and in this PR it starts none for a pipeline
   render. No pipeline listing could be published before this PR, so no such row exists.
   Each pipeline line is therefore "priced by hand", and `reprice` finds no row at
   submit either. `prepare.py` and the `PrepareListing` workflow start no slice: they
   only render (checked by reading both on `main`, and by scadify on #6581's tree).
3. **`feat(storefront): price a pipeline listing`** (backend). The two rules of §7.2 in
   `render_estimate` and `reprice` (the inputs compared by deep equality of the parsed
   values, not by key order), the slice request through #6581's `plan_variant`
   and `slice_request` (§7.3), and the slice-price test for a pipeline variant. It
   builds on #6581, which is merged (bd5889e76).
4. **`feat(storefront): set a listing's design inputs`** (frontend). The Design block,
   the "Set up the design" dialog, the schema form fallback, Sell's inputs, the refusal
   mapping, the mocks and the Playwright run. It can go up in parallel with 3, after 2.
5. **`feat(agent): draft a pipeline listing`** (agent, shared vectors). After 2. The
   frontend's vector cases land in 4 or 5, whichever is first; the other reads them.

Then the prod checks of §10.4, as a comment on #5701.

## 12. Questions for the owner

1. **The editor.** Is "Set up the design" (the template's UI in a dialog, §5.3) wanted
   in the first version? Without it, Sell on storefront is the only way to set the
   inputs, and PR 4 is about half its size.
2. **The schema file.** Must a pipeline template declare `pipeline.inputs` before it can
   be listed (this design)? The other choice is to list any pipeline and check nothing
   but the size, which trusts the shop's inputs to the template's own clamping.
3. **Render and slice cost.** A visitor's render of a pipeline listing is one job, but it fans out
   to one openscad run per entry of `pipeline.py` `house_pieces` (each entry has its own
   `piece` and `course`, so its own piece key): 11 or 12 for a one-storey house, 14 or 15
   for two or three storeys. `house_pieces` leaves out an entry whose count is 0, such as
   the window wall of a house with no windows. Measured on 2026-11-17 by calling
   `house_pieces` from `models/dollhouse-kit/pipeline/pipeline.py` for every house within
   `LIMITS` (cols 1-4, rows 1-3, windows 0-14, per storey count). The
   render limits count jobs (`storefront_renders_per_minute`, `…_per_day`,
   `storefront_render_concurrency`). Should a pipeline render count as more than one,
   for example by its pieces?
   The slice is the larger cost. A kit's price is one slice of a multi-plate 3MF, and it
   holds one of the `storefront_slices_concurrent` places (`SlicePrices.claim(...,
   limit=)`) for much longer than a keychain's slice. While it runs, other visitors'
   prices wait ("Working out the price…"). This design counts a pipeline variant as one
   slice, as any variant. That is acceptable while kits are few, since a variant is
   sliced once and its price is kept by key. Should a kit slice instead take more than
   one place, or run on a place of its own?
4. **Several outputs.** This design prices a render with more than one output by hand
   (§7.2). Should publish instead refuse a listing whose pipeline can make several
   outputs? Publish cannot know that without a render.
5. **Parameters the pipeline sets.** Should a template name the parameters its pipeline
   sets itself (for the dollhouse, `SHAPE`), so the editor never offers them? This design
   relies on the smart defaults to lock them.
6. **A customer who changes an input.** Is a later version with shop-set bounds on
   inputs (§6, `input_bounds`) wanted? It is out of this design.
7. **A back-office revision of a storefront pipeline item.** The order's "Design inputs"
   (`DesignInputs.tsx`) edits `params`. This design does not check what a revision there
   does to the item's other inputs. Should that be a follow-up issue?

Not checked here: whether a listing can name a `builtin:` slug (prod's dollhouse
outputs are of `builtin:dollhouse-kit`, fact 3). PR 2's first API test settles it.

## 13. Out of scope

- A customer who changes an input (§6).
- Showing the pipeline's `bom` or extra files (`assembly.svg`) to the customer.
- A storefront page change: the public site shows a pipeline listing as any other.
