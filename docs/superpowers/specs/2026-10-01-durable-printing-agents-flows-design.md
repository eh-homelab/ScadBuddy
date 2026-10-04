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
| print watcher (`bambuddy/watcher.py`: a rescan loop plus a `_follow` task per output) | `FollowPrint`, a workflow per queued print on `bambuddy`, started by the `PrintRun` workflow, after its `print_record` activity, as an abandoned child (§5.3). It polls Bambuddy inside a heartbeating activity until the print ends, then writes the outcome and the event |
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
- **Superseding.** A request naming `supersedes: <job id>` (and `RenderService.cancel`, `render/submit.py:185`) looks
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
  - drops `heartbeat_at`;
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
    as it does `CLAUDE_CODE_VERSION` for the TypeScript SDK.
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
- **A build step** exports `ALL_TOOLS` as `[{name, description, input_schema, tier}]`
  JSON, generated like `gen:api`. The Python worker declares each one as
  `activity_as_tool(activity.defn(name=<name>)(_remote), description=…, input_schema=…,
  needs_approval=(tier == "outward"), task_queue="agent-tools")`. `_remote` is never run:
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

- An outward call waits in the workflow (`needs_approval`) until a decision.
- The panel's existing approve/deny actions, for a durable session, send the workflow's
  `review` Update (`agent.decide(tool_use_id, approved, approver)`, with the
  `validate_decision` validator). The approver is the authenticated principal, and the
  route checks ownership exactly as `approvals/service.ts` `decide` does. The pending
  list comes from the `pending_approvals` Query.
- `approval_expiry_seconds` (`approvals/service.ts:115`) becomes a workflow timer per
  waiting call that decides *deny* when it fires.
- Classic sessions keep `ai_approvals` unchanged.

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
  3. delete our rows, as today.

  Postgres backups hold the key row for their 7-day window, the same window in which
  they already hold `ai_session_entries` today. So the guarantee is the one deletion
  gives now.
- **Temporal UI** shows these workflows' payloads as ciphertext. No Codec Server is
  deployed, by design: reading a session's content goes through ScadBuddy's own access
  checks, not through the Temporal UI link (#668).

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
| `wait_for_human(question, timeout=…)` | waits on the run's `answer` Update | a person |
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
  Approve/Deny sends the harness's Update. A flow has no chat session, so its approvals
  do not appear in the assistant's panel. An `agent(...)` step is a durable session of
  its own, and that session's approvals appear in the assistant's panel (§6.4), with
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
    workflow_id, workflow_run_id, created_at, updated_at)`, written by the workflow's
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
  answered (§7.2); both read the pending request from the workflow and answer it with
  an Update, so there is no second copy to keep in sync. An
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
     `follow_print`, with the watcher's loop. The heartbeat carries when the print last
     moved, so a retried attempt keeps its age. `PrintRun` starts it as an abandoned
     child after `print_finish`, or pokes the one already running (starting it again if
     it closed in between). The progress route only starts it when none is running,
     never pokes. A poke cancels the attempt (`TRY_CANCEL`) and starts a fresh one that
     reads at once, then backs off as usual. A worker shutdown ends an attempt at once. `bambuddy/watcher.py` and its lock and
     rescan are gone; a boot pass hands the prints in `print_watches` to `FollowPrint`
     and empties it (the table is dropped later).
3. **Library commands** (§4.3 `library`, §4.4 Schedules): the `scadbuddy-library`
   container, every git, file and download command, and the sweeps as Schedules. Done by
   route group, one plan per group if the plan says so.
   - As built so far (3a, #1054, plan `2026-10-03-durable-phase-3a-housekeeping-schedule.md`):
     the Schedule `scadbuddy-housekeeping-<queue>` (overlap `SKIP`, every
     `asset_sweep_interval`; `0` deletes it) starts `Housekeeping` on the `library` queue.
     It runs four activities in order, each tried once and best effort: prune settled
     render jobs, sweep unused uploads, sweep unreferenced blobs, sweep old duplicate
     staging. A second Schedule, `scadbuddy-prune-<queue>`, runs only the prune every
     300 s (the old loop's cadence) and stays when the interval is `0`. Setup is retried
     until Temporal takes it. Its worker runs in the API process, which holds the data volume, until the
     `scadbuddy-library` container. The API's sweep loop and `RenderService`'s prune
     loop are gone. The boot passes stay in the boot, since they must finish before the
     first request; the boot then triggers the Schedule once.
   - As built so far (3b, #1054, plan `2026-10-03-durable-phase-3b-library-pins.md`): an
     `OperationKind` names its queue (`bambuddy` or `library`), and each worker serves only
     its own kinds. Pin, re-pin, unpin and checkout removal are `library` kinds
     (`library/operations.py`) on the generic `Operation` workflow. They are `done`, not
     `accepted`: a fast clone answers the model as before, and one past the deadline
     answers 202 with the operation, so no request is held past it either way. The
     library check (`POST …/check`) stays a request. The UI's and the agent's pin calls go
     through `command()`.
   - As built so far (3c, #1054, plan `2026-10-03-durable-phase-3c-model-lifecycle.md`):
     model create (all three bodies), import, patch, duplicate and delete are `library`
     kinds (`library/model_operations.py`). Each route keeps the refusals that read only
     the request. The check makes the ones that read the volume, and the run makes them
     again just before the effect. A create's source, thumbnail and README travel by
     claim check (`operations/claims.py`): the route writes them to `cache/claims/`,
     named by their sha256, and the operation carries only the names, because a source
     may be 1M characters and a thumbnail 10 MB, past Temporal's payload limits. Since
     the name is the digest, a re-send reaches the same operation key. A claim is
     removed by the claim sweep (`housekeeping_sweep_claims`, on the prune Schedule
     every 300 s whatever the sweep interval, a day after the last request put it), not
     by its run, because two requests may share the same bytes. An operation's inline
     request is capped at 128 KB (413 past it), and an import's subject is the URL's host. The UI's and the agent's calls to these routes go through `command()`.
4. **Tools as activities** (§6.3): the `ALL_TOOLS` export and the `agent-tools` worker in
   the agent service, plus the plugin package install as a command.
5. **Durable session mode** (§6.1, §6.2, §6.4): `agent-durable/`, the plugin pin, the
   `SessionStore`, the credential port, the event subscriber, HITL, the mode UI and
   setting.
6. **Flows** (§7): the harness verification, `ProjectWorkflow`, host functions, records,
   routes, Reset, and the Workflows page.

Follow-ups outside this spec: moving the plugin pin to PyPI once the package is
published; and `ai_plugins` / `ai_plugin_packages` for durable sessions, behind a Python
port of the plugin vetting (§6.3b).
