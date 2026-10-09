# Library parity 3: progress route and UI for library prints (#1751)

Epic #1749 rule: a library-file print is no different from any other print; only the
3MF source differs. Rows R10, F1, F2, F6, F7, F9, plus the #1947 leftovers.

## 1. Bind a library follow to its own run (bug, test first)

`library_progress` reads every send of the file within 24 h of the newest, so a run
whose queue item never settles holds every later run's follow open.

- Migration `<ts>_print_sends_run.sql`: `print_sends.run_id text` (nullable; old rows
  have none).
- `PrintSend.run_id`; `RecordInput.run_id` (default `None`), set by `PrintRun` to
  `accepted.run.id`. An activity's input is not part of replay's command matching, so
  no `patched()` is needed; the replay tests prove it.
- `PrintPipeline.record` passes it to `record_sends`.
- `library_progress` reads the newest send's run: every send with its `run_id`. A send
  with no run id (recorded before this) keeps the 24 h window over such sends.
- Failing test first: two runs, the first's item pending for ever, the second's done:
  the progress is the second run's, settled.

## 2. One follow loop

`Follower.follow` and `_follow_library` fold into one loop over a per-subject read:
an output's reads its meta (`deleted` when gone) then `progress_for`; a library file's
`read_library`. Both publish through `ProgressObserver` (keyed by run subject, so a
library print gets `print.progress`/`print.settled` on `print:library:<id>`), both
emit once per distinct failure. Activity code only: no workflow change.
`print_succeed` calls `observer.started` for a library run too (R4).

## 3. Progress route (R10)

`GET /api/v1/print/library/{file_id}/progress`: as the output's, by subject. A newest
run that failed before queueing anything is the progress (`route: "run"`), as
`_failed_before_queueing`; a print still moving is followed (`follows.ensure`).
Agent: a `coverage.ts` entry only (library tools are #1756).

## 4. UI (F1, F2, F6, F7, F9)

- `usePrintProgress(subject)` takes the run subject (an output id or
  `library:<id>`), reading the matching route; the topic is `print:<subject>`.
- `PrintPicker` follows a library run as an output's (F1). Attaching to a project
  (F2) is `POST /outputs/{id}/project`, output-only: a library run is filed under its
  project by the run itself (`project_id` on `print_enqueue`), so F2 is closed by the
  same follow, nothing more.
- `PrintItem` `PrintingNow` for a library print (F7).
- `PrintHistory` per-file view (`?file=`): a Waiting row for the file's newest run
  while no listed print came from its queue items (F9). `PrintSummary` gains
  `queue_item_id`.
- `LibraryPage` `onRan` (F6): checked; the dialog itself now follows the run.

## Checks

Backend ruff, format, mypy, focused pytest (linking, follow, printing routes, replay);
frontend lint, typecheck, focused vitest; agent typecheck + coverage test.
