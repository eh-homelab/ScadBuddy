# Durable phase 6: Code Mode flows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent (or anyone, through the API) registers a Python script as a flow, starts runs of it, and each run executes durably for as long as it needs. It runs as `ProjectWorkflow` on queue `projects`, a model-free temporal-agent-harness agent, in Code Mode over ScadBuddy's host functions. People answer its questions and approve its outward calls on a Workflows page, and an operator changes course with Temporal Reset.

**Architecture:** `ProjectWorkflow` is an `@agent.defn` with one model-free `@agent.accepts` handler, `execute(RunFlow)`, which runs the script through `agent.code_mode_tool([...host functions])` with `runner.run_tool`. That is the shape of the harness's `agent_dag` `DagBuilderAgent.execute`. The run is started by update-with-start through the harness's own `AgentClient.start_and_submit_message`. The route answers with the run id at acceptance, and the workflow's first activity writes the `workflow_runs` row. Approvals and `wait_for_human` answers use the harness's own Updates (`tool_approval`, `provide_callback_result`), sent after ScadBuddy records the decision in an operation. Pending entries are read with the harness's `agent_status` Query. Flow payloads are sealed per run by the subject payload codec that phase 5 built for `flow-<uuid>`.

**Tech Stack:** Python 3.12, `temporalio` 1.34 (`>=1.33.0,<1.35`), `temporal-agent-harness[code-mode]` 0.6.0 at `04a49d1b037de46da2cba31676d2aa2f4c59118f` (pulls `pydantic-monty==1.0.0`), psycopg 3, FastAPI; TypeScript tools in `agent/` (`@temporalio/*` 1.24.0 unchanged); React 19 for the page.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §7 (all of it), §4.2–§4.3, §6.5 (codec, `flow-<uuid>`), §6.6 (the flow rows of its tables), §8 ("Flows"), §10 phase 6. The user's decisions A–C below override §7.2 and §7.3 where they differ. Issue #1057, epic #1058. Depends on #1056 (phase 5), only where a task says so.

## Phase 6 is split into eight sub-PRs

Each one ships on its own, merged in order unless the "Depends on" column says otherwise. A sub-PR marked **waits on #1056** does not start until that issue is closed. Phase 5's remaining work (5c PR 3, 5d, 5e) touches `agent/src/temporal/toolActivities.ts`, `agent/src/sessions/*` and `agent-durable/`, and this plan never edits those files before then.

| Sub-PR | Scope | Depends on |
|---|---|---|
| **6a** | The harness pin and its monitor; the backend port of the subject payload codec; the harness-facts tests that pin every behaviour this plan relies on | — |
| **6b** | `workflow_definitions`, `workflow_runs`; `ProjectWorkflow` with `sleep` and `wait_for_human`; `python -m scadbuddy.worker --queue projects` and the in-process flag; register, list, start, get; the orphan sweep; agent tools | 6a |
| **6c** | `workflow_run_decisions`; `POST …/answer` and `POST …/decide` as `projects` operations that record and then call the harness Updates; the outward host functions `render`, `print`, `arrange` | 6b |
| **6d** | `tool(name, args)` over `agent-tools`, and the agent's `flow-<uuid>` branch in `toolActivities.ts` | 6c; **waits on #1056** |
| **6e** | Reset preview and Reset (`projects` operation `flow_reset`), and their tools | 6c |
| **6f** | The Workflows page, and the assistant panel's link to a session's runs | 6c (6e for the history link) |
| **6g** | `agent(...)` and `ask_session(...)` | 6d; **waits on #1056** (5c PR 3 and 5d: durable sessions are created and sent through the agent service) |
| **6h** | Deploy: the `scadbuddy-projects` Deployment and its pin in `deploy.reusable.yml` | 6b (ship once 6c lands) |

## User decisions (binding; they override spec §7.2/§7.3)

- **A. Approvals.** A person approves or denies on the Workflows page. ScadBuddy's route runs a `projects` operation (`flow_decide`). Its activity first records the decision in `workflow_run_decisions`, then calls the harness's `tool_approval` Update through `AgentClient.approve_tool`. ScadBuddy adds no Update or Query of its own to `ProjectWorkflow` (no `respond`, no `pending_input`). It never calls a private harness member.
- **B. `wait_for_human`** is a harness callback tool (`@agent.callback_tool_defn`) with a typed result. It is answered the same way as an approval: the `flow_answer` operation records the answer, then calls `provide_callback_result` through `AgentClient.provide_callback_result`. The page reads pending approvals and callbacks with the harness's `agent_status` Query (`AgentClient.get_status`).
- **C. Run creation answers at acceptance.** `POST /api/v1/workflows/{id}/runs` type-checks the script with `code_mode_type_check`, starts the run, and answers with the run id (202). The row is written moments later by the run's first activity. Until it exists, `GET /api/v1/workflow-runs/{id}` answers `status: "starting"` from the execution.

## What the pinned harness is (measured 2026-10-07 on the spike, re-read 2026-10-09)

Facts this plan relies on. 6a pins each one with a test (Task A3), so a pin bump that changes one fails there first.

- **Package.** PyPI `temporal-agent-harness` 0.6.0 is tag `0.6.0`, commit `04a49d1b037de46da2cba31676d2aa2f4c59118f`. `requires-python >=3.11`, `temporalio>=1.31.0`, `pydantic>=2.0`. The `code-mode` extra is exactly `pydantic-monty==1.0.0`. Main (`f843bc8`) adds a virtual filesystem and starts an unawaited host call before it is awaited. This plan pins 0.6.0, where a call starts only when awaited.
- **No session manager.** A model-free `@agent.defn` with `execute` runs under `AgentHarnessPlugin(tools=...)` on the worker. `AgentClient.start_and_submit_message` is update-with-start (`WithStartWorkflowOperation(..., id_conflict_policy=USE_EXISTING)`) onto the agent workflow itself. It takes no id-reuse policy, Search Attributes or memo; `update_id` is its idempotency key.
- **The workflow never ends by itself.** After `execute` returns, the runner waits for the next message. Only the `close` Signal completes it (`agent_workflow.py:2112`, the harness's own `stop_subagent` and web app send it). On close the harness auto-denies pending approvals and times out pending callbacks.
- **Handlers every agent workflow registers:** Updates `send_agent_message`, `tool_approval`, `provide_callback_result`, `__temporal_workflow_stream_poll`; Queries `agent_status`, `agent_interface`, `__temporal_workflow_stream_offset`; Signals `close`, `__temporal_workflow_stream_publish`. ScadBuddy adds none.
- **Code Mode.** `agent.code_mode_tool(tools, *, name, inherently_safe=True, auto_approval_criteria=None, injections=None)` returns an inline tool `(script: str) -> str`. The string is `"Script error (<Exc>: msg)"`, `"output:\n…\nresult: <repr>"` or `"result: <repr>"`. A script error is returned, never raised.
  - `agent.code_mode_type_check(tool, script) -> str | None` is async, works outside a workflow, and returns `None` or `"MontySyntaxError: …"` / `"MontyTypingError: …"` with line-numbered diagnostics.
  - A failed host call reaches the script as an exception at its `await`. A failed activity is `Exception("ApplicationError: <msg>")`, a denial is `Exception("ToolApprovalDenied: …")`, and a built-in exception keeps its type.
  - `asyncio.gather` runs host calls concurrently, each its own `run_tool`. `asyncio.sleep` is a durable timer. The script may compute for at most 1.0 s between host interactions. It can import only `asyncio`.
  - The sandbox steps **inside the workflow**: a process-wide `pydantic-monty` subprocess pool on the workflow worker, with a 1 s request timeout live and 3 s on replay. There are no stepping activities. So the `projects` worker carries the `code-mode` extra (a change from spec §3.2's wording).
  - Arguments are coerced to each parameter's type. Results go to the script as `model_dump(mode="json")`, and the type-check stubs render models as `TypedDict`s. A model or tool module must not use `from __future__ import annotations`.
- **Tools.** `@agent.activity_tool_defn(*, inherently_safe=False, auto_approval_criteria=None, activity_config=None, name=None)`; `@agent.tool_defn(*, inherently_safe=False, auto_approval_criteria=None)` runs inline in the workflow, where any workflow API works; `@agent.callback_tool_defn(*, inherently_safe=False, auto_approval_criteria=None, name=None, timeout=None)` has a body of exactly `...` and a required return annotation, which validates the result. An injected parameter is `Injected[AgentWorkflowRunner]`, hidden from the script.
  - An `activity_tool_defn` routed to another queue passes a trailing `AgentToolContext` argument, and the lifecycle events are published by the Python activity body. So a TypeScript activity must be called from an inline `tool_defn` with `workflow.execute_activity(name, ..., task_queue=...)`, as the harness documents.
  - `runner.run_tool(call_id, tool, /, *args, injections=None, **kwargs)` applies the tool's approval gate in the tool's own prologue. So a tool body may call `run_tool` on another tool and gets that tool's gate (Task A3 pins it).
- **Approvals.** `ToolApprovalPolicy.allow_inherently_safe()` lets a tool marked `inherently_safe` through and parks every other call until `tool_approval`. There is **no approval timeout**: a call waits until it is decided or the workflow closes. `approve_tool(tool_id, *, approved, reason=None, remember=False, update_id=None)`; an unknown or settled id raises `ToolApprovalError` (`UnknownToolApproval`, `ToolApprovalAlreadyResolved`).
- **Callbacks.** `provide_callback_result(tool_id, *, result=None, error=None, update_id=None)`. Errors are `UnknownCallback`, `CallbackAlreadyResolved` and `MalformedCallbackResult`; a malformed result does not consume the entry.
  - The callback gate's only timeout is the decorator's, fixed for every call.
  - **A cancelled wait keeps its entry** (measured in 6a, facts case 7). Cancelling the `run_tool` of a callback, for example with `asyncio.wait_for`, skips the gate's `finalize_callback`. The entry stays in `agent_status.pending_callbacks` until the workflow closes, and a late `provide_callback_result` for it is accepted, with nobody waiting.
  - So a per-call timeout never cancels the wait (Ruling 11).
- **Events.** Topic `turn_events`. `tool_start` (`tool_id`, `tool_name`, `tool_input`), `tool_end` (`tool_output: str`) and `tool_error` per call. In Code Mode each host call's `tool_id` is `workflow.uuid4()`. There is no continue-as-new in the harness.
- **Large payloads.** `AgentHarnessPlugin(large_payload_offload=...)` defaults to local-disk storage, which is single-host only. ScadBuddy passes `None` (Ruling 9).
- **Reset** (spike, 0.6.0 and main):
  - To after host call N, calls up to N are not run again.
  - To before `execute`'s Update was accepted, the server re-applies the Update and the whole script runs again.
  - A child started before the reset point is re-attached.
  - A child with a deterministic id that is still running when you reset to before its start makes the replayed start raise `WorkflowAlreadyStartedError`, which the script sees as that host call failing (Ruling 4).

## Rulings (where the spec is silent or the harness differs; the spec governs the rest)

- **Ruling 1, the pin.** `temporal-agent-harness[code-mode] @ git+https://github.com/temporal-community/temporal-agent-harness@04a49d1b037de46da2cba31676d2aa2f4c59118f`, a direct reference by full SHA, as phase 5 pinned its SDK. The SHA is the PyPI 0.6.0 release, so the version and the commit are both fixed. A bump is its own PR that carries the diff between the two SHAs and passes the 6a facts tests and the 6b replay tests. A weekly workflow runs `uv lock --check` against the pin, like `agent-durable-pin.yml`.
- **Ruling 2, ids.** A run's id is a UUID. Its workflow id is `flow-<run id>`, the form the codec seals (`codec.py` `_SUBJECT`). The run id is `uuid5(FLOW_NAMESPACE, f"{definition_id}:{version}:{idempotency_key}")`, so a re-sent `POST` reaches the same workflow id. `Idempotency-Key` is required (428 without, as #1143 does for operations).
- **Ruling 3, a repeat never runs the script twice.**
  - The route reads `workflow_runs` by id first. A row answers the repeat (`repeated: true`), whatever state its execution closed in. That is §4.2's first guard, which covers every closed run, because a run that wrote no row did nothing.
  - While the execution is open, `USE_EXISTING` attaches, and `update_id = idempotency_key` makes Temporal answer the first `execute`'s acceptance rather than run a second one.
  - `execute` is `MidTurn.REJECT`, and it refuses (non-retryable `ApplicationError`, type `AlreadyExecuted`) once a script has started in the execution. So a different `update_id` sent to the same workflow id cannot start a second script.
- **Ruling 4, the child and request-id policy.** Every id a host call derives includes the Temporal **workflow run id** and the harness's call id: `<run id>:<workflow run id>:<call id>`. That covers a print's `request_id`, an approval's request id, and an agent-tools activity id (`tool-<call id>`).
  - So after a Reset, every host call past the reset point runs again as a new effect (§7.4: "Every host call after the reset point runs again"). The preview lists those calls before the person confirms.
  - Nothing replayed ever collides with a pre-Reset id (the spike's `WorkflowAlreadyStartedError`).
  - A call before the reset point is in history and is not run again.
- **Ruling 5, `render`, `print` and `arrange` call ScadBuddy's own routes.** They do not start `TemplatePipeline`, `PrintRun` or `Arrange` as child workflows.
  - `start_child_workflow` has no `id_conflict_policy`, so a child could not join a render other callers already started. That is phase 3f's finding for `RenderPreview`, and the same holds for `render-<render_key>`.
  - `PrintRun` takes its answer and refusals through its `accepted` Update (§5.1). A workflow cannot send update-with-start.
  - So each is two activities on `projects`: `flow_<fn>_start` POSTs the route at `SCADBUDDY_API_INTERNAL_URL` with the Ruling 4 request id as `Idempotency-Key` (or `request_id`), and `flow_<fn>_wait` follows the job or run with heartbeats until it settles (§3.1: frequent polling inside the activity).
  - The routes keep every check they make today, and the run's `print` gets §5's record, repeat rules and `may_have_queued` unchanged. The resulting `PrintRun` is not a child, so a Reset never re-attaches to it (§7.4). That changes the spec's "child" wording; it is not a deviation from Temporal (§9). See open question 3.
- **Ruling 6, the flow's principal.** `workflow_runs.started_by` is the request's author: `core/authorship.py` `current_author()` (`{kind: "agent", principal, session}`), or `{kind: "browser"}` when the header is absent. Its `tool(...)` calls run as that principal (6d). Decisions and answers come from the browser only. Both routes refuse a request that carries `X-ScadBuddy-Agent-Author` (403, `flow-entry-browser-only`), and they get no tool (§6.6: "flow entries have no route or tool a grant holder could reach").
- **Ruling 7, records.**
  - `workflow_runs` is keyed by `id` (the run id). `workflow_id` is unique (`flow-<id>`, one row per flow), and `workflow_run_id` is the execution's current run id, updated by every projection write. A Reset keeps the workflow id and changes the run id (§6.6). The first insert is `ON CONFLICT (id) DO NOTHING`.
  - `steps` holds one entry per host call: `{seq, fn, call_id, status, outward, started_at, ended_at, history_length, error?}`. It never holds an argument or a result. `history_length` is `workflow.info().get_current_history_length()` when the step started, which is how a Reset drops the steps past its point (6e).
  - `waiting_on` holds the parked calls `{call_id, kind: "approval" | "answer", fn, prompt?}`. `prompt` is the question of an `answer`, as §6.6's `answer` entries carry it on purpose.
  - Projection writes are local activities (`flow_project`), as `render_accept` is.
- **Ruling 8, the run ends with `close`.** After `execute`'s script returns, the handler writes the final status (`succeeded`, or `failed` for a `Script error`) and then runs the `flow_close` activity. That activity sends the harness's `close` Signal to its own workflow through `activity.client()`, so the execution completes. A run that is terminated or reset away is caught by the orphan sweep (Task B6).
- **Ruling 9, payloads.**
  - `AgentHarnessPlugin(tools=..., large_payload_offload=None)`: the default local-disk store would break across the API and worker pods. A script is capped at 64 KiB (413). A `tool(...)` result reaches the script as text (6d), as a durable session's does (5c Ruling 4).
  - The converter is `pydantic_data_converter` with `payload_codec=SubjectPayloadCodec` and `failure_converter_class=DefaultFailureConverterWithEncodedAttributes`. Only the flows client (`workflows/flows_client.py`) and the `projects` worker use it. The render, print and library clients keep `pydantic_data_converter`.
  - A child or route-started workflow has its own id, so its payloads pass through unsealed.
- **Ruling 10, the codec's third implementation.**
  - `backend/scadbuddy/workflows/payload_codec.py` ports `agent-durable/src/scadbuddy_durable/codec.py` unchanged in behaviour. It opens every vector in `agent/test/fixtures/payload-vectors.json` and re-seals each byte for byte.
  - It reads and creates `ai_payload_keys` rows under the same advisory lock and tombstone check (`ai_forgotten_subjects`) as the other two, with the backend's KEK (`core/secrets.py`, `SCADBUDDY_SECRET_KEY_FILE`).
  - Flows are refused 503 `flows-unavailable` without a KEK or without the `ai_payload_keys` table, never run unsealed. See open question 2.
- **Ruling 11, a parked entry's timer: `flow_entry_timeout`** (revised after 6a's facts case 7). The harness's callback timeout is fixed at decoration, and cancelling a callback's wait leaves its entry listed and answerable. So a per-call timeout runs beside the wait and ends the entry through the harness's own Update, as a person's answer would be (decisions A and B). It is built in #2058 (6a; this revision merges after it) as `backend/scadbuddy/workflows/flow_entries.py`:
  - `run_callback(run_tool, tool, timeout_s, *, call_id=None, **kwargs)` starts `runner.run_tool(call_id, tool, ...)` as a task and waits on it with `workflow.wait_condition(call.done, timeout=timeout_s)`.
  - When the timer fires, it runs the activity `flow_entry_timeout(EntryTimeout{workflow_id, call_id})`. That sends the public `provide_callback_result(call_id, error="timed out", update_id=f"timeout-{call_id}")` through `activity.client()`.
  - The harness then ends the call its own way, with `CallbackToolError`, and `run_callback` re-raises it as `TimeoutError`. The entry leaves `pending_callbacks`, and a late answer is refused (`CallbackAlreadyResolved`).
  - If an answer reached the harness first, the activity gets `CallbackAlreadyResolved` or `UnknownCallback`, returns `False`, and the caller gets the answer.
  - The activity has `start_to_close_timeout` 10 s and retries a transport failure at most 5 times, with Temporal's default backoff (1 s, doubling); the same Update id makes a resend safe. Any other refusal from the harness is non-retryable (`EntryRefused`), so the host call fails rather than retrying forever. The worst case before a timed-out call fails is therefore about 65 s past its timer: 5 attempts × 10 s, plus the 1 + 2 + 4 + 8 s backoff between them. A change to the retry policy changes this number.
  - **When the activity itself fails** (`EntryRefused`, or its 5 attempts spent), `run_callback` cancels its own wait on the callback and the host call raises `TimeoutError`, as a timeout that worked does (#2095). The harness's entry stays listed and answerable until the run closes (facts case 7): the callback gate's `wait_condition` also wakes on close and finalizes the call as closed (`agent_workflow.py`, `AgentWorkflowRunner.await_callback_result`, line 2786, at 04a49d1). An answer sent before then is accepted and unused, as with any answer to a closed question, and the run's step already shows the call `failed`.
  - The activity runs on `projects`, the worker that runs the flow (Task B4 registers it).
  - `wait_for_human(question, timeout_s=3600)` is the first user. It checks `10 <= timeout_s <= 86400`, raising `ValueError` at run time for a computed value. It runs the callback tool `human_answer` (`callback_tool_defn(inherently_safe=True, timeout=timedelta(seconds=86400))`, the decorator's timeout as a backstop) through `run_callback`, under the call id its step records (Task B3). On timeout the host call raises `TimeoutError` and never returns an answer (§6.6's table).
  - A literal `timeout_s` outside the range is refused at registration and at run start by `flows/typecheck.py`, an `ast` pass beside `code_mode_type_check`.
  - Approvals are the mechanism's second user once the approval timeout is decided (open question 5). That would be the public `tool_approval` deny (`approve_tool(call_id, approved=False, reason="timed out")`) at `approval_expiry_seconds`. It stays unwired until then.
- **Ruling 12, approvals have no timer** (pending open question 5). The harness has no approval timeout, and this plan adds none until the user decides. If they choose one, it is Ruling 11's mechanism. An outward call waits until a person decides, the run is reset, or the run is closed. The Workflows page shows how long each entry has waited.
- **Ruling 13, worker versioning.** The `projects` worker is unversioned, like `library` and `agent-tools`: a flow may run for weeks, and a pinned build would hold a drain for that long. So `ProjectWorkflow` sets no versioning behavior. Every change to it, to its host functions or to the harness pin must replay the recorded histories in `backend/tests/fixtures/project_workflow_histories/` (Task B4). A change that cannot replay goes behind `workflow.patched()`.

## Global Constraints

- Backend schema changes are new files in `backend/scadbuddy/migrations/` named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`. Never edit a merged one.
- The harness dependency is exactly Ruling 1's line. Never a fork, never vendored. `pydantic-monty` comes only through the `code-mode` extra.
- `temporalio>=1.33.0,<1.35`, as today. `@temporalio/*` pins are unchanged.
- No private harness member (a leading underscore, or anything not re-exported from `temporal_agent_harness.harness.agent`, `.agent_protocol`, `.agent_client`, `.agent_workflow` or `temporal_agent_harness.plugin`) is imported, called or patched, in code or tests.
- `ProjectWorkflow` registers no Update, Query or Signal of its own.
- Tool and model modules for the harness have no `from __future__ import annotations`.
- No secret, data key, KEK, script, host-call argument, host-call result or answer text is logged, traced or put in an exception message. The trace scrub (`core/trace_scrub.py`) applies.
- Every `/api/v1` route gets an agent tool or a `agent/src/tools/coverage.ts` `NOT_A_TOOL` entry in the same PR.
- Each sub-PR: `uv run --frozen ruff check .`, `ruff format --check .`, `mypy`, `pytest -n auto` in `backend/`; `pnpm lint && pnpm typecheck && pnpm test && pnpm build` in `agent/` and `frontend/` when touched.
- Every CI job runs on `ubuntu-latest`. A new `type=gha` scope uses `mode=min`.
- Conventional-commit titles. Each PR body says `Part of #1057`, and the last one says `Fixes #1057`.

## Review Focus

1. **A `POST …/runs` re-sent with the same `Idempotency-Key` after its run finished, or while it was starting.** Expect the same run id, `repeated: true`, and the script run exactly once: never a second execution or a second `execute`. Pinned in Task B5 (three cases: before the row exists, while running, after `close`).
2. **A worker restarted in the middle of a long script, or a run replayed on a new build.** Expect the run to resume where it was, with no host call run twice. Pinned by the replay fixtures and a kill-the-worker test (Task B4).
3. **A run terminated by an operator, reset away from its last state, or whose worker vanished.** Expect the row not to stay `running`/`waiting` forever: the orphan sweep marks it `terminated` (or re-reads it after a Reset) within one sweep (Task B6).
4. **An answer or decision sent twice, for a call id from before a Reset, or by the agent.** Expect 409 (`already-resolved`, `stale-entry`) or 403 (`flow-entry-browser-only`), and no `approved` row left behind for a call the harness refused. If the decision row is written but the Update is refused, the row is deleted in the same activity (Task C2).
5. **A script that is huge, never awaits, loops for more than 1 s between calls, or returns a 5 MB result.** Expect 413 at registration for size, a `Script error (TimeoutError …)` run outcome for the loop, and a result stored truncated to 4 KiB with `result_truncated: true`. Never a stuck workflow task or an oversized payload (Tasks B2, B4).

---

## File structure

Backend, new:
- `backend/scadbuddy/workflows/payload_codec.py`: the subject codec (Ruling 10).
- `backend/scadbuddy/workflows/flows_client.py`: `connect_flows`, the converter, and the `AgentHarnessPlugin` list.
- `backend/scadbuddy/workflows/project.py`: `ProjectWorkflow`, `RunFlow`, `FlowStart`.
- `backend/scadbuddy/workflows/flow_tools.py`: the host functions (harness tools). No `from __future__ import annotations`.
- `backend/scadbuddy/workflows/flow_entries.py`: `run_callback` and the `flow_entry_timeout` activity (Ruling 11; built in 6a).
- `backend/scadbuddy/workflows/flow_steps.py`: `step(...)`, the context manager every host function records its step and waits through.
- `backend/scadbuddy/workflows/flow_activities.py`: `FlowActivities` (record, project, close, and in 6c the route calls).
- `backend/scadbuddy/workflows/flow_models.py`: what crosses the history (`FlowStep`, `FlowWaiting`, `ProjectionWrite`).
- `backend/scadbuddy/flows/`: `store.py` (`FlowStore`: definitions, runs, decisions), `typecheck.py`, `component.py` (`FLOWS`, `FlowsDep`), `operations.py` (`OPERATION_KINDS` for queue `projects`: `flow_answer`, `flow_decide`, `flow_reset`), `models.py` (API models), `history.py` (6e).
- `backend/scadbuddy/api/flows.py`: the routes.
- Migrations `<ts>_workflow_definitions.sql`, `<ts>_workflow_runs.sql` (6b), `<ts>_workflow_run_decisions.sql` (6c).

Backend, modified: `pyproject.toml`, `uv.lock`, `core/settings.py` (queue, in-process flag), `worker.py` (`--queue projects`), `main.py` (in-process `projects` worker), `workflows/housekeeping.py` (the sweep), `operations/kinds.py` (queue `projects`).

Agent: `agent/src/tools/flows.ts` (new), `agent/src/tools/index.ts`, `agent/src/tools/coverage.ts`; 6d: `agent/src/temporal/toolActivities.ts`, `agent/src/temporal/flowRuns.ts` (new).

Frontend (6f): `frontend/src/pages/WorkflowsPage.tsx`, `frontend/src/pages/WorkflowRunPage.tsx`, `frontend/src/api/flows.ts`, `frontend/src/mocks/features/flows.ts`, `App.tsx`, `components/AppShell.tsx`.

CI: `.github/scripts/harness-pin.sh`, `.github/workflows/harness-pin.yml` (6a). Deploy (6h): `.github/workflows/deploy.reusable.yml`.

---

## 6a: the pin, the codec, the facts

### Task A1: Pin the harness

**Files:**
- Modify: `backend/pyproject.toml`, `backend/uv.lock`
- Create: `.github/scripts/harness-pin.sh`, `.github/scripts/harness-pin.test.sh`, `.github/workflows/harness-pin.yml`

**Interfaces:**
- Produces: `temporal_agent_harness` importable in the backend venv, with `pydantic_monty`.

- [ ] **Step 1: Add the dependency.** In `backend/pyproject.toml` `[project] dependencies`, add:

```toml
    # temporal-agent-harness 0.6.0 (PyPI) by its tag's full SHA, as phase 5 pins its
    # SDK (plan 2026-10-09-durable-phase-6-flows.md Ruling 1). The code-mode extra is
    # exactly pydantic-monty 1.0.0. A bump is its own PR carrying the diff between the
    # SHAs, and must pass tests/flows/test_harness_facts.py and the replay fixtures.
    "temporal-agent-harness[code-mode] @ git+https://github.com/temporal-community/temporal-agent-harness@04a49d1b037de46da2cba31676d2aa2f4c59118f",
```

and add, beside `[tool.hatch.build.targets.wheel]`:

```toml
[tool.hatch.metadata]
allow-direct-references = true
```

- [ ] **Step 2: Lock and check.**

Run: `cd backend && uv lock && uv sync && uv run python -c "import temporal_agent_harness, pydantic_monty; from temporal_agent_harness.harness import agent; print(agent.code_mode_tool)"`
Expected: prints a function. `git diff uv.lock` shows `temporal-agent-harness` with `source = { git = "…?rev=04a49d1b…#04a49d1b…" }` and `pydantic-monty` 1.0.0, and `temporalio` still 1.34.0.

- [ ] **Step 3: The pin monitor.** Copy `.github/scripts/agent-durable-pin.sh` to `harness-pin.sh`, pointed at `backend/` (`uv lock --check`, then a no-cache `uv sync --frozen --no-dev --no-install-project`), with a test script modelled on `agent-durable-pin.test.sh`. Copy `.github/workflows/agent-durable-pin.yml` to `harness-pin.yml` (weekly, `ubuntu-latest`, opens an issue on failure exactly as the original does). Run `shellcheck .github/scripts/harness-pin*.sh` and `actionlint .github/workflows/harness-pin.yml`.

- [ ] **Step 4: The image.** `docker build --target test -t scadbuddy:test .`. Expected: builds; `pydantic_monty` imports in `/opt/venv`. If its wheel has no build for the image's platform, stop and report (open question 1).

- [ ] **Step 5: Commit.** `git add backend/pyproject.toml backend/uv.lock .github/scripts/harness-pin.sh .github/scripts/harness-pin.test.sh .github/workflows/harness-pin.yml && git commit -m "build(backend): pin temporal-agent-harness 0.6.0 with code-mode (#1057)"`

### Task A2: The backend's subject payload codec

**Files:**
- Create: `backend/scadbuddy/workflows/payload_codec.py`, `backend/scadbuddy/workflows/flows_client.py`
- Modify: `backend/scadbuddy/core/secrets.py` (add `seal_bytes`, `open_bytes`)
- Test: `backend/tests/test_payload_codec.py`

**Interfaces:**
- Consumes: `core/secrets.py` `Kek`, `load_kek`; the agent's `ai_payload_keys` and `ai_forgotten_subjects` tables; `agent/test/fixtures/payload-vectors.json`.
- Produces:
  - `SubjectPayloadCodec(keys: PayloadKeys, subject: str | None = None)`, a `PayloadCodec` and `WithSerializationContext`.
  - `PgPayloadKeys(connect: Connect, kek: Kek, previous: Kek | None = None)` with `async key_for(subject: str, create: bool) -> bytes`.
  - `subject_of(workflow_id: str | None) -> str | None`.
  - `flows_converter(keys: PayloadKeys) -> DataConverter`.
  - `async connect_flows(address: str, namespace: str, keys: PayloadKeys, *, lazy: bool = False) -> Client`.
  - `harness_plugins(tools: Sequence[Callable[..., Any]]) -> list[AgentHarnessPlugin]`.

As built in #2058: the backend's tests run in an image that holds only `backend/`. So they read copies of `agent/test/fixtures/payload-vectors.json` and the agent's `20261009T0421Z_payload_keys.sql` under `backend/tests/fixtures/`. `.github/scripts/lint-codec-copies.sh` (lint job) fails CI when a copy differs from the agent's file, or when the backend's codec differs from agent-durable's outside its imports, docstrings and converter function.

- [ ] **Step 1: Failing vector test.**

```python
# backend/tests/test_payload_codec.py
import base64
import json
from pathlib import Path

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import WorkflowSerializationContext

from scadbuddy.workflows.payload_codec import (
    SubjectPayloadCodec,
    open_payload,
    seal_payload,
    subject_of,
)

VECTORS = Path(__file__).parents[2] / "agent/test/fixtures/payload-vectors.json"


def _vectors() -> list[dict[str, str]]:
    return json.loads(VECTORS.read_text())["vectors"]


@pytest.mark.parametrize("v", _vectors(), ids=lambda v: v["name"])
def test_opens_and_reseals_every_agent_vector(v: dict[str, str]) -> None:
    key = base64.b64decode(v["data_key"])
    sealed = Payload.FromString(base64.b64decode(v["sealed_payload"]))
    opened = open_payload(key, v["subject"], sealed)
    assert opened.SerializeToString() == base64.b64decode(v["payload"])
    resealed = seal_payload(key, v["subject"], opened, iv=base64.b64decode(v["iv"]))
    assert resealed.SerializeToString() == sealed.SerializeToString()


def test_subject_is_only_session_or_flow_uuid() -> None:
    assert subject_of("flow-0b0e2a0c-1111-4222-8333-444455556666") is not None
    assert subject_of("print-abc") is None
    assert subject_of("flow-not-a-uuid") is None


async def test_other_workflows_pass_through() -> None:
    codec = SubjectPayloadCodec(keys=None).with_context(  # type: ignore[arg-type]
        WorkflowSerializationContext(namespace="n", workflow_id="render-abc")
    )
    p = Payload(metadata={"encoding": b"json/plain"}, data=b"{}")
    assert await codec.encode([p]) == [p]
```

Before writing the test, read the fixture's actual field names (`jq '.vectors[0] | keys' agent/test/fixtures/payload-vectors.json`) and use them exactly. The names above are the expected ones, and the fixture is authoritative.

- [ ] **Step 2: Run.** `cd backend && uv run --frozen pytest tests/test_payload_codec.py -v`. Expected: FAIL, `ModuleNotFoundError: scadbuddy.workflows.payload_codec`.

- [ ] **Step 3: Port.**
  - Add `seal_bytes(key: bytes, data: bytes, context: str, iv: bytes | None = None) -> bytes` and `open_bytes(key: bytes, sealed: bytes, context: str) -> bytes` to `core/secrets.py`. Copy the bodies of `agent-durable/src/scadbuddy_durable/secrets.py:106-121`, over the module's existing `_seal`/`_open`.
  - Create `payload_codec.py` by copying `agent-durable/src/scadbuddy_durable/codec.py` and changing three things. Imports come from `scadbuddy.core.secrets`. The header says it is the backend's copy, pinned by the same vectors. `data_converter` becomes `flows_converter`, built over `pydantic_data_converter` (the backend's converter, which the harness requires) instead of `DataConverter.default`:

```python
def flows_converter(keys: PayloadKeys) -> DataConverter:
    """The converter of the flows client and the `projects` worker (Ruling 9)."""
    return dataclasses.replace(
        pydantic_data_converter,
        payload_codec=SubjectPayloadCodec(keys),
        failure_converter_class=DefaultFailureConverterWithEncodedAttributes,
    )
```

  - `flows_client.py`:

```python
"""The Temporal client of everything that touches a flow run (plan 6, Rulings 9-10)."""

from collections.abc import Callable, Sequence
from typing import Any

from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor

from scadbuddy.workflows.payload_codec import PayloadKeys, flows_converter


def harness_plugins(tools: Sequence[Callable[..., Any]]) -> list[AgentHarnessPlugin]:
    # No large-payload offload: its default is one host's disk (Ruling 9).
    return [AgentHarnessPlugin(tools=list(tools), large_payload_offload=None)]


async def connect_flows(
    address: str, namespace: str, keys: PayloadKeys, *, lazy: bool = False
) -> Client:
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=flows_converter(keys),
        lazy=lazy,
        interceptors=[TracingInterceptor()],
    )
```

  The plugin goes on the worker (Task B4), not the client: the harness installs `PydanticPayloadConverter` only over a default converter, and ours is already pydantic.

- [ ] **Step 4: Postgres and Temporal cases.** Add to the test file. Mark the Postgres cases `requires_postgres`. Their fixture creates `ai_payload_keys` and `ai_forgotten_subjects` in the throwaway schema by executing `agent/src/db/migrations/20261009T0421Z_payload_keys.sql` (and the file creating `ai_forgotten_subjects`, found with `rg -l ai_forgotten_subjects agent/src/db/migrations`). The cases:
  - a key created here opens with the agent's KEK through `PgPayloadKeys` from a fresh instance;
  - a tombstoned subject refuses to create a key (`SubjectForgottenError`);
  - `requires_temporal`: a `flow-<uuid>` workflow started through `connect_flows` has no plaintext of its input in `fetch_history()` read through a plain `pydantic_data_converter` client.

- [ ] **Step 5: Run.** `uv run --frozen pytest tests/test_payload_codec.py -v`. Expected: PASS (the Postgres and Temporal cases skip without their services).

- [ ] **Step 6: Commit.** `git add backend/scadbuddy/core/secrets.py backend/scadbuddy/workflows/payload_codec.py backend/scadbuddy/workflows/flows_client.py backend/tests/test_payload_codec.py && git commit -m "feat(flows): the subject payload codec in the backend, on the shared vectors (#1057)"`

### Task A3: The harness facts, as tests

**Files:**
- Create: `backend/tests/flows/__init__.py`, `backend/tests/flows/harness_probe.py` (test-only workflow and tools; no `from __future__ import annotations`), `backend/tests/flows/test_harness_facts.py`

**Interfaces:**
- Produces: a failing test the moment a harness bump breaks a fact in "What the pinned harness is".

- [ ] **Step 1: The probe agent.** It is the spike's `ProjectWorkflow` (`flows-spike/spike/flow_wf.py`), with these tools:
  - `step(n) -> StepResult` (activity, `inherently_safe=True`);
  - `outward(what) -> str` (activity, gated);
  - `human_answer(question) -> HumanAnswer` (`callback_tool_defn(inherently_safe=True, timeout=timedelta(seconds=86400))`);
  - `wait(question: str, timeout_s: int, runner: Injected[AgentWorkflowRunner]) -> HumanAnswer` (inline, `inherently_safe=True`), which runs `human_answer` through `run_callback(runner.run_tool, human_answer, timeout_s, question=question)` (Ruling 11);
  - `gated_via_nested(what: str, runner: Injected[AgentWorkflowRunner]) -> str` (inline, `inherently_safe=True`), which runs `outward` through `runner.run_tool`;
  - `child(n) -> int` (inline), which starts a child with id `probe-child-{workflow.info().workflow_id}-{workflow.info().run_id}-{n}` (Ruling 4).

  Each test file write goes to a temp file named by an environment variable, as the spike's `effects.jsonl` did.

- [ ] **Step 2: The tests.** Each one is `requires_temporal` and runs a `Worker(client, task_queue=q, workflows=[ProbeWorkflow, ProbeChild], plugins=harness_plugins(PROBE_TOOLS))`. The client is the `tests/support/temporal.py` dev server's, wrapped with `flows_converter(FakeKeys())`, where `FakeKeys` returns one fixed 32-byte key. Starting is `AgentClient(client, wf_id).start_and_submit_message("execute", {"script": s}, workflow_name="ProbeWorkflow", task_queue=q, start_config=AgentConfig(), update_id=key)`. The cases:
  1. `code_mode_type_check` outside a workflow returns `None` for a good script and a `MontyTypingError` string naming `main.py:3` for a bad one.
  2. `asyncio.gather(step(1), step(2))` runs both; the reply text is `result: 3`.
  3. The same `update_id` sent twice through `start_and_submit_message` gives one `execute` (effects file has one `step(1)`), and the second reply's `turn_number` equals the first's.
  4. A gated `outward` parks: `AgentClient.get_status().pending_approvals` lists it. `approve_tool(id, approved=True)` runs it once; a second `approve_tool` raises `ToolApprovalError` with `error_type == "ToolApprovalAlreadyResolved"`.
  5. `gated_via_nested` parks under the nested `run_tool`'s id: the gate is in the tool's prologue (the fact Ruling 11 and 6d rely on).
  6. A denial reaches the script as an exception whose text starts `ToolApprovalDenied`.
  7. `wait('q', 3)` with no answer, through `flow_entry_timeout`. Three assertions:
     - the script sees `TimeoutError`;
     - afterwards `get_status().pending_callbacks` is empty;
     - `provide_callback_result` for the old id raises `CallbackResultError` (`UnknownCallback` or `CallbackAlreadyResolved`).

     And the race: with a `flow_entry_timeout` that delivers an answer before calling the real activity, the real activity returns `False` and the script gets the answer.
  8. `wait('q', 60)` answered with `{"answer": "pink"}` returns it. A malformed result raises `MalformedCallbackResult`, and a corrected one is then accepted.
  9. After `execute` returns, the workflow is still `RUNNING`. The `close` Signal sent from an activity's `activity.client()` completes it.
  10. Reset to the workflow task after `step(1)` completed: `step(1)` does not run again and `step(2)` does. Reset to before the Update was accepted: the script runs again from the top (spike cases 4 and 5).
  11. Reset to before `child` started, with the child still running: the replayed start, under a new workflow run id, starts a new child and does not raise `WorkflowAlreadyStartedError` (Ruling 4).
  12. The handler set: every Update, Query and Signal name the running probe registers (from `agent_interface` and the history's accepted Update names, plus the workflow's described handlers) is in the "Handlers every agent workflow registers" list above. A new name fails the test (§8).

- [ ] **Step 3: Run.** `uv run --frozen pytest tests/flows/test_harness_facts.py -v`. Expected: all PASS with a Temporal dev server.

  If any case fails, the plan's premise is wrong. Stop, and report the case and its output to the user before writing more code (§9).

- [ ] **Step 4: Commit.** `git add backend/tests/flows/ && git commit -m "test(flows): pin the harness facts phase 6 relies on (#1057)"`

**6a PR:** `feat(flows): harness pin, backend payload codec, harness facts (#1057, phase 6a)`.

---

## 6b: runs, `ProjectWorkflow`, the `projects` worker

### Task B1: Migrations and `FlowStore`

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_workflow_definitions.sql`, `backend/scadbuddy/migrations/<ts>_workflow_runs.sql`, `backend/scadbuddy/flows/__init__.py`, `backend/scadbuddy/flows/store.py`, `backend/scadbuddy/flows/models.py`
- Test: `backend/tests/test_flow_store.py` (`requires_postgres`)

**Interfaces:**
- Produces:
  - `FlowStore(pool, events: TransactionalEvents)` with:
    - `create_definition(name: str, script: str, created_by: dict[str, Any]) -> Definition` (new version of `name`; the first is 1);
    - `get_definition(id: str) -> Definition | None`;
    - `list_definitions() -> list[Definition]` (latest version of each name);
    - `get_run(id: str) -> Run | None`;
    - `list_runs(*, definition_id: str | None = None, session: str | None = None, limit: int = 50) -> list[Run]`;
    - `insert_run(row: RunInsert) -> Run` (idempotent);
    - `project(run_id: str, write: ProjectionWrite) -> Run | None` (guarded).
  - Models `Definition{id, name, version, script, created_by, created_at}` and `Run{id, definition_id, version, name, status, waiting_on, steps, result, result_truncated, workflow_id, workflow_run_id, started_by, created_at, updated_at}`.
  - `RunStatus = Literal["starting", "running", "waiting", "succeeded", "failed", "terminated"]`.
  - Events `FlowRunEvent(topic="workflow-runs", run: Run)` through `publish_in`.

- [ ] **Step 1: Migrations.**

```sql
-- <ts>_workflow_definitions.sql (spec 2026-10-01 §7.3). Versions are immutable.
CREATE TABLE workflow_definitions (
    id         text PRIMARY KEY,
    name       text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    version    integer NOT NULL CHECK (version >= 1),
    script     text NOT NULL CHECK (octet_length(script) <= 65536),
    created_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);
```

```sql
-- <ts>_workflow_runs.sql: written by ProjectWorkflow's activities (plan 6 Ruling 7).
CREATE TABLE workflow_runs (
    id               text PRIMARY KEY,
    definition_id    text NOT NULL REFERENCES workflow_definitions (id),
    version          integer NOT NULL,
    name             text NOT NULL,
    status           text NOT NULL CHECK (status IN
                       ('running', 'waiting', 'succeeded', 'failed', 'terminated')),
    waiting_on       jsonb NOT NULL DEFAULT '[]',
    steps            jsonb NOT NULL DEFAULT '[]',
    result           text,
    result_truncated boolean NOT NULL DEFAULT false,
    workflow_id      text NOT NULL UNIQUE,
    workflow_run_id  text NOT NULL,
    started_by       jsonb NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX workflow_runs_definition ON workflow_runs (definition_id, created_at DESC);
CREATE INDEX workflow_runs_open ON workflow_runs (updated_at) WHERE status IN ('running', 'waiting');
CREATE INDEX workflow_runs_session ON workflow_runs ((started_by ->> 'session'), created_at DESC);
```

- [ ] **Step 2: Failing store tests.** Cases:
  - versions count up per name;
  - `insert_run` twice with the same id returns the same row and publishes one event (count `publish_in` calls on a recording `TransactionalEvents`);
  - `project` with a terminal status refuses to move it back to `running`, the guard of `render/projection.py`;
  - `project` replaces `workflow_run_id`;
  - `list_runs(session=…)` filters on `started_by.session`.

- [ ] **Step 3: Run, implement, run.** Run `pytest tests/test_flow_store.py -v`, which fails on the import. Implement `FlowStore` in the `operations/store.py` style:
  - one `with pool.connection() as conn, conn.transaction():` per write, and `publish_in` only when a row changed;
  - `project`'s guard is `WHERE id = %s AND status NOT IN ('succeeded','failed','terminated')`, except a `ProjectionWrite(reset=True)` (6e), which may reopen;
  - `steps` is merged by `call_id` in Python inside the transaction (`SELECT … FOR UPDATE`), never by concatenating JSON in SQL.

  Then run it again: PASS.

- [ ] **Step 4: Commit.** `git add backend/scadbuddy/migrations/*_workflow_definitions.sql backend/scadbuddy/migrations/*_workflow_runs.sql backend/scadbuddy/flows/ backend/tests/test_flow_store.py && git commit -m "feat(flows): workflow_definitions and workflow_runs (#1057)"`

### Task B2: Type check at registration and start

**Files:**
- Create: `backend/scadbuddy/flows/typecheck.py`
- Test: `backend/tests/flows/test_typecheck.py`

**Interfaces:**
- Consumes: `workflow_tools.FLOW_TOOLS` and `run_flow_tool()` (Task B3).
- Produces: `async check_script(script: str) -> list[ScriptProblem]`, empty when clean. `ScriptProblem{line: int | None, message: str}`. `MAX_SCRIPT_BYTES = 65536`.

- [ ] **Step 1: Failing tests.**

```python
async def test_clean_script_has_no_problems() -> None:
    assert await check_script(
        "import asyncio\nasync def main():\n    await sleep(1)\nasyncio.run(main())"
    ) == []


async def test_type_error_is_reported_by_line() -> None:
    problems = await check_script(
        "import asyncio\nasync def main():\n    await sleep('x')\nasyncio.run(main())"
    )
    assert problems and problems[0].line == 3


async def test_literal_wait_timeout_out_of_range() -> None:
    script = (
        "import asyncio\nasync def main():\n"
        "    await wait_for_human('swap spool', timeout_s=5)\nasyncio.run(main())"
    )
    assert [p.line for p in await check_script(script)] == [3]


async def test_oversized_script() -> None:
    assert (await check_script("#" * 65537))[0].message.startswith("script is over 64 KiB")
```

- [ ] **Step 2: Implement.**
  - Size first.
  - Then `agent.code_mode_type_check(run_flow_tool(), script)`. Parse its string into problems by the ` --> main.py:<line>:<col>` markers, keeping the whole text when no line is found.
  - Then an `ast` walk: a `wait_for_human` call whose `timeout_s` (keyword, or second positional) is an `int` constant outside 10–86400 is a problem at its line.
  - A `MontyCrashedError` from the check becomes one problem, `"the checker failed: try again"`, and is logged at warning without the script.

- [ ] **Step 3: Run.** `pytest tests/flows/test_typecheck.py -v`. Expected: PASS.
- [ ] **Step 4: Commit.** `git add backend/scadbuddy/flows/typecheck.py backend/tests/flows/test_typecheck.py && git commit -m "feat(flows): type check scripts, with wait_for_human's timeout range (#1057)"`

### Task B3: `ProjectWorkflow` and the first host functions

**Files:**
- Create: `backend/scadbuddy/workflows/project.py`, `backend/scadbuddy/workflows/flow_tools.py`, `backend/scadbuddy/workflows/flow_activities.py`, `backend/scadbuddy/workflows/flow_models.py`
- Test: `backend/tests/flows/test_project_workflow.py` (`requires_temporal`, `requires_postgres`)

**Interfaces:**
- Consumes: `FlowStore` (B1), `harness_plugins`, `flows_converter` (A2).
- Produces:
  - `PROJECT_WORKFLOW = "ProjectWorkflow"`.
  - `RunFlow(BaseModel){script: str}`.
  - `FlowStart(BaseModel){run_id, definition_id, version, name, started_by: dict, search_attributes: bool}`.
  - `FLOW_TOOLS: list` (6b: `sleep`, `wait_for_human`, `human_answer`; 6c and later append), and `run_flow_tool()`.
  - `FlowActivities(store: FlowStore)` with:
    - `flow_record(FlowRecord) -> None` (activity);
    - `flow_project(ProjectionWrite) -> None` (local activity);
    - `flow_close(str) -> None` (activity: signals `close` to the workflow id).

- [ ] **Step 1: Failing tests.** One worker per test: `Worker(client, task_queue=q, workflows=[ProjectWorkflow], activities=[*FlowActivities(store).all(), flow_entry_timeout], plugins=harness_plugins(FLOW_TOOLS))`. The cases:
  1. A script `await sleep(1); return 7`: the row goes `running` → `succeeded`, `result == "result: 7"`, two steps are recorded in order, and the execution completes (Ruling 8).
  2. `wait_for_human('Swap to pink?')`: the row is `waiting` with `waiting_on == [{call_id, kind: "answer", fn: "wait_for_human", prompt: "Swap to pink?"}]`, and `AgentClient.get_status().pending_callbacks` lists `human_answer`. `provide_callback_result(id, result={"answer": "ok"})` → the row is `succeeded` and `waiting_on == []`.
  3. `wait_for_human('q', timeout_s=10)` unanswered (time-skipping is not available with the harness's stream, so the test uses `timeout_s=10` and waits): the run is `failed` with `Script error (TimeoutError`, `waiting_on == []`, `pending_callbacks == []`, and a late `AgentClient.provide_callback_result` for the call raises `CallbackResultError` (`CallbackAlreadyResolved`). 6c's tests cover the route's 409.
  4. A `Script error` (a `raise ValueError`): the row is `failed`, and the result holds the error line.
  5. A result over 4 KiB is stored truncated, with `result_truncated` true.
  6. A second `execute` on the same workflow id with a new `update_id` fails the Update with `AlreadyExecuted`, and the script ran once.
  7. A script that spins for 2 s with no host call: `failed`, `TimeoutError` in the result, and the workflow completes.

- [ ] **Step 2: Implement `flow_models.py`.**

```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class FlowStep(BaseModel):
    seq: int
    fn: str
    call_id: str
    status: Literal["running", "succeeded", "failed"]
    outward: bool
    started_at: datetime
    ended_at: datetime | None = None
    history_length: int
    error: str | None = None


class FlowWaiting(BaseModel):
    call_id: str
    kind: Literal["approval", "answer"]
    fn: str
    prompt: str | None = None
    since: datetime


class ProjectionWrite(BaseModel):
    run_id: str
    workflow_run_id: str
    status: Literal["running", "waiting", "succeeded", "failed"] | None = None
    step: FlowStep | None = None
    waiting_add: FlowWaiting | None = None
    waiting_remove: str | None = None
    result: str | None = None
    reset: bool = False


class FlowRecord(BaseModel):
    run_id: str
    definition_id: str
    version: int
    name: str
    workflow_id: str
    workflow_run_id: str
    started_by: dict[str, object]
```

  When `waiting_on` is non-empty the store sets `status` to `waiting`, and back to `running` when it empties, unless the write names a terminal status.

- [ ] **Step 3: Implement `flow_tools.py`.** It has no `from __future__ import annotations`.

```python
"""ScadBuddy's flow host functions (spec 2026-10-01 §7.1), as harness tools.

Every wrapper records its step through the run's projection (Ruling 7) and derives its
ids from the run id, the Temporal run id and its harness call id (Ruling 4).
"""

from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner, Injected

    # Passed through like the harness it wraps: it holds no module state, and its
    # workflow calls (a task, a timer, an activity) go through `workflow.*`, which
    # resolves the running workflow per call, as the harness's own tools do.
    from scadbuddy.workflows.flow_entries import run_callback

# Not passed through: it keeps a per-run step counter, which the sandbox's reload gives
# each workflow run.
from scadbuddy.workflows.flow_steps import step  # the context manager, below

MIN_WAIT_S = 10
MAX_WAIT_S = 86_400


class HumanAnswer(BaseModel):
    """A person's answer on the Workflows page."""

    answer: str


@agent.callback_tool_defn(inherently_safe=True, timeout=timedelta(seconds=MAX_WAIT_S))
async def human_answer(question: str) -> HumanAnswer:
    """Wait for a person to answer `question` on the Workflows page."""
    ...


@agent.tool_defn(inherently_safe=True)
async def wait_for_human(
    question: str, runner: Injected[AgentWorkflowRunner], timeout_s: int = 3600
) -> HumanAnswer:
    """Ask a person `question` and wait up to `timeout_s` seconds (10 to 86400) for the
    answer. Raises TimeoutError if nobody answers in time."""
    if not MIN_WAIT_S <= timeout_s <= MAX_WAIT_S:
        raise ValueError(f"timeout_s must be {MIN_WAIT_S} to {MAX_WAIT_S} seconds")
    call_id = str(workflow.uuid4())
    async with step("wait_for_human", call_id, outward=False, waiting="answer", prompt=question):
        answer: HumanAnswer = await run_callback(
            runner.run_tool, human_answer, timeout_s, call_id=call_id, question=question
        )
        return answer


@agent.tool_defn(inherently_safe=True)
async def sleep(seconds: float) -> None:
    """Wait `seconds` seconds, durably (a Temporal timer)."""
    async with step("sleep", str(workflow.uuid4()), outward=False):
        await workflow.sleep(timedelta(seconds=seconds))


FLOW_TOOLS: list = [sleep, wait_for_human, human_answer]


def run_flow_tool():  # type: ignore[no-untyped-def]
    """The Code Mode tool over the host functions a script may call."""
    return agent.code_mode_tool(
        [t for t in FLOW_TOOLS if t is not human_answer], name="run_flow"
    )
```

  `human_answer` is registered with the plugin (`FLOW_TOOLS`) but not offered to the script.

  `step` lives in `workflows/flow_steps.py`, an async context manager. It keeps a per-workflow sequence counter in a `contextvars`-free module dict keyed by `workflow.info().run_id`, cleared by `ProjectWorkflow` at `execute` start. It does three things:
  - on enter, runs `flow_project` as a local activity with a `running` `FlowStep` (`history_length=workflow.info().get_current_history_length()`) and, when `waiting` is given, a `FlowWaiting`;
  - on exit, runs it again with the step's end (and `error=type(e).__name__`, never the message) and `waiting_remove`;
  - re-raises.

  The local activity's options are `start_to_close_timeout=8s` and `RetryPolicy(maximum_attempts=0)`, i.e. unlimited, so a projection write never fails the script.

- [ ] **Step 4: Implement `project.py`.**

```python
"""ProjectWorkflow: a flow run (spec §7.2, plan 6). A model-free harness agent; the
shape of the harness's agent_dag DagBuilderAgent.execute."""

from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from scadbuddy.workflows.flow_activities import FLOW_CLOSE, FLOW_PROJECT, FLOW_RECORD
    from scadbuddy.workflows.flow_models import FlowRecord, ProjectionWrite
    from scadbuddy.workflows.flow_steps import reset_steps
    from scadbuddy.workflows.flow_tools import run_flow_tool

PROJECT_WORKFLOW = "ProjectWorkflow"
RESULT_MAX = 4096
KIND = SearchAttributeKey.for_keyword("ScadbuddyKind")
SUBJECT = SearchAttributeKey.for_keyword("ScadbuddySubject")
STATUS = SearchAttributeKey.for_keyword("ScadbuddyStatus")
FLOW = SearchAttributeKey.for_keyword("ScadbuddyFlow")


class RunFlow(BaseModel):
    """A flow script to run once."""

    script: str


class FlowStart(BaseModel):
    """Who and what this run is; fixed for the execution."""

    run_id: str
    definition_id: str
    version: int
    name: str
    started_by: dict[str, object]
    search_attributes: bool = False


@agent.defn(name=PROJECT_WORKFLOW)
class ProjectWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig, data: FlowStart) -> None:
        self._start = data
        self._executed = False
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._flow = run_flow_tool()

    @agent.accepts(mid_turn=MidTurn.REJECT)
    async def execute(self, message: RunFlow) -> TextReply:
        """Run the flow's script, no model in the loop."""
        if self._executed:
            raise ApplicationError("this run has a script already", type="AlreadyExecuted",
                                   non_retryable=True)
        self._executed = True
        info = workflow.info()
        await workflow.execute_activity(
            FLOW_RECORD,
            FlowRecord(run_id=self._start.run_id, definition_id=self._start.definition_id,
                       version=self._start.version, name=self._start.name,
                       workflow_id=info.workflow_id, workflow_run_id=info.run_id,
                       started_by=self._start.started_by),
            start_to_close_timeout=timedelta(seconds=8),
        )
        self._upsert("running")
        reset_steps()
        output = await self._runner.run_tool(str(workflow.uuid4()), self._flow,
                                             script=message.script)
        status = "failed" if output.startswith("Script error") else "succeeded"
        await workflow.execute_local_activity(
            FLOW_PROJECT,
            ProjectionWrite(run_id=self._start.run_id, workflow_run_id=info.run_id,
                            status=status, result=output),
            start_to_close_timeout=timedelta(seconds=8),
        )
        self._upsert(status)
        await workflow.execute_activity(FLOW_CLOSE, info.workflow_id,
                                        start_to_close_timeout=timedelta(seconds=8))
        return TextReply(text=output[:RESULT_MAX])

    def _upsert(self, status: str) -> None:
        if not self._start.search_attributes:
            return
        workflow.upsert_search_attributes([
            SearchAttributePair(KIND, "flow"),
            SearchAttributePair(SUBJECT, self._start.definition_id),
            SearchAttributePair(STATUS, status),
            SearchAttributePair(FLOW, self._start.name),
        ])
```

  The store truncates `result` to `RESULT_MAX` and sets `result_truncated`. `TextReply.text` is truncated too, so the Update's result payload stays small.

  The `ScadbuddyFlow` Search Attribute is new. Add it to the clusters registration list in 6h and to `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES`'s doc. Check the `ScadbuddyStatus` values already used (`printing.py`, `operation.py`) and use the same words.

- [ ] **Step 5: Implement `flow_activities.py`.** It follows `operation_activities.py`'s class-with-`all()` shape:
  - `FLOW_RECORD = "flow_record"`, which calls `store.insert_run`;
  - `FLOW_PROJECT = "flow_project"`, which calls `store.project`;
  - `FLOW_CLOSE = "flow_close"`, which does `await activity.client().get_workflow_handle(workflow_id).signal("close")`. That is the harness's public `close` handler, sent as its own web app sends it.

- [ ] **Step 6: Run.** `pytest tests/flows/test_project_workflow.py -v`. Expected: PASS.
- [ ] **Step 7: Commit.** `git add backend/scadbuddy/workflows/project.py backend/scadbuddy/workflows/flow_tools.py backend/scadbuddy/workflows/flow_steps.py backend/scadbuddy/workflows/flow_activities.py backend/scadbuddy/workflows/flow_models.py backend/tests/flows/test_project_workflow.py && git commit -m "feat(flows): ProjectWorkflow with sleep and wait_for_human (#1057)"`

### Task B4: The `projects` worker, replay fixtures

**Files:**
- Modify: `backend/scadbuddy/core/settings.py`, `backend/scadbuddy/worker.py`, `backend/scadbuddy/main.py`, `backend/tests/api/conftest.py`
- Create: `backend/scadbuddy/workflows/projects_worker.py`, `backend/tests/flows/test_project_replay.py`, `backend/tests/fixtures/project_workflow_histories/*.json`
- Test: `backend/tests/test_worker.py` (extend)

**Interfaces:**
- Produces:
  - `Settings.temporal_task_queue_projects: str = "projects"`;
  - `Settings.temporal_projects_worker_inprocess: bool = False`;
  - `projects_worker(client, queue, activities, *, workflows=(ProjectWorkflow, OperationWorkflow)) -> Worker`;
  - `async run_projects_worker(settings, stop)`.

- [ ] **Step 1: Settings.** Add both fields, the queue to `_temporal_without_whitespace` and to the not-a-runtime-setting map. The queue's reason: "Paired with the Temporal address: the API starts flow runs on it, and the worker that serves it must name the same queue." The flag's reason: "A deployment choice: a one-process dev run serves flows itself." Run `pytest tests/test_settings_coverage.py`.

- [ ] **Step 2: The worker factory.**

```python
from scadbuddy.workflows.flow_entries import flow_entry_timeout


def projects_worker(
    client: Client, queue: str, activities: Sequence[Callable[..., Any]],
    *, workflows: Sequence[type] = (ProjectWorkflow, OperationWorkflow),
) -> Worker:
    """The `projects` queue (spec §4.3): flows, and the flow operations. Unversioned
    (Ruling 13). The client must carry flows_converter."""
    return Worker(
        client,
        task_queue=queue,
        workflows=list(workflows),
        # flow_entry_timeout: a parked entry's timer (Ruling 11). Always added here,
        # so a caller never passes it (a name registered twice fails worker start).
        activities=[*activities, flow_entry_timeout],
        plugins=harness_plugins(FLOW_TOOLS),
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "opentelemetry", "pydantic_monty"
            )
        ),
    )
```

  The `projects` operation kinds come in 6c (`OperationWorkflow` already exists and serves whatever kinds name the queue). In 6b it registers with no kinds.

- [ ] **Step 3: `--queue projects`.** In `worker.py`: `Queue = Literal["render", "bambuddy", "projects"]`, add the choice, and branch `_main` to `run_projects_worker`.
  - That builds `PgPayloadKeys` from the database URL and `load_kek(settings.secret_key_file)`, refusing to start without a KEK (exit 2 naming `SCADBUDDY_SECRET_KEY_FILE`), and the `FlowStore`.
  - It connects with `connect_flows` and serves `projects_worker` with the same `_health_server` (port 9090).
  - It uses an unversioned serve loop, the one `library_worker` uses in `main.py` (`_serve_until`); `_serve_versioned` is for versioned workers.
  - Health `/healthz` reports `task_queue: projects`.
  - Extend `tests/test_worker.py`'s `parse_queue` cases.

- [ ] **Step 4: In-process.** `main.py` runs it as `_run_projects_worker` beside `_run_print_worker` when `temporal_worker_inprocess` or `temporal_projects_worker_inprocess` is set, using the `FLOWS` component's client (B5). In `tests/api/conftest.py`'s settings override, add `temporal_task_queue_projects=f"{queue}-projects"` and terminate it after.

- [ ] **Step 5: Replay fixtures.** `test_project_replay.py` has two parts:
  - It records three histories against a dev server and writes them to `tests/fixtures/project_workflow_histories/` when `SCADBUDDY_RECORD_HISTORIES=1` (B3's cases 1 and 2, and a run `sleep`ing across a worker restart). Record them once and commit the JSON.
  - Every run, it replays every fixture with `Replayer(workflows=[ProjectWorkflow], plugins=harness_plugins(FLOW_TOOLS), data_converter=flows_converter(FakeKeys()))`. Expected: no `NondeterminismError`.

  Also add a kill test: stop the worker mid-`sleep(5)`, start a new one, and the run completes with each step recorded once.

- [ ] **Step 6: Run.** `pytest tests/test_worker.py tests/flows -v`. Expected: PASS.
- [ ] **Step 7: Commit.** `git add backend/scadbuddy/core/settings.py backend/scadbuddy/worker.py backend/scadbuddy/main.py backend/scadbuddy/workflows/projects_worker.py backend/tests/api/conftest.py backend/tests/test_worker.py backend/tests/flows/test_project_replay.py backend/tests/fixtures/project_workflow_histories && git commit -m "feat(flows): the projects worker and replay fixtures (#1057)"`

### Task B5: Routes and tools: register, list, start, get

**Files:**
- Create: `backend/scadbuddy/flows/component.py`, `backend/scadbuddy/api/flows.py`, `agent/src/tools/flows.ts`
- Modify: `agent/src/tools/index.ts`
- Test: `backend/tests/api/test_flows.py`, `agent/test/tools/flows.test.ts`

**Interfaces:**
- Consumes: `FlowStore`, `check_script`, `connect_flows`, `ProjectWorkflow` names.
- Produces:
  - Component `FLOWS` → `Flows{store: FlowStore, client: Client | None, queue: str, search_attributes: bool}`. `client` is `None` without a KEK, so flows are unavailable.
  - Routes:
    - `POST /api/v1/workflows` `{name, script}` → 201 `Definition`, or 422 `{problems: [ScriptProblem]}`;
    - `GET /api/v1/workflows` → `[Definition]` (without `script`; `GET /api/v1/workflows/{id}` has it);
    - `POST /api/v1/workflows/{id}/runs` (header `Idempotency-Key` required) → 202 `RunAnswer{id, status: "starting", repeated}`, or 200 for a repeat;
    - `GET /api/v1/workflow-runs?definition_id=&session=` → `[Run]`;
    - `GET /api/v1/workflow-runs/{id}` → `RunView` = `Run`, plus `pending: [{call_id, kind, fn, prompt?, since?}]` from `agent_status` when the execution is open, and `live: bool`.
  - Tools: `register_flow` (write), `list_flows` (read), `get_flow` (read), `start_flow_run` (write, through `command()`), `list_flow_runs` (read), `get_flow_run` (read).

- [ ] **Step 1: Failing API tests** (`tests/api/`, Temporal and Postgres, the in-process `projects` worker):
  1. Registering a bad script answers 422 with problems by line, and writes nothing.
  2. Register v1 and v2 of `swap`: `GET /workflows` lists `swap` at v2.
  3. Start without `Idempotency-Key`: 428.
  4. Start: 202 `{id, status: "starting"}`. Polling `GET /workflow-runs/{id}` goes `starting` (no row) → `succeeded`.
  5. Review Focus 1, three re-sends with the same key: immediately after the 202, while running (`sleep(3)`), and after `succeeded`. Each answers the same id (the first `starting`/202, the later ones 200 `repeated: true`), and the effects file shows the script ran once.
  6. A run waiting on `wait_for_human`: `GET` has `pending == [{kind: "answer", fn: "wait_for_human", prompt}]` and `live: true`.
  7. Without a KEK (settings with `secret_key_file=None`): start answers 503 `flows-unavailable`.
  8. A script over 64 KiB: 413.
  9. Start a definition whose script no longer type-checks against the current host functions: 422 (§7.2, "again before each run").
- [ ] **Step 2: Implement the start route.**

```python
@router.post("/workflows/{definition_id}/runs", status_code=202, response_model=RunAnswer,
             responses=temporal_problems(), summary="Start a run of a flow")
async def start_run(definition_id: str, response: Response, flows: FlowsDep,
                    key: IdempotencyKey) -> RunAnswer:
    if key is None:
        raise ApiError(428, "Send an Idempotency-Key.", type_=KEY_REQUIRED_PROBLEM)
    definition = await flows.store.get_definition(definition_id)
    if definition is None:
        raise ApiError(404, "No such flow.")
    run_id = str(uuid.uuid5(FLOW_NAMESPACE, f"{definition.id}:{definition.version}:{key}"))
    if (existing := await flows.store.get_run(run_id)) is not None:
        response.status_code = 200
        return RunAnswer(id=existing.id, status=existing.status, repeated=True)
    if flows.client is None:
        raise ApiError(503, "Flows need the secret key file.", type_=FLOWS_UNAVAILABLE)
    if problems := await check_script(definition.script):
        raise ApiError(422, "The flow no longer type-checks.", problems=problems)
    try:
        await AgentClient(flows.client, f"flow-{run_id}").start_and_submit_message(
            "execute", {"script": definition.script},
            workflow_name=PROJECT_WORKFLOW, task_queue=flows.queue,
            start_config=AgentConfig(),
            start_data=FlowStart(run_id=run_id, definition_id=definition.id,
                                 version=definition.version, name=definition.name,
                                 started_by=started_by(),
                                 search_attributes=flows.search_attributes),
            update_id=key,
        )
    except WorkflowUpdateFailedError as e:
        # AlreadyExecuted: the same run under another key cannot happen (the id is the
        # key's); anything else is the harness refusing the message.
        raise ApiError(409, "This run could not be started.") from e
    except RPCError as e:
        raise temporal_unavailable(e) from e
    return RunAnswer(id=run_id, status="starting", repeated=False)
```

  The call is bounded by `COMMAND_ANSWER_DEADLINE`: wrap it in `asyncio.wait_for(…, COMMAND_ANSWER_DEADLINE.total_seconds())`. A timeout answers 503 `command-still-accepting` with `Retry-After: 2` (§4.2), and the client re-sends the same key.

  `started_by()` maps `current_author()` per Ruling 6.

- [ ] **Step 3: Implement `GET /workflow-runs/{id}`.**
  - With no row: describe `flow-{id}`. Not found → 404. Open → `RunView(status="starting", …)` built from `FlowStart`; read it from the execution's input through `fetch_history`'s first event, decoded by the flows client.
  - With a row whose status is `running`/`waiting`: `AgentClient(client, row.workflow_id).get_status()` (timeout 2 s). Its `pending_approvals` and `pending_callbacks` become `pending`, joined with the row's `waiting_on` for `prompt` and `since`, and `live=True`. On a Query timeout or failure, `pending` comes from `waiting_on` and `live=False`.
  - `pending` never carries an approval's `tool_input`: only `fn`, plus the outward tool name for a `tool(...)` call (6d). That is §6.6's rule that an `approval` entry never carries the raw input.

- [ ] **Step 4: The agent tools.** In `agent/src/tools/flows.ts`, one `defineTool` per route, in the `prints.ts` style. `start_flow_run` uses `command(ctx, \`start flow ${definition_id}\`, (headers) => ctx.backend.POST('/api/v1/workflows/{definition_id}/runs', {params: {path: {definition_id}}, headers}))`. Spread them into `index.ts`. Run `cd agent && pnpm test -- coverage flows`.

- [ ] **Step 5: Run.** `cd backend && uv run --frozen pytest tests/api/test_flows.py -v`; `cd agent && pnpm lint && pnpm typecheck && pnpm test`. Expected: PASS.
- [ ] **Step 6: Commit.** `git add backend/scadbuddy/flows/component.py backend/scadbuddy/api/flows.py backend/tests/api/test_flows.py agent/src/tools/flows.ts agent/src/tools/index.ts agent/test/tools/flows.test.ts && git commit -m "feat(flows): register and start flows, read runs (#1057)"`

### Task B6: The orphan sweep

**Files:**
- Modify: `backend/scadbuddy/workflows/housekeeping.py`
- Test: `backend/tests/flows/test_flow_sweep.py`

**Interfaces:**
- Produces: the activity `housekeeping_sweep_flow_runs`, appended to `SWEEPS` (CLAUDE.md: "A new periodic pass is an activity in its SWEEPS, never a loop in the API").

- [ ] **Step 1: Failing test.** Start a run that `sleep`s for 60 s, terminate it, and run the sweep. Its row is `terminated`, and a `workflow-runs` event is published. A running run's row is untouched, and it is not described again within 10 minutes (a `last_checked_at`-style guard kept in memory per sweep run is enough, since the sweep runs every interval).
- [ ] **Step 2: Implement.**
  - For rows `running`/`waiting` with `updated_at < now() - 10 min`, describe `workflow_id` with the flows client.
  - A closed execution with no terminal row → `project(reset=False, status="terminated")`, a new store method `mark_terminated(id)`, guarded.
  - A different current run id than the row's (a Reset whose projection has not caught up) → only `workflow_run_id` is updated.
  - Not found → `terminated`.
  - The sweep needs the flows client. The `library` worker that runs housekeeping gets it from the `FLOWS` component. Without one (no KEK), the sweep is skipped and logs at debug.
- [ ] **Step 3: Run.** `pytest tests/flows/test_flow_sweep.py -v`. Expected: PASS.
- [ ] **Step 4: Commit.** `git add backend/scadbuddy/workflows/housekeeping.py backend/scadbuddy/flows/store.py backend/tests/flows/test_flow_sweep.py && git commit -m "feat(flows): sweep runs whose execution closed without finishing (#1057)"`

**6b PR:** `feat(flows): ProjectWorkflow, the projects worker, and flow runs (#1057, phase 6b)`. Update `CLAUDE.md` "Layout" with `workflows/project.py`, `flow_tools.py`, `flows/`, and the `projects` queue, in this PR.

---

## 6c: decisions and the outward host functions

### Task C1: `workflow_run_decisions`, and the `projects` operation kinds

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_workflow_run_decisions.sql`, `backend/scadbuddy/flows/operations.py`
- Modify: `backend/scadbuddy/operations/kinds.py` (accept `queue="projects"`), `backend/scadbuddy/flows/store.py`, `backend/scadbuddy/workflows/projects_worker.py`
- Test: `backend/tests/flows/test_flow_operations.py`

**Interfaces:**
- Produces:
  - `FlowStore.record_decision(d: Decision) -> bool` (False when the request id exists), `delete_decision(request_id) -> None`, `get_decision(request_id) -> Decision | None`.
  - `Decision{request_id, run_id, workflow_run_id, call_id, kind: "approval" | "answer", outcome: "approved" | "denied" | "answered", response: dict, responder: str, created_at}`.
  - Kinds `flow_decide` and `flow_answer` on queue `projects`, both `done`.

```sql
-- <ts>_workflow_run_decisions.sql: a person's answer to a flow run's parked call,
-- recorded before the harness is told (plan 6, decisions A and B). request_id is
-- flow:<run id>:<workflow run id>:<call id> (spec §6.6).
CREATE TABLE workflow_run_decisions (
    request_id      text PRIMARY KEY CHECK (request_id ~ '^flow:'),
    run_id          text NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
    workflow_run_id text NOT NULL,
    call_id         text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('approval', 'answer')),
    outcome         text NOT NULL CHECK (outcome IN ('approved', 'denied', 'answered')),
    response        jsonb NOT NULL DEFAULT '{}',
    responder       text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 1: Failing tests** for the kinds' runs, against a real `ProjectWorkflow` parked on `wait_for_human`, and on a gated test tool appended to `FLOW_TOOLS` only in the test module:
  1. `flow_answer` records, then `provide_callback_result`; the run continues; the decision row exists with `outcome: answered`.
  2. `flow_answer` for the same call again: the check refuses 409 `already-resolved` (the row exists), and no Update is sent.
  3. `flow_answer` for a call id the harness does not know (a stale id after a Reset, simulated by an unknown id): the Update raises `CallbackResultError(UnknownCallback)`. The run deletes the decision row it wrote and fails 409 `stale-entry`.
  4. `MalformedCallbackResult` → the row is deleted, 422, and the entry is still pending (it can be answered again).
  5. `flow_decide` approve → `approve_tool(call_id, approved=True, update_id=request_id)`; deny with a reason → the script sees `ToolApprovalDenied`.
  6. `ToolApprovalAlreadyResolved` → row deleted, 409.
- [ ] **Step 2: Implement.** `flows/operations.py` exports `OPERATION_KINDS` (a `KindsBuild`, as `library/operations.py` does) with both kinds. Each kind:
  - **check:** the run exists and is open; the call id is in the row's `waiting_on` with the right `kind`; no decision row for `request_id`;
  - **effect** (an activity on `projects`):
    - `record_decision`; when it returns False, 409.
    - Then the harness Update through `AgentClient(activity.client(), run.workflow_id)`, with `update_id=request_id` so a retried activity resends the same Update.
    - On a harness refusal, `delete_decision` and the mapped problem.
    - A transport error is retried by the activity's policy (3 attempts), the row kept, because the same `update_id` makes the resend safe.

  The `request_id` uses the run's current `workflow_run_id` from the row, and the check compares it with `agent_status`'s run, so a decision for a pre-Reset card is `stale-entry` (§6.6). Register the kinds on the `projects` worker (`operations/kinds.py` discovers them; the worker filters by queue).
- [ ] **Step 3: Run, commit.** `pytest tests/flows/test_flow_operations.py -v`, then `git add backend/scadbuddy/migrations/*_workflow_run_decisions.sql backend/scadbuddy/flows/operations.py backend/scadbuddy/flows/store.py backend/scadbuddy/operations/kinds.py backend/scadbuddy/workflows/projects_worker.py backend/tests/flows/test_flow_operations.py && git commit -m "feat(flows): record a decision, then tell the harness (#1057)"`

### Task C2: `POST …/answer` and `POST …/decide`

**Files:**
- Modify: `backend/scadbuddy/api/flows.py`, `agent/src/tools/coverage.ts`
- Test: `backend/tests/api/test_flows.py` (extend)

**Interfaces:**
- Produces:
  - `POST /api/v1/workflow-runs/{id}/answer` `{call_id, answer: str (≤ 16 KiB)}`;
  - `POST /api/v1/workflow-runs/{id}/decide` `{call_id, approved: bool, reason?: str (≤ 1 KiB)}`;
  - both through `run_operation` (`Idempotency-Key`), answering the operation's result `{run: RunView}`.

- [ ] **Step 1: Failing tests.**
  - The happy paths.
  - Review Focus 4: a second send → 409; a stale call id → 409 `stale-entry`.
  - A request with `X-ScadBuddy-Agent-Author` → 403 `flow-entry-browser-only`, and no operation started.
  - A missing `Idempotency-Key` → 428.
- [ ] **Step 2: Implement.** Both routes check `current_author() is None` before `run_operation`.
- [ ] **Step 3: Coverage.** Add both routes to `NOT_A_TOOL` in `coverage.ts` with the reason "A flow's entries are answered by the browser user on the Workflows page only (spec §6.6, plan 6 Ruling 6)."
- [ ] **Step 4: Run, commit.** `git add backend/scadbuddy/api/flows.py backend/tests/api/test_flows.py agent/src/tools/coverage.ts && git commit -m "feat(flows): answer and decide a run's parked calls (#1057)"`

### Task C3: `render`, `print`, `arrange`

**Files:**
- Modify: `backend/scadbuddy/workflows/flow_tools.py`, `backend/scadbuddy/workflows/flow_activities.py`, `backend/scadbuddy/workflows/projects_worker.py` (deps: an httpx client on `api_internal_url`)
- Test: `backend/tests/flows/test_outward_host_functions.py`

**Interfaces:**
- Produces host functions:
  - `render(slug: str, params: dict[str, Any] | None = None) -> RenderResult{job_id, status, error?}`;
  - `print(source: PrintSource, choices: dict[str, Any] | None = None) -> PrintOutcome{run_id, status, may_have_queued, error?}`, where `PrintSource = {output_id?: str, file_id?: int}`;
  - `arrange(objects: list[dict[str, Any]], goal: str) -> ArrangeResult{output_id?, status, error?}`.

  `render` is `inherently_safe`. `print` and `arrange` are gated. Each is an inline `tool_defn` that runs `flow_<fn>_start` and then `flow_<fn>_wait`.

- [ ] **Step 1: Failing tests,** against the API test app (the in-process worker; `api_internal_url` pointed at the TestClient server through the `WorkerThread` support):
  1. `render('basic-box', {'width': 20})` returns `succeeded` and the job id the route created; a second identical call in the same script joins it (`render_key`).
  2. `print({'output_id': o}, {...})` parks for approval; approve → a `PrintRun` is created with `request_id = sha256("flow:<run>:<wfrun>:<call>")[:32]`, and the host call returns its outcome.
  3. A print refusal (422 from the route) reaches the script as an exception whose text names the problem's `detail`; the step is `failed`.
  4. `print` worker killed during `flow_print_wait`: the wait is retried and follows the same run (no second `PrintRun`).
  5. `arrange` parks for approval and returns the output id.
- [ ] **Step 2: Implement.** The activities share one `httpx.AsyncClient(base_url=settings.api_internal_url)`.
  - `start` POSTs the route (`POST /api/v1/models/{slug}/render`; `POST /api/v1/print/outputs/{output_id}/run` or `/print/library/{file_id}/run` with `request_id`; `POST /api/v1/outputs/arrange`) with `Idempotency-Key` = the Ruling 4 id. It follows `202`/`503 command-still-accepting` exactly as the agent's `command()` does: re-send while still accepting, up to the activity's 60 s `start_to_close`.
  - `wait` polls the job or run every 2 s with heartbeats (`heartbeat_timeout=30s`, `start_to_close_timeout` 24 h for print, 2 h for render and arrange) until it settles.
  - Non-retryable failures: a 4xx problem (`ApplicationError(type="RouteRefused", non_retryable=True)` with the problem's `detail` only).
  - The projects worker refuses to start these activities without `api_internal_url` (the print worker's `ApiUrlMissingError`).
- [ ] **Step 3: Run, commit.** `git add backend/scadbuddy/workflows/flow_tools.py backend/scadbuddy/workflows/flow_activities.py backend/scadbuddy/workflows/projects_worker.py backend/tests/flows/test_outward_host_functions.py && git commit -m "feat(flows): render, print and arrange host functions (#1057)"`

  Re-record the replay fixtures (`SCADBUDDY_RECORD_HISTORIES=1`), adding one with an approved `print`, and commit them.

**6c PR:** `feat(flows): decisions and answers, and the render, print and arrange host functions (#1057, phase 6c)`.

---

## 6d: `tool(name, args)` (waits on #1056)

Start only after #1056 is closed. Re-read `agent/src/temporal/toolActivities.ts` then, since 5c PR 3 may have changed it.

### Task D1: The agent's `flow-<uuid>` branch

**Files:**
- Create: `agent/src/temporal/flowRuns.ts`
- Modify: `agent/src/temporal/toolActivities.ts`
- Test: `agent/test/temporal.worker.test.ts` (extend)

**Interfaces:**
- Produces:
  - `flowOf(workflowId?: string): string | undefined`;
  - `FlowRuns` (Pg over the backend's `workflow_runs` and `workflow_run_decisions`, read-only): `startedBy(runId): Promise<Owner | undefined>`, `approved(requestId): Promise<boolean>`.
  - In `runAsActivity`: a `flow-<uuid>` workflow runs as the run's `started_by` principal. A gated tool needs `workflow_run_decisions.outcome = 'approved'` for `flow:<run>:<wfrun>:<call id>` (the `tool-` prefix stripped from the activity id). `browser_*` tools and `DURABLE_ONLY_NAMES` are refused for flows (no tab, no session). The audit row has `sessionId` null and `requestId` set.

- [ ] **Step 1: Failing tests** (`@temporalio/testing`, the Postgres suite):
  - a flow's read tool runs as its starter;
  - a gated tool without a decision row fails `NotApproved` and writes the `refused` audit row;
  - with the row it runs;
  - a `flow-<uuid>` with no `workflow_runs` row fails `UnknownFlow`;
  - a `browser_click` from a flow fails `NotForFlows`.

  The test creates the two backend tables from their migration files.
- [ ] **Step 2: Implement; run `pnpm test`; commit.** `git add agent/src/temporal/flowRuns.ts agent/src/temporal/toolActivities.ts agent/test/temporal.worker.test.ts && git commit -m "feat(agent): agent-tools serves flow runs as their starter (#1057)"`

### Task D2: The `tool` host function

**Files:**
- Modify: `backend/scadbuddy/workflows/flow_tools.py`; `backend/scadbuddy/flows/manifest.py` (new: loads `dist/tools.json`, path in `SCADBUDDY_FLOW_TOOLS_JSON`, as 5c's `SCADBUDDY_DURABLE_TOOLS_JSON`)
- Test: `backend/tests/flows/test_tool_host_function.py`

**Interfaces:**
- Produces: `tool(name: str, args: dict[str, Any] | None = None) -> str`, an inline `tool_defn(inherently_safe=True)`.
  - It looks `name` up in the manifest; an unknown name raises `ValueError`.
  - For tier `outward` it runs the gated inline tool `outward_tool(name, args, call_id)` through `runner.run_tool(call_id, outward_tool, …)`, so the harness parks it (fact 5 of Task A3).
  - Other tiers call `workflow.execute_activity(name, args or {}, activity_id=f"tool-{call_id}", task_queue=settings agent-tools queue, start_to_close_timeout=120s, retry_policy=RetryPolicy(maximum_attempts=1 if outward else 3, non_retryable_error_types=["ToolError", "NotApproved"]))`.
  - The result is the activity's content blocks joined as text (images become a note, 5c Ruling 4).
- [ ] **Step 1: Failing tests** with a stand-in TypeScript-named activity registered in Python on a test `agent-tools` queue (`@activity.defn(name="get_model")`):
  - a read call runs with the `tool-<call id>` activity id;
  - an outward one parks, and approve via `flow_decide` runs it;
  - an unknown name is a script exception.
- [ ] **Step 2: Implement, run, re-record fixtures, commit.** `git add backend/scadbuddy/workflows/flow_tools.py backend/scadbuddy/flows/manifest.py backend/tests/flows/test_tool_host_function.py backend/tests/fixtures/project_workflow_histories && git commit -m "feat(flows): the tool host function over agent-tools (#1057)"`

**6d PR:** `feat(flows): tool(name, args) over agent-tools (#1057, phase 6d)`.

---

## 6e: Reset preview and Reset

### Task E1: What a Reset would run again

**Files:**
- Create: `backend/scadbuddy/flows/history.py`
- Test: `backend/tests/flows/test_reset_preview.py`

**Interfaces:**
- Produces: `async outward_since(client, workflow_id, event_id) -> ResetPreview{event_id, as_of_event_id, calls: [{fn, call_id, scheduled_event_id}]}`.
  - It reads the current run's `fetch_history()` through the flows client.
  - Outward calls are an `ActivityTaskScheduled` whose type is `flow_print_start` or `flow_arrange_start`, or an agent-tools activity whose name has tier `outward` (manifest), or (6g) an `agent`/`ask_session` turn that made an outward call. Each must have `event_id < scheduled_event_id`.
  - It also returns `valid: bool`: `event_id` names a `WorkflowTaskCompleted` before the last event. Reset accepts only those (`workflow_task_finish_event_id`).
- [ ] **Step 1: Failing tests** over recorded histories (fixtures with two prints, a render and a `wait_for_human`):
  - the list is exactly the calls after the point;
  - `as_of_event_id` is the last event's id;
  - a non-WFT event is `valid: false`.
- [ ] **Step 2: Implement, run, commit.** `git add backend/scadbuddy/flows/history.py backend/tests/flows/test_reset_preview.py && git commit -m "feat(flows): list the outward calls a Reset would run again (#1057)"`

### Task E2: The routes, the `flow_reset` kind, tools

**Files:**
- Modify: `backend/scadbuddy/api/flows.py`, `backend/scadbuddy/flows/operations.py`, `backend/scadbuddy/flows/store.py`, `agent/src/tools/flows.ts`
- Test: `backend/tests/api/test_flow_reset.py`

**Interfaces:**
- Produces:
  - `GET /api/v1/workflow-runs/{id}/reset-preview?event_id=` → `ResetPreview` (a read, §4.1);
  - `POST /api/v1/workflow-runs/{id}/reset {event_id, as_of_event_id}` (`Idempotency-Key`) → the operation's result `{run: RunView}`, or 409 `{preview: ResetPreview}` when outward calls were added after `as_of_event_id`;
  - kind `flow_reset` (queue `projects`, `done`);
  - tools `preview_flow_reset` (read) and `reset_flow_run` (outward; its call is gated and audited by the agent like every tool).
- [ ] **Step 1: Failing tests:**
  1. Reset a run whose `print` failed (the route refused it) to before that step. The script runs again from there and the earlier `render` is not run again (one render job).
  2. A preview, then a new outward call lands, then the reset with the old `as_of_event_id` → 409 with the new list. A reset with the new one succeeds.
  3. After a Reset, the row's `workflow_run_id` is the new run, steps whose `history_length > event_id` are gone, `waiting_on` is emptied and re-filled by the re-parked call, and the old call id's `answer` is 409 `stale-entry` (§8: "a respond carrying the pre-Reset id is refused as stale").
  4. A reset to before `execute` was accepted re-runs the whole script, and the row is not re-inserted (its `flow_record` sees the id and updates the run id).
- [ ] **Step 2: Implement.**
  - The kind's check recomputes the preview and compares it with the one at `as_of_event_id`, by the call ids after `as_of_event_id`.
  - Its effect calls `client.workflow_service.reset_workflow_execution(ResetWorkflowExecutionRequest(namespace=…, workflow_execution=WorkflowExecution(workflow_id=…), reason=f"flow reset by {responder}", workflow_task_finish_event_id=event_id, request_id=operation request id))`. Then it writes `ProjectionWrite(reset=True, workflow_run_id=new_run_id, status="running")`, with `steps` filtered by `history_length <= event_id` and `waiting_on=[]`.
  - `FlowStore.insert_run`'s `ON CONFLICT (id)` becomes `DO UPDATE SET workflow_run_id = EXCLUDED.workflow_run_id`, guarded `WHERE workflow_runs.workflow_run_id <> EXCLUDED.workflow_run_id`, so the re-applied `execute` after a full Reset moves the row to the new run. An event is published only when it changed.
- [ ] **Step 3: Run, commit.** `git add backend/scadbuddy/api/flows.py backend/scadbuddy/flows/operations.py backend/scadbuddy/flows/store.py backend/tests/api/test_flow_reset.py agent/src/tools/flows.ts && git commit -m "feat(flows): Reset a run, with a preview pinned to its history (#1057)"`

**6e PR:** `feat(flows): reset preview and reset (#1057, phase 6e)`.

---

## 6f: the Workflows page

### Task F1: List and run pages

**Files:**
- Create: `frontend/src/api/flows.ts`, `frontend/src/pages/WorkflowsPage.tsx`, `frontend/src/pages/WorkflowRunPage.tsx`, `frontend/src/mocks/features/flows.ts`, tests beside each page (`*.test.tsx`), `frontend/e2e/workflows.spec.ts`
- Modify: `frontend/src/App.tsx` (`workflows`, `workflows/runs/:id`), `frontend/src/components/AppShell.tsx` (`NAV` gets `{to: '/workflows', label: 'Workflows'}`)

**Interfaces:**
- Consumes: the 6b/6c routes and the `workflow-runs` event topic (`useSubscription('workflow-runs', …)`; poll every 5 s while the socket is down, as `usePrintProgress` does).

- [ ] **Step 1: Failing vitest cases** (msw):
  - the list shows each run's flow name and version, status, and what it waits on ("Waiting for your answer: Swap to pink?", "Waiting for approval: print");
  - Answer submits `POST …/answer` through `command()` with an `Idempotency-Key`;
  - Approve and Deny (with an optional reason) submit `…/decide`;
  - a 409 `stale-entry` shows "This request is out of date. The run has moved on." and refreshes;
  - `live: false` shows "Live status unavailable; showing the last known state";
  - a `starting` run shows "Starting…".
- [ ] **Step 2: Implement.** The run page lists steps (fn, status, started, duration) and links to the Temporal UI (`settings.temporal_ui_url`, `/namespaces/<ns>/workflows/flow-<id>`) when set, as Settings → Administration does. There is no Reset UI: Reset is the route and its tool (§7.4). The page shows the preview read only when 6e has landed.
- [ ] **Step 3: e2e.** `e2e/workflows.spec.ts` (msw-mocked): answer a waiting run and see it move to running.
- [ ] **Step 4: Run, commit.** `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test e2e/workflows.spec.ts`, then `git add frontend/src/api/flows.ts frontend/src/pages/WorkflowsPage.tsx frontend/src/pages/WorkflowRunPage.tsx frontend/src/pages/*Workflow*.test.tsx frontend/src/mocks/features/flows.ts frontend/src/App.tsx frontend/src/components/AppShell.tsx frontend/e2e/workflows.spec.ts && git commit -m "feat(ui): the Workflows page (#1057)"`

### Task F2: The panel's link to a session's runs

- [ ] The assistant panel shows "Flow runs started here (N)" linking to `/workflows?session=<id>` when `GET /api/v1/workflow-runs?session=<id>` is non-empty (§7.3). Read the panel's session header component before editing it, and add one vitest case. Commit with the explicit paths you touched: `feat(ui): link a session to the flow runs it started (#1057)`.

**6f PR:** `feat(ui): the Workflows page (#1057, phase 6f)`.

---

## 6g: `agent(...)` and `ask_session(...)` (waits on #1056)

Start only after #1056 is closed. Before writing code, read the merged 5c PR 3 and 5d: the agent service's route that creates a durable session (with `mode`), the route that sends a message, and how a turn's end is read. The tasks below name those as `CREATE_SESSION`, `SEND_MESSAGE` and `TURN_RESULT`. Replace each with the exact route from the merged code, and stop and report if any of them is missing.

### Task G1: `agent(prompt, skills=None, result_schema=None)`

- Host function, gated only when the session's own tools are (`inherently_safe=True`): the durable session's own outward calls park in that session and appear in the assistant's panel (§7.2).
- Activities `flow_agent_start` and `flow_agent_wait` on `projects`, over the agent service's internal URL (`SCADBUDDY_AGENT_INTERNAL_URL`, new, beside `api_internal_url`):
  - **start:** `CREATE_SESSION` with `mode: durable`, owner = the run's `started_by`, idempotent on the Ruling 4 id. Then `SEND_MESSAGE` with the prompt, `result_schema` appended as the instruction to answer in that JSON.
  - **wait:** heartbeats until `TURN_RESULT` settles, then validates the result against `result_schema` when given.
- The step's entry carries `session_id`, so the page links to the session.
- Tests against `agent/test/support/fakeAnthropic.ts` through the agent's e2e harness (§8: "`agent(...)` and `ask_session` against the fake endpoint").

### Task G2: `ask_session(session_id, message)`

- The same `SEND_MESSAGE`/`TURN_RESULT` pair against an existing session. It refuses a classic session (`mode != durable`, §7.3) and a session the run's starter does not own.
- `reset_preview` counts an `agent`/`ask_session` step as outward when its session's `ai_audit` rows for that turn include an outward `tool_call` (§7.4). That is read through a new read route on the agent service, named in the PR.

**6g PR:** `feat(flows): agent and ask_session host functions (#1057, phase 6g)`.

---

## 6h: deploy

### Task H1: The `scadbuddy-projects` Deployment

- `deploy.reusable.yml` pins the image for a new `MANIFEST_PROJECTS: applications/scadbuddy/scadbuddy-projects.yaml`, beside `MANIFEST_PRINT`. Run `bash .github/scripts/deploy-pin.test.sh`.
- The clusters change (eh-homelab/clusters, its own PR, linked from this one):
  - A Deployment `scadbuddy-projects`, 1 replica, running `python -m scadbuddy.worker --queue projects`. It has the database URL, the KEK file (read-only secret mount), `SCADBUDDY_TEMPORAL_*`, `SCADBUDDY_API_INTERNAL_URL` and, after 6g, `SCADBUDDY_AGENT_INTERNAL_URL`. It has no data volume and no Bambuddy key (§4.3, "nothing outward").
  - Health on 9090.
  - Add the pod's label to the Temporal frontend's ingress `CiliumNetworkPolicy` from 5e.
  - Register `ScadbuddyFlow` (Keyword) with the other Search Attributes.
- Memory: `pydantic-monty`'s pool is process-wide. Set the request at 256 Mi and the limit at 1 Gi, and record the measured steady state from a 50-step script in the PR.
- The `test` stage already carries the extra (A1). No separate image: the backend image runs every worker.

**6h PR:** `ci(deploy): the scadbuddy-projects worker (#1057, phase 6h)`, with `Fixes #1057` once 6g has merged (or `Part of #1057` if 6g is still waiting).

---

## Spec coverage (self-check)

| Spec | Where |
|---|---|
| §7.1 host functions | `sleep`, `wait_for_human` B3; `render`, `print`, `arrange` C3; `tool` D2; `agent`, `ask_session` G1–G2 |
| §7.2 harness agent, `execute`, `projects`, approvals, type check, verify around `ProjectWorkflow` | A3, B3, B4, C1–C2 (decisions A–B), B2 |
| §7.3 tables, update-with-start, Search Attributes, routes and tools, UI, panel link | B1, B5 (decision C), B3 `_upsert`, B5/C2/E2, F1, F2 |
| §7.4 Reset, preview pinned to `as_of_event_id`, in-flight children | E1, E2, Ruling 4, Ruling 5 |
| §6.5 flow payloads sealed | A2, Ruling 10 |
| §6.6 flow entries off the assistant badge, browser-only, request id `flow:…`, stale after Reset | Ruling 6, C1–C2, E2 test 3 (no `ai_pending_input` row is ever written) |
| §8 Flows: type-check refusal, `wait_for_human` answered, outward tool waiting, `agent`/`ask_session` on the fake endpoint, Reset to before a failed step | B2, B3, C3/D2, G1–G2, E2 test 1 |
| §8 handler set test against `ProjectWorkflow` | A3 case 12 |
| §10 phase 6 | all |

## Open questions for the user

1. **`pydantic-monty` wheels.** The image's platform must have a wheel. A1 Step 4 stops if it does not.
2. **The backend writes `ai_payload_keys`** (Ruling 10), an agent-owned table, as `agent-durable` already does. The alternative is an agent route that seals and opens. That would put a network hop in every flow payload, so it is not recommended.
3. **`render`, `print` and `arrange` call routes instead of starting children** (Ruling 5). The spec says "child", but a child cannot join an existing render or answer through `PrintRun`'s `accepted` Update. Confirm the activity-over-route design. It keeps every check and record the routes make today.
4. **The agent reads two backend tables** (`workflow_runs.started_by`, `workflow_run_decisions`) in 6d to run a flow's tools as its starter and to prove an approval. The proof has to come from somewhere a forged workflow cannot write.
5. **Approvals never time out** (Ruling 12), because the harness has no approval timer. A §6.6-style expiry would need ScadBuddy's own timer racing the gate, which wraps the harness. Confirm that no timer is acceptable.
6. **`forgetSubject` for a flow run** deletes its key and workflow, but not its `workflow_runs` row (`agent/src/sessions/forget.ts` deletes session rows only). Once #1056 closes, either forget also deletes the flow's rows, or a flow delete route is added. Neither is in phase 6's scope as written.
