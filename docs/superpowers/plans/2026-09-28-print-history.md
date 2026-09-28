# Print history (epic #305): plan and cross-issue contracts

Children: #306 (link), #307 (archive client and proxy), #308 (prints API), #309
(attachments), #310 (history UI), #311 (detail page).

This plan fixes the contracts the children share, so they can be built in parallel
without renegotiating them. Every Bambuddy claim cites Bambuddy's source at
**v1.2.5.6, commit `14da007`** (`github.com/maziggy/bambuddy`), as `path:line`
relative to its repository root.

## 1. Verified Bambuddy facts

### Authentication and scopes

| # | Fact | Source |
|---|---|---|
| A1 | An API key in `X-API-Key` is enough for every archive route. The media routes (`/thumbnail`, `/timelapse`, `/photos/{f}`, `/plate-thumbnail/{i}`) use `require_media_token_ownership`, which hands any header caller to `require_ownership_permission` unchanged. A `?token=` media token is needed only by a caller with no header, such as an `<img>` pointed straight at Bambuddy, and ScadBuddy never does that. | `backend/app/core/auth.py:2416-2456`; routes at `backend/app/api/routes/archives.py:2352`, `:2588`, `:3310`, `:4287` |
| A2 | Archive reads (detail, list, runs, download, source, timelapse and its info and thumbnails, photos, printer-media) need `ARCHIVES_READ_ALL`, which maps to the key scope **`can_read_status`**. | `backend/app/core/auth.py:87`; `archives.py:395`, `:1610`, `:1667`, `:2230`, `:2391`, `:2588`, `:3072`, `:3106`, `:3310`, `:4888` |
| A3 | Photo upload needs `ARCHIVES_UPDATE_ALL`, and photo delete needs `ARCHIVES_DELETE_ALL`. Both map to **`can_manage_archives`**. | `archives.py:3270`, `:3368`; `auth.py:194`, `:196` |
| A4 | `POST /archives/{id}/timelapse/select?filename=` needs `ARCHIVES_UPDATE_ALL`, which maps to **`can_manage_archives`**. | `archives.py:2924-2928`; `auth.py:194` |
| A5 | `GET /archives/{id}/printer-media` lists the files on the printer only when the key also has `PRINTERS_FILES`, which maps to **`can_control_printer`**. Otherwise it returns only the local timelapse and adds `"printer_files_forbidden"` to `warnings`. The response is `{archive_id, printer_id, local_timelapse, remote_files[], warnings[]}`. | `archives.py:2400`, `:2431-2440`; `auth.py:140` |
| A6 | Bambuddy's settings UI calls the flag "Manage Archives". It defaults to on for a new key. | `frontend/src/i18n/locales/en.ts:2270`; `backend/app/schemas/api_key.py:18` |
| A7 | Library upload needs `LIBRARY_UPLOAD` (**`can_manage_library`**), and so does folder create. Library download needs `LIBRARY_READ_ALL` (**`can_read_status`**). | `library.py:2238-2245`, `:1105-1110`, `:5202-5208`; `auth.py:93`, `:154` |

### Linking a print to its archive

| # | Fact | Source |
|---|---|---|
| L1 | The scheduler creates the archive when it dispatches a library-file queue item, and sets `item.archive_id = archive.id` in the same step. The archive also gets `library_file_id` (#1897) and `plate_id`. | `backend/app/services/print_scheduler.py:6414-6435` |
| L2 | A queue item **survives** its print. It goes away only through a user's `DELETE /queue/{id}`, deletion of the archive, or deletion of the user who owns it. The "entry is gone by the first poll" case in #306 is therefore the exception, not the rule. | `backend/app/api/routes/print_queue.py:2110`; `backend/app/services/archive.py:1014`; `backend/app/api/routes/users.py:450` |
| L3 | `PrintQueueItemResponse.archive_id` is `None` until dispatch. `GET /queue/{id}` returns the item. | `backend/app/schemas/print_queue.py:191`; `print_queue.py:1849` |
| L4 | A pipeline run's jobs carry `queue_entry_id`, not an archive id. The pipeline route reaches its archive through the queue item. | `backend/app/schemas/pipeline_run.py:107-114` |
| L5 | `ArchiveResponse` does **not** expose `library_file_id`, even though the model has it. It does expose `content_hash`. So `library_file_id` cannot be used as a matcher through the API. | `backend/app/schemas/archive.py:52-60`; `backend/app/models/archive.py:14-20`, `:29` |
| L6 | The archive's `content_hash` is the SHA-256 of a byte-for-byte copy of the dispatched file. A sliced library file's `file_hash` is the SHA-256 of the same bytes. So `archive.content_hash == sliced.file_hash` identifies the print of that slice. `GET /library/files/{id}` returns `file_hash`. | `archive.py:33-48`, `:1054-1061`, `:1317`; `library.py:4610`; `backend/app/schemas/library.py:151` |
| L7 | `GET /archives/` filters only by `printer_id`, `project_id`, `date_from`, `date_to`, `limit` and `offset`. There is no hash filter, so the fallback matcher scans a printer's archives within a date window. | `archives.py:395-402` |

### Media and files

| # | Fact | Source |
|---|---|---|
| M1 | `GET /archives/{id}/timelapse` returns a Starlette `FileResponse` (`video/mp4`, `.avi` or `.mkv`, with an `ETag` of the mtime). Bambuddy requires `starlette>=1.3.1`, whose `FileResponse` answers `Range` with `206` and `Content-Range`. | `archives.py:2588-2630`; `requirements.txt:147`; Starlette `responses.py:362-405` (1.6.0, which ScadBuddy has installed) |
| M2 | Photos are served as `FileResponse`, and only for names listed in `archive.photos`. The finish photo is one of them. | `archives.py:3310-3358` |
| M3 | Photo upload accepts only `.jpg`, `.jpeg`, `.png` and `.webp`, names the file `uuid[:8]+ext`, and returns `{status, filename, photos}`. It reads the whole upload into memory. | `archives.py:3280`, `:3296-3308` |
| M4 | `GET /archives/{id}/timelapse/info` returns `{duration, width, height, fps, codec, file_size, has_audio}`. `.../timelapse/thumbnails` returns `{thumbnails: [base64 JPEG], timestamps}`: they are inline data, not URLs. | `backend/app/schemas/timelapse.py:6-22`; `archives.py:3072-3140` |
| M5 | `GET /archives/{id}/runs` returns `{items: [PrintLogEntry], total}`. Each entry has `status`, `started_at`, `completed_at`, `duration_seconds`, `filament_used_grams`, `cost` and `failure_reason`. | `archives.py:1667`; `backend/app/schemas/print_log.py:6-39` |
| M6 | **`POST /archives/{id}/reprint` is gone (410).** "Print again" is `POST /queue/` with `archive_id`, and needs `QUEUE_CREATE`, which maps to `can_queue`. | `archives.py:4651-4673`; `print_queue.py:800`; `backend/app/schemas/print_queue.py:77`; `auth.py:130` |
| M7 | Library upload has no file-type allowlist. `validate_print_file_upload` rejects only raw `.gcode` and non-zip `.3mf`, so an `.mp4` or `.webm` is accepted. The upload is **read whole into memory**. Library download is a `FileResponse`, so it serves Range. | `library.py:236-280`, `:2284-2286`, `:5222` |
| M8 | Folder create takes `{name, parent_id, project_id, archive_id}`. | `backend/app/schemas/library.py` `FolderCreate`; `library.py:1105` |

## 2. Contracts

### 2.1 Link: output → archive (#306)

- A new **Postgres** table, `output_bambuddy_prints`:

  | Column | Type |
  |---|---|
  | `output_id` | text |
  | `archive_id` | bigint |
  | `queue_item_id` | bigint null |
  | `plate_id` | int null |
  | `printer_id` | bigint null |
  | `matched_by` | text: `'queue_item'` or `'content_hash'` |
  | `first_seen` | timestamptz default now() |

  The primary key is `(output_id, archive_id)`, with an index on `archive_id` (the prints API joins by it). Deleting an output or a model deletes its rows, as #462 does for uploads.
- **Primary matcher (L1–L3).** Record the item's `archive_id` wherever ScadBuddy already reads a queue item:
  - on the slice-queue route, `meta.plates[*].queue_item_id`;
  - on the pipeline route, `jobs[*].queue_entry_id` → `GET /queue/{id}` (L4);
  - in the progress read (#89, and #430's watcher once merged) and the project attach.

  `matched_by='queue_item'`.
- **Fallback matcher (L5–L7).** Use it for a queue item that 404s before an archive was seen. Take the output's recorded slices (#462 `output_bambuddy_slices`), and store each slice's `file_hash` when it is first seen. #306 adds a nullable `file_hash text` column to `output_bambuddy_slices`, filled from `GET /library/files/{sliced_id}`. Then scan `GET /archives/?printer_id=&date_from=&date_to=` around the send and match `content_hash == file_hash`. `matched_by='content_hash'`. **Drop** the filename and #80-link matchers from #306: they are ambiguous or unverified, and L6 makes them unnecessary.
- A reprint made inside Bambuddy adds a run (M5), not an archive, so there is nothing to link. Runs are read, never stored.
- **The spike in #306 is still required,** on a real H2C print on both routes, and its results go into the print-flow spec as verified facts. It must confirm two things: that L1 holds for pipeline-dispatched items, and that L6's hashes are equal in practice. L6 fails if anything rewrites the sliced file between slicing and dispatch.

### 2.2 Archive client models (#307)

In `backend/scadbuddy/bambuddy/models.py`:

- **`Archive`** stays the list row that `choices.py` and `pipelines.py` use. #307 adds `content_hash`, `project_id` and `plate_id` to it (L7 scan).
- **`ArchiveDetail(Archive)`** mirrors `ArchiveResponse` (`schemas/archive.py:52-137`) as far as #308 needs it:
  - `filename`, `file_size`, `thumbnail_path`, `timelapse_path`, `source_3mf_path`;
  - `print_time_seconds`, `actual_time_seconds`;
  - `filament_used_grams`, `filament_type`, `filament_color`;
  - `layer_height`, `nozzle_diameter`, `cost`, `notes`, `tags`;
  - `photos: list[str]`, `failure_reason`, `quantity`;
  - `run_count`, `successful_run_count`, `failed_run_count`, `last_run_at`.

  `extra="ignore"`, as every `BambuddyModel` has.
- **`ArchiveRun`** mirrors `PrintLogEntrySchema` (M5), and **`ArchiveRunList`** is `{items, total}`.
- **`TimelapseInfo`** and **`TimelapseThumbnails`** mirror M4.
- **`PrinterMedia`** is `{archive_id, printer_id, local_timelapse, remote_files: [{name, path, size, mtime, kind}], warnings: [str]}` (A5).
- **`ArchivePhotoUpload`** is `{status, filename, photos}` (M3).

`errors.Scope` gains **`MANAGE_ARCHIVES = "Manage Archives"`** (A3, A6) and **`CONTROL_PRINTER = "Control Printer"`**, the latter used only to explain the A5 warning. The settings connection test adds a `can_manage_archives` probe, reported as a warning rather than a failure, because only attachments need it.

`BambuddyClient` methods, each with its declared `Scope`:

| Method | Bambuddy | Scope |
|---|---|---|
| `archive(id)` | `GET /archives/{id}` | READ_STATUS |
| `archive_runs(id)` | `GET /archives/{id}/runs` | READ_STATUS |
| `archives(printer_id, date_from, date_to, limit, offset)` | `GET /archives/` (extended) | READ_STATUS |
| `timelapse_info(id)`, `timelapse_thumbnails(id)` | M4 | READ_STATUS |
| `printer_media(id)` | A5 | READ_STATUS |
| `select_timelapse(id, filename)` | A4 | MANAGE_ARCHIVES |
| `upload_archive_photo(id, name, stream)`, `delete_archive_photo(id, name)` | A3 | MANAGE_ARCHIVES |
| `stream(path, range)` | any media or file route (2.3) | READ_STATUS |
| `queue_item(id)` | exists; now also read for `archive_id` | READ_STATUS |

### 2.3 Range-capable media proxy (#307)

- There is one streaming primitive, `BambuddyClient.stream(path, *, range: str | None)`. It is an async context manager over `httpx` `client.stream("GET", …)` that sends `X-API-Key` and passes `Range` and `If-Range` through.
- ScadBuddy's route returns a `StreamingResponse` with Bambuddy's status (`200`, `206` or `416`). It copies only `Content-Type`, `Content-Length`, `Content-Range`, `Accept-Ranges`, `ETag`, `Last-Modified` and `Content-Disposition`. The body is forwarded chunk by chunk and never buffered.
- **The key never reaches the browser (A1).** No media token is minted, and no Bambuddy URL is handed to the browser.
- **Proxy only archives linked to an output.** Every proxy route first checks `output_bambuddy_prints` for the `archive_id` and answers 404 otherwise. ScadBuddy's proxy must not become a read-through to the whole of Bambuddy's history.
- Routes, all under `/api/v1/prints/{archive_id}`:

  | Route | Bambuddy |
  |---|---|
  | `/timelapse` | `/archives/{id}/timelapse` (M1), with Range |
  | `/photos/{filename}` | M2 |
  | `/thumbnail` | `/archives/{id}/thumbnail` |
  | `/plates/{index}/thumbnail` | `/archives/{id}/plate-thumbnail/{index}` |
  | `/files/sliced` | `/archives/{id}/download` |
  | `/files/source` | `/archives/{id}/source` |
  | `/attachments/{library_file_id}` | `/library/files/{id}/download` (M7), with Range; used for videos (2.5) |

  The last route also checks that the library file is one of *this* print's attachments.
- Timelapse poster frames are base64 JPEG (M4), so #308 returns them inline in the detail rather than proxying URLs.
- **Done-when for #307:** respx tests from recordings for every client method; a proxy test that a `Range: bytes=100-199` request reaches Bambuddy and the `206` and `Content-Range` come back; and the browser seek check in the issue.

### 2.4 Prints API shape (#308)

A **print** is one linked archive (`output_bambuddy_prints` row joined to `GET /archives/{id}`), keyed by `archive_id`. Runs are listed inside it.

- **`GET /api/v1/prints`** takes `slug`, `status`, `printer_id`, `from`, `to`, `q` (print name or params text), `limit` (≤100) and `cursor`. It returns `{items: [PrintSummary], next_cursor}`.
  - `PrintSummary` is: `archive_id`, `output_id`, `slug`, `output_name`, `status`, `printer_id`, `started_at`, `completed_at`, `actual_time_seconds`, `filament_used_grams`, `cover` (`{kind: 'photo'|'thumbnail', url}`, pointing at the 2.3 proxy), `has_timelapse`, `attachment_count`, `params_diff` (values that differ from the template defaults) and `run_count`.
  - The list is driven by ScadBuddy's own table (only linked archives, as #308 says). Archive reads are cached per archive for 30 s in process.
- **`GET /api/v1/prints/{archive_id}`** returns `PrintDetail`, which adds:
  - `provenance` (`slug`, `model_version`, `params`, `output_id`, `edit_url`);
  - `files` (each `{kind: 'output_3mf'|'sliced'|'source'|'preview_glb', name, size, url}`);
  - `media` (`finish_photo`, `photos[]`, `timelapse` `{url, info, poster_frames[]}` or null, `plate_thumbnails[]`, `attachments[]` from 2.5);
  - `outcome` (status, failure reason, estimated and actual times, filament, cost, printer, `runs[]`);
  - `printer_media` (A5; empty when not requested, fetched on `?printer_media=1` so the page does not hit the printer by default);
  - `links` (`bambuddy_url`, `customize_url`).
- **Finish photo.** `photos[0]` is treated as the finish photo **only** when Bambuddy captured it. This is **unverified**, so it is a spike item in #308. Until then, `finish_photo` is null and every photo goes in `photos[]`.
- **Deleted archive.** A linked archive that 404s is returned in the list with `status: "deleted_in_bambuddy"`, not dropped (#310). An output with a send but no link yet is not a print. The Prints tab shows it as "waiting for Bambuddy" from the outputs data.

### 2.5 Attachments (#309)

- **Photos** go twice (A3, M3, M7):
  1. `POST /archives/{id}/photos`, so the photo shows on the print in Bambuddy;
  2. an upload to the project's `Media/` folder.

  Both are streamed from ScadBuddy's spooled temp file. The archive route allows only `.jpg`, `.jpeg`, `.png` and `.webp` (M3), and ScadBuddy refuses other image types before uploading.
- **Videos** go to `Media/` only (M7).
- **The `Media/` folder** is found or created as `{name: "Media", parent_id: <project folder>, project_id}` (M8), on demand (#317's layout). A print with no project uses the inbox (`library_folder_id`), with no `Media/` subfolder.
- **ScadBuddy-side state, in Postgres.** A new table, `print_attachments`:

  | Column | Type |
  |---|---|
  | `id` | bigserial |
  | `archive_id` | bigint |
  | `kind` | text: `'photo'` or `'video'` |
  | `archive_photo` | text null: Bambuddy's `photos[]` name for a photo |
  | `library_file_id` | bigint null: the `Media/` copy |
  | `caption` | text null |
  | `created_at` | timestamptz |

  It is indexed on `archive_id`. Captions live here, keyed by the attachment row rather than the file name, because Bambuddy renames photos (M3).
- **Delete** removes the Bambuddy side first and then the row, for the same reason as #316's forget-after-delete rule.
- **Memory.** Both Bambuddy uploads read the whole body into memory (M3, M7), so a 1 GiB video costs about 1 GiB of RAM in the Bambuddy pod. #309 must size the default limit with this in mind, and file an upstream issue asking for a streamed library upload.
- **Promote to template gallery** copies the file into the template's media, in #456's store. Built-ins are offered a duplicate first.
- **Scopes:** photo upload and delete need `can_manage_archives` (A3); `Media/` and video uploads need `can_manage_library` (A7).

### 2.6 Storage

- Every piece of new ScadBuddy state (the links in 2.1, the attachments and captions in 2.5, and the slice `file_hash` column in 2.1) is a **Postgres** table or column. Each change is a new timestamped file in `backend/scadbuddy/migrations/` (#491).
- **No backfill:** existing outputs simply have no prints until they print again or the reconciler links them.
- Several open PRs also add migrations (#374, #430, #462, #463, #461, …), which no longer conflict: each adds its own file, and a file with an older timestamp that merges later is still applied.
- Nothing goes in `data/`. There is no in-memory fallback: the database is required (#401, #467).

### 2.7 UI routes (#310, #311)

| Route | Page | Issue |
|---|---|---|
| `/prints` | Global print history (nav entry), with filters in the URL (`?slug=&status=&printer=&from=&to=&q=&view=cards\|list`) | #310 |
| `/m/:slug/prints` | The template's Prints tab: the same list, with `slug` fixed | #310 |
| `/prints/:archiveId` | Print detail | #311 |

- These sit beside the existing `m/:slug/history` and `m/:slug/versions` in `frontend/src/App.tsx`.
- Image clicks open the lightbox (#275); a row click navigates.
- Downloads use `lib/embed.ts`'s blob path.
- "Open in Bambuddy" uses `window.open` when embedded.
- "Print again" posts `POST /queue/` with `archive_id` through a new ScadBuddy route (M6), behind the send confirmation.
- "Pull timelapse from printer" is shown only when `printer_media.remote_files` has a timelapse, and calls `timelapse/select` on click (A4, A5).

## 3. Order and ownership

1. **#306 and #307 in parallel.** #307 needs no table. #306 adds `output_bambuddy_prints` and the slice `file_hash` column, and needs #462 merged first because it extends #462's slices table.
2. **#308** on top of both.
3. **#309, #310 and #311 in parallel.** #311 also needs #275, and #309's promote step needs #456.

## 4. Open items carried into the children

- #306 spike: L1 on the pipeline route, and L6 in practice (2.1).
- #308 spike: whether the finish photo is always `photos[0]` (2.4).
- #309:
  - Bambuddy's in-memory uploads (2.5): **awaiting Elan's decision** on whether a 1 GiB video goes through Bambuddy's in-memory library upload at all. Until then §2.5 stands as written; file the upstream issue either way;
  - whether Bambuddy's library UI previews video.
- Auth-enabled verification: the homelab runs with auth off, so A2–A7 are verified from source only. #307's connection test should exercise them once a key with auth on is available.
