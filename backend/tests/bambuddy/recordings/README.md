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

Added for #87, over the ingress on 2026-09-24 (still every request a `GET`):

| File | Source |
|---|---|
| `inventory-spools.json` | `GET /api/v1/inventory/spools` |
| `inventory-assignments.json` | `GET /api/v1/inventory/assignments` |
| `inventory-remain.json` | `GET /api/v1/printers/1/inventory-remain` |
| `spool-filament-presets.json` | `GET /api/v1/inventory/spools/9/filament-presets` |
| `filament-requirements.json` | `GET /api/v1/library/files/62/filament-requirements` |

Added for #89 and #79, same day and the same way:

| File | Source |
|---|---|
| `library-folders-nested.json` | `GET /api/v1/library/folders`, re-read once a folder had children |

`tag_uid` and `tray_uuid` are replaced in the two inventory files: they are the RFID
identities of physical spools and nothing in ScadBuddy reads them.

Added for spool-first print (2026-09-27), over the ingress (all `GET`). AMS unit `serial_number` values, tray `tag_uid` (RFID identifier), and `tray_uuid` are redacted (see Step 1 command):

| File | Source |
|---|---|
| `archives.json` | `GET /api/v1/archives/?printer_id=1&limit=5` |
| `printer-status-rack.json` | `GET /api/v1/printers/1/status` |
| `slicer-presets-h2c.json` | `GET /api/v1/slicer/presets` |

Added for #469 on 2026-09-28, over the ingress (a `GET`, no auth). AMS and nozzle-rack
`serial_number`, tray `tag_uid` and `tray_uuid` are replaced with `REDACTED`:

| File | Source |
|---|---|
| `printer-status-fts.json` | `GET /api/v1/printers/1/status` — the Filament Track Switch fitted, right 0.2 HS00, left 0.4 HH01 |

- **With the switch fitted, `ams_extruder_map` is `{}`** and `ams_switch_inlet` names each
  AMS's inlet instead (`{"0":"B","1":"B","128":"A","2":"A"}`). Inlet A feeds the left
  extruder and B the right, and `nozzles[0]` is the right extruder, `nozzles[1]` the left.

## What the recordings settle

- **`/api/v1/printers` 404s.** Only `/api/v1/printers/` exists. The design spec and the
  first `/settings/test` implementation both used the slashless form.
- **Ids are integers** everywhere — folders, files, printers, pipelines, links, jobs.
- **`printers/`, `library/folders` and `external-links/` answer with a bare list.**
- **`bed_levelling` and `flow_cali` on `POST /queue/` are `"off" | "on" | "auto"`**, not
  booleans. `layer_inspect` and `timelapse` default to `false`, not `true`.
- **`POST /library/files/{id}/slice` spells the plate `plate`**; `POST /queue/` spells it
  `plate_id`.
- **A library file has no `url` field.** `FileUpdate` takes `filename`, `folder_id`,
  `project_id` and `notes`, and `notes` is the only free text on one — see
  "Where an Edit in ScadBuddy link can live" below.
- **`PUT /library/files/{id}` is a partial update**, so sending `notes` alone cannot
  clear the file's folder or project. This one could not be settled by reading — the
  schema shows every field as `anyOf [type, null]` with no `required` list, which is
  equally consistent with a full replace — and it could not be settled by measuring
  either, because that would mean writing to the live instance. It was settled from
  Bambuddy's **source**: `update_file` guards every assignment with
  `if data.<field> is not None`, and `FileUpdate` defaults each field to `None`
  ([library.py#L5103-L5136](https://github.com/maziggy/bambuddy/blob/9e9c08ba2cc08bf1e746ed98bef2b46b7bedea02/backend/app/api/routes/library.py#L5103-L5136)).
  Two details from the same lines: the sentinel that *clears* `folder_id`/`project_id`
  is **`0`**, not `null`, and an empty `notes` string is stored as `NULL`.
- **`notes` is still a whole-field write, and a person may have typed in it.** The
  partial update above is about the *other* fields; `notes` itself is replaced by
  whatever is sent. `GET /api/v1/library/files/{file_id}` (`openapi/routes.txt`) is
  therefore read first and the "Edit in ScadBuddy" line merged into what is there —
  replacing an earlier one of ours, keeping everything else.
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
- **`GET /api/v1/library/folders` answers with a tree, not a flat list.** A sub-folder
  arrives inside its parent's `children` rather than alongside it —
  `library-folders-nested.json` has `Supplies` carrying two. The older
  `library-folders.json` has no nested rows, which is why this went unnoticed: a scan of
  the top level alone reports a nested folder as missing.
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

## What could not be recorded

- **401 / 403 bodies.** This instance runs with authentication disabled — an absent and a
  bogus `X-API-Key` both return `200` through the Service, not just over loopback. The
  scope-mapping tests therefore drive `respx` with FastAPI's `{"detail": ...}` shape, which
  is what the recorded `404` body confirms Bambuddy uses.
- **Slice-job and enqueue responses.** Both need a `POST` against the live instance, which
  was out of bounds. `queue-item.json` is a real `PrintQueueItemResponse` read back from
  `GET /api/v1/queue/` — the same schema `POST /api/v1/queue/` returns.

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

Added for #307 on 2026-09-28, from Bambuddy **1.2.5.6** over the ingress (auth off,
every request a `GET`):

| File | Source |
|---|---|
| `archive-detail.json` | `GET /api/v1/archives/35` |
| `archive-runs.json` | `GET /api/v1/archives/35/runs` |
| `timelapse-info.json` | `GET /api/v1/archives/35/timelapse/info` |
| `timelapse-thumbnails.json` | `GET /api/v1/archives/35/timelapse/thumbnails`, cut to 3 frames of 64 base64 characters (the real ones are about 4.5 KB each) |
| `printer-media.json` | `GET /api/v1/archives/35/printer-media` |

`GET /archives/35/timelapse` and `/photos/{name}` with `Range: bytes=100-199` answered
`206` with `Content-Range: bytes 100-199/<size>` and `Accept-Ranges: bytes`: Bambuddy's
`FileResponse` serves ranges itself, so the proxy passes the header through.

Added for #313 on 2026-09-28, from Bambuddy 1.2.5.6 over the ingress (every request a
`GET` except the one probe slice below; `created_by_username` nulled):

| File | Source |
|---|---|
| `library-files-root.json` | `GET /api/v1/library/files/` (the root: `include_root` defaults to true) |
| `library-files-folder.json` | `GET /api/v1/library/files/?folder_id=4` (3MF, sliced 3MF and STL) |
| `library-plates-single.json` | `GET /api/v1/library/files/89/plates` |
| `library-plates-multi.json` | `GET /api/v1/library/files/67/plates` |
| `library-plates-stl.json` | `GET /api/v1/library/files/46/plates` |
| `filament-requirements-stl.json` | `GET /api/v1/library/files/46/filament-requirements` |

- **`GET /library/files/` answers a bare list of `FileListResponse`** and is filtered by
  `folder_id`; without one it lists the root only. There is no pagination.
- **`/library/files/{id}/plates` declares no schema** (its 200 is `{}`). The body is
  `{file_id, filename, plates: [{index, name, objects, object_count, has_thumbnail,
  thumbnail_url, print_time_seconds, filament_used_grams, filaments}], is_multi_plate,
  ...}`. An STL answers `plates: []`.
- **An STL's `filament-requirements` is `filaments: []`.**
- **Slicing a raw STL (#313 probe, the only write):** PASS: job 24 completed, sliced file
  177 left in the library. STL_PRINTABLE = yes.
