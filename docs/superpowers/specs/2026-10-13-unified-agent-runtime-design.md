# One agent runtime: every session is a workflow, every turn is the classic turn

Design written 2026-10-13. It answers this request from the user:

> Unify the tool layer, permissions etc. it should be 100% the same. The only difference
> is how things happen. Why can't we use sticky task queues and have the workflow keep the
> process in memory as long as it can for perf?

This spec amends `2026-10-01-durable-printing-agents-flows-design.md` (the "durable spec"
below). It replaces §6.2, §6.3, §6.3a and §6.3b for agent sessions, and the "Classic
sessions" part of §6.6. §6.5 (the payload codec and `forgetSubject`) and §7 (flows) stay,
with the changes listed in §9 and §12. It also closes the parity plan
`docs/superpowers/plans/2026-10-11-durable-capability-parity.md`: there is nothing left to
keep in parity.

Implementation plan: `docs/superpowers/plans/2026-10-13-unified-agent-runtime.md`. Epic: #2726.

Every claim about code cites `path:line` on `origin/main` at `4e248b03c`. A measured fact
says where and how it was measured (§2). Anything not measured is marked **unverified**,
and the plan task that checks it is named.

## 1. Why

ScadBuddy runs an agent turn in two unrelated ways today.

- **Classic.** `SessionManager.runTurn` (`agent/src/sessions/manager.ts:1456`) assembles
  the turn and calls `runHarness` (`agent/src/harness/run.ts`).
  - It uses the TypeScript Claude Agent SDK 0.3.296, which bundles Claude Code 2.1.296
    (`agent/node_modules/@anthropic-ai/claude-agent-sdk/package.json`, `claudeCodeVersion`).
  - A turn makes one `query()` (`run.ts:486`), so each turn spawns a Claude Code process
    and resumes the transcript from Postgres. The transcript is `ai_session_entries`,
    written through `PostgresSessionStore` (`agent/src/sessions/store.ts:59-123`) with
    `sessionStoreFlush: 'eager'` (`run.ts:414-425`).
  - Tools are in-process SDK MCP servers. The tier seam is `canUseTool` plus a
    `PreToolUse` hook (`agent/src/harness/permissions.ts:153-223`). Parked calls wait on
    `ai_approvals` and `ai_questions` rows, polled every second
    (`agent/src/approvals/service.ts:1185-1284`).
  - A turn is owned by whichever replica claimed the row, under a 60 s lease renewed every
    second (`manager.ts:351-354`, `1244-1252`, `1514-1530`). A reaper ends the turns of a
    dead pod (`manager.ts:2505-2538`).
- **Durable.** `DurableSession` is a Python workflow on queue `agent`
  (`agent-durable/src/scadbuddy_durable/session/workflow.py:214`).
  - It is built on the unreleased `temporalio-claude-agent-sdk` plugin, pinned to
    `temporalio/ai-integrations@b1cf3848` (`agent-durable/pyproject.toml:12`).
  - It uses the Python `claude-agent-sdk==0.2.164`, which bundles Claude Code 2.1.292
    (`pyproject.toml:14`). That is a different Claude Code from the classic one.
  - Each model segment is an activity. Each durable tool is another activity, on
    `agent-tools` (`session/tools.py:102-116`), served by the TypeScript agent.
  - The conversation lives in the workflow's state, not in `ai_session_entries`
    (`session/runner.py`; the plugin's `__claude_agent_conversation` Query).

Keeping these two in parity has cost a nine-phase plan (the parity plan, 2026-10-11).
Gaps remain even after it (durable spec §6.3b, last paragraph):

- a durable session cannot be forked (`manager.ts:2300-2305`);
- a durable subagent gets ScadBuddy's own tools only, through a loopback server
  (`agent/src/routes/subagentMcp.ts`);
- of an `allow_refused` package's built-ins, durable offers only `Bash`/`PowerShell`;
- durable runs one tool call at a time, because the plugin keeps one paused call per
  run (durable spec §3.2);
- durable has two bundled Claude Codes to bump and check;
- three ports have to be kept equal to TypeScript, with shared vector files: secrets
  (`agent-durable/.../secrets.py`), the payload codec (`codec.py`) and the gate validator
  (`gate/validate.py`).

The duplication also hides bugs. One example: a flow's approved outward turn tool is
always refused, because `turnTools.ts:422` drops the input hash that `PgFlowRuns.approved`
requires (`agent/src/temporal/flowRuns.ts:84`). The unit test's fake ignores the hash
(`agent/test/turnTools.test.ts:118`). This is filed as #2728, and §12 lists it.

**The ask, restated.** There is one turn implementation, the classic one, and one tool and
permission layer. Durability is about *where* and *when* that turn runs:

- in an activity the session workflow schedules;
- on the replica that already has the session's Claude Code process warm;
- surviving restarts and parks through the workflow.

## 2. Measured facts

### 2.1 How they were measured

- **Where.** In the worktree at `origin/main` `4e248b03c`, after `pnpm install
  --frozen-lockfile` in `agent/`. That gives `@anthropic-ai/claude-agent-sdk` 0.3.296 and
  Claude Code 2.1.296.
- **Machine.** Node v24.16.0 on WSL2, kernel 6.6.87.2.
- **Harness.** A scratch vitest file runs the bundled CLI (`bundledCliPath()`) against
  `test/support/fakeAnthropic.ts`, with `tools: []`, `settingSources: []` and a fresh
  `CLAUDE_CONFIG_DIR` for each query. It is the same seam `test/run.test.ts` uses.
- **Not committed.** The file was not committed. Plan Task 1.1 commits E1, E4 and E5 as
  `agent/test/sdkRuntime.sdk.test.ts`, so an SDK bump re-checks them.
- **Timings.** They come from the fake endpoint, so they show process and resume overhead
  only, not model latency.

| # | Experiment | Result |
|---|---|---|
| E1 | One `query()` whose `prompt` is an `AsyncIterable<SDKUserMessage>`. Push three messages, waiting for each `result`, and count spawns with `spawnClaudeCodeProcess`. | **1 spawn for 3 turns.** Turn latency was 1316 ms, then 80 ms, then 57 ms. The third request carried the whole conversation (`user,assistant,user,assistant,user`). |
| E2 | Turn 1 with `sessionStore: InMemorySessionStore` and `sessionStoreFlush: 'eager'`. Then a *new* `query()` with `resume: <id>`, the same store, and a *different, empty* `CLAUDE_CONFIG_DIR`, which is how another host looks. | **The resume carried turn 1's content.** A cold resume took 1945 ms, against the 57–80 ms of a warm turn. |
| E3 | An in-process MCP tool that hangs; `abortController.abort()` during the call; then resume from the store. | The transcript holds a synthetic `tool_result`, `is_error: true`: *"The tool call was interrupted before a result was received. It may or may not have completed on the server — verify before assuming it succeeded, and retry if needed."* **The tool ran once.** Resume did not run it again. |
| E4 | The same, but `SIGKILL` of the Claude Code process during the call (pod death); then resume on a fresh config dir with a `canUseTool` that records calls. | The store ends at the assistant `tool_use`: entries mirrored up to the kill and none after it. On resume, Claude Code synthesizes an error result: *"[Tool call interrupted: the session ended before this call's result was recorded, so its outcome is unknown. Check whether it took effect before relying on it or running it again.]"* **The tool ran once. `canUseTool` was not called on resume.** |
| E5 | A streaming-input query with `maxBudgetUsd: 0.00012` (one reply costs $0.000105), then one with `maxTurns: 2`, four messages each. | **`maxBudgetUsd` is per process, cumulative.** The results were `success`, then `error_max_budget_usd` three times, with no further model calls. **`maxTurns` is per message:** `num_turns` was 1 on every result, and all four succeeded. |
| E6 | `/proc/<pid>/status` of an idle warm process after 3 turns, with no plugins and no MCP. | `VmRSS` 211012 kB (about 206 MiB), 18 threads. |

### 2.2 From the SDK's type declarations (0.3.296, `sdk.d.ts`)

- **`SessionStore.append`** is called after the local write. It is batched about every
  100 ms, or per frame with `'eager'`. A failure is retried 3 times and then dropped with
  a `mirror_error` system message. Entries carry a `uuid` meant as an idempotency key
  (`sdk.d.ts:6620-6660`).
- **`load` runs before spawn.** The SDK writes the loaded entries to a temporary JSONL
  file and the CLI resumes from that file. `loadTimeoutMs` defaults to 60 s
  (`sdk.d.ts:1836-1845`).
- **`total_cost_usd`** is cumulative across turns in streaming-input sessions.
  `maxBudgetUsd` counts the spend since this `query()` started (`sdk.d.ts:5738`).
  This agrees with E5.
- **`Query` has no live setter for the budget or the credential env.** It does have
  `setModel`, `setPermissionMode`, `setMcpServers`, `interrupt`, `streamInput`,
  `stopTask` and `reloadSkills` (`sdk.d.ts:2865-3215`).

### 2.3 Temporal (`@temporalio/*` 1.24.0, `agent/package.json`)

- **Sticky execution is for workflow tasks only.** It caches workflow state, and "the
  concept of Sticky Execution is not relevant to Activity Tasks"
  ([docs.temporal.io/sticky-execution](https://docs.temporal.io/sticky-execution)).
- **What the user's "sticky task queue" maps to.** Temporal documents a *worker-specific
  task queue* for activities that cache data in a process ("Route Activity Tasks to a
  specific process", [docs.temporal.io/task-routing](https://docs.temporal.io/task-routing)).
  ScadBuddy already uses that pattern: `agent-tools.<hostname>`
  (`agent/src/main.ts:426`, `530-539`).
- **Worker Sessions are not in the TypeScript SDK.** A grep of `@temporalio/worker` and
  `@temporalio/workflow` 1.24.0 `lib/*.d.ts` for a session API finds none.
- **Heartbeats are throttled to `heartbeatTimeout × 0.8`**
  (`@temporalio/worker/lib/worker-options.d.ts:427-435`). An activity learns that it was
  cancelled on a heartbeat round trip, so with a 20 s heartbeat timeout a cancel can take
  up to 16 s to arrive. Interrupt therefore has a fast path of its own (§5.6).
- **Worker Deployments.** The 1.24 worker supports `workerDeploymentOptions` with
  `useWorkerVersioning` and `defaultVersioningBehavior`
  (`worker-options.d.ts:54-70`, `642-654`).

### 2.4 Deployment (`eh-homelab/clusters` `applications/scadbuddy/scadbuddy.yaml`, 2026-10-12)

- The pod runs 3 replicas (`:107`), with the `agent` and `agent-durable` sidecars
  (`:479`, `:644`).
- The `agent` container requests 512Mi of memory, with a 2Gi limit (`:620-624`).
- The rollout strategy is **`Recreate`** (`:131`): every deploy stops all three pods
  together.
- `terminationGracePeriodSeconds` is not set, so it is Kubernetes' 30 s default.

## 3. Goals and non-goals

**Goals**

1. **One turn.** The code that assembles and runs a turn is the same for every session, so
   there is nothing to drift: options, tiers, plugins, packages, subagents, hooks, memory,
   built-ins, images, budget and credential fallback.
2. **Every session is durable.** Its turns, its parks and its bookkeeping survive a pod
   restart and a deploy.
3. **A warm turn costs a warm turn.** The Claude Code process stays alive on one replica
   between turns. The measured difference is 57–80 ms against about 1.3–1.9 s (E1, E2),
   before remote MCP servers reconnect.
4. **Nothing pins forever.** No session holds a process, a slot or a queue while it waits
   on a person beyond a bounded hold.
5. **Retire `agent-durable/` and the `temporalio-claude-agent-sdk` pin.** This includes the
   Python ports, their vector files where only Python reads them, and the
   `agent-durable-pin` workflow.
6. **Flows keep working unchanged.** `ProjectWorkflow` and its `agent()` / `ask_session()`
   calls behave as they do today.
7. **A CI check fails if a second turn path appears.**

**Non-goals**

- Changing the tool registry, tiers, the approval policy or the gate's public shape. The
  `pending_input` Query, the `respond` Update, `ai_pending_input` and
  `GET/POST /api/v1/ai/pending-input` keep working as they do (durable spec §6.6).
- Changing `/mcp`. It still serves ScadBuddy's own tools, with prepare/confirm on
  `ai_approvals`, and it has no session.
- Running Claude Code's own background tasks (Cron, Wakeup, Monitor) inside a warm
  process. §8 makes them workflows instead.

## 4. The design in one picture

```
browser / MCP / flow ──send──▶ SessionManager.send ──update-with-start──▶ AgentSession workflow
                                                                          (session-<id>, queue agent-sessions)
                                                                                   │
            ┌──────────── schedule session_turn on home queue ─────────────────────┘
            ▼            (agent-tools.<hostname>, 5 s schedule-to-start; else agent-tools)
   agent replica  ──▶ TurnHost.run(turn)
                       ├─ WarmSessions: reuse the live Claude Code process if its fingerprint
                       │  and transcript tail match, else spawn with resume (SessionStore)
                       ├─ TurnRunner: the classic turn, unchanged
                       │  (canUseTool / PreToolUse tier seam, in-process tools, plugins, hooks…)
                       ├─ a park: Update `open_input` on its own workflow, then wait on the
                       │  response row (LISTEN/NOTIFY + 1 s poll); hold ≤ park_hold_seconds
                       └─ events → ai_session_events (as classic does today), heartbeat every 5 s
```

What changes for each session operation:

| Concern | Today, classic | Today, durable | Unified |
|---|---|---|---|
| Who runs the turn | the replica that claimed the row (lease) | `run_claude_segment` on any `agent-durable` | the `session_turn` activity on the session's home replica |
| Turn code | `runTurn` → `runHarness` (TS SDK) | the plugin's runner (Python SDK) | `TurnRunner`, the extracted classic turn |
| Claude Code process | one per turn | one per segment | one per session, kept warm between turns |
| Transcript | `ai_session_entries` (plaintext) | workflow state (sealed by codec) | `ai_session_entries`, sealed under the session's data key (§6.2) |
| Tool calls | in-process | each an activity on `agent-tools` | in-process, inside the turn activity |
| Parallel tool calls | yes | no (one paused call per run) | yes |
| Parks | `ai_approvals`/`ai_questions`, in-memory wait | workflow entries, `ai_pending_input` | workflow entries, `ai_pending_input`; the activity holds, then releases (§5.4) |
| Restart mid-turn | turn ends, `interrupted` | segment retried | activity retried on another replica, resumed (§5.5) |
| Fork | yes | refused | yes |
| Subagents | in-process, full tool set | loopback MCP, own tools only | in-process, full tool set |

## 5. The session workflow and the turn activity

### 5.1 `AgentSession`

- **Language and bundle.** A TypeScript workflow, `agent/src/temporal/session/workflow.ts`.
  It is exported from `agent/src/temporal/workflows.ts` and bundled with `AgentOperation`
  by `agent/scripts/bundle-workflows.mjs`.
- **Identity.** The workflow id stays `session-<ai_sessions.id>`. The payload codec
  (`agent/src/temporal/payloadCodec.ts:45`), the gate's request ids
  (`agent/src/gate/ids.ts:40-42`), `forgetSubject` (`agent/src/sessions/forget.ts:50`)
  and the pending-input routes all key on that id, so none of them change.
- **Name and queue.** The workflow type is a new name, `AgentSession`, on a new queue,
  `agent-sessions`. The old `DurableSession` workflows stay on `agent`, served by
  `agent-durable` until §10 migrates them. A TypeScript worker therefore never receives a
  Python history.
- **Versioning.** The `agent-sessions` worker is a Worker Deployment, `scadbuddy-agent`,
  with the build id taken from the image. The workflow is **`AUTO_UPGRADE`**:
  - A session lives for days. Pinning would keep every old build polling until its last
    session ended, and `Recreate` (§2.4) gives no old build to poll anyway.
  - Changes to the workflow therefore go behind `patched()`, and replay fixtures live in
    `agent/test/fixtures/agent_session_histories/`, as `AgentOperation` does
    (`workflows.ts:45`).
  - The workflow continues-as-new after every turn, which keeps histories small and makes
    every new turn run the newest code. **Open decision D4.**
- **State.**
  - `owner`;
  - `home`: the host queue of the last turn, or none;
  - `turn`: the running turn's id and input, or none;
  - `parked`: the gate entries, as `session/workflow.py` keeps them today;
  - `continuation`: what the next activity must do first (§5.4, §5.5);
  - `ended`.
- **Handlers.** Every handler `DurableSession` has keeps its name and payload
  (`session/workflow.py:770-916`), so `DurableTurns` (`agent/src/sessions/durable.ts`) and
  `DurableGate` (`agent/src/gate/durable.ts`) keep working with only the workflow type and
  queue changed:
  - Update `send_message`, whose validator refuses `busy` and `ended`;
  - Update `respond`, whose validator is `gate/validate.ts`;
  - Update `cancel_input`;
  - Signals `interrupt` and `end`;
  - Query `pending_input`;
  - Query `turn`.

  New handlers:
  - Update **`open_input(entry)`**. The running turn activity calls it to park a call, and
    it returns the entry's id. Its validator refuses a turn id that is not the running one.
  - Signal **`owner_changed({owner})`**, for handoff.
- **Turn loop.** For each turn:
  1. Wait for a `send_message` or `end`.
  2. Run `session_turn` (§5.2), with `continuation` if one is set.
  3. Act on the result:
     - `done`: write `finish_turn`.
     - `released`: wait on the gate (§5.4), set `continuation`, and go back to step 2.
     - Failed after its retries: `finish_turn(failed)`.
  4. Continue-as-new.

### 5.2 `session_turn` and placement

- **Placement.** The workflow schedules `session_turn(input)` on `home` when it has one,
  with `scheduleToStartTimeout: 5s` and one attempt. On a schedule-to-start timeout it
  schedules again on the shared `agent-tools` queue, so any replica may take it. The
  pinned turn-tool listing does exactly this today (`session/workflow.py:407-438`).
- **The result.** It carries the replica's own queue, and the workflow stores that as the
  new `home`.
- **Why `home` dies with its pod.** The queue name is `agent-tools.<os.hostname()>`, and in
  Kubernetes the hostname is the pod name. A replaced pod therefore never inherits a dead
  pod's queue, and a turn for a dead home always falls back after 5 s.
- **Activity options.**
  - `heartbeatTimeout: 20s`, with a heartbeat every 5 s while the turn runs or holds.
  - `startToCloseTimeout: 2h`. A turn that runs longer than that is cut. Classic has no
    wall clock today, so 2 h is new; **open decision D5.**
  - `retry: { maximumAttempts: 3, nonRetryableErrorTypes: ['BudgetExhausted',
    'CredentialRefused', 'SessionGone'] }`.
- **The worker.** `session_turn` is registered on **both** the shared `agent-tools` worker
  and the per-host worker. The host worker serves it only when the replica's warm cache
  has room (§5.3); when it is full, the activity hands the turn back with `CacheFull`, and
  the workflow schedules it on `agent-tools`.
- **Ownership.** The activity *is* the turn's claim. The 60 s row lease, its renew loop
  and the reaper retire (`manager.ts:351-354`, `1514-1530`, `2505-2538`):
  - Two activities for one session cannot run at once, because the workflow schedules one
    at a time.
  - A dead replica is noticed by the heartbeat timeout, not by a lease.

### 5.3 The warm process cache (`WarmSessions`)

`agent/src/sessions/warm.ts` keeps one entry per session on this replica:

```ts
type WarmEntry = {
  sessionId: string
  query: Query                // one streaming-input query (E1)
  inbox: Inbox                // AsyncIterable<SDKUserMessage> the turns push into
  fingerprint: string         // §5.3 "Reuse rule"
  tailUuid: string | null     // last transcript entry uuid this process mirrored
  capUsd: number              // maxBudgetUsd at spawn
  costAtSpawnUsd: number      // the process's total_cost_usd baseline (resumed sessions carry history)
  lastUsedAt: number
}
```

- **Reuse rule.** A turn reuses the entry only when all three of these hold.
  1. **The fingerprint matches.** It is a hash of everything fixed at spawn:
     - the credential row id and its `updated_at`;
     - the model;
     - the own-plugin switch;
     - the enabled remote plugins (id, URL and header version);
     - the loaded packages' content hashes;
     - the sender's tiers;
     - the owner;
     - the system append;
     - the disabled tools;
     - the `builtin_tools` setting.

     Everything `TurnRunner` passes to `query()` goes into this hash except the prompt.
     A Task 2.1 test asserts that every key of `HarnessRun` is either hashed or listed as
     per-turn.
  2. **The transcript tail matches.** The last `ai_session_entries.uuid` for the session
     equals `tailUuid`. A turn that ran elsewhere in the meantime, for example after a
     fallback, makes the process stale.
  3. **The budget matches.** The lineage's remaining budget equals
     `capUsd − (cost now − costAtSpawnUsd)` within $1e-6. There is no live budget
     setter (§2.2) and the cap is cumulative per process (E5). A raise, or a fork sibling's
     spend, therefore means a respawn with the new remaining amount.

  On any mismatch: `query.close()`, then spawn with `resume` (E2).
- **Eviction.**
  - **Idle.** After `warm_idle_seconds` (default 600, the same 10 min as `SessionClients`,
    `agent/src/temporal/sessionClients.ts:5-27`).
  - **LRU.** Past `warm_max_per_replica`, which defaults to **4**. An idle process is
    about 206 MiB (E6) and the container's limit is 2Gi (§2.4). Raising either is **open
    decision D3**.
  - **Explicit**, on:
    - handoff, archive, forget and end;
    - a credential, plugin or package change (`NOTIFY scadbuddy_warm_evict` with the
      session id, or `*`);
    - shutdown.
- **Turn-scoped callbacks go through a slot.** `runTurn` builds closures that are bound to
  one turn. A warm process fixes its options at spawn, so each of these closures becomes a
  stable function that reads the *current turn* from the entry's `TurnSlot`
  (`{turnId, principal, gates, auditor, trace, http}`). The activity sets the slot before
  it pushes the message and clears it after the `result`.
  - **What they are:**
    - the approval and question gates (`manager.ts:1756-1757`);
    - the tier resolver and `BuiltInPolicy`;
    - the in-process MCP servers' `ctx`, which holds the session, turn and principal
      (`createHarnessServer`);
    - the memory and trace hooks (`run.ts:395-406`);
    - the http tool's per-session state.
  - **The abort `signal` is not used for a warm turn.** A turn ends through
    `q.interrupt()`. Abort, meaning close, is used only to end the process.
  - **A callback with no turn in the slot denies.** It can only fire between turns, from a
    straggling hook, so a stray call fails closed.
  - **What can stay static.** `systemAppend()` (`manager.ts:836-838`) is static, so it is
    safe to fix at spawn.
- **What warmth buys besides spawn time.**
  - A remote plugin's MCP connection lives in the process. A stateful plugin, such as
    Playwright MCP `--isolated` since #2568, keeps its page between turns while the
    process is warm.
  - #2641 keeps a durable session's plugin client on one replica for the same reason.
    After unification that keep is the warm process itself, and `SessionClients` retires
    with the turn-tool activities (§11).
  - A recycle or eviction loses the page, as an idle `SessionClients` eviction does today.

### 5.4 Parks: hold, then release

A parked call is any call the tier seam sends to the gate: an outward call (`approval`),
`AskUserQuestion` or `ask_user` (`answer`), `request_user_attention`, or a browser call
waiting for a tab (`tab_disconnected`, `manager.ts:210-344`). The gate's shape, its ids and
its timers are the durable spec's §6.6.

1. **Open.**
   - `canUseTool`, or the question and attention handlers, build the entry as today.
     The request id is `durable:<session>:<runId>:<tool_use_id>` (`gate/ids.ts`).
   - The activity sends **Update `open_input`** to its own workflow, using the worker's
     Temporal client. An activity talking to its own workflow is ordinary client use.
   - The workflow records the entry and starts its timer, then runs the `open_input`
     *activity*. That activity is the TypeScript port of `gate/store.py:305-398`, writing
     `ai_pending_input`, the events and the status.
2. **Hold.**
   - The activity waits for the entry's response row in `ai_input_responses`. It wakes on
     a new `NOTIFY scadbuddy_input` (sent by `resolve_input` with the request id) and also polls every 1 s, as `approvals/service.ts`'s waiter
     does today.
   - It keeps heartbeating while it waits. The process stays warm, so a quick approval
     costs nothing.
3. **Resolve.**
   - `respond` (from the panel, or from a grant) or the entry's timer resolves the entry
     in the workflow.
   - The workflow runs `resolve_input`, the port of `gate/store.py:459-470`, writing
     `ai_input_responses`, the audit row and the events.
   - The held activity sees the row and returns the decision or answer to `canUseTool`.
     The turn goes on exactly as a classic turn does.
4. **Release.** The hold ends after `park_hold_seconds`. The default is 300 s, which is
   below the default approval expiry of 600 s and well below the default question expiry
   of 3600 s; **open decision D2**. It also ends at shutdown. On release, the activity:
   - closes the query (`q.close()`, so the process ends; E3 shows the transcript then
     carries a synthetic "interrupted" result for the parked call);
   - evicts the warm entry;
   - returns `{status: 'released', parked: [request ids]}`.

   The workflow then waits on the gate with no activity running. Nothing is pinned.
5. **Continue after a release.** When every released entry is resolved, the workflow
   schedules `session_turn` with
   `continuation = {kind: 'parked', calls: [{request_id, tool_use_id, tool, input, resolution}]}`.
   Before it resumes Claude Code, the activity handles each call:
   - **`approval`, approved.** The activity runs the call once, outside the model, through
     the same tool layer: `runToolWithOutcome(..., gate: 'workflow')` with the recorded
     approval, which `toolActivities.ts:393-457` already checks by input hash. Its
     Idempotency-Key is the request id (§7).
   - **`approval`, denied or expired.** There is nothing to run.
   - **`answer`.** The answer *is* the result.

   It then resumes and sends one user message (`client_composed: true`) that states each
   call's outcome. An example: *"Your call `send_to_bambuddy` (id toolu_…) was paused
   waiting for approval. It was approved and ScadBuddy ran it once. Result: …"*. E4 shows
   that the resumed Claude Code has already marked the call *unknown*, so this message is
   what settles it.
6. **What the model may still do.** It may call the tool again anyway. That is a new
   `tool_use_id`, so an outward call parks again and a person sees the duplicate before
   anything runs.

A hold that outlives its pod is the same as a release. The heartbeat times out, the
workflow still holds the entry (workflow state survives), and the retry finds the entry
open. It waits for the resolution in the workflow before it runs `session_turn` with the
continuation, as in step 5. A park therefore survives a restart, which classic questions
never did (`questions/service.ts:34-38`).

### 5.5 A turn interrupted by a restart or deploy

- **Heartbeat timeout.** The workflow retries `session_turn` on attempt 2, on the shared
  queue, with `continuation = {kind: 'restarted'}`.
- **Resume.**
  - The new activity resumes from `ai_session_entries`. Eager flush means entries are
    there up to the kill (E4).
  - Claude Code marks any call that was in flight as *unknown* and never re-runs it (E4).
  - The activity sends one user message: *"The agent service restarted during this turn.
    Calls that were in flight are marked as interrupted; check before repeating any of
    them. Continue the task."*
- **Retries are bounded** by `maximumAttempts: 3`. After that the turn ends `failed`,
  with the reason, as classic's `interrupted` does today.
- **Deploy.** On SIGTERM the worker stops polling. A running turn gets up to 12 s
  (`stopTurns` `graceMs`, `main.ts:691-695`), and is then closed with `q.close()`, which
  gives E3's graceful synthetic result. The activity fails with `ShuttingDown`, which is
  retryable, and the new pods resume it as above. `Recreate` means every turn of every
  session takes this path at each deploy. **Open decision D6** asks whether to move the
  agent to a `RollingUpdate` once its queues are versioned.

### 5.6 Interrupt

- **Fast path.** The route keeps doing what classic does for a turn on another replica: it
  sets `interrupt_requested = true` and `NOTIFY`s (`manager.ts:2114-2117`). The activity
  watches the row on its 1 s tick and on the notify. It calls `q.interrupt()`, then aborts
  after `INTERRUPT_GRACE_MS` (`run.ts:352`, `538-559`).
- **Backstop.** The route also sends the workflow's `interrupt` Signal, as today
  (`durable.ts:360-380`). The workflow cancels any parked entries
  (`cancel_input(interrupted)`) and cancels the activity. Activity cancellation arrives
  within `0.8 × 20 s` (§2.3), so the backstop covers a missed notify. It is not the main
  path.
- **What stays.** The warm process survives an interrupt. `q.interrupt()` ends the turn,
  not the process.

## 6. Everything else a session does

### 6.1 Send, attach, events

- **Send.** `SessionManager.send` claims the row as `DurableTurns.send` does
  (`durable.ts:198-214`) and starts or updates `AgentSession`
  (`executeUpdateWithStart('send_message')`, update id = turn id, `durable.ts:264-300`).
  The answers keep their meanings: `busy`, `closed`, `budget_exhausted` and
  `OutcomeUnknown`.
- **Events.** The activity writes `ai_session_events` directly through the classic
  translation (`agent/src/sessions/sdkEvents.ts`), with `event_seq` and `pg_notify`. So
  the chat socket, attach and the panel are unchanged.
- **What retires.** `follow_session` and `durable_offset` (`session/events.py`) retire
  with the Python worker.

### 6.2 The transcript, sealed

- `ai_session_entries.entry` is sealed under the session's data key: the
  `ai_payload_keys` row for subject `session-<id>`, the same key the codec uses
  (`payloadCodec.ts:151-190`). The format is `secrets.ts`'s sealed format, with AAD
  `ai_session_entries:<session_id>:<subpath>:<uuid|seq>`.
- `PostgresSessionStore` (`store.ts:59-123`) seals in `append` and opens in `load`. It is
  the only reader of the column. The others only name the table in comments, or delete
  from it (`forget.ts:88`).
- **Forgetting becomes crypto-shredding.** After the key row is deleted, entries in
  Postgres backups are unreadable too. A migration seals the existing rows (plan Task 3.2).
- **What never enters Temporal.** The transcript is not in workflow state, so no Temporal
  payload carries it. Payloads that do carry content (the message text, the gate entries,
  a continuation's tool result) stay sealed by the codec as today.

### 6.3 Fork

- Fork is classic's: `sdkForkSession(id, {sessionStore, upToMessageId})`
  (`manager.ts:2285-2381`). It works for every session, and the `durable` refusal goes
  (`manager.ts:2300-2305`).
- Through the sealing store, the copy is opened under the parent's key and sealed under
  the child's.
- The child's workflow starts at its first send.

### 6.4 Handoff

The row's owner changes, as today. Then:

- the workflow is signalled `owner_changed`;
- parked entries are cancelled (`cancel_input(handoff)`, `durable.ts:388-399`);
- the warm entry is evicted, because the owner is in the fingerprint.

### 6.5 Done, archive, forget

- **Done** sends the `end` Signal (`edits.ts:109`) and evicts the warm entry.
- **Archive** is unchanged (`edits.ts:76-90`). It is refused while a turn runs or anything
  is parked.
- **Forget** is `forgetSubject`, which deletes the key row, then the workflow, then the
  rows (`forget.ts:50-90`). It also sends `NOTIFY scadbuddy_warm_evict` so every replica
  closes the session's process.

### 6.6 Budget

- **The claim.** The pooled lineage check stays in the claim (`POOL_COST < POOL_BUDGET`,
  `manager.ts:758-763`, `1249`).
- **The process cap.** Each spawn gets `maxBudgetUsd` = the lineage's remaining amount.
  Each turn's cost is the *difference* between `total_cost_usd` before and after that turn
  in one process (E5, §2.2), and it is written as classic writes it today
  (`manager.ts:1944-1957`, `2045-2051`).
- **Recycling.** The reuse rule (§5.3) respawns whenever the remaining amount moved
  outside this process.
- **`raiseBudget`** is unchanged (`manager.ts:2393-2452`).
- **What is fixed.** The Python runner counted segments as turns
  (`session/runner.py:700-706`). Unified, `turns` counts user turns again.

### 6.7 Credential, KEK, security

- **The credential.** It is opened per spawn by the `CredentialPool`, and goes only into
  the query's `env` (`credentialEnv`, `run.ts:233`, `383-389`), as classic does today.
  - A warm process holds its credential in its environment for its life. That is at
    most `warm_idle_seconds` past its last turn, or until its credential row changes
    (fingerprint, plus `NOTIFY`).
  - A fallback (`runWithFallback`, `manager.ts:1793`) means a respawn with the next
    credential.
- **Plugin vetting is unchanged** (`agent/src/harness/plugins.ts`,
  `plugins/packages/vet.ts`). Nothing that starts a process inherits the credential env.
- **The KEK** is needed by the agent alone: for the codec, the sealing store, credentials
  and payload keys. The `agent-durable` container, its Secret mount and its copy of the
  credential path all go.
- **Background tasks.** `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` stays (`run.ts:387`).
  A warm process must not run work the workflow cannot see (§8).
- **The remaining residual risk** is the durable spec §9 entry: a worker on the Temporal
  frontend could send `respond`. Fewer workers now hold a Temporal client.

### 6.8 Subagents, packages, built-ins, memory, images

All of these are classic's, because the turn is classic's:

- **Subagents** are in-process, through the `Agent` built-in and the own plugin's
  `agents/` (`agent/src/harness/ownPlugin.ts`), with the turn's whole tool set and tier
  seam. `routes/subagentMcp.ts`, `subagentToken.ts` and `session/subagents.py` retire.
- **Packages** load from `<stateDir>/plugins/` and are re-hashed before every spawn, as
  classic does today. `temporal/turnPackages.ts` retires, along with the payload volume
  copies under `plugins/<content hash>/` that only Python read.
- **`allow_refused` built-ins** are all offered, as in classic, not only `Bash` and
  `PowerShell`.
- **Memory** is the classic Hindsight hooks (`agent/src/memory/hindsight.ts`).
  `memoryActivities.ts` retires.
- **Tool-result images** are classic's: `view_tool_images` keeps working on the session's
  blobs.

### 6.9 Tracing and dashboards

- **Spans.** `TurnTrace` (`agent/src/telemetry/turn.ts`) wraps each turn inside the
  activity. The span links to the workflow's `traceparent`, which is carried in the
  `session_turn` input. A park ends the turn's spans and a continuation opens
  `agent.turn.resume`, as it does today.
- **New span names**, as `*_SPAN` constants, so `lint-dashboard.sh` sees them:
  - `agent.turn.spawn`, with attribute `warm.outcome` = `hit|miss|recycle`, and on a
    recycle `warm.reason` = `fingerprint|tail|budget|evicted`;
  - `agent.turn.release`;
  - `agent.turn.continue`.
- **Dashboard.** The agent has no Prometheus metrics of its own. Its dashboard reads span
  metrics. `deploy/grafana/scadbuddy-agent.json` gains four panels:
  - warm hit ratio;
  - spawn latency (p50/p95) by outcome;
  - releases per hour;
  - continued turns by kind.
- **Temporal's own metrics** for `agent-sessions` (schedule-to-start, task latency) come
  from the Temporal server's dashboards, as for other queues.

### 6.10 Scaling

- **Three replicas, three warm caches.** A session's turns go to its home while that
  replica lives and has room. Otherwise they go to any replica, which becomes the new
  home.
- **One replica per session.** The workflow schedules one activity at a time, and the
  transcript-tail check (§5.3) catches a process that went stale.
- **Shared queue capacity.** The shared queue is served by every replica.
  `maxConcurrentActivityTaskExecutions` for `session_turn` is set to
  `warm_max_per_replica + 2`, so a replica with a full cache still takes cold turns, and
  evicts its LRU entry to make room.

## 7. Exactly-once, call by call

E3 and E4 settle the core question: **Claude Code never re-runs a call whose result it
did not record. It marks the call "outcome unknown" for the model.** So the runtime
never runs a tool twice by itself. What remains is the model choosing to call again, and
a host-side run of an approved call after a release.

| Situation | What runs | Why it is at most once |
|---|---|---|
| A read or write call, process killed mid-call | the call may or may not have finished | Claude Code does not re-run it (E4). The model is told "unknown", and the restart message says to check before repeating. |
| An outward call killed mid-call after approval | the same | It is not re-run. A repeat by the model is a new `tool_use_id`, so it parks again and a person sees it. |
| An approved call after a release (§5.4, step 5) | the activity runs it once | The activity is retried only if it fails before it records the run. The backend command carries `Idempotency-Key: <request id>`, so a retried activity's second send is answered from the `operations` record, not run again. Agent-side commands use `AgentOperation` with the same key. |
| A tool's own re-send after an answer that never arrived | the backend dedupes | Unchanged: `command()` re-sends with the same key (`agent/src/tools/command.ts`). |
| A `turn` re-run by Temporal (attempt 2) | the turn resumes from the transcript | It is not replayed from the prompt, and calls in flight are unknown (E4). |

**The change this needs.** Idempotency keys stop being `randomUUID()` per call
(`tools/command.ts:148`, `tools/customizer.ts:209`, `tools/flows.ts:307`). They become
`call:<session>:<tool_use_id>`, or the gate's request id for a released call. Then a key
is stable for one model call, across every host path that runs it. The ids reach a tool's
`ctx` from `canUseTool`'s `toolUseID` (classic) or from the continuation.

**What cannot be made exactly-once.** An outward effect outside ScadBuddy, such as a remote
plugin's tool or `http_request`, that was cut off mid-call has an unknown outcome. That
was already true for classic, and for durable's retried segments.

## 8. Background tasks as workflows

Another agent is adding Claude Code's background built-ins (#2393; local branch
`feat/background-builtins`). That branch leaves `CronCreate`, `CronDelete`, `CronList`,
`ScheduleWakeup`, `Monitor`, `Workflow`, `TaskStop` and `PushNotification` out of durable
sessions, because they "only work inside one Claude Code process". The unified runtime
gives them a single home:

- **Each one is a workflow on `agent-tools`,** started by its tool. The tool runs in the
  turn, through the ordinary tier seam.
  - `CronCreate` is a Temporal Schedule. `ScheduleWakeup` is a timer workflow. `Monitor`
    is a workflow with a heartbeating poll activity.
  - When one fires, it sends `send_message` to `session-<id>` with author `system:<kind>`,
    the same path as a user's send. The turn then runs as any other.
- **Their ids are `session-<id>-<kind>-<n>`.** The codec's subject pattern
  (`payloadCodec.ts:45`) widens to `^(session|flow)-<uuid>(-[a-z0-9-]+)?$`, with subject
  `session-<uuid>`. Their prompts are then sealed under the session's key, and
  `forgetSubject` also terminates every workflow whose id has that prefix (a Visibility
  query on `WorkflowId STARTS_WITH`). **Unverified**: Task 9.2 checks that the deployed
  Temporal's Visibility store supports `STARTS_WITH` on `WorkflowId`.
- **Why not in the process.** A warm process is evicted after 10 minutes idle and
  recycled on any change, and a cron that lives in it dies with it.
- This spec fixes only the contract above. The other agent's plan owns the tools
  themselves.

## 9. Flows

- `ProjectWorkflow` does not talk to sessions directly. It calls the tools
  `sessions_start` (with `mode: "durable"`), `sessions_send`, `flow_session_check` and
  `flow_session_turn` as activities on `agent-tools`
  (`backend/scadbuddy/workflows/flow_tools.py:795-948`). Those reach `SessionManager`,
  which now starts `AgentSession`. **Nothing in `backend/` changes.**
- `flow_session_check` refuses a classic session (`agent/src/temporal/flowSessions.ts:50`).
  After unification every session qualifies, and the check keeps only its owner test.
- A flow's own `tool()` calls keep running as `toolActivities` on `agent-tools`. They are
  not session turns. `turn:<server>__<tool>` stays for flows only (§11).
- `mode` in `sessions_start` input is accepted and ignored (§10.1). The tool schema keeps
  it so existing flow scripts still type-check (`backend/scadbuddy/flows/typecheck.py`).

## 10. Migration

### 10.1 The mode switch

- **Classic as its own mode is retired.** There is no Temporal-free turn path left to
  fall back to.
  - The backend already requires Temporal (#546).
  - The agent's Temporal is optional only in configuration
    (`main.ts:482-539`, `temporal ? … : undefined`).
  - `durable` is already the default mode (`docs/superpowers/plans/2026-10-09-durable-phase-5d-mode.md` Ruling 1).
  - Keeping a no-Temporal fallback would keep a second driver to test. The two would
    share `TurnRunner`, but the claim, park and restart code would differ, and that is
    exactly the drift the user asked to end.
- **Without Temporal configured, the agent refuses to start sessions** with `unavailable`,
  as an explicit durable request does today (`manager.ts:1154-1161`). The credential test
  and `/mcp` work without Temporal.
- **Tests** drive `TurnRunner` directly (unit), or the workflow on a dev server
  (`*.temporal.test.ts`, which the `agent` CI job already runs).
- **`ai_sessions.mode`** stops being read. New rows get `'durable'` until a later migration
  drops the column (plan Phase 10). Everywhere `mode` is accepted it is ignored:
  - the chat socket;
  - `StartBody`;
  - `sessions_start`;
  - `ai_settings.session_mode` and its routes.

  The UI's mode picker and the Durable badge go.
- **Existing classic sessions need no data migration.** Their transcript is already in
  `ai_session_entries`. A classic session's next send starts its `AgentSession`.

### 10.2 In-flight `DurableSession`s

`DurableSession` workflows are long-running: one per durable session that was never marked
done. Their conversation exists only in workflow state.

1. **New durable sends go to `AgentSession`** once Phase 7 ships. The flag
   `session_runtime=unified` comes first (Phase 5) and becomes the only path in Phase 7.
2. **Lazy migration on next send.**
   - When `send` finds an open `DurableSession` for the session (`describe` reports the
     workflow type), it first cancels its parked entries: `cancel_input(migrated)`.
   - It reads the conversation through the plugin's `__claude_agent_conversation` Query,
     page by page, and appends the entries to `ai_session_entries` through the sealing
     store. They are idempotent by `uuid`.
   - It sends `end` and waits for the workflow to close, then starts `AgentSession`.
   - **Unverified:** that those entries are `SessionStoreEntry`s the TypeScript CLI 2.1.296
     resumes. The Python runner seeds an in-memory `SessionStore` from them
     (`PLUGIN/_runner.py:25-31`), which suggests the SDK transcript format. Task 7.1
     verifies it on a recorded history before anything else in Phase 7.
3. **Backfill.** An operator script, `node dist/migrate-durable-sessions.js`, migrates
   every open `DurableSession` the same way. It runs once Phase 7 is deployed.
4. **Retirement.** `agent-durable` keeps running only to answer those Queries. It is
   removed (Phase 8) after the backfill reports zero open `DurableSession`s, so no
   history is ever left without a worker.
5. **Columns.**
   - `durable_turn_id` stays: it becomes the turn id the workflow owns.
   - `durable_offset` is dropped in Phase 10.
   - `turn_tiers` stays, since the activity reads it as the sender's tiers.
   - `ai_pending_input` and `ai_input_responses` stay; they are the gate.
   - `ai_approvals` stays for `/mcp` prepare/confirm only. Session approvals move to the
     gate in Phase 5, and `ai_questions` retires with the classic question path.

## 11. What retires

- **`agent-durable/`, entirely,** with:
  - the Dockerfile stage `agent-durable`;
  - the `agent-durable` CI job;
  - `.github/scripts/agent-durable-pin.sh` and `agent-durable-pin.yml`;
  - the `temporalio-claude-agent-sdk` pin;
  - the Python `claude-agent-sdk` pin;
  - the clusters `agent-durable` container (a clusters PR, after Phase 8).
- **Vector files.** `agent/test/fixtures/secret-vectors.json` and `payload-vectors.json`
  stay as TypeScript self-tests (format stability). The Python halves go, along with
  `pending-input-vectors.json`'s Python consumer.
- **Agent files that existed only for durable turns:**
  - `temporal/turnTools.ts` and `turnServers.ts` (session use; the flow `turn:` path stays,
    see below);
  - `turnPackages.ts`;
  - `memoryActivities.ts`;
  - `subagentToken.ts` and `routes/subagentMcp.ts`;
  - `sessionClients.ts` (the warm process keeps the plugin connection);
  - `gate/sweep.ts`'s describe-based orphan pass (the workflow owns its entries);
  - `sessions/durableSweep.ts`.

  The `turn:<server>__<tool>` activity and `turn_tool_tier` stay for **flows**
  (`flow_tools.py:651-723`), built on the same `turnServers` code.
- **Classic-only code:**
  - the row lease, renew loop and reaper (`manager.ts:351-354`, `1514-1530`, `2505-2538`);
  - `resumeApproved` (replaced by §5.4 step 5);
  - the classic session approval and question gates (`gate/classic.ts`, the
    `ApprovalService.gate` path for sessions).
- **Docs:**
  - durable spec §6.2–§6.3b and the §6.6 "Classic sessions" part are marked superseded,
    pointing here;
  - the parity plan is marked complete or superseded;
  - `docs/ai/operating.md`;
  - `CLAUDE.md`'s layout bullets for `agent-durable/` and the commands block.

## 12. Risks, unknowns, follow-ups

- **Unverified, checked by a plan task before it is relied on:**
  - the DurableSession transcript resumes under the TypeScript CLI (Task 7.1);
  - Visibility `STARTS_WITH` (Task 9.2);
  - that `q.close()` during a held `canUseTool` writes E3's synthetic result and not
    E4's (Task 5.4). Both are error results the continuation message overrides, so this
    only changes the wording the model sees.
- **The `Recreate` deploy restarts every turn** (§5.5, D6).
- **Warm memory.** 4 × about 206 MiB, plus remote MCP connections, against a 2Gi limit
  (D3). The `agent.turn.spawn` panel shows whether the hit ratio justifies a larger cap.
- **SDK `sessionStore` is `@alpha`** (`sdk.d.ts:1826-1834`). Classic has relied on it
  since #828 (`8ee7a89f4`), with eager flush. Task 1.1's pinned tests catch a behaviour change at a bump.
- **The flow turn-tool hash bug** (`turnTools.ts:422`). It is independent of this design
  and filed as #2728, since flows keep that path.

## 13. Open decisions for the user

- **D1. Retire classic as a mode** (recommended, §10.1), or keep a Temporal-free fallback
  driver that shares `TurnRunner`.
- **D2. `park_hold_seconds` default.** 300 s is recommended: below the 600 s approval
  expiry, so most approvals are answered warm, and the process is released after that.
- **D3. Warm cache size.** 4 per replica at the current 2Gi limit is recommended. The
  alternative is raising the agent's limit to 3Gi and the cap to 8. That is a clusters
  change.
- **D4. `AUTO_UPGRADE` plus continue-as-new per turn** (recommended), or Pinned with drain.
- **D5. Turn wall clock** `startToCloseTimeout: 2h`, which is new; classic has none.
- **D6.** Move the ScadBuddy pod to `RollingUpdate` once `library` and `agent-sessions`
  are versioned, so a deploy stops restarting every turn at once. This is a clusters
  change, outside this plan's PRs.
- **D7. Seal `ai_session_entries`** (recommended, §6.2), so forgetting a session also
  shreds the transcript in backups, as for durable payloads today.
