# Flow answerers (B1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person can set who besides people may answer a flow's question, an allowed assistant answers with a tool, and the record says who answered.

**Architecture:** The CRUD service holds the setting and decides at the two answer routes. An agent's answer runs as an operation kind of its own, whose check asks the facts again over a CRUD route. The decision row names the answerer. The run's waiting entry says whether a question is for people only.

**Tech Stack:** Python 3.12, FastAPI, psycopg, Temporal + temporal-agent-harness, TypeScript agent, React 19 + vitest + msw.

**Spec:** `docs/superpowers/specs/2026-11-25-flow-answerers-design.md`. Issue #7471. The plan covers B1 (§6). Starts when #7588 is on `main`, because both change `flows/operations.py` and `flows/store.py`.

## Global Constraints

- With the default setting every caller that is not a person gets 403, as before. A person is never refused by the policy.
- Only the CRUD service holds SQL. The projects worker asks through `crud_router("projects")` routes (root CLAUDE.md "Database access").
- A person's Update id and operation key keep the same bytes. `responder` is in an agent's only.
- `responder` is `browser`, `session:<id>` or `token:<id>` for an answer, and never starts with `grant:`.
- `FlowWaiting.people_only` is `bool | None`, default `None`; only `False` lets an agent answer.
- `ProjectWorkflow` is AUTO_UPGRADE: every history in `backend/tests/fixtures/project_workflow_histories/` replays.
- A migration is a NEW file `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`. A backend flow migration has its copy in `agent/test/fixtures/backend-flow-migrations/`.
- The answer forms stay `USER_ONLY`. Nothing in the browser answers for the assistant.
- Never commit `backend/openapi.json` or a `schema.d.ts`. UI tasks load the frontend-design skill first.
- Docs in Simplified Technical English, with no counts. Every statement the change makes false is corrected in the same task.

## Review Focus

1. An agent's request whose author headers are forged, or that comes without the `agent` credential: refused with every value set, in each auth mode. → Task 4.
2. A session that a flow started, its fork, a session started from it, a flow's turn in a browser session, and a session handed to another owner: refused. → Task 4.
3. A person and an agent give the same answer at once: one wins, the row names the winner, the other is told that it was answered already. → Task 3.
4. A person's answer in flight while the workers roll: not sent twice under two ids, not refused. → Task 3.
5. A Reset between the route and the operation puts a people-only question under the same run: the operation refuses. → Task 4.

---

### Task 1: The setting

**Files:**
- Modify: `backend/scadbuddy/library/settings_store.py`, `api/settings.py`, `core/setting_docs.py`
- Create: `backend/scadbuddy/flows/answerers.py`
- Test: `backend/tests/test_settings*.py` (beside the print watcher's), `backend/tests/flows/test_answerers.py`

**Interfaces:**
- Produces (`flows/answerers.py`): `AnswererRule = Annotated[str, StringConstraints(pattern=…)]` for `starter`, `sessions` and `token:<uuid>`; `MAX_ANSWERERS: Final = 100`.
- Produces: `StoredSettings.flow_answerers: list[str] | None`, `SettingsPatch.flow_answerers: list[AnswererRule] | None`, `SettingsView.flow_answerers: list[str]` (default `[]`), a `SettingDoc`.
- Produces (`api/settings.py`): `FLOW_ACCESS = frozenset({"flow_answerers"})` and `_flow_access_by_a_person(patch)`, which looks at `model_fields_set` and `reset`, as `_mail_connection_by_a_person` does.

- [ ] **Step 1: Failing tests.** `test_flow_answerers_default_is_people_only` (`[]`); `test_a_person_sets_and_resets_flow_answerers`; `test_an_agent_neither_sets_nor_resets_flow_answerers` (403, type `PERSON_ONLY`, stored value unchanged, for a patch, a `null` and `reset`); `test_an_unknown_answerer_is_refused` (422 for `everyone`, `token:`, `token:x y`, `grant:1`).
- [ ] **Step 2: Run them, see them fail** (`uv run --frozen pytest <files>`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, see them pass; ruff, format, mypy.**
- [ ] **Step 5: Commit** `feat(settings): flow_answerers, a person's setting (#7471)`.

### Task 2: `people_only`

**Files:**
- Modify: `backend/scadbuddy/workflows/flow_tools.py`, `workflows/flow_models.py`, `flows/typecheck.py`, `flows/flow_steps.py` (wherever `waiting_on` is written), `api/flows.py` (`PendingEntry`, every path that builds one), `agent/src/tools/flows.ts` (the host-function text), `frontend/src/mocks/flows.ts`
- Test: `backend/tests/flows/test_project_replay.py`, the `wait_for_human` and `prompt` tests, the typecheck tests

**Interfaces:**
- Produces: `wait_for_human(question, timeout_s=None, people_only=False)`; `prompt(..., people_only=False)`, the last keyword; a value that is not a bool raises `ValueError` before the call parks.
- Produces: `FlowWaiting.people_only: bool | None = None`; `PendingEntry.people_only: bool` (`True` unless the entry has `False`).

- [ ] **Step 1: Failing tests.** `test_a_question_is_open_unless_the_script_says_people_only` (`False` in the entry for both functions); `test_a_people_only_question_says_so`; `test_an_entry_without_the_flag_reads_as_people_only`; `test_people_only_must_be_a_bool`; `test_the_stored_histories_replay` (existing) plus one recorded history with `people_only=True`.
- [ ] **Step 2: See them fail.**
- [ ] **Step 3: Implement.** No new command and no new random draw; a refusal that a stored history could meet goes behind a per-call patch.
- [ ] **Step 4: See them pass; ruff, format, mypy.**
- [ ] **Step 5: Commit** `feat(flows): a question may be for people only (#7471)`.

### Task 3: The record names the answerer

**Files:**
- Create: `backend/scadbuddy/migrations/<stamp>_decision_admitted_by.sql` (`workflow_run_decisions.admitted_by text`, nullable) and its agent copy
- Modify: `backend/scadbuddy/flows/models.py` (`Decision.admitted_by: str | None = None`), `flows/store.py`, `flows/operations.py`, `crud/projects.py`, `api/crud_projects.py`
- Test: `backend/tests/test_flow_store.py`, `tests/flows/test_flow_operations.py`, `tests/crud/test_crud_projects_runs.py`

**Interfaces:**
- Consumes: #7597's `take_over_answer`, `withdraw_answer`, `answer_update_id`.
- Produces: `answer_update_id(keys, decision)` puts the responder in the tag when it is not `browser`.
- Produces: `FlowStore.record_decision` refuses an answer whose row has another responder; `take_over_answer` sets `responder` and `admitted_by`; `put_back` restores both. A person takes over any answer; an agent only its own responder's.
- Produces: `_decision(request, checked, …)` takes `responder` from `request.get("responder", "browser")`.

- [ ] **Step 1: Failing tests.** `test_a_persons_update_id_has_not_changed` (the literal id of a fixed key and decision, computed on `main`); `test_the_same_answer_of_two_answerers_is_two_answers`; `test_a_person_takes_over_an_agents_lost_answer` (row: responder `browser`, `admitted_by` null); `test_an_agent_takes_over_only_its_own_lost_answer` (409 for another's and for a person's); `test_putting_back_restores_the_answerer`; `test_withdraw_deletes_only_its_own_answerers_row`.
- [ ] **Step 2: See them fail.**
- [ ] **Step 3: Implement in every comparison:** `_same` (both), `_record_decision`, `_take_over_answer`, `_withdraw_answer`, `take_over`'s scan of operations, `give`'s `sent`.
- [ ] **Step 4: See them pass, with the whole of `tests/flows`, `tests/tasks`, `tests/crud`; ruff, format, mypy.**
- [ ] **Step 5: Commit** `feat(flows): an answer's row names who gave it (#7471)`.

### Task 4: The policy and the routes

**Files:**
- Modify: `backend/scadbuddy/flows/answerers.py`, `flows/store.py`, `flows/operations.py`, `api/flows.py`, `api/tasks.py`, `api/crud_projects.py`, `crud/projects.py`, `auth/routes.py`
- Test: `backend/tests/flows/test_answerers.py`, `tests/api/test_flows.py`, `tests/api/test_tasks.py`, `tests/auth/test_routes.py`, `tests/crud/`

**Interfaces:**
- Produces (`flows/answerers.py`):
  - `class Answerer(BaseModel)`: `kind: Literal["person", "session", "token"]`, `id: str | None`, `principal: str | None`, `owner: Literal["browser", "token", "other"] | None`; `responder: str` (property).
  - `def answerer_of(caller: Caller, author: AgentAuthor | None) -> Answerer | None`: `None` when nobody may answer as it (no `agent` credential, a flow-run header, no session and no token). It does not read the database.
  - `def admitted(answerer: Answerer, rules: Sequence[str], started_by: dict[str, Any]) -> bool`
- Produces (`FlowStore`): `async def answer_session(session: str, principal: str) -> SessionOwner | None`, where `SessionOwner` has `kind` (`browser` or `token`) and `token_id`. It is None unless the session is not archived, is the principal's, has `background_tools` and not `turn_flow`, and its owner holds `write`; None on `UndefinedTable`. And `async def answer_token(token_id: str) -> bool`: not revoked, not expired, and its tier has `write`.
- Produces: CRUD `POST /internal/crud/projects/decisions/answerer` (that the answerer's session or token may still answer), and its `RemoteFlowStore` method.
- Produces: operation kind `AGENT_ANSWER = "flow_agent_answer"` on `projects`, for text and task answers (`task_id` in the request says which), keyed with `responder` and `admitted_by`. Its check refuses `people_only` other than `False` and asks the CRUD route.
- Produces: problem type `https://scadbuddy.dev/problems/flow-answer-not-allowed` (403), whose detail names the setting `flow_answerers`, or says that the question is for people only.

- [ ] **Step 1: Failing tests.** The cases of spec §8 that name a caller, a value of the setting, a session's state, a token's state, `starter` or `people_only`, on both routes, each in `off`, `observe` and `enforce` where the mode matters. Also `test_a_repeat_answers_its_first_answer_after_the_question_is_gone`, `test_a_reset_to_a_people_only_question_refuses_in_the_operation`, `test_a_worker_without_the_kind_records_nothing`, `test_a_token_without_write_is_refused`, and `test_an_agents_task_answer_records_its_responder` (the task's `answered_by` is `session:<id>` or `token:<id>`; a person's stays the principal).
- [ ] **Step 2: See them fail.**
- [ ] **Step 3: Implement.** The policy runs in `before_start`. `answer_task` and `answer_flow_run` leave `PERSON_ENDPOINTS` and no longer call `browser_only`; `tests/auth/test_routes.py` keeps its rule for the routes that do.
- [ ] **Step 4: See them pass, with `tests/flows`, `tests/api`, `tests/auth`, `tests/crud`; ruff, format, mypy.**
- [ ] **Step 5: Commit** `feat(flows): the setting says who else may answer a question (#7471)`.

### Task 5: The tool

**Files:**
- Modify: `agent/src/tools/flows.ts`, `agent/src/tools/coverage.ts`, `agent/src/tools/touched.ts`, the registry index
- Test: `agent/test/flows.test.ts`, `coverage.test.ts`, `touched.test.ts`, `projections.test.ts` and the access vectors they name

**Interfaces:**
- Produces: tool `answer_flow_question`, label "Answer a flow's question", `risk: 'outward'`, not for flows. Input `{run_id: string, call_id: string, answer: string | object}`. A string goes to `POST /workflow-runs/{run_id}/answer`; an object to `POST /tasks/{call_id}/answer`, after a read of the task shows that its run is `run_id`.

- [ ] **Step 1: Failing tests.** `sends a text to the run's answer route`; `sends an object to the task`; `refuses a task of another run`; `gives the 403's reason to the model`; `is refused in a flow run`; `waits for an approval at its default access`.
- [ ] **Step 2: See them fail. Step 3: Implement. Step 4: See the whole agent unit suite and typecheck pass.**
- [ ] **Step 5: Commit** `feat(agent): answer_flow_question (#7471)`.

### Task 6: The screens

**Files:**
- Modify: `backend/scadbuddy/api/flows.py` (`answered_by` and `admitted_by` of an answered question), `flows/store.py` (decisions of kind `answer` by call id), `tasks/models.py` if the task's read needs `admitted_by`
- Modify: `frontend/src/pages/settings/` (`fields.ts`, `values.ts`, the flows or assistant section), `frontend/src/components/flows/TaskCard.tsx`, the run page's answered questions, mocks
- Test: the backend read tests, the vitest files beside each component, one Playwright case in the settings e2e

**Interfaces:**
- Consumes: `flow_answerers`, the responder texts.
- Produces (`frontend/src/lib/flows.ts`): `answererLabel(by: string | null): {text: string, sessionId?: string}`: "You" or the principal for a person, "The assistant" with its session, "MCP token <name or id>".

- [ ] **Step 1: Failing tests.** The setting shows "People only" at the default, two choices (the session that started the run; any of your sessions) and the list of MCP tokens by name; saving writes the list; a revoked token that is still in the list is shown and can be removed. The task card and the run page name the assistant and link its session. A people-only question says so.
- [ ] **Step 2: See them fail. Step 3: Implement (frontend-design first). Step 4: vitest, typecheck, eslint, the e2e case.**
- [ ] **Step 5: Commit** `feat(frontend): who may answer a flow's questions, and who did (#7471)`.

### Task 7: `GrantSpec.answers` for B2, and the docs

**Files:**
- Modify: `backend/scadbuddy/flows/grants.py`, `docs/features/flows.md`, `docs/ai/browser-bridge.md`, `docs/ai/user-guide.md`, `docs/security/trust-boundaries.md`, `backend/CLAUDE.md`, `frontend/CLAUDE.md`, and every other copy that this change made false (`git grep -niE "only a person (answers|may answer)|never by an agent|a person answers"`)
- Test: `backend/tests/flows/test_grants.py`

**Interfaces:**
- Produces: `class GrantAnswers(BaseModel)`: `who: list[AnswererRule]` (1 to `MAX_ANSWERERS`); `GrantSpec.answers: GrantAnswers | None = None`, left out of every dump when it is None, and refused by `validate_spec` when it is not.

- [ ] **Step 1: Failing tests.** `test_a_spec_without_answers_dumps_without_the_key` (plain `model_dump`, JSON mode, and through the grant and trigger stores); `test_a_spec_with_answers_is_refused_until_b2`; `test_a_stored_spec_with_answers_still_loads`.
- [ ] **Step 2: See them fail. Step 3: Implement, correct the docs. Step 4: See them pass; markdownlint; mypy.**
- [ ] **Step 5: Commit** `feat(flows): grants know the answers field (#7471)`.
