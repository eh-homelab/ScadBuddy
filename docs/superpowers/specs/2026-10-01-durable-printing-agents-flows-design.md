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
listed as a deviation, and the user decides. §9 lists the open ones (none).

Base design: `2026-09-22-scadbuddy-design.md`. Renders on Temporal, the blob store and
the trust model (§9): `2026-09-27-template-pipelines-design.md` (the "template spec"
below). Print flow: `2026-09-24-print-flow-design.md`,
`2026-09-27-spool-first-print-design.md`, `2026-09-28-print-library-file-design.md`.
AI agent: `2026-09-27-ai-integration-design.md`. Issues: #470 (the output run's 202,
implemented in PR #567), #742 (the library run's 202, implemented in PR #945, merged
2026-10-01 18:56 UTC after this spec's incident), #305 / #912 (print history).

## 1. Why

On 2026-10-01 at 14:49 UTC, Envoy cut `POST /api/v1/print/library/68/run` off at its
15 s `response_timeout` and answered 504.

The 15 s is the Envoy Gateway's default route timeout on `scadbuddy.internal`:
- the access log has `response_flags: UT`, `duration: 14999`;
- `eh-homelab/clusters` `clusters/prod/scadbuddy/httproute.yaml:31` lifts that default
  only for the agent's streaming routes.

`2026-09-27-spool-first-print-design.md` records "the 60 s ingress timeout" for #470,
measured on a different path into ScadBuddy. Both numbers are real, so a request must fit
the shortest proxy in front of it, which today is 15 s.

The backend carried on regardless. Bambuddy's log shows one
`POST /library/files/68/slice` and one `POST /queue/`, and its pending queue went from 8
to 9. The dialog said "may still have been queued", which was true.

- At the time, `POST /print/library/{id}/run` uploaded, sliced and queued inside one
  request (#742). PR #945 has since fixed that.
  - The route now answers 202 through the same `accept_run` as an output's run
    (`api/printing.py:210`), keyed and recorded as `library:<file id>`.
  - The frontend follows both kinds with one `followPrintRun`
    (`frontend/src/api/client.ts:362`).
- **What remains is the same for both kinds.** A run is an `asyncio` task in the API
  process (`bambuddy/runs.py:378` `PrintRuns.start`), and it dies with the pod.
  - Run `3c241a88` (07:29 UTC the same day) is still `running` with
    `enqueue_attempted = false`, and its heartbeat never moved.
  - Only the `LOST` expiry would ever end it, and then the dialog tells the person to
    check Bambuddy's queue by hand.
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
- **One architecture for every operation (§4).** Every command, whether a render, a
  print, a git commit, a Bambuddy write or a download, is a Temporal workflow of the same
  shape. ScadBuddy's own background loops become workflows or Schedules. No request
  waits on work a proxy can cut off, and no retry repeats an effect.

Non-goals

- Sandboxing beyond what the frameworks give. A flow's script runs in Code Mode's
  sandbox, and a template's pipeline is governed by the template spec §9.
- Classic sessions as flow targets. Only a durable session can be sent a step (§7.3).
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
  - The PR is a draft by an external contributor (GitHub author association `NONE`), in
    Temporal's repository. It stays a draft until two known limitations are solved:
    - **Built-in tools run inside the model segment.** Claude Code's own tools (Bash,
      Edit and the rest) are not activities, so a segment that runs again can run them
      again.
      - In ScadBuddy this reaches only `Skill`. The plugin enables built-in tools only
        when `builtin_tools` is passed, and a durable session passes exactly
        `["Skill"]` (§6.3b). Running `Skill` again reads the same instructions again,
        which is harmless.
      - Every tool with an effect is a durable tool (§6.3).
      - Phase 5 asserts in a test that a durable session's engine is started with no
        built-in tool but `Skill`.
    - **One durable tool call at a time.** The engine keeps one paused call per run. When
      Claude asks for several in one message, the first pauses, and each of the others
      is told to call again after its result.
      - A durable session's tool calls are therefore serial within a turn. That is
        correct but slower for a turn that would have read several things at once.
      - §6 designs to it rather than around it: the system prompt asks for one tool call
        per message, as the plugin's runner already does.
      - Parallel work belongs in a flow. A flow's host calls run in Code Mode (§7.2),
        not through this plugin, so `asyncio.gather` there is real parallelism.
    - Phase 5 re-reads the PR's limitation list at its start. A limitation that is still
      open and that this design relies on stops the work, and the user decides (§9).
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

## 4. One shape for every operation

Renders, prints, flows and sessions each got a Temporal design above. They must not
become four architectures, so this section fixes the one they share. It also says what
happens to every other operation ScadBuddy performs. As of 2026-10-01, every route below
does its slow or outward work inside the request, except an output's print run, and the
API process runs its own background loops.

### 4.1 Commands and reads

- **A command** is an operation with an effect outside the one Postgres transaction that
  records it: a git commit, a file written to the data volume, a clone or download, a
  Bambuddy write, an `openscad` render, or a message to an agent. **Every command is a
  Temporal workflow, in the shape of §4.2.**
- **A read** has no effect: a `GET`, a proxied Bambuddy stream, a `/check` or `/choices`
  that only reads, or an LSP socket. It stays a plain request. Temporal's durability buys
  nothing for a call that changes nothing, and the result belongs to the caller who
  waits for it.
- **A write that is only one Postgres transaction**, such as settings, saved presets
  (`saved_presets`) and the credential, is already atomic and leaves nothing half-done
  to resume. It stays a request. If such a write ever gains an outward step (for example
  registering ScadBuddy in Bambuddy's sidebar), it becomes a command.

### 4.2 The command shape

The same for every kind:

1. **Identity.** The workflow ID is `<kind>-<key>`.
   - The key hashes the subject (the model slug, output id, library file id, etc.), the
     canonical body, and a client `request_id` (one per deliberate press, as #470 made
     for prints).
   - **A repeat never repeats the effect, whether the first execution is still running
     or already closed.** Two SDK policies do this together:
     - `id_conflict_policy = USE_EXISTING`: a retry while the execution is running
       attaches to it.
     - `id_reuse_policy = ALLOW_DUPLICATE_FAILED_ONLY` for every `request_id`-keyed
       kind: a retry after a *completed* execution cannot start a second one. A `done`
       command finishes in under a second, so this is the usual case after a dropped
       answer. The start fails with `WorkflowAlreadyStartedError`.
     - **Which outcomes count as "failed" in Temporal's sense is tied to our record.**
       A command's workflow *fails* only when step 3 wrote no record: a refusal, or an
       error before the insert. Nothing was done, so its retry may start again.
     - Once the record exists, every outcome *completes* the workflow, success or
       failure, with that outcome as its result and in our record. That covers a
       print that failed with `may_have_queued`, a slice start that failed without a
       job id, and a commit that failed after writing. So a retry with the same
       `request_id` can never run it again; it gets the recorded outcome, and the next
       deliberate press (a new `request_id`) is a new command.
     - On that error the route reads our record by `workflow_id` and answers with it,
       as the original answer would have been (`repeated: true`).
     - The route also reads the record first, before calling Temporal. That makes a
       repeat cheap, and it still holds after the namespace's 168h retention has
       forgotten the ID.
     - **How "a record means the workflow completes" is enforced**, since Temporal does
       not do it for us. Two guards, either of which is enough:
       - *The record lookup comes first.* A retry whose `request_id` already has a
         record is answered from the record and never reaches
         `start_workflow`, whatever state the execution closed in (`Failed`,
         `TimedOut`, `Terminated` or `Canceled`, all of which
         `ALLOW_DUPLICATE_FAILED_ONLY` would let a new start reuse). The reuse policy
         only covers the window before the record exists, when there is no effect to
         repeat.
       - *The command workflow base class never lets the execution fail after the
         record.* Everything after step 3 runs inside one `try`: an `ActivityError`,
         `ApplicationError` or cancellation is caught, written through the kind's
         projection activity as an `unexpected` failed outcome with its message, and
         the workflow returns normally. Any other exception in workflow code is, in
         the Python SDK, a failed *workflow task* that Temporal retries, so it leaves
         the execution running, not `Failed`. Command workflows set no execution or
         run timeout, so none can close `TimedOut`. An operator's terminate is the
         one way left, and the first guard covers it.
   - A body-only print key (a client that sends no `request_id`) keeps #470's rule
     instead: a repeat within `REPEAT_WINDOW` is the same run, and after it a new one
     (§5.2). Such a print uses `ALLOW_DUPLICATE` with the window.
   - **The one exception is a command that is idempotent by content**, whose repeat
     *should* join the first rather than be a second effect. Its key is the content alone,
     with no `request_id`.
     - Renders are the case: `render_key` (`render/job_models.py:162`) hashes slug,
       revision and params only, so that identical requests from any caller coalesce
       onto one job (§4.5).
     - A kind declares which key it uses. Every command with a physical or external
       effect (prints, sends, Bambuddy writes, git commits) uses `request_id`.
2. **Update-with-start.** The route calls `execute_update_with_start_workflow` with the
   Update `accepted`.
3. **First activity: validate and record.** Refusals (422, 404, 409, as each route
   answers today) end the workflow with nothing written. Otherwise, in one transaction, it
   writes our record and publishes the event in the same transaction
   (`PgNotifyEventBus.publish_in`, `core/pg_events.py:413`).
   - **The insert is idempotent against Temporal retrying this activity** (a worker that
     dies after the commit but before reporting it). Every record carries
     `workflow_id` and `workflow_run_id` with a unique index on the pair, and the
     insert is `ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING`. On a conflict
     the activity re-reads the existing row and returns it. The event is published only
     when the row was actually inserted, in the same transaction, so a retry neither
     duplicates the row nor announces it twice.
   - The run id is part of the key because a workflow id can have several executions
     over time (a render's `render-<render_key>` after its first closed, a print key
     after `REPEAT_WINDOW`), and each execution has its own row.
4. **The answer.** Each kind declares one of two:
   - **`done`**, for commands that normally finish in under a second (a git commit, a
     delete). The route answers as it does today (200/201/204 with today's body).
     - The `accepted` Update handler is `async` and awaits
       `workflow.wait_condition(lambda: self.result is not None)`. `self.result` is set
       once the effect's activities have finished, after which the workflow writes its
       final projection and completes.
     - An Update handler waiting on workflow state is the SDK's documented use of
       `async` handlers. It is not a wait for workflow completion, which Updates do not
       offer.
     - So for a `done` command, step 5 below is the effect the Update waits on, not
       work that comes after the answer. If the result is not there within
     `command_answer_deadline` (default 10 s, below Envoy's 15 s), the route answers
     **202** with the operation instead, and the client follows it.
   - **`accepted`**, for commands that take long (a print, a pin's clone, a URL import,
     a font install, a send). The route answers **202** with the record once step 3
     is done.
   - **Both waits are bounded by `command_answer_deadline`.** Step 3's activity has
     `start_to_close_timeout` 8 s, and its retries continue in the workflow.
     - If the `accepted` Update has not returned by the deadline, there is no record
       yet to return. The route answers **503** with `Retry-After: 2` and the problem
       type `command-still-accepting`.
     - The client re-sends the same `request_id`, which `USE_EXISTING` attaches to the
       same execution, until it gets the record or the refusal.
     - So no command, of either kind, holds a request open past the deadline, and a
       slow validation (a sluggish Bambuddy status read, say) cannot reproduce §1's 504.
5. **Then** (or, for `done`, while the Update waits) the workflow carries on with its activities. Each transition is a guarded
   write to our record plus an event in the same transaction, the `render/projection.py`
   pattern. Search Attributes are upserted at each transition.
   - **Every kind carries the same three:** `ScadbuddyKind` (`render`, `print`,
     `flow`, `session`, or an `operations` kind), `ScadbuddySubject` (the slug, output
     id, `library:<file id>`, definition id or session id) and `ScadbuddyStatus`.
     They hold no content, only identifiers and states, because Search Attributes
     are not passed through the payload codec (§6.5).
   - A kind with its own needs adds typed attributes on top, never instead. Prints
     add `ScadbuddyMayHaveQueued` (Bool); flows add `ScadbuddyFlow` (the definition's
     name).
   - So one Visibility query (`ScadbuddySubject = "<x>" AND ScadbuddyStatus = "running"`)
     finds every in-flight command of every kind for that subject.

**Our record.**
- A kind that already has its own table writes that: `render_jobs`, `print_runs`,
  `workflow_runs`.
- Every other kind writes one generic table, `operations(id, kind, subject, status,
  request jsonb, result jsonb, error jsonb, workflow_id, workflow_run_id, created_at,
  finished_at)`. It is
  read by `GET /api/v1/operations/{id}` (and so a tool) and announced as `operation.*`
  events. Its pruning is a Postgres setting, `operation_retention_seconds`, like
  `print_run_retention_seconds`.
- Temporal's namespace Archival (§5.4) covers history past 168h for every kind. A
  durable session's and a flow run's payloads are encrypted with a key of their own
  before they reach history (§6.5), so the archive holds ciphertext for them.

**The client.**
- One helper, `command()` in `frontend/src/api/client.ts`, sends a `request_id`. It takes
  either answer, follows a 202 (event or `GET /operations/{id}`) to its result, and
  re-sends the same `request_id` after an answer that never arrived.
- This generalises today's `followPrintRun` loop (`client.ts:362`), with its `reattach`
  (`:343`) and `mayHaveRun` (`:273`).
- The agent's tools get the same behaviour from one wrapper in `agent/src/api/`.

### 4.3 Queues follow what a worker holds

The template spec §9 separates two workers by what they hold: render workers hold only
the store key, and the `bambuddy` Deployment holds the fuller Bambuddy key and runs no
template code. This spec extends that rule, as its own decision, to every queue below:
each queue's worker holds only what its commands need.

| Queue | Worker | Commands | Holds |
|---|---|---|---|
| `render` | `scadbuddy-render` | renders, previews, Arrange | store key; runs template code |
| `library` | `scadbuddy-library`, a container in the API pod (it needs `scadbuddy-data`, which is RWO) | model create/import/patch/duplicate/delete, source and file writes, thumbnail/readme/media writes, preset writes that need `openscad`, version restore, upstream merge/dismiss/detach, library pin/repin/unpin/remove, font install, the sweeps | the data volume and git; no Bambuddy key; runs no template code |
| `bambuddy` | `scadbuddy-print` (§5.5) | prints, send, project file, create project, file into project, Bambuddy part of output delete, reprint, timelapse pull, sidebar registration, analyzer fix apply | full Bambuddy key |
| `agent-tools`, `agent` | `agent-tools`: the agent service's sidecar; `agent`: the `agent-durable` sidecar beside it (§6.2) | tool calls, sessions, plugin package install/approve (a git fetch) | agent secrets, Anthropic credential |
| `projects` | `scadbuddy.worker --queue projects` | flows (§7) | nothing outward; the KEK, read-only, only to encrypt flow payloads (§6.5) |

Git writes to one model's history still take the catalogue's existing lock inside the
activity. Two commands on one model are therefore ordered as git requires, and nothing
new serialises across models.

### 4.4 Background loops

| Today (`main.py` lifespan) | Becomes |
|---|---|
| render reconciler (started at `render/submit.py:112`; `reconcile_once` at `:192`) | deleted: renders are created by §4.2, so nothing needs reconciling |
| print runs' tasks and heartbeat (`bambuddy/runs.py`) | deleted (§5) |
| print watcher (`bambuddy/watcher.py`: a rescan loop plus a `_follow` task per output) | `FollowPrint`, a workflow per queued print on `bambuddy`, started by the `PrintRun` workflow, after its `print_succeed` activity, as an abandoned child (§5.3). It polls Bambuddy inside a heartbeating activity, on the `<bambuddy queue>-follow` queue, until the print ends, then writes the outcome and the event |
| asset/blob/staging sweeper (`main.py:211`) and the boot sweeps (`main.py:225–281`) | Temporal **Schedules** on `library`. The interval is today's setting; the boot sweeps run once more as a schedule trigger at deploy |
| preview scheduler (`PreviewScheduler.start`, `render/previews.py:155`) and its boot pass over every model (`request_all`, `:184`) | `RenderPreview` is already a workflow; the backfill becomes a Schedule-triggered workflow |
| PgNotify bus, settings follower, in-process dev worker, openscad version probe, git reaper thread, LSP subprocesses | stay. They are the process's own plumbing, not operations |

### 4.5 Renders join the shape

`POST /models/{slug}/render` becomes §4.2 with answer `accepted`:
- the first activity resolves the revision and schema (today's `history.resolve`, a
  `git rev-parse`, at `api/jobs.py:173`, and `schema_of` at `:216`) and inserts the
  `render_jobs` row;
- `TemplatePipeline` continues;
- `RenderService`'s insert-then-start and `reconcile_once` are deleted.

**Coalescing, claims and superseding move into the workflow.** Today they live in the
row (template spec §3.3: `claims + 1` on a pending row with the same `render_key`; a
`supersedes` request releases a claim; the last release marks the row `cancelled` and
cancels the workflow). The template spec chose insert-then-start over start-first for
two reasons, and both are answered here:

- *"Coalescing and claim counting would then live in two systems."* They live in one,
  the workflow.
  - `TemplatePipeline` is started as `render-<render_key>` with `USE_EXISTING`, so every
    request for the same content reaches the same execution.
  - This changes the ID of a shipped workflow, which is `render-{job_id}` today
    (`render/projection.py:74`). At the phase 2 rollout, executions already running as
    `render-{job_id}` finish on the build they are pinned to (Worker Versioning, and the
    old worker drains on SIGTERM, `worker.py`). New requests start
    `render-<render_key>`. A request during the drain for content an old
    execution is still rendering does not join it, so at most one extra render per
    in-flight job happens once, and `piece_key` still dedupes its openscad work.
  - Its `accepted` Update adds one claim, in workflow state. A workflow's handlers run
    one at a time, so that count is exact, as the `claims + 1` row update was.
  - The row's `claims` column becomes a projection of that count, written with each
    change.
- *"A `GET /jobs/{id}` between the start and the first activity would 404."* It cannot.
  The route answers only after the `accepted` Update returns, and that is after the first
  activity inserted the row (§4.2 step 3).

The rest:
- **The first request** of an execution runs the first activity. It resolves the
  revision and schema, checks `render_queue_max`, inserts the row with a new job id, and
  publishes the event.
  - The queue check is the same counted insert under the same lock as
    `JobProjection.submit`'s today (`render/projection.py:134`), so a full queue is still a 429 with `Retry-After`
    (`QueueFullError`), and nothing is started.
  - **A later request** that reaches the open execution gets that row back with
    `coalesced: true` and one more claim. This is today's response.
  - The job id stays unique per job. A request for the same content after the
    execution has closed starts a new execution under the same workflow ID
    (`ALLOW_DUPLICATE` reuse), with a new job id. Today, too, only an unfinished row
    coalesces.
- **Superseding.** A request naming `supersedes: <job id>` looks
  up that row's `workflow_id` and run id and sends that execution the `release` Update.
  - `release` takes off one claim. At zero the workflow writes `cancelled` through its
    projection activity and stops at the next activity boundary.
  - Its `RenderPiece` children are left to finish (`ABANDON`), so the request that
    superseded it finds the pieces they share already rendered.
  - This is template spec §3.3's behaviour, with the cancellation decided by the
    workflow instead of by the API.
- Each execution carries §4.2's `ScadbuddyKind = render`, `ScadbuddySubject` (the slug)
  and `ScadbuddyStatus`.
- `render_key` is the content-keyed exception of §4.2, so there is no `request_id`.
  `piece_key` keeps deduping openscad runs across jobs (CLAUDE.md: never swap them).

## 5. Printing on Temporal

### 5.1 Creates: update-with-start, the workflow writes our record

Today the API inserts and then starts. `RenderService.submit` commits the `render_jobs`
row, then starts `TemplatePipeline`, and `reconcile_once` starts any row whose start was
lost. Both print routes go through `accept_run` (`api/printing.py:210`), which `claim`s a
`print_runs` row and then starts an in-process task. Both need repair
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

A row exists only if its workflow does, so there is nothing to reconcile. This is §4.2's
shape with answer `accepted`; renders take the same shape (§4.5).

### 5.2 Identity and repeats

- `run_key` is `bambuddy/runs.py:167`. Its first argument is already the run's subject,
  an output id or `library:<file id>` (#945). It is followed by the canonical request
  body, including `request_id`. The workflow ID is `print-` + that key.
- **While the run is in flight**, `USE_EXISTING` attaches a repeat to it.
- **After it ends:** a run that succeeded, or failed with `may_have_queued`, keeps its
  workflow open for `REPEAT_WINDOW` (10 min, `runs.py:89`) on a timer. While open, the
  `accepted` Update answers repeats with the finished row. Then the workflow completes,
  and the same key later starts a new run. A run refused or failed before any enqueue
  completes at once, so a retry is a new run. This is #470's rule, kept by the workflow
  instead of `PrintRunStore.find`.
- The window and `ALLOW_DUPLICATE` apply to body-only keys. A key with a `request_id`
  (every current client sends one) is `ALLOW_DUPLICATE_FAILED_ONLY` with the record
  lookup of §4.2.
  - A retry of the same press, however late, gets its run.
  - The next press has a new `request_id`, so it is a new print.
  - A refused run *fails* its workflow and writes no record, so its retry is a new
    start, which is refused again or runs.
  - A run that has a record *completes* its workflow whatever its outcome (§4.2), so
    its retry gets that run as recorded: a failure with `may_have_queued`, or a slice
    start that failed (§5.3). That is #470's rule.

### 5.3 The workflow

`backend/scadbuddy/workflows/printing.py`, `PrintRun`, on `bambuddy`. The activities are
today's `execute_run` (`bambuddy/print_run.py:366`) and `slice_and_queue`
(`bambuddy/dispatch.py:60`), cut at each Bambuddy call:

| Activity | Does | Retry |
|---|---|---|
| `print_accept` | §5.1 step 2 | default; a refusal is non-retryable |
| `print_upload` | `source.file_to_print`. An output's 3MF is fetched from the API (§5.5), replated and recoloured, and `POST /library/files`; a library file is a no-op | default; phase 1 confirms a retried upload reuses `ensure_uploaded`'s existing file rather than adding a second |
| `print_resolve` | spool presets, `gather_plate_options`, `resolve` per plate, hardware warnings | default |
| `print_slice_start` | `POST /library/files/{id}/slice` (`client.py:623`), which starts a **new** slice job on every call; returns the job id | **`maximum_attempts = 1`** |
| `print_slice_wait` | polls `/slice-jobs/{id}` every 2 s **inside the activity**, heartbeating, up to `DEFAULT_SLICE_TIMEOUT` (600 s); returns the sliced file id | default (re-polling an existing job is safe); heartbeat timeout 30 s |
| `print_enqueue` | `POST /queue/` | **`maximum_attempts = 1`** |
| `print_record` | `source.record` (which adds the print to the `PrintLog`), `remember_project` | default |
| `print_project` | guarded transition of the row (`running` → `succeeded` / `failed`), `print.run` event in the same transaction | default |

**Following the print.** An activity cannot start a workflow's child, so the record
step is split between the two. In phase 1 the watcher is still the API's. It hears the
run's `print.run` event on the PgNotify bus and calls `PrintWatcher.started` for a run
that queued a plate. Its rescan of the `PrintLog` row `print_record` wrote
(`RESCAN_INTERVAL`, 300 s, `bambuddy/watcher.py:85`) is the backstop for a missed
event, and the progress route's `watch` brings it back for anyone looking. From phase 2, after `print_record` returns,
the `PrintRun` workflow code itself calls `workflow.start_child_workflow(FollowPrint,
…, parent_close_policy=ABANDON)` (§4.4).

Per plate: `print_slice_start`, then `print_slice_wait` on the job id the workflow
recorded, then the workflow records `enqueue_attempted` in its own state
**and** through `print_project` before it schedules `print_enqueue`. An enqueue that
times out, or a later plate failing after an earlier one queued, ends the run `failed`
with `may_have_queued = true`, which is today's meaning.

A worker that dies while polling resumes polling the same job, so no second slice is
started. If `print_slice_start` fails without returning a job id (a timeout, or a
dropped connection after Bambuddy may have accepted it), the run fails before any
enqueue. The run already has its record, so this *completes* the workflow with the
run `failed` (§4.2): a retry with the same `request_id` gets that failed run, and the
next press is a new run.
- Each job id is recorded, so every sliced file the run created is known.
- `print_project` lists the sliced files of a failed run on its row.
- An unknown job that a lost start may have created stays in Bambuddy's library. The
  row's error says so, as `may_have_queued` does for the queue. A worker that dies mid-enqueue cannot
retry it (`maximum_attempts = 1`), so the run reports `may_have_queued`.

### 5.4 Our record, Visibility, Archival

- **`print_runs` is our system of record**, written only by the workflow's activities,
  following the `render/projection.py` pattern. `GET /print/runs/{id}` (`id` is the row
  id, returned by the 202) reads it, as today. A new migration:
  - leaves `heartbeat_at`, which a pre-#1052 pod still writes during the rolling update;
    a later migration drops it (expand/contract);
  - adds `workflow_id text` and `workflow_run_id text`, with a unique index on the pair
    (§4.2 step 3).

  `output_id` keeps holding the run's subject as #945 records it: an output id, or
  `library:<file id>`, whose event topic is `print:library:<file id>`.
- **Retention** becomes a Postgres setting, `print_run_retention_seconds`.
  - It is in seconds, matching the existing `event_log_retention_seconds`
    (`core/settings.py:218`), and is shown in days in Settings → Printing.
  - Empty, the default, keeps every row: the rows become the start of print history
    (#305, #912). A number prunes older rows.
- **Visibility:** §4.2's `ScadbuddyKind = print`, `ScadbuddySubject` (the output id or
  `library:<file id>`) and `ScadbuddyStatus`, plus `ScadbuddyMayHaveQueued` (Bool).
  They are set at start and upserted at each transition. All of §4.2's attributes are
  registered on the namespace in `eh-homelab/clusters`.
- **Archival:** history and visibility archival on the `scadbuddy` namespace, with the
  S3 provider on DO Spaces under its own prefix. A closed run's history is then readable
  after the 168h retention. The bucket, prefix and credentials item are a clusters
  change, made with phase 1's manifests.

`PrintRuns`, `PrintRunStore`'s `claim`, `find`, `heartbeat` and `_expire_lost`, `HEARTBEAT_INTERVAL`,
`LOST_AFTER` and the `LOST`/`LOST_UNQUEUED` texts are deleted.

### 5.5 The `scadbuddy-print` worker

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
- `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` serves `bambuddy` (and `projects`, §7) too, for
  dev and `tests/api`.

### 5.6 The frontend and the agent

- Nothing changes for clients. #945 already made `runPrint` and `runLibraryPrint`
  (`frontend/src/api/client.ts:853`, `:989-990`) share `followPrintRun` (`:362`), with its
  202, follow loop, `reattach` and `mayHaveRun` (`:273`).
- The route's answers (202 / 200 `repeated` / 422) and `GET /print/runs/{id}` keep their
  shapes, so the client and `agent/src/tools/print.ts` need no change beyond §4.2's
  shared `command()` helper.

## 6. Durable agent sessions

### 6.1 Mode

- `ai_sessions.mode text not null default 'classic' check (mode in ('classic','durable'))`,
  in a new agent migration. It is set at insert and never updated.
- `mode` is accepted only when a session is created: the chat socket's `user.message`
  without `sessionId` (`agent/src/routes/chat.ts:317`), `POST /api/v1/ai/sessions`
  (`StartBody`, `sessions.ts:108`) and the `sessions_start` tool. When it is omitted, the
  `ai_settings` key `session_mode` applies (default `classic`). On an existing session
  it is refused.
- **UI.**
  - While the chat is empty (no session yet), the composer shows Advanced, a disclosure
    with one select: Classic / Durable. After the first message it is gone, and the
    header shows a Durable badge on a durable session.
  - The last choice is remembered in `localStorage` key `scadbuddy.assistant.mode`,
    wrapped in try/catch like `frontend/src/components/assistant/AssistantChat.tsx:42-67`. With nothing stored, the global
    default is used.
  - Settings → Assistant gains "Default session mode", next to Session limits, through a
    `GET`/`PUT /api/v1/ai/settings/session-mode` route group (a `src/routes/` module).

### 6.2 The session workflow

- `DurableSession`, workflow ID `session-<ai_sessions.id>`, on queue `agent`, in a new
  Python package `agent-durable/`. It is shipped as the Dockerfile target
  `agent-durable` and runs as **a sidecar in the ScadBuddy pod**, beside the `agent`
  sidecar (AI spec §4.1), with the same trust.
  - It needs nothing on `localhost`: tool calls reach the agent service as activities
    on `agent-tools` (§6.3), events go to Postgres (`ai_session_events`), and it reads
    nothing from the data volume.
  - It runs at one replica to start. More replicas are safe: the session store is in
    Postgres, and each worker's `cwd` is the same fixed path.
  - It is built on `DurableClaudeAgent` with `auto_continue_as_new` and
    `live_output=True`.
  - Each user message is an Update, `send_message`. Between messages it calls
    `agent.continue_as_new()` when suggested, as the README prescribes for chats.
- **The dependency** is `temporalio-claude-agent-sdk @ git+https://github.com/temporalio/ai-integrations@<sha>#subdirectory=python/claude_agent_sdk`,
  pinned to one full commit SHA in `uv.lock`. It is **not vendored**; it moves to the
  PyPI release once one exists.
  - **The source is Temporal's own repository.** #33's head commit is fetchable from
    `temporalio/ai-integrations` by SHA (as of 2026-10-01 the head is `766c6479c40eadbe5300c1133bcf064725540df6`,
    resolved through `repos/temporalio/ai-integrations/commits/<sha>`), so the pin
    never names the contributor's fork, and a force-push there cannot change what is
    built.
  - Phase 5's first task checks that `uv lock` resolves that SHA from
    `temporalio/ai-integrations`. If it does not, the work stops and the user decides
    (§9).
  - **Every bump is reviewed.** A pin change is its own PR, carrying the diff of
    `python/claude_agent_sdk` between the old and new SHAs. It is a supply-chain change:
    this package runs in the container that holds the Anthropic credential.
  - **The pin's continued existence is monitored, not assumed.** A commit that only an
    open draft PR references can become unreachable if that PR is force-pushed or closed
    unmerged, and GitHub may then collect it.
    - A scheduled workflow (weekly, on the hosted runners) and every PR that touches
      `agent-durable/uv.lock` run `uv lock --check` against
      `temporalio/ai-integrations`. A failure opens an issue.
    - Images already built keep the installed package, so a vanished commit stops
      rebuilds, never a running deployment.
    - The remedy is a reviewed bump to the PR's new head, or to the PyPI release.
    - Vendoring was ruled out by the user (§9).
  - The Dockerfile asserts the Claude Code version the Python `claude-agent-sdk` bundles,
    as it does for the TypeScript SDK (`agent/src/check-cli-version.ts`).
  - The two are bumped together.
- **`SessionStore`** is implemented on Postgres, following the SDK's Postgres example.
  If its shape is `ai_session_entries`' (which already mirrors the SDK transcript), it
  uses that table; otherwise it adds `ai_durable_entries`. Every worker shares one `cwd`
  (`/srv/agent`), as the store keys sessions by it.
- **The credential** is read from `ai_credentials` and decrypted in Python with a port of
  `openSecret` (`agent/src/secrets.ts:187`):
  - There may be several rows (#1093). A query takes them in the pool's fallback order
    (`CredentialPool`, `agent/src/harness/fallback.ts`): by `priority`, skipping any
    that are cooling down, disabled, or not openable with the mounted key.
  - AES-256-GCM, sealed format `version | IV(12) | tag(16) | ciphertext`;
  - AAD `v2|ai_credentials:<row id>:{"kind":…,"base_url":…}` for the secret, as
    `credentialAad` (`agent/src/credentials.ts`) builds it. The row id is `default`
    for the row migrated from the single-credential table. The data key's AAD is
    `dek:` + that;
  - KEK id = the first 16 hex characters of the key's SHA-256.

  **Test vectors, from one source of truth.** `agent/test/fixtures/secret-vectors.json`
  holds one envelope per credential kind (`anthropic_api_key`, `claude_oauth_token`,
  `gateway`) and per
  version. A TypeScript script writes it with `seal` under a fixed KEK, data key and IV.
  - The agent's test suite regenerates it and fails if the committed file differs. So a
    change to `secrets.ts`'s format or AAD cannot land without new vectors.
  - The Python suite opens every vector in the file. So new vectors cannot land without
    the port opening them.
  - The `agent-durable` CI job runs whenever `agent/src/secrets.ts`,
    `agent/src/credentials.ts` or the vectors change.

  The result goes to the runner's `env` as `ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN` for a `claude_oauth_token`, or `ANTHROPIC_BASE_URL` plus
  `ANTHROPIC_AUTH_TOKEN` for a gateway (`credentialEnv`,
  `agent/src/harness/run.ts:185`). It never enters history.
- **Limits.** `max_turns` and `budget_usd` are the session row's. Each segment gets the
  remaining budget as `max_budget_usd`, and the row's `cost_usd` and `turns` are updated
  by an activity after each segment.
- **Events.** The durable worker runs one `follow_agent` subscriber per running session,
  translating `text` / `tool_call` / `approval_needed` / `tool_result` / `done` / `error`
  into the existing `ai_session_events` vocabulary. So the chat socket and the panel
  render a durable session unchanged. That is the README's one-subscriber-in-the-backend
  advice.

### 6.3 Tools as activities

- Every `/api/v1` operation already has a tool, or a `src/tools/coverage.ts` entry
  (`agent/test/coverage.test.ts`). The registry `ALL_TOOLS` (`agent/src/tools/index.ts:26`)
  is therefore the set of activities.
- **The TypeScript agent service** runs a `@temporalio/worker` on `agent-tools`, with one
  activity per tool, registered under the tool's name. Each runs
  `runToolWithOutcome(tool, args, ctx)` (`agent/src/tools/registry.ts:267`), the entry
  point `/mcp` uses, so parsing, tiers, scope and audit are unchanged. The `ctx`
  principal is the session's owner, and `ctx.session` is the session's id, exactly as
  `createHarnessServer` sets it for a classic session (`agent/src/tools/harness.ts`).
  Revision commits take their session trailer from it (#252), and the record of what a
  session touched (#931) is written from it, so an activity without it attributes nothing.
- **A build step** exports `ALL_TOOLS` as `[{name, description, input_schema, tier,
  hitl}]` JSON, generated like `gen:api`. `hitl` is the tool's HITL kind (§6.6):
  `approval` for every outward tool, `answer` for `ask_user` and `wait_for_user`, and
  absent otherwise. The Python worker declares each one as
  `activity_as_tool(activity.defn(name=<name>)(_remote), description=…, input_schema=…,
  needs_approval=(hitl is not None), task_queue="agent-tools")`, so an `answer` tool
  parks at the gate like an outward one. `_remote` is never run:
  the plugin needs an `@activity.defn` callable to name the activity (§3.2), and the
  TypeScript worker serves that name.
- **`browser_*` tools** need a paired tab. With none, they fail at once with that
  message rather than wait.

### 6.3a The `agent-durable` container

It is trusted like the `agent` sidecar, including the git-pinned plugin package, and is
configured the same way.

- **Image.** The Dockerfile stage `agent-durable` runs as `USER 10001:10001`, like the
  `agent` stage (`Dockerfile:271`), with the same pod security settings as the `agent`
  container.
- **Configuration.** It mounts the `agent` container's Secret and reads the same
  infrastructure variables: `SCADBUDDY_DATABASE_URL`, `SCADBUDDY_SECRET_KEY_FILE` and
  `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` (rotation), plus the Temporal address. It holds
  no Bambuddy key.
- **Network.** No policy of its own: it is in the ScadBuddy pod, and the existing
  ingress policy (`applications/scadbuddy/networkpolicy-agent.yaml`) covers only the
  `agent` port. It opens no port besides its health check.
- **The credential** is decrypted per segment, passed to the runner's `env`, and never
  logged, written to disk or put in history (§6.2).

### 6.3b Plugins, skills and settings in a durable session

A classic query loads ScadBuddy's own plugin (`agent/src/harness/ownPlugin.ts`: the
`skills` and `agents` of `plugins/scadbuddy/`, with the `Skill` and `Agent` built-ins and
`settingSources: []`, `agent/src/harness/options.ts:60`). It also loads whatever
`ai_plugins` and `ai_plugin_packages` are approved, vetted by `harness/plugins.ts`, which
refuses anything that starts a process because it would inherit the credential env.

A durable session in phase 5 gets:

- **No filesystem settings.** The runner's `extra_options` carries
  `setting_sources=[]`, as `options.ts` does. Nothing in `/srv/agent` or the image is
  read as Claude Code configuration.
- **ScadBuddy's own skills only.** The image copies `plugins/scadbuddy/skills` into the
  `agent-durable` stage, as the `agent-build` stage does for the classic harness. They
  are loaded through the runner, with `builtin_tools=["Skill"]`.
  - Skills are instructions: no hooks, no MCP servers, no LSP, no processes.
  - The plugin's `.mcp.json` is for installs outside ScadBuddy and is not copied, as
    in the classic harness.
- **No subagents.** The plugin cannot pause a durable tool call made from a subagent and
  fails closed (§3.2). So `Agent` is not enabled, and `plugins/scadbuddy/agents` is not
  loaded. A durable session that needs a subagent's role asks for a new session with
  `agent(...)` in a flow (§7).
- **No `ai_plugins` and no `ai_plugin_packages`.** Giving them to durable sessions needs
  a Python equivalent of `harness/plugins.ts` and `plugins/packages/vet.ts`, with the
  same process-spawning refusal and the forwarder for remote MCP. That is its own later
  issue. Until then the durable mode's description in the mode picker says that
  plugins are not available in it.

### 6.4 Human-in-the-loop

Every point where a session waits on a person is a tool call parked at the session's
tool-call gate. §6.6 defines that gate once, for both modes: the pending list is the
`pending_input` Query, the person answers with the `respond` Update, and a timer never
answers for them. For an outward call in a durable session:

- The call waits in the workflow (`needs_approval`) until a decision.
- The panel's approve/deny actions send `respond` (§6.6). It calls the plugin's
  `agent.decide(tool_use_id, approved, approver)`, and its validator includes the
  plugin's `validate_decision`.
- `approval_expiry_seconds` (`approvals/service.ts:115`) is the call's timer, and it
  denies (§6.6, "Timeouts").
- Classic sessions keep `ai_approvals`, read and answered through the same shape
  (§6.6, "Classic sessions").

### 6.5 Deleting a durable session or a flow run

Today a session's content lives only in our Postgres tables (`ai_session_entries`,
`ai_session_events`). Removing those rows removes it: the SDK's own
`SessionStore.delete` (`agent/src/sessions/store.ts:99`) does, and so can an operator.
There is no user-facing delete route today, and this spec adds none. A durable
session's prompts, tool calls and answers also reach Temporal: in its history, in Visibility, and past 168h in the Archival bucket.
Deleting our rows alone would no longer make them unrecoverable. Temporal's documented
mechanism for this is a **Payload Codec**, and with it deletion is crypto-shredding:

- **The codec.** `SubjectPayloadCodec`, implementing the SDK's
  `WithSerializationContext`, which gives the codec each payload's `workflow_id`
  (`temporalio.converter.WorkflowSerializationContext` /
  `ActivitySerializationContext`, `temporalio` 1.33).
  - It encrypts every payload of a workflow whose ID is `session-<id>` or `flow-<id>`.
    It uses AES-256-GCM, the same sealed format as `agent/src/secrets.ts`, under that
    subject's own data key.
  - The data key is a row of `ai_payload_keys(subject, dek_sealed, kek_id, created_at)`,
    sealed under the KEK and re-wrapped on rotation like `ai_credentials`.
  - Other workflows' payloads (renders, prints, commands) pass through unencrypted. A
    flow's child `PrintRun` has its own workflow ID and carries no conversation.
- **Who runs it.** Every client and worker that encodes or decodes those payloads:
  - the `agent` worker in `agent-durable`, and the `projects` worker;
  - the agent service's TypeScript worker on `agent-tools`, since its activities receive
    a session's tool arguments;
  - the clients that start sessions and flow runs or send them Updates.

  Each of them reads the KEK. Phase 4 starts by confirming that the TypeScript SDK
  exposes the same serialization context to a codec. If it does not, the work stops
  and the user decides (§9).
- **Deleting.** One operation, `forgetSubject(subject)` in the agent service, is what
  every deletion of a durable session or a flow run goes through. Its callers are the
  `SessionStore.delete` path, an operator, and any delete route added later. It does
  three things:
  1. delete the `ai_payload_keys` row, after which every copy of the payloads (history,
     Visibility memo, Archival) is undecryptable;
  2. terminate the workflow if it is open, then `DeleteWorkflowExecution`;
  3. delete our rows, as today, including the session's `ai_pending_input` and
     `ai_input_responses` rows (§6.6), which hold questions and answers in plaintext.

  Postgres backups hold the key row for their 7-day window, the same window in which
  they already hold `ai_session_entries` today. So the guarantee is the one deletion
  gives now.
- **Temporal UI** shows these workflows' payloads as ciphertext. No Codec Server is
  deployed, by design: reading a session's content goes through ScadBuddy's own access
  checks, not through the Temporal UI link (#668).

### 6.6 Waiting on a person: the tool-call gate

This follows Temporal's
[human-in-the-loop cookbook](https://docs.temporal.io/ai/cookbook/human-in-the-loop-python):
the workflow keeps its pending requests in its own state, waits with
`workflow.wait_condition(..., timeout=...)`, and receives the person's response. Nearly
every wait a session can hit is already a tool call, so the gate is where the waiting
happens, and there is no separate request system.

- **Any tool call may park for a person.** A tool's *HITL policy* says whether its call
  parks, who may answer, what the answer looks like, and what the timer does. The policy
  extends the spec's `ToolApprovalPolicy` (§3.2, §7.2) and is declared with the tool.
  Its `kind` is one of:
  - **`approval`**: the response is the call's *decision*. Approve runs the call with
    the input that was shown, and deny returns the plugin's rejection to the model.
  - **`answer`**: the response is the tool's *result*. The tool exists to ask, and
    running it has no effect of its own (`ask_user`, `wait_for_user`).
- **What parks today and soon.** Each row is a tool call unless it says otherwise.

| Wait | Tool call? | Kind | Who may answer | Response | Timer | Today |
|---|---|---|---|---|---|---|
| Outward tool call (`harness/permissions.ts:7-20`, `ApprovalService.gate`) | yes | `approval` | the browser user, or a principal with the approval grant, never on its own call (`approvals/service.ts:73-85`) | `{decision: approve\|deny, input_hash?}` | deny at `approval_expiry_seconds`, default 600 s (`approvals/service.ts:96-101,115`) | classic: `ai_approvals`; durable: §6.4 |
| Off-origin browser navigation (`harness/browserOrigins.ts:21-27`) | yes | `approval` (the input guard makes the call outward) | as above | as above | as above | classic only |
| Headless-browser request grant, `authorize_request` (`harness/headlessGrants.ts:12-31`) | yes | `approval` (an outward tool) | as above | as above | as above; the grant it writes then lasts `GRANT_TTL_SECONDS` = 120 s | classic only |
| MCP prepare/confirm (`approvals/mcp.ts`, `tools/approvals.ts`) | yes, with no session | `approval` | as above; never the MCP principal that prepared it | as above | as above | classic only, and it stays there (no session) |
| Structured question, `AskUserQuestion` (PR #998, `ai_questions`, #940) | yes | `answer` | the browser user, owner of the session (#998) | `{answers: {<question>: string \| string[]}}`, the built-in tool's `answers` | **cancel** at `question_expiry_seconds`; #998 adds no expiry and cancels only with the turn (`questions/service.ts:27-33`) | classic only (`ai_questions`, merged in #998) |
| Attention request, `request_user_attention` / `wait_for_user` (#815) | yes | `answer` | the browser user, owner; for `tab_disconnected`, also the system on re-pair | `{choice?: <one of options>, text?}`, or `reconnected` | the call's own `timeout_s` (default 300 s), then `on_timeout`: see "Timeouts" | not built (#993 built only the badge) |
| Flow `wait_for_human(question, timeout=…)` (§7.1) | yes, a Code Mode host call | `answer` | the browser user, on the Workflows page (§7.3) | `{answer: string}` | the script's `timeout`, then the host call raises; it never returns an answer | spec only |
| Budget exhausted, `raiseBudget` (`SessionManager.raiseBudget`, #790, #823) | **no** | (none) | the browser user, owner | `{add_usd}` | none | both modes, unchanged |
| Handoff offer (`SessionManager.handoff`, TTL `HANDOFF_OFFER_TTL_MS` 1 h) | **no** | (none) | the principal it is offered to | accept or decline | the offer lapses | classic; durable unchanged |

- **What is not a session wait.** Approving a plugin package (`routes/pluginPackages.ts:19`,
  `plugins/packages/store.ts:238`) is an admin's review in Settings. No tool installs
  or approves one, and no session parks on it. The credential "test connection"
  (`harness/testConnection.ts:31`) is a one-turn query under its own 60 s timeout, with
  nobody to wait on. Both stay as they are.

#### Reads: the `pending_input` Query

- One Query, `pending_input`, returns a durable session's parked calls. It replaces
  per-kind Queries such as the plugin's `pending_approvals`. It is read-only and adds
  nothing to history.
- Each entry is
  `{id, kind, tool, summary, input_hash, prompt, requested_by, responders, created_at, expires_at}`:
  - `id` is the `request_id` that `respond` takes. It is opaque to clients, and its
    prefix names its store: `approval:<uuid>` or `question:<uuid>`, the row id, for a
    classic entry (an `ai_approvals` or `ai_questions` row, whose `tool_use_id`
    is not unique, since cancelled, expired and re-asked rows are kept), and
    `durable:<session id>:<workflow run id>:<tool_use_id>` for a durable session's,
    where the plugin runs each `tool_use_id` once per run (§3.2). The run id makes a
    Reset's replayed park a new entry, never a collision with the pre-Reset outcome. A
    parked call never spans a continue-as-new, which happens only between tool calls
    (§3.2). The activity on `agent-tools` derives the same id from its activity info
    (`tool-<tool_use_id>`, and the run id). A flow run's is
    `flow:<run id>:<workflow run id>:<call id>`, where `<run id>` is the `workflow_runs`
    id, which a Reset keeps (§7.4), and `<call id>` is the harness's id for the host
    call, which a replay repeats. The Temporal `workflow_run_id`, which a Reset changes,
    is what keeps a replayed park from answering to a pre-Reset card;
  - `kind` and `responders` come from the tool's HITL policy;
  - for an `approval`, the entry carries the scrubbed `summary` and the `input_hash`
    and **never the call's raw input**, as the approval reads already do
    (`routes/approvals.ts:20-22`); `prompt` is empty;
  - for an `answer`, `prompt` is taken from the tool's input **on purpose**: the
    question and its options, or the attention message, are what the person must read.
    It is scrubbed of secrets as `QuestionService` does for a classic question, and
    capped at 16 KiB, the same as a response; a longer one is refused to the model
    as malformed, without parking.
- In a durable session it is built from the plugin's `pending_approvals()` (`id`,
  `name`, `input`), joined with the entry's policy and times, which the workflow keeps
  in its own state when the call parks. The plugin runs one durable call at a time
  (§3.2), so a durable session has at most one entry. A flow run can hold several: its
  host calls may run in parallel (`asyncio.gather`, §3.2), and each parked one is an
  entry.
- **Who answers a flow's entries.** A flow run has no session and no owner. Its
  `wait_for_human` is answered by the browser user only, as every `answer` kind is. Its
  outward `tool(...)` approvals are decided by the browser user only, on the Workflows
  page: flow entries have no route or tool a grant holder could reach them through
  (`sessions_approve` / `sessions_deny` take session ids). The shared vectors include
  both.
- **Flow entries stay out of the assistant's reads.** They are listed and answered on
  the Workflows page only (§7.2, §7.3), through `ProjectWorkflow`'s own `pending_input`
  and `respond`. They get no `ai_pending_input` row, so `GET /api/v1/ai/pending-input`
  and the badge never count them. An `agent(...)` step's entries are its own durable
  session's, and do appear there (§7.2).
- **How a durable entry opens and closes.** Two activities on the `agent` queue, run by
  `DurableSession`, own every write about an entry:
  - `open_input`: `DurableSession` waits with `workflow.wait_condition` for
    `agent.pending_approvals()` to gain a call, using only the plugin's public API. It
    then runs `open_input`, which inserts the `ai_pending_input` row and appends
    `input.requested` to `ai_session_events` in one transaction, and starts the entry's
    timer.
  - `resolve_input`: every resolution, by `respond` or by the timer and of either kind,
    runs it *before* `agent.decide`. In one transaction it takes the row with the
    guarded `DELETE … RETURNING` described below (and, finding it gone, does nothing
    but tell the plugin), and it writes the outcome to
    `ai_input_responses` (every kind: `approved`, `denied`, `expired`, `answered`,
    `cancelled`, `timed_out`), deletes the `ai_pending_input` row, appends
    `input.resolved`, and, for an `approval`, writes the `ai_audit` row that
    `approvals/service.ts` writes for a classic decision or expiry (`audit/log.ts:276`).
    So no resolution reaches the plugin without the projection, the event and the audit
    recording it, and a durable expiry is recorded as `expired` there.
  - **The approved call's own audit row.** Today `ai_audit`'s `tool_call` row names its
    approver by sub-selecting `ai_approvals` with the approval id, and only when that id
    is a UUID (`audit/log.ts:270-289`). A durable approval has no `ai_approvals` row and
    a non-UUID id, so that lookup finds nobody. For a durable call, the `agent-tools`
    activity passes its `request_id`, and the audit insert copies `approved_by_*` from
    `ai_input_responses` (`outcome = 'approved'`, its `responder`). `ai_audit.approval_id`
    is a `uuid`, so phase 5's migration adds `ai_audit.request_id text`, written on both
    the `kind = 'approval'` row and the `tool_call` row, which is how an auditor joins
    them. §8 asserts that the `tool_call` row of an approved durable call names its
    approver and joins to its decision on `request_id`.
  - **Ending without a decision**, as classic `cancelPending` does
    (`approvals/service.ts:87-95`). An interrupt, a handoff and a new turn that
    supersedes the call each first send `DurableSession` a `cancel_input(reason)`
    Update, from the agent service's interrupt, handoff and send paths, and proceed
    once it returns. `cancel_input` resolves the parked entry as `cancelled` through
    `resolve_input`, then lets the call end: an `approval` with `agent.decide(…, False,
    "system:cancel")`, so it never runs, and an `answer` let through with the
    `cancelled` outcome. So an interrupted turn leaves no entry, and a new owner after a
    handoff never inherits an approval asked for in the previous owner's turn. The
    attention `stop` path (Timeouts) is an interrupt and goes through it too.
    The Update needs the `agent-durable` worker, so each caller waits at most 10 s,
    then:
    - **interrupt** proceeds anyway. It also sends `DurableSession` an `interrupt`
      **Signal**, which Temporal records in history even with no worker running (an
      Update is not recorded until a worker accepts it). When the worker returns, the
      Signal's handler cancels the turn's `agent.run` task. The handler can only
      cancel: like `cancel_input` it resolves the entry as `cancelled` and calls
      `agent.decide(…, False, "system:cancel")`, and it takes no decision from its
      caller. Stop is what a person presses when a session is stuck, so it must not
      hang. The entry is not left to its timer: whenever the turn's `agent.run` is
      cancelled, for any cause (an interrupt after this timeout, the attention `stop`),
      `DurableSession` first resolves any parked entry as `cancelled` through
      `resolve_input`. So a cancelled turn never leaves an entry listed, and no
      `respond` reaches a call the plugin no longer holds;
    - **handoff** is refused with a retryable error ("the session's worker is not
      answering; try again"), so a new owner never inherits a parked approval;
    - **a superseding send** is refused the same way, so the send can be retried once
      the worker answers.
  - **When the workflow ends some other way** (an operator's terminate, a Reset,
    `forgetSubject`), nothing in it runs. Two things clean up:
    - `forgetSubject` deletes the subject's `ai_pending_input` and `ai_input_responses`
      rows with its other rows (§6.5 step 3);
    - every entry has an `expires_at` of at most 86 400 s, which bounds how long it
      waits while its worker runs. While the `agent-durable` worker is down, an entry
      can outlive `expires_at` until the worker returns and resolves it. The aggregate
      read marks such rows `expiring` rather than pending, and the badge does not count
      them. Separately, the agent service's sweep that runs `expireDue()`
      every 30 s (`APPROVAL_SWEEP_MS`, `main.ts:49`) checks `ai_pending_input` rows
      older than 10 minutes against Temporal, so that a closed run's entry is removed
      promptly rather than at its expiry. It calls `DescribeWorkflowExecution` on the
      row's `workflow_id` and `workflow_run_id` columns. The work is bounded: it
      describes each distinct run at most once per tick, and a run found open is not
      described again for 10 minutes (`last_checked_at` on the row). So the load is
      one Describe per open durable run per 10 minutes, not one per badge poll. Only a run that is closed or
      not found is an orphan, so a terminated run's entry leaves the badge within one
      sweep, not at its expiry. A row whose run is still open is left alone, because
      it is a live entry, perhaps one whose `resolve_input` is still retrying (the
      worker or Postgres is down).
    - Both writers remove the row through one guarded `DELETE … WHERE request_id = $1
      RETURNING`. Whoever deletes it writes the outcome, the event and the audit, and
      the loser writes nothing. So a request never gets two `input.resolved` events.
    - A Reset starts a new run. `open_input` on the new run first deletes, in its own
      transaction, the session's rows that carry another `workflow_run_id` (each with
      `input.resolved`, outcome `cancelled`, reason `reset`), then opens the replayed
      park under the new run id. So after a Reset the aggregate read shows exactly one
      entry for the call. `open_input`
      is still an upsert on `request_id`, so a retried activity adds no second row.
  - `ai_pending_input(request_id primary key, session_id, workflow_id, workflow_run_id,
    kind, tool, summary, input_hash, prompt, requested_by, responders, created_at,
    expires_at, last_checked_at)` and `ai_input_responses(request_id primary key, session_id, kind,
    outcome, response jsonb, responder, created_at)` are one new agent migration in
    phase 5, and the `agent-durable` container writes both with the agent's database URL
    (§6.3a). The workflow pair is the
    one `workflow_runs` records too. The sweep reads it, and nothing but the route's
    prefix dispatch parses `request_id`.
- **Two reads, two sources.**
  - `GET /api/v1/ai/sessions/{id}/pending-input` answers for one session. For a durable
    one it sends the Query, which is the source of truth. It writes nothing. The
    projection below is written by `open_input` and `resolve_input`, each in one
    transaction with its event and before the plugin is told, and swept for orphans as
    described above.
  - `GET /api/v1/ai/pending-input` answers for everything the principal may see, the
    badge's read. It is **one Postgres read of a projection**, `ai_pending_input`,
    written by `open_input` and `resolve_input` (above), as `render_jobs` is projected
    by its workflow's activities. It never fans a Query out per workflow: that would
    cost one worker round-trip per open session per poll, and every poll would time
    out while the `agent-durable` worker is down. It is the union of that table
    (durable entries) and the classic stores' pending rows.
  - The aggregate includes **session-less entries**: an MCP prepare (`mcp.ts:97`,
    `session_id` null, `tool_use_id` `mcp:<uuid>`) that `GET /api/v1/ai/approvals`
    lists today, so the badge keeps counting them.
  - Both are reads (§4.1). Each gets a `read`-tier tool. `agent/test/coverage.test.ts`
    checks only `backend/openapi.json`'s operations, not the agent's own
    `/api/v1/ai/*` routes, so §8 adds a check for these three routes. The `POST`
    route has no tool of its own, and the check accepts it only through an entry in
    the same exemption list `src/tools/coverage.ts` keeps, with its reason recorded:
    "browser-only for `answer` kinds; `approval` through `sessions_approve` /
    `sessions_deny`".
  - `respond` gets **no tool** for the `answer` kinds: only the browser user answers
    them. For `approval`, the existing `outward` tools `sessions_approve` /
    `sessions_deny` (`tools/sessions.ts:30`) stay its tool, for a principal holding
    the approval grant, as today.

#### Responses: the `respond` Update

- `respond(request_id, response, responder, role)` answers one entry; `role` is
  defined under "Authorization" below. The route is
  `POST /api/v1/ai/pending-input/{request_id}`, for a session's entry and a session-less
  one alike; the route dispatches on the id's prefix (`approval:`, `question:`,
  `durable:`, `flow:`) to its store or its workflow, and refuses an unknown prefix, or
  a classic id with no row, as stale. In the HTTP
  request the principal is the authenticated caller, never a field of the body. The
  route then passes it to the Update as `responder`.
- **The route is the Update's only legitimate caller, and the Update cannot tell.** A
  Temporal client is trusted with whatever it puts in an Update's arguments, so a client
  that reaches the Temporal frontend can send `respond` naming any responder. Today
  nothing narrows who that is: `eh-homelab/clusters` has no ingress policy and no mTLS
  on the `scadbuddy` Temporal frontend (only `networkpolicy-agent.yaml`, which selects
  the agent pod). That client could equally terminate or reset the workflow, so this is
  the frontend's boundary, not the gate's. Phase 5 adds it: a `CiliumNetworkPolicy`
  ingress on the Temporal frontend that admits only the ScadBuddy pod (the backend, the
  agent service and its `agent-durable` sidecar), the ScadBuddy worker Deployments and
  the Temporal UI (#668, an operator surface), each by its pod label in the
  `scadbuddy` namespace. §8 asserts the policy, since a forged responder cannot be
  refused at the Update layer.
  - **Residual risk.** The worker Deployments include the render worker, which runs
    `openscad` on user-supplied templates. It must reach the frontend to poll its
    queue, and a network policy cannot let a client poll but not send Updates. So a
    compromised render worker could forge a `respond`. Closing that needs per-client
    authorization at Temporal itself (a frontend authorizer, or mTLS identities per
    worker with a claim mapper that refuses `respond` from any identity but the agent
    service's). That is a phase-5 follow-up, recorded in §9, not this spec's design.
- **The validator** refuses, before anything is written to history:
  - a stale id, meaning no such call is parked;
  - an entry that is already resolved (decided, answered, timed out or cancelled);
  - a responder the policy does not allow. For an `approval` that includes a principal
    deciding its own call (`approvals/service.ts:74-81`). That rule is the `approval`
    kind's only. An `answer` is answered by the browser user alone, who is the
    session's owner whenever one parks (below), even though the call was made in their
    own session;
  - a response that does not match the kind's shape, and any response over 16 KiB.
  For an `approval` it also runs the plugin's `validate_decision`, and it checks
  `input_hash` when one is sent, as `ApprovalService.decide` does.
- **Authorization is the route's; the validator checks what the workflow knows.** An
  Update validator must be deterministic and can read neither Postgres nor a grant
  that changes. So the split is:
  - the **route** decides who may answer, exactly as `authorize` does today
    (`ApprovalService.authorize`): the session's current owner from `ai_sessions`, and
    whether the principal holds the approval grant (`grants(principal)`). It passes
    the result to the Update as `responder` plus `role`, the outcome of `authorize`:
    `browser` (the browser user, who may decide an `approval` whoever owns the session,
    and answers every `answer`), `grant` (a non-browser principal holding the grant),
    or `owner` (a non-browser owner, which no kind accepts). **Ownership wins:** a
    non-browser principal that owns the session gets `owner`, never `grant`, even when
    it holds the grant, so it cannot approve calls in its own session, as classic
    `authorize` refuses (`approvals/service.ts:78-80`). A shared vector covers that
    principal;
  - the **validator** checks only what the workflow holds: staleness, *resolving*,
    the kind's allowed roles, self-decision against the `requested_by` and session
    starter it recorded at `open_input`, and the shape.
  A handoff changes the owner in `ai_sessions`, which the route reads, and
  `cancel_input` has already cleared the entry. So the workflow never needs to be told
  the new owner. The shared vectors split the same way: grant and ownership vectors
  run against the route in both modes, and the rest against both validators. The agent's
  `approvers` list is left unset: who may answer depends on the kind, which the
  plugin's single list cannot say, so our validator decides. With `approvers` unset the
  plugin accepts any approver name (`_workflow.py:360`), so the guard is structural:
  `DurableSession` registers **no decision Signal**, and the only handlers that reach
  `agent.decide` are `respond`, `cancel_input` and the `interrupt` Signal (both of
  which can only cancel) and the entry's timer. A test in §8 reads the running workflow's handlers rather than our source. It
  allows the plugin's own, such as the Workflow Streams poll Update that
  `live_output=True` registers, by name, and fails on any other, so a handler the
  plugin or harness adds later is caught too. Phase 5 checks whether the plugin registers any.
- **Why an Update, not the cookbook's Signal.** A Signal cannot refuse. The plugin's
  `decide` logs an invalid decision and drops it, so the person who clicked would see
  nothing happen. An Update's validator rejects synchronously: the panel gets the reason
  ("already answered", "not yours to answer") at once, and a rejected Update is never
  written to history. The cookbook allows either; the plugin documents both.
- **How each kind resolves.**
  - `approval`: the handler is async. It marks the entry *resolving*, runs
    `resolve_input` (no answer to write, only the outcome, the projection, the event
    and the audit), then calls `agent.decide(tool_use_id, approved, responder)`.
  - `answer`: the handler is async. It marks the entry *resolving*, runs
    `resolve_input` with the response (the same activity as for an approval, so the
    response, the projection delete, the event and the outcome are one transaction),
    then calls `agent.decide(tool_use_id, True, responder)`. The tool's activity on `agent-tools`
    reads that row by `request_id` and returns it as the result. The plugin's gate
    carries only a yes or no, so this is how an answer becomes the result with only its
    documented API. The table is the durable store for every `answer` kind (questions
    and attention requests); it does not depend on PR #998's `ai_questions`, which stays
    the classic store. Phase 5 checks this at its start; anything that needs the
    plugin's private state goes to the user (§9).
  - Whichever comes first wins, for both kinds. The handler marks the entry
    *resolving* in workflow state before its first `await`: the validator refuses another response, and a timer
    that fires meanwhile waits for the handler rather than acting.
    - If `resolve_input` succeeds, the entry is resolved and the plugin is told.
    - **If `resolve_input` fails** (its retries are exhausted, or it fails
      non-retryably), the handler puts the entry back to pending and re-raises, so the
      Update fails and the person sees the error and can answer again. If the timer
      came due meanwhile, it fires at once. So a failed write never leaves the call
      parked with no timer and no way to answer.
    - On the `respond` path, `resolve_input` has a bounded retry policy
      (`schedule_to_close_timeout` 30 s), because a person is waiting on the Update's
      answer.
    - On the **timer** path nobody is waiting, so `resolve_input` retries with no
      deadline (backoff capped at 5 minutes) until Postgres takes the write. Meanwhile
      the entry stays *resolving*: it is still listed, `respond` is refused as "being
      resolved", and the call stays parked. That is the right state while the outcome
      cannot be recorded. `agent.decide` runs only after the write succeeds, so the
      tool's activity always finds its row. If the row is missing anyway (an operator
      deleted it), the activity returns the `cancelled` outcome, never an answer.
    - The plugin also keeps only the first decision.

#### Timeouts

- Each entry has its timer, started when the call parks, at `expires_at`. It is the
  wait's own timer, as in the cookbook. **A timer never approves and never answers on
  the person's behalf.**

| Kind | When the timer fires |
|---|---|
| `approval` | **Deny.** The call does not run. A classic session tells the model nobody decided (`approvals/service.ts:341`). A durable one calls `agent.decide(tool_use_id, False, "system:timeout")`. The plugin's `decide` takes no message, so the model reads its fixed rejection ("A human reviewer rejected this action. Do not retry it.", `_workflow.py:718-723`), not the classic *expired* wording. This is a known difference, listed in §9. It fails closed, since the call never runs, and the panel and audit still record `expired`. Letting the call through instead, as an `answer` does, is not possible: an approved `approval` runs the real tool. |
| `answer`, a question | **Cancel.** The tool returns an error result, "nobody answered", which is never an answer. The window is a new `ai_settings` key, `question_expiry_seconds` (default 3600, bounded like `approval_expiry_seconds`, 10 to 86 400). |
| `answer`, an attention request | **#815's rule, as written:** `on_timeout` is `proceed` (the default), `wait` or `stop`. `proceed` returns `timed_out` and the agent carries on with non-outward work only. Any outward call it then makes parks for its own approval, so `proceed` never lets anything outward run. `stop` ends the turn: in a classic session as `interrupt` does; in a durable one by cancelling the turn's `agent.run` task, which the plugin turns into "This tool call was interrupted" for the open call (`_workflow.py:634-651`). Phase 5 verifies that the plugin ends the turn cleanly under that cancellation. Until it does, a durable `wait_for_user`'s input schema offers only `proceed`, so `stop` fails closed by not being offered. `wait` keeps it parked to the 86 400 s ceiling, then does what `stop` does. |
| `answer`, flow `wait_for_human` | The host call raises in the script, which decides what to do. It never returns an answer. `timeout` defaults to 3600 s and is bounded to 10–86 400 s like `question_expiry_seconds`. A literal out of bounds is refused at type check (§7.2); since only a literal can be checked statically, the host call also enforces the bound at run time and raises `ValueError` in the script for a value outside it, never clamping. |

- `proceed` exists only for attention requests. An approval or a question can never
  proceed. #815's `approval_pending` reason is refused as malformed: the approval is
  already its own entry, with its own notification.
- In a durable session the timer is a workflow timer, and in a classic one the existing
  expiry sweep (`ApprovalService.expireDue`) or a new sweep for
  `ai_questions`, which phase 5 adds: #998 has no expiry of its own. A timer that fires goes
  through the same resolution as `respond`, recorded as `expired`, `cancelled` or
  `timed_out`, with the system as the responder. For an `answer` kind that means the
  outcome is written and the call is let through, and the tool's activity returns the
  outcome as an error result (cancelled) or as `timed_out` (attention). It is never
  `decide(False)`, whose text tells the model a reviewer rejected something.

#### Notifications

- Notifications (#815 §3) are driven by an entry being created, the same way for every
  kind and both modes:
  - in-app: the badge and the panel;
  - then OS notifications and webhooks as #815 adds them.
- The trigger in a durable session is `open_input`'s `input.requested` event, not the
  plugin's `approval_needed` stream event, so a notification and the projection come
  from one write. In a classic one it is the gate's insert. Both modes emit one
  `input.requested` and one `input.resolved` event to the bus, with the entry.
- The panel's cards keep their events. `approval.required` (emitted by `ApprovalService.create`)
  and PR #998's `question.asked` / `question.resolved` are still emitted, beside
  `input.requested` / `input.resolved`, as the approval routes stay beside `respond`.
  - In a durable session, **`open_input` and `resolve_input` write them**, in the same
    transaction as `input.*`: `approval.required` for an `approval`, `question.asked` for
    a question, and the matching resolution on the way out. The `follow_agent`
    subscriber (§6.2) reads only the plugin's stream, never `ai_session_events`, so it
    cannot be the one to translate `input.*`.
  - The subscriber **drops** the plugin's `approval_needed`. So one parked call gives
    exactly one card; §8 asserts it.
  - Moving the cards onto the `input.*` events is a later cleanup, not phase 5's.
- #815's throttling (one open request per session per reason, a per-user rate limit)
  applies to attention requests, which an agent creates at will. Approvals and questions
  are already bounded by the one call parked at a time.
- `tab_disconnected` (#815 §2): the failing `browser_*` call returns its error
  (`bridge/hub.ts:69`) as today. The agent then calls `wait_for_user(reason:
  "tab_disconnected")`, which is the entry. Re-pairing the tab resolves it as
  `reconnected`, a classic-only resolution: the bridge's pairing path resolves the
  `ai_*` row as the system. A durable session has no pairing hook into its workflow:
  `browser_*` tools fail at once without a tab (§6.3), and nothing but `respond` and the
  timer resolves its entry. So there, `tab_disconnected` is answered by the person
  (`respond`, "I'm back") or times out by #815's rule. Adding a re-pair resolution is a
  later change, through the `respond` route with a `system:repair` responder that the
  attention policy allows.

#### Classic sessions

- Classic sessions keep their Postgres stores: `ai_approvals` for `approval`, PR #998's
  `ai_questions` for questions, and a table for attention requests when #815 lands.
  They use the same kinds, entry shape and validators. `pending_input` and `respond`
  have one route each, which reads and writes those tables for a classic session and
  sends the Query or Update for a durable one. So the panel and the badge read one
  shape, whichever mode the session runs in.
  - `fetchPendingApprovals()` (`frontend/src/agent/attention.ts:26`) reads
    `GET /api/v1/ai/pending-input` and counts every kind, session-less MCP approvals
    included. **The badge's meaning changes, by decision:** from "actions waiting for
    your approval" to "things waiting for you". The accessible name becomes
    `Assistant, N waiting for you`, and the title lists the counts by kind ("2
    approvals, 1 question"). Attention requests count, `proceed` ones included: they
    are bounded by #815's throttle (one open per session per reason), and while one is
    open the agent is in fact waiting.
  - The approval routes (`routes/approvals.ts:13-16`) and `sessions_approve` /
    `sessions_deny` stay as aliases of `respond` for the `approval` kind.
- The session's status projection is the same in both modes: `waiting_approval` while an
  `approval` entry is parked, `waiting_input` while an `answer` entry is parked. The
  status bus already maps both to `session.waiting` (`sessions/busEvents.ts:73`).
  `waiting_input` is in the schema (`sessions/protocol.ts:16`), and its only writer is
  the classic question gate (`QuestionService`, from #998).
- **An `answer` tool parks only while the session's owner is the browser user**, in
  both modes. Otherwise it returns an error result at once and parks nothing, as the
  classic gate already does (`questions/service.ts:271-274`), so no entry ever waits
  for a responder who is not allowed. A handoff to another principal mid-wait cancels
  the entry (`cancel_input`). A shared vector covers it.
- `AskUserQuestion` is Claude Code's built-in in a classic session (PR #998). A durable
  session has no built-in tools but `Skill` (§6.3b), so there it is a ScadBuddy tool,
  `ask_user`, with the same input and the same answer.

#### The two waits that are not tool calls

- **Budget raise** (#790; #823 is the fork gap). Kept separate. Nothing parks: the turn
  ends with `budget_exhausted`, and the session waits for its next message as it always
  does. The panel's meter already shows it. A durable segment that ends on the budget
  ends the same way, and `raiseBudget` writes the row in both modes. It is not an entry,
  because no call is waiting, so there is nothing for a timer to time out or for
  `respond` to resume. #823's fix belongs in `sessions_fork`, not here.
- **Handoff offer** (`SessionManager.handoff`). Kept separate. Only another principal
  is offered a session; a handoff to the browser user is immediate. So it is never a
  wait on a person, and its 1 h TTL lapses the offer rather than answering anything.

#### Out of scope

- Questions from subagents (#940's remaining item 1). Durable sessions have no
  subagents (§6.3b). In classic ones, whether a subagent's `AskUserQuestion` reaches
  `canUseTool` is still to be measured in #940.
- Answers from MCP clients or other principals, for `answer` kinds. Only the browser
  user answers them, as in #998 and #940's safety rule.
- The audit row for an answer (#940's remaining item 3).
- OS, Web Push and webhook channels, and the `done` summary (#815 §3-§4). This section
  says only what triggers them.
- Plugin package approval and the credential test, which are not session waits.

## 7. Flows

### 7.1 What a flow is

- A flow is a Python script an agent writes (or anyone writes through the API). It runs
  in Code Mode over these host functions:

| Host function | Is | Answered by |
|---|---|---|
| `render(slug, inputs)` | child `TemplatePipeline` via `RenderService` | the render pipeline |
| `print(source, choices)` | child `PrintRun` (§5) | the print worker |
| `arrange(...)` | child `Arrange` (template spec phase 5, when it lands) | the render worker |
| `tool(name, args)` | activity on `agent-tools` (§6.3) | the tool registry |
| `agent(prompt, skills=…, result_schema=…)` | child `DurableSession` | a new durable session |
| `ask_session(session_id, message)` | `send_message` to that session; waits for the turn's answer | an existing durable session |
| `wait_for_human(question, timeout=…)` | parks at the run's gate as an `answer` entry (§6.6) | a person |
| `sleep(duration)` | a workflow timer | Temporal |

- Multi-plate campaigns are ordinary loops over plates, with `wait_for_human`
  ("swap to the pink spool") between them. Choosing a printer or spools is `tool(...)`
  calls and logic. Pre/post steps and file transforms are `tool(...)` and `agent(...)`
  calls.

### 7.2 Running on the harness

- `ProjectWorkflow` is a temporal-agent-harness agent (`@agent.defn`) with a model-free
  `execute(script)` operation, the shape of `agent_dag`'s `DagBuilderAgent.execute`. It
  runs on queue `projects` in `scadbuddy.worker --queue projects`, which holds nothing
  outward.
- The host functions are harness tools (`@agent.activity_tool_defn`, or workflow
  functions for the child workflows). Approvals use the harness's `ToolApprovalPolicy`:
  an outward `tool(...)` goes to a human through the Workflows page (§7.3), whose
  Approve/Deny is §6.6's `respond`, `ProjectWorkflow`'s own Update, which runs
  `resolve_input` and then makes the harness's decision call inside the workflow. No
  harness or plugin decision Update is exposed to clients. Phase 6 checks how the
  harness's approval API carries a `wait_for_human` answer; anything that would mean
  departing from it goes to the user (§9). A flow has no chat session, so its approvals
  do not appear in the assistant's panel. An `agent(...)` step is a durable session of
  its own, and that session's approvals appear in the assistant's panel (§6.4, §6.6), with
  the Workflows page linking to it. Event
  streams and continue-as-new use the harness defaults.
- Type checking (`code_mode_type_check`) runs when a flow is registered, and again before
  each run. A script that fails is refused with its errors, by line.
- Phase 6 starts by verifying what the harness needs around `ProjectWorkflow` (its
  `SessionManagerWorkflow`, the `code-mode` extra) and how `execute` behaves across a
  Reset (§7.4). Anything that would mean departing from the harness is brought to the
  user (§9).

### 7.3 Records and API

- **Tables** (backend migrations, our system of record):
  - `workflow_definitions(id, name, version, script, created_by, created_at)`; versions
    are immutable.
  - `workflow_runs(id, definition_id, version, status, waiting_on jsonb, steps jsonb,
    workflow_id, workflow_run_id, started_by, created_at, updated_at)`, written by the workflow's
    activities.

  Creates follow §5.1: `POST /runs` is update-with-start, and the first activity
  type-checks, inserts the row and publishes the event. Each run carries §4.2's
  `ScadbuddyKind = flow`, `ScadbuddySubject` (the definition id) and
  `ScadbuddyStatus`, plus `ScadbuddyFlow` (the definition's name).
- **Routes:** `POST /api/v1/workflows`, `GET /api/v1/workflows`,
  `POST /api/v1/workflows/{id}/runs`, `GET /api/v1/workflow-runs/{id}` and
  `POST /api/v1/workflow-runs/{id}/answer`. Each gets a tool, as the coverage test
  requires, so an agent in either mode registers and starts flows, and a durable one gets
  them as activities.
- `ask_session` reaches durable sessions only. A classic session is not a workflow.
- **UI:** Settings → Administration links to the Temporal UI already (#668). A minimal
  Workflows page lists runs with their status, what each waits on, and Approve/Answer.
  It is the one place a flow's own approvals and `wait_for_human` questions are
  answered (§7.2). Both are entries at the run's gate (§6.6): the page reads them with
  `ProjectWorkflow`'s `pending_input` Query and answers with its `respond` Update, the
  same shape, validators and timer rules as a session's, so there is no second copy to
  keep in sync. An
  agent panel links to the runs its session started.

### 7.4 Changing course: Temporal Reset

- There is no in-place revision. When a run goes wrong (a jammed printer on plate 7),
  you reset it to the last good event with Temporal Reset. The run replays to there and
  continues on the current code, with the same inputs.
- A changed script is a new run of a new version, because the script is the run's input.
- **Reset does not undo side effects.**
  - Everything already done in the world stays done: a sliced file, a queued or finished
    print, a committed template, a tool's outward effect.
  - Every host call after the reset point runs again.
  - Before resetting, the read `GET /api/v1/workflow-runs/{id}/reset-preview?event_id=`
    lists the host calls between that event and now that had outward effects. Those are
    `print`, an outward `tool`, and `agent`/`ask_session` turns that made outward calls.
    It is a read (§4.1) and changes nothing.
  - **The list is pinned to the run's history at the moment it is shown.** The preview
    carries `as_of_event_id`, the last event of the history the list was computed from.
    - The command is `POST /api/v1/workflow-runs/{id}/reset {event_id, as_of_event_id}`.
      The route recomputes the list. If the history has moved past `as_of_event_id` and added an outward
      call, it refuses with 409 and the new list, and the person confirms again.
    - The run is not paused while the person decides. A flow waiting on a printer may
      sit for hours, and pausing it would hold up work they did not ask to stop. The
      check at confirm time is what makes the approved list the true one.
  - It has a tool and the audit trail, like every route.
  - Phase 6's plan states what Reset does to child workflows and activities that were
    in flight past the reset point. A child `PrintRun` already past its enqueue keeps its
    own history and outcome, and the replayed flow does not re-attach to it.
- `print_enqueue`'s `maximum_attempts = 1` (§5.3) is set explicitly on that activity and
  asserted by a test, never inherited from a default retry policy.

## 8. Errors and testing

- **The command shape** (phase 1, before any `done` kind exists, against a test-only
  `done` kind):
  - a retry while running attaches;
  - a retry after the execution closed returns the record and does not run the effect
    again (`ALLOW_DUPLICATE_FAILED_ONLY` + record lookup);
  - a retry after a refusal starts again;
  - a retry after a failure that followed the record returns that failure and does not
    run the effect again;
  - the first activity retried after its commit (the worker killed before reporting)
    returns the same row and publishes no second event;
  - for every kind, a generic property test injects an exception at each activity
    boundary after the record write and asserts that the execution *completes* with an
    `unexpected` failed outcome, never *fails*, and that a retry with the same
    `request_id` is answered from the record without a second start;
  - a retry after an operator terminated the execution post-record is answered from the
    record;
  - a retry after the workflow ID has been forgotten (the record exists, the history
    does not) returns the record.
- **Print** (backend, `requires_temporal` and `requires_postgres`):
  - the 422 through update-with-start;
  - a repeat while running, within the window, and after it;
  - both routes (output and `library:<file id>`) on the same workflow;
  - a worker killed mid-slice (resumes) and mid-enqueue (`may_have_queued`);
  - a slice start that fails without a job id completes the run `failed`, and its retry
    with the same `request_id` returns that run;
  - the projection's guarded transitions and the event in the same transaction;
  - the retention setting.

  These use the existing `tests/api/conftest.py` Temporal fixture and a fake Bambuddy
  client.
- **Durable session:**
  - `agent-durable/` tests run the bundled Claude CLI against
    `agent/test/support/fakeAnthropic.ts` as a gateway; tests never call Anthropic;
  - the `agent-tools` activities use `@temporalio/testing`;
  - the shared credential vectors (regenerated and compared in TypeScript, all opened in Python);
  - `respond` approve, deny, and expiry (deny, recorded `expired` in `ai_audit`); a
    question answered, timed out (cancelled, never answered) and answered after the
    timer (refused);
  - `pending_input` returns the same entry shape for a classic and a durable session,
    and an `approval` entry never carries the call's raw input;
  - an `answer` whose write activity fails returns to pending, can be answered again,
    and still times out;
  - from the running workflow's registered handlers (not our source): no handler other
    than `respond`, `cancel_input`, the `interrupt` Signal (cancel-only,
    `decide(False, "system:cancel")`) and the entry's timer reaches `agent.decide`. The
    same test runs against `ProjectWorkflow`, where a plugin- or harness-registered
    decision Update fails it. The
    expected set is every handler `DurableSession` registers, Queries included:
    the Updates `send_message`, `respond` and `cancel_input`, the Signal `interrupt`,
    and the Query `pending_input`, plus what the plugin registers by default, such as the Workflow Streams poll Update that
    `live_output=True` adds (§3.2, §6.2), each listed by name with why it cannot decide.
    A new, unlisted handler fails the test;
  - interrupt, handoff and a superseding send each cancel a parked durable entry
    through `cancel_input`: the row is gone, `input.resolved` and the audit say
    `cancelled`, and an approval asked before a handoff cannot be approved after it;
  - the sweep removes an `ai_pending_input` row whose workflow was terminated, and
    leaves one whose worker was down past `expires_at` + 10 minutes. When the worker
    returns, that entry gets exactly one `input.resolved`;
  - a Reset to before a resolution parks again under a new `request_id` and resolves
    without a key collision, for a durable session and for a flow run, and a `respond`
    carrying the pre-Reset id is refused as stale;
  - `cancel_input` with the `agent-durable` worker down: an interrupt still stops the
    turn, and a handoff is refused with a retryable error;
  - after a Reset the aggregate read shows exactly one entry for the call, and a
    terminated run's entry leaves it within one sweep;
  - a classic id with an unknown prefix, or with no row, is refused as stale;
  - classic question expiry: an expired `ai_questions` row is resolved `cancelled` by
    the new sweep, never answered; the parked `AskUserQuestion` call returns the error
    result; `question_expiry_seconds` is bounded to 10–86 400 like
    `approval_expiry_seconds`;
  - a non-browser owner holding the grant is given role `owner` and cannot approve a
    call in its own session, in both modes;
  - flow `wait_for_human` `timeout` outside 10–86 400 s is refused at type check for a
    literal, and raises at run time for a computed value;
  - an interrupt with the `agent-durable` worker down: when the worker returns, the
    entry is gone with exactly one `input.resolved`;
  - an `answer` tool in a session owned by a non-browser principal returns an error at
    once and parks nothing; the browser user decides an `approval` in a session owned
    by another principal (role `browser`), in both modes;
  - one durable approval yields exactly one `approval.required`;
  - `forgetSubject` removes the subject's `ai_pending_input` and `ai_input_responses`
    rows;
  - the aggregate read includes a session-less MCP approval, and a durable entry
    leaves it when it is answered or times out;
  - every validator refusal (stale id, resolved, wrong responder, self, malformed, over
    16 KiB) is a **shared vector** in `agent/test/fixtures/pending-input-vectors.json`,
    run against the classic TypeScript validators and the durable Python ones, as the
    credential vectors are, so the two cannot drift. The `self` vector is marked
    `kind: approval`, beside a vector in which the owner answers their own session's
    question and is accepted. *Being resolved* is marked
    `durable_only`: classic decisions are one guarded `UPDATE` (`approvals/service.ts`
    `settle`), with no in-between state, and a classic run skips those vectors by that
    flag, never silently;
  - the Temporal frontend's ingress policy admits only the listed workloads, since a
    forged `responder` cannot be refused by the Update itself;
  - flow entries never appear in `GET /api/v1/ai/pending-input`;
  - the timer path's `resolve_input` failing: the entry stays listed and refuses
    `respond`, and is resolved once the write succeeds;
  - a durable `wait_for_user`'s input schema offers only `on_timeout: proceed` until
    phase 5 has verified cancelling the turn;
  - the three `pending-input` routes are checked for their tools, as the coverage test
    does for `backend/openapi.json`;
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

## 9. Deviations from Temporal

None. The ones considered, and how each was resolved:

| Considered | Resolution |
|---|---|
| Our Postgres rows beside Temporal | Kept, and Visibility + Archival added. The user decided ScadBuddy is the system of record. Rows are written only by workflow activities, and creates are update-with-start, so no row exists without its execution. |
| Polling slices on a workflow timer | Replaced by polling inside the activity with heartbeats (§3.1). |
| `resume_from` (reusing earlier runs' results) | Dropped for Temporal Reset (§7.4). |
| Code Mode pieces without a harness agent | Dropped. `ProjectWorkflow` is a harness agent (§7.2). |
| Vendoring the unmerged `temporalio-claude-agent-sdk` | Not vendored, by the user's decision. The residual risk (an unreachable commit) is monitored by a scheduled `uv lock --check` (§6.2). This is not a deviation from Temporal. |
| Encrypting session and flow payloads | Temporal's documented Payload Codec with serialization context (§6.5). Not a deviation. |
| A durable approval that expires, or is cancelled by an interrupt, handoff or superseding send, reaches the model as the plugin's rejection text ("A human reviewer rejected this action. Do not retry it.") | Not a deviation: the plugin's `decide` takes no message. It fails closed, and the panel and audit record `expired` or `cancelled` (§6.6). The cost is the model's reading: after a handoff, "do not retry" steers the new owner's turn away from a legitimate call. Phase 5 asks upstream for a reason on `decide` covering both, and adopts it once released. Until then, an interrupt or handoff that ends the turn anyway (cancelling `agent.run`, as `stop` does) is preferred over `decide(False)` once phase 5 verifies that path. |
| A render worker on the Temporal frontend could send `respond` | Not a deviation; a known residual risk (§6.6). Network policy cannot split polling from Updates. Per-client authorization (a frontend authorizer or per-worker mTLS identities) is a phase-5 follow-up. |
| Tool stubs for TypeScript activities | Not a deviation. The plugin's documented `activity_as_tool` with `task_queue`, and Temporal resolves activities by name (§6.3). |

If implementing any phase turns up a place where following the SDK or a framework is
not possible, the work stops and the user decides.

## 10. Phasing

Each phase is its own implementation plan and ships alone.

1. **The command shape and PrintRun** (§4.2, §5).
   - The shared pieces: the update-with-start helper, the `operations` table and route,
     the Search Attributes, the frontend `command()` and the agent wrapper.
   - `PrintRun` is their first user, for both kinds of run: its activities, the
     `print_runs` migration and retention setting, `accept_run` replaced by
     update-with-start, `scadbuddy.worker --queue`.
   - The clusters manifests: `scadbuddy-print`, Search Attributes, Archival.
   - Fixes the lost run (a print run that dies with the API pod). #742, the library
     route's 202, is already done (#945).
   - As built (#1052, plan `2026-10-02-durable-phase-1-printrun.md`): the `bambuddy`
     worker runs inside the API process, because an output's source reads and records on
     the data volume; the `scadbuddy-print` Deployment and a volume-free source are #1060.
     The `operations` table and route, and the frontend `command()` and agent wrapper
     beyond the print POST's re-send on `command-still-accepting`, move to phase 2. The
     Search Attributes are upserted only with `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES` set,
     until the clusters change registers them.
2. **Renders and Bambuddy commands** (§4.5, §4.3 `bambuddy`, §4.4 `FollowPrint`): renders
   join the shape and `reconcile_once` goes; send, projects, reprint, timelapse pull,
   sidebar and analyzer fixes move to `bambuddy`; the print watcher becomes `FollowPrint`.
   - As built so far (2a, #1053, plan `2026-10-03-durable-phase-2a-operations-bambuddy.md`):
     `operations`, `GET /operations/{id}`, the `Operation` workflow, and send, project
     file, create project, attach project, reprint, timelapse pull and sidebar
     registration as its kinds. Every kind answers `done` (today's body inside the
     deadline, 202 past it); the client's key is the `Idempotency-Key` header; analyzer
     fix apply stays a request (Postgres only, §4.1); output delete's Bambuddy part goes
     with the library commands (phase 3). Renders (2b) and `FollowPrint` (2c) follow.
   - As built (2b, #1053, plan `2026-10-03-durable-phase-2b-renders.md`): renders start
     `render-<render_key>` with update-with-start, and the reconciler is gone. The route
     still makes the revision, schema and parameter checks before Temporal (they need the
     API's git and volume, §4.1 reads). The first step is the local activity
     `render_accept` (the queue check and the insert), and claims are projected by
     `render_claims`, so neither waits behind openscad for an activity slot. A full queue
     stays today's 503. A supersede starts the new render first, then sends `release`.
     A boot pass fails legacy pending rows that no workflow will run.
   - As built (2c, #1053, plan `2026-10-03-durable-phase-2c-follow-print.md`): `FollowPrint`
     (`follow-print-<output id>`, `bambuddy` queue) runs one heartbeating activity,
     `follow_print`, with the watcher's loop, on a queue of its own,
     `<bambuddy queue>-follow` (`follow_queue`): an attempt holds its slot for as long as
     the print moves, so it must never take one a `PrintRun` or an `Operation` is
     waiting for. That worker's slots are explicit (`FOLLOW_SLOTS`, gauge
     `scadbuddy_print_follows_running`). The heartbeat carries when the print last
     moved, so a retried attempt keeps its age. `PrintRun` starts it as an abandoned
     child after `print_succeed`, or pokes the one already running (starting it again if
     it closed in between). The progress route only starts it when none is running,
     never pokes. A poke cancels the attempt (`TRY_CANCEL`) and starts a fresh one that
     reads at once, then backs off as usual. A worker shutdown ends an attempt at once. `bambuddy/watcher.py` and its lock and
     rescan are gone; each boot hands the prints in `print_watches` to `FollowPrint`,
     deleting a row once its follow is running (the migration that drops the table
     removes that pass). An old pod still writes the table, so this upgrade needs a
     `Recreate` rollout: old replicas at 0 before the new one starts.
3. **Library commands** (§4.3 `library`, §4.4 Schedules): the `scadbuddy-library`
   container, every git, file and download command, and the sweeps as Schedules. Done by
   route group, one plan per group if the plan says so.
4. **Tools as activities** (§6.3): the `ALL_TOOLS` export and the `agent-tools` worker in
   the agent service, plus the plugin package install as a command.
5. **Durable session mode** (§6.1, §6.2, §6.4, §6.6): `agent-durable/`, the plugin pin,
   the `SessionStore`, the credential port, the event subscriber, the tool-call gate
   (§6.6), the mode UI and setting. The gate's work, both modes:
   - durable: `pending_input`, `respond`, `cancel_input` and its callers in the
     interrupt, handoff and send paths, the timers, `open_input` / `resolve_input`, the
     orphan sweep,
     the `ai_pending_input` and `ai_input_responses` migration, `ask_user` /
     `wait_for_user` as tools, `open_input` / `resolve_input` emitting
     `approval.required` / `question.asked`, and the subscriber dropping
     `approval_needed`;
   - clusters: the Temporal frontend's ingress `CiliumNetworkPolicy`;
   - classic: the `pending-input` and `respond` routes over `ai_approvals` and
     `ai_questions`, the approval routes and `sessions_approve` / `sessions_deny` as
     aliases, the `question_expiry_seconds` setting and its sweep (over #998's
     merges), and #815's attention tools;
   - frontend: `fetchPendingApprovals()` on the new route and the badge's new wording.
6. **Flows** (§7): the harness verification, `ProjectWorkflow`, host functions, records,
   routes, Reset, and the Workflows page.

Follow-ups outside this spec: moving the plugin pin to PyPI once the package is
published; and `ai_plugins` / `ai_plugin_packages` for durable sessions, behind a Python
port of the plugin vetting (§6.3b).
