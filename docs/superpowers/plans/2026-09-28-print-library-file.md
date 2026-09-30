# Print any Bambuddy library file (#313) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new Library page lists Bambuddy's library by folder. **Print** on an unsliced 3MF opens the same spool-first Print dialog an output uses, and it resolves, slices and queues the file as it stands in Bambuddy. Its choices are remembered per library file.

**Architecture:** A small `PrintSource` protocol sits under the print run. `OutputSource` is today's code moved behind it (upload, replate, recolor, record). `LibrarySource` reads plates and slots from Bambuddy and slices the file id as is. New routes under `/api/v1/print/library` mirror the output print routes. A new Postgres table remembers choices per Bambuddy file id. In the frontend, `PrintPicker` and its #481 hooks take a `source` union instead of an output id. A `/library` page opens the dialog with `{kind: "library", file}`.

**Tech Stack:** Python 3.12, FastAPI, pydantic, httpx, respx, psycopg 3 (Postgres 17); React 19, Vite, msw, vitest, Playwright; the agent service's vitest coverage check.

**Spec:** `docs/superpowers/specs/2026-09-28-print-library-file-design.md`

## Global Constraints

- Prerequisites: #538, #481 and #312 are merged to main, and `feat/313-print-library-file` has origin/main merged in (Task 0). Paths use post-#312 names: `backend/scadbuddy/bambuddy/print_run.py`, not `pipelines.py`.
- File types: an unsliced `.3mf` (Bambuddy `file_type == "3mf"`) is listed and printable by default. **Advanced** lists every file. A `gcode.3mf` is never printable here: it is hidden by default and shown without **Print** under Advanced. An STL is printable under Advanced only if Task 1's probe passes (Task 7). Otherwise it is listed without Print.
- No Bambuddy change is asked for, and no library file is ever uploaded, modified, replated or recolored by a library print.
- The resolver, slicing and queueing are shared unchanged. #469's nozzle refusals apply to library prints.
- Remembered choices are per Bambuddy file id, in the Postgres table `library_print_choices (file_id int primary key, choices jsonb, updated_at)`. They are added as a NEW migration file in `backend/scadbuddy/migrations/` named `$(date -u +%Y%m%dT%H%MZ)_library_print_choices.sql`. Never edit a merged migration.
- A library run records nothing in ScadBuddy: no `meta.json` and no progress panel. The dialog shows the queued items and Bambuddy's queue link.
- Routes live under `/api/v1/print/library`. `POST /print/library/{file_id}/run` answers the same `PrintRunResult` as the output run, and a `gcode.3mf` there is a 422.
- Every new backend route needs an entry in `agent/src/tools/coverage.ts`. Ruling: `NOT_A_TOOL` citing #313. `PENDING_ROUTES` is impossible here, because `agent/test/coverage.test.ts` fails for a pending operation that is present in `backend/openapi.json`.
- Generated files `backend/openapi.json`, `frontend/src/api/schema.d.ts` and `agent/src/api/schema.d.ts` are NEVER committed. Regenerate with `pnpm gen:api` (typecheck, test and build run it) and never `git add` them.
- Tests that need the database use `@pytest.mark.requires_postgres`. Run them with `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/scadbuddy_test`.
- Gates per task:
  - Backend (`cd backend`): `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/scadbuddy_test uv run --frozen pytest`.
  - Frontend (`cd frontend`): `pnpm lint && pnpm typecheck && pnpm exec vitest run`.
  - Agent (`cd agent`): `pnpm typecheck && pnpm exec vitest run`.
- One commit per task, with explicit `git add` pathspecs. Messages end with a blank line and then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Use US spelling ("color") in all new prose, UI copy and names. Existing identifiers such as `colours` and `normalise_colour` keep their spelling.
- Frontend mocks: msw handlers for every new route go in `frontend/src/mocks/handlers.ts`.

## Review Focus

1. **A file deleted in Bambuddy between listing and run.** `POST /print/library/{id}/run` must answer 404 (`bambuddy-not-found`) with nothing sliced or queued, and the dialog must show the detail and keep Print enabled, since the failure isn't a 422. Owned by Task 6 (`test_a_file_deleted_in_bambuddy_is_a_404_with_nothing_sliced`).
2. **A `.gcode.3mf` posted to run** (a stale tab, or a hand-made call). The answer must be a 422 naming the file and saying to print it from Bambuddy, with no slice. The choices and filaments reads refuse it the same way. Owned by Task 6 (`test_a_sliced_file_is_a_422_before_anything_is_sliced`).
3. **A folder with hundreds of files.** The listing is one Bambuddy read (no request per file) that returns every row, and the page renders them with lazy-loaded thumbnails. Owned by Task 6 (`test_a_folder_of_hundreds_of_files_is_one_read`) and Task 10 (`renders a folder of hundreds of files with lazy thumbnails`).
4. **A library file with no plates metadata** (third-party 3MF, `plates: []`, possibly `filaments: []`). It must print as plate 1 with one filament of unknown color, not crash or slice nothing. Owned by Task 5 (`test_a_file_with_no_plates_or_filaments_is_one_plate_one_filament`) and Task 6 (`test_a_file_with_no_plate_metadata_prints_plate_one`).
5. **Remembered choices naming a printer or spool that no longer exists.** The dialog must open on an active printer (the configured one, else the first), and a vanished spool falls back to the auto-match for its slot. The printer case is owned by Task 6 (`test_a_remembered_printer_that_is_gone_falls_through`). The spool case is already pinned by `frontend/src/lib/filaments.test.ts` `seedPlan`, and Task 9 adds `library choices seed through the same seedPlan` to prove the library path reaches it.
6. **Bambuddy unreachable while listing.** The page must show the problem detail, not a blank grid. Owned by Task 10 (`says when the library cannot be read`).

---

## File Structure

Backend (`backend/`):
- `scadbuddy/bambuddy/models.py` (modify): `LibraryListRow`, `LibraryPlate`, `LibraryPlates`.
- `scadbuddy/bambuddy/client.py` (modify): `library_files(folder_id=)` and `library_plates(file_id)`.
- `scadbuddy/migrations/<stamp>_library_print_choices.sql` (create).
- `scadbuddy/library/settings_store.py` (modify): `library_choices` and `set_library_choices`.
- `scadbuddy/bambuddy/print_source.py` (create): the `PrintSource` protocol, `ReadFile`, `PrintFile`, `OutputSource`, `LibrarySource`, `PRINTABLE_TYPES` and `printable()`.
- `scadbuddy/bambuddy/print_run.py` (modify): `run_print` and `filament_options` over a source. `run_for_output` and `filament_options_for_output` become thin wrappers, and `run_for_library` and `filament_options_for_library` are added.
- `scadbuddy/bambuddy/choices.py` (modify): `choices_for(client, source, settings, *, remembered, printer_id)`, with `choices_for_output` kept as a wrapper.
- `scadbuddy/bambuddy/send.py` (modify): `resolve_print_options(slug: str | None, ...)`.
- `scadbuddy/bambuddy/library_listing.py` (create): `LibraryListing`, `LibraryEntry`, `LibraryFolderView` and `list_library`.
- `scadbuddy/api/library_print.py` (create): the `/print/library` routes. `main._api_router` mounts it automatically.
- Tests (all created): `tests/bambuddy/test_library_client.py`, `tests/test_library_choices_store.py`, `tests/bambuddy/test_print_source.py`, `tests/api/test_print_library.py`.
- `tests/bambuddy/recordings/*.json` and `README.md` (Task 1).

Agent (`agent/`):
- `src/tools/coverage.ts` (modify): the `NOT_A_TOOL` entries.

Frontend (`frontend/`):
- `src/api/types.ts` and `src/api/client.ts` (modify): the library types and calls.
- `src/mocks/library.ts` (create): library fixtures. `src/mocks/handlers.ts` (modify): state and handlers.
- `src/lib/printSource.ts` (create): the `PrintSource` union and `sourceApi()`.
- `src/lib/usePrintChoices.ts`, `src/lib/useFilamentPlan.ts` and `src/lib/useRunPrint.ts` (modify): take `source`.
- `src/components/PrintPicker.tsx` (modify): the `source` prop. `src/components/ActionBar.tsx` (modify): passes it.
- `src/components/print/PlatesToPrint.tsx` (modify): `thumbnailUrl` prop. `src/components/PrintOptionsDisclosure.tsx` (modify): optional `slug`.
- `src/pages/LibraryPage.tsx` (create). `src/App.tsx` and `src/components/AppShell.tsx` (modify): route and nav tab.
- `e2e/library.spec.ts` (create).

---

### Task 0: Prerequisites

**Files:** none (merge only)

- [ ] **Step 1: Confirm the three prerequisites are on main**

Run: `gh pr view 538 --json state,mergedAt; gh pr list --state merged --search "481 in:title" --json number,title; gh pr list --state merged --search "312 in:title" --json number,title`
Expected: all three merged. If any is not, stop and report it. Every later task assumes their code.

- [ ] **Step 2: Merge origin/main into the branch**

```bash
cd /home/elan/repos/eh-homelab/ScadBuddy/.claude-worktrees/313-library
git fetch origin
git merge origin/main
```
Expected: `backend/scadbuddy/bambuddy/print_run.py` exists, `pipelines.py` doesn't, `backend/scadbuddy/bambuddy/extruders.py` has `plan_extruders`, and `frontend/src/lib/usePrintChoices.ts` exists.

- [ ] **Step 3: Run every gate once on the merged tree**

Run the backend, frontend and agent gates from Global Constraints.
Expected: green. A red gate here belongs to main, not #313. Report it before continuing.

(The merge commit is the only commit of this task.)

---

### Task 1: Recordings and the STL slice probe

A read-only recording pass, plus one test slice of an STL. **Never POST `/queue/`.** The slice job is the only write, and it leaves one sliced library file in Bambuddy. Report that file's id to the user and don't delete it.

**Files:**
- Create: `backend/tests/bambuddy/recordings/library-files-root.json`, `library-files-folder.json`, `library-plates-single.json`, `library-plates-multi.json`, `library-plates-stl.json`, `filament-requirements-stl.json`
- Modify: `backend/tests/bambuddy/recordings/README.md`

**Interfaces:**
- Produces: the recordings above (Tasks 2, 5 and 6 read them), and the decision `STL_PRINTABLE = yes | no`, recorded in the README (Task 7 reads it).

- [ ] **Step 1: Record the listing and plate routes (GET only)**

```bash
B=https://bambuddy.internal.nullreference.io/api/v1
R=backend/tests/bambuddy/recordings
redact='import json,sys
d=json.load(sys.stdin)
rows=d if isinstance(d,list) else [d]
for r in rows:
    if isinstance(r,dict) and "created_by_username" in r: r["created_by_username"]=None
print(json.dumps(d,indent=2))'
curl -s "$B/library/files/" | python3 -c "$redact" > $R/library-files-root.json
curl -s "$B/library/files/?folder_id=4" | python3 -c "$redact" > $R/library-files-folder.json
curl -s "$B/library/files/89/plates" | python3 -m json.tool > $R/library-plates-single.json
curl -s "$B/library/files/67/plates" | python3 -m json.tool > $R/library-plates-multi.json
curl -s "$B/library/files/46/plates" | python3 -m json.tool > $R/library-plates-stl.json
curl -s "$B/library/files/46/filament-requirements" | python3 -m json.tool > $R/filament-requirements-stl.json
```
At planning time (2026-09-28) these ids were: 89 `bag-clip…3mf` (root, one plate), 104 its `.gcode.3mf` (root), 67 `Clara's Wand.3mf` (folder 1, two plates), and 46 `Desiccant_Box.stl` (folder 4). Re-list first (`curl -s "$B/library/files/?folder_id=4" | python3 -c 'import json,sys;[print(r["id"],r["file_type"],r["filename"]) for r in json.load(sys.stdin)]'`) and substitute if they moved.
Check: `library-files-folder.json` contains at least one each of `3mf`, `gcode.3mf` and `stl`. `library-plates-multi.json` has `"is_multi_plate": true` and two plates. `library-plates-stl.json` has `"plates": []`. `filament-requirements-stl.json` has `"filaments": []`.

- [ ] **Step 2: The probe — slice the STL once, no print**

```bash
B=https://bambuddy.internal.nullreference.io/api/v1
curl -s "$B/slicer/presets" | python3 -c '
import json,sys
d=json.load(sys.stdin)
def find(kind,name):
    for tier in ("cloud","standard","local"):
        for p in d.get(tier,{}).get(kind,[]):
            if p.get("name")==name: return {"source":p["source"],"id":p["id"]}
    raise SystemExit(f"no {kind} preset {name!r}")
print(json.dumps({"printer_preset":find("printer","Bambu Lab H2C 0.4 nozzle"),
  "process_preset":find("process","0.20mm Standard @BBL H2C"),
  "filament_presets":[find("filament","Bambu PLA Basic @BBL H2C")],
  "plate":1}))' \
| curl -s -X POST -H 'Content-Type: application/json' --data-binary @- "$B/library/files/46/slice"
```
Expected: `{"job_id": N, ...}`. Then poll until it settles:
```bash
for i in $(seq 1 60); do curl -s "$B/slice-jobs/N" | tee /dev/stderr | grep -qE '"status": ?"(completed|failed)"' && break; sleep 10; done
```
**PASS** means `status == "completed"` and `result.library_file_id` isn't null. Anything else is **FAIL**. Note the job's `error`/`error_message` in the README. Write down the sliced file id and tell the user it is left in Bambuddy's library.

- [ ] **Step 3: Record what the recordings settle**

Append this section to `backend/tests/bambuddy/recordings/README.md`:

```markdown
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
- **Slicing a raw STL (#313 probe, the only write):** <PASS: job N completed, sliced file
  M left in the library | FAIL: job N failed with "...">. STL_PRINTABLE = <yes | no>.
```
Fill in the angle-bracket line with the probe's actual outcome. That line is the one place this task writes a result down.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/bambuddy/recordings/library-files-root.json backend/tests/bambuddy/recordings/library-files-folder.json backend/tests/bambuddy/recordings/library-plates-single.json backend/tests/bambuddy/recordings/library-plates-multi.json backend/tests/bambuddy/recordings/library-plates-stl.json backend/tests/bambuddy/recordings/filament-requirements-stl.json backend/tests/bambuddy/recordings/README.md
git commit -m "test(bambuddy): record the library listing and plates, and probe an STL slice (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Client reads for the library listing and plates

**Files:**
- Modify: `backend/scadbuddy/bambuddy/models.py` (after `LibraryFile`)
- Modify: `backend/scadbuddy/bambuddy/client.py` (the `# --- library` section, and the models import)
- Test: `backend/tests/bambuddy/test_library_client.py`

**Interfaces:**
- Consumes: Task 1's recordings.
- Produces: `BambuddyClient.library_files(*, folder_id: int | None) -> list[LibraryListRow]`, `BambuddyClient.library_plates(file_id: int) -> LibraryPlates`, and the models `LibraryListRow`, `LibraryPlate` and `LibraryPlates`.

- [ ] **Step 1: Write the failing tests**

```python
"""The two reads a library print adds (#313): the folder's files and a file's plates."""

from __future__ import annotations

import httpx
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_the_root_lists_without_a_folder(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/library/files/").mock(
        return_value=httpx.Response(200, json=recording("library-files-root.json"))
    )

    rows = await bambuddy.library_files(folder_id=None)

    assert "folder_id" not in route.calls.last.request.url.params
    assert rows and all(row.folder_id is None for row in rows)
    assert {"3mf", "gcode.3mf"} <= {row.file_type for row in rows}


@respx.mock
async def test_a_folder_is_asked_for_by_id(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/library/files/").mock(
        return_value=httpx.Response(200, json=recording("library-files-folder.json"))
    )

    rows = await bambuddy.library_files(folder_id=4)

    assert route.calls.last.request.url.params["folder_id"] == "4"
    assert {"3mf", "gcode.3mf", "stl"} <= {row.file_type for row in rows}


@respx.mock
async def test_plates_read_the_undeclared_shape(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/67/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-multi.json"))
    )
    respx.get(f"{API}/library/files/46/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-stl.json"))
    )

    multi = await bambuddy.library_plates(67)
    stl = await bambuddy.library_plates(46)

    assert [plate.index for plate in multi.plates] == [1, 2]
    assert multi.is_multi_plate
    assert stl.plates == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_library_client.py -v`
Expected: FAIL with `AttributeError: 'BambuddyClient' object has no attribute 'library_files'`.

- [ ] **Step 3: Add the models (in `models.py`, after `LibraryFile`)**

```python
class LibraryListRow(BambuddyModel):
    """A row of ``GET /api/v1/library/files/`` (Bambuddy's ``FileListResponse``;
    ``library-files-root.json``). ``file_type`` is ``"3mf"``, ``"gcode.3mf"`` for a
    sliced file, ``"stl"`` and so on."""

    id: int
    filename: str
    file_type: str
    folder_id: int | None = None
    file_size: int | None = None
    thumbnail_path: str | None = None
    print_count: int = 0
    sliced_for_model: str | None = None


class LibraryPlate(BambuddyModel):
    """One plate of ``GET /api/v1/library/files/{id}/plates``."""

    index: int
    name: str | None = None
    has_thumbnail: bool = False


class LibraryPlates(BambuddyModel):
    """``GET /api/v1/library/files/{id}/plates``. Bambuddy's OpenAPI declares no schema
    for it (its 200 is ``{}``); this is the recorded shape. An STL, or a 3MF that
    carries no plate metadata, answers ``plates: []``."""

    file_id: int
    plates: list[LibraryPlate] = Field(default_factory=list)
    is_multi_plate: bool = False
```

- [ ] **Step 4: Add the client methods (in `client.py`, after `library_file`)**

Add `LibraryListRow` and `LibraryPlates` to the `from scadbuddy.bambuddy.models import (...)` list, then:

```python
    async def library_files(self, *, folder_id: int | None) -> list[LibraryListRow]:
        """``GET /library/files/`` — one folder's files, or the root's without one
        (``include_root`` defaults to true). One read, however many files: Bambuddy
        does not paginate it."""
        what = (
            "list the library files"
            if folder_id is None
            else f"list the files of library folder {folder_id}"
        )
        response = await self._send(
            "GET",
            "/library/files/",
            scope=Scope.MANAGE_LIBRARY,
            what=what,
            params={"folder_id": folder_id} if folder_id is not None else None,
        )
        return [LibraryListRow.model_validate(row) for row in self._rows(response, what=what)]

    async def library_plates(self, file_id: int) -> LibraryPlates:
        """``GET /library/files/{id}/plates`` — the plates Bambuddy reads out of the
        file, with whether each has a cover image."""
        response = await self._send(
            "GET",
            f"/library/files/{file_id}/plates",
            scope=Scope.MANAGE_LIBRARY,
            what=f"read the plates of library file {file_id}",
        )
        return LibraryPlates.model_validate(response.json())
```

- [ ] **Step 5: Run the tests and the backend gate**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_library_client.py -v`, then the full backend gate.
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/bambuddy/models.py backend/scadbuddy/bambuddy/client.py backend/tests/bambuddy/test_library_client.py
git commit -m "feat(bambuddy): read a library folder's files and a file's plates (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Remember the dialog's choices per library file (Postgres)

**Files:**
- Create: `backend/scadbuddy/migrations/<stamp>_library_print_choices.sql`, where `<stamp>` is `$(date -u +%Y%m%dT%H%MZ)` at the moment you create it and must sort after every file already in the directory
- Modify: `backend/scadbuddy/library/settings_store.py` (the module docstring's table list, and two methods on `SettingsStore`)
- Test: `backend/tests/test_library_choices_store.py`

**Interfaces:**
- Produces: `SettingsStore.library_choices(file_id: int) -> ModelPrintChoices` and `SettingsStore.set_library_choices(file_id: int, choices: ModelPrintChoices) -> ModelPrintChoices`.

- [ ] **Step 1: Write the failing tests**

```python
"""#313 — the print dialog's choices per Bambuddy library file, in Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from scadbuddy.bambuddy.models import NozzleChoice, SlotChoice
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import ModelPrintChoices, SettingsStore

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(Settings(data_dir=tmp_path, database_url=pg_conninfo))
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


CHOSEN = ModelPrintChoices(
    printer_id=1,
    filament_plan=[SlotChoice(slot_id=1, spool_id=9)],
    nozzles=[NozzleChoice(size="0.2")],
    tier="fine",
)


def test_nothing_remembered_is_the_empty_choice(store: SettingsStore) -> None:
    assert store.library_choices(89) == ModelPrintChoices()


def test_a_files_choices_are_its_own(store: SettingsStore) -> None:
    store.set_library_choices(89, CHOSEN)

    assert store.library_choices(89) == CHOSEN.model_copy(
        update={"nozzles": [NozzleChoice(size="0.2"), NozzleChoice(size="0.2")]}
    )
    assert store.library_choices(67) == ModelPrintChoices()


def test_remembering_nothing_deletes_the_row(store: SettingsStore, pg_conninfo: str) -> None:
    store.set_library_choices(89, CHOSEN)
    store.set_library_choices(89, ModelPrintChoices())

    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM library_print_choices").fetchone() == (0,)


def test_a_row_that_no_longer_validates_is_nothing_remembered(
    store: SettingsStore, pg_conninfo: str
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO library_print_choices (file_id, choices) VALUES (89, %s::jsonb)",
            ('{"nozzles": [{"size": "0.4"}, {"size": "0.4"}, {"size": "0.4"}]}',),
        )

    assert store.library_choices(89) == ModelPrintChoices()
```
(Check how `pg_conninfo` scopes its schema in `tests/conftest.py`. If the raw `psycopg.connect` needs the throwaway schema's `search_path`, copy what `tests/test_settings_store.py` does.)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/scadbuddy_test uv run --frozen pytest tests/test_library_choices_store.py -v`
Expected: FAIL with `AttributeError: 'SettingsStore' object has no attribute 'library_choices'`.

- [ ] **Step 3: Write the migration**

`backend/scadbuddy/migrations/<stamp>_library_print_choices.sql`:
```sql
-- #313: what the print dialog last chose for a file in Bambuddy's library, keyed by
-- Bambuddy's own file id (a library file has no ScadBuddy slug). The same shape as
-- model_print_choices. A file later deleted in Bambuddy leaves its row behind: the
-- dialog can no longer open on it, and a row is a few hundred bytes.
CREATE TABLE library_print_choices (
    file_id    integer PRIMARY KEY,
    choices    jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Add the store methods (after `set_printer_bed_type`)**

Add `from pydantic import ValidationError` to the pydantic import, and add `library_print_choices` to the module docstring's table list:

```python
    def library_choices(self, file_id: int) -> ModelPrintChoices:
        """What the dialog last chose for one Bambuddy library file (#313); nothing
        remembered is the empty choice. A row this version cannot read is nothing
        remembered too, rather than a dialog that will not open."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT choices FROM library_print_choices WHERE file_id = %s", (file_id,)
            ).fetchone()
        if row is None:
            return ModelPrintChoices()
        try:
            return ModelPrintChoices.model_validate(row["choices"])
        except ValidationError:
            logger.warning("unreadable library print choices", extra={"file_id": file_id})
            return ModelPrintChoices()

    def set_library_choices(self, file_id: int, choices: ModelPrintChoices) -> ModelPrintChoices:
        """Remember one library file's choices; an empty ``choices`` forgets them."""
        with self._pool.connection() as conn:
            if choices == ModelPrintChoices():
                conn.execute("DELETE FROM library_print_choices WHERE file_id = %s", (file_id,))
            else:
                conn.execute(
                    "INSERT INTO library_print_choices (file_id, choices) VALUES (%s, %s)"
                    " ON CONFLICT (file_id) DO UPDATE"
                    " SET choices = EXCLUDED.choices, updated_at = now()",
                    (file_id, Jsonb(choices.model_dump(mode="json"))),
                )
        # The dialog's remembered choices, as for a model: no new section is needed.
        emit(self.events, SettingsChanged(section="model_choices"))
        return self.library_choices(file_id)
```

- [ ] **Step 5: Run the tests, then the backend gate (with the DB env)**

Expected: PASS, including `tests/test_pg_migrations.py`, which applies every file in the directory.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/migrations/*_library_print_choices.sql backend/scadbuddy/library/settings_store.py backend/tests/test_library_choices_store.py
git commit -m "feat(print): remember the dialog's choices per library file in Postgres (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The PrintSource seam, with OutputSource as today's behavior

A pure refactor: the output print suites must pass unchanged.

**Files:**
- Create: `backend/scadbuddy/bambuddy/print_source.py`
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`filament_options_for_output`, `_spool_colours`, `_spool_sides`, `run_for_output`, `_record_queued`)
- Modify: `backend/scadbuddy/bambuddy/choices.py` (`choices_for_output`)
- Modify: `backend/scadbuddy/bambuddy/send.py` (`resolve_print_options`)
- Test: `backend/tests/bambuddy/test_print_source.py`

**Interfaces:**
- Consumes: `copy_to_read`, `ensure_uploaded`, `target_for` (send.py), `folder_for` (projects.py), `plates_of` (render/bambu3mf.py), `QueueOutcome` (dispatch.py), `PlateSend`, `OutputMeta`, `OutputStore` and `MODEL_NAME` (library/outputs.py), and `BambuddyUploadStore` and `SlicedCopy` (uploads.py).
- Produces, in `scadbuddy.bambuddy.print_source`:
  - `ReadFile(id: int, own_colours: list[str] | None)` and `PrintFile(id: int, folder_id: int | None)`, both frozen dataclasses.
  - `class PrintSource(Protocol)`:
    - properties `colours -> list[str]`, `filament_count -> int` and `options_slug -> str | None`
    - `async plate_ids(client) -> list[int]`
    - `async file_to_read(client) -> ReadFile`
    - `async file_to_print(client, *, printer_id: int, nozzle_size: str, plan: FilamentPlan, project_id: int | None) -> PrintFile`
    - `async record(library_file_id: int, plate_id: int, outcome: QueueOutcome, project_id: int | None, sent: list[PlateSend]) -> list[PlateSend]`
  - `OutputSource(store, uploads, meta, settings)`, a dataclass.
- Produces, in `scadbuddy.bambuddy.print_run`:
  - `async run_print(client, source: PrintSource, settings: StoredSettings, request: PrintRunRequest) -> PrintRunResult`
  - `async filament_options(client, source: PrintSource, *, printer_id: int | None = None, plate_id: int = 1, all_plates: bool = False) -> FilamentOptions`
  - `run_for_output` and `filament_options_for_output`, unchanged signatures, now wrappers.
- Produces, in `scadbuddy.bambuddy.choices`: `async choices_for(client, source: PrintSource, settings, *, remembered: ModelPrintChoices | None, printer_id: int | None) -> ChoicesView`.
- Produces, in `scadbuddy.bambuddy.send`: `resolve_print_options(settings, slug: str | None, printer_id, request_scope)`.

- [ ] **Step 1: Write the failing test**

`backend/tests/bambuddy/test_print_source.py`:
```python
"""The print source seam (#313): what the run reads from an output or a library file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scadbuddy.bambuddy.print_source import OutputSource, PrintSource
from scadbuddy.library.settings_store import StoredSettings


class _Meta:
    id = "a" * 32
    slug = "name-keychain"
    colors = ["#FF0000", "#0000FF"]


def test_an_output_source_is_the_models_colors_and_slug(tmp_path: Path) -> None:
    source: PrintSource = OutputSource(
        store=Any, uploads=Any, meta=_Meta(), settings=StoredSettings()  # type: ignore[arg-type]
    )

    assert source.colours == ["#FF0000", "#0000FF"]
    assert source.filament_count == 2
    assert source.options_slug == "name-keychain"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_print_source.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scadbuddy.bambuddy.print_source'`.

- [ ] **Step 3: Create `print_source.py` with the protocol and OutputSource**

```python
"""What a print is of (#313): an output ScadBuddy rendered, or a file already in
Bambuddy's library. The run (`print_run.run_print`) reads everything that differs
between the two through :class:`PrintSource`; the resolver, slicing and queueing are
shared unchanged.

- :class:`OutputSource` is today's behaviour, moved as is: the output's ``model.3mf``
  is uploaded on demand, replated for the printer (#105) and recolored for the spools
  (#476), and each queued plate is recorded on the output (#83).
- :class:`LibrarySource` (below) prints the library file as its author left it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentPlan, normalise_colour
from scadbuddy.bambuddy.projects import folder_for
from scadbuddy.bambuddy.send import copy_to_read, ensure_uploaded, target_for
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, SlicedCopy
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, PlateSend
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import plates_of


@dataclass(frozen=True)
class ReadFile:
    """The library file the filament step reads slots from, and the colours to show
    in place of the file's own (an output's copy recolored for a run, #457)."""

    id: int
    own_colours: list[str] | None = None


@dataclass(frozen=True)
class PrintFile:
    """The library file a run slices, and the folder it went into (#79)."""

    id: int
    folder_id: int | None = None


class PrintSource(Protocol):
    @property
    def colours(self) -> list[str]:
        """One colour per filament of the file, in slot order: what a slot with no
        spool keeps, and the fallback when Bambuddy reads no slots."""
        ...

    @property
    def filament_count(self) -> int:
        """How many filaments the file has, which #469's nozzle check counts."""
        ...

    @property
    def options_slug(self) -> str | None:
        """The model whose remembered print options apply (#88); ``None`` for none."""
        ...

    async def plate_ids(self, client: BambuddyClient) -> list[int]: ...

    async def file_to_read(self, client: BambuddyClient) -> ReadFile: ...

    async def file_to_print(
        self,
        client: BambuddyClient,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
    ) -> PrintFile: ...

    async def record(
        self,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
    ) -> list[PlateSend]: ...


async def _spool_colours(
    client: BambuddyClient, meta: OutputMeta, plan: FilamentPlan
) -> list[str] | None:
    # Moved verbatim from print_run.py (#476): body unchanged.
    if not plan.slots:
        return None
    rgba = {spool.id: normalise_colour(spool.rgba) for spool in await client.spools()}
    return [
        rgba.get(plan.spool_for(index + 1) or 0) or colour
        for index, colour in enumerate(meta.colors)
    ]


@dataclass
class OutputSource:
    store: OutputStore
    uploads: BambuddyUploadStore
    meta: OutputMeta
    settings: StoredSettings

    @property
    def colours(self) -> list[str]:
        return list(self.meta.colors)

    @property
    def filament_count(self) -> int:
        return len(self.meta.colors)

    @property
    def options_slug(self) -> str | None:
        return self.meta.slug

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return [plate.index for plate in plates_of(self.store.directory(self.meta.id) / MODEL_NAME)]

    async def file_to_read(self, client: BambuddyClient) -> ReadFile:
        copy = await copy_to_read(client, self.store, self.uploads, self.meta, self.settings)
        return ReadFile(copy.id, own_colours=list(self.meta.colors) if copy.recolored else None)

    async def file_to_print(
        self,
        client: BambuddyClient,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
    ) -> PrintFile:
        # Placed for the chosen printer's plate, stating the chosen nozzle (#105, #126).
        target = await target_for(
            client,
            self.settings,
            printer_id=printer_id,
            nozzle_diameter=nozzle_size,
            colours=await _spool_colours(client, self.meta, plan),
        )
        # A project's folder replaces the one from Settings for this send (#79); the
        # copy is looked up by (folder, target), so a project gets a copy of its own (#316).
        folder_id = await folder_for(client, project_id) if project_id is not None else None
        file_id = await ensure_uploaded(
            client, self.store, self.uploads, self.meta, self.settings,
            target=target, folder_id=folder_id,
        )
        return PrintFile(file_id, folder_id)

    async def record(
        self,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
    ) -> list[PlateSend]:
        # The body of print_run._record_queued, moved verbatim (#83, #316).
        await self.uploads.record_sliced(
            self.meta.id,
            library_file_id,
            SlicedCopy(id=outcome.sliced_library_file_id, preset_key=outcome.preset_key),
        )
        sent = sent + [
            PlateSend(plate_id=plate_id, queue_item_id=item, slice_job_id=outcome.slice_job_id)
            for item in outcome.queue_item_ids
        ]
        for queue_item_id in outcome.queue_item_ids:
            self.store.record_send(
                self.meta.id,
                queue_item_id=queue_item_id,
                print_route="slice_queue",
                slice_job_id=outcome.slice_job_id,
                project_id=project_id,
                plates=sent,
            )
        return sent
```
Run `ruff format` afterwards; it will reflow the `ensure_uploaded(...)` call. If importing `folder_for` from `projects` creates a cycle, keep the import local inside `file_to_print`. `print_run.py` imports it at module level today, so there should be no cycle.

- [ ] **Step 4: Rewrite `print_run.py` over the source**

Remove `_spool_colours` and `_record_queued` (both moved). Change `_spool_sides` to take the count instead of `meta`:
```python
async def _spool_sides(
    client: BambuddyClient,
    count: int,
    plan: FilamentPlan,
    printer_id: int,
    printer_status: PrinterStatus | None,
) -> list[SlotSide]:
    """Each chosen spool's side on ``printer_id``, for the file's own filaments (#469)."""
    own = plan.model_copy(update={"slots": [s for s in plan.slots if s.slot_id <= count]})
    assignments = await client.spool_assignments()
    return slot_sides(own, assignments, printer_status, printer_id=printer_id)
```
Replace `filament_options_for_output` with a source-generic function and a wrapper:
```python
async def filament_options(
    client: BambuddyClient,
    source: PrintSource,
    *,
    printer_id: int | None = None,
    plate_id: int = 1,
    all_plates: bool = False,
) -> FilamentOptions:
    """The filament step's whole payload for one print source (#87); the docstring of
    the old ``filament_options_for_output`` moves here unchanged."""
    read_file = await source.file_to_read(client)
    plate_ids = ((await source.plate_ids(client)) or [1]) if all_plates else [plate_id]
    read = [
        await gather_options(
            client,
            library_file_id=read_file.id,
            printer_id=printer_id,
            plate_id=plate,
            fallback_colours=list(source.colours),
            own_colours=read_file.own_colours,
        )
        for plate in plate_ids
    ]
    options = read[0] if len(read) == 1 else every_plate(read)
    if printer_id is None:
        return options
    try:
        printer_status = await client.printer_status(printer_id)
    except (ApiError, ValueError):
        logger.info("printer status unreadable; the filament step opens with no nozzles known")
        options.nozzles = []
        return options
    options.nozzles = printer_status.nozzles
    # Each loaded spool's side, so the picker can mark one whose nozzle differs (#469).
    return with_sides(options, printer_status)


async def filament_options_for_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    printer_id: int | None = None,
    plate_id: int = 1,
    all_plates: bool = False,
) -> FilamentOptions:
    return await filament_options(
        client,
        OutputSource(store, uploads, meta, settings),
        printer_id=printer_id,
        plate_id=plate_id,
        all_plates=all_plates,
    )
```
Replace `run_for_output` with `run_print` plus a wrapper. Below is the merged body (#312's upload order plus #538's extruder check) with every `meta` read replaced. **Diff it line by line against the merged file's `run_for_output`.** If the merged file differs anywhere else (a comment, an extra check), keep the merged file's version of that part. Only the source reads change.
```python
async def run_print(
    client: BambuddyClient,
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
) -> PrintRunResult:
    """<the old run_for_output docstring, unchanged>"""
    plate_ids = await source.plate_ids(client) if request.all_plates else [request.plate_id]
    if not plate_ids:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This output's 3MF lays out no plates, so there is nothing to print.",
        )
    printer_id = request.printer_id or settings.printer_id
    if printer_id is None:
        raise not_configured(
            "no printer is chosen and none is configured, so there is nothing to print on"
        )
    await _require_resolvable_printer(client, printer_id)
    choices = request.choices
    catalogue = await _catalogue(client)
    refused = choice_errors(choices, catalogue)
    if refused:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, " ".join(error.message for error in refused)
        )
    # Before anything is uploaded or sliced (#469).
    printer_status = await _read_status(client, printer_id)
    sides = await _spool_sides(
        client, source.filament_count, request.filament_plan, printer_id, printer_status
    )
    extruders = plan_extruders(
        sides,
        printer_status,
        size=choices.nozzles[0].size,
        filament_count=source.filament_count,
    )
    if extruders.errors:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, " ".join(extruders.errors))
    project_id = request.project_id or settings.last_project_id
    printed = await source.file_to_print(
        client,
        printer_id=printer_id,
        nozzle_size=choices.nozzles[0].size,
        plan=request.filament_plan,
        project_id=project_id,
    )
    library_file_id = printed.id
    print_options = resolve_print_options(
        settings,
        source.options_slug,
        printer_id,
        request_scope(request.copies, request.options),
    ).model_copy(update={"project_id": None})
    copies = print_options.quantity or 1

    spool_presets = {
        spool_id: await client.spool_filament_presets(spool_id)
        for spool_id in sorted({slot.spool_id for slot in request.filament_plan.slots})
    }
    planned: list[tuple[int, FilamentOptions, Resolved, SlicePlan]] = []
    errors: list[str] = []
    for plate_id in plate_ids:
        options = await gather_options(
            client,
            library_file_id=library_file_id,
            printer_id=printer_id,
            plate_id=plate_id,
            fallback_colours=list(source.colours),
        )
        # ... the resolve / errors / SlicePlan block, unchanged ...
    if errors:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, " ".join(errors))

    hardware = await _hardware_warnings(
        client, printer_id, choices, printer_status, printer_name=planned[0][1].printer_name
    )
    outcomes: list[QueueOutcome] = []
    sent: list[PlateSend] = []
    warnings: list[FilamentWarning] = []
    for plate_id, options, resolved, plan in planned:
        outcome = await slice_and_queue(
            client,
            library_file_id=library_file_id,
            plan=plan,
            printer_id=printer_id,
            filaments=queue_filaments(options, request.filament_plan),
            plate_id=plate_id,
            copies=copies,
            project_id=project_id,
            options=print_options,
        )
        sent = await source.record(library_file_id, plate_id, outcome, project_id, sent)
        outcomes.append(outcome)
        # ... the per-plate warnings block, unchanged ...
    # ... the low-filament across_plates block, unchanged ...
    return _queued(
        client,
        outcomes,
        library_file_id,
        project_id,
        printed.folder_id,
        copies=copies,
        warnings=warnings + hardware + extruders.warnings,
    )


async def run_for_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: PrintRunRequest,
) -> PrintRunResult:
    return await run_print(client, OutputSource(store, uploads, meta, settings), settings, request)
```
The three `# ... unchanged ...` markers stand for blocks of the merged file that you copy across untouched: the `resolve` loop body, the per-plate warnings and the `across_plates` low-filament list. They contain no `meta` reads. Add `from scadbuddy.bambuddy.print_source import OutputSource, PrintSource`, and drop the imports that are now unused (`target_for`, `ensure_uploaded`, `copy_to_read`, `folder_for`, `plates_of`, `MODEL_NAME`, `SlicedCopy`, `normalise_colour`, if nothing else in the file uses them; ruff will say).

- [ ] **Step 5: `resolve_print_options` takes no slug for a library file (send.py)**

```python
def resolve_print_options(
    settings: StoredSettings, slug: str | None, printer_id: int | None, request_scope: PrintOptions
) -> PrintOptions:
    """global → per-printer → per-model → per-request, least specific first. A library
    file (#313) has no model, so its per-model layer is empty."""
    return resolve(
        settings.print_options,
        settings.printer_print_options.get(str(printer_id)) if printer_id is not None else None,
        settings.model_print_options.get(slug) if slug is not None else None,
        request_scope,
    )
```

- [ ] **Step 6: `choices_for` in choices.py**

Rename the body of `choices_for_output` to `choices_for(client, source: PrintSource, settings, *, remembered: ModelPrintChoices | None, printer_id: int | None) -> ChoicesView`. Delete its `remembered = settings.model_print_choices.get(meta.slug)` line, since `remembered` is now the parameter. Replace the `filament_options_for_output(...)` call with `await filament_options(client, source, printer_id=printer_id)`. Everything else is unchanged. Then:
```python
async def choices_for_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    printer_id: int | None,
) -> ChoicesView:
    return await choices_for(
        client,
        OutputSource(store, uploads, meta, settings),
        settings,
        remembered=settings.model_print_choices.get(meta.slug),
        printer_id=printer_id,
    )
```
Update the imports: `from scadbuddy.bambuddy.print_run import BED_TYPES, filament_options` and `from scadbuddy.bambuddy.print_source import OutputSource, PrintSource`. Add `"choices_for"` to `__all__`.

- [ ] **Step 7: Run the new test and the whole backend gate (DB env)**

Expected: PASS. In particular, every existing print suite must be green with no test edits: `tests/api/test_print_run_choices.py`, `test_print_plates.py`, `test_print_choices.py`, `test_print_filaments.py`, `test_library_copies.py` and `test_print_options*.py`. That is the proof this task changed no behavior.

- [ ] **Step 8: Commit**

```bash
git add backend/scadbuddy/bambuddy/print_source.py backend/scadbuddy/bambuddy/print_run.py backend/scadbuddy/bambuddy/choices.py backend/scadbuddy/bambuddy/send.py backend/tests/bambuddy/test_print_source.py
git commit -m "refactor(print): read what differs between prints through a PrintSource (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: LibrarySource

**Files:**
- Modify: `backend/scadbuddy/bambuddy/print_source.py` (append)
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (append two wrappers)
- Test: `backend/tests/bambuddy/test_print_source.py` (append)

**Interfaces:**
- Consumes: `BambuddyClient.library_file`, `library_plates` (Task 2) and `filament_requirements`.
- Produces, in `print_source`: `PRINTABLE_TYPES: frozenset[str] = frozenset({"3mf"})`, `SLICED_TYPE = "gcode.3mf"`, `UNKNOWN_COLOUR = ""` and `printable(file_type: str | None) -> bool`. Also `LibrarySource(file_id: int, colours: list[str], plates: list[int])` (frozen dataclass) with `async classmethod load(client, file_id) -> LibrarySource`.
- Produces, in `print_run`: `async run_for_library(client, settings, file_id: int, request: PrintRunRequest) -> PrintRunResult` and `async filament_options_for_library(client, file_id: int, *, printer_id=None, plate_id=1, all_plates=False) -> FilamentOptions`.

- [ ] **Step 1: Write the failing tests (append)**

```python
import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.print_source import UNKNOWN_COLOUR, LibrarySource
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


def _file(file_id: int, file_type: str) -> None:
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200, json={"id": file_id, "filename": f"f{file_id}.{file_type}", "file_type": file_type}
        )
    )


@respx.mock
async def test_a_library_file_is_its_plates_and_its_filaments(bambuddy: BambuddyClient) -> None:
    _file(67, "3mf")
    respx.get(f"{API}/library/files/67/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-multi.json"))
    )
    respx.get(f"{API}/library/files/67/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements.json"))
    )

    source = await LibrarySource.load(bambuddy, 67)

    assert await source.plate_ids(bambuddy) == [1, 2]
    assert source.colours == ["#0047BB", "#FF1493"]
    assert source.filament_count == 2
    assert source.options_slug is None
    assert (await source.file_to_read(bambuddy)).id == 67
    printed = await source.file_to_print(
        bambuddy, printer_id=1, nozzle_size="0.4", plan=None, project_id=5  # type: ignore[arg-type]
    )
    assert (printed.id, printed.folder_id) == (67, None)
    # Nothing is uploaded, replated or recolored: the only calls were the three reads.
    assert {call.request.method for call in respx.calls} == {"GET"}


@respx.mock
async def test_a_file_with_no_plates_or_filaments_is_one_plate_one_filament(
    bambuddy: BambuddyClient,
) -> None:
    _file(70, "3mf")
    respx.get(f"{API}/library/files/70/plates").mock(
        return_value=httpx.Response(200, json={**recording("library-plates-stl.json"), "file_id": 70})
    )
    respx.get(f"{API}/library/files/70/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )

    source = await LibrarySource.load(bambuddy, 70)

    assert await source.plate_ids(bambuddy) == [1]
    assert source.colours == [UNKNOWN_COLOUR]
    assert source.filament_count == 1


@respx.mock
@pytest.mark.parametrize("file_type", ["gcode.3mf", "stl"])
async def test_a_file_the_dialog_cannot_print_is_a_422(
    bambuddy: BambuddyClient, file_type: str
) -> None:
    _file(104, file_type)

    with pytest.raises(ApiError) as refused:
        await LibrarySource.load(bambuddy, 104)

    assert refused.value.status_code == 422
    assert f"f104.{file_type}" in refused.value.detail


@respx.mock
async def test_a_file_deleted_in_bambuddy_is_a_404(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )

    with pytest.raises(ApiError) as missing:
        await LibrarySource.load(bambuddy, 89)

    assert missing.value.status_code == 404
```
(Check `ApiError`'s attribute names in `scadbuddy/core/problems.py`. If they are `status`/`detail` rather than `status_code`/`detail`, use those.)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_print_source.py -v`
Expected: FAIL with `ImportError: cannot import name 'LibrarySource'`.

- [ ] **Step 3: Implement (append to `print_source.py`)**

```python
from fastapi import status

from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.core.problems import ApiError

#: The ``file_type`` values the dialog prints from the library (spec 2026-09-28 §2).
PRINTABLE_TYPES: frozenset[str] = frozenset({"3mf"})
#: A sliced file: printed from Bambuddy directly, never through the dialog.
SLICED_TYPE = "gcode.3mf"
#: The colour of the one filament of a file Bambuddy reads none from (an STL, a 3MF
#: without slice metadata). ``normalise_colour`` reads it as unknown.
UNKNOWN_COLOUR = ""


def printable(file_type: str | None) -> bool:
    return (file_type or "") in PRINTABLE_TYPES


def _refusal(file: LibraryFile) -> str:
    if file.file_type == SLICED_TYPE:
        return f"{file.filename} is sliced already. Print it from Bambuddy."
    kind = file.file_type or "file of unknown type"
    return f"ScadBuddy prints only 3MF files from the library, and {file.filename} is a {kind}."


@dataclass(frozen=True)
class LibrarySource:
    """A file already in Bambuddy's library (#313), printed as its author left it:
    never uploaded, replated or recolored, and recorded nowhere in ScadBuddy."""

    file_id: int
    colours: list[str]
    plates: list[int]
    options_slug: str | None = None

    @classmethod
    async def load(cls, client: BambuddyClient, file_id: int) -> LibrarySource:
        """Read the file, its plates and its filaments. A file deleted in Bambuddy is
        its 404; one the dialog cannot print is a 422 before anything else is read."""
        file = await client.library_file(file_id)
        if not printable(file.file_type):
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, _refusal(file))
        plates = sorted(plate.index for plate in (await client.library_plates(file_id)).plates)
        needs = (await client.filament_requirements(file_id)).filaments
        colours = [UNKNOWN_COLOUR] * max((need.slot_id for need in needs), default=0)
        for need in needs:
            colours[need.slot_id - 1] = need.color or UNKNOWN_COLOUR
        # No plate metadata is one plate, and no filaments is one of unknown colour: a
        # file laid out that way still has something on the bed to print.
        return cls(file_id=file_id, colours=colours or [UNKNOWN_COLOUR], plates=plates or [1])

    @property
    def filament_count(self) -> int:
        return len(self.colours)

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return list(self.plates)

    async def file_to_read(self, client: BambuddyClient) -> ReadFile:
        return ReadFile(self.file_id)

    async def file_to_print(
        self,
        client: BambuddyClient,
        *,
        printer_id: int,
        nozzle_size: str,
        plan: FilamentPlan,
        project_id: int | None,
    ) -> PrintFile:
        return PrintFile(self.file_id)

    async def record(
        self,
        library_file_id: int,
        plate_id: int,
        outcome: QueueOutcome,
        project_id: int | None,
        sent: list[PlateSend],
    ) -> list[PlateSend]:
        # Recorded nowhere in ScadBuddy: Bambuddy's queue and archives are the record
        # (print history is #305).
        return sent
```
Move the new imports to the top of the module. Note that `filament_requirements` colours come as `#RRGGBBAA`. The resolver and `_requirements` normalise them, and the test asserts the recorded `#RRGGBB` values of `filament-requirements.json`.

Append to `print_run.py`:
```python
async def run_for_library(
    client: BambuddyClient, settings: StoredSettings, file_id: int, request: PrintRunRequest
) -> PrintRunResult:
    """Resolve, slice and queue a file already in Bambuddy's library (#313)."""
    return await run_print(client, await LibrarySource.load(client, file_id), settings, request)


async def filament_options_for_library(
    client: BambuddyClient,
    file_id: int,
    *,
    printer_id: int | None = None,
    plate_id: int = 1,
    all_plates: bool = False,
) -> FilamentOptions:
    return await filament_options(
        client,
        await LibrarySource.load(client, file_id),
        printer_id=printer_id,
        plate_id=plate_id,
        all_plates=all_plates,
    )
```

- [ ] **Step 4: Run the tests and the backend gate**

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/bambuddy/print_source.py backend/scadbuddy/bambuddy/print_run.py backend/tests/bambuddy/test_print_source.py
git commit -m "feat(print): print a Bambuddy library file as its author left it (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The `/print/library` routes, and their agent coverage

**Files:**
- Create: `backend/scadbuddy/bambuddy/library_listing.py`
- Create: `backend/scadbuddy/api/library_print.py`
- Test: `backend/tests/api/test_print_library.py`
- Modify: `agent/src/tools/coverage.ts` (`NOT_A_TOOL`)

**Interfaces:**
- Consumes: `client.folders()`, `client.library_files` and `client.library_plates` (Task 2); `SettingsStore.library_choices` and `set_library_choices` (Task 3); `choices_for` and `LibrarySource` (Tasks 4 and 5); `run_for_library` and `filament_options_for_library` (Task 5); `_proxy` and `MEDIA_RESPONSES` (api/prints.py); `OutputPlate` (api/outputs.py).
- Produces the routes (all `/api/v1` prefixed):
  - `GET /print/library?folder_id=&all=` → `LibraryListing`
  - `GET /print/library/{file_id}/plates` → `list[OutputPlate]`
  - `GET /print/library/{file_id}/thumbnail` → image bytes
  - `GET /print/library/{file_id}/plates/{index}/thumbnail` → image bytes
  - `GET /print/library/{file_id}/choices?printer_id=` → `ChoicesView` (`model_choices` carries the file's remembered choices)
  - `PUT /print/library/{file_id}/choices` → body and answer `ModelPrintChoices`
  - `GET /print/library/{file_id}/filaments?printer_id=&plate_id=&all_plates=` → `FilamentOptions`
  - `POST /print/library/{file_id}/run` → body `PrintRunRequest`, answer `PrintRunResult`
- Produces the schema names the frontend aliases: `LibraryListing`, `LibraryEntry` and `LibraryFolderView`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/api/test_print_library.py`:
```python
"""#313 — printing a file already in Bambuddy's library through the Print dialog."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import body, run_routes
from tests.api.test_send import BASE, configure
from tests.bambuddy.conftest import recording

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"


def library_file(file_id: int = 89, *, file_type: str = "3mf", plates: str = "library-plates-single.json") -> None:
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200,
            json={"id": file_id, "filename": f"file-{file_id}.{file_type}", "file_type": file_type},
        )
    )
    respx.get(f"{API}/library/files/{file_id}/plates").mock(
        return_value=httpx.Response(200, json={**recording(plates), "file_id": file_id})
    )


def listing_routes(files: Any) -> respx.Route:
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=recording("library-folders-nested.json"))
    )
    return respx.get(f"{API}/library/files/").mock(return_value=httpx.Response(200, json=files))


@respx.mock
def test_the_root_lists_unsliced_3mfs_and_advanced_lists_every_file(client: TestClient) -> None:
    configure(client)
    listing_routes(recording("library-files-root.json"))

    plain = client.get("/api/v1/print/library").json()
    every = client.get("/api/v1/print/library", params={"all": "true"}).json()

    assert plain["files"] and {row["file_type"] for row in plain["files"]} == {"3mf"}
    assert all(row["printable"] for row in plain["files"])
    sliced = [row for row in every["files"] if row["file_type"] == "gcode.3mf"]
    assert sliced and not any(row["printable"] for row in sliced)
    assert plain["hidden"] == len(every["files"]) - len(plain["files"])
    # The folder tree arrives flattened, each with its depth.
    assert {"Supplies": 0, "Storage": 1}.items() <= {
        row["name"]: row["depth"] for row in plain["folders"]
    }.items()


@respx.mock
def test_an_stl_is_listed_under_advanced_without_print(client: TestClient) -> None:
    configure(client)
    listing_routes(recording("library-files-folder.json"))

    every = client.get("/api/v1/print/library", params={"folder_id": 4, "all": "true"}).json()

    stls = [row for row in every["files"] if row["file_type"] == "stl"]
    assert stls and not any(row["printable"] for row in stls)


@respx.mock
def test_a_folder_of_hundreds_of_files_is_one_read(client: TestClient) -> None:
    configure(client)
    rows = [
        {"id": 1000 + n, "filename": f"part-{n}.{'3mf' if n % 2 else 'gcode.3mf'}",
         "file_type": "3mf" if n % 2 else "gcode.3mf", "folder_id": 9, "file_size": 1,
         "print_count": 0, "created_at": "2026-09-28T00:00:00"}
        for n in range(500)
    ]
    files = listing_routes(rows)

    plain = client.get("/api/v1/print/library", params={"folder_id": 9}).json()
    every = client.get("/api/v1/print/library", params={"folder_id": 9, "all": "true"}).json()

    assert (len(plain["files"]), plain["hidden"], len(every["files"])) == (250, 250, 500)
    assert files.call_count == 2  # one read per listing, never one per file
    assert files.calls.last.request.url.params["folder_id"] == "9"


@respx.mock
def test_a_library_file_is_sliced_as_it_stands_and_queued(client: TestClient) -> None:
    configure(client)
    library_file(89)
    run_routes()
    sliced = slice_routes()
    queued = queue_route()
    upload = respx.post(f"{API}/library/files")

    response = client.post("/api/v1/print/library/89/run", json=body())

    assert response.status_code == 200, response.text
    assert response.json()["library_file_id"] == 89
    assert response.json()["folder_id"] is None
    assert "/library/files/89/slice" in str(sliced.calls.last.request.url)
    assert json.loads(queued.calls.last.request.content)["printer_id"] == 1
    assert not upload.called


@respx.mock
def test_a_file_deleted_in_bambuddy_is_a_404_with_nothing_sliced(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )
    run_routes()
    sliced = slice_routes()
    queued = queue_route()

    response = client.post("/api/v1/print/library/89/run", json=body())

    assert response.status_code == 404, response.text
    assert response.json()["type"].endswith("/bambuddy-not-found")
    assert not sliced.called and not queued.called


@respx.mock
def test_a_sliced_file_is_a_422_before_anything_is_sliced(client: TestClient) -> None:
    configure(client)
    library_file(104, file_type="gcode.3mf")
    run_routes()
    sliced = slice_routes()

    run = client.post("/api/v1/print/library/104/run", json=body())
    choices = client.get("/api/v1/print/library/104/choices")

    assert run.status_code == 422, run.text
    assert "sliced already" in run.json()["detail"]
    assert choices.status_code == 422
    assert not sliced.called


@respx.mock
def test_a_file_with_no_plate_metadata_prints_plate_one(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/70/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )
    library_file(70, plates="library-plates-stl.json")
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = client.post(
        "/api/v1/print/library/70/run",
        json={**body(), "all_plates": True, "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    assert sliced.call_count == 1
    sent = json.loads(sliced.calls.last.request.content)
    assert sent["plate"] == 1 and len(sent["filament_presets"]) == 1
    assert client.get("/api/v1/print/library/70/plates").json() == []


@respx.mock
def test_the_nozzle_refusals_apply_to_a_library_file(client: TestClient) -> None:
    """#469 on a library file: two filaments, a 0.2 on the right and a 0.4 on the left."""
    configure(client)
    library_file(89)
    run_routes()
    sliced = slice_routes()

    response = client.post(
        "/api/v1/print/library/89/run",
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 10}]}},
    )

    assert response.status_code == 422, response.text
    assert "The slicer spreads a multi-color print across both" in response.json()["detail"]
    assert not sliced.called


@respx.mock
def test_the_choices_are_remembered_per_library_file(client: TestClient) -> None:
    configure(client)
    library_file(89)
    library_file(67, plates="library-plates-multi.json")
    run_routes()
    remembered = {"printer_id": 1, "filament_plan": [{"slot_id": 1, "spool_id": 9}],
                  "nozzles": [{"size": "0.2"}], "tier": "fine"}

    put = client.put("/api/v1/print/library/89/choices", json=remembered)
    own = client.get("/api/v1/print/library/89/choices").json()["model_choices"]
    other = client.get("/api/v1/print/library/67/choices").json()["model_choices"]

    assert put.status_code == 200, put.text
    assert own["tier"] == "fine" and [n["size"] for n in own["nozzles"]] == ["0.2", "0.2"]
    assert other["tier"] is None and other["nozzles"] == []


@respx.mock
def test_a_remembered_printer_that_is_gone_falls_through(client: TestClient) -> None:
    configure(client)
    library_file(89)
    run_routes()
    client.put("/api/v1/print/library/89/choices", json={"printer_id": 99})

    choices = client.get("/api/v1/print/library/89/choices")

    assert choices.status_code == 200, choices.text
    assert choices.json()["printer_id"] == 1
```
The nozzle-refusal test relies on `run_routes()` mocking `printer-status-rack.json` (right 0.2, left 0.4) and `body()` choosing 0.2, which is exactly #538's own `test_a_multi_color_print_on_differing_nozzles_is_a_422_before_upload`. The generic `filament-requirements.json` regex (two filaments) comes from `inventory_routes`. For the no-metadata test, the specific requirements route is registered **before** `run_routes()`, so it wins over that regex.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/scadbuddy_test uv run --frozen pytest tests/api/test_print_library.py -v`
Expected: FAIL with 404s: no route matches `/api/v1/print/library`.

- [ ] **Step 3: The listing (`bambuddy/library_listing.py`)**

```python
"""The Library page's listing (#313): Bambuddy's folder tree and one folder's files."""

from __future__ import annotations

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import Folder, LibraryListRow
from scadbuddy.bambuddy.print_source import printable

#: What the page lists without Advanced: unsliced 3MFs (spec 2026-09-28 §2).
DEFAULT_TYPES: frozenset[str] = frozenset({"3mf"})


class LibraryFolderView(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    #: How deep the folder sits, 0 at the top; the page indents by it.
    depth: int = 0
    file_count: int | None = None


class LibraryEntry(BaseModel):
    id: int
    filename: str
    file_type: str
    folder_id: int | None = None
    has_thumbnail: bool = False
    print_count: int = 0
    #: Whether the dialog prints it; a sliced file is printed from Bambuddy directly.
    printable: bool


class LibraryListing(BaseModel):
    folder_id: int | None = None
    all: bool = False
    folders: list[LibraryFolderView] = Field(default_factory=list)
    files: list[LibraryEntry] = Field(default_factory=list)
    #: This folder's files that only Advanced lists.
    hidden: int = 0


def _flatten(folders: list[Folder], depth: int = 0) -> list[LibraryFolderView]:
    out: list[LibraryFolderView] = []
    for folder in folders:
        out.append(
            LibraryFolderView(
                id=folder.id,
                name=folder.name,
                parent_id=folder.parent_id,
                depth=depth,
                file_count=folder.file_count,
            )
        )
        out.extend(_flatten(folder.children, depth + 1))
    return out


def _entry(row: LibraryListRow) -> LibraryEntry:
    return LibraryEntry(
        id=row.id,
        filename=row.filename,
        file_type=row.file_type,
        folder_id=row.folder_id,
        has_thumbnail=row.thumbnail_path is not None,
        print_count=row.print_count,
        printable=printable(row.file_type),
    )


async def list_library(
    client: BambuddyClient, *, folder_id: int | None, show_all: bool
) -> LibraryListing:
    """One read of the tree and one of the folder, however many files it holds."""
    folders = _flatten(await client.folders())
    here = [row for row in await client.library_files(folder_id=folder_id) if row.folder_id == folder_id]
    shown = [row for row in here if show_all or row.file_type in DEFAULT_TYPES]
    return LibraryListing(
        folder_id=folder_id,
        all=show_all,
        folders=folders,
        files=[_entry(row) for row in sorted(shown, key=lambda row: row.filename.casefold())],
        hidden=len(here) - len(shown),
    )
```

- [ ] **Step 4: The routes (`api/library_print.py`)**

```python
"""``/api/v1/print/library/…`` — printing a file already in Bambuddy's library (#313).

The same dialog as an output's (``printing.py``): its choices, filament step and run,
over :class:`~scadbuddy.bambuddy.print_source.LibrarySource`. Nothing is uploaded, and
nothing is recorded in ScadBuddy; the images are proxied so the API key never reaches
the browser.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import StreamingResponse

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.outputs import OutputPlate
from scadbuddy.api.prints import MEDIA_RESPONSES, _proxy
from scadbuddy.bambuddy.choices import ChoicesView, choices_for
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.library_listing import LibraryListing, list_library
from scadbuddy.bambuddy.print_run import (
    PrintRunRequest,
    PrintRunResult,
    filament_options_for_library,
    run_for_library,
)
from scadbuddy.bambuddy.print_source import LibrarySource
from scadbuddy.library.settings_store import ModelPrintChoices

router = APIRouter(prefix="/print/library", tags=["print"])

FileIdPath = Annotated[int, Path(ge=1)]


@router.get("", response_model=LibraryListing, summary="Bambuddy's library, one folder at a time")
async def get_library(
    store: SettingsStoreDep,
    folder_id: Annotated[int | None, Query()] = None,
    show_all: Annotated[bool, Query(alias="all")] = False,
) -> LibraryListing:
    """The folder tree and one folder's files (the root's without ``folder_id``).
    Without ``all`` only unsliced 3MFs; with it every file, each flagged ``printable``."""
    async with client_for(store.load()) as client:
        return await list_library(client, folder_id=folder_id, show_all=show_all)


@router.get("/{file_id}/plates", response_model=list[OutputPlate], summary="The library file's plates")
async def get_library_plates(file_id: FileIdPath, store: SettingsStoreDep) -> list[OutputPlate]:
    """What the dialog offers as ``plate_id``; empty when Bambuddy reads none (an STL,
    or a 3MF with no plate metadata), which prints as plate 1."""
    async with client_for(store.load()) as client:
        plates = await client.library_plates(file_id)
    return [OutputPlate(index=plate.index, has_thumbnail=plate.has_thumbnail) for plate in plates.plates]


@router.get(
    "/{file_id}/thumbnail",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="The library file's thumbnail",
)
async def get_library_thumbnail(
    file_id: FileIdPath, request: Request, store: SettingsStoreDep
) -> StreamingResponse:
    return await _proxy(
        store, request, f"/library/files/{file_id}/thumbnail", what="show the file's thumbnail"
    )


@router.get(
    "/{file_id}/plates/{index}/thumbnail",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="One plate's image of a library file",
)
async def get_library_plate_thumbnail(
    file_id: FileIdPath,
    index: Annotated[int, Path(ge=1)],
    request: Request,
    store: SettingsStoreDep,
) -> StreamingResponse:
    return await _proxy(
        store,
        request,
        f"/library/files/{file_id}/plate-thumbnail/{index}",
        what="show the plate image",
    )


@router.get("/{file_id}/choices", response_model=ChoicesView, summary="What the print dialog offers for this library file")
async def get_library_choices(
    file_id: FileIdPath,
    store: SettingsStoreDep,
    printer_id: Annotated[int | None, Query()] = None,
) -> ChoicesView:
    """As ``/print/outputs/{id}/choices``; ``model_choices`` is what this file last
    printed with, so the dialog reopens on it."""
    settings = store.load()
    remembered = store.library_choices(file_id)
    async with client_for(settings) as client:
        source = await LibrarySource.load(client, file_id)
        return await choices_for(client, source, settings, remembered=remembered, printer_id=printer_id)


@router.put("/{file_id}/choices", response_model=ModelPrintChoices, summary="Remember this library file's choices")
def put_library_choices(
    file_id: FileIdPath, body: ModelPrintChoices, store: SettingsStoreDep
) -> ModelPrintChoices:
    """Replaces this file's entry whole; an empty body forgets it. Needs no Bambuddy."""
    return store.set_library_choices(file_id, body)


@router.get("/{file_id}/filaments", response_model=FilamentOptions, summary="Spools that can print this library file")
async def get_library_filaments(
    file_id: FileIdPath,
    store: SettingsStoreDep,
    printer_id: Annotated[int | None, Query()] = None,
    plate_id: Annotated[int, Query(ge=1)] = 1,
    all_plates: Annotated[bool, Query()] = False,
) -> FilamentOptions:
    async with client_for(store.load()) as client:
        return await filament_options_for_library(
            client, file_id, printer_id=printer_id, plate_id=plate_id, all_plates=all_plates
        )


@router.post("/{file_id}/run", response_model=PrintRunResult, summary="Slice this library file with the dialog's choices and queue it")
async def post_library_run(
    file_id: FileIdPath, body: PrintRunRequest, store: SettingsStoreDep
) -> PrintRunResult:
    """As ``/print/outputs/{id}/run``, on the file as it stands in Bambuddy. A sliced
    file is a 422, and a file deleted in Bambuddy is its 404, both before any slice."""
    settings = store.load()
    async with client_for(settings) as client:
        return await run_for_library(client, settings, file_id, body)
```
If `tests/api/test_routes.py::test_no_two_modules_match_the_same_request` flags the `""` path, use `"/"`. The test asserts no overlap with `printing.py`'s `/print/...` routes, and none of the paths above repeats one.

- [ ] **Step 5: Run the tests and the backend gate (DB env)**

Expected: PASS, including `tests/api/test_openapi.py` and `tests/api/test_routes.py`.

- [ ] **Step 6: Agent coverage**

In `agent/src/tools/coverage.ts`, add above the `NOT_A_TOOL` array:
```ts
const LIBRARY_PRINT_LATER =
  'Printing a file already in Bambuddy\'s library (#313) lands UI-first; an agent tool for it is a ' +
  'follow-up (spec 2026-09-28 §6). A run slices and queues a real print, so the tool must go through ' +
  'the outward approval flow (AI spec §8.2) when it is written.'
```
and append to `NOT_A_TOOL`:
```ts
  ...(
    [
      'GET /api/v1/print/library',
      'GET /api/v1/print/library/{file_id}/plates',
      'GET /api/v1/print/library/{file_id}/choices',
      'PUT /api/v1/print/library/{file_id}/choices',
      'GET /api/v1/print/library/{file_id}/filaments',
      'POST /api/v1/print/library/{file_id}/run',
    ] as const
  ).map((operation) => ({ operation, reason: LIBRARY_PRINT_LATER })),
  ...(
    [
      'GET /api/v1/print/library/{file_id}/thumbnail',
      'GET /api/v1/print/library/{file_id}/plates/{index}/thumbnail',
    ] as const
  ).map((operation) => ({
    operation,
    reason: "Serves Bambuddy's image of a library file to the browser; an agent has no use for the bytes (#313).",
  })),
```
(`PENDING_ROUTES` is not used: `test/coverage.test.ts` fails for a pending operation that is present in `backend/openapi.json`, and these are present as soon as this task lands.)

Run: `cd agent && pnpm typecheck && pnpm exec vitest run test/coverage.test.ts`, then the full agent gate.
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/bambuddy/library_listing.py backend/scadbuddy/api/library_print.py backend/tests/api/test_print_library.py agent/src/tools/coverage.ts
git commit -m "feat(print): list Bambuddy's library and print a library file (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: STL, per the probe (conditional)

Read the `STL_PRINTABLE` line that Task 1 wrote in `backend/tests/bambuddy/recordings/README.md`.

**If `STL_PRINTABLE = no`:** make no code change. The default path already lists STLs without Print, and Tasks 5 and 6 pin that (`test_a_file_the_dialog_cannot_print_is_a_422[stl]` and `test_an_stl_is_listed_under_advanced_without_print`). Skip to Step 5 and commit nothing. Record in the PR body that STL stays unprintable, and why (the probe's error).

**If `STL_PRINTABLE = yes`:**

**Files:**
- Modify: `backend/scadbuddy/bambuddy/print_source.py` (`PRINTABLE_TYPES`)
- Modify: `backend/tests/bambuddy/test_print_source.py` and `backend/tests/api/test_print_library.py`

**Interfaces:**
- Consumes: `PRINTABLE_TYPES` (Task 5), `DEFAULT_TYPES` (Task 6), which stays `{"3mf"}` so STL is still listed only under Advanced.

- [ ] **Step 1: Flip the tests first**

In `test_print_source.py`, change the parametrize of `test_a_file_the_dialog_cannot_print_is_a_422` to `["gcode.3mf"]` and add:
```python
@respx.mock
async def test_an_stl_is_one_plate_of_one_filament(bambuddy: BambuddyClient) -> None:
    _file(46, "stl")
    respx.get(f"{API}/library/files/46/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-stl.json"))
    )
    respx.get(f"{API}/library/files/46/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )

    source = await LibrarySource.load(bambuddy, 46)

    assert (await source.plate_ids(bambuddy), source.filament_count) == ([1], 1)
```
In `test_print_library.py`, rename `test_an_stl_is_listed_under_advanced_without_print` to `test_an_stl_is_listed_under_advanced_with_print`, and change its last assertion to `assert stls and all(row["printable"] for row in stls)`. Add:
```python
@respx.mock
def test_an_stl_slices_as_one_plate(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/46/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )
    library_file(46, file_type="stl", plates="library-plates-stl.json")
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = client.post(
        "/api/v1/print/library/46/run",
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["plate"] == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=... uv run --frozen pytest tests/bambuddy/test_print_source.py tests/api/test_print_library.py -v`
Expected: the new STL tests fail with a 422 ("ScadBuddy prints only 3MF files").

- [ ] **Step 3: Enable STL**

```python
#: The ``file_type`` values the dialog prints from the library (spec 2026-09-28 §2). An
#: STL is one plate of one filament; Bambuddy's slice route takes it (the #313 probe,
#: recordings/README.md).
PRINTABLE_TYPES: frozenset[str] = frozenset({"3mf", "stl"})
```
and change `_refusal`'s second message to `f"ScadBuddy prints only 3MF and STL files from the library, and {file.filename} is a {kind}."`.

- [ ] **Step 4: Run the backend gate**

Expected: PASS.

- [ ] **Step 5: Commit (only if Step 3 was done)**

```bash
git add backend/scadbuddy/bambuddy/print_source.py backend/tests/bambuddy/test_print_source.py backend/tests/api/test_print_library.py
git commit -m "feat(print): print an STL from the library as one plate (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Frontend API calls, types and msw mocks for the library routes

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`
- Create: `frontend/src/mocks/library.ts`
- Modify: `frontend/src/mocks/handlers.ts` (state, reset and handlers after the `/print/outputs/:id/progress` handler)
- Test: `frontend/src/mocks/handlers.test.ts` (append a `describe('library print')`)

**Interfaces:**
- Consumes: the routes from Task 6.
- Produces:
  - Types `LibraryListing`, `LibraryEntry` and `LibraryFolderView`.
  - `api.listLibrary({folderId: number | null, all: boolean}): Promise<LibraryListing>`
  - `api.getLibraryPlates(fileId: number): Promise<OutputPlate[]>`
  - `api.libraryThumbnailUrl(fileId: number): string` and `api.libraryPlateThumbnailUrl(fileId: number, index: number): string`
  - `api.getLibraryChoices(fileId: number, printerId?: number | null): Promise<ChoicesView>`
  - `api.getLibraryFilaments(fileId: number, query?: {printerId?, plateId?, allPlates?}): Promise<FilamentOptions>`
  - `api.runLibraryPrint(fileId: number, body: PrintRunRequest): Promise<PrintRunResult>`
  - `api.putLibraryChoices(fileId: number, body: ModelPrintChoices): Promise<ModelPrintChoices>`
  - Mock fixtures `libraryFolders` and `libraryFiles` (`mocks/library.ts`) with ids 89 (root 3mf), 104 (root gcode.3mf), 67 (folder 1, two plates), 46 (folder 4 stl), and folder 9 "Bulk" of 300 3mf files, ids 2000–2299.

- [ ] **Step 1: Write the failing tests (append to `handlers.test.ts`)**

```ts
describe('library print', () => {
  beforeEach(() => resetMockState())

  it('lists the root 3MFs, and every file under all', async () => {
    const plain = await api.listLibrary({ folderId: null, all: false })
    const every = await api.listLibrary({ folderId: null, all: true })
    expect(plain.files?.map((file) => file.id)).toEqual([89])
    expect(every.files?.find((file) => file.id === 104)?.printable).toBe(false)
    expect(plain.hidden).toBe(1)
  })

  it('remembers the choices per file', async () => {
    await api.putLibraryChoices(89, { printer_id: 1, filament_plan: [], nozzles: [{ size: '0.2', flow: 'standard' }, { size: '0.2', flow: 'standard' }] })
    expect((await api.getLibraryChoices(89)).model_choices?.nozzles?.[0]?.size).toBe('0.2')
    expect((await api.getLibraryChoices(67)).model_choices?.nozzles ?? []).toEqual([])
  })

  it('refuses a sliced file and a missing one', async () => {
    await expect(api.runLibraryPrint(104, runBody)).rejects.toMatchObject({ status: 422 })
    await expect(api.runLibraryPrint(999, runBody)).rejects.toMatchObject({ status: 404 })
  })
})
```
Define `runBody: PrintRunRequest` at the top of that describe block, the same way the file's existing output-run tests build theirs. Copy their shape, e.g. `{ printer_id: 1, filament_plan: { slots: [], force_colour_match: false }, choices: { nozzles: DEFAULT_NOZZLES, tier: 'standard', process_name: null, bed_type: 'Textured PEI Plate', filament_overrides: {} } }`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/mocks/handlers.test.ts`
Expected: FAIL. TypeScript first reports `api.listLibrary` doesn't exist (vitest runs `gen:api` via `pnpm test`; `vitest run` alone needs `pnpm gen:api` once).

- [ ] **Step 3: Types and client**

`src/api/types.ts`:
```ts
/** #313 — the Library page's listing, and one row of it. */
export type LibraryListing = Schemas['LibraryListing']
export type LibraryEntry = Schemas['LibraryEntry']
export type LibraryFolderView = Schemas['LibraryFolderView']
```
`src/api/client.ts` (add the three types to the import; place after `getPrintProgress`):
```ts
  /** #313 — Bambuddy's folder tree and one folder's files; `all` adds sliced files and STLs. */
  listLibrary: (query: { folderId: number | null; all: boolean }) => {
    const search = new URLSearchParams()
    if (query.folderId !== null) search.set('folder_id', String(query.folderId))
    if (query.all) search.set('all', 'true')
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<LibraryListing>(`/print/library${suffix}`)
  },

  libraryThumbnailUrl: (fileId: number) => `${API_BASE}/print/library/${fileId}/thumbnail`,

  libraryPlateThumbnailUrl: (fileId: number, index: number) =>
    `${API_BASE}/print/library/${fileId}/plates/${index}/thumbnail`,

  getLibraryPlates: (fileId: number) => request<OutputPlate[]>(`/print/library/${fileId}/plates`),

  getLibraryChoices: (fileId: number, printerId?: number | null) => {
    const search = new URLSearchParams()
    if (printerId !== null && printerId !== undefined) search.set('printer_id', String(printerId))
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<ChoicesView>(`/print/library/${fileId}/choices${suffix}`)
  },

  getLibraryFilaments: (
    fileId: number,
    query: { printerId?: number | null; plateId?: number; allPlates?: boolean } = {},
  ) => {
    const search = new URLSearchParams()
    if (query.printerId !== null && query.printerId !== undefined) {
      search.set('printer_id', String(query.printerId))
    }
    if (query.plateId !== undefined) search.set('plate_id', String(query.plateId))
    if (query.allPlates) search.set('all_plates', 'true')
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<FilamentOptions>(`/print/library/${fileId}/filaments${suffix}`)
  },

  runLibraryPrint: (fileId: number, body: PrintRunRequest) =>
    request<PrintRunResult>(`/print/library/${fileId}/run`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  putLibraryChoices: (fileId: number, body: ModelPrintChoices) =>
    request<ModelPrintChoices>(`/print/library/${fileId}/choices`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
```

- [ ] **Step 4: Fixtures (`src/mocks/library.ts`)**

```ts
import type { LibraryEntry, LibraryFolderView } from '../api/types'

/** #313 — the mocked Bambuddy library, shaped like tests/bambuddy/recordings. */
export const libraryFolders: LibraryFolderView[] = [
  { id: 1, name: 'MakerWorld', parent_id: null, depth: 0, file_count: 1 },
  { id: 3, name: 'Supplies', parent_id: null, depth: 0, file_count: 0 },
  { id: 4, name: 'Storage', parent_id: 3, depth: 1, file_count: 1 },
  { id: 9, name: 'Bulk', parent_id: null, depth: 0, file_count: 300 },
]

function entry(id: number, filename: string, fileType: string, folderId: number | null): LibraryEntry {
  return {
    id,
    filename,
    file_type: fileType,
    folder_id: folderId,
    has_thumbnail: fileType !== 'stl',
    print_count: 0,
    printable: fileType === '3mf',
  }
}

export const libraryFiles: LibraryEntry[] = [
  entry(89, 'bag-clip.3mf', '3mf', null),
  entry(104, 'bag-clip.gcode.3mf', 'gcode.3mf', null),
  entry(67, "Clara's Wand.3mf", '3mf', 1),
  entry(46, 'Desiccant_Box.stl', 'stl', 4),
  ...Array.from({ length: 300 }, (_, n) => entry(2000 + n, `part-${n}.3mf`, '3mf', 9)),
]

/** The two-plate file; every other 3MF is one plate and an STL none. */
export const MULTI_PLATE_FILE = 67
```
If Task 7 enabled STL, change `printable` to `fileType === '3mf' || fileType === 'stl'`.

- [ ] **Step 5: Handlers (`src/mocks/handlers.ts`)**

Add `libraryChoices: {} as Record<string, ModelPrintChoices>,` to `state`, and `state.libraryChoices = {}` to `resetMockState`. Import `LibraryListing` in the types import and `{ libraryFiles, libraryFolders, MULTI_PLATE_FILE }` from `./library`. Then add after the progress handler:
```ts
  // --- #313: printing a file already in Bambuddy's library ---------------------------

  http.get(`${base}/print/library`, ({ request }) => {
    const search = new URL(request.url).searchParams
    const asked = search.get('folder_id')
    const folderId = asked === null ? null : Number(asked)
    const all = search.get('all') === 'true'
    const here = libraryFiles.filter((file) => (file.folder_id ?? null) === folderId)
    const files = all ? here : here.filter((file) => file.file_type === '3mf')
    return HttpResponse.json({
      folder_id: folderId,
      all,
      folders: libraryFolders,
      files,
      hidden: here.length - files.length,
    } satisfies LibraryListing)
  }),

  http.get(`${base}/print/library/:id/plates/:index/thumbnail`, () => pngResponse()),
  http.get(`${base}/print/library/:id/thumbnail`, () => pngResponse()),

  http.get(`${base}/print/library/:id/plates`, ({ params }) => {
    const file = libraryFiles.find((row) => row.id === Number(params['id']))
    if (!file) return problem(404, 'Not Found', 'Bambuddy has no such resource')
    if (file.file_type === 'stl') return HttpResponse.json([] satisfies OutputPlate[])
    const count = file.id === MULTI_PLATE_FILE ? 2 : 1
    return HttpResponse.json(
      Array.from({ length: count }, (_, n) => ({ index: n + 1, has_thumbnail: true })) satisfies OutputPlate[],
    )
  }),

  http.get(`${base}/print/library/:id/choices`, ({ params, request }) => {
    const refused = libraryRefusal(Number(params['id']))
    if (refused) return refused
    const remembered = state.libraryChoices[String(params['id'])] ?? NO_MODEL_CHOICES
    const asked = new URL(request.url).searchParams.get('printer_id')
    const printerId =
      asked !== null ? Number(asked) : (remembered.printer_id ?? choicesView.printer_id ?? null)
    return HttpResponse.json({
      ...choicesView,
      printer_id: printerId,
      filaments: { ...choicesView.filaments, library_file_id: Number(params['id']), printer_id: printerId },
      model_choices: remembered,
    } satisfies ChoicesView)
  }),

  http.put(`${base}/print/library/:id/choices`, async ({ params, request }) => {
    const key = String(params['id'])
    const body = (await request.json()) as ModelPrintChoices
    if (isNoModelChoices(body)) delete state.libraryChoices[key]
    else state.libraryChoices[key] = { ...NO_MODEL_CHOICES, ...body }
    return HttpResponse.json(state.libraryChoices[key] ?? NO_MODEL_CHOICES)
  }),

  http.get(`${base}/print/library/:id/filaments`, ({ params, request }) => {
    const refused = libraryRefusal(Number(params['id']))
    if (refused) return refused
    const printerId = new URL(request.url).searchParams.get('printer_id')
    return HttpResponse.json({
      ...fixtures.filamentOptions,
      ...(printerId === null ? { nozzles: [] } : {}),
      library_file_id: Number(params['id']),
      printer_id: printerId === null ? null : Number(printerId),
    } satisfies FilamentOptions)
  }),

  http.post(`${base}/print/library/:id/run`, async ({ params, request }) => {
    const fileId = Number(params['id'])
    const refused = libraryRefusal(fileId)
    if (refused) return refused
    const body = (await request.json()) as PrintRunRequest
    await delay(200)
    return HttpResponse.json({
      route: 'slice_queue',
      library_file_id: fileId,
      printer_id: body.printer_id ?? null,
      slice_job_id: nextNumber(),
      sliced_library_file_id: nextNumber(),
      queue_item_ids: [nextNumber()],
      copies: body.copies ?? 1,
      warnings: [],
      project_id: body.project_id ?? null,
      folder_id: null,
      bambuddy_url: `${state.settings.bambuddy_url}/queue`,
    } satisfies PrintRunResult)
  }),
```
Add these helpers near `problem()`:
```ts
/** #313 — what the library routes answer for a file that is gone or not printable. */
function libraryRefusal(fileId: number) {
  const file = libraryFiles.find((row) => row.id === fileId)
  if (!file) return problem(404, 'Not Found', `Bambuddy has no such resource when asked to read library file ${fileId}`)
  if (file.file_type === 'gcode.3mf') return problem(422, 'Unprocessable Content', `${file.filename} is sliced already. Print it from Bambuddy.`)
  if (!file.printable) return problem(422, 'Unprocessable Content', `ScadBuddy prints only 3MF files from the library, and ${file.filename} is a ${file.file_type}.`)
  return null
}

function pngResponse() {
  const bytes = Uint8Array.from(atob(fixtures.MEDIA_PNG_BASE64), (char) => char.charCodeAt(0))
  return new HttpResponse(bytes, { headers: { 'Content-Type': 'image/png' } })
}
```
Register the plate-thumbnail handler **before** `/print/library/:id/plates` (it is listed first above) so the more specific path is never shadowed.

- [ ] **Step 6: Run the tests and the frontend gate**

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/mocks/library.ts frontend/src/mocks/handlers.ts frontend/src/mocks/handlers.test.ts
git commit -m "feat(frontend): library print calls and their msw mocks (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: The Print dialog takes a `source`

**Files:**
- Create: `frontend/src/lib/printSource.ts` and `frontend/src/lib/printSource.test.ts`
- Modify: `frontend/src/lib/usePrintChoices.ts`, `useFilamentPlan.ts` and `useRunPrint.ts`, plus their `.test.ts` files
- Modify: `frontend/src/components/PrintPicker.tsx` and `PrintPicker.test.tsx`, `frontend/src/components/ActionBar.tsx`, `frontend/src/components/print/PlatesToPrint.tsx` and `frontend/src/components/PrintOptionsDisclosure.tsx`

**Interfaces:**
- Consumes: the Task 8 `api.*Library*` calls.
- Produces:
  - `type PrintSource = { kind: 'output'; output: Pick<Output, 'id' | 'slug'> } | { kind: 'library'; file: Pick<LibraryEntry, 'id' | 'filename'> }`
  - `sourceKey(source: PrintSource | undefined): string | undefined`
  - `sourceApi(source: PrintSource): SourceApi`, where `SourceApi` has `getChoices(printerId: number | null)`, `getFilaments(query)`, `getPlates()`, `plateThumbnailUrl(index)`, `run(body)` and `remember(choices)`.
  - `usePrintChoices(open: boolean, source: PrintSource | undefined)`
  - `useFilamentPlan(source: PrintSource | undefined, choices, plate, size)`
  - `useRunPrint({ source, choices, printerId, selection, plan, planChanged, copies, projectId, options, onRan })`. `outputId` and `slug` are gone from its input.
  - `PrintPicker` props `{ open, source: PrintSource | undefined, slug?: string, onClose, onRan, onPrinterModel? }`
  - `PlatesToPrint` props `{ plates, value, onChange, thumbnailUrl: (index: number) => string }`
  - `PrintOptionsDisclosure` prop `slug?: string`

- [ ] **Step 1: Write the failing tests**

`src/lib/printSource.test.ts`:
```ts
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { choicesView, queuedResult } from '../mocks/choices'
import { sourceApi, sourceKey } from './printSource'

const OUTPUT = { kind: 'output', output: { id: 'a'.repeat(32), slug: 'name-keychain' } } as const
const LIBRARY = { kind: 'library', file: { id: 89, filename: 'bag-clip.3mf' } } as const

describe('printSource', () => {
  afterEach(() => vi.restoreAllMocks())

  it('keys an output and a library file apart', () => {
    expect(sourceKey(OUTPUT)).toBe(`output:${'a'.repeat(32)}`)
    expect(sourceKey(LIBRARY)).toBe('library:89')
    expect(sourceKey(undefined)).toBeUndefined()
  })

  it('routes an output to the output calls', async () => {
    const choices = vi.spyOn(api, 'getChoices').mockResolvedValue(choicesView)
    const remember = vi.spyOn(api, 'putModelChoices').mockResolvedValue({ filament_plan: [] })
    await sourceApi(OUTPUT).getChoices(2)
    await sourceApi(OUTPUT).remember({ filament_plan: [] })
    expect(choices).toHaveBeenCalledWith('a'.repeat(32), 2)
    expect(remember).toHaveBeenCalledWith('name-keychain', { filament_plan: [] })
  })

  it('routes a library file to the library calls', async () => {
    const run = vi.spyOn(api, 'runLibraryPrint').mockResolvedValue(queuedResult)
    const remember = vi.spyOn(api, 'putLibraryChoices').mockResolvedValue({ filament_plan: [] })
    await sourceApi(LIBRARY).run({} as never)
    await sourceApi(LIBRARY).remember({ filament_plan: [] })
    expect(run).toHaveBeenCalledWith(89, {})
    expect(remember).toHaveBeenCalledWith(89, { filament_plan: [] })
    expect(sourceApi(LIBRARY).plateThumbnailUrl(2)).toBe('/api/v1/print/library/89/plates/2/thumbnail')
  })
})
```
In `PrintPicker.test.tsx`, add:
```ts
it('prints a library file through the library run and remembers per file', async () => {
  const run = vi.spyOn(api, 'runLibraryPrint')
  const remember = vi.spyOn(api, 'putLibraryChoices')
  const { user } = renderPage(
    <PrintPicker open source={{ kind: 'library', file: { id: 89, filename: 'bag-clip.3mf' } }} onClose={vi.fn()} onRan={vi.fn()} />,
  )
  await loaded()
  await user.click(screen.getByRole('radio', { name: /0\.2 mm/ }))
  await user.click(screen.getByRole('button', { name: 'Print', exact: true }))
  await screen.findByTestId('queued-items')
  expect(run).toHaveBeenCalledWith(89, expect.objectContaining({ printer_id: expect.any(Number) }))
  await waitFor(() => expect(remember).toHaveBeenCalledWith(89, expect.objectContaining({ nozzles: expect.any(Array) })))
  expect(screen.queryByTestId('print-progress')).toBeNull()
  expect(screen.queryByRole('option', { name: 'This model' })).toBeNull()
})

it('library choices seed through the same seedPlan', async () => {
  server.use(
    http.get('/api/v1/print/library/89/choices', () =>
      HttpResponse.json({ ...choicesView, model_choices: { printer_id: 1, filament_plan: [{ slot_id: 1, spool_id: 99999 }] } }),
    ),
  )
  renderPage(<PrintPicker open source={{ kind: 'library', file: { id: 89, filename: 'bag-clip.3mf' } }} onClose={vi.fn()} onRan={vi.fn()} />)
  await loaded()
  // A remembered spool that is no longer in the inventory falls back to the suggestion.
  const suggested = choicesView.filaments.suggested?.[0]?.spool_id
  expect(within(screen.getByTestId('filament-slot-1')).getByTestId(`spool-${suggested}`)).toBeChecked()
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/lib/printSource.test.ts src/components/PrintPicker.test.tsx`
Expected: FAIL with `Cannot find module './printSource'`, and a `source` prop that doesn't exist on `PrintPicker`.

- [ ] **Step 3: `src/lib/printSource.ts`**

```ts
import { api } from '../api/client'
import type {
  ChoicesView,
  FilamentOptions,
  LibraryEntry,
  ModelPrintChoices,
  Output,
  OutputPlate,
  PrintRunRequest,
  PrintRunResult,
} from '../api/types'

/**
 * #313 — what the Print dialog prints: an output ScadBuddy rendered, or a file already
 * in Bambuddy's library. Everything else about the dialog is the same.
 */
export type PrintSource =
  | { kind: 'output'; output: Pick<Output, 'id' | 'slug'> }
  | { kind: 'library'; file: Pick<LibraryEntry, 'id' | 'filename'> }

export type FilamentQuery = { printerId?: number | null; plateId?: number; allPlates?: boolean }

/** The dialog's reads and writes for one source. */
export interface SourceApi {
  getChoices: (printerId: number | null) => Promise<ChoicesView>
  getFilaments: (query: FilamentQuery) => Promise<FilamentOptions>
  getPlates: () => Promise<OutputPlate[]>
  plateThumbnailUrl: (index: number) => string
  run: (body: PrintRunRequest) => Promise<PrintRunResult>
  /** What this source reopens on next time: per model for an output, per file here. */
  remember: (choices: ModelPrintChoices) => Promise<ModelPrintChoices>
}

/** One string per source, so what belongs to one is reset when it changes. */
export function sourceKey(source: PrintSource | undefined): string | undefined {
  if (!source) return undefined
  return source.kind === 'output' ? `output:${source.output.id}` : `library:${source.file.id}`
}

/** Looked up at call time, so a test's `vi.spyOn(api, …)` still sees every call. */
export function sourceApi(source: PrintSource): SourceApi {
  if (source.kind === 'output') {
    const { id, slug } = source.output
    return {
      getChoices: (printerId) => api.getChoices(id, printerId),
      getFilaments: (query) => api.getFilaments(id, query),
      getPlates: () => api.getOutputPlates(id),
      plateThumbnailUrl: (index) => api.outputPlateThumbnailUrl(id, index),
      run: (body) => api.runPrint(id, body),
      remember: (choices) => api.putModelChoices(slug, choices),
    }
  }
  const { id } = source.file
  return {
    getChoices: (printerId) => api.getLibraryChoices(id, printerId),
    getFilaments: (query) => api.getLibraryFilaments(id, query),
    getPlates: () => api.getLibraryPlates(id),
    plateThumbnailUrl: (index) => api.libraryPlateThumbnailUrl(id, index),
    run: (body) => api.runLibraryPrint(id, body),
    remember: (choices) => api.putLibraryChoices(id, choices),
  }
}
```

- [ ] **Step 4: The hooks take `source`**

`usePrintChoices(open: boolean, source: PrintSource | undefined)`:
- At the top: `const key = sourceKey(source)` and `const latest = useLatest(source)` (import `useLatest` from `./useLatest`, and `sourceApi` and `sourceKey` from `./printSource`).
- `reload`: replace `if (!open || !outputId) return` with `const current = latest.current; if (!open || !current || sourceKey(current) !== key) return`. Replace `api.getChoices(outputId, askedPrinter)` with `sourceApi(current).getChoices(askedPrinter)`. Deps become `[open, key, askedPrinter, latest]`.
- `const sourceKey = outputId` becomes `const resetKey = key`. Its effect deps are `[resetKey]`, and it returns `sourceKey: resetKey`, so `PrintPicker`'s own `sourceKey` reset keeps working.
- The plates effect: `if (!open || !latest.current) return`, then `sourceApi(latest.current).getPlates()`, with deps `[open, key, latest]`.
- Update the doc comment's `GET /print/outputs/{id}/choices` to say "the source's choices read".

`useFilamentPlan(source: PrintSource | undefined, choices, plate, size)`: add `const key = sourceKey(source)` and `const latest = useLatest(source)`. Replace `!outputId` with `!latest.current` and `api.getFilaments(outputId, …)` with `sourceApi(latest.current).getFilaments(…)`. In the deps, replace `outputId` with `key` and `latest`.

`useRunPrint`: in `RunInput`, replace `outputId: string | undefined` and `slug: string` with `source: PrintSource | undefined`. In `rememberChoices`, replace `void api.putModelChoices(slug, next).catch(() => undefined)` with `if (source) void sourceApi(source).remember(next).catch(() => undefined)`. In `run`, use `if (!source || !choices || bedType === null) return` and `const ran = await sourceApi(source).run(body)`.

Tests: in `usePrintChoices.test.ts`, replace each `usePrintChoices(true, OUTPUT_A)` with `usePrintChoices(true, { kind: 'output', output: { id: OUTPUT_A, slug: 'm' } })`, and the same for `OUTPUT_B` and the `rerender` props. The `toHaveBeenLastCalledWith(OUTPUT_A, 2)` assertions stay as they are (they spy `api.getChoices`). In `useRunPrint.test.ts`'s `input()`, replace `outputId: OUTPUT, slug: 'name-keychain'` with `source: { kind: 'output' as const, output: { id: OUTPUT, slug: 'name-keychain' } }`. Update `useFilamentPlan` call sites the same way wherever they exist.

- [ ] **Step 5: `PlatesToPrint`, `PrintOptionsDisclosure`, `PrintPicker` and `ActionBar`**

`PlatesToPrint`: replace `outputId: string` with `thumbnailUrl: (index: number) => string`, and use `src={thumbnailUrl(entry.index)}`. Drop the `api` import.

`PrintOptionsDisclosure` (`slug?: string`):
```ts
  const fallback: OptionScope = slug ? 'model' : 'global'
  const activeScope: OptionScope =
    (scope === 'printer' && !printerKey) || (scope === 'model' && !slug) ? fallback : scope
```
- The model layer: `{ scope: 'model', options: slug ? remembered?.models?.[slug] : undefined }`.
- `storedFor`'s last line: `return slug ? remembered?.models?.[slug] : undefined`.
- The PUT key: `activeScope === 'global' ? null : activeScope === 'printer' ? printerKey : (slug ?? null)`.
- The option: `{slug && <option value="model">This model</option>}`.
- The no-printer note: `No printer is picked yet, so options can only be remembered {slug ? 'for this model or every print' : 'for every print'}.`

`PrintPicker`:
- Props: `source: PrintSource | undefined` replaces `output`, and `slug?: string` (documented as "the model, for its print-options scope; a library file has none").
- `const outputId = source?.kind === 'output' ? source.output.id : undefined` stays the key for `usePrintProgress` and the project attach, so a library run polls nothing and shows no progress panel.
- Hooks: `usePrintChoices(open, source)`, `useFilamentPlan(source, choices, plate, size)`, and `useRunPrint({ source, choices, … })`, without `outputId` and `slug`.
- `rememberedCopies`: `slug === undefined ? undefined : remembered?.models?.[slug]`.
- Plates: `{picker.plates.length > 1 && source && (<PlatesToPrint plates={picker.plates} value={plate} onChange={picker.setPlate} thumbnailUrl={sourceApi(source).plateThumbnailUrl} />)}`.
- Update the header doc comment's two route lines to "the source's choices read (`/print/outputs/{id}/…` or `/print/library/{file_id}/…`, #313)".

`ActionBar`: `source={output ? { kind: 'output', output } : undefined}` in place of `output={output}`.

`PrintPicker.test.tsx`: replace every `output={X}` (six places, including `renderPicker`) with `source={{ kind: 'output', output: X }}`.

- [ ] **Step 6: Run the tests and the frontend gate**

Expected: PASS, the whole existing PrintPicker suite included.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/printSource.ts frontend/src/lib/printSource.test.ts frontend/src/lib/usePrintChoices.ts frontend/src/lib/usePrintChoices.test.ts frontend/src/lib/useFilamentPlan.ts frontend/src/lib/useRunPrint.ts frontend/src/lib/useRunPrint.test.ts frontend/src/components/PrintPicker.tsx frontend/src/components/PrintPicker.test.tsx frontend/src/components/ActionBar.tsx frontend/src/components/print/PlatesToPrint.tsx frontend/src/components/PrintOptionsDisclosure.tsx
git commit -m "refactor(print): the Print dialog takes an output or a library file as its source (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
(If a `useFilamentPlan.test.ts` exists and changed, add it to the pathspec.)

---

### Task 10: The Library page

**Files:**
- Create: `frontend/src/pages/LibraryPage.tsx` and `frontend/src/pages/LibraryPage.test.tsx`
- Modify: `frontend/src/App.tsx` (route) and `frontend/src/components/AppShell.tsx` (`NAV`)

**Interfaces:**
- Consumes: `api.listLibrary` and `api.libraryThumbnailUrl` (Task 8); `PrintPicker` with `source` (Task 9).
- Produces: the `/library` route, the nav tab "Library", the local-storage key `scadbuddy.library.advanced` (`'1'` or `'0'`), and the test ids `library-file-<id>`, `library-print-<id>`, `library-folder-<id|root>` and `library-advanced`.

- [ ] **Step 1: Write the failing tests**

```tsx
import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { LibraryPage } from './LibraryPage'

describe('LibraryPage', () => {
  beforeEach(() => {
    resetMockState()
    window.localStorage.clear()
  })

  it('lists the root 3MFs with Print, and hides the sliced file', async () => {
    renderPage(<LibraryPage />, { route: '/library' })
    const card = await screen.findByTestId('library-file-89')
    expect(within(card).getByRole('button', { name: 'Print' })).toBeInTheDocument()
    expect(screen.queryByTestId('library-file-104')).toBeNull()
    expect(screen.getByText(/1 more under Advanced/)).toBeInTheDocument()
  })

  it('Advanced lists the sliced file without Print and is remembered', async () => {
    const { user, unmount } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    const sliced = await screen.findByTestId('library-file-104')
    expect(within(sliced).queryByRole('button', { name: 'Print' })).toBeNull()
    expect(within(sliced).getByText(/print it from Bambuddy/i)).toBeInTheDocument()
    unmount()
    renderPage(<LibraryPage />, { route: '/library' })
    expect(await screen.findByTestId('library-file-104')).toBeInTheDocument()
  })

  it('renders a folder of hundreds of files with lazy thumbnails', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await user.click(await screen.findByTestId('library-folder-9'))
    await waitFor(() => expect(screen.getAllByTestId(/^library-file-/)).toHaveLength(300))
    const images = screen.getAllByRole('img')
    expect(images.every((image) => image.getAttribute('loading') === 'lazy')).toBe(true)
  })

  it('opens the Print dialog on the file', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await user.click(await screen.findByTestId('library-print-89'))
    expect(await screen.findByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it('says when the library cannot be read', async () => {
    server.use(
      http.get('/api/v1/print/library', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/bambuddy-unavailable', title: 'Bad Gateway', status: 502, detail: 'could not reach Bambuddy to list the library files: ConnectError' },
          { status: 502 },
        ),
      ),
    )
    renderPage(<LibraryPage />, { route: '/library' })
    expect(await screen.findByRole('alert')).toHaveTextContent('could not reach Bambuddy')
  })
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/pages/LibraryPage.test.tsx`
Expected: FAIL with `Cannot find module './LibraryPage'`.

- [ ] **Step 3: `src/pages/LibraryPage.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { LibraryEntry, LibraryListing } from '../api/types'
import { PrintPicker } from '../components/PrintPicker'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'

/** #313 — remembered per viewer: whether the page lists every file type. */
const ADVANCED_KEY = 'scadbuddy.library.advanced'

function readAdvanced(): boolean {
  try {
    return window.localStorage.getItem(ADVANCED_KEY) === '1'
  } catch {
    return false
  }
}

/**
 * #313 — Bambuddy's library by folder. Each unsliced 3MF has Print, which opens the same
 * spool-first dialog an output uses; the file is sliced as it stands in Bambuddy.
 * Advanced also lists sliced files and STLs; a sliced file is printed from Bambuddy.
 */
export function LibraryPage() {
  const [folderId, setFolderId] = useState<number | null>(null)
  const [advanced, setAdvanced] = useState(readAdvanced)
  const [listing, setListing] = useState<LibraryListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [printing, setPrinting] = useState<LibraryEntry | null>(null)

  useEffect(() => {
    let live = true
    setError(null)
    api
      .listLibrary({ folderId, all: advanced })
      .then((next) => live && setListing(next))
      .catch((cause: unknown) => {
        if (!live) return
        setListing(null)
        setError(cause instanceof ApiError ? cause.detail : 'Could not read the Bambuddy library.')
      })
    return () => {
      live = false
    }
  }, [folderId, advanced])

  function toggleAdvanced() {
    const next = !advanced
    setAdvanced(next)
    try {
      window.localStorage.setItem(ADVANCED_KEY, next ? '1' : '0')
    } catch {
      // A browser that refuses storage still toggles for this visit.
    }
  }

  const folders = listing?.folders ?? []
  const files = listing?.files ?? []
  const hidden = listing?.hidden ?? 0

  return (
    <div className="mx-auto flex max-w-6xl gap-6 p-6">
      <nav aria-label="Library folders" className="w-56 shrink-0 space-y-1 text-[13px]">
        <button
          type="button"
          data-testid="library-folder-root"
          onClick={() => setFolderId(null)}
          className={`block w-full rounded-[6px] px-2 py-1 text-left ${folderId === null ? 'bg-accent/8 text-ink' : 'text-muted'}`}
        >
          Top level
        </button>
        {folders.map((folder) => (
          <button
            key={folder.id}
            type="button"
            data-testid={`library-folder-${folder.id}`}
            onClick={() => setFolderId(folder.id)}
            style={{ paddingLeft: `${8 + (folder.depth ?? 0) * 12}px` }}
            className={`block w-full rounded-[6px] py-1 pr-2 text-left ${folderId === folder.id ? 'bg-accent/8 text-ink' : 'text-muted'}`}
          >
            {folder.name}
            {folder.file_count ? <span className="sb-num ml-1 text-faint">{folder.file_count}</span> : null}
          </button>
        ))}
      </nav>

      <section className="min-w-0 flex-1 space-y-4">
        <header className="flex items-center gap-3">
          <h1 className="text-[15px] text-ink">Library</h1>
          <span id="library-advanced" className="ml-auto text-[13px] text-ink">
            Advanced
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={advanced}
            aria-labelledby="library-advanced"
            aria-describedby="library-advanced-help"
            data-testid="library-advanced"
            onClick={toggleAdvanced}
            className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors ${
              advanced ? 'border-accent bg-accent' : 'border-line-strong bg-surface-3'
            }`}
          >
            <span
              className={`absolute top-[2px] size-3.5 rounded-full transition-[left] ${
                advanced ? 'left-[18px] bg-accent-ink' : 'left-[2px] bg-muted'
              }`}
            />
          </button>
          <span id="library-advanced-help" className="text-[12px] text-faint">
            List every file, sliced files and STLs too.
          </span>
        </header>

        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        {!listing && !error && (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Reading the Bambuddy library
          </p>
        )}
        {listing && files.length === 0 && (
          <p className="text-[13px] text-muted">No files here{hidden > 0 ? '' : ' yet'}.</p>
        )}
        {!advanced && hidden > 0 && (
          <p className="text-[12px] text-faint">
            <span className="sb-num">{hidden}</span> more under Advanced.
          </p>
        )}

        <ul className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-3">
          {files.map((file) => (
            <li
              key={file.id}
              data-testid={`library-file-${file.id}`}
              className="flex flex-col gap-2 rounded-[8px] border border-line bg-surface-2 p-2"
            >
              {file.has_thumbnail ? (
                <img
                  src={api.libraryThumbnailUrl(file.id)}
                  alt=""
                  loading="lazy"
                  className="aspect-square w-full rounded-[4px] object-contain"
                />
              ) : (
                <div className="aspect-square w-full rounded-[4px] bg-surface-3" aria-hidden />
              )}
              <p className="truncate text-[13px] text-ink" title={file.filename}>
                {file.filename}
              </p>
              {file.printable ? (
                <Button variant="primary" data-testid={`library-print-${file.id}`} onClick={() => setPrinting(file)}>
                  Print
                </Button>
              ) : (
                <p className="text-[12px] text-faint">
                  {file.file_type === 'gcode.3mf'
                    ? 'Sliced already. Print it from Bambuddy.'
                    : 'ScadBuddy cannot print this file type.'}
                </p>
              )}
            </li>
          ))}
        </ul>
      </section>

      <PrintPicker
        open={printing !== null}
        source={printing ? { kind: 'library', file: printing } : undefined}
        onClose={() => setPrinting(null)}
        onRan={() => undefined}
      />
    </div>
  )
}
```
Check that `Button` forwards `data-testid` and `onClick` (it does in `PrintPicker`). If its variants differ, use the same one `PrintPicker`'s Print uses. The `/1 more under Advanced/` text in the test depends on the root listing's `hidden` count from the Task 8 mock (one gcode file at the root).

- [ ] **Step 4: Route and nav**

`App.tsx`: `import { LibraryPage } from './pages/LibraryPage'`, and add `<Route path="library" element={<LibraryPage />} />` before `settings`.
`AppShell.tsx` `NAV`:
```ts
const NAV = [
  { to: '/', label: 'Models', end: true },
  { to: '/library', label: 'Library', end: false },
  { to: '/settings', label: 'Settings', end: false },
]
```

- [ ] **Step 5: Run the tests and the frontend gate**

Expected: PASS. If an AppShell or App test pins the nav items, add "Library" to its expectation.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/LibraryPage.tsx frontend/src/pages/LibraryPage.test.tsx frontend/src/App.tsx frontend/src/components/AppShell.tsx
git commit -m "feat(frontend): a Library page that prints files already in Bambuddy (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
(Add any nav test you updated to the pathspec.)

---

### Task 11: Playwright e2e against msw

The repo's e2e runs the production bundle with the msw worker (`frontend/playwright.config.ts`, `VITE_MOCK_API=1`), and `e2e/print.spec.ts` is the model to follow.

**Files:**
- Create: `frontend/e2e/library.spec.ts`

**Interfaces:**
- Consumes: the Task 10 page and test ids, and the Task 8 mocks (file 89 at the root, 104 a sliced file).

- [ ] **Step 1: Write the spec**

```ts
import { expect, test } from '@playwright/test'

test.describe('library', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('prints a library 3MF and reopens on its last choices', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: 'Library' }).click()
    await expect(page.getByTestId('library-file-89')).toBeVisible()
    await expect(page.getByTestId('library-file-104')).toHaveCount(0)

    await page.getByTestId('library-print-89').click()
    let dialog = page.getByRole('dialog', { name: 'Print' })
    await dialog.getByRole('radio', { name: /0\.2 mm/ }).check()
    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('Queue #')
    await expect(dialog.getByRole('button', { name: 'Open in queue' })).toBeVisible()
    await dialog.getByRole('button', { name: 'Done' }).click()

    await page.getByTestId('library-print-89').click()
    dialog = page.getByRole('dialog', { name: 'Print' })
    await expect(dialog.getByRole('radio', { name: /0\.2 mm/ })).toBeChecked()
  })

  test('Advanced lists a sliced file without Print, and is remembered', async ({ page }) => {
    await page.goto('/library')
    await page.getByRole('switch', { name: 'Advanced' }).click()
    const sliced = page.getByTestId('library-file-104')
    await expect(sliced).toContainText('Print it from Bambuddy')
    await expect(sliced.getByRole('button', { name: 'Print' })).toHaveCount(0)

    await page.reload()
    await expect(page.getByTestId('library-file-104')).toBeVisible()
  })
})
```

- [ ] **Step 2: Run it**

Run: `cd frontend && E2E_PREVIEW_PORT=4183 pnpm exec playwright test e2e/library.spec.ts`
Expected: PASS. The port override keeps this run off another worktree's preview server. Then run the whole mocked e2e suite once, `E2E_PREVIEW_PORT=4183 pnpm exec playwright test`, to confirm `print.spec.ts` still passes over the `source` change.

- [ ] **Step 3: Run the frontend gate one last time, plus the backend and agent gates**

Expected: all green. Confirm the generated files aren't staged: `git status --short backend/openapi.json frontend/src/api/schema.d.ts agent/src/api/schema.d.ts` prints nothing (they are gitignored).

- [ ] **Step 4: Commit**

```bash
git add frontend/e2e/library.spec.ts
git commit -m "test(e2e): print a Bambuddy library file from the Library page (#313)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:**
  - §2 Where: Task 10.
  - §2 File types: Tasks 5, 6, 7 and 10.
  - §2 STL probe: Tasks 1 and 7.
  - §2 Memory: Tasks 3, 6 and 9.
  - §2 Upstream: no Bambuddy change anywhere.
  - §3 table: plates from `/plates` (Task 5), slots from the file's `filament-requirements` (Task 5), no replate and no recolor (`LibrarySource.file_to_print`), #469 refusals (Task 6 test), per-file memory (Task 3), no local record (`LibrarySource.record`).
  - §4 seam: Tasks 4 and 5. §4 routes: Task 6. §4 table: Task 3. §4 agent coverage: Task 6, with the `NOT_A_TOOL` ruling.
  - §5: Tasks 9 and 10.
  - §7: every gate, plus Task 11.
- **Additions beyond §4's route list:** `/plates`, `/thumbnail` and `/plates/{index}/thumbnail`, needed because the browser can't reach Bambuddy (see Task 6).
- **Type consistency:**
  - `LibrarySource.load(client, file_id)`, `run_for_library(client, settings, file_id, request)` and `filament_options_for_library(client, file_id, …)` match between Tasks 5 and 6.
  - `choices_for(client, source, settings, *, remembered, printer_id)` matches between Tasks 4 and 6.
  - The frontend `PrintSource` shape is the same in Tasks 9 and 10. The `sourceApi` method names match the Task 8 `api.*` names.
