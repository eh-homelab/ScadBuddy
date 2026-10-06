# ScadBuddy

A self-hosted OpenSCAD customizer that sits next to [Bambuddy](https://github.com/maziggy/bambuddy):
pick a parametric model, fill in its parameters, preview the render, and send
the result to Bambuddy's library or print queue. It ships as one container
image built on `openscad/openscad:dev` with a Python backend and a React
frontend layered on. The design and the measured OpenSCAD behaviour it rests
on are in [`docs/superpowers/specs/2026-09-22-scadbuddy-design.md`](docs/superpowers/specs/2026-09-22-scadbuddy-design.md).

It uses the same parameter syntax as MakerWorld's Parametric Model Maker, so a
`.scad` file that works there works here unchanged. Its output is a Bambu-style
3MF with one object per colour, each assigned to its own extruder, so the colours
are mapped to filaments without any painting in the slicer.

![The customizer rendering the bundled name keychain](docs/images/customizer.png)

**The [user guide](docs/user-guide.md)** covers the parameter syntax, the
multi-colour rules, connecting Bambuddy and each feature.

## Features

- **MakerWorld-parity customizer**: tabs from `/* [Group] */`, sliders, dropdowns,
  toggles, text limits, and `// color` / `// font` pickers.
- **Presets**: save named parameter sets per template — built-ins too — and start
  from one, changing only what differs this time (a name, a colour, a size). A
  preset keeps only the values that differ from the defaults. A template defines its
  own read-only presets in the `presets` list of its `model.json`
  (`{"id": "bag-tag", "name": "Bag tag", "params": {…}}`; the `id` keeps a preset the
  same one when it is renamed or moved); **Duplicate** copies one of those, or any
  saved preset, to an editable preset of your own. Any preset can carry a short
  Markdown `description` and `tags`, shown under the picker; **Edit details** renames a
  saved one and sets them. Saved presets are kept in the database.
- **The preview is the real render**: OpenSCAD (Manifold) runs on every parameter
  change and shows per-colour parts and the bounding box.
- **Multi-colour 3MF**: one closed solid per colour, each on its own extruder, with
  plate cover images and a layout sized for the target printer's plate.
- **Send to Bambuddy**: upload to a library folder; printing is the print picker's job.
- **Print picker**: spool-first — pick spools, nozzle size, a quality tier and a plate,
  and ScadBuddy derives the printer, process and filament presets and slices and queues
  through Bambuddy. No slicer pipeline to pick or maintain; Advanced mode adds per-side
  nozzle flow, the full process list and a per-slot filament preset override. Also lets
  you set copies, a project, and print options.
- **Library**: print any file already in Bambuddy's library through the same print
  picker, printed exactly as its author left it — never replated, recolored or
  uploaded again. Advanced also lists STLs, which print as one plate, and sliced
  `.gcode.3mf` files, which print from Bambuddy directly.
- **Fonts**: the image's fonts, plus any Google Fonts family, which is installed on
  demand.
- **Paste source / upload**: add models from a `.scad` file or pasted source,
  parse-checked by OpenSCAD before they are saved; edit the source in the browser.
- **History and versions**: every output keeps its parameters. Every model change is
  a git commit, so revisions can be diffed, customized or restored.
- **Delete**: remove a model and its outputs; the source stays in the git history.
- **Inside Bambuddy**: one click adds ScadBuddy to Bambuddy's sidebar as an External
  Link that opens inside Bambuddy.

| | |
|---|---|
| ![Catalogue](docs/images/catalogue.png) | ![Print picker](docs/images/print-picker.png) |

## Running it

```bash
docker run -d --name scadbuddy -p 8080:8080 -v scadbuddy-data:/data \
  -e SCADBUDDY_DATABASE_URL=postgresql://scadbuddy:secret@db:5432/scadbuddy \
  -e SCADBUDDY_PUBLIC_URL=http://<host>:8080 \
  ghcr.io/eh-homelab/scadbuddy:main
```

**Set `SCADBUDDY_PUBLIC_URL` to the URL you open the UI at** (#962). Writes from a
browser are only accepted from that origin, one in `SCADBUDDY_ALLOWED_ORIGINS`, or
loopback (see "Realtime" below). Left unset, with no allowed origins either, the
backend accepts a write from the page's own origin (its `Host`) so the install can
still be configured from Settings, but that leaves it open to DNS rebinding until a
public URL is saved.

**A PostgreSQL database is required** (#401): without `SCADBUDDY_DATABASE_URL`
the backend refuses to start and says so. Settings and the render jobs live
there; the schema is created and migrated at startup, so an empty database is
enough.

Then open `http://<host>:8080`, go to **Settings** and connect Bambuddy (see
[Connecting Bambuddy](docs/user-guide.md#connecting-bambuddy): the API key needs
**Manage Library**, **Manage Queue** and **Read Status**, plus **Manage Projects**
for the project picker).

**Renders run on Temporal** (#424, #546). `SCADBUDDY_TEMPORAL_ADDRESS` (the
Temporal frontend's `host:port`) is required: without it the backend does not start.
The API submits and a separate render worker (see "Render worker" under Deploying)
runs them. The API's client connects lazily, so the API boots while Temporal is
down, and its renders wait. For a one-process dev run, let the API host the worker itself:

```bash
temporal server start-dev --namespace scadbuddy      # listens on 127.0.0.1:7233
cd backend
SCADBUDDY_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy \
SCADBUDDY_DATA_DIR=$HOME/.scadbuddy-data \
SCADBUDDY_TEMPORAL_ADDRESS=127.0.0.1:7233 \
SCADBUDDY_TEMPORAL_WORKER_INPROCESS=true \
  uv run --frozen uvicorn --factory scadbuddy.main:create_app --port 8080
```

`SCADBUDDY_DATA_DIR` must be writable (its default is `/data`), and a real `openscad`
must be on `PATH` (or named by `SCADBUDDY_OPENSCAD`). The API serves the UI only once
`frontend/dist` is built (`pnpm build` in `frontend/`); otherwise it serves only the API.
`SCADBUDDY_TEMPORAL_NAMESPACE` defaults to `scadbuddy` (hence `--namespace` above)
and `SCADBUDDY_TEMPORAL_TASK_QUEUE_RENDER` to `render`
(`SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY`, the API's own housekeeping queue, to `library`).
`SCADBUDDY_TEMPORAL_WORKER_INPROCESS` is for dev and tests only; it does not drain
on shutdown.

- **Image:** `ghcr.io/eh-homelab/scadbuddy` is a **public** GHCR package (no pull
  secret needed), built for `linux/amd64` and `linux/arm64`. It has these tags:
  `main` (latest `main`), `sha-<short>`, and `X.Y.Z` / `X.Y` for releases.
- **LAN only.** ScadBuddy has **no authentication**. Anyone who can reach it can
  add, edit and delete models, and send prints using the stored Bambuddy key.
  Keep it on a trusted network, as you would Bambuddy's slicer sidecar. Do not
  expose it to the internet.
- **State** lives in `/data` (`SCADBUDDY_DATA_DIR`): models (a git repository),
  outputs, downloaded fonts and caches. Back up the volume. Settings and saved presets are in the
  database, not in `/data`: back that up too. The Bambuddy API key is stored there as plain text
  (it used to be a 0600 file on the volume), so it is in every database backup;
  supply it with `SCADBUDDY_BAMBUDDY_API_KEY` from a secret if that matters. The
  image carries BOSL2 at the catalogue's ref and copies it into
  `/data/libraries` at start when it is not there, so a fresh install renders
  BOSL2 models without network access (licence:
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
- **Environment** (all optional but `SCADBUDDY_DATABASE_URL` and
  `SCADBUDDY_TEMPORAL_ADDRESS`): every variable is a field of `Settings` in `backend/scadbuddy/core/settings.py`, as
  `SCADBUDDY_<FIELD>`, with what it does in the comment beside it. The full list,
  with each type and default, is generated rather than kept here:
  `cd backend && uv run python -m scadbuddy.tools.settings_reference`. A new
  setting adds a field there, not a line here (#508). The notes below are the
  ones that need more than a comment.
  A variable whose "In Settings" column is `live` or `restart` only sets the
  starting value (#322): once a value is saved from the UI it wins, taking effect
  at once or at the next start; a field the UI never saved keeps following the
  variable, and one it cleared stays cleared (the upload limit instead goes back
  to the variable). `no` is read from the environment only.
  `SCADBUDDY_PREVIEW_RENDERS` (default `true`: a model with no thumbnail and no
  generated output is rendered at its default settings in the background, one at
  a time and behind any render someone asked for, and that plate image is its
  catalogue thumbnail; `false` renders nothing, and such a model shows no image
  until one is set or generated. The previews are kept in the
  `SCADBUDDY_DATABASE_URL` database's `model_previews` table. The pass over every
  model is the Temporal Schedule `scadbuddy-previews-library`, on the API's own
  `library` queue: every hour and once at each start, it renders the previews that
  are missing or stale, which after the first pass is none. `false` deletes it; a
  Schedule paused in the Temporal UI stays paused across restarts. Its id ends in the
  library queue's name, like the housekeeping ones);
  `SCADBUDDY_OPENSCAD_LSP` (default `openscad-lsp`, the language server binary);
  `SCADBUDDY_LIBRARY_MAX_BYTES` (default 200000000, the most one added library's
  clone may take on the volume; the clone's size is measured while it runs, so it
  can overshoot by roughly one poll interval's worth of transfer, 0.2 to 2 s, plus
  one walk of the clone, which takes longer the more files it has, before it is
  stopped); `SCADBUDDY_DUPLICATE_STAGING_MAX_AGE` (default 3600 s, at least 1: how
  old a duplicate's staging copy under `/data/cache` must be before it is treated
  as a crashed copy and removed, at startup, after a duplicate and with the
  periodic upload sweep; keep it well above the longest copy, since replicas
  sharing `/data` may be mid-copy).
  Each concurrent render or check is its own `openscad` process, and each open
  source editor holds one `openscad-lsp` process for as long as it stays open,
  so size CPU and memory for the sum of all three. Past the session cap an
  editor still works, without completion and hover.
  A render's closed parts take one more `openscad` run per colour, and
  `SCADBUDDY_SOLID_CONCURRENCY` of those run at once per render. Left at 0 it is
  the CPUs the container may use (a cgroup v1 or v2 CPU limit counts), less
  `SCADBUDDY_CHECK_CONCURRENCY`, divided by `SCADBUDDY_RENDER_CONCURRENCY`, between
  1 and 8, so renders and checks together stay at one process per CPU. If no
  cgroup CPU controller is readable, a warning is logged at the first render and it
  sizes for every CPU it can see; set it by hand there. It reads no memory limit:
  size memory for `SCADBUDDY_RENDER_CONCURRENCY` × this many processes.
  Each of them gets the whole `SCADBUDDY_RENDER_TIMEOUT` from when it starts.
- **Uploaded files** (the SVGs and PNGs for `// file` parameters, in
  `/data/assets`):
  - `SCADBUDDY_ASSET_MAX_TOTAL_BYTES` (default 1000000000) and
    `SCADBUDDY_ASSET_MAX_COUNT` (10000), 0 for no limit: a new upload that would
    take the store past either is refused with 413. Re-uploading a file already
    stored is never refused.
  - `SCADBUDDY_ASSET_SWEEP_GRACE` (default 604800 s, a week; at least 3600): a
    file that no saved output, preset or render job references is removed once
    nothing has uploaded or used it for this long. The same grace applies to the
    blob store's pieces and snapshots (see "Blob store and render workers").
  - `SCADBUDDY_ASSET_SWEEP_INTERVAL` (default 86400 s): how often that sweep runs;
    0 turns it off. It is the interval of the Temporal Schedule
    `scadbuddy-housekeeping-library`, on the API's own `library` queue
    (`SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY`); 0 deletes the Schedule. Every start
    also triggers the Schedule once, a full sweep: on the Bambuddy blob store it
    converges the uploads with the store (reconcile and backfill), so expect that
    Bambuddy traffic right after a deploy. A run still open from before the start
    goes first, and that sweep follows it. A Schedule paused in the Temporal UI stays
    paused across restarts, and a start does not trigger it: while it is paused nothing
    sweeps the uploads or reconciles them with the store, and a start only backfills
    them to the Bambuddy store. Settled render jobs are pruned every 300 s by a second
    Schedule, `scadbuddy-prune-library`, which 0 leaves alone. Both ids end in the
    library queue's name: changing `SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY` leaves the
    old two Schedules starting runs on a queue nothing serves, so delete them by hand
    (`temporal schedule delete --schedule-id scadbuddy-housekeeping-<old queue>`, and
    the same for `scadbuddy-prune-<old queue>`). The same interval drives the blob
    store's sweep, which 0 also turns off, and a render worker's piece-cache
    eviction, which 0 does not: a worker then evicts every 300 s.
  - The same periodic sweep also clears old duplicate staging
    (`SCADBUDDY_DUPLICATE_STAGING_MAX_AGE`), so 0 leaves that to startup and the
    next duplicate.
  - The `library` queue's worker is not versioned: any API replica may take any of
    its tasks. A release that adds a sweep (a new activity) says so here and needs a
    `Recreate` rollout, or the old replicas scaled to 0 first: a replica still on the
    old build takes the new sweep's task and fails it as unregistered, and since a
    sweep is not retried, that sweep waits for the next tick (a day, by default).
  - Settings shows the usage under "Uploaded files"; so do
    `GET /api/v1/assets/usage` and the `scadbuddy_assets_*` metrics.
- **Template media** (images and videos, in `/data/models/<slug>/media`):
  `SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES` (default 1073741824, 1 GiB) is the largest
  single upload. Settings can change it, and a change applies at once
  (`media_upload_max_bytes`).
  The upload is streamed to the data volume, never held in memory. Images (and
  posters) are also capped at 10 MiB, since they are committed to the models'
  history; videos are not committed.
- **Render queue.** By default every render request is accepted and runs on
  Temporal: the API records the job in `render_jobs` and starts its workflow, and
  the render worker renders `SCADBUDDY_RENDER_CONCURRENCY` at once. A preview
  replaced by a newer one is cancelled, waiting or running, and identical waiting requests share
  one job. An identical OpenSCAD run (same template, revision, file and
  parameters) is rendered once and its piece kept in the blob store
  (`/data/blobs/`), so a later job that needs it reuses it; a piece no job
  references is removed after `SCADBUDDY_JOB_TTL`.
  - `SCADBUDDY_RENDER_QUEUE_MAX` (0 = no limit): set, a request that would be a new
    job while that many already wait gets 503 with `Retry-After`. A request that
    matches a job still open (pending or running) joins it and is never refused, and
    the waiting preview a request supersedes does not count against the limit.
  - `SCADBUDDY_DATABASE_URL` (libpq URL, required): the jobs are rows in Postgres
    (`render_jobs`), so accepted renders survive a restart. A row is written by its
    workflow's first activity, so it exists only once Temporal has the render; with
    Temporal unreachable a render is refused (503 `temporal-unavailable`). At start and
    every five minutes the API fails the rows nothing will settle: one whose workflow
    closed without settling it (terminated by hand, say), and a pending or running one
    an older release left with no workflow running. Each pass lists the open
    `TemplatePipeline` runs from Visibility once and describes only rows over 30 s old
    that the listing leaves out; `scadbuddy_render_settle_failed_total` and
    `scadbuddy_render_settle_errors_total` count what it failed and the passes that
    could not finish.
    `SCADBUDDY_DATABASE_POOL_SIZE` (10, per pool: the jobs and the settings each
    hold one). The schema is created and migrated at startup.
  - The **event bus** (spec §7) is in the same Postgres database (the backend
    will not start without `SCADBUDDY_DATABASE_URL`, #467): each change is appended to an `events` table and sent with
    `NOTIFY scadbuddy_events` in one transaction, and every replica's subscribers
    hear it once over the process's one `LISTEN` connection. After
    that connection drops and comes back, subscribers get a `bus.resync` event.
    The table keeps events for `Last-Event-ID` replay:
    `SCADBUDDY_EVENT_LOG_RETENTION_SECONDS` (86400) and
    `SCADBUDDY_EVENT_LOG_RETENTION_ROWS` (100000), 0 for no limit, pruned by every
    replica every 5 minutes.
    `scadbuddy_events_published_total`, `scadbuddy_events_dropped_total{reason}`,
    `scadbuddy_events_received_total`, `scadbuddy_events_resyncs_total` and
    `scadbuddy_event_log_pruned_total` show it working. NOTIFY channels are per
    database, so give each deployment its own database, not just its own schema.
  - `SCADBUDDY_RENDER_QUEUE_DEPTH_SLO` (16) and `SCADBUDDY_RENDER_LATENCY_SLO`
    (60 s): targets, not limits. They are exported with the metrics for alerts.
- `GET /healthz` reports the OpenSCAD version, whether the data directory is
  writable, the build revision, and a `temporal` object (`address`, `namespace`,
  `task_queue`, `worker_inprocess`).
- `GET /metrics` serves Prometheus metrics: render queue depth and oldest wait
  (read from the store in Postgres, so across replicas), wait time and latency
  (`scadbuddy_render_job_latency_seconds`, by outcome), per-stage render time, whether
  the jobs table can be read (`scadbuddy_render_store_up`), whether the process's
  `LISTEN` connection is up (`scadbuddy_render_queue_listener_connected`,
  `scadbuddy_render_queue_listener_reconnects_total`), the SLO targets, the upload store's files and bytes against its caps
  (`scadbuddy_assets_*`), and HTTP requests by route. It is unauthenticated, like the rest of
  the app.

**Realtime.** The UI follows changes over `WS /api/v1/ws`, served by the
backend (spec §4.2, #266). A browser's `Origin` must be the stored public URL's
origin (Settings, seeded from `SCADBUDDY_PUBLIC_URL`), one of
`SCADBUDDY_ALLOWED_ORIGINS`, or a loopback origin; anything else is refused,
which stops DNS rebinding. If the socket can't connect, the header shows "Live
updates unavailable" and views poll instead. A deployment reached under more
than one hostname (a LAN host and an SSO proxy, say) lists every hostname that
is not the public URL in `SCADBUDDY_ALLOWED_ORIGINS`
(`https://scadbuddy.internal.example,https://scadbuddy.sso.example`); otherwise
the pages on the other hostname show "Live updates unavailable" while the same
pages on the public URL work, and the backend log says
`refused a realtime socket from origin ...`.

**Writes** (#962) use the same rule. A `POST`, `PUT`, `PATCH` or `DELETE` whose
`Origin` is not one of those origins gets a `403` problem whose detail names
`SCADBUDDY_PUBLIC_URL` and `SCADBUDDY_ALLOWED_ORIGINS`, before its body is read, and
the backend logs `refused a POST /api/v1/... from origin ...`. This is what stops a
page on another site from creating models, restoring revisions or printing through
a LAN user's browser. So a hostname missing from the list cannot save, render or
print, not just lose live updates. A request with no `Origin` (curl, scripts, the
agent's server-side calls) is not a browser page and is not affected. While neither
a public URL nor `SCADBUDDY_ALLOWED_ORIGINS` is configured, a write from the
request's own origin (`Origin` equal to its scheme and `Host`) is also accepted, so a
fresh install can be configured; that gives up DNS-rebinding protection until one
of the two is set. The agent reads the same variable for its own origin check
(below).

## Deploying

ScadBuddy runs on the homelab cluster from
[eh-homelab/clusters](https://github.com/eh-homelab/clusters)
(`applications/scadbuddy/scadbuddy.yaml`, deployed by ArgoCD, the render
worker's `applications/scadbuddy/scadbuddy-render.yaml`, and the print worker's
`applications/scadbuddy/scadbuddy-print.yaml`). Those manifests pin the
image **by digest**; this repo's workflows are what move the pin.
Nothing here talks to the cluster.

The backend needs its database (#401): the manifest must set
`SCADBUDDY_DATABASE_URL` (the cluster's `scadbuddy-db`), or the pod never
becomes ready and its log names the missing variable. The settings live in that
database, so the Bambuddy connection a deployment needs from the first start
comes from `SCADBUDDY_BAMBUDDY_URL`, `SCADBUDDY_BAMBUDDY_API_KEY` (from a Secret)
and `SCADBUDDY_PUBLIC_URL`. Every hostname the UI is served under must be the
public URL or listed in `SCADBUDDY_ALLOWED_ORIGINS`: from any other, browser writes
are refused with a `403` and a `refused a ... from origin ...` log line (#962), and
live updates are unavailable.

A deploy that rolls the pod also migrates the database at startup
(`backend/scadbuddy/migrations/`: `20260928T0630Z_events.sql` adds the `events`
log, `20260928T0724Z_analyzer_decisions.sql` the print analyzers'
`analyzer_decisions`, and `20260928T0840Z_settings.sql` the settings tables). The
event log's retention is `SCADBUDDY_EVENT_LOG_RETENTION_SECONDS` /
`SCADBUDDY_EVENT_LOG_RETENTION_ROWS` (see the render queue settings above); the
defaults need no manifest change.

```mermaid
flowchart LR
    A[push to main] --> B[Build Image]
    T[publish release vX.Y.Z] --> B
    B -->|main build| C[Deploy main]
    T --> D[Deploy release]
    C --> R[deploy.reusable.yml]
    D --> R
    R -->|"PR deploy(scadbuddy): version<br/>auto-merge armed"| K[eh-homelab/clusters]
    K -->|merge| G[ArgoCD sync]
    G --> V[scadbuddy-verify-deploy.yml<br/>/healthz revision == pinned]
    V -->|comment| K
    V -->|release notes or commit comment| S[this repo]
```

### The two paths

| Path | Trigger | What gets pinned | Reports to |
|---|---|---|---|
| **Continuous** — `deploy-main.yml` | `Build Image` succeeds for a push to `main` | `scadbuddy:sha-<short>@sha256:…` | a comment on the commit |
| **Release** — `deploy-release.yml` | a GitHub release `vX.Y.Z` is published (pre-releases excluded) | `scadbuddy:X.Y.Z@sha256:…` | a `## Deployment` section appended to the release notes |

Both call `deploy.reusable.yml`, which:

1. mints an `eh-homelab-org-runners` App token scoped to `clusters`
   (contents + pull requests) — never `GITHUB_TOKEN`, which cannot write to
   another repo and whose PRs would not run clusters' own CI;
2. rewrites the image line and the three `scadbuddy.eh-homelab.io/*`
   annotations (`version`, `revision`, `source`) in `scadbuddy.yaml`, and in
   `scadbuddy-render.yaml` and `scadbuddy-print.yaml` when those files exist in
   clusters (#547, #1060; until clusters adds one, the run notes its absence and
   pins the rest). Each
   file must have exactly one such image line and one of each annotation, before
   and after the rewrite, or the deploy stops. In the same PR it moves the
   dashboard's pin in `clusters/prod/scadbuddy/kustomization.yaml`, the line
   `- https://github.com/eh-homelab/ScadBuddy//deploy/grafana?ref=<40-hex SHA>`, to
   the same revision. That line is optional: an overlay that does not mention
   `eh-homelab/ScadBuddy//deploy/grafana` at all deploys the images alone with a
   notice; one that mentions it in any other form (a short SHA, a branch, a
   comment, other casing or spacing) or twice stops the deploy;
3. opens **one** PR, `deploy(scadbuddy): <version>`, on the fixed branch
   `deploy/scadbuddy`, and arms `gh pr merge --auto --squash`. A newer deploy
   closes an older open one and replaces the branch; this one job carries a
   concurrency group shared by both paths, so only the branch/PR handling
   serialises — a release's image wait never holds up a main deploy. GitHub
   keeps one running and one queued job per group and drops the queued one
   when a third arrives, so the **newest** request always runs; an overtaken
   deploy is reported (⚠️ on the commit or release) rather than lost, and
   re-running it deploys that build anyway.

The clusters ruleset (`CI Summary` + `claude-review`) gates the merge; the
merge is the deploy. ArgoCD then syncs the `scadbuddy` Application, and
clusters' `scadbuddy-verify-deploy.yml` (on a LAN runner, since GitHub-hosted
runners cannot reach the cluster) polls the pod's `/healthz` until it reports
the pinned `revision` — the commit stamped into the image at build time by
`SCADBUDDY_REVISION` — then posts the result on the deploy PR and back here.
That is a stronger proof than "the image field changed": the ReplicaSet rolled
and the new pod is serving that exact build.

### Render worker (#424)

Renders run on a separate worker. It is the
**same image** run as `python -m scadbuddy.worker`, and it serves `/healthz`
(`{"ok": true, "build_id": …, "task_queue": …}`) and `/metrics` on port **9090**.
Probe that port: the image's `HEALTHCHECK` is the API's 8080.

- **One replica in phase 1**, sharing `/data` with the API. A rendered piece is
  written to `/data/blobs/<piece_key>/` and read back by the API, so both pods mount
  the same volume. A ReadWriteOnce volume is fine as long as both pods run on the
  same node (pod affinity); a `ReadWriteOncePod` volume is not, because only one pod
  may mount it. A piece no job references any more is removed by the API's periodic
  upload sweep once it has gone `SCADBUDDY_JOB_TTL` untouched, the same retention as
  the jobs.
- **Environment:** `SCADBUDDY_DATABASE_URL` (the same database: the worker writes
  the `render_jobs` rows and their `job.*` events), `SCADBUDDY_TEMPORAL_ADDRESS`,
  `SCADBUDDY_TEMPORAL_NAMESPACE`, `SCADBUDDY_TEMPORAL_TASK_QUEUE_RENDER`,
  `SCADBUDDY_DATA_DIR`, and `SCADBUDDY_REVISION`, which is the worker's **build
  id** (the image stamps it). Keep the render settings (`SCADBUDDY_RENDER_TIMEOUT`,
  `SCADBUDDY_RENDER_CONCURRENCY`, `SCADBUDDY_SOLID_CONCURRENCY`) the same as the
  API's: the worker runs openscad under them, and the API derives the workflows'
  timeouts from the same values.
- **Versioning:** workflows are pinned to the build that started them. At start
  the worker makes its own build the deployment's current version, so a new build
  receives new workflows once it is polling. It retries that for a minute after it
  starts polling, because Temporal 1.28 accepts a build only once it has a poller.
- **Shutdown:** SIGTERM (tini forwards it; no `preStop` needed) starts the
  drain. The worker keeps polling until no workflow pinned to its build is
  running. If its build is still (or again) the current one, as on a restart of the
  same build (a manifest change, a node drain), it stops after 30 s instead, because
  the next pod of that build serves its pinned runs; it logs "stopping without
  draining" then, so a pod that never comes back is visible. The drain lasts at
  most `2 × (SCADBUDDY_RENDER_TIMEOUT + 60) + 120` s. Then the
  SDK gives in-flight activities up to `SCADBUDDY_RENDER_TIMEOUT + 60` s. If the
  drain's bound passes first, the worker exits anyway (it logs "drain timed out"):
  workflows still pinned to its build then have no poller, and their jobs stay
  `running` until that build polls again. Set `terminationGracePeriodSeconds` ≥
  `3 × (SCADBUDDY_RENDER_TIMEOUT + 60) + 120` plus a little slack for teardown
  (e.g. 30 s): **690 s** at the default 120 s timeout. Roll a new build out with
  `RollingUpdate` and `maxSurge >= 1`, so the new build is current before the old
  pod drains. Under `Recreate` the old build is still current when it stops, so it
  stops after the 30 s, and the new build's pod makes itself current only after it
  starts: runs pinned to the old build then have no poller.
- **Temporal itself** comes from the Temporal operator with a CNPG Postgres in
  `eh-homelab/clusters` (clusters#1454). `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` (the
  API hosting the worker) is for dev and tests only.
- **Upgrading from a release that still had the legacy queue** (before #546): its
  pods render in-process and renew a lease in `render_jobs.heartbeat_at`, which this
  release's first start drops. Do not let one overlap a new pod: stop the old pods
  (or use a `Recreate` rollout) before the new API starts, and start the API before
  the render workers. At start the API fails every render the old queue left
  `running` (no workflow; nothing would finish it), with an error naming the
  upgrade; its `pending` renders are failed too (#1053: nothing reconciles them). From a release
  already on Temporal (#600 or later, `SCADBUDDY_TEMPORAL_ADDRESS` set) there is
  nothing to do. Nothing reads what the legacy queue left on the volume any more:
  `data/jobs/` (job files and `.work` dirs) and `models/*/.renders/` can be deleted.
- **Upgrading to the release with #1053** moves renders onto the command shape: the
  workflow `render-<render key>` inserts its own row. Do not let an older API overlap a
  new one: stop the old API pods (or use a `Recreate` rollout, as the manifest does)
  before the new API starts. An older API beside it would restart this release's
  waiting renders as its own (its reconciler) and count requests into them that the
  workflow never sees (its insert). The older render workers may keep running: they
  finish the renders pinned to their build. A pending row of the older API's that no
  workflow will run is failed, once it is 30 s old, by the next render of its key or
  the API's next pass over such rows.
- **Upgrading from a release with the in-process print watcher** (before #1053): roll
  it out with `Recreate` (old replicas at 0 first). An old pod still logs prints to
  `print_watches` after the new one hands that log to `FollowPrint` at start, and
  those prints would go unfollowed until someone opens their progress.

### Bambuddy writes on the `bambuddy` queue (#1052, #1053, #1060)

Print runs and every other Bambuddy write (send, project files, projects, reprint,
timelapse pull, sidebar registration, and the inbox copies an output delete takes) run as
Temporal workflows on the `bambuddy` task queue (`SCADBUDDY_TEMPORAL_TASK_QUEUE_BAMBUDDY`),
with `FollowPrint`'s long `follow_print` activity on `<bambuddy queue>-follow`
(`bambuddy-follow` by default), so a followed print never holds a slot a print run or an
operation needs.

**The print worker (#1060)** serves both queues: the **same image** run as
`python -m scadbuddy.worker --queue bambuddy`, the Deployment `scadbuddy-print` in
clusters. It serves `/healthz` (`{"ok": true, "build_id": …, "task_queue": …}`) and
`/metrics` on **9090**, like the render worker.

- **No volume.** It does not mount `scadbuddy-data` and runs no template code. It reads an
  output's record, its stored `model.3mf` and the names taken from the model's files from
  the API's cluster-internal routes (`/api/v1/internal/outputs/…`, not in the OpenAPI
  schema), at `SCADBUDDY_API_INTERNAL_URL` (the API's Service, e.g.
  `http://scadbuddy:8080`; never the ingress). Those routes are internal by path only:
  like the rest of the API they have no auth today, so whoever reaches the API reaches
  them; once the API gains auth, they need a cluster-internal guard of their own. An output's last print is recorded in
  Postgres (`output_last_prints`); an older `meta.json`'s last print still reads for an
  output not printed since.
- **Environment:** `SCADBUDDY_DATABASE_URL` (the stored settings, the Bambuddy key
  included, are read from it on every use), `SCADBUDDY_TEMPORAL_ADDRESS`,
  `SCADBUDDY_TEMPORAL_NAMESPACE`, `SCADBUDDY_TEMPORAL_TASK_QUEUE_BAMBUDDY`,
  `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES`, `SCADBUDDY_API_INTERNAL_URL`, and the Bambuddy
  URL and key as the API has them (they only seed a database never saved). Its build id
  is `SCADBUDDY_REVISION`, stamped in the image.
- **Versioning:** the worker deployment `scadbuddy-print`, made current at start as the
  render worker's is. `PrintRun` and `Operation` are pinned to the build that started
  them; `FollowPrint` is AUTO_UPGRADE, since it lasts as long as the print, and moves to
  the new build. Changes to any of them are still made with `workflow.patched`.
- **Shutdown:** SIGTERM drains the build's pinned runs, as the render worker does, for at
  most 1920 s (`REPEAT_WINDOW` 600 s, two slice timeouts of 600 s, and 120 s): a print run
  stays open for `REPEAT_WINDOW` after it ends, so repeats of the same press find it. Set
  `terminationGracePeriodSeconds` to **2000** and roll with `RollingUpdate`,
  `maxSurge >= 1`.
- **The API** serves the queue itself only with `SCADBUDDY_TEMPORAL_WORKER_INPROCESS`
  (dev, tests) or `SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS` (a deployment without the
  `scadbuddy-print` Deployment yet), unversioned, on its own volume. Either way the API
  ends print runs and operations whose execution was terminated, and hands the old
  watcher's prints to `FollowPrint` at start.
- **Upgrading to the release with #1060**: deploy `scadbuddy-print` with it, or set
  `SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS=true` on the API until it exists; without
  either nothing polls the `bambuddy` queue, and prints and Bambuddy writes wait. Roll the
  API out with `Recreate`, so no old replica keeps polling the queue unversioned beside
  the new worker.
- **Upgrading to the release with #1053** adds two workflow types (`Operation`,
  `FollowPrint`) and their activities to that queue, and the `<bambuddy queue>-follow`
  queue beside it. The follow worker has `FOLLOW_SLOTS` (200, `bambuddy/follow.py`) slots
  per process: each print holds one while it moves (a poke's old attempt holds its own
  for up to about 24 s more). Past them, new prints wait on the queue unfollowed: watch
  `scadbuddy_print_follows_running`, and the warning "every follow slot is taken". This
  release **must** roll out with `Recreate` (or the old replicas scaled to 0 before the
  new ones start). The homelab deployment sets `strategy: Recreate` in
  eh-homelab/clusters#1669. A replica still on the old build takes those tasks and fails
  them as unregistered. A workflow task is retried, so an `Operation` or `FollowPrint`
  there only stalls. An activity task's failure counts against its retry policy: the
  effect of a reprint, a timelapse pull or a project write runs at most once, so one such
  task on an old replica records the operation `failed` as "may have been done" although
  nothing reached Bambuddy, and a check whose three attempts all land there is refused
  with a 500.
- **Retention:** Settings' "Keep finished Bambuddy operations for" (at least a day)
  must be at least the Temporal namespace's retention (`DescribeNamespace`'s
  `workflow_execution_retention_ttl`): a save below it is refused with a 422 beside the
  field, and while Temporal cannot be reached a changed value is refused with the
  `temporal-unavailable` 503 rather than saved unchecked (the other settings still save);
  one Temporal refuses to describe (a denied permission) is a `temporal-refused` 500.
  A retry of an operation whose record was deleted while Temporal still holds its closed
  execution would answer 409 "may have been done" instead of its outcome. Raising the
  namespace's retention after the save is not re-checked.
- **Later changes** to `PrintRun` or `Operation` are made with `workflow.patched` (see
  "Versioning" above); a release that adds a workflow or activity type to the queue says
  so here, and where the API still serves the queue itself (unversioned) needs the same
  `Recreate` rollout.

### Blob store and render workers (#426)

Everything a render reads or writes (rendered pieces, template snapshots, uploaded SVGs
and PNGs, downloaded fonts) lives in the blob store. Settings → **Blob store** picks
where. The choice is read at start, so it takes effect only when the API and the
workers restart.

- **`local`** (the default): this server's volume, phase 1's topology. There is **one**
  render worker, and it shares the API's `/data` volume, as described under "Render
  worker (#424)" above.
- **`bambuddy`**: Bambuddy's library. Files go to `<Library folder>/<Template>/Work/`,
  and ScadBuddy deletes only inside a `Work/` folder of the Library folder Settings
  names. Changing that folder leaves the previous one's `Work/` files for you to delete.
  The same goes for the Bambuddy URL: folders are recorded per instance, so pointing
  ScadBuddy at another Bambuddy makes new folders there and never deletes by the old
  instance's folder ids (#683). Respelling the same URL (host case, a default port, a
  trailing slash) is the same instance; another host, scheme, port or path is not.
  To switch:
  1. Set Bambuddy's URL and a **Library folder** (the store's inbox) in Settings.
  2. In Bambuddy, create a key with *Manage Library* only, and paste it into Settings as
     **Render key**.
  3. Choose **Bambuddy library** under **Blob store**, and restart the API and the
     workers.

  The workers then need no shared volume:
  - Give each one an `emptyDir` at `/data`. It holds the worker's piece cache (`blobs/`)
    and the snapshots, fonts, uploads and library checkouts it fetched.
  - Previews (the catalogue's default renders) render on the workers too, from the
    snapshot of the template's last commit, which the API stores before it asks.
  - At start a worker seeds the image's libraries (BOSL2 and the rest of the curated
    set) onto its volume, as the API does. A library a template pins outside the
    image is cloned on **each worker pod**, the first time it renders that template,
    so the workers need network access (egress) to those libraries' Git remotes.
  - On the workers, set `SCADBUDDY_ASSET_SWEEP_INTERVAL` short, e.g. `900`. On that
    interval, and **only** then, a worker trims its cache to
    `SCADBUDDY_WORKER_CACHE_MAX_BYTES` (least recently used first), removes the
    revision exports it has not used for `SCADBUDDY_JOB_TTL` (a day by default), and
    removes the uploads it has not used for `SCADBUDDY_ASSET_SWEEP_GRACE` (at least an
    hour; it fetches one again when a render names it). `0` (or unset) does not
    stop that pass: the worker falls back to a fixed 300 s.
  - Size the `emptyDir`'s `sizeLimit` for what bounds each part, plus one interval's
    writes. Past it, the kubelet evicts the pod mid-render, with no drain.

    | Part | What bounds it | Default bound |
    |---|---|---|
    | the cache after a trim | `SCADBUDDY_WORKER_CACHE_MAX_BYTES` | 10 GiB |
    | one interval's new pieces | render slots × (interval ÷ time per piece) × piece size, e.g. 2 × (900 s ÷ 30 s) × 20 MiB | 1.2 GiB |
    | uploads fetched (`assets/`) | last use: those a render named within `SCADBUDDY_ASSET_SWEEP_GRACE`; in practice no more than the API's `SCADBUDDY_ASSET_MAX_TOTAL_BYTES` | 1 GB |
    | revision exports (`cache/`) | last use: the template revisions rendered within `SCADBUDDY_JOB_TTL` | measure |
    | fonts (`fonts/`) and library checkouts (`libraries/`) | nothing: the families and libraries the rendered templates name, kept for the pod's life | measure |
    | **sum; `sizeLimit` with slack** | 10 + 1.2 + 0.93 (1 GB) ≈ 12.1 GiB, plus the measured rows (~1 GiB on a typical worker) ≈ 13.1 GiB | **`14Gi`: ~0.9 GiB of slack** |

    Use your own render timings and piece sizes for the second row; a longer interval
    scales it linearly. Measure the last two rows with
    `du -sh /data/cache /data/fonts /data/libraries` on a running worker. A trim may
    not bring the cache down to its cap while crash-left staging directories are
    counted (follow-up #8), so watch `scadbuddy_worker_cache_bytes` against
    `SCADBUDDY_WORKER_CACHE_MAX_BYTES`.
  - Scale the Deployment freely.

  **Sweeps.** The API runs the store's sweep on its own `SCADBUDDY_ASSET_SWEEP_INTERVAL`.
  A piece or snapshot that no job references is deleted from the
  store (on `bambuddy`, from Bambuddy's library) once nothing has used it for
  `SCADBUDDY_ASSET_SWEEP_GRACE` (a week by default). The API's own upload sweep uses the
  same grace.

  `/healthz` on the API and on each worker (port 9090) carries a `store` object:
  - `multi_worker: true` says more than one replica is safe;
  - `backend` differs from `configured_backend` until the restart;
  - `render_key_fallback: true` means no render key is stored, so the workers hold the
    full Bambuddy key and template code can print. The Settings page shows the same
    warning.

  If a `bambuddy` store cannot start (no URL or folder), see "Recovering an unready
  blob store" below.

**Environment:**

| Variable | Default | What it does |
|---|---|---|
| `SCADBUDDY_STORE_BACKEND` | `local` | Seeds the stored **Blob store** setting. A value saved in Settings wins. |
| `SCADBUDDY_BAMBUDDY_RENDER_API_KEY` | none | Seeds the stored **Render key**. It is stored like `SCADBUDDY_BAMBUDDY_API_KEY` and never returned by the API. |
| `SCADBUDDY_STORE_MAX_TOTAL_BYTES` | 50 GiB | Past this, a new blob is refused (a re-put of one already stored never is). `0` is no limit. |
| `SCADBUDDY_STORE_MAX_COUNT` | 200000 | The same, counted in blobs. |
| `SCADBUDDY_WORKER_CACHE_MAX_BYTES` | 10 GiB | Each process's local piece cache on the `bambuddy` store. It is trimmed to this every `SCADBUDDY_ASSET_SWEEP_INTERVAL` (on a worker, every 300 s when that is `0`), not on write. |

The caps are checked, not reserved, so concurrent puts can overshoot them by one blob
each. **GET `/api/v1/store/usage`** and the Settings page's **Store** section show the
count and size against them.

**Metrics:**
- `scadbuddy_store_*`: `operations_total{op,outcome}`, `blobs`, `bytes{kind}`,
  `max_blobs`, `max_bytes` and `render_key_fallback`;
- `scadbuddy_worker_cache_*`: `total{result}` (hit or miss) and `bytes`, per process.

### Recovering an unready blob store

With `store_backend` set to `bambuddy`, the API and the render worker refuse to start
without a Bambuddy URL and a library folder (the store's inbox), so the Settings page
is out of reach. Settings no longer saves that state, but a stored `bambuddy` beats
`SCADBUDDY_STORE_BACKEND`. To start on the local store, run this in ScadBuddy's
database, in the schema `SCADBUDDY_DATABASE_URL` uses (the `settings` table is
unqualified: on a custom schema, `SET search_path` to it first), then set the URL and
folder in Settings and choose the Bambuddy store again:

```sql
UPDATE settings SET value = '"local"' WHERE name = 'store_backend';
```

When nothing is stored (the refusal comes from `SCADBUDDY_STORE_BACKEND=bambuddy`),
set `SCADBUDDY_STORE_BACKEND=local` instead.

### The agent sidecar (AI, #261)

The AI agent service in `agent/` ships as a **separate image**,
`ghcr.io/eh-homelab/scadbuddy-agent` (the Dockerfile's `--target agent`,
published by the `agent` job in `build-image.yml` with the same tags as the
backend image). It is meant to run as a **second container in the ScadBuddy
pod**, not inside the backend image: the sidecar layout chosen in §4.1 of the
AI design spec (`docs/superpowers/specs/2026-09-27-ai-integration-design.md`,
issue #250; on branch `claude/scad-buddy-ai-integration-pfn00c` until it
merges). The two containers share the pod network, so the agent reaches
the backend on `http://127.0.0.1:8080` (§4.3).

- It listens on port `8081` and answers `GET /healthz` (`agent/src/app.ts`).
- It reads only infrastructure variables (`agent/src/config.ts`; spec §9):
  `SCADBUDDY_DATABASE_URL`, `SCADBUDDY_BACKEND_URL` (default
  `http://127.0.0.1:8080`), `SCADBUDDY_SECRET_KEY_FILE`,
  `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`, `SCADBUDDY_PUBLIC_URL`,
  `SCADBUDDY_ALLOWED_ORIGINS`, `SCADBUDDY_AGENT_TRUSTED_PROXIES` and
  `SCADBUDDY_BROWSER_ALLOWED_ORIGINS`, each described below. With no database
  URL it still runs and `/healthz` reports `"ai": "disabled (no database)"`.
- **`SCADBUDDY_SECRET_KEY_FILE`** is the key-encryption key for the Claude
  credential, which is stored encrypted in the database (envelope encryption,
  spec §9; `agent/src/secrets.ts`). The file holds exactly 32 random bytes,
  base64-encoded; mount it from a Kubernetes Secret:

  ```bash
  openssl rand -base64 32 > scadbuddy-secret.key
  kubectl -n scadbuddy create secret generic scadbuddy-agent-kek \
    --from-file=secret.key=scadbuddy-secret.key
  # container: volumeMount at /etc/scadbuddy/kek, readOnly;
  # env SCADBUDDY_SECRET_KEY_FILE=/etc/scadbuddy/kek/secret.key
  ```

  It is read once at start. Without it (or with a malformed one) the service
  still runs, but saving a credential answers `503` naming the reason and
  `/healthz` reports `"ai": "disabled (no key-encryption key: …)"` (the
  reason names the variable, never the path; the log has the detail). Keep a
  copy: a credential sealed under a lost key cannot be decrypted and must be
  entered again (`/healthz` then says it was sealed with a different key).
- **Rotating the key** (spec §9, "re-wraps the data keys only"): create a new
  key file, mount it as `SCADBUDDY_SECRET_KEY_FILE`, mount the old one as
  **`SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`**, and restart. Once migrations have
  applied, every credential sealed under the old key has its data key
  re-sealed under the new one (the secret itself is not decrypted into a
  route or re-entered); the log says how many. Then remove the previous file
  and restart again. A row the old key cannot open is left as it is and
  counted in that log line.
- `/healthz` reports `"ai": "enabled"` only when the database answers, its
  `ai_*` migrations have applied (`agent/src/db/migrations/`, run at start
  under an advisory lock with a lock timeout, retried on the next call), the
  key is loaded and a Claude credential is saved. Otherwise `ai` names the
  first missing piece; each database step is bounded (2 s), so a stuck lock
  shows as `"unavailable (database timed out)"` instead of a hung probe. An
  edited, already-applied migration stops the service at start with a message
  naming it (each file's sha256 is recorded).
- The Claude credentials (Anthropic API keys, Claude Code OAuth tokens from
  `claude setup-token`, or gateway base URLs plus tokens) are set in
  Settings → Assistant → **Claude credentials**, a list in the order the
  assistant tries them (#1000, #1093). Each shows whether it is active, rate
  limited until a time, or disabled and why, and can be moved, tested, reset,
  given a new key or deleted. Underneath, and from a loopback shell when there
  is no UI (`docs/ai/operating.md`), they are `/api/v1/ai/credentials/entries`,
  tested with `POST …/entries/{id}/test` (one test at a time, at most
  one per 10 s; otherwise `429` with `Retry-After`). No route returns the
  secret. The sealed value is bound to its `kind` and `base_url`, so editing
  either in the database makes it fail to decrypt rather than send the token
  elsewhere.
- **Where writes may come from** (`agent/src/http/origins.ts`,
  `agent/src/routes/guard.ts`). Credential writes need both:
  - HTTPS: `X-Forwarded-Proto: https` from a peer in
    **`SCADBUDDY_AGENT_TRUSTED_PROXIES`** (comma-separated CIDRs, e.g. the
    ingress controller's pod range `10.42.0.0/16`), or a loopback peer.
    `X-Forwarded-*` from any other peer is ignored; unset, it is ignored from
    everyone.
  - The UI's origin: `Origin` and the request's host (`X-Forwarded-Host` from a
    trusted proxy, else `Host`) must both be the same origin from the list:
    the origin of **`SCADBUDDY_PUBLIC_URL`**, the same variable the backend
    reads for Bambuddy's sidebar link (set it to the `https://` URL users
    open), plus any in **`SCADBUDDY_ALLOWED_ORIGINS`** (comma-separated; the
    other hostnames the same deployment answers on, which the backend also
    reads for its realtime socket). Default ports are normalised. Unset, only
    `localhost`/`127.0.0.1`/`[::1]` with the matching Origin, from a loopback
    peer, is accepted. This is what stops DNS rebinding: an attacker's page
    re-pointed at the agent sends its own name in both `Host` and `Origin`,
    which is not on the list.
- **Where the headless browser may go** (`agent/src/harness/browserOrigins.ts`,
  [`docs/ai/headless-browser.md`](docs/ai/headless-browser.md)). It always opens the
  backend (`SCADBUDDY_BACKEND_URL`), and a URL on `SCADBUDDY_PUBLIC_URL` or
  `SCADBUDDY_ALLOWED_ORIGINS` is rewritten onto it. **`SCADBUDDY_BROWSER_ALLOWED_ORIGINS`**
  (comma-separated origins, or `*` for any) lets it open other origins too, each only
  after a human approves it once per session in the ScadBuddy UI. Unset, it opens
  nothing else. `*` plus that approval is the intended setting for full use; it also
  lets the model ask to open services on your LAN, so read the risks in that doc first.

  That is not authentication, and the human approval spec §8.2 asks for comes
  with #258.
- **Gateway base URLs** may be public, private (`10/8`, `172.16/12`,
  `192.168/16`, `fc00::/7`) or loopback, since a LiteLLM gateway on the LAN or
  in the cluster is the usual reason to use one. Link-local
  (`169.254.0.0/16`, `fe80::/10`) and cloud metadata hosts
  (`metadata.google.internal`, `100.100.100.200`, `fd00:ec2::254`, …) are
  refused, checked against every address the name resolves to, at save and
  again at test time (`agent/src/http/egress.ts`). Claude Code resolves the
  name again when it connects, so this narrows SSRF; an egress
  NetworkPolicy is the boundary.
- Plugins handed to the harness must not start processes of their own: command
  hooks, stdio MCP servers, LSP servers and monitors are refused
  (`agent/src/harness/plugins.ts`, spec §8.6), since they would inherit the
  credential's environment. The one exception is the headless browser (#349,
  [`docs/ai/headless-browser.md`](docs/ai/headless-browser.md)): the harness
  writes that plugin itself and starts its server under `env -i`. It is off
  until switched on in Settings ("AI headless browser", stored through
  `PUT /api/v1/ai/settings/headless-browser`, guarded like the credential
  writes), and the image carries its Chromium (about 600 MB of the image). The
  backend refuses its outward requests unless a human approved that exact one
  (`backend/scadbuddy/api/agent_actor.py`). A guard on every page refuses
  any redirect off `SCADBUDDY_BACKEND_URL`'s origin, a proxy's included.
  Chromium keeps its sandbox only where the pod's seccomp profile allows user
  namespaces (not `RuntimeDefault`); otherwise the agent warns at the first
  browser turn and runs it with `--no-sandbox`
  ([`docs/ai/headless-browser.md`](docs/ai/headless-browser.md), "Sandbox").
- **Plugin endpoints (#297)**: "provide an endpoint and we'll add it to the
  harness". A plugin is a remote MCP server, stored in Postgres (`ai_plugins`,
  no files) and managed through `/api/v1/ai/plugins` (below). The session
  manager takes enabled plugins for each turn (`remotePlugins`), as
  Streamable HTTP MCP servers named after the plugin, so their tools reach
  the model as `mcp__<name>__<tool>` (`main.ts` passes
  `forwardForRun(loadEnabledPlugins(…))` to the `SessionManager`; an outward
  plugin tool parks for approval like any other, #258). Sessions start from
  the assistant's chat socket and the session routes (next bullet). Rules (`agent/src/plugins/registry.ts`):
  - The URL must be `https://`; plain `http://` only when every address the
    host resolves to is loopback. Link-local and cloud metadata hosts are
    refused, including IPv6 forms that embed one (NAT64, 6to4, Teredo), as
    for gateway base URLs. It is checked at save, at test, and again each
    time a run loads the plugin. No query string, no credentials in the URL,
    no `$`.
  - **Claude Code never gets the plugin's URL or secret.** Each run registers
    its plugins with a loopback forwarder in the agent
    (`agent/src/plugins/forwarder.ts`) and hands Claude Code
    `http://127.0.0.1:<port>/p/<random token>`. The forwarder connects to the
    address the check passed (no second DNS lookup; TLS still verified
    against the hostname), follows no redirect, turns a 401 into a failure
    instead of starting OAuth discovery, and adds the auth header itself.
    This matters because Claude Code's own MCP client follows redirects and
    `WWW-Authenticate` `resource_metadata` URLs with the configured header.
    An egress NetworkPolicy on the pod is still the real boundary.
  - An optional auth header (name in the clear, value sealed with the same
    key-encryption key as the Claude credential and bound to the plugin's
    name, URL and header name). Changing the URL or the header name needs the
    value again. No route returns it; views show the header name and the last
    four characters.
  - **Every tool is `outward`, so it needs approval, until you set its tier.**
    `tool_tiers` sets tools to `read` or `write` (or `outward` explicitly).
    `disabled_tools` removes tools from the model's view entirely. MCP
    annotations such as `readOnlyHint` are only shown as a suggestion by the
    test; they never change a tier.
  - Claude Code renames every character outside `[A-Za-z0-9_-]` in a tool
    name to `_` (`files.list` becomes `mcp__<name>__files_list`). So only
    tools already named in that alphabet can take a tier; others stay
    `outward` (or disable them, by their real name). When two tools end up
    with the same name, both are hidden from the model, and the test marks
    them `collision`.
  - New plugins start disabled (`enabled: true` on create is refused). Run
    the test, review the tools, then enable.

  | Route | |
  |---|---|
  | `GET /api/v1/ai/plugins`, `GET …/{name}` | list, one |
  | `POST /api/v1/ai/plugins` | register (disabled): `name`, `url`, optional `auth_header` (default `Authorization`), `secret`, `tool_tiers`, `disabled_tools` |
  | `PATCH /api/v1/ai/plugins/{name}` | change any of those but `name`, and `enabled`; `secret: null` removes the header |
  | `DELETE /api/v1/ai/plugins/{name}` | remove |
  | `POST /api/v1/ai/plugins/{name}/test` | through the forwarder: connect, one `tools/list` (10 s timeout), and report each tool with its harness name and tier |

  Writes and the test go through the same guard as credential writes (next
  bullet). Reads are guarded too (`uiReadProblem`): HTTPS through the trusted
  proxy or loopback, addressed to the public origin (or loopback), `Origin`
  checked when present, and a cross-site `Sec-Fetch-Site` refused. A generic example against a loopback peer (a shell in the pod, or
  `kubectl port-forward … 8081`; the `Origin` must match the address used):

  ```bash
  curl -sS -X POST http://127.0.0.1:8081/api/v1/ai/plugins \
    -H 'Origin: http://127.0.0.1:8081' -H 'Content-Type: application/json' \
    -d '{"name": "memory", "url": "https://memory.internal.example/mcp/",
         "secret": "Bearer <token>"}'
  curl -sS -X POST http://127.0.0.1:8081/api/v1/ai/plugins/memory/test \
    -H 'Origin: http://127.0.0.1:8081'
  curl -sS -X PATCH http://127.0.0.1:8081/api/v1/ai/plugins/memory \
    -H 'Origin: http://127.0.0.1:8081' -H 'Content-Type: application/json' \
    -d '{"tool_tiers": {"search": "read"}, "enabled": true}'
  ```

  **Hindsight** (the motivating example, #297). Its docs give a per-bank MCP
  endpoint at `…/mcp/<bank_id>/`, transport `http`, an optional
  `Authorization: Bearer <api key>` header for Hindsight Cloud (none for a
  local Docker deployment), and the tools `retain`, `recall` and `reflect`
  among others ([MCP memory server](https://hindsight.vectorize.io/blog/2026/03/04/mcp-agent-memory)).
  Registered as a plugin that is `{"name": "hindsight", "url":
  "https://<hindsight host>/mcp/<bank_id>/", "secret": "Bearer <api key>"}`.
  Which of its tools to lower to `read` is your call after the test lists
  them; `recall` is the obvious candidate. Not yet verified against a running
  Hindsight: the tool names and annotations a real server lists, and whether
  `reflect` writes anything.
- **Sessions and the assistant's chat** (#300, #256; `agent/src/routes/chat.ts`,
  `agent/src/routes/sessions.ts`). The browser never holds a Claude credential:
  every model call is the agent's own, and every route below acts as the browser
  user behind the same guards as the credential routes.

  | Route | |
  |---|---|
  | `GET /api/v1/ai/status` | unguarded, like `/healthz`: `{available, state, ai, reason?}`, what the UI's gate reads |
  | `GET /api/v1/ai/chat` (WebSocket) | the assistant panel's protocol (`frontend/src/agent/chat/protocol.ts`) both ways: start or continue a chat, attach (replay then follow), approve or deny, interrupt, take over |
  | `GET/POST /api/v1/ai/sessions`, `GET …/{id}` | list, start (`{prompt?, title?}`; `429` past 10 new sessions a minute per owner, counted with the socket's), one |
  | `POST …/{id}/messages`, `…/interrupt`, `…/handoff` | send a turn (`{text}`; `409` while one runs), stop it, take the session over |
  | `GET …/{id}/events` | Server-Sent Events: the session's panel events from `Last-Event-ID` (a reconnect) or else `?after=`, then live |
  | `POST …/{id}/fork` | `{title?}` → `201 {session}`: a new session with the transcript so far and a budget of its own (the panel's "Continue in a new chat", #790); counted like a start (`429`). With the headless browser's agent-actor marker, or through the `sessions_fork` tool, the fork instead spends from the parent's budget: the parent and all its forks share one budget, and a spent one's fork is refused (`409`, #823) |
  | `POST …/{id}/budget` | `{add_usd}` (0.01–100): adds to the budget that session spends from (shared with its forks and its parent, #823), up to $100 in all. User-only and owner-only, refused with the headless browser's agent-actor marker, audited (#790) |
  | `GET/PUT /api/v1/ai/settings/session-limits` | `{budget_usd, max_turns}` (0.01–100 USD, 1–200 turns) for sessions started after a change; audited (#790) |

  A write body over `JSON_BODY_MAX` (about 251 KiB: the longest message in any
  script, fully JSON-escaped, plus 64 KiB; `agent/src/routes/guard.ts`) gets `413`
  before it is read; the socket caps a frame at 256 KiB. New sessions, from the
  socket or `POST`, are limited per owner (`MAX_NEW_SESSIONS` in
  `agent/src/sessions/manager.ts`, counted in `ai_sessions`, so reconnecting or
  another replica does not reset it); the socket answers an `error` frame with
  code `rate_limited`. Approvals
  and the agent's questions are answered through
  `POST /api/v1/ai/pending-input/{request_id}` (the panel's one respond route, #815);
  `/api/v1/ai/approvals` and the socket's `approval.decision` / `question.answer`
  still work. A chat
  session's model gets the ScadBuddy tools in-process (`mcp__scadbuddy__*`, at
  their tiers), plus enabled plugins. Every agent response carries
  `X-ScadBuddy-Service: agent`.
- It runs as uid 10001 and writes only under `/var/lib/scadbuddy-agent`
  (mount an `emptyDir` there) and `/tmp` (another `emptyDir`; Claude Code and
  Chromium use it), so the root filesystem can be read-only
  (spec §4.4; the CI smoke test runs it with `--read-only`). At start it
  recreates `claude/`, `work/` and `plugins/` in that volume, and it exits 1 with a
  message naming the directory if it cannot (`agent/src/harness/stateDirs.ts`).
- **Routing** (spec §4.2): the ingress sends `/api/v1/ai/*` and `/mcp` to the
  agent's port `8081`, ahead of the backend's `/`. That keeps the SPA, the
  backend, the agent and the assistant's WebSocket on one origin, which is what
  works inside Bambuddy's iframe. The rules, an example `Ingress` and a
  `curl` check per path (every agent response carries
  `X-ScadBuddy-Service: agent`) are in `docs/ai/operating.md` §1.1.
  `frontend/vite.config.ts` routes the same way for `pnpm dev` and
  `pnpm preview`. The clusters manifest is in eh-homelab/clusters, and until it
  deploys the sidecar the image's publish job is
  `continue-on-error`, so it cannot hold back a backend deploy, and the new
  GHCR package needs the same one-time **public** visibility step as
  `scadbuddy` (see the header of `build-image.yml`).

### Switching continuous deploy off

Disable the one workflow; nothing else changes:

```bash
gh workflow disable "Deploy main"   # merges to main stop deploying
gh workflow enable  "Deploy main"   # resume; the NEXT main build deploys
```

`Build Image` keeps publishing `:main` and `sha-*` images while it is off, and
releases keep deploying through `deploy-release.yml`. To deploy the current
head after re-enabling, re-run the latest `Build Image` run.

### Cutting a release

Release Drafter maintains a draft from merged PR titles. Publishing it creates
the `vX.Y.Z` tag; the tag push runs `Build Image` (which publishes `X.Y.Z` and
`X.Y`), and `deploy-release.yml` waits for that image, pins it, and writes the
deploy PR link into the release notes. There is no human step after
"Publish release".

### Reading a deploy

- **Which build is live:** `curl https://scadbuddy.internal.nullreference.io/healthz`
  reports `revision` (commit) and `version` — the same label the manifest
  pins (`X.Y.Z` for a release, `sha-<short>` for a main build), so the two
  should match the Deployment's annotations exactly.
- **What is pinned:** the annotations on the Deployments in
  `applications/scadbuddy/scadbuddy.yaml` and
  `applications/scadbuddy/scadbuddy-render.yaml`; one deploy pins both to the
  same digest.
- **Which dashboard is live:** the `?ref=` on the `deploy/grafana` line of
  clusters' `clusters/prod/scadbuddy/kustomization.yaml`, which should equal the
  `revision` annotation.
- **No ✅ within ~20 min of a merge/publish:** look at the clusters deploy PR
  first — a red required check there means the merge never happened and
  nothing reports until it does. Failed *verification* (merged, but the pod
  never served the revision) is reported with ❌ and fails
  `scadbuddy-verify-deploy.yml` on clusters `main`.

### Manual fallback

There should be no reason for one; but the mechanism is only a PR. Editing the
image line and annotations in the clusters manifests (both, once the render
worker's exists), and the dashboard line's `?ref=` (the full 40-character
revision), by hand and merging does exactly what the pipeline does. Do not
`kubectl rollout restart` — the pin is what makes the running image knowable.

### Tracing (#988)

The API, the render worker and the agent sidecar export OpenTelemetry traces over
OTLP/HTTP when `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT` is set (in the
cluster, the `alloy-receiver`; see eh-homelab/clusters#1596). Without one, nothing is
exported. `OTEL_TRACES_EXPORTER` may be unset or `otlp` (a comma list that includes
`otlp` counts); `none` turns export off, and any other value (`console`, `zipkin`, …)
also turns it off, with a warning in the log, since only the OTLP exporter ships.
`OTEL_EXPORTER_OTLP_TRACES_HEADERS`
and `OTEL_EXPORTER_OTLP_HEADERS` apply as the SDK defines. Only standard `OTEL_*`
variables apply: `OTEL_RESOURCE_ATTRIBUTES` (add `deployment.environment`),
`OTEL_TRACES_SAMPLER` (replaces the default, which drops parentless client spans:
database queries and Bambuddy calls from background loops; it keeps everything that
starts at a request, a workflow or a named span), and `OTEL_SDK_DISABLED=true`, the kill switch for an SDK
problem. Design: `docs/superpowers/specs/2026-10-01-distributed-tracing-design.md`.

The agent starts as `node --import ./dist/telemetry.js dist/main.js` (the image's
`CMD` and `pnpm start`): the import registers the ESM loader hook and the SDK before
the app loads. A chat turn is one trace; an approval ends the turn's spans when the
call parks, and the decision is a trace of its own linked to it
(`ai_approvals.traceparent`).

**Browser spans** reach the collector through the backend: the page posts OTLP/JSON to
`POST /telemetry/v1/traces` on ScadBuddy's own origin, and the relay
(`backend/scadbuddy/telemetry/`) forwards it in the background to
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (used as is) or else
`$OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces`, with `OTEL_EXPORTER_OTLP_TRACES_HEADERS` or else
`OTEL_EXPORTER_OTLP_HEADERS` sent on every post. It accepts only the UI's own origins (the
public URL, `SCADBUDDY_ALLOWED_ORIGINS` and loopback, as the realtime socket does; a `Sec-Fetch-Site` the browser sends must be
`same-origin`, so a page on another allowed origin is refused; a request without
`Sec-Fetch-Site`, from an older browser or a non-browser client, is admitted on `Origin`
alone), at
most 256 KiB and 512 spans a batch (and 16 `resourceSpans`, 64 `scopeSpans`), and rewrites every batch's resource to
`service.name=scadbuddy-web`. A page span's URLs keep no path of their own: each is
reduced to the backend route template its path matches, or to its origin (a relative
one on no route is dropped), as a server span keeps only its route; a URL on any host
but those same origins keeps only its origin, since that host has none of the routes.
No user agent and no `exception.message` is forwarded, wherever the page put it. Without an endpoint, or with `OTEL_TRACES_EXPORTER=none` or `OTEL_SDK_DISABLED=true`, it
answers `204` with `X-ScadBuddy-Tracing: off` (the browser side, the page stopping its
export, arrives with row 4 of #988). Its rate
limits are per pod (100 batches at once and 20 a second overall; 20 and 2 a second per
client), so with more than one API replica the overall ceiling multiplies.
**`SCADBUDDY_TRUSTED_PROXIES`** (comma-separated CIDRs, default empty) names the peers
whose `X-Forwarded-For` is believed, and then only its last value, as the agent's
`SCADBUDDY_AGENT_TRUSTED_PROXIES` does; set it to the gateway's range so each browser
gets a bucket of its own. Empty, every browser behind the gateway shares one. It is the
only trust decision: the image starts uvicorn with `--no-proxy-headers`, so uvicorn's own
`FORWARDED_ALLOW_IPS` (loopback by default) rewrites nothing; a custom command that drops
that flag lets any loopback caller name its own client.
`scadbuddy_trace_relay_batches_total{outcome}` counts `forwarded`, `failed`,
`queue_full` and `shutdown`; any rise in the last three means browser spans were lost.

The ScadBuddy dashboard (uid `scadbuddy`) is `deploy/grafana/`: a kustomize
directory whose `configMapGenerator` makes the ConfigMap `scadbuddy-dashboard`
in `cattle-dashboards`, labelled `grafana_dashboard: "1"`, which the
rancher-monitoring Grafana's sidecar loads. clusters' `clusters/prod/scadbuddy`
overlay will list it as a remote resource pinned to a full commit SHA, once
clusters#1596 Phase 5 adds the line, and the deploy
moves that pin with the image (above), so the dashboard shown is the one written
for the build that is serving. The overlay must namespace its own resources with
an `unsetOnly` NamespaceTransformer, not a plain `namespace:` field, or the
ConfigMap is moved out of `cattle-dashboards` and never loads (clusters#1596
Phase 5). Datasources are the variables `DS_PROMETHEUS` and `DS_TEMPO` (default
uid `tempo`); until clusters#1596 Phase 4 adds Tempo the trace tables are empty
and the metric panels are unaffected. CI's `lint` job checks the dashboard
(`.github/scripts/lint-dashboard.sh`): every series it reads must be declared in
`core/metrics.py`, and every span name must be one the service emits.

## Development

- `backend/` — FastAPI, `uv run --frozen pytest` (tests marked
  `requires_openscad` need a real `openscad`; the Dockerfile's `test` target
  is where they run in CI).
- `frontend/` — Vite + React, `pnpm test`, `pnpm build`.
- `agent/` — the AI agent service (Node 24, Hono, Claude Agent SDK),
  `pnpm test`, `pnpm build`; see "The agent sidecar" above.
- `models/` — bundled example models; `models/<name>/verify.sh` renders one
  against `openscad/openscad:dev` and checks the result.
- `backend/openapi.json` and the frontend and agent `src/api/schema.d.ts`
  are generated at build time and not committed: `pnpm gen:api` in either
  package exports the spec (`python -m scadbuddy.tools.export_openapi`) and
  writes the client. CI posts the API diff on each PR.
  `frontend/public/mockServiceWorker.js` is committed and checked against
  msw in CI (`pnpm exec msw init public --save`).

The base image is a dated OpenSCAD nightly pinned by tag and digest, and the
Dockerfile asserts the OpenSCAD version it was verified against
(`OPENSCAD_VERSION`). To move to a newer nightly, re-verify §3 of the design
spec against it, then change the tag, digest and `OPENSCAD_VERSION` in the same
commit.
