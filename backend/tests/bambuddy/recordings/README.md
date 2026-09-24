# Bambuddy recordings

Taken from the live Bambuddy **1.2.5.5** on the `primary` cluster on 2026-09-23, over
the pod's own loopback so no API key was involved and nothing was written:

```sh
POD=$(kubectl -n bambuddy get pod -l app.kubernetes.io/name=bambuddy -o jsonpath='{.items[0].metadata.name}')
kubectl -n bambuddy exec "$POD" -- curl -s http://localhost:8000/api/v1/printers/
```

Every request was a `GET`. Access codes are nulled and serials/IPs replaced.

| File | Source |
|---|---|
| `printers.json` | `GET /api/v1/printers/` |
| `printers-no-trailing-slash-404.json` | `GET /api/v1/printers` — **404**, see below |
| `library-folders.json` | `GET /api/v1/library/folders` |
| `slicer-pipelines.json` | `GET /api/v1/slicer-pipelines/` |
| `slicer-presets.json` | `GET /api/v1/slicer/presets`, truncated to 3 rows per tier |
| `external-links.json` | `GET /api/v1/external-links/` |
| `queue-item.json` | one row of `GET /api/v1/queue/` |
| `openapi/routes.txt` | every route of `GET /openapi.json`, unfiltered |
| `openapi/scadbuddy-routes.json` | the routes ScadBuddy calls, with their schemas |

Added for #85, over the ingress on 2026-09-23 (still every request a `GET`):

| File | Source |
|---|---|
| `printer.json` | `GET /api/v1/printers/1` |
| `printer-status.json` | `GET /api/v1/printers/1/status` |
| `available-filaments.json` | `GET /api/v1/printers/available-filaments?model=H2C` |
| `local-presets.json` | `GET /api/v1/local-presets/` |
| `projects.json` | `GET /api/v1/projects/` |
| `folders-by-project.json` | `GET /api/v1/library/folders/by-project/1` |
| `slicer-pipelines-configured.json` | `GET /api/v1/slicer-pipelines/`, now that one exists |
| `pipeline-runs.json` | `GET /api/v1/slicer-pipelines/1/runs` |

## What the recordings settle

- **`/api/v1/printers` 404s.** Only `/api/v1/printers/` exists. The design spec and the
  first `/settings/test` implementation both used the slashless form.
- **Ids are integers** everywhere — folders, files, printers, pipelines, links, jobs.
- **`printers/`, `library/folders` and `external-links/` answer with a bare list**, while
  `slicer-pipelines/` wraps its rows in `{"pipelines": [...]}`.
- **`bed_levelling` and `flow_cali` on `POST /queue/` are `"off" | "on" | "auto"`**, not
  booleans. `layer_inspect` and `timelapse` default to `false`, not `true`.
- **`POST /library/files/{id}/slice` spells the plate `plate`**; `POST /queue/` spells it
  `plate_id`.
- **A library file has no `url` field.** `FileUpdate` takes `filename`, `folder_id`,
  `project_id` and `notes`, and `notes` is the only free text on one — see
  "Where an Edit in ScadBuddy link can live" below.
- **An AMS `id` is the printer's numbering, not a list index.** The recorded H2C
  reports units `[0, 1, 128, 2]` — unsorted, with the single-slot AMS-HT at `128`,
  which is also how `ams_switch_inlet` keys them (as **strings**; JSON has no integer
  keys). `status.ams[2]` is the AMS-HT, not AMS 2. The external spool is not an AMS at
  all: it arrives on `vt_tray`, ids 254/255.
- **`remain: -1` means "unknown", not "empty"** — that is what an untagged spool reports.
- **`nozzle_diameter` is a string** (`"0.4"`) on `/printers/{id}/status`, while the queue
  route reports the same quantity back as a float.
- **`available-filaments` requires `model`** (a 422 without it, not "all printers"), and
  spells colours `#RRGGBBAA` *with* a leading `#` — the AMS tray they came from spells
  the same colour without one.
- **`local-presets` is not the `local` tier of `/slicer/presets`.** It is grouped by type
  rather than source, its ids are integers, and `compatible_printers` /
  `default_filament_colour` are JSON-encoded **strings** there, not lists.
- **`SlicerPipelineCreate` carries no target or fanout fields** even though
  `SlicerPipelineResponse` returns them, so a pipeline cannot be created pre-targeted.
- **`check-eligibility` answers 200 with the report**; only `run` turns the same report
  into a 409. Under `target_kind: "printer_class"` its `ok` means *at least one* printer
  passes, and the per-printer reasons are in `printer_reports`, not `issues`.
- **`POST /api/v1/library/folders/` needs the trailing slash**, while `folders()` reads
  the slashless `GET /api/v1/library/folders`. Both are real routes here, unlike
  `/printers`.
- **`DELETE /api/v1/library/files/{file_id}` exists** (`openapi/routes.txt`), which is
  what makes a replace-on-re-send possible.

## What could not be recorded

- **401 / 403 bodies.** This instance runs with authentication disabled — an absent and a
  bogus `X-API-Key` both return `200` through the Service, not just over loopback. The
  scope-mapping tests therefore drive `respx` with FastAPI's `{"detail": ...}` shape, which
  is what the recorded `404` body confirms Bambuddy uses.
- **Slice-job and enqueue responses.** Both need a `POST` against the live instance, which
  was out of bounds. `queue-item.json` is a real `PrintQueueItemResponse` read back from
  `GET /api/v1/queue/` — the same schema `POST /api/v1/queue/` returns.
- **A `PipelineRunResponse` with `jobs[]`, and an eligibility report.** Same reason: both
  need a `POST`. `pipeline-runs.json` is genuinely `{"runs": [], "total": 0}` — no run has
  ever been made against this pipeline — so the run-shape tests build their bodies from
  Bambuddy's `openapi.json` inline instead.
- **Which API-key scope guards `/slicer-pipelines/`.** This instance runs with
  authentication disabled, so a key's `can_*` flags are never consulted. The authoritative
  scope list is `APIKeyCreate` in Bambuddy's `openapi.json` (`can_read_status`,
  `can_manage_library`, `can_queue`, `can_manage_projects`, …); the pipeline routes are
  mapped to `Manage Queue` on the reasoning that running one queues prints, and that
  mapping is the one thing here that is inferred rather than measured.

## Where an "Edit in ScadBuddy" link can live (#80)

Measured against the live 1.x on 2026-09-23 — `GET /openapi.json` plus the deployed
web bundle, which is what says whether a field is *rendered*, not merely stored.

| Candidate | Verdict |
|---|---|
| Library file `notes` (`PUT /library/files/{id}`) | **Chosen.** The only per-file free text Bambuddy declares. One call, no new scope. |
| Library file `url` | Does not exist. `external_url` is on an **archive**, not a file. |
| Archive `external_url` / `PATCH /archives/{id}/project-page` | Unreachable from a send: an archive is created by Bambuddy *after* a print, so ScadBuddy has no archive id to write to. |
| 3MF root-model metadata (`Title`/`Description`/`Designer`) | Bambuddy's project page does read exactly these keys — but Bambu Studio **rewrites them on slice**. Archive 13's sliced 3MF carries `Title`, `Description`, `Designer` and `Origin` as empty strings. Kept for the file itself, not as the Bambuddy surface. |
| Folder README (`GET /library/folders/{id}/readme`) | Rendered as markdown with live anchors, but read-only over the API — the only write path is dropping a `.md` into the folder, which the library's upload may reject and which cannot be verified without writing to the live instance. Also folder-scoped, so concurrent sends would race on one file. |

Known limitation: the deployed web UI renders a library row's filename, tags and print
count, and only ever `PUT`s `{"filename": …}` — it does not render `notes` today. The
link is attached to the file in Bambuddy's own data model and readable over its API;
showing it as an **Edit in ScadBuddy** action is a change on the Bambuddy side.
