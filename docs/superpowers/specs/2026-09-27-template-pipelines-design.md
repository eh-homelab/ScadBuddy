# Templates that own their UI and their pipeline, on Temporal

Design written 2026-09-27. It answers one request — *templates need a mechanism to
control the UX: complicated UIs, conditional rendering, different `.scad`s* — and
the questions that fell out of it: where template code runs, how a render is executed
durably across machines, where files live, and how any of it is versioned.

Base design: `2026-09-22-scadbuddy-design.md` (§5 the customizer, §6 the render
pipeline). Print flow: `2026-09-24-print-flow-design.md`. AI agent:
`2026-09-27-ai-integration-design.md`. This spec changes §4's "one container, no
database" (already superseded by the Postgres render queue, #241) and §6's "one
`openscad` run per Generate".

## 1. Why

Today a template is one `model.scad` and a `model.json` carrying `name`,
`description`, `tags`, `source`. The customizer is OpenSCAD's `.param` export plus the
`// color` / `// font` / `// file` overlays (`render/schema.py`). Nothing can show or
hide a parameter based on another, and nothing can choose what to render besides
`-D` values on that one file.

The templates already strain against this:

- `maze-puzzle`: `parts` and `lid_color` are meaningless unless `mode = ball_lid`, yet
  the form always shows them. Captions across the catalogue carry the conditions as
  prose ("Used only when faces is Custom words", `dice`; "only used when there is
  text", `building-brick`).
- `dollhouse-kit`: one render makes one piece, chosen by a 13-entry dropdown. The
  thing a person wants — *a house* — is dozens of pieces, several colours, many
  plates, and a bill of materials. No amount of parameter-form polish gets there.
- #289 (a template's parts on more than one plate) and #314 (objects from several
  outputs on one plate) each add a composition mechanism to a renderer that has none.

The decision, taken in the brainstorm this spec records: **templates get full
control.** A template may ship its own UI (JavaScript, unsandboxed, in the page) and
its own pipeline (Python, run as a Temporal workflow). ScadBuddy provides the
primitives — render a file, split colours, pack plates, write outputs — and the
durable execution underneath. This is a self-hosted tool whose operator uploads their
own templates; the trust model is stated in §9 rather than pretended away.

## 2. Goals and non-goals

Goals

- A template with no extra files behaves exactly as today. All 31 bundled templates
  keep working unchanged.
- A template can replace the parameter panel, or the whole Customize page, with its
  own UI, using the host's widgets (sliders, colour pickers with extruder numbers,
  font and file pickers, the 3D preview) or drawing its own.
- A template can define what one Generate does: any number of `openscad` runs over
  any of its files, results feeding later steps, arbitrary Python between them,
  arbitrary plates and extra output files.
- Rendering runs as Temporal workflows. Steps are activities; a dead worker costs the
  step it was on, not the job. Once the store lands (phase 3) steps may run on
  different machines.
- Everything a step reads or writes lives in a content-addressed store any worker
  can reach. The one backend implemented is Bambuddy's library; the store is a
  configurable interface.
- Objects and plates are separate: a layout step over rendered objects, callable
  from a pipeline, on its own, or from a later print campaign, so that plates can be
  re-arranged by filament without re-rendering.
- Versioning: contract majors for UI and pipeline APIs; template-versioned inputs
  with a migration hook; in-flight workflows finish on the build that started them; a
  reproducibility record on every output.
- Presets, reopened outputs, the AI agent and `verify.sh` keep working through one
  concept — *inputs* — whether the template has a custom UI or not.

Non-goals

- Sandboxing template code. Decided against (§9). Containment is by *what a worker
  can reach*, not by restricting the code.
- A TypeScript pipeline runtime. The pipeline contract is language-neutral; only
  Python is implemented.
- Storage backends other than Bambuddy. The interface is defined; one implementation.
- Editing a template's `ui/` and `pipeline/` files in the in-app source editor. Until
  a follow-up, multi-file templates are edited by upload or by the agent (#252).
- Running without Postgres or Temporal. Both become required (§3.1).
- The print campaign workflow (plate-by-plate printing across days). Designed for in
  the shape of things (§7, §8) and left to its own spec.

## 3. Execution on Temporal

### 3.1 Topology

- A Temporal server deployed from `eh-homelab/clusters` with the **Temporal
  operator**, its persistence and visibility databases on a **CloudNativePG (CNPG)**
  cluster — the same way that repo runs Postgres for ScadBuddy today. Namespace
  `scadbuddy`. The manifests are that repo's change, made with its maintainers; this
  spec only names what ScadBuddy needs from them (address, namespace, the two task
  queues).
- The backend image gains a **worker** entrypoint (`python -m scadbuddy.worker`), run
  as its own Deployment so renders scale apart from the API. Same image, so the
  measured OpenSCAD facts (base spec §3) hold on every worker.
- Two task queues:
  - `render` — the pipeline workflows and every rendering activity. Workers on it get
    the store credential (§6) and nothing else.
  - `bambuddy` — activities that print, queue or touch projects. Only these workers
    hold a key with *Manage Queue* / *Manage Projects*.
- New required settings: `SCADBUDDY_TEMPORAL_ADDRESS`, `SCADBUDDY_TEMPORAL_NAMESPACE`
  (default `scadbuddy`), `SCADBUDDY_TEMPORAL_TASK_QUEUE_RENDER` /
  `_BAMBUDDY` (defaults `render`, `bambuddy`). `SCADBUDDY_DATABASE_URL` becomes
  required; the in-memory `render/job_store.py` is deleted.

### 3.2 The job table is a projection

`render_jobs` stays, and stays the thing the API reads. It stops being a queue.

The table today is `pg_store.py`'s `MIGRATIONS`: `id`, `slug`, `params`,
`model_version`, `state` (`pending` | `running` | `done` | `failed`), `created_at`,
`started_at`, `finished_at`, `log_tail`, `error`, `result`, `render_key`, `claims`,
`attempts`, `heartbeat_at`, `diagnostics`, `diagnostics_dropped`. The `Job` model
(`render/job_models.py`) mirrors it. This spec keeps the name `state` and its
values, and adds one: `cancelled`.

Kept as they are: `id`, `slug`, `model_version` (the revision the job pinned),
`state`, `created_at`, `started_at`, `finished_at`, `log_tail`, `error`, `result`,
`render_key`, `claims`, `attempts`, `diagnostics`, `diagnostics_dropped`, and the
partial unique index on `render_key` over `pending` rows that coalesces identical
requests.

Removed: `heartbeat_at` and its `render_jobs_running` index, the lease, `reap`, the
`FOR UPDATE SKIP LOCKED` claim, the `NOTIFY`/`QueueListener` wake-up, the worker poll
loop, and the startup step that fails unfinished jobs (Temporal resumes them). The
Prometheus metric names in `core/metrics.py` are unchanged;
`scadbuddy_render_jobs_running` is derived from the projection.

Added: `workflow_id` (always `render-<job_id>`), `kind` (`render` | `arrange`),
`inputs` (jsonb; §4.3), `pipeline_version`, `steps` (jsonb, `[{name, state, done,
total}]`, what `ctx.progress` writes and `GET /jobs/{id}` shows). `params` stays
through phase 1 as the default pipeline's `inputs.params`, and is dropped by phase
2's migration once every reader uses `inputs`.

### 3.3 Submit: insert, start, reconcile

The API's `POST /models/{slug}/render`:

1. One Postgres transaction inserts the row `queued`, or coalesces onto the pending
   row with the same `render_key` (`claims + 1`), exactly as today. The response
   (job id, `coalesced`) comes from that commit.
2. `start_workflow(TemplatePipeline, id=f"render-{job_id}",
   id_conflict_policy=USE_EXISTING, task_queue="render")`. Starting twice is a no-op;
   a coalesced request finds the running workflow.
3. A **reconciler** (in the API process, every 5 s and at boot) re-issues step 2 for
   every `queued` row older than 5 s that has no `started_at`. Idempotent thanks to
   the fixed ID. This is the transactional-outbox pattern with Temporal's workflow ID
   as the dedup key, and it is what makes a crash between 1 and 2 harmless.

Why not start-first (start the workflow, then write the row from an activity)?
Coalescing and claim counting would then live in two systems for the one step that
must be atomic, and a `GET /jobs/{id}` between the start and the first activity would
404. The row is the source of truth for *whether* a job exists; Temporal is the source
of truth for *how far it got*.

Superseding and cancelling. A request that names `supersedes` releases one claim on
that job; releasing its last claim marks the row `cancelled` and cancels workflow
`render-<that_id>`, whose cancellation handler writes the final projection. Because
`start_workflow` follows the insert immediately, the superseded job is usually already
running: cancellation stops its `TemplatePipeline` at the next activity boundary, and
its `RenderPiece` children are left to finish (§3.4, `ABANDON`), so the request that
superseded it — typically the same template with one changed input — finds the
pieces it shares already rendered. The base spec's "dropped unrendered if no worker
has taken it" becomes "cancelled at the next step".

### 3.4 Workflows

All in `backend/scadbuddy/workflows/`, `temporalio` Python SDK.

```
TemplatePipeline   id render-<job_id>          one per Generate (render_jobs.kind = 'render')
  └─ RenderPiece   id piece-<piece_key>         one per DISTINCT openscad render; USE_EXISTING
Arrange            id render-<job_id>          objects → plates (§7); render_jobs.kind = 'arrange'
```

Two keys with two jobs: `render_jobs.render_key` is the **job** key (slug, revision,
canonical inputs) that coalesces identical requests, as today. `piece_key =
sha256(slug, revision, file, canonical params)` names one `openscad` invocation and is
what dedups `RenderPiece` children across jobs. They are never interchangeable, and
both carry the slug for the reason `resolve_source` does (§3.4 step 1).

`Arrange` is submitted through the same insert → start → reconcile path as a render
(§3.3), so `GET /jobs/{id}` works for it unchanged; the row's `kind` says which
workflow `render-<job_id>` runs. A pipeline's `ctx.pack` calls Arrange's packing
activity directly, not the workflow.

`TemplatePipeline.run(job_id)`:

1. `load_pipeline(slug, revision)` — activity; returns the source of the template's
   `pipeline/pipeline.py` (or the built-in default, §5.3) at the job's pinned
   revision, plus its declared `api` major. **The source text becomes part of the
   workflow history**, so a replay always runs the code the job started with.
2. Execute that source inside Temporal's Python workflow sandbox and call
   `run(ctx, inputs)`. The `ctx` primitives (§5.2) are the only way out of the
   sandbox: each is an activity or a child workflow.
3. Every state change is a `project(job_id, …)` activity that updates the row in
   place, guarded by state order (`pending < running < done | failed | cancelled`)
   so a retried activity cannot move a job backwards.

`RenderPiece.run(req)`:

1. `resolve_source(slug, revision)` — the template snapshot from the store (§6).
   Always with the slug: a revision is the template's own last commit
   (`library/history.py` `last_commit`), and one commit that touched several
   templates — the seed of the bundled ones, a repo-wide reformat — gives them all
   the same value.
2. `export_schema(file)` — `.param` → `CustomizerSchema`, cached by source sha (as
   today, `render/runner.py:cached_schema`), now per file.
3. `validate(params, schema)` — the `require_valid_params` rule; a bad param fails
   *this* piece with today's 422 message.
4. `openscad_render(file, params)` — the main 3MF; heartbeats.
5. `openscad_solid(file, params, colour)` × N in parallel — the closed per-colour
   parts (`render/solids.py`); heartbeats.
6. `build_piece` — split, GLB, bbox, echoes, colour slots → a **Part** written to
   the `BlobStore` (§6) and returned by reference. In phase 1 the store is the
   `local` backend on the data volume, which is why phase 1 runs a single render
   worker (§11).

A `RenderPiece` is keyed by `piece_key`. Fourteen identical walls render once; two
people building the same house share one child; changing the wallpaper re-renders
walls but not floors or corner posts. Children are started with
`parent_close_policy=ABANDON`: cancelling a `TemplatePipeline` never cancels a piece
another job may be sharing, and a Part nothing references is swept by the store's
grace rule (§6.2).

Activity timeouts, and who kills what. `SCADBUDDY_RENDER_TIMEOUT` (operator-set,
default 120 s) stays the one number an operator tunes: the `openscad_*` activities
run the subprocess under it exactly as `runner.py` does today, and their
`start_to_close` is **derived** from it — `render_timeout + 60 s` — so the two can
never invert; the spec forbids a separate activity-timeout setting. On
cancellation, heartbeat failure or the activity's own timeout, the activity's
cancellation path kills the `openscad` process group before returning (the child
is killed, not awaited — the base spec's rule for the runner carries over); a
Temporal timeout alone does not stop a subprocess. Heartbeat every 5 s; retry
policy 3 attempts with backoff.

### 3.5 Worker versioning

Workers register with a **build ID** = the image's `SCADBUDDY_REVISION`. Temporal's
worker versioning (`use_worker_versioning=True`, build-ID sets) pins a workflow to
the build that started it: a deploy rolls new workers in, old workers drain their
in-flight runs, then exit. The Deployment's `preStop` waits for the worker's
`shutdown()` so a rolling update never kills a mid-house render. Workflow code
carries `workflow.patched(...)` markers only where a change must apply to running
workflows; the default is to let them finish on the old build.

### 3.6 Tests

- `temporalio.testing.WorkflowEnvironment` (time-skipping) for workflow logic, with
  activities mocked to record their calls.
- `requires_postgres` tests for the projection, coalescing and reconciler.
- The real `openscad` path runs in the Dockerfile's `test` image as today.
- CI's `postgres:17` service stays; a `temporalio/auto-setup` service is added to the
  backend job for an end-to-end submit → done test against a fake store.

## 4. Template UI

### 4.1 Declaration

`model.json` gains one optional object:

```json
"ui": {"module": "ui/index.js", "slot": "panel", "api": 1}
```

- `module` — an ES module under the template directory, served by
  `GET /models/{slug}/ui/{path}?version=<revision>` with the right MIME type and a
  `Cache-Control` tied to the revision. Assets it imports (`ui/**`) are served the same
  way. The files are git-tracked with the template, so duplicate, upstream merge,
  history and revision pinning carry the UI along.
- `slot` — `"panel"` replaces `ParameterPanel`; the host keeps preview, presets,
  history, Generate and Print. `"page"` gets the whole content area under the header;
  the host's preview and Generate are available as custom elements.
- `api` — the host-API major (§4.3) the UI was written against.

No `ui` → today's generated form.

### 4.2 Mounting

```js
export function mount(root: ShadowRoot, host: Host, ctx: {slot, version, theme}): (() => void) | void
```

Mounted into a shadow root for style isolation; **not** sandboxed (§9). If `mount`
throws, or the declared `api` major is unsupported, the host renders the generated
form with an error banner naming the template file and the error. A broken UI never
bricks a template. The Customize page shows the template's origin (built-in / mine /
imported from URL) beside a custom UI.

### 4.3 Host API v1

```ts
interface Host {
  inputs: { get(): Json; set(patch: Json): void; subscribe(fn: (i: Json) => void): () => void }
  schema(file?: string): Promise<CustomizerSchema>     // default model.scad
  files: { url(path: string): string }                  // template assets
  generate(): Promise<{ jobId: string }>
  openPrint(outputId: string): void
  presets: { list(); save(name); load(id) }             // over inputs
  describe?: (fn: () => string) => void                 // agent-facing summary (optional)
}
```

`inputs` is the one piece of state. It is JSON the template owns. For a template
with no custom UI, inputs are exactly the parameter values, so today's
`{params}` is one shape of inputs. Presets store inputs; outputs record inputs;
reopening an output restores inputs; the agent reads and writes inputs.

Custom elements, usable inside the shadow root under any framework:

- `<sb-param file="model.scad" name="lid_color">` — every existing widget by type,
  extruder numbers for colours, fonts, file upload with the asset store; bound to
  `inputs[name]` by default, or to a path via `bind="style.exterior"`.
- `<sb-preview>` — the react-three-fiber preview of the last job.
- `<sb-generate>` — the Generate button with the job's progress.

### 4.4 Frontend changes

`CustomizePage` becomes a shell: header, then either `<ParameterPanel>` + preview
(no `ui`) or `<TemplateUi slot=…>` which loads the module with a dynamic `import()` of
the versioned URL, creates the shadow root, builds the `Host` over the existing
hooks (`useRenderJob`, presets, `defaultValues`), and calls `mount`. The custom
elements are defined once, wrapping the existing widget components with
`react-dom/client` roots per element.

`RenderRequest` gains `inputs` (replacing `params`, which stays accepted and is
mapped to `{params}` inputs). `require_valid_params` moves into `RenderPiece`
(§3.4 step 3), since the API no longer knows which files the pipeline will render.

## 5. Template pipelines

### 5.1 Declaration and layout

```
models/dollhouse-kit/
  model.scad                unchanged
  parts/roof.scad           more entry files, any name
  ui/index.js               §4
  pipeline/pipeline.py      the workflow: run(ctx, inputs)
  pipeline/activities.py    arbitrary Python the pipeline calls by name
  model.json                + "ui": {...}, "pipeline": {"module": "pipeline/pipeline.py", "api": 1}
```

### 5.2 The contract

`pipeline.py` must be deterministic (Temporal's workflow sandbox enforces the import
and I/O rules and the error names the offending line). It gets one object:

```python
class Ctx:
    inputs_version: int
    plate: PlateGeometry                       # the selected/default printer's bed (#81)
    async def render(self, file: str, **params) -> Part        # child RenderPiece, deduped
    async def activity(self, name: str, *args, **kwargs) -> Any  # pipeline/activities.py:<name>
    async def pack(self, items: list[Part | tuple[Part, int]], *, goal: Goal = "fewest_plates") -> Layout
    def plate_of(self, items, *, at: list[tuple[x, y, rot]] | None = None) -> Plate
    async def output(self, *, plates: Layout | list[Plate], name: str | None = None,
                     bom: list[BomEntry] | None = None, files: dict[str, bytes | Blob] = {}) -> OutputRef
    def progress(self, message: str, *, done: int | None = None, total: int | None = None) -> None
```

- `render` is a child `RenderPiece` (§3.4). `Part` is a reference: per-colour meshes
  as blobs, `bbox`, `footprint`, `colours`, `echoes`. Reading mesh bytes happens in
  activities, never in the workflow.
- `activity(name, …)` runs `pipeline/activities.py:<name>` through one generic
  `run_template_activity(slug, revision, name, args)` on the `render` queue. Arguments
  and results are JSON plus `Blob` references; a template activity that needs a mesh
  gets it from the store. Heartbeat is automatic every 5 s; `start_to_close`
  defaults to `SCADBUDDY_RENDER_TIMEOUT + 60 s`, overridable per call up to
  `SCADBUDDY_TEMPLATE_ACTIVITY_MAX_TIMEOUT` (default 30 min). The generic activity
  runs the template function in a subprocess it kills on cancellation, like the
  `openscad_*` activities (§3.4).
- `pack` is the Arrange packing activity (§7) — `async`, because it reads footprints
  from the store and, for filament-aware goals, spool state from Bambuddy.
  `plate_of` is pure in-workflow construction of an explicit plate. Both yield a
  `Layout`; `output` writes it (multi-plate 3MF via
  #289's writer, thumbnails, `bom`, extra `files`) to the store and records the
  output row. A pipeline may call `output` more than once (one 3MF per storey).
- `bom` is structured, not a file: `[{piece, label, count, plates: [int], part: PartRef}]`,
  stored on the output and rendered by the host as a table; the agent reads it.

Template activities import what the worker image ships (`numpy`, `lxml`, `Pillow`,
the stdlib, and `scadbuddy.render.*` as a public surface documented in the authoring
skill). Nothing is installed per template.

### 5.3 The default pipeline

A template with no `pipeline` runs:

```python
async def run(ctx, inputs):
    part = await ctx.render("model.scad", **inputs["params"])
    await ctx.output(plates=await ctx.pack([part]), name=inputs.get("name"))
```

which is today's behaviour, including #289's `plates = N` echo handling inside
`pack`. Every Generate, on every template, goes through `TemplatePipeline`; there is
one code path.

### 5.4 Worked example: a whole dollhouse

UI (`ui/index.js`, `slot: "page"`): a room grid you click to add rooms, `<sb-param>`
for exterior, wallpaper and colours, a pitch slider, `<sb-preview>`, `<sb-generate>`.
It only edits inputs:

```json
{"v": 1,
 "rooms": [{"x": 0, "y": 0, "w": 2, "d": 2, "door": "south"},
           {"x": 2, "y": 0, "w": 2, "d": 2, "window": ["south", "east"]}],
 "pitch": 35,
 "style": {"exterior": "brick", "wallpaper": "stripes", "wall_color": "#F5E6C8", "exterior_color": "#B5523B"},
 "grid": {"module_size": 150, "course_height": 210}}
```

Pipeline:

```python
INPUTS_VERSION = 1

async def run(ctx, inputs):
    house = plan_house(inputs["rooms"])                       # pure template logic
    common = {**inputs["grid"], **inputs["style"]}
    parts = {}
    for i, (kind, params, count) in enumerate(house.bill):    # ("wall_window", {...}, 4)
        parts[kind] = (await ctx.render("model.scad", piece=kind, **params, **common), count)
        ctx.progress(f"Rendered {kind}", done=i + 1, total=len(house.bill))
    span = house.width_mm + 2 * parts["corner_post"][0].bbox.x       # a dependent step
    roof = await ctx.render("parts/roof.scad", span=span, pitch=inputs["pitch"], **common)
    halves = await ctx.activity("split_to_fit", roof, bed=ctx.plate)  # template Python
    guide = await ctx.activity("assembly_guide", house, list(parts))
    layout = await ctx.pack([*parts.values(), *[(h, 2) for h in halves]], goal="fewest_swaps")
    await ctx.output(name=f"{len(inputs['rooms'])}-room house", plates=layout,
                     bom=house.bom(parts), files={"assembly.svg": guide})
```

What happens: 9 distinct `RenderPiece` children (walls × course, window walls, door
halves, corner posts, floor tile) run in parallel across the render workers; the row
shows "Rendered wall (3 of 9)"; a worker dying at minute two costs one piece; the
output is one multi-plate 3MF (6 plates, 34 objects) plus `assembly.svg` and a BOM;
changing wallpaper re-renders walls but floors come from the store in a second;
reopening the output restores the room grid.

### 5.5 Authoring support

- `plugins/scadbuddy/skills/authoring` gains sections for `ui/`, `pipeline/`,
  inputs versioning and the `ctx` surface, with sources cited as `lint-plugin.sh`
  requires.
- `verify.sh` can run a template's pipeline against `temporalio.testing` with a
  local file-backed store, in the `base` image, so template CI exercises the
  pipeline and not only `openscad -D` cases.

## 6. The store

### 6.1 Why one store

Once activities run on any worker, everything a step reads must be reachable from
any worker. Today it all sits on one pod's volume:

| What | Today | Now |
|---|---|---|
| `// file` assets viewers upload (#204, #296, #384) | `data/assets`, content-addressed, reference-kept, grace-swept, capped | the store |
| Template source at a revision | git in `data/models` | a snapshot blob per (slug, revision) |
| Pinned libraries, Google Fonts | data volume | **fetched by each worker from their pin** (git `url@commit`, or an external URL with a sha256), cached locally; optionally mirrored into the store |
| Per-piece Parts (meshes, GLB) | attempt work dir | the store |
| Final outputs (3MF, extras) | `outputs/` | the store, in the folder #317 assigns |

The existing `AssetStore` already has the right rules: the id is the sha256 of the
content, a blob lives while an output, preset, job or template names it, unreferenced
blobs are swept after `SCADBUDDY_ASSET_SWEEP_GRACE`, the whole is capped and the usage
is on the Settings page. This spec generalises those rules to a `BlobStore` interface
and moves `AssetStore` onto it.

### 6.2 Interface

```python
class BlobStore(Protocol):
    async def put(self, kind: BlobKind, data: bytes | AsyncIterator[bytes], *, name: str, scope: Scope) -> BlobRef
    async def get(self, ref: BlobRef) -> AsyncIterator[bytes]
    async def stat(self, ref: BlobRef) -> BlobStat | None
    async def delete(self, ref: BlobRef) -> None
    async def list(self, scope: Scope) -> AsyncIterator[BlobStat]
```

`BlobRef = {sha256, kind, backend_id}`; `Scope = {slug, folder: "work" | "output",
project_id}`. References (`blob_refs` table: `ref`, `holder_kind`, `holder_id`) keep
a blob alive; `sweep` deletes unreferenced blobs older than the grace period, and
`put` refuses past `SCADBUDDY_STORE_MAX_TOTAL_BYTES` / `_MAX_COUNT`, except for
re-puts of what already exists. Workers keep a local LRU cache by sha256 under
`SCADBUDDY_WORKER_CACHE_DIR` (sticky scheduling is an optimisation, never a
correctness requirement).

`SCADBUDDY_STORE_BACKEND` selects the implementation. **`bambuddy` is the one
production backend.** A `local` backend (the data volume, today's layout) exists for
tests, `verify.sh`, and the phase-1 deployment, where it is correct only with a
single render worker on the same volume as the API; the interface lands in phase 1
so that phase 3 swaps the backend without touching the workflows.

### 6.3 The Bambuddy backend

Follows the layout #316 and #317 fix: the Settings `library_folder_id` folder is
ScadBuddy's inbox and the only place it deletes from; project folders are the user's
record, flat except `Media/`, never moved or deleted by ScadBuddy; no dot-named
folders (Bambuddy does not hide them).

```
<Inbox>/                              settings.library_folder_id — ScadBuddy-owned
  Dollhouse Kit/                      one folder per template
    Work/                             blobs: pieces, snapshots, assets; swept
      piece-3f9a…c1.3mf               a Part: its per-colour objects as a valid 3MF
      src-77b0…e4.zip                 a template snapshot at a revision
      asset-9c2d…a0.svg               a `// file` upload
    2-room house.3mf                  outputs made with "No project"
    2-room house — assembly.svg
Kids' room/                           a user project (#317)
  2-room house.3mf                    outputs made with this project active
  2-room house — assembly.svg
  2-room house.gcode.3mf              Bambuddy's slice, on print
  Media/
```

Files live in Bambuddy; metadata (manifests, BOM, inputs, revision, `library_files`
per #316) lives in Postgres. Render workers use a key with *Manage Library* only.

**To verify against the live Bambuddy before phase 3 lands** (the base spec's §3
rule: measured, not recalled): non-3MF uploads (SVG, PNG, ZIP) are accepted;
upload size limit; throughput of ~40 uploads per house; fetch by file id without a
folder scan; whether a `Work/` folder with hundreds of files degrades Bambuddy's
library UI; the maximum plates per 3MF Bambu Studio opens.

## 7. Objects and layout are separate; Arrange is a workflow

Rendering produces **objects**; plates are a **layout** over objects. `replate_3mf`
(`render/bambu3mf.py`) already re-lays out a finished 3MF for another printer without
re-rendering; this makes that the rule.

- Every output stores an **object manifest**: per object, its Part ref, bbox,
  footprint, colour slots, count, provenance (template, revision, inputs, BOM entry).
- A **Layout** is `[{plate: int, objects: [{part, at: (x, y, rot)}]}]` plus the plate
  geometry it was packed for. The 3MF is written *from* manifest + layout.
- `Arrange` is a workflow (a `render_jobs` row with `kind = 'arrange'`, §3.4) taking
  objects from one or more outputs (#314's build list), a filament assignment — a
  `FilamentPlan`, one chosen spool per slot, as the print-flow spec §1 defines it and
  the Print dialog already collects — and a `goal`:
  `fewest_plates` | `fewest_swaps` | `by_colour` (single-colour plates skip the prime
  tower) | `keep_together` groups. It produces a new layout → 3MF in seconds, with no
  re-render. `ctx.pack(goal=…)` in a pipeline awaits the same packing activity
  (§5.2).
- Heuristic, not a solver: group by colour signature, first-fit-decreasing 2D packing
  against `plate.py`'s exclusion zones and prime-tower rules, then order plates to
  minimise swaps. `Goal` is pluggable.
- Limit: objects from foreign 3MFs (Bambuddy library files, #313) have meshes and
  per-object filaments but no ScadBuddy manifest; Arrange can place them but not
  split them by colour.

## 8. Versioning

Four layers, each with an owner:

1. **Contract majors.** `model.json`'s `ui.api` and `pipeline.api`. The host supports
   the current major and the previous one; an unsupported major falls back (UI: the
   generated form with a banner; pipeline: the job fails at `load_pipeline` with a
   message naming the majors). Minor additions never break a template.
2. **Inputs.** A template declares `INPUTS_VERSION` in `pipeline.py` and stamps it as
   `inputs.v`. It may define `migrate(inputs, from_version) -> inputs`. Presets and
   outputs store inputs with their `v`; when one opens under a newer template
   revision, the host calls the pipeline's `migrate` (an activity, so it may read the
   snapshot) before handing inputs to the UI. A failed migration shows the raw inputs
   read-only with the error.
3. **Workflows.** §3.5: build-ID worker versioning; running workflows finish on the
   build they started on; `load_pipeline` records the pipeline source in history so a
   template edit never changes a running job.
4. **Reproducibility record.** Every output stores: template revision, `ui.api`,
   `pipeline.api`, `inputs.v`, the worker image digest (`SCADBUDDY_REVISION`), the
   `openscad --version` string, the plate geometry key, and the store refs of every
   Part it was built from. "Re-render" is a new job on the same record; "Customize
   this version" pins the revision.

## 9. Trust and containment

Decided: template code is **not sandboxed**. UI JavaScript runs in the page;
pipeline Python runs in the worker. The operator uploads their own templates.

Containment is by reach, not by restriction:

- Render workers hold: the store credential (Bambuddy key, *Manage Library* only),
  Postgres for the projection and manifests. They do **not** hold a key with *Manage
  Queue* or *Manage Projects*, the agent's secret key file, or the API's settings
  store. Template code can render and store; it cannot print, queue, or read the
  Claude credential.
- Provisioning the second key: Settings gains `bambuddy_render_api_key`, optional,
  created by the operator in Bambuddy with *Manage Library* only and stored like the
  existing `bambuddy_api_key` (same encryption, same rotation UI, §10). Render
  workers read only that field. When it is unset they fall back to the full key and
  the Settings page shows a persistent warning — "render workers hold the full
  Bambuddy key; template code can print" — so the containment claim is either true
  or visibly false, never silently false.
- Printing activities run on the `bambuddy` task queue in a separate Deployment
  that holds the fuller key and runs no template code.
- A URL-imported template (#174) that ships `ui/` or `pipeline/` is shown as such
  before its first Generate, with the file list; the operator confirms once per
  template revision.
- The homelab Bambuddy currently runs with authentication disabled
  (`bambuddy/errors.py:36`), so the scope split is real only once auth is on there.
  Stated, not hidden.

## 10. API changes

- `POST /models/{slug}/render`: body `{inputs, version?, supersedes?}`; `params` still
  accepted and wrapped as `{"params": …}`.
- `GET /jobs/{id}`: unchanged shape (`state` gains the value `cancelled`), plus
  `steps: [{name, state, done, total}]` from
  the projection.
- `GET /models/{slug}/schema?file=parts/roof.scad`.
- `GET /models/{slug}/files` — the template's files with kinds (`scad`, `ui`,
  `pipeline`, `sample`, `preset`).
- `GET /models/{slug}/ui/{path}?version=`.
- `GET /outputs/{id}`: plus `inputs`, `bom`, `manifest`, `record` (§8.4), `files`.
- `POST /outputs/arrange` `{objects: [{output_id, part, count}], goal, printer_id,
  filament_plan?}` → a `render_jobs` row with `kind = 'arrange'`, polled with
  `GET /jobs/{id}` like any render; its output is the new layout's 3MF.
- Presets (`library/presets.py`): `params` → `inputs` with `v`; old files read as
  `{"params": …, "v": 0}`.
- Settings: store usage replaces the asset-store line; Temporal address and namespace
  shown read-only; `bambuddy_render_api_key` beside `bambuddy_api_key`, with the
  fallback warning of §9.

Regenerate `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `agent/src/api/schema.d.ts`
per CLAUDE.md.

## 11. Phasing

Each phase is an epic with its own implementation plan; each ships independently and
leaves every template working.

1. **Temporal execution** (§3, §8.3). Replace the queue with `TemplatePipeline` +
   `RenderPiece` running only the default pipeline; projection; reconciler; worker
   Deployment at **one replica** sharing the API's data volume, with the `BlobStore`
   interface over the `local` backend (§6.2); worker versioning; `clusters`
   manifests (Temporal operator + CNPG, §3.1); `bambuddy_render_api_key` in
   Settings (§9). Visible change: none,
   except a worker restart no longer loses a render. Scaling render workers past one
   waits for phase 3.
2. **Template UI** (§4, §8.1). `ui` in `model.json`, served modules, `Host` v1,
   custom elements, the panel and page slots, inputs replacing params in presets and
   outputs. First user: `maze-puzzle` hides lid options; second: a `dollhouse-kit`
   house designer that still renders one piece at a time.
3. **Store** (§6). `BlobStore`, the Bambuddy backend, `AssetStore` on it, snapshots,
   worker cache, Settings usage, the Bambuddy verifications listed in §6.3.
4. **Template pipelines** (§5, §8.2). `pipeline` in `model.json`, `ctx`, template
   activities, `load_pipeline` with source in history, inputs versioning and
   `migrate`, the dollhouse house pipeline, authoring skill and `verify.sh` support.
5. **Arrange** (§7). Manifests on outputs, `Layout`, the `Arrange` workflow and
   `POST /outputs/arrange`, `goal`s, `ctx.pack(goal=…)`; folds #314's "combine N
   objects" onto it.

Order rationale: 1 is the foundation and has no template-visible surface, so it can
land while templates are untouched; 2 delivers the requested UX on the existing
renderer; 3 must precede 4 because multi-machine pipelines need the store; 5 needs
manifests from 4.

## 12. Relationship to open issues

- #289 (multi-plate templates, PR #386): its writer becomes `output`'s; its `plates =
  N` echo is honoured by the default pipeline's `pack`.
- #314 (combine objects onto one plate): becomes `Arrange` over several outputs.
- #316 / #317 (library file layout; project file on Generate): the store's folder
  rules; `output` writes where #317 says.
- #81 (plate follows printer): `ctx.plate` and `Arrange`'s plate geometry.
- #83 (plate picker): reads the layout's plates.
- #313 (print any library file): foreign objects in `Arrange`.
- #174 (URL import): the confirmation in §9.
- #15 (Customizer UI epic), #154 (templates epic), #325 (presets as first-class):
  parents of phases 2 and the presets change.
- #241 (Postgres render queue): superseded by phase 1; its projection survives.
- #252 (agent authoring): the agent writes `ui/` and `pipeline/` files too.
