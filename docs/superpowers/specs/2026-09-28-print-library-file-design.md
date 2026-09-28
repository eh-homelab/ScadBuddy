# Print any Bambuddy library file (#313)

Part of epic #84. Approved in chat on 2026-09-28. The design, and every answer below,
is the user's.

## 1. Goal

Anyone can print a file already in Bambuddy's library through the same spool-first
Print dialog an output uses (spool-first spec 2026-09-27 §4): pick spools, nozzle,
quality and plate, then resolve → slice → queue. Until now only a ScadBuddy output
could be printed that way.

## 2. Decisions (user, 2026-09-28)

- **Where:** a new top-level **Library** page that browses Bambuddy's library by
  folder. Each file has **Print**, which opens the Print dialog.
- **File types:** unsliced `.3mf` files by default. An **Advanced** toggle on the page
  lists every file Bambuddy holds, STL included. Sliced `.gcode.3mf` files are never
  printable here: they are hidden by default and shown without **Print** under
  Advanced. A sliced file is printed from Bambuddy directly.
  An STL under Advanced is printable as one plate. Whether Bambuddy's slice route
  takes a raw STL, and what its plates and `filament-requirements` routes answer for
  one, is checked with a test slice (no print) before the STL path is built. If it
  can't, STLs stay listed without **Print**, like `.gcode.3mf`.
- **Memory:** the dialog remembers its choices per library file, keyed by Bambuddy's
  file id, the way it remembers them per model for an output.
- **Upstream:** no Bambuddy change is asked for.

## 3. What differs from an output

| | Output (today) | Library file |
|---|---|---|
| Where the file comes from | `model.3mf` on the PVC, uploaded on demand | Already in Bambuddy; never uploaded or modified |
| Plates | `plates_of(model.3mf)` | `GET /library/files/{id}/plates` |
| Slots | `filament-requirements` of the uploaded copy, colors from `meta.colors` | `filament-requirements` of the file itself |
| Replate for the printer (#105) | yes | no: the file is laid out as its author left it |
| Recolor for the spools (#476) | yes | no: the plate thumbnail keeps the file's colors |
| Nozzle refusals (#469) | yes | yes: they read the printer, not the file |
| Remembered choices | per model slug | per library file id |
| Where the run is recorded | the output's `meta.json` (progress, History) | nowhere in ScadBuddy; Bambuddy's queue and archives (#305 owns print history) |

The resolver, slicing and queueing are shared unchanged.

## 4. Backend

**A print source seam.** `run_for_output` and `filament_options_for_output` read
everything from the output's `meta`. They get a small `PrintSource` interface with
two implementations:

- `OutputSource`: today's behavior, moved as-is (upload, replate, recolor, record).
- `LibrarySource(file_id)`: plates from Bambuddy's plates route, slots from the
  file's `filament-requirements`, the file id as the library file to slice, no
  upload, and no local record.

**Routes**, under `/api/v1/print/library`:

- `GET /print/library?folder_id=&all=` lists folders and files, proxied from
  `GET /library/folders` and `GET /library/files/`. Without `all`, only
  `file_type == "3mf"`. Each row carries a `printable` flag, false for `gcode.3mf`.
- `GET /print/library/{file_id}/choices` and `/filaments` mirror the output routes.
- `POST /print/library/{file_id}/run` mirrors the output route, returning the same
  `RunResult`. A `gcode.3mf` file is a 422.
- `PUT /print/library/{file_id}/choices` remembers the dialog's choices in a new
  Postgres table `library_print_choices (file_id int primary key, choices jsonb,
  updated_at)`. New persistent state goes in Postgres (storage decision, 2026-09-28).
  No file-data migration.

**Agent coverage.** `agent/test/coverage.test.ts` fails CI when a backend route has
neither an agent tool nor an entry in `PENDING_ROUTES` or `NOT_A_TOOL` in
`agent/src/tools/coverage.ts`. The new routes go in `PENDING_ROUTES`, citing #313. An
agent tool for library prints is a follow-up.

## 5. Frontend

- **Library page** (`/library`, a nav tab beside Models and Settings): a folder list,
  a file grid with Bambuddy's thumbnail for each file, and an **Advanced** toggle for
  §2's wider listing. It is remembered per viewer in local storage.
- **Print dialog:** `PrintPicker` takes a `source` of either
  `{kind: "output", output}` or `{kind: "library", file}` in place of `output`. It
  depends on #481's split of PrintPicker into smaller components, so #481 lands
  first.
- After a library run the dialog shows the queued items and a link to Bambuddy's
  queue. There is no ScadBuddy progress panel, since no output is recorded.

## 6. Out of scope

- Printing a sliced `.gcode.3mf` as-is.
- Editing or re-uploading someone else's library file.
- Library print history: #305.
- An MCP/agent tool for library prints.

## 7. Done when

- The Library page lists 3MF files, and Advanced lists every file type.
- **Print** on a 3MF opens the Print dialog, and a run queues it through
  resolve → slice → queue, with #469's refusals applied.
- Reopening the dialog on the same file starts from the last choices.
- Backend, frontend, agent and e2e gates pass, with an msw mock for the new routes.
