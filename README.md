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
  saved preset, to an editable preset of your own. Saved presets are kept in the
  database.
- **The preview is the real render**: OpenSCAD (Manifold) runs on every parameter
  change and shows per-colour parts and the bounding box.
- **Multi-colour 3MF**: one closed solid per colour, each on its own extruder, with
  plate cover images and a layout sized for the target printer's plate.
- **Send to Bambuddy**: upload to a library folder, or slice and queue it.
- **Print picker**: spool-first — pick spools, nozzle size, a quality tier and a plate,
  and ScadBuddy derives the printer, process and filament presets and slices and queues
  through Bambuddy. No slicer pipeline to pick or maintain; Advanced mode adds per-side
  nozzle flow, the full process list and a per-slot filament preset override. Also lets
  you set copies, a project, and print options.
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
  ghcr.io/eh-homelab/scadbuddy:main
```

**A PostgreSQL database is required** (#401): without `SCADBUDDY_DATABASE_URL`
the backend refuses to start and says so. Settings and the render queue live
there; the schema is created and migrated at startup, so an empty database is
enough.

Then open `http://<host>:8080`, go to **Settings** and connect Bambuddy (see
[Connecting Bambuddy](docs/user-guide.md#connecting-bambuddy): the API key needs
**Manage Library**, **Manage Queue** and **Read Status**, plus **Manage Projects**
for the project picker).

**Renders on Temporal** (#424) are behind `SCADBUDDY_TEMPORAL_ADDRESS` (the
Temporal frontend's `host:port`). Empty, renders run on the legacy in-process
queue described below. Set, they run on Temporal: the API submits and a separate
render worker (see "Render worker" under Deploying) runs them. Temporal is
optional until #546 makes the address required and removes the legacy queue. For
a one-process dev run, let the API host the worker itself:

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
and `SCADBUDDY_TEMPORAL_TASK_QUEUE_RENDER` to `render`.
`SCADBUDDY_TEMPORAL_WORKER_INPROCESS` is for dev and tests only; it does not drain
on shutdown.
Drain renders, and stop the worker Deployment, before flipping
`SCADBUDDY_TEMPORAL_ADDRESS` either way: a restart across the flip fails the renders
in flight (a pending one carries over).

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
- **Environment** (all optional but `SCADBUDDY_DATABASE_URL`):
  `SCADBUDDY_BAMBUDDY_URL`, `SCADBUDDY_BAMBUDDY_API_KEY`, `SCADBUDDY_PUBLIC_URL`,
  `SCADBUDDY_DEFAULT_PLATE` and `SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES` (default
  1073741824, 1 GiB) set the starting values for Settings. Once a value is saved
  from the UI it wins; a field the UI never saved keeps following the variable,
  and one it cleared stays cleared (the upload limit instead goes back to the
  variable). `SCADBUDDY_GOOGLE_FONTS_API_KEY`; `SCADBUDDY_RENDER_TIMEOUT`
  (default 120 s), `SCADBUDDY_RENDER_CONCURRENCY` (2),
  `SCADBUDDY_SOLID_CONCURRENCY` (0 = derived; see below),
  `SCADBUDDY_CHECK_CONCURRENCY` (1), `SCADBUDDY_LSP_SESSIONS` (4);
  `SCADBUDDY_REALTIME_SOCKETS` (256, the most open realtime sockets, one per tab);
  `SCADBUDDY_PREVIEW_RENDERS` (default `true`: a model with no thumbnail and no
  generated output is rendered at its default settings in the background, one at
  a time and behind any render someone asked for, and that plate image is its
  catalogue thumbnail; `false` renders nothing, and such a model shows no image
  until one is set or generated. The previews are kept in the
  `SCADBUDDY_DATABASE_URL` database's `model_previews` table);
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
    nothing has uploaded or used it for this long.
  - `SCADBUDDY_ASSET_SWEEP_INTERVAL` (default 86400 s): how often that sweep runs
    after the one at startup; 0 turns it off.
  - The same periodic sweep also clears old duplicate staging
    (`SCADBUDDY_DUPLICATE_STAGING_MAX_AGE`), so 0 leaves that to startup and the
    next duplicate.
  - Settings shows the usage under "Uploaded files"; so do
    `GET /api/v1/assets/usage` and the `scadbuddy_assets_*` metrics.
- **Template media** (images and videos, in `/data/models/<slug>/media`):
  `SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES` (default 1073741824, 1 GiB) is the largest
  single upload. It is set only here (no Settings override); `GET /api/v1/settings`
  reports it read-only as `media_upload_max_bytes`.
  The upload is streamed to the data volume, never held in memory. Images (and
  posters) are also capped at 10 MiB, since they are committed to the models'
  history; videos are not committed.
- **Render queue.** By default every render request is accepted;
  `SCADBUDDY_RENDER_CONCURRENCY` jobs are rendered at once per process, oldest
  first. A preview replaced before it started is dropped, and identical waiting
  requests share one job. A finished render is kept under its template
  (`models/<slug>/.renders/<key>/`, beside the source like its media) and a
  later request for the same parameters at the same revision is answered from it
  without running OpenSCAD; an entry with a file missing is rendered again, and
  entries unused for `SCADBUDDY_JOB_TTL` are removed with the jobs. Installing a
  font or moving a library pin does not change the key, so a render kept before
  that is served until it expires or the template is edited.
  - `SCADBUDDY_RENDER_QUEUE_MAX` (0 = no limit): set, a request that would be a new
    job while that many already wait gets 503 with `Retry-After`. A request that
    supersedes a waiting preview, or matches one, is never refused.
  - `SCADBUDDY_DATABASE_URL` (libpq URL, required): the queue is in Postgres, so
    accepted renders survive a restart. Several replicas can share one queue only
    if they also share `/data` (a ReadWriteMany volume): a job's files are written
    there by whichever replica renders it. On a ReadWriteOnce PVC run one replica,
    as the design does. `SCADBUDDY_DATABASE_POOL_SIZE` (10, per pool: the queue
    and the settings each hold one). The schema is created and migrated at
    startup.
  - The **event bus** (spec §7) is in the same Postgres database (the backend
    will not start without `SCADBUDDY_DATABASE_URL`, #467): each change is appended to an `events` table and sent with
    `NOTIFY scadbuddy_events` in one transaction, and every replica's subscribers
    hear it once over the same `LISTEN` connection the job store uses. After
    that connection drops and comes back, subscribers get a `bus.resync` event.
    The table keeps events for `Last-Event-ID` replay:
    `SCADBUDDY_EVENT_LOG_RETENTION_SECONDS` (86400) and
    `SCADBUDDY_EVENT_LOG_RETENTION_ROWS` (100000), 0 for no limit, pruned by every
    replica every 5 minutes.
    `scadbuddy_events_published_total`, `scadbuddy_events_dropped_total{reason}`,
    `scadbuddy_events_received_total`, `scadbuddy_events_resyncs_total` and
    `scadbuddy_event_log_pruned_total` show it working. NOTIFY channels are per
    database, so give each deployment its own database, not just its own schema.
  - `SCADBUDDY_RENDER_QUEUE_TIMEOUT` (0 = never): fail a render that waited longer
    than this for a worker, unrendered.
  - `SCADBUDDY_RENDER_POLL_INTERVAL` (1 s): how often an idle worker checks for
    jobs it was not woken for, only while the listener below is disconnected.
  - `SCADBUDDY_RENDER_FALLBACK_POLL_INTERVAL` (30 s), Postgres only: each process
    `LISTEN`s on `scadbuddy_render_queue`, and a submit or requeue on any replica
    sends `NOTIFY` in the same transaction, so an idle worker starts the job at
    once. While that connection is up, idle workers still check this often, to
    catch a notification missed around a reconnect. The listener reconnects with
    back-off; `scadbuddy_render_queue_listener_connected` and
    `scadbuddy_render_queue_listener_reconnects_total` show its state.
  - `SCADBUDDY_RENDER_LEASE_TIMEOUT` (60 s) and `SCADBUDDY_RENDER_MAX_ATTEMPTS` (2),
    Postgres only: a running job whose worker stops heartbeating for a lease is
    requeued, and failed after its last attempt.
  - `SCADBUDDY_RENDER_QUEUE_DEPTH_SLO` (16) and `SCADBUDDY_RENDER_LATENCY_SLO`
    (60 s): targets, not limits. They are exported with the metrics for alerts.
- `GET /healthz` reports the OpenSCAD version, whether the data directory is
  writable, and the build revision. On the Temporal path it also has a `temporal`
  object (`address`, `namespace`, `task_queue`, `worker_inprocess`); on the legacy
  queue that key is absent.
- `GET /metrics` serves Prometheus metrics: render queue depth and oldest wait
  (read from the store in Postgres, so across replicas), wait time and latency
  (`scadbuddy_render_job_latency_seconds`, by outcome), per-stage render time, whether
  the queue's store can be read (`scadbuddy_render_store_up`), the
  SLO targets, the upload store's files and bytes against its caps
  (`scadbuddy_assets_*`), and HTTP requests by route. It is unauthenticated, like the rest of
  the app.

**Realtime.** The UI follows changes over `WS /api/v1/ws`, served by the
backend (spec §4.2, #266). A browser's `Origin` must be the stored public URL's
origin (Settings, seeded from `SCADBUDDY_PUBLIC_URL`) or a loopback origin;
anything else is refused, which stops DNS rebinding. If the socket can't
connect, the header shows "Live updates unavailable" and views poll instead.

## Deploying

ScadBuddy runs on the homelab cluster from
[eh-homelab/clusters](https://github.com/eh-homelab/clusters)
(`applications/scadbuddy/scadbuddy.yaml`, deployed by ArgoCD). That manifest
pins the image **by digest**; this repo's workflows are what move the pin.
Nothing here talks to the cluster.

The backend needs its database (#401): the manifest must set
`SCADBUDDY_DATABASE_URL` (the cluster's `scadbuddy-db`), or the pod never
becomes ready and its log names the missing variable. The settings live in that
database, so the Bambuddy connection a deployment needs from the first start
comes from `SCADBUDDY_BAMBUDDY_URL`, `SCADBUDDY_BAMBUDDY_API_KEY` (from a Secret)
and `SCADBUDDY_PUBLIC_URL`.

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
   annotations (`version`, `revision`, `source`) in the manifest;
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

With `SCADBUDDY_TEMPORAL_ADDRESS` set, renders run on a separate worker. It is the
**same image** run as `python -m scadbuddy.worker`, and it serves `/healthz`
(`{"ok": true, "build_id": …, "task_queue": …}`) and `/metrics` on port **9090**.
Probe that port: the image's `HEALTHCHECK` is the API's 8080.

- **One replica in phase 1**, sharing `/data` with the API. A rendered piece is
  written to `/data/blobs/<piece_key>/` and read back by the API, so both pods mount
  the same volume. A ReadWriteOnce volume is fine as long as both pods run on the
  same node (pod affinity); a `ReadWriteOncePod` volume is not, because only one pod
  may mount it. A piece no job references any more is removed by the API's periodic
  upload sweep once it has gone `SCADBUDDY_JOB_TTL` untouched, the same retention a
  render has on the legacy queue.
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
  receives new workflows once it is polling.
- **Shutdown:** SIGTERM (tini forwards it; no `preStop` needed) starts the
  drain. The worker keeps polling until no workflow pinned to its build is
  running, for at most `2 × (SCADBUDDY_RENDER_TIMEOUT + 60) + 120` s. Then the
  SDK gives in-flight activities up to `SCADBUDDY_RENDER_TIMEOUT + 60` s. If the
  drain's bound passes first, the worker exits anyway (it logs "drain timed out"):
  workflows still pinned to its build then have no poller, and their jobs stay
  `running` until that build polls again. Set `terminationGracePeriodSeconds` ≥
  `3 × (SCADBUDDY_RENDER_TIMEOUT + 60) + 120` plus a little slack for teardown
  (e.g. 30 s): **690 s** at the default 120 s timeout. While a single replica drains
  it is still current, so new renders keep landing on it; a rollout that starts
  the new pod first (surge) lets it drain promptly.
- **Temporal itself** comes from the Temporal operator with a CNPG Postgres in
  `eh-homelab/clusters` (clusters#1454). `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` (the
  API hosting the worker) is for dev and tests only.

### Blob store and render workers (#426)

Everything a render reads or writes (rendered pieces, template snapshots, uploaded SVGs
and PNGs, downloaded fonts) lives in the blob store. Settings → **Blob store** picks
where. The choice is read at start, so it takes effect only when the API and the
workers restart.

- **`local`** (the default): this server's volume, phase 1's topology. There is **one**
  render worker, and it shares the API's `/data` volume, as described under "Render
  worker (#424)" above.
- **`bambuddy`**: Bambuddy's library. Files go to `<Library folder>/<Template>/Work/`,
  and ScadBuddy deletes only inside a `Work/` folder. To switch:
  1. Set Bambuddy's URL and a **Library folder** (the store's inbox) in Settings.
  2. In Bambuddy, create a key with *Manage Library* only, and paste it into Settings as
     **Render key**.
  3. Choose **Bambuddy library** under **Blob store**, and restart the API and the
     workers.

  The workers then need no shared volume:
  - give each one an `emptyDir` at `/data`, which holds its piece cache, bounded by
    `SCADBUDDY_WORKER_CACHE_MAX_BYTES` and evicted least recently used every
    `SCADBUDDY_ASSET_SWEEP_INTERVAL`;
  - scale the Deployment freely.

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
| `SCADBUDDY_WORKER_CACHE_MAX_BYTES` | 10 GiB | Each process's local piece cache on the `bambuddy` store. |

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
database, then set the URL and folder in Settings and choose the Bambuddy store again:

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
  `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`, `SCADBUDDY_PUBLIC_URL` and
  `SCADBUDDY_AGENT_TRUSTED_PROXIES`, each described below. With no database
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
- The Claude credential (an Anthropic API key, or a gateway base URL plus
  token) is managed through `GET/PUT/DELETE /api/v1/ai/credentials` and
  tested with `POST /api/v1/ai/credentials/test` (one test at a time, at most
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
    trusted proxy, else `Host`) must both be the origin of
    **`SCADBUDDY_PUBLIC_URL`**, the same variable the backend reads for
    Bambuddy's sidebar link (set it to the `https://` URL users open). Default
    ports are normalised. Unset, only `localhost`/`127.0.0.1`/`[::1]` with the
    matching Origin, from a loopback peer, is accepted. This is what stops DNS
    rebinding: an attacker's page re-pointed at the agent sends its own name
    in both `Host` and `Origin`, which is not on the list.

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
  credential's environment.
- **Plugin endpoints (#297)**: "provide an endpoint and we'll add it to the
  harness". A plugin is a remote MCP server, stored in Postgres (`ai_plugins`,
  no files) and managed through `/api/v1/ai/plugins` (below). The session
  manager takes enabled plugins for each turn (`remotePlugins`), as
  Streamable HTTP MCP servers named after the plugin, so their tools reach
  the model as `mcp__<name>__<tool>` (`main.ts` passes
  `forwardForRun(loadEnabledPlugins(…))` to the `SessionManager`; an outward
  plugin tool parks for approval like any other, #258). Nothing starts a
  session over HTTP yet, so for now the connection test is what reaches a
  plugin. Rules (`agent/src/plugins/registry.ts`):
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
- It runs as uid 10001 and writes only under `/var/lib/scadbuddy-agent`
  (mount an `emptyDir` there), so the root filesystem can be read-only
  (spec §4.4; the CI smoke test runs it with `--read-only`). At start it
  recreates `claude/` and `work/` in that volume, and it exits 1 with a
  message naming the directory if it cannot (`agent/src/harness/stateDirs.ts`).
- Nothing deploys it yet. The clusters manifest, and the ingress routes for
  `/mcp` and `/api/v1/ai/*` (spec §4.2), come with the stories
  that give it routes. Until then the image's publish job is
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
- **What is pinned:** the annotations on the Deployment in
  `applications/scadbuddy/scadbuddy.yaml`.
- **No ✅ within ~20 min of a merge/publish:** look at the clusters deploy PR
  first — a red required check there means the merge never happened and
  nothing reports until it does. Failed *verification* (merged, but the pod
  never served the revision) is reported with ❌ and fails
  `scadbuddy-verify-deploy.yml` on clusters `main`.

### Manual fallback

There should be no reason for one; but the mechanism is only a PR. Editing the
image line and annotations in the clusters manifest by hand and merging does
exactly what the pipeline does. Do not `kubectl rollout restart` — the pin is
what makes the running image knowable.

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
