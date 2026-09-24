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

Added for #88 on 2026-09-24 (a `GET`):

| File | Source |
|---|---|
| `slicer-pipeline.json` | `GET /api/v1/slicer-pipelines/1` |

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

- **`POST /slicer-pipelines/{id}/run` cannot carry print options, and no amount of
  patching afterwards fixes that.** `PipelineRunCreateRequest` has exactly four fields
  (`source_library_file_id`, `source_archive_id`, `copies`, `force`). The route answers
  **202** and hands the work to a background task which, once slicing finishes, creates
  each copy's queue entry as a bare
  `PrintQueueItem(printer_id, target_model, library_file_id, status)` — Bambuddy's own
  model defaults, with nothing plumbed through from the request. `jobs[].queue_entry_id`
  is therefore still null when the 202 returns, so there is not even an id to
  `PATCH /api/v1/queue/{item_id}` (which in any case omits `quantity`, `insert_at_top`
  and `project_id`, all three of which `PrintQueueItemCreate` accepts). Read off
  `/app/backend/app/api/routes/pipeline_runs.py` in the running 1.2.5.5 pod on
  2026-09-24, not inferred from the spec. This is why a send carrying print options
  slices and queues itself from the pipeline's own presets instead of running it.
- **`GET /slicer-pipelines/{id}` returns the same shape as a row of the list route**,
  so one model covers both — including `target_kind`, `target_printer_id` and
  `target_model_class`, which is what lets a slice-and-queue send aim at whatever the
  pipeline aims at.

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
