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

Added for #87, over the ingress on 2026-09-24 (still every request a `GET`):

| File | Source |
|---|---|
| `inventory-spools.json` | `GET /api/v1/inventory/spools` |
| `inventory-assignments.json` | `GET /api/v1/inventory/assignments` |
| `inventory-remain.json` | `GET /api/v1/printers/1/inventory-remain` |
| `spool-filament-presets.json` | `GET /api/v1/inventory/spools/9/filament-presets` |
| `filament-requirements.json` | `GET /api/v1/library/files/62/filament-requirements` |

Added for #89, same day and the same way:

| File | Source |
|---|---|
| `pipeline-run.json` | `GET /api/v1/pipeline-runs/1` — a **real** run, and a failed one |

`tag_uid` and `tray_uuid` are replaced in the two inventory files: they are the RFID
identities of physical spools and nothing in ScadBuddy reads them.

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

- **Bambuddy computes `ams_mapping` itself, and ScadBuddy therefore sends none.**
  `print_scheduler.py`'s `_compute_ams_mapping_for_printer` is "called when a queue
  item has no ams_mapping set", and it resolves the tray against the printer the job is
  actually dispatching to — including the Filament Track Switch case, where a switcher
  routes any AMS slot to either extruder so the per-nozzle filter must not apply (its
  issue #2186). Recomputing the flat tray id here would be a second, worse copy.
- **`filament_overrides` and `required_filament_types` are fields of
  `PrintQueueItemCreate` and of nothing else.** `PipelineRunCreateRequest` carries only
  `source_library_file_id` / `source_archive_id` / `copies` / `force`. Naming the
  spools therefore forces the slice + `POST /queue/` route.
- **A `filament_overrides` entry is `{slot_id, type, color, used_grams}`**, optionally
  with `force_color_match`. That is the shape Bambuddy's own 3MF parser produces
  (`services/filament_requirements.py`) and the one its scheduler validates.
- **A spool row's `nozzle_temp_min` / `nozzle_temp_max` are null** on every row of this
  instance. The real window is on the AMS tray the spool is loaded in. Nothing in
  ScadBuddy reads either any more — whether two filaments can share a plate is
  Bambuddy's eligibility question — but the asymmetry is worth keeping recorded.
- **`GET /library/files/{id}/filament-requirements` works on an unsliced 3MF** and
  answers `used_grams: 0` for every slot — *unknown*, not zero. `type` comes back `""`
  on one too, so the material is unknown as well.
- **`GET /inventory/spools/{id}/filament-presets` is keyed by printer model *and*
  nozzle diameter**: one spool maps to `GFSG00_24` through a 0.2 and `GFSG00_23`
  through a 0.4. ScadBuddy does **not** call it: choosing among them needs a nozzle
  diameter, which lives nowhere but a process preset's *name*, and guessing one is
  exactly the kind of decision that belongs to Bambuddy. The spool row's own
  `slicer_filament` is used for the slice instead. The recording stays because the
  keying is the reason the shortcut is safe to take.
- **`/inventory/assignments` is unfiltered across every printer** while
  `/printers/{id}/inventory-remain` covers one. Joining them on `(ams_id, tray_id)`
  alone gives a spool in printer B's AMS 0 slot 1 printer A's remaining weight.
- **A failed pipeline run keeps reporting `status: "in_progress"`.** `pipeline-run.json`
  is a real run whose slice failed: `status: "in_progress"`, `copies_in_progress: 1`,
  `copies_failed: 0` — *and* `completed_at` set, `sliced_library_file_id: null` and
  `error_message: "Slice failed: The selected printer is not compatible with the process
  preset in the 3mf."`. Neither the status nor the copy counters ever move, so a poll
  built on either never terminates. **`completed_at` is the terminal signal.**
- **`GET /api/v1/pipeline-runs/{run_id}` exists** — a single-run read that needs no
  pipeline id, which is what makes following a recorded run id possible. The
  `/slicer-pipelines/{id}/runs` list is not needed for it.
- **`jobs[].queue_entry_id` is null until the background task has created the entry.**
  The recorded run's one job is still `status: "pending"` with no printer and no queue
  entry, minutes after the run finished — because nothing was sliced to queue.
- **`/api/v1/inventory/locations` is empty here**, so storage locations come back as the
  free-text `storage_location` on the spool rather than as a location id.
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
