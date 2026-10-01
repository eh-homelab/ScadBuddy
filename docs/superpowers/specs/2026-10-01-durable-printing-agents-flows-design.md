# Durable printing, durable agent sessions, and agent-written flows on Temporal

Design written 2026-10-01. It answers three requests made after a library print timed
out at the proxy and queued anyway. *Printing should run on Temporal.* *An agent session
should be durable, on Temporal's own framework, and keep human-in-the-loop.* *An agent
should be able to register a workflow with ScadBuddy.* That workflow runs for weeks
(generating, template changes and plate-by-plate printing for a whole dollhouse), and
its steps are answered by ScadBuddy's built-in workflows, by an agent session that
exists, or by a new one.

Ground rule, decided in the brainstorm: **follow the Temporal SDK and Temporal's agent
frameworks as they are documented.** Anything in this spec that departs from them is
listed as a deviation, and the user decides. §8 lists the open ones (none).

Base design: `2026-09-22-scadbuddy-design.md`. Renders on Temporal, the blob store and
the trust model (§9): `2026-09-27-template-pipelines-design.md` (the "template spec"
below). Print flow: `2026-09-24-print-flow-design.md`,
`2026-09-27-spool-first-print-design.md`, `2026-09-28-print-library-file-design.md`.
AI agent: `2026-09-27-ai-integration-design.md`. Issues: #742 (the library run is
synchronous), #470 / #567 (the output run's 202), #305 / #912 (print history).

## 1. Why

On 2026-10-01 at 14:49 UTC, Envoy cut `POST /api/v1/print/library/68/run` off at its
15 s `response_timeout` and answered 504. The backend carried on regardless. Bambuddy's
log shows one `POST /library/files/68/slice` and one `POST /queue/`, and its pending queue
went from 8 to 9. The dialog said "may still have been queued", which was true.

- `POST /print/library/{id}/run` (`api/library_print.py:149`) still uploads, slices
  and queues inside one request. #567 moved only an output's run to *202 and follow*,
  and the comment at `frontend/src/api/client.ts:986` says so.
- An output's run is an `asyncio` task in the API process (`bambuddy/runs.py:378`
  `PrintRuns.start`). It dies with the pod. Run `3c241a88` (07:29 UTC the same day)
  is still `running` with `enqueue_attempted = false`, and its heartbeat never moved.
  Only the `LOST` expiry would ever end it.
- Renders have run on Temporal since #424. Printing, the step with a physical effect,
  is the part that is not durable.

The agent side has the same shape. A classic session (`agent/src/sessions/manager.ts`)
runs `query()` inside the agent process. A turn that dies with the process is lost, and
nothing can wait weeks for a printer.

## 2. Goals and non-goals

Goals

- Every print, of an output or of a library file, is a Temporal workflow. A pod restart
  never loses one, and no request waits on a slice.
- A durable agent session mode, built on `temporalio-claude-agent-sdk`, chosen per
  session. Every outward tool call still waits for a human.
- Flows: Python scripts an agent writes and registers. They run durably for as long as
  they need to, and each step is a ScadBuddy workflow, a ScadBuddy tool, a durable agent
  session, or a human.
- ScadBuddy stays the system of record. Our Postgres rows are written by the workflows,
  and Temporal's Visibility and Archival sit beside them.

Non-goals

- Changing renders. `RenderService` keeps its insert-then-start and reconciler for now
  (§4.1 says why that should change, as a follow-up).
- Sandboxing beyond what the frameworks give. A flow's script runs in Code Mode's
  sandbox, and a template's pipeline is governed by the template spec §9.
- Classic sessions as flow targets. Only a durable session can be sent a step (§6.3).
- A TypeScript agent runtime on Temporal. The integration is Python only (§3.2).

## 3. External facts this rests on (verified 2026-10-01)

### 3.1 Temporal's agent guidance

- [AI reference architecture](https://go.temporal.io/platform-hub/ai-engineering/ai-reference-architecture):
  workflows orchestrate and activities execute. Every LLM call and every tool call is an
  activity. A session is a long-lived workflow that takes a turn per Update, and uses
  `continue_as_new` past a turn threshold.
- [Human-in-the-loop cookbook](https://docs.temporal.io/ai-cookbook/human-in-the-loop-python):
  a risky action waits on a Signal or Update. It can wait for days and uses no compute
  while it does.
- Polling (Temporal's activity guidance): frequent polling (seconds) goes inside the
  activity, with heartbeats. Infrequent polling uses the activity's retry policy.
- Early return: update-with-start starts a workflow and waits on one Update. The caller
  gets an answer as soon as the workflow's first steps decide it, and the workflow
  carries on.

### 3.2 The Claude Agent SDK on Temporal

- [temporal-agent-harness](https://github.com/temporal-community/temporal-agent-harness)
  (Temporal Technologies, Python, 0.5.0, experimental) is Temporal's agent framework. It
  supports Pydantic AI, OpenAI Agents and Google GenAI. Temporal declined the Claude
  Agent SDK there
  ([harness#131](https://github.com/temporal-community/temporal-agent-harness/issues/131)):
  the SDK runs its loop in a `claude` subprocess, so the harness cannot see each tool call.
  Temporal pointed instead to
  [temporalio/ai-integrations](https://github.com/temporalio/ai-integrations).
- [ai-integrations#33](https://github.com/temporalio/ai-integrations/pull/33) adds
  `temporalio-claude-agent-sdk` (`temporalio.claude_agent_sdk`). It is a draft, it is not
  on PyPI, and it was updated 2026-10-01. From its README and `_workflow.py`:
  - `DurableClaudeAgent` runs in a workflow. Each model *segment* is an activity
    that runs Claude Code until Claude calls a durable tool or finishes. A
    `PreToolUse` hook defers durable tools, and the workflow runs the call as its own
    activity with ID `tool-<tool_use_id>`.
  - `activity_as_tool(fn, name=, description=, input_schema=, needs_approval=,
    task_queue=, ...)`. `fn` must be an `@activity.defn` callable (`TypeError`
    otherwise). The call is `workflow.execute_activity(fn, ..., task_queue=...)`, so
    the activity is resolved by its name on whatever worker polls that queue.
  - `needs_approval=True` makes the call wait in the workflow for a `decide()`, sent
    through an Update with a validator (or a Signal). `pending_approvals()` serves a
    Query.
  - The conversation lives in a `SessionStore` that every worker reaches. The SDK has
    a Postgres example.
  - `auto_continue_as_new` continues at a safe point between tool calls. For a chat,
    `agent.continue_as_new()` runs between messages.
  - `live_output=True` publishes events through Workflow Streams, and `follow_agent`
    reads them. Every subscriber poll is an Update, so the README recommends one
    subscriber in the backend that fans the events out.
  - It requires Claude Code ≥ 2.1.273. ScadBuddy pins 2.1.283. It needs an API key,
    Bedrock/Vertex/Foundry, or `CLAUDE_CODE_OAUTH_TOKEN`; an app login cannot
    refresh on resume.
  - Subagents run in the foreground and cannot call durable tools.
- Code Mode (harness README, "Code Mode"; `examples/agent_dag`). `code_mode_tool(tools)`
  gives a model one tool that runs a Python script over host functions. The script is
  type-checked against their signatures before it runs (`code_mode_type_check`) and runs
  in `pydantic-monty`. Each host call is dispatched as its own durable activity or child
  workflow and keeps that tool's approval policy (`ToolApprovalPolicy`). `agent_dag`'s
  `DagBuilderAgent` has a model-free `execute(script)` handler that runs a script an
  agent wrote, each `run_agent` call a subagent workflow. A changed flow is a new script,
  and you run that.

### 3.3 Deployed Temporal

- The namespace `scadbuddy` keeps closed workflows for 168h
  (`eh-homelab/clusters` `applications/scadbuddy/temporal-cluster.yaml:159`), the same
  7 days as `print_runs`' own pruning today (`bambuddy/runs.py:91` `RETENTION`).
- DO Spaces (`atl1.digitaloceanspaces.com`, bucket `eh-hs-db-backups`) already holds
  the databases' backups (`applications/scadbuddy/cnpg-objectstore.yaml`).

## 4. Printing on Temporal

### 4.1 Creates: update-with-start, the workflow writes our record

Today the API inserts and then starts. `RenderService.submit` commits the `render_jobs`
row, then starts `TemplatePipeline`, and `reconcile_once` starts any row whose start was
lost. The print route `claim`s a `print_runs` row, then starts a task. Both need repair
work because the row can exist without its execution.

From here on a create is one call to Temporal:

1. The route calls `execute_update_with_start_workflow` with:
   - the start: `PrintRun.run`, `id = "print-" + run_key`, task queue `bambuddy`,
     `id_conflict_policy = USE_EXISTING`;
   - the Update: `accepted`.
2. The workflow's first activity, `print_accept`, does today's `prepare_run` reads (the
   plates, the printer, the preset catalogue and the printer's status: reads only,
   seconds). Then, in one transaction, it inserts the `print_runs` row and publishes the
   `print.run` event.
3. The `accepted` Update returns once `print_accept` has finished:
   - **refused:** the route answers 422 with the problem it answers today, nothing is
     inserted, and the workflow ends;
   - **accepted:** the route answers 202 with the row;
   - **a repeat** (the workflow already existed): the Update returns that run's
     current row, and the route answers 200 with `repeated: true`.

A row exists only if its workflow does, so there is nothing to reconcile. Renders
should move to the same shape (it deletes `reconcile_once`). That is a follow-up issue,
not this spec.

### 4.2 Identity and repeats

- `run_key` is `bambuddy/runs.py:167`. Its first argument becomes a source key,
  `output:<id>` or `library:<file id>`, followed by the canonical request body including
  `request_id`.
- **While the run is in flight**, `USE_EXISTING` attaches a repeat to it.
- **After it ends:** a run that succeeded, or failed with `may_have_queued`, keeps its
  workflow open for `REPEAT_WINDOW` (10 min, `runs.py:89`) on a timer. While open, the
  `accepted` Update answers repeats with the finished row. Then the workflow completes,
  and the same key later starts a new run. A run refused or failed before any enqueue
  completes at once, so a retry is a new run. This is #567's rule, kept by the workflow
  instead of `PrintRunStore.find`.

### 4.3 The workflow

`backend/scadbuddy/workflows/printing.py`, `PrintRun`, on `bambuddy`. The activities are
today's `execute_run` (`bambuddy/print_run.py:378`) and `slice_and_queue`
(`bambuddy/dispatch.py:60`), cut at each Bambuddy call:

| Activity | Does | Retry |
|---|---|---|
| `print_accept` | §4.1 step 2 | default; a refusal is non-retryable |
| `print_upload` | `source.file_to_print`. An output's 3MF is fetched from the API (§4.5), replated and recoloured, and `POST /library/files`; a library file is a no-op | default; phase 1 confirms a retried upload reuses `ensure_uploaded`'s existing file rather than adding a second |
| `print_resolve` | spool presets, `gather_plate_options`, `resolve` per plate, hardware warnings | default |
| `print_slice` | `POST /library/files/{id}/slice`, then polls `/slice-jobs/{id}` every 2 s **inside the activity**, heartbeating, up to `DEFAULT_SLICE_TIMEOUT` (600 s) | default; heartbeat timeout 30 s |
| `print_enqueue` | `POST /queue/` | **`maximum_attempts = 1`** |
| `print_record` | `source.record`, `remember_project`, starts the progress observer and watcher | default |
| `print_project` | guarded transition of the row (`running` → `succeeded` / `failed`), `print.run` event in the same transaction | default |

Per plate: `print_slice`, then the workflow records `enqueue_attempted` in its own state
**and** through `print_project` before it schedules `print_enqueue`. An enqueue that
times out, or a later plate failing after an earlier one queued, ends the run `failed`
with `may_have_queued = true`, which is today's meaning. A worker that dies mid-slice
loses only that activity attempt, which retries. A worker that dies mid-enqueue cannot
retry it (`maximum_attempts = 1`), so the run reports `may_have_queued`.

### 4.4 Our record, Visibility, Archival

- **`print_runs` is our system of record**, written only by the workflow's activities,
  following the `render/projection.py` pattern. `GET /print/runs/{id}` (`id` is the row
  id, returned by the 202) reads it, as today. A new migration:
  - drops `heartbeat_at`;
  - adds `source_kind text not null default 'output' check (source_kind in ('output','library'))`
    and `file_id integer`;
  - adds `workflow_id text`.

  For a library run, `output_id` is null (made nullable), and the event topic is
  `print:library:<file id>`.
- **Retention** becomes a Postgres setting, `print_run_retention_days` (the
  `core/settings.py` pattern, shown in Settings → Printing). Empty, the default, keeps
  every row: the rows become the start of print history (#305, #912). A number prunes
  older rows.
- **Visibility:** custom Search Attributes `ScadbuddySource` (Keyword),
  `ScadbuddyOutputId` (Keyword), `ScadbuddyLibraryFileId` (Int), `ScadbuddyStatus`
  (Keyword) and `ScadbuddyMayHaveQueued` (Bool). They are set at start and upserted at
  each transition, and registered on the namespace in `eh-homelab/clusters`.
- **Archival:** history and visibility archival on the `scadbuddy` namespace, with the
  S3 provider on DO Spaces under its own prefix. A closed run's history is then readable
  after the 168h retention. The bucket, prefix and credentials item are a clusters
  change, made with phase 1's manifests.

`PrintRuns`, `PrintRunStore`'s claim/find/heartbeat/expire, `HEARTBEAT_INTERVAL`,
`LOST_AFTER` and the `LOST`/`LOST_UNQUEUED` texts are deleted.

### 4.5 The `scadbuddy-print` worker

- `python -m scadbuddy.worker --queue bambuddy`. The `--queue` flag is new (today the
  queue comes only from `temporal_task_queue_render`, `worker.py:175`), with a setting
  `temporal_task_queue_bambuddy` (default `bambuddy`). It has its own worker-deployment
  name, `scadbuddy-print`, versioned and drained like `scadbuddy-render`
  (`workflows/client.py`).
- **Its own Deployment** in `eh-homelab/clusters` `applications/scadbuddy/`. It holds the
  full Bambuddy key and runs no template or agent code. That is the template spec §9's
  "printing activities run on the `bambuddy` task queue in a separate Deployment".
- It does not mount `scadbuddy-data`, which is a single-node (RWO) volume. `print_upload`
  reads an output's 3MF from the API's cluster-internal service
  (`GET /api/v1/outputs/{id}/model.3mf`). Once the blob store's `StoreBundle` lands
  (#672), it reads from the store instead. Phase 1's plan confirms the route returns the
  stored bytes that `OutputSource` expects, or adds a variant that does.
- `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` serves `bambuddy` (and `projects`, §6) too, for
  dev and `tests/api`.

### 4.6 The frontend and the agent

- `runLibraryPrint` (`frontend/src/api/client.ts:987`) follows the run exactly like
  `runPrint` (`:812`). The 202, the follow loop, `reattach` and `mayHaveRun` are shared.
- `agent/src/tools/print.ts`: the library print tool re-attaches with the same
  `request_id`, like the output one.

## 5. Durable agent sessions

### 5.1 Mode

- `ai_sessions.mode text not null default 'classic' check (mode in ('classic','durable'))`,
  in a new agent migration. It is set at insert and never updated.
- `mode` is accepted only when a session is created: the chat socket's `user.message`
  without `sessionId` (`agent/src/routes/chat.ts:313`), `POST /api/v1/ai/sessions`
  (`StartBody`, `sessions.ts:108`) and the `sessions_start` tool. When it is omitted, the
  `ai_settings` key `session_mode` applies (default `classic`). On an existing session
  it is refused.
- **UI.**
  - While the chat is empty (no session yet), the composer shows Advanced, a disclosure
    with one select: Classic / Durable. After the first message it is gone, and the
    header shows a Durable badge on a durable session.
  - The last choice is remembered in `localStorage` key `scadbuddy.assistant.mode`,
    wrapped in try/catch like `AssistantChat.tsx:42-67`. With nothing stored, the global
    default is used.
  - Settings → Assistant gains "Default session mode", next to Session limits, through a
    `GET`/`PUT /api/v1/ai/settings/session-mode` route group (a `src/routes/` module).

### 5.2 The session workflow

- `DurableSession`, workflow ID `session-<ai_sessions.id>`, on queue `agent`, in a new
  Python package `agent-durable/`. It is shipped as the Dockerfile target
  `agent-durable` and runs as a second container in the agent pod.
  - It is built on `DurableClaudeAgent` with `auto_continue_as_new` and
    `live_output=True`.
  - Each user message is an Update, `send_message`. Between messages it calls
    `agent.continue_as_new()` when suggested, as the README prescribes for chats.
- **The dependency** is `temporalio-claude-agent-sdk @ git+https://github.com/osamastro7-droid/ai-integrations@<commit>#subdirectory=python/claude_agent_sdk`,
  pinned to a commit in `uv.lock`. It is **not vendored**; it moves to the PyPI release
  once one exists.
  - The Dockerfile asserts the Claude Code version the Python `claude-agent-sdk` bundles,
    as it does `CLAUDE_CODE_VERSION` for the TypeScript SDK.
  - The two are bumped together.
- **`SessionStore`** is implemented on Postgres, following the SDK's Postgres example.
  If its shape is `ai_session_entries`' (which already mirrors the SDK transcript), it
  uses that table; otherwise it adds `ai_durable_entries`. Every worker shares one `cwd`
  (`/srv/agent`), as the store keys sessions by it.
- **The credential** is read from `ai_credentials` and decrypted in Python with a port of
  `openSecret` (`agent/src/secrets.ts:187`):
  - AES-256-GCM, sealed format `version | IV(12) | tag(16) | ciphertext`;
  - AAD `v2|ai_credentials:default:{"kind":…,"base_url":…}` for the secret, and
    `dek:` + that for the data key;
  - KEK id = the first 16 hex characters of the key's SHA-256.

  One test vector, sealed by the TypeScript code and committed, is opened by both test
  suites. The result goes to the runner's `env` as `ANTHROPIC_API_KEY`, or
  `ANTHROPIC_BASE_URL` plus `ANTHROPIC_AUTH_TOKEN` for a gateway (`credentialEnv`,
  `agent/src/harness/run.ts:176`). It never enters history.
- **Limits.** `max_turns` and `budget_usd` are the session row's. Each segment gets the
  remaining budget as `max_budget_usd`, and the row's `cost_usd` and `turns` are updated
  by an activity after each segment.
- **Events.** The durable worker runs one `follow_agent` subscriber per running session,
  translating `text` / `tool_call` / `approval_needed` / `tool_result` / `done` / `error`
  into the existing `ai_session_events` vocabulary. So the chat socket and the panel
  render a durable session unchanged. That is the README's one-subscriber-in-the-backend
  advice.

### 5.3 Tools as activities

- Every `/api/v1` operation already has a tool, or a `src/tools/coverage.ts` entry
  (`agent/test/coverage.test.ts`). The registry `ALL_TOOLS` (`agent/src/tools/index.ts:26`)
  is therefore the set of activities.
- **The TypeScript agent service** runs a `@temporalio/worker` on `agent-tools`, with one
  activity per tool, registered under the tool's name. Each runs
  `runToolWithOutcome(tool, args, ctx)` (`agent/src/tools/registry.ts:267`), the entry
  point `/mcp` uses, so parsing, tiers, scope and audit are unchanged. The `ctx`
  principal is the session's owner.
- **A build step** exports `ALL_TOOLS` as `[{name, description, input_schema, tier}]`
  JSON, generated like `gen:api`. The Python worker declares each one as
  `activity_as_tool(activity.defn(name=<name>)(_remote), description=…, input_schema=…,
  needs_approval=(tier == "outward"), task_queue="agent-tools")`. `_remote` is never run:
  the plugin needs an `@activity.defn` callable to name the activity (§3.2), and the
  TypeScript worker serves that name.
- **`browser_*` tools** need a paired tab. With none, they fail at once with that
  message rather than wait.

### 5.4 Human-in-the-loop

- An outward call waits in the workflow (`needs_approval`) until a decision.
- The panel's existing approve/deny actions, for a durable session, send the workflow's
  `review` Update (`agent.decide(tool_use_id, approved, approver)`, with the
  `validate_decision` validator). The approver is the authenticated principal, and the
  route checks ownership exactly as `approvals/service.ts` `decide` does. The pending
  list comes from the `pending_approvals` Query.
- `approval_expiry_seconds` (`approvals/service.ts:115`) becomes a workflow timer per
  waiting call that decides *deny* when it fires.
- Classic sessions keep `ai_approvals` unchanged.

## 6. Flows

### 6.1 What a flow is

- A flow is a Python script an agent writes (or anyone writes through the API). It runs
  in Code Mode over these host functions:

| Host function | Is | Answered by |
|---|---|---|
| `render(slug, inputs)` | child `TemplatePipeline` via `RenderService` | the render pipeline |
| `print(source, choices)` | child `PrintRun` (§4) | the print worker |
| `arrange(...)` | child `Arrange` (template spec phase 5, when it lands) | the render worker |
| `tool(name, args)` | activity on `agent-tools` (§5.3) | the tool registry |
| `agent(prompt, skills=…, result_schema=…)` | child `DurableSession` | a new durable session |
| `ask_session(session_id, message)` | `send_message` to that session; waits for the turn's answer | an existing durable session |
| `wait_for_human(question, timeout=…)` | waits on the run's `answer` Update | a person |
| `sleep(duration)` | a workflow timer | Temporal |

- Multi-plate campaigns are ordinary loops over plates, with `wait_for_human`
  ("swap to the pink spool") between them. Choosing a printer or spools is `tool(...)`
  calls and logic. Pre/post steps and file transforms are `tool(...)` and `agent(...)`
  calls.

### 6.2 Running on the harness

- `ProjectWorkflow` is a temporal-agent-harness agent (`@agent.defn`) with a model-free
  `execute(script)` operation, the shape of `agent_dag`'s `DagBuilderAgent.execute`. It
  runs on queue `projects` in `scadbuddy.worker --queue projects`, which holds nothing
  outward.
- The host functions are harness tools (`@agent.activity_tool_defn`, or workflow
  functions for the child workflows). Approvals use the harness's `ToolApprovalPolicy`:
  an outward `tool(...)` goes to a human, through the same panel and an Update. Event
  streams and continue-as-new use the harness defaults.
- Type checking (`code_mode_type_check`) runs when a flow is registered, and again before
  each run. A script that fails is refused with its errors, by line.
- Phase 4 starts by verifying what the harness needs around `ProjectWorkflow` (its
  `SessionManagerWorkflow`, the `code-mode` extra) and how `execute` behaves across a
  Reset (§6.4). Anything that would mean departing from the harness is brought to the
  user (§8).

### 6.3 Records and API

- **Tables** (backend migrations, our system of record):
  - `workflow_definitions(id, name, version, script, created_by, created_at)`; versions
    are immutable.
  - `workflow_runs(id, definition_id, version, status, waiting_on jsonb, steps jsonb,
    workflow_id, created_at, updated_at)`, written by the workflow's activities.

  Creates follow §4.1: `POST /runs` is update-with-start, and the first activity
  type-checks, inserts the row and publishes the event. Search Attributes
  `ScadbuddyFlow` and `ScadbuddyStatus` are set on each run.
- **Routes:** `POST /api/v1/workflows`, `GET /api/v1/workflows`,
  `POST /api/v1/workflows/{id}/runs`, `GET /api/v1/workflow-runs/{id}` and
  `POST /api/v1/workflow-runs/{id}/answer`. Each gets a tool, as the coverage test
  requires, so an agent in either mode registers and starts flows, and a durable one gets
  them as activities.
- `ask_session` reaches durable sessions only. A classic session is not a workflow.
- **UI:** Settings → Administration links to the Temporal UI already (#668). A minimal
  Workflows page lists runs with their status, what each waits on, and Approve/Answer. An
  agent panel links to the runs its session started.

### 6.4 Changing course: Temporal Reset

- There is no in-place revision. When a run goes wrong (a jammed printer on plate 7),
  you reset it to the last good event with Temporal Reset. The run replays to there and
  continues on the current code, with the same inputs.
- A changed script is a new run of a new version, because the script is the run's input.
- `POST /api/v1/workflow-runs/{id}/reset {event_id}` wraps the reset, with a tool and the
  audit trail.

## 7. Errors and testing

- **Print** (backend, `requires_temporal` and `requires_postgres`):
  - the 422 through update-with-start;
  - a repeat while running, within the window, and after it;
  - the library route's 202;
  - a worker killed mid-slice (resumes) and mid-enqueue (`may_have_queued`);
  - the projection's guarded transitions and the event in the same transaction;
  - the retention setting.

  These use the existing `tests/api/conftest.py` Temporal fixture and a fake Bambuddy
  client.
- **Durable session:**
  - `agent-durable/` tests run the bundled Claude CLI against
    `agent/test/support/fakeAnthropic.ts` as a gateway; tests never call Anthropic;
  - the `agent-tools` activities use `@temporalio/testing`;
  - the credential test vector on both sides;
  - `review` approve, deny, and expiry;
  - `mode` refused on an existing session;
  - an end-to-end test from the chat socket to a durable turn.
- **Flows:**
  - type-check refusal;
  - `wait_for_human` answered;
  - an outward `tool` waiting for approval;
  - `agent(...)` and `ask_session` against the fake endpoint;
  - Reset to before a failed step.
- **CI:**
  - new jobs follow CLAUDE.md (hosted runners only, `type=gha` with `mode=min` for new
    scopes);
  - the `agent-durable` image gets a scope of its own;
  - the git-pinned dependency is in the lockfile, so `--frozen` installs reproduce it.

## 8. Deviations from Temporal

None. The ones considered, and how each was resolved:

| Considered | Resolution |
|---|---|
| Our Postgres rows beside Temporal | Kept, and Visibility + Archival added. The user decided ScadBuddy is the system of record. Rows are written only by workflow activities, and creates are update-with-start, so no row exists without its execution. |
| Polling slices on a workflow timer | Replaced by polling inside the activity with heartbeats (§3.1). |
| `resume_from` (reusing earlier runs' results) | Dropped for Temporal Reset (§6.4). |
| Code Mode pieces without a harness agent | Dropped. `ProjectWorkflow` is a harness agent (§6.2). |
| Tool stubs for TypeScript activities | Not a deviation. The plugin's documented `activity_as_tool` with `task_queue`, and Temporal resolves activities by name (§5.3). |

If implementing any phase turns up a place where following the SDK or a framework is
not possible, the work stops and the user decides.

## 9. Phasing

Each phase is its own implementation plan and ships alone.

1. **PrintRun on Temporal** (§4): `PrintRun` and its activities, update-with-start
   creates, the `print_runs` migration and retention setting, the library route's 202,
   `runLibraryPrint`, `scadbuddy.worker --queue`, and the clusters manifests
   (`scadbuddy-print`, Search Attributes, Archival). Fixes #742 and the lost run.
2. **Tools as activities** (§5.3): the `ALL_TOOLS` export and the `agent-tools` worker in
   the agent service.
3. **Durable session mode** (§5.1, §5.2, §5.4): `agent-durable/`, the plugin pin, the
   `SessionStore`, the credential port, the event subscriber, HITL, the mode UI and
   setting.
4. **Flows** (§6): the harness verification, `ProjectWorkflow`, host functions, records,
   routes, Reset, and the Workflows page.

Follow-up issues, outside this spec: renders' creates move to update-with-start (§4.1),
deleting `reconcile_once`; and moving the plugin pin to PyPI once the package is
published.
