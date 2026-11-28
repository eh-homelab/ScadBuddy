# Flow task screens (A2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A flow's task is answered on a screen drawn from its schema, with widgets and
context blocks. A station page shows a place's open tasks. A scanned code finds the task
that waits for it. `wait_for_human` opens a task like `prompt()` does.

**Architecture:**
- **Backend:** the prompt schema check accepts `x-sb-widget` and `expects`. `tasks` gains
  `expects` (the codes and kinds a scan field takes) and `assignee`. `filament_locations`
  gains `station`. The existing routes get filters: `GET /tasks?code=`, `?assignee=`, and
  `GET /filament/locations?station=true`.
- **Frontend:** the widgets extend `inputsForm.tsx`. A task panel that needs no run page
  draws the form, and the station page and `/x/<code>` reuse that panel.
- **`wait_for_human`:** it becomes a text `prompt()` behind `workflow.patched`.

**Tech Stack:** Python 3.12 (FastAPI, psycopg, Temporal), `jsonschema` Draft 2020-12, React +
TypeScript (vitest, msw, Playwright).

**Spec:** `docs/superpowers/specs/2026-11-24-shop-floor-flows-design.md` §6.3, §6.4 (only what
A2 reads), §10 A2, D3, D11a, D12. Issue #8062, epic #7361. A1's plan:
`docs/superpowers/plans/2026-11-24-flow-prompts-a1.md`.

## Rulings (the plan's own calls; each is cheap to change)

- R1. **No new route, so no Go work.** Go core serves no browser `/api/v1` route, and it has no
  tasks or locations area (`tools/go/core/README.md`). A2 only adds query filters to existing
  Python routes. This follows the owner's rule (new routes in Go; a route with a Go copy
  changes both): it adds no route, and none it changes has a Go copy.
- R2. **`expects`** sits on a scan field: `{"kinds": ["location", "spool"]}` and/or
  `{"codes": ["L-12"]}` (at most 20 entries each). A code matches when it is in `codes`, or
  its kind is in `kinds`. The kind comes from the prefix, as `codes/resolve.py` `PREFIXES`
  reads it. At open, the store writes `tasks.expects text[]` with `kind:<kind>` and
  `code:<CODE>` entries, so `GET /tasks?code=` is one indexed query
  (`expects && ARRAY[…]`).
- R3. **Scan answers are checked on the server.** A scan answer must parse as a code, match
  its field's `expects`, and resolve to a row that is not archived. A failure is a 422 on that
  field, and its message never quotes the value.
- R4. **Photo and file answers.** The screen uploads the file to the existing
  `POST /entities/task:<id>/attachments`, which needs the new entity kind `task`. The answer
  is the attachment's id. The answer route checks that the id names an attachment of this
  task.
- R5. **The entity widget** in A2 serves `kind: library_file` (the existing
  `orders/LibraryFilePicker`; the answer is the file's id) and `location`/`spool` (a scan field
  with `expects.kinds` set to that kind). Any other kind falls back to a text field and logs it.
  The #7340 picker replaces `LibraryFilePicker` when it lands.
- R6. **Weight is typed grams.** No live scale reading exists yet (the weighing epic #7681 has
  typed grams only). The widget is a gram field with the `expect` block's target beside it.
  A live reading is a follow-up issue.
- R7. **`assignee`** is a principal string set by `prompt(assignee=...)`. The card shows it,
  and `GET /tasks?assignee=me` lists the signed-in person's tasks. Nothing refuses another
  person's answer: the spec gives `assignee` no authority (D12 makes a station an ordinary
  signed-in session).
- R8. **Context blocks** are checked at open: each item has exactly one key of `entity`,
  `text`, `image`, `table`, `checklist` or `expect`, with the shape below. A dict of any other
  shape is still stored and drawn as A1 draws it, a definition list, so A1 flows keep working.
  `image` is `{entity, attachment_id}`, served by the existing attachment content route.
- R9. **`wait_for_human` on `prompt()`.** Under patch `flow-human-prompt-8062`, a new call opens a
  task with schema `{answer: string}`, waits with no default limit (A1's D11a), and returns
  `HumanAnswer(answer=...)`. Runs that are already waiting, and replays from before the
  patch, keep the callback path, and `CallCard` stays for them.

## Global Constraints

- JSON Schema Draft 2020-12 with `format` checks on. A failure is a 422 naming the field, and
  its message never quotes the rejected value (spec §6.3 step 4).
- `x-sb-widget` values are exactly: `scan`, `photo`, `file`, `entity`, `weight`, `confirm`.
  An unknown value falls back to the plain type's widget and is logged (spec §6.3). The
  server still refuses any other `x-sb-*` key.
- Widget mapping (spec §6.3 table): a `string` `enum` of ≤ 5 options is segmented buttons,
  and > 5 is a select. `boolean` is a switch. A bounded `integer`/`number` is a stepper, and
  an unbounded one a number field. An `array` of scalars is one item per line.
- The station view is `/station/<place>`: open tasks for that place, oldest first, large
  touch targets, and the scan field always focused (spec §6.3). A station is a
  `filament_locations` row with `station = true` (D3). The migration's column comment says
  it marks a station for flow tasks (agreed with the inventory session, agent-fleet DM
  seq 251).
- `/x/<code>`: exactly one open task matching the code offers it, several are listed, and
  none opens the entity as today (spec §6.3).
- A station screen is an ordinary signed-in session. `answered_by` is the signed-in person
  (D12).
- New migrations only: `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`.
  Never edit a merged one.
- Every UI task loads the `frontend-design:frontend-design` skill and follows the existing
  flows components. No new visual language.
- Markdown is drawn with `agent/chat/Markdown.tsx` only, never as HTML.
- No counts in docs. Never commit `backend/openapi.json` or a `schema.d.ts`.

## Review Focus

1. **A code with a prefix that resolves to no row,** or an archived row, scanned into a task:
   a 422 on that field, not a 500, and the task stays open (Task 2 test).
2. **A schema with `expects` but no `x-sb-widget: scan`,** or `expects` with neither list:
   refused at open with a problem naming the field (Task 1 test).
3. **A station screen left open while a task is answered on another device:** the station
   drops the task on `task.changed` and keeps its scan focus (Task 6 test).
4. **A photo answer naming an attachment of another task:** a 422, never accepted (Task 2
   test).
5. **A run waiting in `wait_for_human` from before the deploy:** its answer still lands
   through the old callback (Task 4 replay test).

---

### Task 1: Prompt schemas take widgets and `expects`; context blocks are checked

**Files:**
- Modify: `backend/scadbuddy/core/input_schema.py` (`_property_problems`, `schema_problems`)
- Modify: `backend/scadbuddy/tasks/schema.py`, `backend/scadbuddy/tasks/models.py` (context)
- Create: `backend/scadbuddy/tasks/widgets.py`
- Test: `backend/tests/tasks/test_task_widgets.py`

**Interfaces:**
- Produces:
  - `WIDGETS = ("scan", "photo", "file", "entity", "weight", "confirm")`
  - `scan_fields(schema: dict) -> dict[str, Expects]`, with
    `Expects = {"kinds": tuple[str, ...], "codes": tuple[str, ...]}`
  - `expects_entries(schema: dict) -> list[str]`, which gives `kind:<kind>` and
    `code:<CODE>` entries (R2)
  - `context_problems(context: list[dict]) -> list[str]`

- [ ] **Step 1: Write the failing tests.**
  - `test_widgets_pass_for_prompts`: each widget on its type passes
    `prompt_schema_problems`. That is `scan`/`photo`/`file`/`entity` on `string`, `weight` on
    `number`/`integer`, and `confirm` on `boolean`. An `entity` field needs
    `"kind": "library_file" | "location" | "spool"`.
  - `test_unknown_widget_passes_and_is_logged`: `x-sb-widget: "dial"` passes, and a warning is
    logged.
  - `test_widget_on_wrong_type_is_refused`: `x-sb-widget: "confirm"` on `string` →
    `"'x-sb-widget' confirm needs boolean"`.
  - `test_other_x_sb_key_is_refused`: `x-sb-colour` is refused as today.
  - `test_expects_rules`:
    - `expects` without `x-sb-widget: scan` is refused;
    - `{}` is refused ("names no kind or code");
    - an unknown kind is refused;
    - `codes: ["L-12"]` passes;
    - 21 codes are refused.
  - `test_expects_entries`: `{"kinds": ["location"], "codes": ["l-12"]}` →
    `["kind:location", "code:L-12"]`.
  - `test_flow_inputs_still_refuse_widgets`: `schema_problems` without the prompt flag still
    refuses `x-sb-widget`, so flow inputs are unchanged.
  - `test_context_blocks`:
    - each of the six block shapes passes;
    - `{"text": 3}` is refused;
    - `{"table": {"columns": ["a"], "rows": [["x", "y"]]}}` is refused (row width);
    - `{"note": "free"}` still passes (R8).
- [ ] **Step 2: Run them; expect them to fail.** `cd backend && uv run pytest tests/tasks/test_task_widgets.py -q`
- [ ] **Step 3: Implement.**
  - `schema_problems(..., widgets: bool = False)`. `prompt_schema_problems` passes
    `widgets=True`, which allows `x-sb-widget`, `expects` and `kind` on the types above.
  - Block shapes:
    - `entity: "<kind>:<ref>"` with a kind in `entities.registry.KINDS`;
    - `text: str` of ≤ 4000 chars;
    - `image: {entity, attachment_id}`;
    - `table: {columns: [str], rows: [[scalar]]}`, ≤ 50 rows, each row as wide as `columns`;
    - `checklist: [str]` of ≤ 30 items;
    - `expect: {weight_g: number > 0, tolerance_g: number ≥ 0}`.
  - `TaskOpen` runs `context_problems`.
- [ ] **Step 4: Run them; expect them to pass.**
- [ ] **Step 5: Commit.** `feat(flows): prompt schemas take task widgets and expects; context blocks are checked (#8062)`

### Task 2: `tasks.expects`, `tasks.assignee`, the scan and photo answer checks, the new filters

**Files:**
- Create: migration `<ts>_tasks_expects_assignee.sql`, which:
  - adds `tasks.expects text[] NOT NULL DEFAULT '{}'` with GIN index `tasks_open_expects`
    `WHERE state = 'open'`;
  - adds `tasks.assignee text` (≤ 200 chars, no control characters);
  - adds `filament_locations.station boolean NOT NULL DEFAULT false`, with
    `COMMENT ON COLUMN ... 'A station for flow tasks (#8062, spec D3)'`.
- Modify:
  - `backend/scadbuddy/tasks/models.py` (`assignee` on `_TaskFields`)
  - `tasks/store.py`: `open` writes `expects_entries`; `list(..., code=None, assignee=None)`
  - `api/tasks.py`:
    - `list_tasks` takes `code: str | None` and `assignee: Literal["me"] | None`;
    - `answer_task` runs the scan and attachment checks.
  - `entities/registry.py`: `_kind("task", "tasks")`
  - `filament/models.py` and `filament/locations.py`: `station` on `Location`, `LocationCreate`
    and `LocationPatch`, and the tree filter
  - `api/filament.py`: `GET /filament/locations?station=true`
  - `crud/tasks.py`, `api/crud_tasks.py`: carry `assignee`
- Test: `backend/tests/api/test_task_screens.py`, `backend/tests/api/test_filament_locations.py`

**Interfaces:**
- Consumes: Task 1 `scan_fields`, `expects_entries`.
- Produces:
  - `GET /api/v1/tasks?code=L-12` → open tasks whose `expects` match, oldest first;
  - `GET /api/v1/tasks?assignee=me`;
  - `Task.assignee: str | None`;
  - `Location.station: bool`;
  - entity kind `task`.

- [ ] **Step 1: Write the failing tests.**
  - `test_list_by_code`:
    - a task expecting `kinds: ["location"]` and one expecting `codes: ["S-3"]`;
    - `?code=L-7` gives the first;
    - `?code=s-3` gives the second (case folded);
    - `?code=X-1` gives none;
    - `?code=nonsense` gives 422.
  - `test_list_by_assignee`: `?assignee=me` lists only tasks whose `assignee` is the
    browser principal.
  - `test_scan_answer_checked`:
    - a code of the wrong kind → 422 `problems[0].field == "box"`, and the message does not
      contain the code;
    - a code with no row → 422;
    - an archived location → 422;
    - a matching live location → 200.
  - `test_photo_answer_must_be_this_tasks_attachment`:
    - an attachment of another task → 422;
    - an attachment of this task → 200.
  - `test_task_entity_kind`: `POST /entities/task:<id>/attachments` stores a file for an open
    task; an unknown task id → 404.
  - `test_station_flag`:
    - `PATCH /filament/locations/{id}` with `{"station": true}` sets it;
    - `GET /filament/locations?station=true` returns only stations, flat, sorted by name.
- [ ] **Step 2: Run them; expect them to fail.**
- [ ] **Step 3: Implement as the Interfaces say.** Resolve scan answers with
  `codes.resolve.resolve` in the route's connection, after `validate_answer` passes.
- [ ] **Step 4: Run them, plus `tests/api/test_tasks.py tests/api/test_flow_prompt.py`; expect all to pass.**
- [ ] **Step 5: Commit.** `feat(flows): tasks find their scan codes, carry an assignee, and locations can be stations (#8062)`

### Task 3: `prompt(assignee=...)` and the context check in flows

**Files:** `backend/scadbuddy/workflows/flow_tools.py` (`prompt`, `_prompt_task`); test
`backend/tests/workflows/test_flow_prompt_widgets.py`.

- [ ] **Step 1: Write the failing tests.**
  - `prompt(..., assignee="browser:jo")` stores it.
  - A context block that fails `context_problems` raises `ValueError` in the script, before
    any activity runs.
  - A widget schema reaches the task unchanged.
- [ ] **Step 2: Run them; expect them to fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect them to pass.**
- [ ] **Step 5: Commit.**

### Task 4: `wait_for_human` opens a task (D11a, R9)

**Files:** `backend/scadbuddy/workflows/flow_tools.py` (`wait_for_human`); tests
`backend/tests/workflows/test_wait_for_human_prompt.py`. Also the replay fixture, made from a
history recorded before the change.

- [ ] **Step 1: Write the failing tests.**
  - A new run's `wait_for_human("Swap to pink?")` opens a task with schema
    `{"type": "object", "properties": {"answer": {"type": "string", "title": "Answer"}},
    "required": ["answer"]}`.
  - Answering that task through `POST /tasks/{id}/answer` returns `HumanAnswer(answer=...)`.
  - `people_only` and `timeout_s` behave as before.
  - A history recorded before the patch replays without nondeterminism and answers through
    `POST /flow-runs/{id}/answer`.
- [ ] **Step 2: Run them; expect them to fail.**
- [ ] **Step 3: Implement under `workflow.patched(f"flow-human-prompt-8062:{call_id}")`.**
- [ ] **Step 4: Run them, plus `tests/workflows` and `tests/api/test_flow_prompt.py`; expect all to pass.**
- [ ] **Step 5: Commit.**

### Task 5: The widgets (frontend; load frontend-design)

**Files:**
- Modify:
  - `frontend/src/lib/flowInputs.ts`: `InputField.widget?`, `kind?`, `expects?`
  - `frontend/src/components/flows/inputsForm.tsx`: `Field`
- Create:
  - `frontend/src/components/flows/widgets/ScanField.tsx`
  - `frontend/src/components/flows/widgets/UploadField.tsx`
  - `frontend/src/components/flows/widgets/Segmented.tsx`
  - `frontend/src/components/flows/widgets/Stepper.tsx`
- Test: `frontend/src/components/flows/widgets.test.tsx`

**Interfaces:**
- `InputField.widget: 'scan'|'photo'|'file'|'entity'|'weight'|'confirm'|undefined`
- `ScanField({id, expects, value, onChange, autoFocus})`. It resolves a typed or scanned code
  with `resolveCode` and shows its name, or says why it does not fit.
- `UploadField({taskId, accept, onChange})`. It posts to `entityApi` `task:<id>` and sets the
  attachment id.

- [ ] **Step 1: Write the failing tests.** Each widget renders from its schema:
  - an enum of 3 options is a radiogroup of buttons; one of 6 options is a select;
  - a boolean is a switch;
  - an `integer` with min 0 and max 10 has − and + buttons that stop at the bounds;
  - a scan field takes "L-7⏎" and shows the location's name; a code of the wrong kind says
    "Expected a spool";
  - a photo field uploads a file and the answer holds its id;
  - `entity` with kind `library_file` opens `LibraryFilePicker`;
  - `weight` takes grams;
  - `confirm` is one large button that submits `true`;
  - an unknown widget draws the plain type's field.
- [ ] **Step 2: Run them; expect them to fail.** `cd frontend && pnpm exec vitest run src/components/flows/widgets.test.tsx`
- [ ] **Step 3: Implement.** Extend the mocks (`mocks/features/tasks.ts`, codes, attachments).
- [ ] **Step 4: Run them; expect them to pass.**
- [ ] **Step 5: Commit.**

### Task 6: Context blocks, the task panel, and the station page (frontend; load frontend-design)

**Files:**
- Create:
  - `frontend/src/components/flows/ContextBlocks.tsx` (replaces `TaskCard`'s `Context`)
  - `frontend/src/components/flows/TaskPanel.tsx`: the form, the context and the limit for a
    `TaskQuestion`, with no run entry; `TaskCard` wraps it
  - `frontend/src/pages/StationPage.tsx`: `/station/:place`, plus `/station` listing stations
- Modify:
  - `frontend/src/App.tsx` (routes)
  - `frontend/src/api/client.ts`: `listTasks({place?, code?, assignee?})`
  - the location page: a "Station for flow tasks" switch
- Test: `ContextBlocks.test.tsx`, `StationPage.test.tsx`

- [ ] **Step 1: Write the failing tests.**
  - Each block draws:
    - `text` as Markdown with no raw HTML;
    - `table` as a table;
    - `checklist` as ticks kept on the screen only;
    - `expect` as "Expect 640 g ± 96 g";
    - `entity` as a link to the entity's page;
    - `image` from the attachment content route.
  - An A1 free dict is still a definition list.
  - The station page:
    - lists the place's open tasks, oldest first;
    - on `task.changed` for `tasks:<place>`, drops an answered one;
    - keeps the station's scan field focused;
    - a scan that matches one task's `expects` moves to that task's scan field and fills it.
  - `/station` lists stations, and the switch on the location page sets `station`.
- [ ] **Step 2: Run them; expect them to fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `src/components/flows src/pages/WorkflowRunPage.test.tsx`; expect all to pass.**
- [ ] **Step 5: Commit.**

### Task 7: Scan-to-answer on `/x/<code>` (frontend; load frontend-design)

**Files:** `frontend/src/pages/CodePage.tsx`; test `CodePage.test.tsx`.

- [ ] **Step 1: Write the failing tests.** With `listTasks({code})`:
  - one open task → its `TaskPanel` with "Open L-7 instead";
  - two → a list with a link to each run;
  - none → today's navigation;
  - a failed task read → today's navigation, and the failure is logged.
- [ ] **Step 2: Run them; expect them to fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect them to pass.**
- [ ] **Step 5: Commit.**

### Task 8: Docs, the agent and the e2e

**Files:**
- `docs/features/flows.md`: widgets, context blocks, stations, scan-to-answer,
  `wait_for_human`
- `docs/architecture/workflows.md`: the patch and `expects`
- `frontend/CLAUDE.md`, `backend/CLAUDE.md`: the layout lines
- the flow authoring guide that the agent reads: `prompt()`'s widgets and `assignee`
- `frontend/e2e/flow-station.spec.ts`: a mocked station answers a scan task

- [ ] **Step 1: Write the e2e test, and run it; expect it to fail.**
- [ ] **Step 2: Update the docs and the guide, and make the test pass.**
- [ ] **Step 3: Run the frontend `lint`, `typecheck` and `test`, the backend `ruff`, `mypy` and touched tests, and the agent tests.**
- [ ] **Step 4: Commit.**
