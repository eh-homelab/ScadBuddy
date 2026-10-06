# Phase 5: durable agent session mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user can start an assistant session in *durable* mode. Its loop runs as the
Temporal workflow `DurableSession` (`session-<ai_sessions.id>`, queue `agent`) in a new Python
sidecar, `agent-durable`, built on `temporalio-claude-agent-sdk`. Every tool is an
`activity_as_tool` stub served by the phase 4 `agent-tools` worker. Outward tools wait in the
workflow for the panel's approve/deny, and the session's payloads are encrypted per subject
so that forgetting a subject crypto-shreds it. The chat socket and the panel show a durable
session the same way they show a classic one (#1056).

**Architecture:**
- `agent-durable/` is a uv project (Python 3.12), package `scadbuddy_durable`. It pins
  `temporalio-claude-agent-sdk` to one commit of `temporalio/ai-integrations`, and
  `claude-agent-sdk==0.2.160`, which bundles Claude Code 2.1.283, the version the
  TypeScript SDK pins. `python -m scadbuddy_durable.worker` runs one Temporal worker on
  `agent` with the `DurableSession` workflow, the plugin's segment activity
  (`ClaudeAgentPlugin` around our `SessionRunner`), and the payload codec. It also runs the
  event projector and serves `/healthz` on 8082. The Dockerfile target is `agent-durable`,
  run as a sidecar trusted like `agent` (#1030).
- **Tools.** The phase 4 build writes `agent/dist/tools.json`. `scadbuddy_durable.tools`
  turns each entry into `activity_as_tool(<a named stub activity>, task_queue="agent-tools",
  needs_approval=(tier == "outward"))`. Only the TypeScript worker ever runs the activity.
- **Conversation.** It is kept in Postgres through a claude-agent-sdk `SessionStore` on the
  existing `ai_session_entries`, the same columns and encoding as the TypeScript
  `PostgresSessionStore`. `SessionRunner` (the plugin's exported `SegmentRunner` protocol)
  builds one `ClaudeAgentSdkRunner` per segment. That runner gets the session's credential
  `env`, opened with the Python port of `openSecret`, and the session's remaining budget as
  `max_budget_usd`. It records each segment's cost and Claude session id in
  `ai_durable_segments`.
- **Events.** The workflow publishes live output (`live_output=True`). The projector holds a
  lease per running durable session (`ai_durable_streams`) and reads `follow_agent`. It
  translates each event into the panel protocol, and appends the events, status and stream
  offset to `ai_session_events` in one transaction, followed by the `scadbuddy_events`
  NOTIFY that `SessionEventPublisher` sends. The chat socket and the panel do not change.
- **Agent service.** `ai_sessions.mode` already exists (phase 4). `start` takes `mode`,
  defaulting to the `ai_settings` key `session_mode`, and refuses it on an existing session.
  `send` on a durable session writes `user.turn` + `running` and then calls update-with-start
  `send_message` on `session-<id>`. An approval id `durable:<session>:<tool_use_id>` routes
  approve/deny to the workflow's `review` Update. `GET/PUT /api/v1/ai/settings/session-mode`.
  `forgetSubject` and its CLI cover deletion.
- **Codec.** `SubjectPayloadCodec` in Python (`WithSerializationContext`) and TypeScript
  (`PayloadCodec` with `SerializationContext`) encrypts every payload of a `session-*` or
  `flow-*` workflow with AES-256-GCM, in `secrets.ts`'s sealed format, under that subject's
  data key in `ai_payload_keys`.
- **Frontend.** While the chat is empty, the composer shows an Advanced disclosure with a
  Classic/Durable select. The last choice is remembered in `scadbuddy.assistant.mode`, and
  the default comes from Settings → Assistant → "Default session mode". A durable session
  shows a Durable badge in the header.

**Tech Stack:** Python 3.12, uv 0.12.19, `temporalio` 1.33.0,
`temporalio-claude-agent-sdk` (git pin below), `claude-agent-sdk` 0.2.160, psycopg 3 +
psycopg-pool, `cryptography` (AESGCM), pytest + pytest-asyncio, ruff, mypy strict. Agent:
Node 24, TypeScript, `@temporalio/*` 1.24.0, postgres.js, Hono, vitest. Frontend: React 19,
vitest, msw.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §6.1,
§6.2, §6.3 (the Python half), §6.3a, §6.3b, §6.4, §6.5, §8 "Durable session" and "CI", §10
phase 5, as amended by #1030 (`agent-durable` is a sidecar trusted like `agent`). Stacked on
`feat/1055-agent-tools` (PR #1337).

## Upstream, verified 2026-10-04

- `temporalio/ai-integrations` PR #33, "Add the Claude Agent SDK plugin
  (python/claude_agent_sdk)": open, **no longer a draft**. The head is
  **`b1cf3848b15ad5cd1f009bd19524e3f751140439`** (committed 2026-10-03T23:57:51Z), and the
  author association is `NONE`. That head is 16 commits ahead of, and 0 behind, the spec's
  `766c6479c40eadbe5300c1133bcf064725540df6`, so nothing the spec read was rewritten. A
  Temporal member (`brianstrauch`, MEMBER) requested changes on 2026-09-30, about
  `FileSessionStore` transcript integrity. The author says 766c647 fixed them, and no re-review
  is recorded yet.
- `gh api repos/temporalio/ai-integrations/commits/b1cf3848…` resolves the commit in
  Temporal's repository. `uv lock` with uv 0.12.19 resolved
  `temporalio-claude-agent-sdk @ git+https://github.com/temporalio/ai-integrations@b1cf3848…#subdirectory=python/claude_agent_sdk`
  next to `claude-agent-sdk==0.2.160` and `temporalio==1.33.0`. It needs
  `[tool.uv] environments = ["sys_platform == 'linux'"]`, because the package's win32 marker
  excludes 0.2.160. The lock records
  `source = { git = "https://github.com/temporalio/ai-integrations?subdirectory=python%2Fclaude_agent_sdk&rev=b1cf3848…#b1cf3848…" }`.
- The API, read at that head (`README.md`, `_workflow.py`, `_runner.py`, `_activity.py`,
  `_models.py`, `_events.py`):
  - `activity_as_tool(fn, *, name, description, input_schema, needs_approval,
    start_to_close_timeout=1min, retry_policy, cancellation_type, schedule_to_close_timeout,
    heartbeat_timeout, task_queue)`. It raises `TypeError` when `fn` is not `@activity.defn`.
    The call is `workflow.execute_activity(tool.activity, call.input,
    activity_id=f"tool-{call.id}", task_queue=tool.task_queue, …)`.
  - `DurableClaudeAgent(*, system_prompt, tools, model, max_turns, builtin_tools,
    tool_activities=("Bash","mcp__*"), tool_approvals, …, max_segments=50, approvers, state,
    auto_continue_as_new, continue_as_new_args, live_output, …)`, with `run(prompt)`,
    `decide(id, approved, approver)`, `validate_decision(id, approver)`,
    `pending_approvals()`, `tool_calls`, `busy`, `should_continue_as_new()`,
    `continue_as_new()`, `state()` and `total_cost_usd`.
  - The `review` Update is the user's own workflow method that calls `decide`, with a
    validator that calls `validate_decision` (README quick start). The plugin has **no
    approval expiry** and **no interrupt** other than Workflow cancellation.
  - `ClaudeAgentSdkRunner(*, session_store, cwd, env, cli_path, extra_options,
    one_tool_at_a_time, model, max_budget_usd)`. `env` and `max_budget_usd` are fixed per
    runner. `extra_options` refuses `model`, `tools`, `max_turns`, `cwd`, `settings`,
    session options, `max_budget_usd`, `cli_path` and `session_store`. It accepts
    `plugins`, `system_prompt` (a string or a preset) and `setting_sources`, and the runner
    already sets `setting_sources=[]` and `strict_mcp_config=True`.
  - `SegmentRunner` (a Protocol with `run(inp: SegmentInput, attempt) -> SegmentOutput`) and
    `ClaudeAgentPlugin(runner)` are exported. `SegmentOutput` carries `session_id`,
    `cost_usd`, `is_error` and `error`, and `SegmentInput` carries `segment_index`.
  - `follow_agent(client, workflow_id, from_offset=)` yields these event types: `prompt`,
    `text` (one assistant message, with `segment` and `attempt`), `tool_call`,
    `approval_needed`, `tool_result` (`status`: `done`, `failed`, `rejected`,
    `unknown tool` or `cancelled`), `retry`, `continued_as_new`, `done` (`result`), `error`
    (`error`) and `cancelled`, each with `at` and `offset`. Every subscriber poll is an
    Update, so the README advises one subscriber in the backend.
  - The requirement is Claude Code ≥ 2.1.273 (`claude-agent-sdk>=0.2.153,<0.3`).
    `claude-agent-sdk` 0.2.160 bundles 2.1.283
    (`anthropics/claude-agent-sdk-python` `v0.2.160` `src/claude_agent_sdk/_cli_version.py`).
  - claude-agent-sdk `SessionStore` (v0.2.160 `types.py`): `append(key, entries)`,
    `load(key)`, `list_sessions(project_key)`, `delete(key)`, `list_subkeys(key)`, with
    `SessionKey = {project_key, session_id, subpath?}`. The repository ships
    `examples/session_stores/postgres_session_store.py` and
    `claude_agent_sdk.testing.session_store_conformance`.
- `temporalio` 1.33.0 has `converter.WithSerializationContext`,
  `WorkflowSerializationContext.workflow_id` and `ActivitySerializationContext.workflow_id`.
  Phase 4 confirmed that a `@temporalio/common` 1.24.0 codec receives `workflowId`.

## Deviations (decided by the user, 2026-10-04)

Each is a place where the spec, written against `766c647`, no longer matches the plugin, or
where the plugin offers nothing for what the spec needs. The user decided all five: 1
accepted (it follows upstream), 2 keep the Postgres store, 3 the wrapper, 4 as revised
below, 5 pin `b1cf3848`.

1. **The two "known limitations" of §3.2 are solved upstream.** Both commits are after
   766c647: parallel durable calls (2c1fcb6), and Bash/MCP calls run as their own activities
   (39257db, 615e8ff). The plan therefore **drops the spec's "one tool call per message"
   system-prompt rule**. It follows the plugin's default: durable calls of one message run at
   once, each with its own approval. It also passes `tool_activities=()`, because a durable
   session enables no Bash and no MCP server (only `Skill`, §6.3b). The spec's test, "the
   engine is started with no built-in tool but `Skill`", is kept. Confirm parallel tool calls
   are acceptable for ScadBuddy's tools.
2. **The conversation now lives in the Workflow by default.** A `SessionStore` has become
   optional upstream (995bac6). The plan **keeps the spec's Postgres `SessionStore`**. The
   README says to "implement `SessionStore` on your database" for production, and to use one
   to keep Continue-As-New state small without External Storage. With the conversation in the
   Workflow and no External Storage, a chat outgrows the 2 MB input after a few hundred steps
   and can no longer continue as new. The alternative is the plugin's default plus Temporal
   External Storage (an S3 driver on DO Spaces), with no Postgres store: one fewer table,
   but a new storage dependency. Recommended: keep the store.
3. **Per-session credential and budget need a `SegmentRunner` wrapper.** `ClaudeAgentSdkRunner`
   takes `env` and `max_budget_usd` once per worker. §6.2 wants the credential decrypted per
   segment and the row's remaining budget per segment. The plan implements the exported
   `SegmentRunner` protocol (`SessionRunner`). It builds a `ClaudeAgentSdkRunner` per segment
   with that segment's `env` and `max_budget_usd`, and delegates `run`. This uses only public
   API, but no upstream example builds a runner per segment. The cost is that the engine
   version (`claude -v`) is checked once per segment instead of once per worker. Recommended:
   accept it, and ask upstream for a per-segment options hook as a follow-up.
4. **Interrupt (decided): Stop cancels the current execution, and the next message starts
   a new one that resumes the conversation.**
   - Stop is Temporal workflow cancellation of `session-<id>`. That is the documented API,
     and the README's only stop.
   - **The store alone is not enough to resume.** Verified at `b1cf3848`:
     - The plugin resumes a Claude session from `AgentState`, not from the store.
       `SegmentInput` carries `session_id` and `checkpoint` (the transcript entry where the
       last committed segment ended). `_runner.py:1620` starts a **new** Claude session when
       `checkpoint is None`. A new execution built with `state=None` therefore gets
       `AgentState()`, and `run()` gives it a fresh `session_id = workflow.uuid4()`
       (`_workflow.py:773`): no history.
     - The rest of what a resume needs is also only in `AgentState`, not in the store:
       - `pending`: the error results owed for calls Claude is still waiting on;
       - `fork_next`: continue in a copy of the session that ends at the checkpoint;
       - `recent_call_ids`: a call never runs twice.
     - The store cannot supply the checkpoint: it may hold entries a cancelled segment wrote
       after it, which is why the plugin forks at the checkpoint (`fork_session_via_store`,
       `_runner.py:1646`).
   - **What the cancel leaves.** When the cancellation reaches `agent.run()`, `run()` calls
     `_end_task("the Workflow was cancelled")` (`_workflow.py:776-783`, `1030-1064`), then
     publishes `cancelled` and re-raises.
     - Every call Claude was still waiting for gets an error result in `state.pending`.
       - An approval that was waiting (its `wait_condition` is cancelled): "This tool call
         did not run: the Workflow was cancelled."
       - A tool activity that had started: "This tool call was interrupted (…); whether it
         took effect is unknown. Check before running it again."
       - A call that had finished: its real result.
     - It sets `task_prompt = None` and `fork_next = True`, and keeps `session_id` and
       `checkpoint` (it clears them only when nothing was ever committed).
     - The store stays consistent: the next segment forks at the checkpoint, so whatever the
       cancelled segment wrote after it never reaches Claude.
   - **The design.**
     - `DurableSession.run` catches `asyncio.CancelledError` around its loop and **returns
       `self.agent.state()` as the workflow's result**. A Python workflow may catch its
       cancellation and complete. The execution then closes as *Completed*, not *Cancelled*:
       a deliberate difference from the README's "ends as cancelled", so that the hand-over
       is in Temporal and is encrypted by the codec.
     - The next message's update-with-start finds the ID closed. The agent service reads the
       last execution's result (the `AgentState`, passed back as opaque JSON) and starts the
       new execution with `[input, state, null]`, reusing the ID
       (`workflowIdReusePolicy: ALLOW_DUPLICATE`).
     - The new run's first segment resumes the same Claude session at its checkpoint, with
       the owed error results delivered alongside the new prompt (README: "After a failed
       task, the agent can take the next one").
   - **An execution that closed without handing over: snapshots (decided by the user).**
     A termination, a workflow-task failure or a deleted history leaves no result to read.
     For those cases the workflow saves the plugin's `AgentState` to Postgres as it goes
     (Ruling 15), and a new execution resumes from the latest snapshot.
     - `resumed_fresh` remains only for a session that has no snapshot at all.
     - Verified 2026-10-04 against the locked environment. `AgentState` round-trips through
       `DataConverter.default.payload_converter`, including `pending` with `ToolOutcome`
       (`content`, `is_error`, `blocks`). It also round-trips as the plain JSON object the
       agent service passes back: `from_payloads(..., [AgentState])` equals the original.
       The keys are `checkpoint`, `conversation`, `external_storage`, `fork_next`,
       `pending`, `recent_call_ids`, `runs`, `segment_index`, `segments`, `session_id`,
       `stream`, `task_prompt`, `task_segments`, `tool_calls` and `total_cost_usd`.
     - **What keeps a call from running twice when resuming from an older snapshot.**
       1. `recent_call_ids` holds the last 256 call ids that ran, across runs, and the agent
          refuses any id in it (`_workflow.py:878-892`). It stops the engine from handing
          back a call that already ran. It cannot stop Claude from asking for the same
          action again under a **new** id, which is what happens after forking at an older
          checkpoint.
       2. The snapshot records the calls in flight (`started` or `waiting for approval`).
          Restoring applies `_end_task`'s rule by hand (`restore_state`, Ruling 15). Each
          in-flight call gets the error result `_outcome_after_stop` would give it ("did
          not run" / "interrupted; whether it took effect is unknown") and joins
          `recent_call_ids`. Then `task_prompt = None`, `task_segments = 0` and
          `fork_next = True`. Claude learns what happened to those calls and is never handed
          them again.
       3. The remaining window: a tool that **finished** after the snapshot was taken, if the
          execution died before the next snapshot. Snapshots are a local activity started
          when the agent's counters change, so the window is a few milliseconds of one
          workflow task. The phase 4 `tool_call` audit rows (`session_id`, `toolUseId`,
          outcome) catch it. On a snapshot restore, the agent service adds to the new
          message's model-only context: "These tool calls ran after this session's last
          saved point, and their results were lost: <name> (<id>), …". It lists the audit
          rows newer than the snapshot whose ids are neither in `recent_call_ids` nor among
          the in-flight calls. So nothing is silently re-run without Claude being told.
5. **The pin is a newer head than the spec names.** The plan pins `b1cf3848`, not `766c647`,
   because the spec's own test and the solved limitations depend on the newer commits. A
   Temporal member's change request is still open on the PR (above). Confirm the pin.

## Rulings

1. **`mode` exists already.** `20261004T1330Z_session_mode.sql` (phase 4) added
   `ai_sessions.mode`. Phase 5 adds no column for it and only starts writing `'durable'`.
2. **Defaults.** When `mode` is omitted, `ai_settings.session_mode` applies, and
   `'classic'` applies when that is unset or invalid. The chat socket's `user.message` with
   a `sessionId`, and `POST /sessions/:id/messages`, refuse `mode` with code `invalid`: "mode
   is chosen when a session starts and cannot change". `sessions_start` and
   `POST /api/v1/ai/sessions` take it.
3. **A durable session's limits.** The session row's `max_turns` is the agent's per-segment
   `max_turns` (classic passes it per query). Each segment's `max_budget_usd` is
   `budget_usd - cost_usd`, read when the segment starts, so a budget raise (#790) applies to
   the next segment. A segment that starts with nothing left returns an `is_error` output
   ("the session's budget is spent"), and the task fails non-retryably. `cost_usd` is the sum
   of every attempt's cost in `ai_durable_segments`, since a retried attempt really spent its
   money. `turns` is the number of distinct segments.
4. **The credential is read, never judged.** `SessionRunner` takes rows in pool order:
   `priority`, skipping `disabled`, `cooling_down` with `cooldown_until > now()`, and any
   that do not open with the mounted key (current, then previous). It uses the first. It
   does not write health (cooldowns, disables, `last_used_at`), because those stay with the
   classic `CredentialPool`. No usable credential raises a non-retryable `ApplicationError`
   ("no usable Claude credential: …"), so the task fails with a visible error rather than
   retrying forever. The `env` follows `credentialEnv`: `ANTHROPIC_API_KEY`,
   `CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`.
5. **One message at a time.** `send_message`'s validator refuses a message while the agent is
   busy or the inbox holds one ("the session is busy"), as classic refuses a second turn. The
   Update handler only enqueues and returns at once. With `auto_continue_as_new`, `run()`
   must be called from the run method, and no handler may wait for a task (README,
   "Chats").
6. **Starting.** The agent service sends update-with-start: Update `send_message`, start
   `DurableSession` on queue `agent`, ID `session-<id>`,
   `workflowIdConflictPolicy: USE_EXISTING`. The start arguments are
   `[input, null, null]`, because Temporal applies types only when every `run` argument is
   passed (README, "Start with every argument"). The workflow completes only when Stop
   cancels it (deviation 4), returning its `AgentState`. An idle open workflow costs nothing.
   When the ID is closed, the agent service starts the next execution with
   `[input, <state>, null]` and `workflowIdReusePolicy: ALLOW_DUPLICATE` (deviation 4).
   `<state>` is the last result for a *Completed* execution, else the latest snapshot with
   `input.restored = {in_flight}`, else `null` with `resumed_fresh`.
7. **Approval ids.** A durable approval's id in `approval.required` is
   `durable:<sessionId>:<tool_use_id>`, at most 200 characters, which fits the socket's
   `id` limit. `ApprovalService.decide` and `decision` route such an id to
   `DurableSessions.review` after the ownership check that `decide` makes
   (`visible` + `authorize`, factored into `authorizeSession`). The approver string is
   `<kind>:<id>`. The route appends `approval.resolved {approved, by}` when the Update
   succeeds. A refused Update (the call is not waiting, or was already decided) answers 409
   `conflict`. `GET /approvals?session=<durable>&pending=true` answers from the
   `pending_approvals` Query.
8. **Expiry.** `approval_expiry_seconds` is read when the session's workflow starts, carried
   in `SessionInput`, and fixed for the session. The workflow starts one timer per waiting
   call. When the timer fires and the call is still waiting, the workflow calls
   `agent.decide(id, False, "system:expired")`. The projector then appends
   `approval.resolved {approved: false}` without `by`, as classic does for an expired
   approval.
9. **Fork and interrupt.** `fork` of a durable session answers 409 `unsupported`, because
   the SDK cannot copy a Workflow's state into a new session.
   - Interrupt is `WorkflowHandle.cancel()` on the running execution (deviation 4). It answers
     `true` when an execution was running, and `false` when none was.
   - The projector turns the `cancelled` event into an `approval.resolved {approved: false,
     reason: "the turn was stopped"}` for each approval left open, then `session.status
     idle`.
   - The projector's offset restarts at 0 for each new execution. The agent service resets
     `ai_durable_streams.next_offset` when it starts a new execution: Workflow Streams
     offsets are per execution chain, and follow Continue-As-New but not a new start.
10. **Text.** `text` events are buffered per `(segment, attempt)`. A `retry` drops the
    older attempt's text, and the buffer is flushed (as one
    `assistant.text.delta` + `assistant.text.done` per message) at the segment's next
    `tool_call`, `approval_needed`, `done` or `error`. The panel never shows text that a
    retried attempt superseded.
11. **Payload keys.** A subject is a workflow ID (`session-<uuid>` or `flow-<uuid>`). Its row
    `ai_payload_keys(subject, dek_sealed, kek_id, created_at)` is inserted in the same
    transaction as the durable `ai_sessions` row. The data key is sealed with AAD
    `dek:ai_payload_keys:<subject>`. Each payload is sealed with AAD
    `ai_payload:<subject>`, as `Payload.SerializeToString()`, into a payload with metadata
    `encoding: binary/encrypted` and `encryption-key-id: <subject>` (Temporal's encryption
    sample's names). A payload of any other workflow passes through. Encoding a subject with
    no key row fails closed. Decoding a `binary/encrypted` payload whose key is gone raises
    `SealError("payload key for <subject> is gone")`. The process caches keys for 60 s, so a
    forgotten subject stops decrypting everywhere within a minute.
12. **`forgetSubject(subject)`.** It deletes the key row, terminates the workflow if open,
    calls `DeleteWorkflowExecution`, then deletes the session's rows (`ai_sessions`, which
    cascades events and `ai_durable_*`, plus the `ai_session_entries` of every Claude session
    id in `ai_durable_segments`). Its caller in phase 5 is an operator CLI,
    `node dist/forget-subject.js session-<id>`. No delete route is added (§6.5).
13. **Skills only.** The runner gets `extra_options={"plugins": [{"type": "local", "path":
    "/app/agent-durable/plugin"}], "system_prompt": {"type": "preset", "preset":
    "claude_code", "append": <UNTRUSTED_CONTENT_POLICY>}}`. The plugin directory holds
    `.claude-plugin/plugin.json` and `skills/` only. The agent gets
    `builtin_tools=["Skill"]` and `tool_activities=()`. No `Agent`, no `ai_plugins`, no
    `ai_plugin_packages` (§6.3b). The policy text comes from the agent build
    (`dist/durable-prompt.txt`), so it is never copied by hand.
14. **The projector's lease.** The `ai_durable_streams` row `(session_id, next_offset,
    holder, lease_until)` is claimed when the session is `running` or `waiting_approval` and
    the lease has expired or is the projector's own. The projector renews it every 5 s, with
    a 20 s lease. One transaction appends the events, sets `ai_sessions.status`, bumps
    `event_seq` and stores `next_offset`, so a crash replays from the last committed offset
    and never duplicates an event. The follower stops after `done`, `error` or `cancelled`.
    The NOTIFY payload and kinds are `SessionEventPublisher`'s, without its throttle (one
    NOTIFY per committed batch; batches are per event group, not per token).

15. **Snapshots.**
    - Table `ai_durable_snapshots(session_id uuid PK → ai_sessions ON DELETE CASCADE,
      version bigint, state text, in_flight jsonb, saved_at timestamptz)`. `state` is the
      payload converter's JSON of `AgentState`, plain text like `ai_session_entries`
      (deleted with the session by `forgetSubject`).
    - The workflow runs one background task, `_snapshots`. It waits until
      `mark = (agent.segments, agent.total_tool_calls, tuple((c["id"], c["status"]) for c
      in agent.tool_calls))` changes, then awaits the local activity `save_snapshot`
      (`SnapshotInput(session_id, state=agent.state(), in_flight=[{id, name, status}],
      version=segments + total_tool_calls)`, 10 s timeout, retried). As built (Task 9
      review): `in_flight` is every call of the unanswered batch, the
      `agent.tool_calls` whose id is not yet in `state.recent_call_ids`, whatever its
      status. A call that finished joins `recent_call_ids` only when the next segment
      commits, so "started/waiting" alone would let a finished call run again.
    - The activity upserts only when `excluded.version >= ai_durable_snapshots.version`, so
      an out-of-order write never moves a snapshot backwards. A new run snapshots at once
      (the initial mark is `None`).
    - Restore: `SessionInput.restored: Restored | None` (`in_flight: list[InFlight]`).
      `DurableSession.__init__` passes `restore_state(state, restored.in_flight)` to the
      agent when it is set. `restore_state` is a pure function in `models.py`: copy the
      state; for each in-flight call set `pending[id] = ToolOutcome(content=<the text for
      its status>, is_error=True)` and append the id to `recent_call_ids` (cap 256). The
      texts, with reason "the previous run of this session stopped unexpectedly"
      (`models.py` `_outcome`): `started` or `cancelled`, `_outcome_after_stop`'s
      "interrupted (…); whether it took effect is unknown"; `done` or `failed`, "This tool
      call ran, but its result was lost when <reason>. Check its effect before running
      it again."; `rejected`, "A human reviewer rejected this action. Do not retry it.";
      `waiting for approval` (or anything else), "This tool call did not run: <reason>." Then set
      `task_prompt = None`, `task_segments = 0` and `fork_next = True`. If
      `checkpoint is None`, set `session_id = None`, as `_end_task` does.

## Global Constraints

- Follow the Temporal SDKs and the plugin as documented. Anything not covered by the
  deviations above stops the work, and the user decides (spec §9).
- The plugin is git-pinned to `b1cf3848b15ad5cd1f009bd19524e3f751140439` in
  `agent-durable/uv.lock` and never vendored. `claude-agent-sdk==0.2.160` (Claude Code
  2.1.283) is bumped in the same commit as `@anthropic-ai/claude-agent-sdk` and
  `CLAUDE_CODE_VERSION`.
- Migrations: NEW files in `agent/src/db/migrations/`, named
  `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`. Never edit a merged one. `agent-durable` runs no
  migrations of its own: the agent owns the `ai_*` tables.
- `agent-durable` reads only infrastructure variables: `SCADBUDDY_DATABASE_URL`,
  `SCADBUDDY_SECRET_KEY_FILE`, `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`,
  `SCADBUDDY_TEMPORAL_ADDRESS`, `SCADBUDDY_TEMPORAL_NAMESPACE` (default `scadbuddy`) and
  `SCADBUDDY_AGENT_DURABLE_HEALTH_PORT` (default 8082). No Bambuddy key and no AI settings.
- The credential never enters a log, a file or a workflow history. It reaches only the
  per-segment runner `env`.
- Tests never call Anthropic. The real engine runs against
  `agent/test/support/fakeAnthropic.ts` as a gateway. Temporal tests skip unless
  `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` names a Temporal CLI (or `temporal` is on `PATH`).
  Postgres tests skip without `SCADBUDDY_TEST_DATABASE_URL`, and need their own database
  when run beside another session (MEMORY: parallel test runs).
- CI: hosted runners only. The new image stage gets a `type=gha` scope of its own,
  `scope=agent-durable`, with `mode=min`.
- Never commit `backend/openapi.json`, a `schema.d.ts`, `agent/dist/tools.json` or
  `agent/dist/durable-prompt.txt`.
- Commits: conventional, naming `(#1056)`, ending with a blank line and
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage explicit paths.
- localStorage key: `scadbuddy.assistant.mode`, with every read and write in try/catch.

## Review Focus

1. **A durable session whose worker is down.** The user sends a message, update-with-start
   succeeds, and no `agent` worker polls. The session must show `running` and must not say
   it failed. When the sidecar starts, the turn runs. Pinned in Task 12: the Update is
   accepted with no worker, and the status stays `running`. Task 17 starts the worker late
   and checks that the reply arrives.
2. **Approve/deny racing expiry, or a second click.** A decision that arrives after the
   timer denied the call, or a second approve, must answer 409 and never run the tool twice.
   Pinned in Task 9 (the validator refuses after expiry, and refuses a decided call) and in
   Task 12 (the route maps the refusal to 409 `conflict`).
3. **Key rotation.** A session's payload key sealed under the previous KEK must still
   decode once `SCADBUDDY_SECRET_KEY_FILE` holds the new key and `…_PREVIOUS_FILE` the old
   one. The same goes for a credential sealed under the previous key. Pinned in Task 3
   (credential) and Task 4 (payload key).
4. **A projector that dies mid-batch.** Events must neither repeat nor go missing in the
   panel. Pinned in Task 10: kill the follower after a batch commits but before the next
   one, and a new holder resumes at `next_offset` with no duplicate `seq` content.
5. **Terminate, then send again.** An operator terminate, or a workflow-task failure, must
   not cost the conversation, and no call may run twice unannounced. Pinned in Task 9
   test 12 (restore from a snapshot taken mid-tool), Task 12 (restore input and the
   lost-results context) and Task 17 (terminate mid-session on the real engine).
6. **Stop, then send again.** The model must see the earlier history, and a call
   interrupted mid-run must be reported as "unknown whether it took effect", never silently
   re-run. Pinned in Task 9 test 10 and Task 17.
7. **A message with `mode` on an existing session, and a classic session's approvals.**
   The first is refused. The second never routes to Temporal: ids without the `durable:`
   prefix still use `ai_approvals`. Pinned in Task 5 and Task 12.

---

### Task 1: The `agent-durable` project and its pin

**Files:**
- Create: `agent-durable/pyproject.toml`, `agent-durable/uv.lock`, `agent-durable/.python-version`,
  `agent-durable/scadbuddy_durable/__init__.py`, `agent-durable/scadbuddy_durable/config.py`,
  `agent-durable/tests/__init__.py`, `agent-durable/tests/conftest.py`,
  `agent-durable/tests/test_config.py`, `agent-durable/tests/test_pin.py`
- Modify: `.gitignore` (`agent-durable/.venv/`)

**Interfaces:**
- Produces: `Config` (frozen dataclass: `database_url: str | None`, `secret_key_file: str | None`,
  `secret_key_previous_file: str | None`, `temporal_address: str | None`,
  `temporal_namespace: str`, `health_port: int`), `load_config(env: Mapping[str, str]) -> Config`,
  `ENV_VARS: tuple[str, ...]`, `ConfigError`.
- Produces (tests): `conftest.py` fixtures `pg_conninfo` (a throwaway schema with every file of
  `agent/src/db/migrations/` applied in name order, skips without `SCADBUDDY_TEST_DATABASE_URL`)
  and `temporal_env` (a dev server from `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` or `temporal` on
  `PATH`, else skip; yields a `temporalio.client.Client` on namespace `default`).

- [ ] **Step 1: Gate — the pin resolves from Temporal's repository.**

```bash
mkdir -p agent-durable && cd agent-durable
cat > pyproject.toml <<'EOF'
[project]
name = "scadbuddy-durable"
version = "0.1.0"
description = "ScadBuddy's durable agent sessions on Temporal (spec 2026-10-01 §6)"
requires-python = ">=3.12,<3.13"
dependencies = [
  "temporalio>=1.33.0,<1.34",
  # Bundles Claude Code 2.1.283, the version agent/package.json's SDK bundles.
  # Bump with @anthropic-ai/claude-agent-sdk and the Dockerfile's CLAUDE_CODE_VERSION.
  "claude-agent-sdk==0.2.160",
  # ai-integrations#33 (spec §6.2): git-pinned to one commit of Temporal's own
  # repository, never vendored; every bump is its own reviewed PR with the diff
  # of python/claude_agent_sdk between the two SHAs.
  "temporalio-claude-agent-sdk @ git+https://github.com/temporalio/ai-integrations@b1cf3848b15ad5cd1f009bd19524e3f751140439#subdirectory=python/claude_agent_sdk",
  "psycopg>=3.2",
  "psycopg-pool>=3.2",
  "cryptography>=43",
]

[dependency-groups]
dev = ["psycopg[binary]>=3.2", "mypy>=1.13", "pytest>=8.3", "pytest-asyncio>=0.24", "ruff>=0.7"]

[tool.uv]
package = false
# claude-agent-sdk 0.2.160 has no Windows wheel and the plugin excludes it there;
# the image and CI are Linux.
environments = ["sys_platform == 'linux'"]

[tool.ruff]
line-length = 110
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
files = ["scadbuddy_durable", "tests"]
strict = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
  "requires_postgres: needs SCADBUDDY_TEST_DATABASE_URL",
  "requires_temporal: needs a Temporal CLI or SCADBUDDY_TEST_TEMPORAL_DEV_SERVER",
  "requires_engine: runs the bundled Claude Code against the fake endpoint (needs node)",
]
EOF
echo 3.12 > .python-version
uv lock
grep -n 'rev=b1cf3848b15ad5cd1f009bd19524e3f751140439#b1cf3848b15ad5cd1f009bd19524e3f751140439' uv.lock
```

Expected: `uv lock` prints `Updated https://github.com/temporalio/ai-integrations (b1cf3848…)`,
and grep finds the `source` line. If the commit does not resolve from `temporalio/ai-integrations`,
**stop and report to the user** (spec §6.2).

- [ ] **Step 2: Gate — the limitation list is re-read.** `gh pr view 33 --repo
  temporalio/ai-integrations --json body,headRefOid`. Confirm the head is still `b1cf3848…`,
  or record the new head and stop for a reviewed bump. Confirm the "limitations, solved" table
  still lists both. Note the result in the PR description.

- [ ] **Step 3: Write the failing tests.**

```python
# tests/test_config.py
import pytest
from scadbuddy_durable.config import ENV_VARS, ConfigError, load_config


def test_defaults_and_names() -> None:
    cfg = load_config({})
    assert cfg.database_url is None and cfg.temporal_address is None
    assert cfg.temporal_namespace == "scadbuddy"
    assert cfg.health_port == 8082
    assert set(ENV_VARS) == {
        "SCADBUDDY_DATABASE_URL", "SCADBUDDY_SECRET_KEY_FILE", "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE",
        "SCADBUDDY_TEMPORAL_ADDRESS", "SCADBUDDY_TEMPORAL_NAMESPACE", "SCADBUDDY_AGENT_DURABLE_HEALTH_PORT",
    }


def test_blank_is_unset_and_bad_values_refused() -> None:
    assert load_config({"SCADBUDDY_TEMPORAL_ADDRESS": "  "}).temporal_address is None
    with pytest.raises(ConfigError):
        load_config({"SCADBUDDY_TEMPORAL_ADDRESS": "host :7233"})
    with pytest.raises(ConfigError):
        load_config({"SCADBUDDY_AGENT_DURABLE_HEALTH_PORT": "http"})
```

```python
# tests/test_pin.py
from pathlib import Path

PIN = "b1cf3848b15ad5cd1f009bd19524e3f751140439"


def test_the_plugin_is_pinned_to_temporals_repository() -> None:
    lock = (Path(__file__).parents[1] / "uv.lock").read_text()
    assert (
        f'source = {{ git = "https://github.com/temporalio/ai-integrations?subdirectory=python%2Fclaude_agent_sdk&rev={PIN}#{PIN}" }}'
        in lock
    )
    assert 'name = "claude-agent-sdk"\nversion = "0.2.160"' in lock
```

- [ ] **Step 4: Run, expect FAIL.** `cd agent-durable && uv run --frozen pytest tests/test_config.py tests/test_pin.py -v`
  Expected: `ModuleNotFoundError: scadbuddy_durable.config`.

- [ ] **Step 5: Implement `config.py`.**

```python
"""Infrastructure variables only (CLAUDE.md: no AI settings in env)."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ENV_VARS = (
    "SCADBUDDY_DATABASE_URL",
    "SCADBUDDY_SECRET_KEY_FILE",
    "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE",
    "SCADBUDDY_TEMPORAL_ADDRESS",
    "SCADBUDDY_TEMPORAL_NAMESPACE",
    "SCADBUDDY_AGENT_DURABLE_HEALTH_PORT",
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    database_url: str | None
    secret_key_file: str | None
    secret_key_previous_file: str | None
    temporal_address: str | None
    temporal_namespace: str
    health_port: int


def _opt(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def load_config(env: Mapping[str, str]) -> Config:
    address = _opt(env, "SCADBUDDY_TEMPORAL_ADDRESS")
    if address is not None and re.search(r"\s", address):
        raise ConfigError("SCADBUDDY_TEMPORAL_ADDRESS must be host:port with no whitespace")
    port_text = _opt(env, "SCADBUDDY_AGENT_DURABLE_HEALTH_PORT") or "8082"
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ConfigError("SCADBUDDY_AGENT_DURABLE_HEALTH_PORT must be a port number")
    return Config(
        database_url=_opt(env, "SCADBUDDY_DATABASE_URL"),
        secret_key_file=_opt(env, "SCADBUDDY_SECRET_KEY_FILE"),
        secret_key_previous_file=_opt(env, "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE"),
        temporal_address=address,
        temporal_namespace=_opt(env, "SCADBUDDY_TEMPORAL_NAMESPACE") or "scadbuddy",
        health_port=int(port_text),
    )
```

  Write `tests/conftest.py`. The Postgres fixture copies the backend's `pg_conninfo` pattern:
  it creates schema `t_<uuid hex>`, runs each `agent/src/db/migrations/*.sql` in sorted order
  with `search_path` set, yields a conninfo with `options=-csearch_path=<schema>`, and drops the
  schema afterwards. The Temporal fixture starts `temporal server start-dev --headless --port
  <free> --db-filename <tmp>/t.db` from the CLI path, waits for `Client.connect`, and stops it
  at session end. It is session-scoped.

- [ ] **Step 6: Run, expect PASS**, then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`.

- [ ] **Step 7: Commit.**

```bash
git add agent-durable/pyproject.toml agent-durable/uv.lock agent-durable/.python-version \
  agent-durable/scadbuddy_durable/__init__.py agent-durable/scadbuddy_durable/config.py \
  agent-durable/tests/__init__.py agent-durable/tests/conftest.py agent-durable/tests/test_config.py \
  agent-durable/tests/test_pin.py .gitignore
git commit -m "build(agent-durable): the durable session package and its git pin (#1056)" \
  -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Secret vectors, from one source of truth (TypeScript)

**Files:**
- Modify: `agent/src/secrets.ts` (injectable randomness; exported `sealBytes`/`openBytes`),
  `agent/package.json` (`gen:vectors`)
- Create: `agent/scripts/gen-secret-vectors.ts`, `agent/test/fixtures/secret-vectors.json`,
  `agent/test/secretVectors.test.ts`

**Interfaces:**
- Produces: `sealSecret(kek, plaintext, aad, random?: (n: number) => Buffer): Envelope`;
  `sealBytes(key: Buffer, plaintext: Buffer, context: string, random?): Buffer`;
  `openBytes(key: Buffer, sealed: Buffer, context: string): Buffer` (the existing private
  `seal`/`open`, exported for the codec); `secretVectors(): SecretVectorFile`.
- Produces: the file `agent/test/fixtures/secret-vectors.json`:
  `{ "kek_base64": "...", "kek_id": "...", "vectors": [{ "name", "version", "row_id", "kind", "base_url", "aad", "secret_sealed_b64", "dek_sealed_b64", "kek_id", "plaintext" }], "payload": [{ "subject", "dek_b64", "dek_sealed_b64", "plaintext_b64", "sealed_b64" }] }`.

- [ ] **Step 1: Write the failing test.**

```ts
// agent/test/secretVectors.test.ts
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { secretVectors } from '../scripts/gen-secret-vectors.js'
import { kekFromBase64, openSecret } from '../src/secrets.js'

const committed = JSON.parse(readFileSync(new URL('./fixtures/secret-vectors.json', import.meta.url), 'utf8'))

describe('secret vectors (spec §6.2)', () => {
  it('regenerates byte for byte: a format or AAD change needs new vectors', () => {
    expect(secretVectors()).toEqual(committed)
  })
  it('covers every credential kind in v2 and the v1 format', () => {
    const names = committed.vectors.map((v: { name: string }) => v.name)
    expect(names).toEqual(expect.arrayContaining(['anthropic_api_key.v2', 'claude_oauth_token.v2', 'gateway.v2', 'v1']))
  })
  it('opens every vector with the TypeScript openSecret', () => {
    const kek = kekFromBase64(committed.kek_base64)
    for (const v of committed.vectors) {
      const envelope = {
        secretSealed: Buffer.from(v.secret_sealed_b64, 'base64'),
        dekSealed: Buffer.from(v.dek_sealed_b64, 'base64'),
        kekId: v.kek_id,
      }
      expect(openSecret(kek, envelope, v.aad)).toBe(v.plaintext)
    }
  })
})
```

- [ ] **Step 2: Run, expect FAIL.** `cd agent && pnpm exec vitest run test/secretVectors.test.ts`. Expected: the import of `../scripts/gen-secret-vectors.js` fails.

- [ ] **Step 3: Implement.** In `secrets.ts`, add an optional `random: (n: number) => Buffer =
  randomBytes` parameter to `seal` and `sealSecret`, and export `sealBytes = seal` and
  `openBytes = open`, with doc comments that point at the payload codec. Add `sealV1ForTests`
  in the generator script only. It writes `0x01 | iv | tag | ct` with the context as AAD.
  `secrets.ts` never writes v1. `scripts/gen-secret-vectors.ts` does the following:
  - It builds the KEK from 32 bytes `0x11`. Data keys use `0x22 + index` and IVs `0x33 + counter`,
    from a deterministic `random(n)` that returns slices of a counter-filled buffer.
  - It writes one vector per kind with `credentialAad(id, kind, baseUrl)`: row ids `default`
    and `c-2`, `c-3`, and `base_url` `https://gateway.example/v1` for the gateway. The
    plaintexts are `sk-ant-test-0001`, `oauth-test-0002` and `gw-token-0003`.
  - It writes one v1 vector with AAD `ai_credentials:default`.
  - It writes two payload vectors for subjects `session-00000000-0000-4000-8000-000000000001`
    and `flow-00000000-0000-4000-8000-000000000002`, using `sealBytes` with Rule 11's AADs.
  - When run as a CLI (`pnpm gen:vectors`), it writes the JSON with 2-space indent and a
    trailing newline.
- [ ] **Step 4: Generate and run.** `pnpm gen:vectors && pnpm exec vitest run test/secretVectors.test.ts test/credentials.pg.test.ts test/credentials.routes.test.ts`. Expected: PASS.
- [ ] **Step 5: Commit** `test(agent): shared secret and payload vectors for the Python port (#1056)`,
  staging the five files.

### Task 3: The Python port of `openSecret`, and credential selection

**Files:**
- Create: `agent-durable/scadbuddy_durable/secrets.py`, `agent-durable/scadbuddy_durable/credentials.py`,
  `agent-durable/tests/test_secrets.py`, `agent-durable/tests/test_credentials.py`

**Interfaces:**
- Consumes: `agent/test/fixtures/secret-vectors.json` (Task 2).
- Produces: `Kek(id: str, key: bytes)`; `kek_from_base64(text) -> Kek`; `load_kek(path) -> Kek`
  (raises `SecretKeyError`); `Envelope(secret_sealed: bytes, dek_sealed: bytes, kek_id: str)`;
  `open_secret(kek, envelope, aad) -> str`; `seal_bytes(key, plaintext, context) -> bytes`;
  `open_bytes(key, sealed, context) -> bytes`; `SealError`;
  `credential_aad(row_id, kind, base_url) -> str`;
  `Credential(kind, secret, base_url)`; `credential_env(c) -> dict[str, str]`;
  `CredentialSource(pool, keks: Sequence[Kek])` with `async first_usable() -> Credential`
  (raises `NoUsableCredential(reason)`).

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_secrets.py
import base64
import json
from pathlib import Path

import pytest
from scadbuddy_durable.secrets import Envelope, SealError, kek_from_base64, open_bytes, open_secret

VECTORS = json.loads(
    (Path(__file__).parents[2] / "agent/test/fixtures/secret-vectors.json").read_text()
)


def b64(s: str) -> bytes:
    return base64.b64decode(s)


@pytest.mark.parametrize("v", VECTORS["vectors"], ids=lambda v: v["name"])
def test_opens_every_vector(v: dict[str, str]) -> None:
    kek = kek_from_base64(VECTORS["kek_base64"])
    assert kek.id == VECTORS["kek_id"]
    env = Envelope(b64(v["secret_sealed_b64"]), b64(v["dek_sealed_b64"]), v["kek_id"])
    assert open_secret(kek, env, v["aad"]) == v["plaintext"]


@pytest.mark.parametrize("p", VECTORS["payload"], ids=lambda p: p["subject"])
def test_opens_every_payload_vector(p: dict[str, str]) -> None:
    assert open_bytes(b64(p["dek_b64"]), b64(p["sealed_b64"]), f"ai_payload:{p['subject']}") == b64(p["plaintext_b64"])


def test_a_changed_aad_or_byte_fails() -> None:
    v = VECTORS["vectors"][0]
    kek = kek_from_base64(VECTORS["kek_base64"])
    env = Envelope(b64(v["secret_sealed_b64"]), b64(v["dek_sealed_b64"]), v["kek_id"])
    with pytest.raises(SealError):
        open_secret(kek, env, v["aad"] + "x")
    tampered = bytearray(env.secret_sealed)
    tampered[-1] ^= 1
    with pytest.raises(SealError):
        open_secret(kek, Envelope(bytes(tampered), env.dek_sealed, env.kek_id), v["aad"])


def test_kek_file_format_matches_typescript() -> None:
    with pytest.raises(ValueError):
        kek_from_base64("not base64!")
    with pytest.raises(ValueError):
        kek_from_base64(base64.b64encode(b"x" * 16).decode())
```

  `tests/test_credentials.py` (`requires_postgres`) inserts rows sealed with the vector KEK:
  - Each `credential_env` mapping is checked for each kind.
  - Order: priority 0 `disabled`, priority 1 `cooling_down` until the future, priority 2
    sealed under a foreign KEK, priority 3 good. The test expects priority 3.
  - A `cooling_down` row whose `cooldown_until` has passed is used.
  - A row sealed under the previous KEK opens when `keks=[current, previous]` (Review
    Focus 3).
  - No usable row raises `NoUsableCredential`, and the reason names each refusal without the
    secret.
  - `credential_aad("default", "gateway", "https://gateway.example/v1")` equals the vector's
    `aad`.

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `secrets.py`.**

```python
"""Port of agent/src/secrets.ts: AES-256-GCM envelopes, sealed as
version (1) | IV (12) | tag (16) | ciphertext, v2 AAD = "v2|" + context."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_BYTES = 32
IV_BYTES = 12
TAG_BYTES = 16
SEAL_V1 = 0x01
SEAL_VERSION = 0x02


class SecretKeyError(ValueError):
    pass


class SealError(Exception):
    """Decryption failed: wrong key or altered bytes. Never carries plaintext."""


@dataclass(frozen=True)
class Kek:
    id: str
    key: bytes = b""

    def __repr__(self) -> str:  # never print the key
        return f"Kek(id={self.id!r})"


@dataclass(frozen=True)
class Envelope:
    secret_sealed: bytes
    dek_sealed: bytes
    kek_id: str


def _aad(version: int, context: str) -> bytes:
    return (context if version == SEAL_V1 else f"v{version}|{context}").encode()


def kek_from_base64(text: str) -> Kek:
    trimmed = text.strip()
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", trimmed) or len(trimmed) % 4:
        raise SecretKeyError("the key file is not base64; it must hold 32 random bytes, base64-encoded")
    try:
        key = base64.b64decode(trimmed, validate=True)
    except binascii.Error as err:
        raise SecretKeyError("the key file is not base64") from err
    if len(key) != KEK_BYTES:
        raise SecretKeyError(f"the key file decodes to {len(key)} bytes, not {KEK_BYTES}")
    return Kek(id=hashlib.sha256(key).hexdigest()[:16], key=key)


def load_kek(path: str) -> Kek:
    return kek_from_base64(Path(path).read_text())


def seal_bytes(key: bytes, plaintext: bytes, context: str) -> bytes:
    iv = os.urandom(IV_BYTES)
    out = AESGCM(key).encrypt(iv, plaintext, _aad(SEAL_VERSION, context))
    ciphertext, tag = out[:-TAG_BYTES], out[-TAG_BYTES:]
    return bytes([SEAL_VERSION]) + iv + tag + ciphertext


def open_bytes(key: bytes, sealed: bytes, context: str) -> bytes:
    if len(sealed) < 1 + IV_BYTES + TAG_BYTES or sealed[0] not in (SEAL_V1, SEAL_VERSION):
        raise SealError("sealed value is malformed or of an unknown version")
    version, iv = sealed[0], sealed[1 : 1 + IV_BYTES]
    tag = sealed[1 + IV_BYTES : 1 + IV_BYTES + TAG_BYTES]
    ciphertext = sealed[1 + IV_BYTES + TAG_BYTES :]
    try:
        return AESGCM(key).decrypt(iv, ciphertext + tag, _aad(version, context))
    except InvalidTag as err:
        raise SealError("sealed value failed authentication (wrong key, or altered)") from err


def open_secret(kek: Kek, envelope: Envelope, aad: str) -> str:
    if envelope.kek_id != kek.id:
        raise SealError(f"secret was sealed with key {envelope.kek_id}, but the mounted key is {kek.id}")
    dek = open_bytes(kek.key, envelope.dek_sealed, f"dek:{aad}")
    return open_bytes(dek, envelope.secret_sealed, aad).decode()
```

  `credentials.py`:
  - `credential_aad` returns `f"ai_credentials:{row_id}:" + json.dumps({"kind": kind,
    "base_url": base_url}, separators=(",", ":"), ensure_ascii=False)`, which is the same as
    `JSON.stringify` for these ASCII values; the gateway vector proves it.
  - `credential_env` mirrors `run.ts` `credentialEnv`.
  - `first_usable` runs
    `SELECT id, kind, base_url, secret_sealed, dek_sealed, kek_id, status, cooldown_until FROM
    ai_credentials WHERE status = 'active' OR (status = 'cooling_down' AND cooldown_until <= now())
    ORDER BY priority`. For each row it picks the KEK whose `id == kek_id`, tries
    `open_secret`, and returns the first row that opens. Otherwise it raises
    `NoUsableCredential("no usable Claude credential: <n> disabled, <n> cooling down, <n> sealed with another key")`.
- [ ] **Step 4: Run, expect PASS** (`uv run --frozen pytest tests/test_secrets.py tests/test_credentials.py -v`), plus ruff and mypy.
- [ ] **Step 5: Commit** `feat(agent-durable): port openSecret and the credential order to Python (#1056)`.

### Task 4: The payload codec, both languages

**Files:**
- Create: `agent/src/db/migrations/<stamp>_payload_keys.sql`, `agent/src/temporal/codec.ts`,
  `agent/src/temporal/payloadKeys.ts`, `agent/test/codec.test.ts`, `agent/test/payloadKeys.pg.test.ts`,
  `agent-durable/scadbuddy_durable/codec.py`, `agent-durable/scadbuddy_durable/payload_keys.py`,
  `agent-durable/tests/test_codec.py`
- Modify: `agent/src/main.ts` (the worker's and the command client's `dataConverter`),
  `agent/test/temporal.worker.test.ts` (the client-side context gate)

**Interfaces:**
- Produces (TS): `isSubject(workflowId: string): boolean` (`/^(session|flow)-[0-9a-f-]{36}$/`);
  `PayloadKeys` (`createKey(subject, tx?)`, `dataKey(subject): Promise<Buffer | undefined>`,
  `forget(subject, tx?)`, with a 60 s cache) as `PgPayloadKeys(sql, keks: {current, previous?})`;
  `SubjectPayloadCodec(keys)` implementing `PayloadCodec` (`encode/decode(payloads, context?)`).
- Produces (Py): `PayloadKeys(pool, keks)` with `async data_key(subject) -> bytes | None`;
  `SubjectPayloadCodec(keys, subject: str | None = None)` implementing
  `temporalio.converter.PayloadCodec` and `WithSerializationContext`;
  `data_converter(keys) -> DataConverter`.
- Migration:

```sql
-- Per-subject data keys for the payload codec (spec 2026-10-01 §6.5). A subject is
-- a workflow id, session-<uuid> or flow-<uuid>. Deleting the row makes every copy of
-- that workflow's payloads (history, Visibility, Archival) undecryptable.
CREATE TABLE ai_payload_keys (
  subject    text PRIMARY KEY CHECK (subject ~ '^(session|flow)-[0-9a-f-]{36}$'),
  dek_sealed bytea NOT NULL,
  kek_id     text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 1: Write the failing tests.**
  - TS `codec.test.ts`:
    - A payload of `session-<uuid>` encodes to `encoding: binary/encrypted` with
      `encryption-key-id`, and decodes back to the original `Payload` (metadata and data).
    - The same holds with `{type: 'activity', workflowId: 'session-…'}`.
    - `render-x`, `print-run-y` and no context pass through unchanged.
    - Encoding a subject with no key throws (fails closed).
    - Decoding after `forget` throws `SealError('payload key for … is gone')`.
    - It decodes the committed payload vector.
  - TS `payloadKeys.pg.test.ts`:
    - `createKey` twice for one subject keeps one row.
    - A key sealed under the previous KEK opens when it is configured as `previous`
      (Review Focus 3).
  - `temporal.worker.test.ts` (extend; the gate, as phase 4 did for activities): a client
    with a spy codec calls `executeUpdateWithStart` on workflow ID `session-<id>`. The spy sees
    `{type: 'workflow', workflowId: 'session-<id>'}` for the start args and the Update args. If
    `@temporalio/client` 1.24.0 passes no context there, **stop and report** (spec §6.5).
  - Py `test_codec.py`:
    - The round trip under `codec.with_context(WorkflowSerializationContext(namespace="n",
      workflow_id="session-…"))` and under
      `ActivitySerializationContext(workflow_id="session-…", …)`.
    - Pass-through for other ids and with no context.
    - Fails closed with no key.
    - A payload encoded by TS opens: the Python test reads the committed
      `secret-vectors.json` `payload` entries through `open_bytes`, and a `requires_postgres`
      test inserts a key row with the vector's `dek_sealed_b64` and decodes a TS-shaped payload
      built from it.

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Python codec core:

```python
class SubjectPayloadCodec(PayloadCodec, WithSerializationContext):
    """Spec §6.5: encrypts the payloads of session-*/flow-* workflows per subject."""

    def __init__(self, keys: PayloadKeys, subject: str | None = None) -> None:
        self._keys = keys
        self._subject = subject

    def with_context(self, context: SerializationContext) -> Self:
        workflow_id = getattr(context, "workflow_id", None)
        subject = workflow_id if isinstance(workflow_id, str) and is_subject(workflow_id) else None
        return type(self)(self._keys, subject)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        if self._subject is None:
            return list(payloads)
        key = await self._keys.data_key(self._subject)
        if key is None:
            raise SealError(f"no payload key for {self._subject}; refusing to write it unencrypted")
        context = f"ai_payload:{self._subject}"
        return [
            Payload(
                metadata={"encoding": b"binary/encrypted", "encryption-key-id": self._subject.encode()},
                data=seal_bytes(key, p.SerializeToString(), context),
            )
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out: list[Payload] = []
        for p in payloads:
            if p.metadata.get("encoding") != b"binary/encrypted":
                out.append(p)
                continue
            subject = p.metadata["encryption-key-id"].decode()
            key = await self._keys.data_key(subject)
            if key is None:
                raise SealError(f"payload key for {subject} is gone")
            decoded = Payload()
            decoded.ParseFromString(open_bytes(key, p.data, f"ai_payload:{subject}"))
            out.append(decoded)
        return out
```

  Decode trusts `encryption-key-id` rather than the context, so a payload that a workflow
  carries from another subject still opens. Each key row is
  `seal_bytes(kek.key, dek, "dek:ai_payload_keys:<subject>")`, unwrapped with the KEK whose
  `id == kek_id`. The TypeScript codec is the same shape over `sealBytes`/`openBytes` and
  `@temporalio/proto`'s `temporal.api.common.v1.Payload.encode/decode`. Wire
  `SubjectPayloadCodec` into `main.ts`: the `AgentWorker`'s `dataConverter.payloadCodecs`, and
  the command client's and the durable client's (Task 12) `Client({ dataConverter })`. Without
  a KEK, the codec stays off, and a durable session cannot be created (Task 5 refuses it).
- [ ] **Step 4: Run, expect PASS**: `pnpm exec vitest run test/codec.test.ts test/payloadKeys.pg.test.ts test/temporal.worker.test.ts`; `uv run --frozen pytest tests/test_codec.py`.
- [ ] **Step 5: Commit** `feat(agent): per-subject payload codec for session and flow workflows (#1056)`.

### Task 5: `mode` in the agent service, and the `session_mode` setting

**Files:**
- Create: `agent/src/routes/sessionMode.ts`, `agent/test/sessionMode.routes.test.ts`
- Modify: `agent/src/sessions/manager.ts` (`SessionRecord.mode`, `StartOptions.mode`, insert,
  `fork` refusal), `agent/src/sessions/protocol.ts` (`SessionSummary.mode`,
  `session.started.mode?`), `agent/src/sessions/clientProtocol.ts` (`user.message.mode?`),
  `agent/src/routes/chat.ts`, `agent/src/routes/sessions.ts` (`StartBody.mode`, `SessionView.mode`),
  `agent/src/tools/sessions.ts` (`sessions_start` `mode`), `agent/test/chat.pg.test.ts`,
  `agent/test/sessions.routes.test.ts` (or the existing sessions route test), `agent/test/mcpSessions.test.ts`

**Interfaces:**
- Consumes: `PayloadKeys.createKey(subject, tx)` (Task 4).
- Produces: `SessionMode = 'classic' | 'durable'`; `SETTING_SESSION_MODE = 'session_mode'`;
  `SessionRecord.mode: SessionMode`; `StartOptions.mode?: SessionMode`; `SessionError` code
  `'unsupported'`; `SESSION_MODE_PATH = '/api/v1/ai/settings/session-mode'` with
  `GET → {mode}` and `PUT {mode} → {mode}`.

- [ ] **Step 1: Write the failing tests.**
  - `start` with `mode: 'durable'` stores `mode = 'durable'` and inserts the
    `ai_payload_keys` row for `session-<id>` in the same transaction (when the insert of the
    key fails, no session row exists).
  - `start` without `mode` uses `session_mode` (`'durable'` stored) and falls back to
    `classic` when the setting is unset or invalid.
  - `start` with `mode: 'durable'` and no KEK refuses with `invalid`: "durable sessions need
    SCADBUDDY_SECRET_KEY_FILE".
  - The chat socket's `user.message` with `sessionId` and `mode` answers an `error` event
    with code `invalid`, and nothing is sent.
  - `POST /api/v1/ai/sessions {mode}` is accepted, and `POST /sessions/:id/messages` with
    `mode` is a 400 (strict body).
  - `sessions_start` takes `mode`.
  - `sessions.snapshot` and `GET /sessions/:id` carry `mode`.
  - `fork` of a durable session refuses with `unsupported`.
  - The session-mode route: GET defaults to `classic`; PUT `durable` stores it; PUT `turbo`
    is a 400; writes need the UI origin (`uiRequestProblem`) and reads the read guard; 503
    without a database. Model the tests on `sessionLimits`' tests.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `insert` adds the `mode` column. For durable, it opens a
  transaction that inserts the row, then calls `payloadKeys.createKey('session-' + id, tx)`.
  The route module copies `sessionLimits.ts`'s shape: `registerSessionModeRoutes`,
  `route: RouteModule`, the PUT audited as `UI_ACTOR` the way session limits are.
  `clientProtocol.ts` adds `mode: z.enum(['classic','durable']).optional()` to `user.message`.
  `chat.ts` passes it to `start`, and refuses it with a `sessionId`.
- [ ] **Step 4: Run, expect PASS**: `pnpm exec vitest run test/sessionMode.routes.test.ts test/chat.pg.test.ts test/mcpSessions.test.ts test/sessions*.test.ts`; `pnpm lint && pnpm typecheck`.
- [ ] **Step 5: Commit** `feat(agent): a session's mode, chosen at start, with a default setting (#1056)`.

### Task 6: The Postgres `SessionStore`, and the durable bookkeeping tables

**Files:**
- Create: `agent/src/db/migrations/<stamp>_durable_sessions.sql`,
  `agent-durable/scadbuddy_durable/store.py`, `agent-durable/scadbuddy_durable/segments.py`,
  `agent-durable/tests/test_store.py`, `agent-durable/tests/test_segments.py`

**Interfaces:**
- Produces: `Snapshots(pool)` with `async save(inp: SnapshotInput) -> None` and `async
  latest(session_id) -> SnapshotRow | None`; the activity `save_snapshot(inp: SnapshotInput)`
  (`SAVE_SNAPSHOT = "durable_save_snapshot"`), defined in `segments.py` and registered by the
  worker.
- Produces: `PostgresSessionStore(pool)` implementing `claude_agent_sdk.SessionStore`;
  `Segments(pool)` with `async record(session_id: str, segment_index: int, attempt: int,
  claude_session_id: str, cost_usd: float) -> None` and `async limits(session_id) ->
  SessionLimits(budget_usd: float, cost_usd: float, max_turns: int)`.
- Migration:

```sql
-- Durable sessions (spec 2026-10-01 §6.2), written by the agent-durable sidecar.
-- One row per segment attempt: its cost (every attempt really spent it) and the
-- Claude session id it ran in, so forgetSubject finds every ai_session_entries key.
CREATE TABLE ai_durable_segments (
  session_id        uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  segment_index     integer NOT NULL,
  attempt           integer NOT NULL,
  claude_session_id text NOT NULL,
  cost_usd          double precision NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, segment_index, attempt)
);
-- The projector's place in each session's live output, and its lease (one
-- follow_agent subscriber per session across replicas).
CREATE TABLE ai_durable_streams (
  session_id  uuid PRIMARY KEY REFERENCES ai_sessions (id) ON DELETE CASCADE,
  next_offset bigint NOT NULL DEFAULT 0,
  holder      text,
  lease_until timestamptz
);
-- The plugin's AgentState as of the latest segment or tool call (plan ruling 15), so an
-- execution that closed without handing over (terminated, failed) can be resumed.
CREATE TABLE ai_durable_snapshots (
  session_id uuid PRIMARY KEY REFERENCES ai_sessions (id) ON DELETE CASCADE,
  version    bigint NOT NULL,
  state      text NOT NULL,
  in_flight  jsonb NOT NULL DEFAULT '[]',
  saved_at   timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 1: Write the failing tests** (`requires_postgres`).
  - `test_store.py` runs the SDK's own conformance suite against the store:
    `await run_session_store_conformance(make_store)`
    (`claude_agent_sdk.testing.session_store_conformance`, v0.2.160: the 14 behavioural
    contracts, optional methods included). `make_store` truncates `ai_session_entries` and
    returns a new `PostgresSessionStore(pool)`.
  - `test_store.py`, cross-language: a row written by this store reads back through the
    TypeScript encoding. `entry` is the `json.dumps(entry, separators=(",", ":"),
    ensure_ascii=False)` text, the `uuid` column holds `entry["uuid"]`, and a duplicate `uuid`
    in one session is ignored.
  - `test_store.py`: `load` and `delete` key by `(project_key, session_id, subpath)`, as the
    SDK's contract and its Postgres example do. The TypeScript store ignores `project_key`
    because classic sessions each have their own cwd. Every durable worker shares `/srv/agent`,
    so the Python store needs no such departure, and only the Python store reads durable
    transcripts.
  - `test_store.py`: `delete` of the main key removes the subpaths too.
  - `test_segments.py`:
    - Recording attempts 1 and 2 of segment 0 at 0.01 each and attempt 1 of segment 1 at
      0.02 sets `ai_sessions.cost_usd = 0.04` and `turns = 2`.
    - Recording the same `(session, segment, attempt)` twice keeps one row (the activity
      retried after its write).
    - `limits` reads the row.
    - `Snapshots(pool).save(SnapshotInput)` at versions 3, then 5, then 4 leaves version 5.
      `latest(session_id)` returns `(state_json, in_flight, saved_at)`.
    - An `AgentState` with `pending` `ToolOutcome`s saved and read back through
      `payload_converter.from_payloads(..., [AgentState])` equals the original (the
      round-trip verified for the plan).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `store.py` from the SDK's `examples/session_stores/postgres_session_store.py`.
  - Adapt it to `ai_session_entries`: `entry` is JSON **text** (jsonb would reject `\u0000`,
    as the TypeScript store notes), `id bigserial` gives the order, and the insert ends with
    `ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING`.
  - `list_sessions` returns `mtime` as epoch milliseconds.
  - Mirror the header comment of `agent/src/sessions/store.ts`, citing both stores.
  - `segments.py`: insert `ON CONFLICT DO NOTHING`, then in the same transaction
    `UPDATE ai_sessions SET cost_usd = (SELECT coalesce(sum(cost_usd),0) FROM
    ai_durable_segments WHERE session_id = $1), turns = (SELECT count(DISTINCT segment_index)
    …), updated_at = now() WHERE id = $1`.
- [ ] **Step 4: Run, expect PASS**, plus ruff and mypy.
- [ ] **Step 5: Commit** `feat(agent-durable): the session store on ai_session_entries and segment accounting (#1056)`.

### Task 7: Tools as `activity_as_tool` stubs, and the durable prompt

**Files:**
- Modify: `agent/src/export-tools.ts` (also writes `dist/durable-prompt.txt`), `agent/package.json`
  (`build`), `agent/test/manifest.test.ts`
- Create: `agent-durable/scadbuddy_durable/tools.py`, `agent-durable/tests/test_tools.py`,
  `agent-durable/tests/fixtures/tools.json` (a three-tool sample: one `read`, one `write`, one `outward`)
- Modify: `agent-durable/tests/conftest.py`. At import, before any `scadbuddy_durable` import,
  it sets `os.environ.setdefault("SCADBUDDY_AGENT_TOOLS_MANIFEST", <fixtures/tools.json>)`, so
  the workflow tests see the sample tools.

**Interfaces:**
- Consumes: `agent/dist/tools.json` (`[{name, description, input_schema, tier}]`, phase 4).
- Produces: `TOOLS_MANIFEST_ENV = "SCADBUDDY_AGENT_TOOLS_MANIFEST"` (default
  `/app/agent-durable/tools.json`); `load_manifest(path) -> list[ManifestEntry]`;
  `durable_tools(entries) -> list[DurableTool]`; `tiers(entries) -> dict[str, str]`;
  `TOOLS: list[DurableTool]` and `TIERS` (loaded once at import, from the env path).
  `dist/durable-prompt.txt` holds `UNTRUSTED_CONTENT_POLICY`.

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_tools.py
from pathlib import Path

from scadbuddy_durable.tools import durable_tools, load_manifest
from temporalio import activity

SAMPLE = Path(__file__).parent / "fixtures/tools.json"


def test_each_tool_is_a_named_stub_on_agent_tools() -> None:
    entries = load_manifest(str(SAMPLE))
    tools = {t.name: t for t in durable_tools(entries)}
    assert set(tools) == {e.name for e in entries}
    for e in entries:
        t = tools[e.name]
        assert t.task_queue == "agent-tools"
        assert t.description == e.description and t.input_schema == e.input_schema
        assert t.needs_approval is (e.tier == "outward")
        defn = activity._Definition.from_callable(t.activity)  # the name Temporal resolves
        assert defn is not None and defn.name == e.name


def test_stubs_are_distinct_callables() -> None:
    tools = durable_tools(load_manifest(str(SAMPLE)))
    assert len({id(t.activity) for t in tools}) == len(tools)
```

  The `agent/test/manifest.test.ts` extension checks that every name matches
  `^[A-Za-z0-9_-]{1,50}$`, the plugin's rule, so no tool is silently uncallable. It also
  checks that `writeDurablePrompt(path)` writes `UNTRUSTED_CONTENT_POLICY` exactly.

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `tools.py`.**

```python
"""Every ALL_TOOLS entry as an activity_as_tool stub (spec §6.3). The TypeScript
agent-tools worker serves each activity by name; _remote never runs here."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.claude_agent_sdk import DurableTool, activity_as_tool

TOOLS_MANIFEST_ENV = "SCADBUDDY_AGENT_TOOLS_MANIFEST"
TOOL_QUEUE = "agent-tools"


@dataclass(frozen=True)
class ManifestEntry:
    name: str
    description: str
    input_schema: dict[str, Any]
    tier: str


def load_manifest(path: str) -> list[ManifestEntry]:
    raw = json.loads(open(path, encoding="utf-8").read())
    return [ManifestEntry(e["name"], e["description"], e["input_schema"], e["tier"]) for e in raw]


def _stub(name: str) -> Any:
    async def _remote(args: dict[str, Any]) -> Any:
        raise RuntimeError(f"{name} is served by the agent-tools worker, not agent-durable")

    _remote.__name__ = f"remote_{name}"
    return activity.defn(name=name)(_remote)


def durable_tools(entries: list[ManifestEntry]) -> list[DurableTool]:
    return [
        activity_as_tool(
            _stub(e.name),
            name=e.name,
            description=e.description,
            input_schema=e.input_schema,
            needs_approval=e.tier == "outward",
            task_queue=TOOL_QUEUE,
            # Renders and print starts answer within their own deadlines (§4.2); a tool
            # that takes longer answers with an operation the model follows.
            start_to_close_timeout=timedelta(minutes=2),
        )
        for e in entries
    ]


def tiers(entries: list[ManifestEntry]) -> dict[str, str]:
    return {e.name: e.tier for e in entries}


_PATH = os.environ.get(TOOLS_MANIFEST_ENV, "/app/agent-durable/tools.json")
# Loaded once per process (the workflow imports it passed through the sandbox). A
# missing manifest leaves no tools; worker.main() refuses to start with none.
_ENTRIES = load_manifest(_PATH) if os.path.exists(_PATH) else []
TOOLS = durable_tools(_ENTRIES)
TIERS = tiers(_ENTRIES)
```

  Retries: the plugin's default `retry_policy=None` is Temporal's default retry. The phase 4
  activity makes tool errors non-retryable (`ToolError`), so only infrastructure failures
  retry.
- [ ] **Step 4: Run, expect PASS** (`uv run --frozen pytest tests/test_tools.py`; `pnpm exec vitest run test/manifest.test.ts`; `pnpm build && test -s dist/durable-prompt.txt`).
- [ ] **Step 5: Commit** `feat(agent-durable): every tool as an activity_as_tool stub on agent-tools (#1056)`.

### Task 8: `SessionRunner`, the per-segment credential and budget

**Files:**
- Create: `agent-durable/scadbuddy_durable/runner.py`, `agent-durable/plugin/.claude-plugin/plugin.json`,
  `agent-durable/plugin/skills` (a symlink to `../../plugins/scadbuddy/skills`, replaced by files in
  the image as the agent stage does), `agent-durable/tests/test_runner.py`

**Interfaces:**
- Consumes: `CredentialSource.first_usable` (Task 3), `Segments.record/limits` (Task 6),
  `PostgresSessionStore` (Task 6).
- Produces: `CWD = "/srv/agent"`; `session_of(workflow_id: str) -> str` (raises
  `ApplicationError(non_retryable=True)` for anything but `session-<uuid>`);
  `SessionRunner(credentials, segments, store, *, plugin_dir, prompt_append, cwd=CWD,
  runner_factory=ClaudeAgentSdkRunner)` implementing `SegmentRunner`;
  `extra_options(plugin_dir, prompt_append) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests.** These are unit tests with a fake `runner_factory`
  that records its kwargs and returns a stub with `run`. Activity info comes from
  `temporalio.testing.ActivityEnvironment` with `workflow_id="session-<uuid>"`.
  - The factory gets `env == credential_env(first_usable())`,
    `max_budget_usd == budget - cost`, `session_store is store`, `cwd == "/srv/agent"`, and
    `extra_options == {"plugins": [{"type": "local", "path": plugin_dir}], "system_prompt":
    {"type": "preset", "preset": "claude_code", "append": prompt_append}}`.
  - After `run`, `segments.record(session, inp.segment_index, attempt, out.session_id,
    out.cost_usd)` was called.
  - A spent budget returns `SegmentOutput(is_error=True, error="the session's budget is
    spent")` and builds no runner.
  - `NoUsableCredential` raises a non-retryable `ApplicationError` whose message is the
    reason and does not contain the secret.
  - A workflow ID `flow-…` or `x` is refused non-retryably.
  - The secret never appears in `caplog` for a run that fails (the factory raises).
  - `extra_options` passes the plugin's own `_check_extra_options` (import it from
    `temporalio.claude_agent_sdk._runner` in the test only).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**

```python
class SessionRunner:
    """The plugin's SegmentRunner (deviation 3): one ClaudeAgentSdkRunner per segment,
    with that session's credential env and remaining budget."""

    def __init__(self, credentials: CredentialSource, segments: Segments, store: SessionStore, *,
                 plugin_dir: str, prompt_append: str, cwd: str = CWD,
                 runner_factory: Callable[..., SegmentRunner] = ClaudeAgentSdkRunner) -> None:
        self._credentials, self._segments, self._store = credentials, segments, store
        self._extra = extra_options(plugin_dir, prompt_append)
        self._cwd, self._factory = cwd, runner_factory

    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
        session_id = session_of(activity.info().workflow_id)
        limits = await self._segments.limits(session_id)
        remaining = limits.budget_usd - limits.cost_usd
        if remaining <= 0:
            return SegmentOutput(session_id=inp.session_id, is_error=True, error="the session's budget is spent")
        try:
            credential = await self._credentials.first_usable()
        except NoUsableCredential as err:
            raise ApplicationError(str(err), non_retryable=True) from None
        with warnings.catch_warnings():
            # The runner warns when none of its ENV_AUTH names is set; a gateway's
            # ANTHROPIC_AUTH_TOKEN is not among them and survives resumes too.
            warnings.simplefilter("ignore")
            runner = self._factory(session_store=self._store, cwd=self._cwd, env=credential_env(credential),
                                   extra_options=self._extra, max_budget_usd=remaining)
        out = await runner.run(inp, attempt)
        await self._segments.record(session_id, inp.segment_index, attempt, out.session_id, out.cost_usd)
        return out
```

  `plugin.json` is the agent's own manifest without the `agents` mention, with the
  description "ScadBuddy's skills for a durable session: no subagents, hooks or MCP servers".
- [ ] **Step 4: Run, expect PASS**, plus ruff and mypy.
- [ ] **Step 5: Commit** `feat(agent-durable): a segment runner with the session's credential and budget (#1056)`.

### Task 9: The `DurableSession` workflow

**Files:**
- Create: `agent-durable/scadbuddy_durable/workflow.py`, `agent-durable/scadbuddy_durable/models.py`,
  `agent-durable/tests/test_workflow.py`

**Interfaces:**
- Consumes: `TOOLS` (Task 7).
- Produces (`models.py`): `SessionInput(session_id: str, max_turns: int,
  approval_expiry_seconds: int, model: str | None = None, restored: Restored | None = None)`;
  `InFlight(id: str, name: str, status: str)`; `Restored(in_flight: list[InFlight])`;
  `SnapshotInput(session_id: str, state: AgentState, in_flight: list[InFlight], version: int)`;
  `restore_state(state: AgentState, in_flight: list[InFlight]) -> AgentState` (Ruling 15); `Message(text: str, context: str |
  None = None)`; `WORKFLOW_NAME = "DurableSession"`; `TASK_QUEUE = "agent"`;
  `SEND_UPDATE = "send_message"`; `REVIEW_UPDATE = "review"`; `PENDING_QUERY = "pending_approvals"`;
  `DECISIONS_QUERY = "decisions"`; `EXPIRED_BY = "system:expired"`; `render_prompt(m: Message) -> str`.
- Produces (`workflow.py`): `DurableSession` with `run(inp, state=None, inbox=None)`,
  Update `send_message(Message) -> None` with a validator, Update `review(tool_use_id: str,
  approved: bool, approver: str) -> None` with a validator, Query `pending_approvals() ->
  list[dict]`, Query `decisions() -> dict[str, str]` (call id → `decided_by`).

- [ ] **Step 1: Write the failing tests** (`requires_temporal`). They use `ScriptedClaude` from
  `temporalio.claude_agent_sdk.testing` with policies, plus stub activities named like the
  sample tools, on a second worker polling `agent-tools`. Policies return a durable call or a
  final answer; read `tests/refund/policy.py` at the pin for the policy signature.
  1. A message is answered: update-with-start `send_message` → `done` with the policy's text.
     A second message continues the same Claude session (the policy sees the earlier turn).
  2. A busy refusal: a second `send_message` while the first runs is refused by the
     validator with "the session is busy", and nothing reaches history (the update fails with
     `WorkflowUpdateFailedError` and the history has no `WorkflowExecutionUpdateAccepted`
     for it).
  3. An empty message is refused.
  4. Approve: the policy calls the outward tool. `pending_approvals` lists it. `review(id,
     True, "browser:browser")` runs the stub activity once, and `decisions()[id] ==
     "browser:browser"`.
  5. Deny: the stub never runs, and the policy sees "A human reviewer rejected this action".
  6. Expiry: with `approval_expiry_seconds=2` and time skipping (or a real 2 s), the call is
     rejected, and `decisions()[id] == EXPIRED_BY`.
  7. Review Focus 2: `review` after expiry is refused by the validator ("already decided" or
     "no tool call … is waiting"), and the stub never ran. `review` twice: the second is
     refused, and the stub ran once.
  8. Continue-As-New with the inbox: with `continue_as_new_after_events` reached between
     messages (use a small test-only `SessionInput` field, or patch
     `should_continue_as_new` via a subclass in the test), the next run keeps the
     conversation and a message waiting in the inbox is still answered.
  9. Parallel calls: a policy that returns two durable calls in one message (deviation 1) runs
     both stubs and delivers both results.
  10. Stop and resume (deviation 4). ScriptedClaude runs with a `state_dir`, which is its
      session-store mode.
      - (a) Cancel while a stub tool activity runs. The execution completes, and its result is
        an `AgentState` with the same `session_id`, a `checkpoint`, `fork_next` true, and
        `pending[call_id]` "…interrupted…".
      - (b) Cancel while an outward call waits for approval. `pending[call_id]` is "This tool
        call did not run: the Workflow was cancelled.", and the stub never ran.
      - (c) Cancel while idle. The result keeps the session's `session_id` and `checkpoint`.
      - (d) For each case, start a new execution on the same ID with `[input, <result>,
        None]` and send a message. The policy's view of the conversation holds the earlier
        turns, so the model sees the earlier history, and the owed error result arrives with
        the new prompt. A call id from before the cancel can never run again.
      - (e) Starting with `state=None` after a cancel begins a new Claude session (a different
        `session_id`). This pins the gap: the store alone does not resume.
  12. Snapshots and restore (Ruling 15).
      - (a) After a message with one tool call, `save_snapshot` was called with
        non-decreasing versions. The last saved state equals `agent.state()` at the end, and
        a snapshot taken while the stub ran lists it in `in_flight` as `started`.
      - (b) Terminate the execution while the stub runs (`handle.terminate()`). Take the
        latest snapshot from the stub activity's recorder, and start a new execution with
        `SessionInput(restored=Restored(in_flight))` and that state. Send a message. The
        policy sees the earlier turns and the error result "…interrupted…" for the stubbed
        call, and the old id is in `recent_call_ids`.
      - (c) The same, with the call waiting for approval: "did not run", and the stub never
        ran.
      - (d) `restore_state` unit tests (no Temporal):
        - in-flight `started` gives "interrupted…", and `waiting for approval` gives "did
          not run…";
        - `recent_call_ids` is capped at 256, with the newest kept;
        - `checkpoint is None` clears `session_id`;
        - the input state is not mutated.
  13. Replay: record the history of test 4 into
      `agent-durable/tests/histories/approve.json`, and replay it with `Replayer` in the
      test, so a later change to the workflow fails replay (TMPRL1100).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**

```python
with workflow.unsafe.imports_passed_through():
    from .tools import TOOLS
from temporalio.claude_agent_sdk import AgentState, DurableClaudeAgent
from temporalio.exceptions import FailureError


@workflow.defn(name=WORKFLOW_NAME)
class DurableSession:
    @workflow.init
    def __init__(self, inp: SessionInput, state: AgentState | None = None,
                 inbox: list[Message] | None = None) -> None:
        if state is not None and inp.restored is not None:
            state = restore_state(state, inp.restored.in_flight)  # Ruling 15
            inp = dataclasses.replace(inp, restored=None)  # Continue-As-New must not restore again
        self._inp = inp
        self._inbox: list[Message] = list(inbox or [])
        self._timed: set[str] = set()
        self.agent = DurableClaudeAgent(
            tools=TOOLS,
            model=inp.model,
            max_turns=inp.max_turns,
            builtin_tools=["Skill"],      # §6.3b: skills only
            tool_activities=(),           # no Bash, no MCP server: nothing runs as a tool step
            max_segments=None,            # a chat; the budget is the limit (ruling 3)
            state=state,
            auto_continue_as_new=True,
            continue_as_new_args=lambda s: [self._inp, s, self._inbox],
            live_output=True,
        )

    @workflow.run
    async def run(self, inp: SessionInput, state: AgentState | None = None,
                  inbox: list[Message] | None = None) -> AgentState:
        asyncio.create_task(self._expire_approvals())
        asyncio.create_task(self._snapshots())
        try:
            while True:
                prompt: str | None = None
                if not self.agent.busy:
                    await workflow.wait_condition(lambda: bool(self._inbox) or self.agent.should_continue_as_new())
                    if not self._inbox:
                        await self.agent.continue_as_new()
                    prompt = render_prompt(self._inbox.pop(0))
                try:
                    await self.agent.run(prompt)
                except FailureError:
                    pass  # the agent published `error`; the session takes the next message
        except asyncio.CancelledError:
            # Stop (deviation 4). agent.run() already ended the task (_end_task: owed error
            # results in pending, fork_next) and published `cancelled`. Hand the state over as
            # this execution's result, so the next message's execution resumes the same
            # Claude session from the SessionStore at the checkpoint.
            return self.agent.state()

    @workflow.update(name=SEND_UPDATE)
    def send_message(self, message: Message) -> None:
        self._inbox.append(message)

    @send_message.validator
    def check_message(self, message: Message) -> None:
        if not message.text.strip():
            raise ValueError("the message is empty")
        if self.agent.busy or self._inbox:
            raise ValueError("the session is busy")

    @workflow.update(name=REVIEW_UPDATE)
    def review(self, tool_use_id: str, approved: bool, approver: str) -> None:
        self.agent.decide(tool_use_id, approved, approver)

    @review.validator
    def check_review(self, tool_use_id: str, approved: bool, approver: str) -> None:
        self.agent.validate_decision(tool_use_id, approver)  # refused Updates never reach history

    @workflow.query(name=PENDING_QUERY)
    def pending_approvals(self) -> list[dict[str, Any]]:
        return self.agent.pending_approvals()

    @workflow.query(name=DECISIONS_QUERY)
    def decisions(self) -> dict[str, str]:
        return {c["id"]: c["decided_by"] for c in self.agent.tool_calls if c.get("decided_by")}

    async def _expire_approvals(self) -> None:
        """Spec §6.4: approval_expiry_seconds becomes a timer per waiting call."""
        def new() -> list[str]:
            return [p["id"] for p in self.agent.pending_approvals() if p["id"] not in self._timed]
        while True:
            await workflow.wait_condition(lambda: bool(new()))
            for call_id in new():
                self._timed.add(call_id)
                asyncio.create_task(self._expire(call_id))

    async def _snapshots(self) -> None:
        """Ruling 15: save AgentState whenever a segment commits or a tool call moves."""
        def mark() -> tuple[Any, ...]:
            return (self.agent.segments, self.agent.total_tool_calls,
                    tuple((c["id"], c["status"]) for c in self.agent.tool_calls))
        last: tuple[Any, ...] | None = None
        while True:
            await workflow.wait_condition(lambda: mark() != last)
            last = mark()
            in_flight = [InFlight(c["id"], c["name"], c["status"]) for c in self.agent.tool_calls
                         if c["status"] in ("started", "waiting for approval")]
            await workflow.execute_local_activity(
                SAVE_SNAPSHOT,
                SnapshotInput(self._inp.session_id, self.agent.state(), in_flight,
                              self.agent.segments + self.agent.total_tool_calls),
                start_to_close_timeout=timedelta(seconds=10),
            )

    async def _expire(self, call_id: str) -> None:
        await workflow.sleep(self._inp.approval_expiry_seconds)
        if any(p["id"] == call_id for p in self.agent.pending_approvals()):
            self.agent.decide(call_id, False, EXPIRED_BY)
```

  `render_prompt` returns `text` when there is no context, else `f"{text}\n\n{context}"`.
  The page context is model-only, as in classic, and `user.turn` (written by the agent
  service) shows `text` alone. `models.py` holds plain dataclasses, safe in the sandbox.
- [ ] **Step 4: Run, expect PASS**, plus ruff and mypy.
- [ ] **Step 5: Commit** `feat(agent-durable): the DurableSession workflow with review and approval expiry (#1056)`.

### Task 10: The event projection into `ai_session_events`

**Files:**
- Create: `agent-durable/scadbuddy_durable/translate.py`, `agent-durable/scadbuddy_durable/projector.py`,
  `agent-durable/tests/test_translate.py`, `agent-durable/tests/test_projector.py`

**Interfaces:**
- Consumes: `TIERS` (Task 7), `DECISIONS_QUERY`, `EXPIRED_BY` (Task 9),
  `scadbuddy_durable.models.WORKFLOW_NAME`.
- Produces (`translate.py`, pure): `durable_approval_id(session_id, tool_use_id) -> str`;
  `Translator(session_id, tiers)` with `feed(event: dict, *, decided_by: str | None = None) ->
  Batch(events: list[dict], status: str | None, final: bool)`; `bus_kind_of(events) ->
  tuple[str, str | None]` (port of `busKindOf`).
- Produces (`projector.py`): `Projector(pool, client, *, holder: str, lease_s=20, renew_s=5,
  poll_s=0.5)` with `async run(stop: asyncio.Event)`; `append_batch(conn, session_id, batch,
  next_offset)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_translate.py` (pure, table-driven), each case an input event list and the expected
    panel events (all with `"v": 1`):
    - `text` ×2 (segment 0, attempt 1) then `tool_call` gives two
      `assistant.text.delta`/`.done` pairs with ids `"<session>-<offset>"`, then `tool.call`
      with `risk` from `TIERS` (`outward` for an unknown name).
    - `text` (seg 0, att 1), `retry` (seg 0, att 2), `text` (seg 0, att 2), `done`: only
      attempt 2's text is emitted (ruling 10), then `session.result` and
      `session.status idle`; `final` is True.
    - `approval_needed` gives `approval.required{id: "durable:<s>:<id>", tool: <id>,
      summary: json input capped at 500, risk: "outward"}` and status `waiting_approval`.
    - `tool_result` with `status: "done"` after it gives `tool.result{ok: true, summary:
      "done"}` and status `running`.
    - `tool_result` `rejected` with `decided_by=EXPIRED_BY` gives `approval.resolved{approved:
      false}` without `by`, then `tool.result{ok: false}`.
    - `rejected` decided by a person gives no `approval.resolved`, because the route wrote it
      (ruling 7).
    - `error` gives a flush, `error{sessionId, message}` and status `idle`, final.
    - `cancelled` after an `approval_needed` with no `tool_result` gives
      `approval.resolved{id, approved: false, reason: "the turn was stopped"}` for each open
      approval, then status `idle`, final.
    - `prompt` and `continued_as_new` give nothing.
    - `bus_kind_of` matches TypeScript's `busKindOf` for the same five shapes.
  - `test_projector.py` (`requires_postgres`, `requires_temporal`, `ScriptedClaude`):
    - A durable session row with status `running` and a workflow that answers produce, in
      `ai_session_events`, the translated sequence in `seq` order. `ai_sessions.status` ends
      `idle`, `ai_durable_streams.next_offset` is past the last offset, and a LISTEN on
      `scadbuddy_events` receives a `session.done` with `replica` = the holder.
    - Review Focus 4: the test lets the projector commit one batch, cancels its follower
      task, starts a second projector with another holder after the lease ends (`lease_s=1`),
      and asserts that the full event list has no repeated `(type, id)` and nothing missing
      against a single-projector run.
    - Two projectors at once: only one holds the lease, and events are not doubled.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `append_batch` is one transaction. It runs the `EventLog.append` SQL from
    `agent/src/sessions/eventLog.ts`: update `event_seq` and `RETURNING base`, then insert
    `unnest(texts) WITH ORDINALITY`, with `status = coalesce($status, status)` in the same
    `UPDATE`. It then runs `UPDATE ai_durable_streams SET next_offset = $n WHERE session_id =
    $s AND holder = $me` (zero rows means the lease was lost: roll back and stop following).
    After commit it sends `SELECT pg_notify('scadbuddy_events', $payload)` with
    `{id: uuid4().hex, at, kind, session_id, seq: last, status?, replica: holder}`.
  - `run` loops every `poll_s`:
    - It claims `INSERT INTO ai_durable_streams (session_id, holder, lease_until) SELECT id, $me,
      now() + $lease FROM ai_sessions WHERE mode = 'durable' AND status IN ('running',
      'waiting_approval') ON CONFLICT (session_id) DO UPDATE SET holder = $me, lease_until =
      excluded.lease_until WHERE ai_durable_streams.lease_until IS NULL OR
      ai_durable_streams.lease_until < now() OR ai_durable_streams.holder = $me RETURNING
      session_id, next_offset`.
    - It starts one follower task per newly claimed session and renews held leases every
      `renew_s`.
  - A follower iterates `follow_agent(client, "session-<id>", from_offset=next_offset)`.
  - For a `tool_result` with status `rejected`, the follower first queries `decisions` for
    `decided_by`.
  - The follower releases (`holder = NULL, lease_until = NULL`) on `final`, and ends.
  - A follower whose workflow does not exist yet (the Update has not started it) retries after
    `poll_s`.
- [ ] **Step 4: Run, expect PASS**, plus ruff and mypy.
- [ ] **Step 5: Commit** `feat(agent-durable): project live output into ai_session_events (#1056)`.

### Task 11: The worker process, and the real engine against the fake endpoint

**Files:**
- Create: `agent-durable/scadbuddy_durable/worker.py` (`main()`, run as `python -m scadbuddy_durable.worker`),
  `agent/test/support/fakeAnthropicServer.ts`, `agent-durable/tests/fake_anthropic.py`,
  `agent-durable/tests/test_engine.py`, `agent-durable/tests/test_worker.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `python -m scadbuddy_durable.worker`; `build_worker(client, deps) -> Worker`;
  `/healthz` on `health_port` → `{"status": "ok"|"starting"|"unavailable", "temporal": …,
  "database": …}`. `fakeAnthropicServer.ts` is a CLI:
  `node agent/test/support/fakeAnthropicServer.ts <script.json>`. It prints `{"url": …}` as
  its first stdout line, and serves the replies of the script in order (a list of `Reply`).
  `GET /__requests` returns the recorded requests. Node 24 runs `.ts` with type stripping.
  `fakeAnthropic.ts` uses only erasable syntax, and this file must too.

- [ ] **Step 1: Write the failing tests.**
  - `test_worker.py`:
    - `build_worker` registers exactly the workflow `DurableSession` and the activities
      `run_claude_segment` and `durable_save_snapshot`, and no tool stub (those are served by TypeScript).
    - The worker's client has the `SubjectPayloadCodec`.
    - `/healthz` answers `starting` before Temporal connects and `ok` after.
    - SIGTERM calls `worker.shutdown()` and the projector stops within 10 s.
  - `test_engine.py` (`requires_engine`, `requires_postgres`, `requires_temporal`) runs the
    real `ClaudeAgentSdkRunner` path:
    - Start the fake with the script
      `[{toolUse: {name: "mcp__durable__get_settings", input: {}}}, {text: "Settings read."}]`.
    - Insert a `gateway` credential whose `base_url` is the fake's URL, sealed with a test KEK.
    - Insert a durable session row with its payload key.
    - Run the worker with a stub `get_settings` activity on `agent-tools`.
    - Send `send_message`.
    - Assert that `done` is published with "Settings read." and the stub ran once.
    - Assert that the first `/v1/messages` request's `tools` names are exactly the
      `mcp__durable__*` tools plus `Skill`: §3.2's assertion that no built-in but `Skill` is
      enabled. Assert that `ai_durable_segments` has rows with the session's Claude session id.
    - Assert that `ai_session_entries` holds that session's transcript.
    - Assert that the gateway token is absent from the worker's captured logs and from the
      workflow history JSON (`client.get_workflow_handle(...).fetch_history()` →
      `to_json()`), since the history is ciphertext and the token is not in the env either.
  - `fake_anthropic.py` is a pytest fixture that spawns `node <repo>/agent/test/support/fakeAnthropicServer.ts`
    with a temp script, reads the URL line, and kills it at teardown. It skips the test when
    `node` is missing.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** In `worker.py`, `main()` does the following:
  - Refuse to start (exit 1, "no tool manifest at …") when `TOOLS` is empty.
  - Load config. Without a database or Temporal address, serve `/healthz` with
    `{"status": "ok", "durable": "disabled (<reason>)"}`, the degraded mode the CI container
    check asserts.
  - Load the KEKs (current, previous).
  - Open a psycopg `AsyncConnectionPool`.
  - `Client.connect(address, namespace=…, data_converter=data_converter(keys))`, retrying
    with backoff while `/healthz` says `starting`.
  - Build `Worker(client, task_queue=TASK_QUEUE, workflows=[DurableSession],
    plugins=[ClaudeAgentPlugin(SessionRunner(...))], max_concurrent_activities=4)`. The README
    puts about 270 MB per segment; 4 caps the sidecar near 1.1 GB, and the limit goes in the
    clusters section.
  - Run the worker and `Projector(...).run(stop)` together.
  - On SIGTERM: `stop.set()`, then `await worker.shutdown()` with
    `graceful_shutdown_timeout=timedelta(seconds=10)`.
  - The health server is `asyncio.start_server` with a minimal HTTP/1.1 response; no
    framework is needed.
- [ ] **Step 4: Run, expect PASS**: `uv run --frozen pytest -m "not requires_engine"` locally, then the engine test with Postgres, Temporal and node available.
- [ ] **Step 5: Commit** `feat(agent-durable): the worker process, tested with the real engine and the fake endpoint (#1056)`.

### Task 12: Durable sessions in the agent service

**Files:**
- Create: `agent/src/durable/client.ts`, `agent/test/durable.client.test.ts`,
  `agent/test/durable.temporal.test.ts`, `agent/test/support/fakeDurable.ts`
- Modify: `agent/src/sessions/manager.ts` (`send`, `interrupt`, `start` → durable path),
  `agent/src/approvals/service.ts` (`durable:` ids, `authorizeSession`),
  `agent/src/routes/approvals.ts` (list for a durable session), `agent/src/main.ts`,
  `agent/test/approvals.pg.test.ts`, `agent/test/chat.pg.test.ts`

**Interfaces:**
- Consumes: `SessionMode`, `PayloadKeys` (Tasks 4–5); the workflow names in Task 9 (copied as
  constants, since TypeScript cannot import Python).
- Produces:

```ts
export const DURABLE_WORKFLOW = 'DurableSession'
export const DURABLE_TASK_QUEUE = 'agent'
export const SEND_UPDATE = 'send_message'
export const REVIEW_UPDATE = 'review'
export const PENDING_QUERY = 'pending_approvals'
export const durableWorkflowId = (sessionId: string) => `session-${sessionId}`
export const DURABLE_APPROVAL_PREFIX = 'durable:'
export function durableApprovalId(sessionId: string, toolUseId: string): string
export function parseDurableApprovalId(id: string): { sessionId: string; toolUseId: string } | undefined
export type DurableSessionInput = {
  session_id: string; max_turns: number; approval_expiry_seconds: number; model: string | null
  restored: { in_flight: { id: string; name: string; status: string }[] } | null
}
export class DurableRefused extends Error {} // a validator refused the Update
export interface DurableSessions {
  send(input: DurableSessionInput, message: { text: string; context: string | null }): Promise<DurableSendResult>
  review(sessionId: string, toolUseId: string, approved: boolean, approver: string): Promise<void>
  pending(sessionId: string): Promise<{ id: string; name: string; input: Record<string, unknown> }[]>
  /** Stop: cancels the running execution; false when none is running. */
  cancel(sessionId: string): Promise<boolean>
}
/**
 * send: `describe()` first. Running → update-with-start with state null (USE_EXISTING
 * attaches). Closed Completed → state = `handle.result()` (the AgentState, opaque JSON).
 * Other closed, or not found → the latest `ai_durable_snapshots` row with
 * `input.restored = {in_flight}`, or state null and `resumedFresh: true` when there is no
 * snapshot. The start carries `workflowIdReusePolicy: 'ALLOW_DUPLICATE'`.
 */
export type DurableSendResult = { started: 'attached' | 'handed_over' | 'restored' | 'fresh'; resumedFresh: boolean }
export class TemporalDurableSessions implements DurableSessions { constructor(client: Client) }
```

- [ ] **Step 1: Write the failing tests.**
  - `durable.client.test.ts` (with a fake `Client`):
    - `send` calls `executeUpdateWithStart(SEND_UPDATE, {args: [message], startWorkflowOperation:
      WithStartWorkflowOperation.create(DURABLE_WORKFLOW, {workflowId: 'session-<id>',
      taskQueue: 'agent', args: [input, state, null], workflowIdConflictPolicy: 'USE_EXISTING',
      workflowIdReusePolicy: 'ALLOW_DUPLICATE'})})`, where `state` is `null` while the ID is
      running or unknown, and the closed execution's result when it completed.
    - A `WorkflowUpdateFailedError` becomes `DurableRefused` with the validator's message.
    - `parseDurableApprovalId` round-trips, and rejects ids without the prefix or with a
      non-uuid session.
  - `chat.pg.test.ts` (with `fakeDurable`):
    - A durable session's send appends `user.turn` and `session.status running`, and calls
      `send` with the session's `max_turns`, the current `approval_expiry_seconds` and
      `model`, and the page context.
    - A `DurableRefused("the session is busy")` becomes `SessionError('busy')`.
    - `session.interrupt` on a durable session calls `durable.cancel(sessionId)` and
      returns true. With no running execution, it returns false (deviation 4).
    - Send after a Stop: when `durable.send` finds the ID closed and *Completed*, it starts
      the new execution with the last result as `state`, and `ai_durable_streams.next_offset`
      is reset to 0 first.
    - When the ID is closed any other way (terminated or failed), or not found but
      `ai_durable_snapshots` has a row, it starts with the snapshot's state and
      `input.restored = {in_flight}`. The message's `context` gains the lost-results line for
      the audit `tool_call` rows newer than `saved_at` whose `toolUseId` is in neither
      `recent_call_ids` nor `in_flight` (deviation 4, point 3). A test inserts such a row and
      expects the line, and expects no line without one.
    - With no snapshot at all, it starts with `state = null`, and the log gains
      `error {code: 'resumed_fresh'}`.
    - A classic session's send never touches `DurableSessions`.
  - Review Focus 1: when `send` resolves but no worker polls, the status stays `running` and
    no `error` event is written.
  - `approvals.pg.test.ts`:
    - `decide(browser, 'durable:<s>:toolu_1', true)` calls `review(s, 'toolu_1', true,
      'browser:browser')` and appends `approval.resolved {approved: true, by}`.
    - A non-owner bearer gets `not_found`, the same visibility rule as classic.
    - `DurableRefused` becomes `ApprovalError('conflict')`, which is 409 on the route
      (Review Focus 2).
    - Review Focus 7: an id without the prefix still settles an `ai_approvals` row and never
      calls `review`.
    - `GET /approvals?session=<durable>&pending=true` maps `pending()` into `ApprovalView`s
      (`id` the durable id, `tier: 'outward'`, `input_summary` capped at 500, `decision:
      null`).
  - `durable.temporal.test.ts` (`requires_temporal`): a Python-free check of the wire shape. A
    TypeScript test workflow registered under the name `DurableSession` on `agent`, with
    handlers named `send_message`/`review`/`pending_approvals`, receives the client's calls
    with the exact argument shapes `[{session_id, max_turns, approval_expiry_seconds,
    model}, null, null]` and `[{text, context}]`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - In `manager.send`, after `get()`: if `before.mode === 'durable'`, check `draining`,
    ownership, `status <> 'done'` and `cost_usd < budget_usd` with one `UPDATE … SET status =
    'running' … WHERE … AND status NOT IN ('running','waiting_approval') RETURNING`.
    Otherwise throw `busy` / `budget` as `whyNotClaimed` does. Then append `user.turn` and
    status, call `durable.send`, and return a `Turn` whose `done` follows the event log from
    the `user.turn` seq to the next settled `session.status` (`idle`/`done`/`failed`) and
    resolves `{kind: 'result', …}` from the row.
  - If `durable.send` throws, set the status back to `idle` and append an `error` event with
    the message (Temporal unavailable → code `busy`, "the durable worker's Temporal is
    unreachable; send again").
  - In `approvals/service.ts`, `decide` and `decision` check `parseDurableApprovalId(id)`
    first and go through `authorizeSession`. `visible`/`authorize` are refactored so that
    their session-level half is shared.
  - `main.ts` builds `TemporalDurableSessions(new Client({connection: Connection.lazy(...),
    namespace, dataConverter: {payloadCodecs: [codec]}}))` when Temporal and a KEK are
    configured. Without them, `start` refuses durable (Task 5).
- [ ] **Step 4: Run, expect PASS**: `pnpm exec vitest run test/durable.client.test.ts test/durable.temporal.test.ts test/approvals.pg.test.ts test/chat.pg.test.ts test/approvals.e2e.test.ts test/chat.e2e.test.ts`; `pnpm lint && pnpm typecheck`.
- [ ] **Step 5: Commit** `feat(agent): durable sessions send through DurableSession and approve through review (#1056)`.

### Task 13: `forgetSubject`

**Files:**
- Create: `agent/src/durable/forget.ts`, `agent/src/forget-subject.ts` (CLI), `agent/test/forget.pg.test.ts`
- Modify: `agent/package.json` (the build emits `dist/forget-subject.js`)

**Interfaces:**
- Consumes: `PayloadKeys.forget` (Task 4); `ai_durable_segments` (Task 6).
- Produces: `forgetSubject(subject, deps: {sql, keys: PayloadKeys, client?: Client}) ->
  Promise<{keyDeleted: boolean; workflow: 'terminated' | 'closed' | 'absent'; rows: number}>`.

- [ ] **Step 1: Write the failing test** (`requires_postgres`, and `requires_temporal` for
  the workflow half).
  - After `forgetSubject('session-<id>')`, the key row is gone and decoding an earlier
    encoded payload throws `SealError`.
  - The open workflow is terminated, and `describe()` then fails with `NotFound` after
    `DeleteWorkflowExecution` (poll up to 10 s; deletion is asynchronous).
  - `ai_sessions`, `ai_session_events`, `ai_durable_*` rows and the `ai_session_entries` of
    every `claude_session_id` are gone.
  - A second call is a no-op that reports `absent`.
  - A malformed subject is refused before anything is deleted.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** in the order of §6.5: key first, then the workflow (terminate if
  running, then `client.workflowService.deleteWorkflowExecution({namespace,
  workflowExecution: {workflowId}})`), then the rows in one transaction. The CLI reads the same
  config as `main.ts`, prints the result as JSON, and exits 1 on error. It writes an audit row
  (`kind: 'operator'`, action `forget_subject`) if the audit kinds allow it. Otherwise it adds
  the kind in this task's migration
  (`<stamp>_audit_forget_kind.sql`, following `20260930T0511Z_audit_memory_kind.sql`).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(agent): forgetSubject crypto-shreds a durable session (#1056)`.

### Task 14: The mode picker and the default in Settings

**Files:**
- Modify: `frontend/src/components/assistant/AssistantChat.tsx`, `frontend/src/agent/chat/useAgentChat.ts`,
  `frontend/src/agent/chat/protocol.ts` (`mode` on `user.message`, `SessionSummary.mode`),
  `frontend/src/components/assistant/badges.tsx` (Durable badge), `frontend/src/api/client.ts`,
  `frontend/src/api/types.ts`, `frontend/src/pages/SettingsPage.tsx` (the Assistant section, beside
  `SessionLimitsSetting`), `frontend/src/mocks/agent.ts`
- Create: `frontend/src/components/SessionModeSetting.tsx`, `frontend/src/components/SessionModeSetting.test.tsx`,
  `frontend/src/components/assistant/ModePicker.tsx`, `frontend/src/components/assistant/ModePicker.test.tsx`,
  `frontend/src/mocks/features/sessionMode.ts`

**Interfaces:**
- Consumes: `GET/PUT /api/v1/ai/settings/session-mode` (Task 5); `user.message.mode`;
  `SessionSummary.mode`.
- Produces: `MODE_KEY = 'scadbuddy.assistant.mode'`; `readMode(): SessionMode | null`;
  `writeMode(m)`; `<ModePicker value onChange />`; `api.getSessionMode()`,
  `api.putSessionMode(mode)`.

- [ ] **Step 1: Write the failing tests.**
  - `ModePicker.test.tsx`:
    - With no session selected and an empty feed, an "Advanced" disclosure shows a select with
      Classic and Durable. Durable's description says "Survives restarts; approvals wait as
      long as needed. Plugins are not available."
    - Choosing Durable writes `scadbuddy.assistant.mode = durable`, and the next
      `user.message` carries `mode: 'durable'`.
    - With nothing stored, the value is the server default (msw returns `durable`).
    - A throwing `localStorage` (getItem/setItem mocked to throw) still renders with the
      default and sends.
    - After the first message the picker is gone, and messages with a `sessionId` never carry
      `mode`.
  - `AssistantChat`: a durable session shows a "Durable" badge in the header. Stop works for
    it as for a classic session: it sends `session.interrupt` (deviation 4).
  - `SessionModeSetting.test.tsx`: it loads the value, saves the change through PUT, and
    shows the error detail of a 400/503. Model it on `SessionLimitsSetting.test.tsx`.
- [ ] **Step 2: Run, expect FAIL.** `cd frontend && pnpm exec vitest run src/components/assistant/ModePicker.test.tsx src/components/SessionModeSetting.test.tsx`
- [ ] **Step 3: Implement.** `readMode`/`writeMode` copy `readAdvanced`'s try/catch. The
  picker sits under the composer only while `state.activeSessionId` is null. Mocks: the
  `sessionMode` feature handlers with `reset`, and `mocks/agent.ts` echoes `mode` into
  `session.started` and `sessions.snapshot`.
- [ ] **Step 4: Run, expect PASS**: `pnpm lint && pnpm typecheck && pnpm exec vitest run src/components src/agent`.
- [ ] **Step 5: Commit** `feat(frontend): choose classic or durable when a chat starts, with a default in Settings (#1056)`.

### Task 15: The `agent-durable` image

**Files:**
- Modify: `Dockerfile` (a global `ARG CLAUDE_CODE_VERSION=2.1.283` redeclared in `agent` and
  `agent-durable`; a new `agent-durable` stage), `.hadolint.yaml` (only if a new rule needs an
  ignore, with a reason)
- Create: `agent-durable/scripts/check_cli_version.py`, `agent-durable/tests/test_cli_version.py`

**Interfaces:**
- Produces: the image target `agent-durable` (`USER 10001:10001`, `CMD ["python", "-m",
  "scadbuddy_durable.worker"]`, `HEALTHCHECK` on 8082, cwd `/srv/agent`, `HOME` and
  `CLAUDE_CONFIG_DIR` under `/var/lib/scadbuddy-agent-durable`).
  `check_cli_version.py <version>` exits non-zero unless
  `claude_agent_sdk._cli_version.__cli_version__ == <version>` and the bundled binary's
  `--version` starts with it.

- [ ] **Step 1: Write the failing test.** `test_cli_version.py`: `main(["2.1.283"])` returns 0
  in the locked environment, `main(["2.1.999"])` returns 1, and the message names both
  versions.
- [ ] **Step 2: Run, expect FAIL**, then implement the script.
- [ ] **Step 3: Write the stage.**

```dockerfile
# ── agent-durable: durable agent sessions on Temporal (spec 2026-10-01 §6.2, #1056) ──
# A sidecar in the ScadBuddy pod, trusted like `agent` (#1030). Python on its own
# slim base: the "no Python in the base image" rule is about the OpenSCAD image.
FROM python:3.12-slim-bookworm AS agent-durable
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
RUN groupadd --gid 10001 scadbuddy \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/scadbuddy-agent-durable --shell /usr/sbin/nologin scadbuddy \
    && install -d -o 10001 -g 10001 /var/lib/scadbuddy-agent-durable /var/lib/scadbuddy-agent-durable/claude /srv/agent
WORKDIR /app/agent-durable
ENV UV_PROJECT_ENVIRONMENT=/app/agent-durable/.venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY agent-durable/pyproject.toml agent-durable/uv.lock agent-durable/.python-version ./
# git: uv fetches the pinned ai-integrations commit (spec §6.2).
RUN uv sync --frozen --no-dev
COPY agent-durable/scadbuddy_durable ./scadbuddy_durable
COPY agent-durable/scripts ./scripts
# The tool manifest and the prompt policy from the agent build (phase 4, Task 7).
COPY --from=agent-build /src/agent/dist/tools.json ./tools.json
COPY --from=agent-build /src/agent/dist/durable-prompt.txt ./durable-prompt.txt
# Skills only (§6.3b): no agents/, no .mcp.json.
COPY agent-durable/plugin/.claude-plugin ./plugin/.claude-plugin
COPY plugins/scadbuddy/skills ./plugin/skills
RUN chmod -R a+rX ./plugin && test -f plugin/skills/customize/SKILL.md && test -z "$(find plugin -type l)"
# The Claude Code the Python SDK bundles, asserted like the agent stage's. Bump with
# claude-agent-sdk in agent-durable/pyproject.toml and the TypeScript SDK.
ARG CLAUDE_CODE_VERSION
RUN .venv/bin/python scripts/check_cli_version.py "$CLAUDE_CODE_VERSION"
ENV PATH=/app/agent-durable/.venv/bin:$PATH \
    CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} \
    HOME=/var/lib/scadbuddy-agent-durable \
    CLAUDE_CONFIG_DIR=/var/lib/scadbuddy-agent-durable/claude \
    SCADBUDDY_AGENT_TOOLS_MANIFEST=/app/agent-durable/tools.json
USER 10001:10001
WORKDIR /srv/agent
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "scadbuddy_durable.worker"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/healthz', timeout=4).status == 200 else 1)"]
```

  `ownPlugin`'s symlink note applies: copy the skills from `plugins/scadbuddy/skills`
  directly, so no link reaches the image. Pin the base by digest
  (`python:3.12-slim-bookworm@sha256:…`), resolved with
  `docker buildx imagetools inspect python:3.12-slim-bookworm` at implementation time.
- [ ] **Step 4: Build and run.** `docker build --target agent-durable -t scadbuddy-agent-durable:dev .`, then
  `docker run --rm -d --read-only --tmpfs /tmp --tmpfs /var/lib/scadbuddy-agent-durable:uid=10001,gid=10001 -p 18082:8082 scadbuddy-agent-durable:dev`.
  `curl :18082/healthz` should show `"durable": "disabled (no database)"`. Then run `hadolint Dockerfile`.
- [ ] **Step 5: Commit** `build: the agent-durable image target (#1056)`.

### Task 16: CI, image publishing and the pin watch

**Files:**
- Modify: `.github/workflows/ci.yml` (`layout` output `agent_durable`; a new `agent-durable`
  job; `summary` `needs` and assertions; `dispatch-review` `needs`), `.github/workflows/build-image.yml`
  (an `agent-durable` job publishing `ghcr.io/eh-homelab/scadbuddy-agent-durable`)
- Create: `.github/workflows/agent-durable-pin.yml`

- [ ] **Step 1: The `agent-durable` job** (`runs-on: ubuntu-latest`, `postgres:17` service as
  in `agent`, 25 min). Its steps:
  1. Checkout.
  2. `astral-sh/setup-uv@v10.2.0` with `cache-dependency-glob: agent-durable/uv.lock`.
  3. `actions/setup-node@v7` node 24 and corepack (the fake endpoint and the agent build).
  4. The Temporal CLI step copied from `agent` (same version and sha256).
  5. `uv run --frozen ruff check .`, `ruff format --check .`, `mypy`, and `pytest` with
     `SCADBUDDY_TEST_DATABASE_URL`, `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` and
     `SCADBUDDY_AGENT_TOOLS_MANIFEST=tests/fixtures/tools.json`.
  6. `cd agent && pnpm install --frozen-lockfile && pnpm build && pnpm exec vitest run
     test/secretVectors.test.ts test/durable.e2e.test.ts` with `SCADBUDDY_TEST_AGENT_DURABLE=$GITHUB_WORKSPACE/agent-durable`.
     This is the end-to-end test of Task 17. Vectors and `secrets.ts`/`credentials.ts`
     changes are covered because the job runs on every PR (§6.2).
  7. `docker/setup-buildx-action@v4` and `build-push-action@v7` with `target: agent-durable`,
     `cache-from: type=gha,scope=agent-durable`, and
     `cache-to: type=gha,scope=agent-durable,mode=min`.
  8. A container check like the agent's: healthy, and
     `.durable == "disabled (no database)"`.
- [ ] **Step 2: `summary`** adds `agent-durable` to `needs` and to its result assertions,
  in the same way as `agent`. `dispatch-review` adds it to `needs`. `CI Summary` keeps its
  name: the ruleset is in eh-homelab/clusters.
- [ ] **Step 3: `build-image.yml`** gets an `agent-durable` job on push to main and tags,
  copied from `agent`, with image `ghcr.io/eh-homelab/scadbuddy-agent-durable`, `scope=agent-durable`
  and `mode=min`. The header comment notes that the package must be made public, like the
  others.
- [ ] **Step 4: `agent-durable-pin.yml`** runs on `schedule: cron: '17 6 * * 1'`, on
  `workflow_dispatch`, and on `pull_request` with `paths: [agent-durable/uv.lock,
  agent-durable/pyproject.toml]`. It runs on `ubuntu-latest` with permissions `contents:
  read` and `issues: write`. It checks out, sets up uv, and runs
  `cd agent-durable && uv lock --check`. A `failure()` step on schedule only runs
  `gh issue create --title "agent-durable: the pinned ai-integrations commit no longer
  resolves" --label bug --body "<run URL>; remedy: a reviewed bump to PR #33's new head or
  the PyPI release (spec §6.2)"`. It first checks for an open issue with that title
  (`gh issue list --search`) to avoid duplicates.
- [ ] **Step 5: Lint.** Run `actionlint` and `shellcheck` on any new script.
- [ ] **Step 6: Commit** `ci: the agent-durable job, image and pin watch (#1056)`.

### Task 17: End to end — the chat socket to a durable turn

**Files:**
- Create: `agent/test/durable.e2e.test.ts`
- Modify: `agent/test/support/liveAgent.ts` (only if a helper to start the app with Temporal is
  missing)

- [ ] **Step 1: Write the test.** It skips unless `SCADBUDDY_TEST_AGENT_DURABLE`,
  `SCADBUDDY_TEST_DATABASE_URL` and a Temporal CLI are present.
  - It starts the dev server (`test/support/temporal.ts`), a throwaway schema with the
    agent's migrations, the fake Anthropic with a script `[toolUse get_settings, text "Done."]`
    (and a second pair for the approval case), the agent app with Temporal, a KEK and a
    `gateway` credential pointed at the fake, and
    `uv run --frozen --project $SCADBUDDY_TEST_AGENT_DURABLE python -m scadbuddy_durable.worker`
    as a child process with the same env and `SCADBUDDY_AGENT_TOOLS_MANIFEST=dist/tools.json`.
  - It opens `/api/v1/ai/chat` with the UI origin and sends `user.message {mode: 'durable',
    text}` without `sessionId`.
  - It expects, in order: `session.started` (with `mode: 'durable'`), `user.turn`,
    `session.status running`, `tool.call get_settings`, `tool.result ok`,
    `assistant.text.delta "Done."`, `session.result`, and `session.status idle`.
  - Approval case: the script calls an outward tool. The test expects `approval.required` with
    id `durable:<s>:…`, sends `approval.decision {approve: false}`, and expects
    `approval.resolved {approved: false, by}`, then `tool.result ok: false`.
  - Review Focus 1: start the Python worker only after `user.message` was sent. The reply
    still arrives.
  - Stop and resume (deviation 4), on the real engine:
    - The script's first turn calls a tool whose stub activity hangs. The test sends
      `session.interrupt` and expects `session.status idle`.
    - It then sends a second `user.message` on the same session.
    - The fake's next `/v1/messages` request must contain the first message's text and the
      `tool_result` "…interrupted…" for the hung call: the model sees the earlier history.
    - It expects `assistant.text.delta` for the second answer.
  - Terminate and resume (Ruling 15), on the real engine:
    - The first turn calls a tool whose stub hangs. The test terminates `session-<id>`
      through a Temporal client (as an operator would).
    - It sends a second `user.message`.
    - The fake's next request must contain the first message, and an error `tool_result`
      for the hung call ("…interrupted…").
    - The answer arrives.
    - No `resumed_fresh` error was written.
- [ ] **Step 2: Run it** with the three variables set: `pnpm build && pnpm exec vitest run test/durable.e2e.test.ts`. Expected: PASS. Fix the plumbing it finds in the owning task's files.
- [ ] **Step 3: Commit** `test(agent): a durable turn from the chat socket through agent-durable (#1056)`.

### Task 18: Docs

**Files:**
- Modify: the spec's §10 phase 5 ("As built", with the deviations as decided), `CLAUDE.md`
  (Commands: an "Agent durable (`agent-durable/`)" block like the backend's; Layout: an
  `agent-durable/` entry; CI rules: the `scope=agent-durable` cache and the
  `CLAUDE_CODE_VERSION` + `claude-agent-sdk` pair), `README.md` ("Deploying": the sidecar,
  its variables, its emptyDir, and that its package must be public),
  `docs/ai/operating.md` (durable mode, the forget CLI, rotation including `ai_payload_keys`)

- [ ] **Step 1: Write them.** Re-read the plan's rulings against the code as built, and
  describe only what exists.
- [ ] **Step 2: Commit** `docs: durable agent sessions (#1056)`.

## Clusters changes (for the lead, eh-homelab/clusters `applications/scadbuddy/`)

- The ScadBuddy pod gets a third container, `agent-durable`
  (`ghcr.io/eh-homelab/scadbuddy-agent-durable`).
  - It has the `agent` container's `securityContext` and mounts the same Secret (the KEK
    files). Its env is `SCADBUDDY_DATABASE_URL`, `SCADBUDDY_SECRET_KEY_FILE`,
    `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`, `SCADBUDDY_TEMPORAL_ADDRESS` and
    `SCADBUDDY_TEMPORAL_NAMESPACE`.
  - It has `readOnlyRootFilesystem: true` with emptyDirs at `/tmp` and
    `/var/lib/scadbuddy-agent-durable`, plus `/srv/agent` (an emptyDir, read by the engine
    as its cwd).
  - Resources: request 512Mi, limit 1.5Gi (4 concurrent segments at about 270 MB each, per the
    plugin README).
  - Liveness and readiness probes on `:8082/healthz`.
- No NetworkPolicy of its own (§6.3a). It is in the ScadBuddy pod: egress to Postgres, to
  Temporal (7233) and to the credential's endpoint already allowed for the pod.
- `agent-tools` and `agent` are new task queues on the `scadbuddy` namespace. Nothing to
  register.
