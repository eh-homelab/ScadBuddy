# Flows (workflows)

## What it does

A flow is a short script that chains ScadBuddy's actions: render, save an output, queue a
print, arrange, call a tool, or ask the assistant. Flows are registered and edited under
Workflows, started by hand, on a schedule or on an event (for example an order change),
and pause for a person's approval where a grant does not cover an outward step. A flow can
also ask a person a question, as free text or as a form with typed fields
([Questions to a person](#questions-to-a-person)).

## How it works

A run is `ProjectWorkflow` on the `projects` queue, id `flow-<run_id>`. Its host
functions call the API's own routes through an activity, and `tool(...)` and
`agent_session(...)` go to the agent's queue. Triggers are Temporal Schedules or bus
events that start `FlowTriggerFire`. See
[workflows.md "Flows"](../architecture/workflows.md#flows) and the
[flow grants and git spec](../superpowers/specs/2026-10-11-flow-grants-and-git-design.md).

```mermaid
sequenceDiagram
    participant UI as Workflows page
    participant API
    participant PJ as Projects worker
    participant AG as Agent (agent-tools)
    UI->>API: POST /api/v1/workflows/{id}/runs
    API->>PJ: ProjectWorkflow flow-<run>
    PJ->>API: flow_route_send → /api/v1/models/{slug}/render, /print/…
    PJ->>AG: tool(...) / agent_session(...)
    PJ->>API: flow_grant_check (or wait for approval)
    PJ->>API: flow_task_open (prompt)
    UI->>API: POST /api/v1/tasks/{id}/answer
    API->>PJ: Operation task_answer, which answers the run's callback
    PJ->>API: flow_task_answered
```

## Questions to a person

A script asks with a host function (`backend/scadbuddy/workflows/flow_tools.py`):

| | `wait_for_human(question, timeout_s=None, people_only=False)` | `prompt(title, schema, context=None, place=None, ui=None, timeout_s=None, people_only=False, assignee=None)` |
|---|---|---|
| The question | a text, the task's title | a title and a form |
| The answer | `{answer}`, a text | a dict by field name |
| Row in `tasks` | one, with the one text field `answer` | one, opened before the call waits |
| Answer route | `POST /api/v1/tasks/{task_id}/answer` | `POST /api/v1/tasks/{task_id}/answer` |
| Operation kind | `task_answer`; an agent's: `flow_agent_answer` | `task_answer`; an agent's: `flow_agent_answer` |
| Harness callback | `task_answer` | `task_answer` |

Since #8062 a `wait_for_human` opens a task as a prompt does (`HUMAN_PROMPT_PATCH`, a patch
id per call), with the schema `HUMAN_ANSWER_SCHEMA` and the question as its title. A
question longer than a title (`TASK_TITLE_MAX`, 500 characters) is cut to fit, ending with
"…", and the whole question is a `text` block of the task's context (at most 4000
characters; a longer one raises `ValueError`; `_human_question`). `wait_for_human("")` raises
`ValueError`, because a task title needs a character. An answer with no character but spaces is
refused, as the text route refuses a blank answer (#2379): the schema's `answer` has a
`minLength` of 1, and `validate_answer` (`backend/scadbuddy/tasks/validate.py`) refuses
an answer of only spaces. Its step is still
`wait_for_human`. A call from before replays with no task: it waits on
`human_answer_open` (`human_answer` for an older one) and takes its answer at
`POST /api/v1/workflow-runs/{run_id}/answer`, operation kind `flow_answer`. The run page shows
it with `CallCard`. The text route refuses a call with a task with 409
`flow-answer-is-a-task` (`backend/scadbuddy/api/flows.py` `ANSWER_IS_A_TASK`).

**No default limit.** Both wait with no limit unless `timeout_s` sets one (`_wait_s`). The
floor is 10 seconds (`MIN_WAIT_S`): less raises `ValueError`, and a literal that is too
short is refused when the script is checked (`backend/scadbuddy/flows/typecheck.py`
`_literal_ranges`). When a `timeout_s` is up the script gets `TimeoutError`. The ceiling is ten
years (`OPEN_WAIT`): a longer `timeout_s` is ten years. The callbacks declare the same ten
years to the harness, which ends a call with no `timeout_s` then. The script does not get
`TimeoutError` for that. It gets the harness's `CallbackToolError`, which a script sees as a
plain `Exception`, and the prompt's task is `cancelled`.

**Old runs keep their timer.** A `wait_for_human` that a run called before this change
replays with the old callback, its default of an hour and its range of 10 to 86400 seconds
(`WAIT_OPEN_PATCH`, a patch id per call). The next call of the same run has no default
limit.

**Widgets.** A property of a prompt's schema may name a screen control with `x-sb-widget`
(`backend/scadbuddy/tasks/widgets.py` `WIDGETS`). The control draws the field's own type, so
the answer is still the type's value:

| `x-sb-widget` | Type | The control |
|---|---|---|
| `scan` | string | a code, typed or scanned; the field looks it up and shows its name |
| `photo`, `file` | string | an upload; the value is the id of an attachment of the task (entity kind `task`) |
| `entity` | string | a pick of a row, with `kind`: `library_file` (the value is the file's id), `location` or `spool` (the value is its code, such as `S-9`) |
| `weight` | number | a weight in grams |
| `confirm` | boolean | a large yes or no |

A `scan` field may also have `expects`, with `kinds` (`spool`, `location`) and `codes` (such
as `S-3`). The route refuses an answer whose code is none of them, or names an archived row
(`backend/scadbuddy/tasks/answer_check.py`). It never says which of the reasons it is. An
`entity` field of kind `location` or `spool` is checked the same way, as a scan of that kind;
one of kind `library_file` must name a library file that is not in the trash. An
unknown `x-sb-widget` is logged and draws the plain control of the type. A widget on a type
it does not fit, `kind` on a field that is not an `entity`, and `expects` on one that is not
a `scan` are refused when the prompt opens (`widget_problems`). The inputs of a flow refuse
all three keys.

**Context blocks.** An item of `context` with one key that names a block is checked and
drawn as that block (`context_problems`, `frontend/src/components/flows/ContextBlocks.tsx`):
`entity` (`<kind>:<ref>`, a link), `text` (Markdown), `image` (`{entity, attachment_id}`),
`table` (`{columns, rows}`), `checklist` (a list of texts, ticked on the screen and not
kept) and `expect` (`{weight_g, tolerance_g}`, which a `weight` field checks the weighing
against). Any other item is stored and drawn as before.

**Assignee.** `prompt(..., assignee=...)` names who the task is for. The task card shows it,
and `GET /api/v1/tasks?assignee=me` lists the tasks of the signed-in person. It does not
limit who may answer.

**Stations.** A place may be a station for flow tasks (`filament_locations.station`; the
switch "Station for flow tasks" on the place, and `GET /api/v1/filament/locations?station=true`
lists them). A task's `place` names its station by the location's code, such as `L-7`. A
place that parses as a location's code is stored in that form, so `l7` and `L-07` are `L-7`
(`backend/scadbuddy/tasks/models.py` `folded_place`). A place that is no code is stored as
it is, and no station shows it. `/station` lists the stations. `/station/<location id>`
shows the open tasks at that location's code, oldest first, over
`GET /api/v1/tasks?place=<code>`, and it follows `tasks:<code>`, so a task that opens or is
answered elsewhere appears or goes at once. A scan field at the top keeps the focus. A code
scanned there goes to the one task that waits for it (`GET /api/v1/tasks?code=`); when more
than one waits, the person chooses. Only a scan field with `expects` makes its task wait for
a code, so a station or `/x/<code>` offers only those fields. The page is an
ordinary signed-in session, so each answer is the signed-in person's own.

**Scan to answer.** `/x/<code>`, the address of a label's QR, shows the open task that
waits for the code before it opens the place or spool, with the code in the task's scan
field. `tasks.expects` holds the entries (`kind:<kind>`, `code:<CODE>`) that the list by
code reads.

**The schema.** A prompt's `schema` is the subset of a flow's `inputs.json`
(`backend/scadbuddy/core/input_schema.py` `schema_problems`,
`backend/scadbuddy/tasks/schema.py` `prompt_schema_problems`). A string, or an array's string
items, may also have a `format`: `email`, `date` or `date-time` (`FORMATS`). A `date-time`
is RFC 3339 with an offset, and a leap second is refused. An `email` has one `@`, a local
part before it and a domain with a dot after it, no empty part between dots in the domain,
no whitespace, no control character, none of `,;<>()[]\"` and at most 254 characters. A
letter need not be ASCII. A `default` must fit its field, its `format` included. The inputs of a flow and of a
pipeline refuse `format`.

**Sizes.** `backend/scadbuddy/tasks/models.py` `TaskOpen` checks them. The title has 1 to
500 characters. The context has at most 16 KiB of JSON (`MAX_JSON_BYTES`), `ui` at most 200
characters, and `place` 1 to 100 characters with no control character (a location's code
is stored as `L-<n>`). The schema has the
limits of an inputs schema. What Postgres cannot store is refused
(`backend/scadbuddy/core/json_values.py` `unstorable`). An answer has at most 16 KiB of
JSON (413).

**What the script gets.** The answer, as the schema types it
(`backend/scadbuddy/flows/inputs.py` `filled`). A field left out is its default. With no
default, a boolean is `False` and any other field is `None`. A field in `required` must be
in the answer, also when it has a default. `prompt` raises `ValueError` for a schema,
title, context, place, `ui` or `timeout_s` it does not take, and for an answer that fits
the schema and not the field's type (a huge number). It raises `RouteRefusedError` when the
run takes no task, because it ended or is gone.

**Who answers.** A person, always (`backend/scadbuddy/core/authorship.py` `by_a_person`). The
person's setting `flow_answerers` (#7471), or the run's approved grant (its `answers`), may
also let in, on the agent's service credential:

| Value | Who |
|---|---|
| `starter` | the session that started the run |
| `sessions` | any session that a browser user owns |
| `token:<id>` | that MCP token, and the sessions it owns |

Empty, the default, is people only. Only a person changes it (403 `person_only`). A run
grant's `answers.who` takes the same values; a person approves it with the grant, an agent
may only propose it, a child run's grant drops it, and a trigger's standing grant may not
hold it. The grant admits only while it is approved and in force with its chain. A session
answers only while it is not archived, is its author's, holds `write`, and is not a flow's
(`ai_sessions.background_tools`, `turn_flow`). A token answers only while it is not revoked
or expired and holds `write`. A call from a flow run never answers. A question that the
script asked with `people_only=True`, or that parked before #7471, is for people only. Any
other caller gets 403 `flow-answer-not-allowed`, whose detail names the setting or says that
the question is for people only (`backend/scadbuddy/flows/answerers.py`,
`backend/scadbuddy/api/flows.py` `answerer` and `admit`). The route reads the setting and
the grant; the operation's check asks again about the question and the session or token, so
a Reset that parks a people-only question in between is refused. The decision's
transaction checks again that the grant is in force (`FlowStore.record_decision`), so a
grant revoked in between records nothing (403). A repeat of a request gets its first
answer. Both routes are `api` routes, so `enforce` lets the agent's credential reach them.

The assistant answers with the tool `answer_flow_question` (tier `outward`, so a person
approves each answer unless they lower it). The decision row names the answerer
(`responder`: `browser`, `session:<id>` or `token:<id>`) and what admitted it
(`admitted_by`: `setting`, or the grant's id when only the grant let it in). A task's
`answered_by` is that text for an agent. The run's page names the assistant, links its
session, and says when the run's grant let it answer. A person's answer keeps the Update id
and the operation key it had before. A person may take over an agent's answer that never
reached the run; an agent only its own.

The forms on the page are user-only (`USER_ONLY`): the assistant cannot press or fill them,
and cannot read the text typed in them
([browser-bridge.md](../ai/browser-bridge.md#data-agent-user-only)).

**The answer's way.** The route checks the answer against the task's schema
(`backend/scadbuddy/tasks/validate.py` `validate_answer`). A problem names a field and
never quotes a value (422 `invalid-answer`). The answer goes to the `task_answer` operation
sealed under the run's payload key (`backend/scadbuddy/flows/operations.py` `seal_answer`).
So no operation record and no Temporal history holds it in the clear. The operation's result
names the task, and `GET /api/v1/tasks/{task_id}` has the answer.

**Two answers.** The run's `workflow_run_decisions` row picks the winner
(`FlowStore.record_decision`), and the other answer is refused with a 409. The exception
is a row whose answer never reached the run and that nothing may still send: a later answer
takes it over (`backend/scadbuddy/flows/operations.py` `take_over`, #7575). The task's row is
marked after the run has the answer. So for a moment an answered task can read `open`, and
so can the `state` of a 409 `not-open`. The operation's mark is best effort. The run's own
mark (`flow_task_answered`, `final`) is of the answer that the script gets. It replaces
another answer on the row and leaves an expired task as it is.

**A task's life** (`backend/scadbuddy/tasks/store.py` `TaskStore`):

| State | When |
|---|---|
| `open` | the prompt asked, and the run waits or soon will |
| `answered` | an answer reached the run |
| `expired` | the prompt's time was up (`flow_task_close`) |
| `cancelled` | the run ended, the call ended with no answer, or a Reset went back to before the prompt |

- **Reset.** A Reset through ScadBuddy makes the tasks match the run
  (`TaskStore.match_waiting_in`). A task that the run waits for again opens again, with no
  answer. An open task of a call that the run no longer has is cancelled. Any other task
  stays as it is. When the point is between the prompt's open and its wait, the task is
  cancelled, and opens again as the call waits (`TaskStore.reopen_in`): it keeps its first
  `expires_at`, and the prompt's timer starts anew.
- **Executions.** Each write by the run names the execution that makes it
  (`backend/scadbuddy/workflows/flow_activities.py` `FlowTaskActivities`). An execution that
  a Reset replaced opens, marks and closes nothing. The run's own mark or close from an
  execution that the row of an open run does not know yet is refused (503 `run-behind`) and
  tried again. From one that the row of an ended run does not know, the write changes
  nothing and returns the task as it stands, so it is not tried again.
- **Retries.** The task activities (`flow_task_open`,
  `flow_task_answered`, `flow_task_close`) are tried without limit. Only `flow_task_open` has final
  refusals: a task that is refused (422), a run that has ended and a run that is gone.
- **Forget.** Forgetting a run deletes its tasks (`ON DELETE CASCADE`).

**Reads.** `GET /api/v1/tasks` lists tasks by `state`, `place`, `code` and `assignee`, without their answers or
who gave them (`TaskQuestion`). `GET /api/v1/tasks/{task_id}` reads one with both
(`backend/scadbuddy/api/tasks.py`). A run's page gets
the question, without its answer, in its pending entry (`PendingEntry.task`). A task that
opens or moves to another state publishes `task.changed` on the realtime topics `tasks`,
`task:<id>` and, with a place, `tasks:<place>` (`backend/scadbuddy/api/realtime.py`
`topics_of`). The station page lists tasks by place. Nothing reads `ui`.

## Decisions on an outward call

A person approves or denies a waiting call with `POST /api/v1/workflow-runs/{run_id}/decide`,
operation kind `flow_decide` (`backend/scadbuddy/flows/operations.py`). The decision is
written to the call's `workflow_run_decisions` row, and then sent to the run.

**The row is what counts.** The Update that the run gets says only yes or no. A print, an
arrange and a tool of ScadBuddy's agent read the row before they act (`flow_decision` in
`backend/scadbuddy/workflows/flow_grants.py`). The agent reads it too before it runs one of
its tools or a plugin's. The call does not run when the row is not an approval, or when there
is no row (#7588). It runs the input that the row approves: the call as it was parked, or the
narrowed input, which must match the row's hash. A run that made an approved call before
#2482 reads the row from its next call on, by a marker of the call's own.

So a call stops, and its step fails with `NoApprovalError`, when its approval reached the run
but its row is gone or is not an approval. A narrowed input that does not match the row's hash
fails with `NarrowedInputError`.
That happens when the row was undone because Temporal said that the Update never arrived and
it then arrived, when a grant's approval was withdrawn and its Update then arrived, or when a
Reset falls between the decision and the call. In the last case the person was told
"approved" for the execution that the Reset replaced.

**Two decisions.** The first one on the row wins, and another one is refused with a 409. The
exception is a person's row whose decision never reached the run and that nothing may still
send, while the call still waits: a later decision of a person takes it over (`take_over`,
#7588). Two approvals are two decisions when they approve another input or were shown
another printer or draft. An approval of a call that names no printer or draft takes no
`bound_hash` (422 `flow-call-not-bound`). The row of a grant or of the starter session is never
taken over (#7675), and a grant's use is never given back (`RuleUse` in
`backend/scadbuddy/flows/grants.py`).

**A late Update.** When a replaced denial reaches the run first, the call is denied and the
denial is on the row again. When a replaced approval reaches the run first, the row does not
change: the call does what the new decision says.

## Code

- UI: `frontend/src/pages/WorkflowsPage.tsx`, `FlowPage.tsx`, `WorkflowRunPage.tsx`,
  `frontend/src/components/flows/FlowEditor.tsx:106`. A question:
  `frontend/src/components/flows/PendingCard.tsx`, `TaskCard.tsx` (a prompt's card),
  `SchemaForm.tsx` (the form, which Start run also uses), `TaskPanel.tsx` (the context and
  the form, shared by the card, the station and the code page), `ContextBlocks.tsx`,
  `widgets/` (the controls), `inputsForm.tsx` and `frontend/src/lib/flowInputs.ts`.
  Stations: `frontend/src/pages/StationPage.tsx`, `CodePage.tsx`.
- Routes: `backend/scadbuddy/api/flows.py` (`register_flow`, `start_flow_run`),
  `backend/scadbuddy/api/flow_triggers.py:65`, `backend/scadbuddy/api/flow_script.py:18`,
  `backend/scadbuddy/api/tasks.py`. The projects worker's task routes:
  `backend/scadbuddy/api/crud_tasks.py`, client `backend/scadbuddy/crud/tasks.py`
  `RemoteTasks`.
- Runs: `backend/scadbuddy/flows/launch.py:54`, `backend/scadbuddy/workflows/project.py:126`,
  `flow_tools.py` (`FLOW_TOOLS`, `SCRIPT_TOOLS`), `flow_activities.py`, `flow_routes.py:192`,
  `flow_grants.py`.
- Tasks: `backend/scadbuddy/tasks/` (`models.py`, `schema.py`, `widgets.py`, `answer_check.py`,
  `validate.py`, `store.py`, `component.py`).
- What a flow's author reads of the host functions: the description of the agent's
  `register_flow` tool (`agent/src/tools/flows.ts`).
- Definitions in git under `data/flows` (`backend/scadbuddy/flows/repository.py`).
- Triggers: `backend/scadbuddy/flow_triggers/fire.py:113`, `events.py:170`,
  `backend/scadbuddy/workflows/flow_triggers.py:55`.
- Agent tools: `agent/src/tools/flows.ts`, `flowTriggers.ts`.

## Data

- Tables: `workflow_definitions`, `workflow_runs`, `workflow_run_decisions`,
  `workflow_flow_states`, `flow_grants`, `flow_grant_uses`, `flow_triggers`,
  `flow_trigger_fires`, `flow_trigger_grants`, `tasks` (`20261124T0449Z_tasks`);
  `ai_flow_blobs` for a run's images; flow payloads sealed per subject (`ai_payload_keys`).
- Only the API has SQL on these tables. The projects worker writes a task through its CRUD
  area `tasks`. A `tasks` row holds the question and the answer as plain `jsonb`.

## Tests

- Backend: `backend/tests/flows/`, `backend/tests/test_flow_store.py`,
  `test_flow_trigger_*.py`, `backend/tests/api/test_flows.py`, `test_flow_grants.py`,
  `test_projects_worker_nodb.py`. Two decisions on one call:
  `backend/tests/flows/test_decision_take_over.py`. Prompts and tasks: `backend/tests/flows/test_prompt.py`,
  `backend/tests/tasks/`, `backend/tests/api/test_tasks.py`, `test_flow_prompt.py`,
  `backend/tests/crud/test_crud_tasks.py`, and the histories that
  `backend/tests/flows/test_project_replay.py` replays.
- Frontend: `frontend/src/components/flows/PendingCard.test.tsx`, `SchemaForm.test.tsx`,
  `frontend/src/lib/flowInputs.test.ts`.
- e2e: `frontend/e2e/workflows.spec.ts`, `frontend/e2e/flow-station.spec.ts`.

## Limits and known issues

- Some rare Resets can leave a task row behind its run: one made by hand in Temporal, and one
  through ScadBuddy whose commit was lost and which was then tried again
  ([#7413](https://github.com/eh-homelab/ScadBuddy/issues/7413)).
