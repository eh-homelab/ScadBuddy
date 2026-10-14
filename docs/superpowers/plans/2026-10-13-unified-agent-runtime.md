# Unified agent runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every agent session runs the classic TypeScript turn, as an activity of a TypeScript `AgentSession` workflow. The activity lands on the replica that keeps the session's Claude Code process warm, so there is one tool and permission layer and no second mode. `agent-durable/` is retired.

**Architecture:**
- Session affinity follows Go Worker Sessions, built on worker-specific queues (spec §5.2).
- `runTurn`'s assembly becomes `TurnRunner`. A per-replica `WarmSessions` cache keeps one streaming-input `query()` per session, reused only while a fingerprint, the transcript tail and the remaining budget match.
- `AgentSession` (workflow id `session-<id>`, queue `agent-sessions`) schedules `session_turn` on its home queue `agent-tools.<hostname>` and falls back to `agent-tools`.
- Parks go through the workflow (Update `open_input`, `ai_pending_input`). The activity holds a park for up to `park_hold_seconds`, then releases it. The next activity runs the approved call once and resumes.
- The transcript stays in `ai_session_entries`, now sealed under the session's data key.

**Tech Stack:** TypeScript (agent, Claude Agent SDK 0.3.296 / Claude Code 2.1.296, `@temporalio/*` 1.24.0, postgres.js, vitest), Postgres, Temporal. Python only for reading old `DurableSession` histories during migration.

**Spec:** `docs/superpowers/specs/2026-10-13-unified-agent-runtime-design.md` (read it first; §2 holds the measured facts, §13 the user's decisions D1–D7).

## Global Constraints

- **One turn path.** Nothing under `agent/src/harness/` or `agent/src/sessions/turnRunner.ts` branches on a session mode. Task 6.3's `lint-one-runtime.sh` enforces it from Phase 6 on.
- **Tiers.** Every outward call parks for a human. A tier is resolved when the call runs (`effectiveTool`, #2088), never at declaration.
- **The credential** goes only into a query's `env` (`credentialEnv`, `agent/src/harness/run.ts:233`). It never reaches a tool server, a plugin, a file, a log or a Temporal payload.
- **No transcript in Temporal.** The transcript never enters a Temporal payload. Session payloads stay sealed by `payloadCodec.ts`.
- **Workflow changes.** `AgentSession` changes after Phase 5 merges go behind `patched()`, and `agent/test/fixtures/agent_session_histories/` replays. `AgentOperation` is unchanged.
- **Migrations.** An agent migration is a NEW file, `agent/src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`. Never edit a merged one.
- **Workflow ids.** They stay `session-<uuid>`. The workflow type is `AgentSession` on queue `agent-sessions`. `DurableSession` on `agent` is never served by the TypeScript worker.
- **Settings (D2/D3/D5, decided).** These are `ai_settings` keys with defaults in code, read through `GET`/`PUT /api/v1/ai/settings/session-runtime` (Task 2.5):
  - `park_hold_seconds` 300 (30–3600);
  - `warm_max_per_replica` 8 (0–32);
  - `warm_idle_seconds` 600 (60–86400);
  - `turn_max_seconds` 7200 (300–86400).
- **Fixed:** the `session_turn` heartbeat timeout is 20 s with a heartbeat every 5 s, the home schedule-to-start is 5 s, there is 1 activity attempt and 3 recovery cycles.
- **Clusters.** Clusters PRs are on `eh-homelab/clusters` and follow that repo's conventions. Each is merged only together with the ScadBuddy phase that needs it. Never touch prod by hand.
- **Shared database.** Test suites share the Postgres `sb-parity-pg` with other agents, so run one suite at a time and use a database of your own (`scadbuddy_test_unify`).
- **Commits** end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01SoD8BcfTLQMNygteacYevp
  ```
- **PRs.** Each phase is one PR, titled conventional-commit style, with a body that says `Refs #2726`.
- **Tests.**
  - Agent tests run with `cd agent && pnpm lint && pnpm typecheck && pnpm test`.
  - Postgres tests need `SCADBUDDY_TEST_DATABASE_URL`.
  - Temporal tests need `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` or `temporal` on `PATH`.
- **Sources.** Docs cite their sources, and measured facts say where and how they were measured (#259).

## Review Focus

1. **A warm process recycled while a Playwright page is open.** Its upstream MCP session must be deleted, not leaked; this includes a dead pod's sessions after a `Recreate` deploy. Test: Task 2.4.
2. **A warm process after a credential is disabled.** The next turn must spawn with the next credential and never reuse the old env. Test: Task 2.2, `recycles when the credential row changes`.
3. **A turn that ran on another replica while this one stayed warm** (a fallback and back). The stale process must not answer from an old transcript. Test: Task 2.2, `recycles when the transcript tail moved`.
4. **An approval granted after the hold released the process.** The call runs exactly once, with the request id as its Idempotency-Key, even if the continuing activity is retried. Test: Task 5.4, `an approved released call runs once across an activity retry`.
5. **An interrupt while parked and held.** The entry is cancelled, the turn ends `interrupted`, the process survives, and no further model call is made. Test: Task 5.6, `interrupt while held cancels the entry and keeps the process`.
6. **A send to a session whose `DurableSession` is still open.** It is migrated before the turn runs, its parked entries are cancelled `migrated`, and the old workflow closes. Test: Task 7.2, `a send migrates an open DurableSession first`.

---

## Phase 1: Pin the SDK facts, extract `TurnRunner` (PR 1, `refactor(agent): one TurnRunner for every turn`)

### Task 1.1: The SDK behaviours this design rests on, as tests

**Files:**
- Create: `agent/test/sdkRuntime.sdk.test.ts`
- Create: `agent/src/harness/inbox.ts`

**Interfaces:**
- Produces: `class Inbox implements AsyncIterable<SDKUserMessage> { push(message: SDKUserMessage): void; close(): void }` in `agent/src/harness/inbox.ts`.

- [ ] **Step 1: Write the tests.** They port spec §2.1's E1, E4 and E5, using `startFakeAnthropic` and `bundledCliPath()`:

```ts
import { spawn } from 'node:child_process'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createSdkMcpServer, InMemorySessionStore, query, type SDKMessage, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { Inbox, userMessage } from '../src/harness/inbox.js'
import { type FakeAnthropic, startFakeAnthropic } from './support/fakeAnthropic.js'

// Spec 2026-10-13 §2.1: what the unified runtime relies on in the bundled Claude Code.
// A failure here after an SDK bump means the spec's facts moved: re-measure, then decide.
const KEY = 'sk-ant-api03-unit-test-key-000011112222'
let fake: FakeAnthropic | undefined
afterEach(async () => fake?.close())

async function env(f: FakeAnthropic) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'sdk-runtime-'))
  return { ANTHROPIC_API_KEY: KEY, ANTHROPIC_BASE_URL: f.url, CLAUDE_CONFIG_DIR: dir, HOME: dir, PATH: process.env.PATH, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1' }
}
const base = () => ({ pathToClaudeCodeExecutable: bundledCliPath(), tools: [], settingSources: [] as [] })

async function nextResult(it: AsyncIterator<SDKMessage>) {
  for (;;) {
    const { value, done } = await it.next()
    if (done) throw new Error('query ended')
    if (value.type === 'result') return value
  }
}

describe('the bundled Claude Code (spec §2.1)', () => {
  it('E1: one streaming-input query is one process for several turns', async () => {
    let n = 0
    fake = await startFakeAnthropic(() => ({ text: `reply ${++n}` }))
    let spawns = 0
    const inbox = new Inbox()
    const q = query({ prompt: inbox, options: { ...base(), env: await env(fake),
      spawnClaudeCodeProcess: (o) => { spawns++; return spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'inherit'] }) } } })
    const it = q[Symbol.asyncIterator]()
    for (const text of ['one', 'two', 'three']) { inbox.push(userMessage(text)); await nextResult(it) }
    expect(spawns).toBe(1)
    expect(fake.messageCalls().at(-1)?.body?.messages?.filter((m) => m.role !== 'system').length).toBe(5)
    q.close()
  }, 120_000)

  it('E4: a killed call is never re-run on resume and is marked unknown', async () => {
    let n = 0
    fake = await startFakeAnthropic(() => (++n === 1 ? { toolUse: { name: 'mcp__x__slow', input: { a: 1 } } } : { text: `reply ${n}` }))
    const store = new InMemorySessionStore()
    let started!: () => void
    const running = new Promise<void>((r) => (started = r))
    let calls = 0
    const server = createSdkMcpServer({ name: 'x', tools: [tool('slow', 'slow', { a: z.number() }, async () => {
      calls++; started(); await new Promise((r) => setTimeout(r, 60_000)); return { content: [{ type: 'text', text: 'done' }] } })] })
    let child: ReturnType<typeof spawn> | undefined
    let sessionId = ''
    const q = query({ prompt: 'go', options: { ...base(), env: await env(fake), sessionStore: store, sessionStoreFlush: 'eager',
      mcpServers: { x: server }, allowedTools: ['mcp__x__slow'],
      spawnClaudeCodeProcess: (o) => (child = spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'inherit'] })) } })
    void (async () => { try { for await (const m of q) if ('session_id' in m && m.session_id) sessionId = m.session_id } catch {} })()
    await running
    await new Promise((r) => setTimeout(r, 500))
    child?.kill('SIGKILL')
    await new Promise((r) => setTimeout(r, 1500))
    const before = fake.messageCalls().length
    let asked = false
    for await (const _ of query({ prompt: 'continue', options: { ...base(), env: await env(fake), sessionStore: store, resume: sessionId,
      mcpServers: { x: server }, canUseTool: async (_n, input) => { asked = true; return { behavior: 'allow', updatedInput: input } } } })) {}
    const resumed = JSON.stringify(fake.messageCalls()[before]?.body?.messages)
    expect(calls).toBe(1)
    expect(asked).toBe(false)
    expect(resumed).toContain('outcome is unknown')
  }, 120_000)

  it('E5: maxBudgetUsd is cumulative per process; maxTurns is per message', async () => {
    let n = 0
    fake = await startFakeAnthropic(() => ({ text: `reply ${++n}` }))
    const subtypes = async (opts: object) => {
      const inbox = new Inbox()
      const q = query({ prompt: inbox, options: { ...base(), env: await env(fake!), model: 'claude-sonnet-4-5', ...opts } })
      const it = q[Symbol.asyncIterator]()
      const out: string[] = []
      for (let i = 0; i < 3; i++) { inbox.push(userMessage(`t${i}`)); out.push((await nextResult(it)).subtype) }
      q.close()
      return out
    }
    expect(await subtypes({ maxBudgetUsd: 0.00012 })).toEqual(['success', 'error_max_budget_usd', 'error_max_budget_usd'])
    expect(await subtypes({ maxTurns: 2 })).toEqual(['success', 'success', 'success'])
  }, 120_000)
})
```

- [ ] **Step 2: Run the tests and watch them fail.** Run `cd agent && pnpm exec vitest run test/sdkRuntime.sdk.test.ts`. Expected: FAIL, `Cannot find module '../src/harness/inbox.js'`.
- [ ] **Step 3: Implement `inbox.ts`.**

```ts
import type { SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'

// A streaming-input prompt (spec 2026-10-13 §5.3): one query, many turns. push() wakes
// the iterator; close() ends it, which closes Claude Code's stdin and so the process.
export class Inbox implements AsyncIterable<SDKUserMessage> {
  private readonly queue: SDKUserMessage[] = []
  private wake: (() => void) | undefined
  private closed = false
  push(message: SDKUserMessage): void {
    if (this.closed) throw new Error('inbox closed')
    this.queue.push(message)
    this.wake?.()
  }
  close(): void {
    this.closed = true
    this.wake?.()
  }
  async *[Symbol.asyncIterator](): AsyncIterator<SDKUserMessage> {
    for (;;) {
      while (this.queue.length > 0) yield this.queue.shift() as SDKUserMessage
      if (this.closed) return
      await new Promise<void>((resolve) => (this.wake = resolve))
    }
  }
}

export function userMessage(content: SDKUserMessage['message']['content']): SDKUserMessage {
  return { type: 'user', message: { role: 'user', content }, parent_tool_use_id: null, session_id: '' }
}
```

- [ ] **Step 4: Run the tests again.** Same command. Expected: 3 passed (about 15 s).
- [ ] **Step 5: Commit** with `git add agent/src/harness/inbox.ts agent/test/sdkRuntime.sdk.test.ts && git commit -m "test(agent): pin the bundled Claude Code behaviours the unified runtime rests on"`.

### Task 1.2: Extract `TurnRunner` from `SessionManager.runTurn`

**Files:**
- Create: `agent/src/sessions/turnRunner.ts`, `agent/src/sessions/turnSlot.ts`
- Modify: `agent/src/sessions/manager.ts:1456-1960` (`runTurn` delegates)
- Test: `agent/test/turnRunner.test.ts`; existing `sessions.manager.test.ts`, `sessions.e2e.test.ts`, `run.test.ts` unchanged and green

**Interfaces:**
- Produces:
  - `type TurnSlot = { current?: TurnContext; set(ctx: TurnContext): void; clear(): void }`;
  - `type TurnContext = { turnId: string; principal: TurnPrincipal; approvalGate: ApprovalGate; questionGate?: QuestionGate; auditor?: ToolAuditor; trace: TurnTrace; http?: HttpState; signalInterrupt(): void }`;
  - `class TurnRunner { constructor(deps: TurnRunnerDeps); assemble(session: ClaimedSession, principal: TurnPrincipal, slot: TurnSlot): Promise<AssembledTurn> }`;
  - `type AssembledTurn = { run: Omit<HarnessRun, 'prompt' | 'credential' | 'signal'>; candidates: CredentialCandidate[]; fingerprintParts: FingerprintParts }`;
  - `type FingerprintParts = { credentialId: string; credentialUpdatedAt: string; model: string | null; ownPlugin: boolean; plugins: { id: string; url: string; headerVersion: string }[]; packageHashes: string[]; tiers: string[] | null; owner: string; systemAppend: string; disabledTools: string[]; builtinTools: string[] }`.
- Every callback in `run` (`approvalGate`, `questionGate`, `tierOf`, hooks, in-process server `ctx`) reads `slot.current`. With no current turn it denies: `{ behavior: 'deny', message: 'no turn is running' }`.

- [ ] **Step 1: Write the failing test.**

```ts
import { describe, expect, it } from 'vitest'
import { createTurnSlot } from '../src/sessions/turnSlot.js'
import { turnRunnerForTest } from './support/turnRunner.js'

describe('TurnRunner', () => {
  it('assembles callbacks that read the current turn, and deny between turns', async () => {
    const slot = createTurnSlot()
    const { runner, session, principal, gateCalls } = await turnRunnerForTest()
    const turn = await runner.assemble(session, principal, slot)
    const decide = turn.run.canUseTool!
    expect(await decide('mcp__scadbuddy__send_to_bambuddy', {}, { signal: new AbortController().signal, toolUseID: 't1' } as never))
      .toMatchObject({ behavior: 'deny', message: 'no turn is running' })
    slot.set({ ...gateCalls.context, turnId: 'turn-1' })
    await decide('mcp__scadbuddy__send_to_bambuddy', {}, { signal: new AbortController().signal, toolUseID: 't1' } as never)
    expect(gateCalls.seen).toEqual([{ turnId: 'turn-1', toolUseId: 't1' }])
  })

  it('names every HarnessRun key either in the fingerprint or as per-turn', async () => {
    const { runner, session, principal } = await turnRunnerForTest()
    const turn = await runner.assemble(session, principal, createTurnSlot())
    const fixed = Object.keys(turn.run).sort()
    expect(fixed).toEqual(TurnRunner.FIXED_AT_SPAWN.slice().sort())
  })
})
```

  `test/support/turnRunner.ts` builds a `TurnRunner` over the same fakes `sessions.manager.test.ts` uses for `SessionManager` (move them there, unchanged). `TurnRunner.FIXED_AT_SPAWN` is a `readonly string[]` exported beside the class. Task 2.1 asserts each entry maps to a `FingerprintParts` field or is a slot-reading callback.
- [ ] **Step 2: Run it and watch it fail.** Run `pnpm exec vitest run test/turnRunner.test.ts`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement.**
  - Move the body of `runTurn` from the start of the assembly (`manager.ts:1466`, `tierOf`) to the end of the options object (`manager.ts:1792`) into `TurnRunner.assemble`.
  - Replace each per-turn closure with a slot reader, for example:

    ```ts
    approvalGate: (req) => { const t = slot.current; return t ? t.approvalGate(req) : Promise.resolve({ behavior: 'deny', message: 'no turn is running' }) },
    ```

  - `runTurn` now does three things:
    1. `const turn = await this.runner.assemble(session, principal, slot)`;
    2. `slot.set(ctx)`;
    3. the existing `runWithFallback(...)` with `{...turn.run, prompt, signal: controller.signal}`.
  - Behaviour is identical: one query per turn.
- [ ] **Step 4: Run the whole agent suite.** Run `pnpm lint && pnpm typecheck && pnpm test`. Expected: PASS, and the existing session tests are unchanged.
- [ ] **Step 5: Commit** with `refactor(agent): TurnRunner assembles every turn; callbacks read the turn slot`.

---

## Phase 2: Warm processes for every turn (PR 2, `perf(agent): keep a session's Claude Code warm between turns`)

This phase ships the latency win to today's classic sessions on its own. It needs no Temporal.

### Task 2.1: The fingerprint

**Files:**
- Create: `agent/src/sessions/warm.ts` (fingerprint part)
- Test: `agent/test/warm.fingerprint.test.ts`

**Interfaces:**
- Produces: `fingerprint(parts: FingerprintParts): string`, the sha256 hex of canonical JSON with sorted keys and sorted arrays.

- [ ] **Step 1: Write the test.**

```ts
import { describe, expect, it } from 'vitest'
import { fingerprint } from '../src/sessions/warm.js'
import { TurnRunner } from '../src/sessions/turnRunner.js'

const parts = { credentialId: 'c1', credentialUpdatedAt: '2026-10-13T00:00:00Z', model: null, ownPlugin: true,
  plugins: [{ id: 'p', url: 'https://x', headerVersion: '1' }], packageHashes: ['h2', 'h1'], tiers: null,
  owner: 'user:alice', systemAppend: 'a', disabledTools: [], builtinTools: [] }

describe('fingerprint', () => {
  it('ignores array order and changes with any field', () => {
    expect(fingerprint({ ...parts, packageHashes: ['h1', 'h2'] })).toBe(fingerprint(parts))
    for (const [k, v] of Object.entries({ credentialId: 'c2', credentialUpdatedAt: 'x', model: 'm', ownPlugin: false, owner: 'user:bob', tiers: ['read'] }))
      expect(fingerprint({ ...parts, [k]: v })).not.toBe(fingerprint(parts))
  })
  it('covers every option fixed at spawn', () => {
    for (const key of TurnRunner.FIXED_AT_SPAWN) expect(TurnRunner.FINGERPRINTED_BY[key], key).toBeDefined()
  })
})
```

  `TurnRunner.FINGERPRINTED_BY: Record<string, keyof FingerprintParts | 'slot'>` lives beside `FIXED_AT_SPAWN`.
- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** `fingerprint` with `createHash('sha256')` over `JSON.stringify(canonical(parts))`, where `canonical` sorts object keys and sorts arrays of primitives. Fill in `FINGERPRINTED_BY`.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): a turn's spawn fingerprint`.

### Task 2.2: `WarmSessions`

**Files:**
- Modify: `agent/src/sessions/warm.ts`
- Test: `agent/test/warm.pg.test.ts` (Postgres for the tail and budget checks), `agent/test/warm.sdk.test.ts` (real CLI against the fake)

**Interfaces:**
- Consumes: `Inbox`, `userMessage` (Task 1.1), `AssembledTurn`, `fingerprint` (Tasks 1.2, 2.1), `runHarness` (`run.ts:484`).
- Produces:

```ts
export type WarmOutcome = 'hit' | 'miss' | 'recycle'
export type RecycleReason = 'fingerprint' | 'tail' | 'budget' | 'evicted'
export type WarmTurn = { entry: WarmEntry; outcome: WarmOutcome; reason?: RecycleReason }
export class WarmSessions {
  constructor(deps: { sql: Sql; maxEntries: number; idleMs: number; now?: () => number; log?: (m: string) => void })
  acquire(sessionId: string, turn: AssembledTurn, credential: Credential, remainingUsd: number): Promise<WarmTurn>
  /** After the turn's result: records the tail and the process's cumulative cost. */
  settle(entry: WarmEntry, result: SDKResultMessage): Promise<void>
  evict(sessionId: string | '*', reason: string): void
  size(): number
  close(): Promise<void>
}
```

- [ ] **Step 1: Write the tests.** Each test runs two turns through `acquire` and `settle` against the fake and asserts the outcome:
  - `reuses a process across turns` → second outcome `hit`, one spawn;
  - `recycles when the credential row changes` → `recycle` / `fingerprint`, and the second spawn's env has the new key;
  - `recycles when the transcript tail moved` → append one entry to `ai_session_entries` between turns, then `recycle` / `tail`;
  - `recycles when the remaining budget moved` → pass a `remainingUsd` that differs by more than 1e-6 from `cap − spent`, then `recycle` / `budget`;
  - `evicts idle entries` → `now` advanced past `idleMs`, and `size()` drops to 0 with the process closed;
  - `evicts LRU past maxEntries` → three sessions with `maxEntries: 2`, so the first is closed.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Spawn:** `runHarness({...turn.run, credential, prompt: inbox, maxBudgetUsd: remainingUsd, resume or sessionId})`.
  - **Tail:** read `SELECT uuid FROM ai_session_entries WHERE session_id=$1 AND subpath='' AND uuid IS NOT NULL ORDER BY id DESC LIMIT 1` before `acquire` decides and after `settle`.
  - **Budget:** check `Math.abs(remainingUsd - (entry.capUsd - (entry.lastTotalUsd - entry.costAtSpawnUsd))) < 1e-6`.
  - **Timers:** an idle timer per entry, `unref`'d.
  - **Close:** `close()` closes every query.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): WarmSessions keeps one Claude Code per session, recycled on any change`.

### Task 2.3: Classic turns use the cache; eviction notifications

**Files:**
- Modify: `agent/src/sessions/manager.ts` (`runTurn`, handoff, done, fork, `stopTurns`), `agent/src/harness/fallback.ts` (`runWithFallback` takes an `acquire`), `agent/src/main.ts` (construct `WarmSessions`, `LISTEN scadbuddy_warm_evict`), `agent/src/credentials.ts`, `agent/src/plugins/registry.ts`, `agent/src/plugins/packages/store.ts` (`NOTIFY scadbuddy_warm_evict, '*'` on change), `agent/src/sessions/forget.ts`
- Test: `agent/test/sessions.e2e.test.ts` (new cases), `agent/test/sessions.manager.test.ts`

- [ ] **Step 1: Write the tests.** In `sessions.e2e.test.ts`:
  - `two sends in a row use one Claude Code process`, counting spawns through the `spawnClaudeCodeProcess` seam that `HarnessRun.spawn` gains for tests;
  - `an interrupt keeps the process`;
  - `handoff evicts`;
  - `a credential change evicts on every replica` (two managers sharing the database);
  - `cost per turn is the difference of total_cost_usd` (`cost_usd` after two turns = 2 × `REPLY_COST_USD`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **`runTurn`:** `const warm = await this.warm.acquire(id, turn, credential, remaining)`, then `slot.set(ctx)`, `warm.entry.inbox.push(userMessage(prompt))`, and read messages from `warm.entry.iterator` until a `result`.
  - **Cost:** `result.total_cost_usd − entry.lastTotalUsd`.
  - **Interrupt:** `entry.query.interrupt()`, never abort.
  - **Fallback:** `runWithFallback` moves to the next candidate by calling `warm.evict(id, 'credential')` and acquiring again.
  - **Shutdown:** `stopTurns` closes the cache after the grace period.
  - **Spans:** emit `agent.turn.spawn` with `warm.outcome` and `warm.reason`, as `WARM_SPAN = 'agent.turn.spawn'` in `agent/src/telemetry/turn.ts`.
- [ ] **Step 4: Run the full agent suite and `pnpm build`.** Expected: PASS.
- [ ] **Step 5: Commit** with `perf(agent): turns reuse the session's warm Claude Code`.

### Task 2.4: Close plugin sessions on recycle; sweep a dead replica's

Spec §5.3 (E7): Claude Code sends no MCP session `DELETE` when its process ends.

**Files:**
- Modify: `agent/src/plugins/forwarder.ts`. Each route records the upstream `mcp-session-id`s it sees in responses. `Registration.terminate(): Promise<void>` sends `DELETE` with each id and the plugin's header to the checked address, with a 5 s timeout and errors ignored, then releases the route.
- Modify: `agent/src/sessions/warm.ts`. An entry keeps its registrations; `evict` calls `terminate()` on each, then `q.close()`.
- Create: `agent/src/plugins/pluginSessions.ts`. It records `(plugin_id, upstream_session_id, replica)` on first sight and deletes the row on terminate. `sweepDeadReplicas(sql, liveReplicas: Set<string>)` sends `DELETE` for every row whose replica is not live, then deletes those rows.
- Create: `agent/src/db/migrations/<ts>_plugin_sessions.sql`:

```sql
CREATE TABLE ai_plugin_sessions (
  plugin_id text NOT NULL,
  upstream_session_id text NOT NULL,
  replica text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (plugin_id, upstream_session_id)
);
```

- Modify: `agent/src/main.ts`. Run the sweep at start and every 5 min. The live replicas are the poller identities from `DescribeTaskQueue('agent-tools')`, and this replica's own identity is always live. `temporal/sessionClients.ts` records its clients' ids too.
- Test: `agent/test/forwarder.test.ts`, `agent/test/pluginSessions.pg.test.ts`, `agent/test/warm.sdk.test.ts`

- [ ] **Step 1: Write the tests.**
  - `evicting a warm entry deletes its upstream MCP session`. Use a fake Streamable HTTP server with session ids (as `test/support/fakeMcp.ts`, with `sessionIdGenerator: randomUUID`), run one turn, then `warm.evict(id, 'test')`. The fake logs exactly one `DELETE` carrying the session's id.
  - `terminate sends the plugin's header`.
  - `the sweep deletes a dead replica's sessions and keeps a live one's`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `fix(agent): close a warm process's plugin sessions; sweep a dead replica's`.

### Task 2.5: The session-runtime settings

**Files:**
- Create: `agent/src/routes/sessionRuntime.ts`, modelled on `agent/src/routes/sessionLimits.ts`: guarded by `uiRequestProblem`/`uiReadProblem`, each key audited as a `settings` row, errors as `{ detail }`. Also create `agent/src/sessions/runtimeSettings.ts`.
- Create: `frontend/src/components/SessionRuntimeSetting.tsx`, beside `SessionLimitsSetting.tsx` in Settings → Assistant. Add its msw handlers in `frontend/src/mocks/features/assistantSessions.ts`, and `getSessionRuntime`/`putSessionRuntime` in `frontend/src/api/client.ts`.
- Modify: `agent/src/main.ts` (`WarmSessions` reads `warm_max_per_replica` and `warm_idle_seconds` at start and on `NOTIFY scadbuddy_settings`; the PUT sends that notify)
- Test: `agent/test/sessionRuntimeRoutes.test.ts`, `frontend/src/components/SessionRuntimeSetting.test.tsx`

**Interfaces:**
- Produces:
  - `type RuntimeSettings = { park_hold_seconds: number; warm_max_per_replica: number; warm_idle_seconds: number; turn_max_seconds: number }`;
  - `RUNTIME_DEFAULTS: RuntimeSettings = { park_hold_seconds: 300, warm_max_per_replica: 8, warm_idle_seconds: 600, turn_max_seconds: 7200 }`;
  - `RUNTIME_BOUNDS`, as spec §13;
  - `readRuntimeSettings(settings: SettingsRepo): Promise<RuntimeSettings>`.
  - The endpoints are `GET /api/v1/ai/settings/session-runtime` → `RuntimeSettings` (the defaults when unset), and `PUT` with a partial body, validated by zod against the bounds and answering 422 `{ detail }` on a value out of range.
- `WarmSessions.resize(max: number, idleMs: number)`. A lowered max evicts idle entries down to it, never one with a turn running.

- [ ] **Step 1: Write the tests.**
  - GET returns the defaults.
  - PUT `{warm_max_per_replica: 2}` stores the value, writes one audit row, and a GET returns it.
  - PUT `{park_hold_seconds: 5}` answers 422.
  - A cross-origin PUT answers 403.
  - `resize(1)` with two idle entries evicts one; with one running and one idle, it evicts the idle one.
  - The frontend form shows the four fields and saves them.
- [ ] **Step 2: Run them.** Run `cd agent && pnpm exec vitest run test/sessionRuntimeRoutes.test.ts test/warm.pg.test.ts`, then `cd frontend && pnpm exec vitest run src/components/SessionRuntimeSetting.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS. Then run each package's full `lint`, `typecheck` and `test`, one suite at a time.
- [ ] **Step 5: Commit** with `feat(agent): session runtime settings (park hold, warm cache, turn limit)`. Open PR 2.

### Task 2.6: Clusters: the agent container's limit to 8Gi (D3)

**Files (eh-homelab/clusters):**
- Modify: `applications/scadbuddy/scadbuddy.yaml`, the `agent` container's `resources.limits.memory` from `2Gi` to `8Gi`, with a comment citing spec §5.3 and D3. The request is unchanged.

- [ ] **Step 1: Read the clusters repo's conventions.** That means its `CLAUDE.md`/`README.md`, the PR template, how ScadBuddy image bumps land, and whether `applications/scadbuddy` changes need a verify workflow. Then check the nodes' allocatable memory in the manifests or inventory docs for three pods at 8Gi limits on separate nodes (`topologyKey: kubernetes.io/hostname`, `scadbuddy.yaml:179`). If the nodes cannot fit it, **stop and ask**.
- [ ] **Step 2:** Branch, edit, and open the PR. Its body links ScadBuddy PR 2 and says it merges only after PR 2 merges.
- [ ] **Step 3:** After ScadBuddy PR 2 merges, drive this PR to green and merge it by that repo's process. Never apply anything to the cluster by hand.

---

## Phase 3: Seal the transcript (PR 3, `feat(agent): seal ai_session_entries under the session's data key`)

### Task 3.1: The sealing store

**Files:**
- Modify: `agent/src/sessions/store.ts` (`PostgresSessionStore` takes `keys: PayloadKeys`), `agent/src/temporal/payloadCodec.ts` (export `PgPayloadKeys.keyFor`, unchanged)
- Create: `agent/src/db/migrations/<ts>_session_entries_sealed.sql`, adding `ALTER TABLE ai_session_entries ADD COLUMN sealed bytea, ALTER COLUMN entry DROP NOT NULL`
- Test: `agent/test/sessions.store.test.ts`

**Interfaces:**
- Produces: `new PostgresSessionStore(sql, { keys })`. `append` writes `sealed = seal(dek, JSON, aad)` with `entry = NULL`. `load` opens `sealed`, or else reads the plaintext `entry` (pre-migration rows). The AAD is `ai_session_entries:<session_id>:<subpath>:<uuid ?? id>`, the row's own `uuid` or (never written, so no two rows share it) its own `id`, never the literal string `'seq'` — a constant AAD would let two such rows in one session and subpath be swapped undetected.

- [ ] **Step 1: Write the tests:**
  - `round-trips sealed entries`;
  - `the row holds no plaintext` (`SELECT entry IS NULL, position('kumquat' in encode(sealed,'escape'))=0`);
  - `a forgotten subject cannot load` (`keys.forget(subject)`, then `load` throws `SubjectForgotten`);
  - `reads legacy plaintext rows`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** with `seal`/`open` from `agent/src/secrets.ts` under the DEK from `keys.keyFor('session-' + id, true)`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): the session transcript is sealed under its data key`.

### Task 3.2: Seal existing rows; fork re-seals

**Files:**
- Create: `agent/src/sessions/sealBackfill.ts` (run at start under advisory lock `SCADSEAL`, in batches of 500 rows, idempotent)
- Modify: `agent/src/main.ts` (start the backfill), `agent/src/sessions/manager.ts:2329-2334` (fork goes through the store, so it opens under the parent's key and seals under the child's)
- Test: `agent/test/sealBackfill.pg.test.ts`, `agent/test/sessions.e2e.test.ts` (`fork of a sealed session resumes in the child`)

- [ ] **Step 1: Write the tests** (plaintext rows → sealed after one run, a second run is a no-op; fork resumes).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): seal existing transcripts; fork re-seals under the child's key`. Open PR 3.

---

## Phase 4: The gate's workflow half in TypeScript (PR 4, `feat(agent): the session gate in TypeScript`)

### Task 4.1: Port `open_input`, `resolve_input` and `grant_input`

**Files:**
- Create: `agent/src/gate/store.ts`, a port of `agent-durable/src/scadbuddy_durable/gate/store.py` (`open_input` :305-398, `resolve_input` :459-470 with :179-302, `grant_input` :401-456, `_append` :134-176)
- Test: `agent/test/gateStore.pg.test.ts`

**Interfaces:**
- Produces:
  - `openInput(sql, entry: InputEntry, runId: string): Promise<void>`;
  - `resolveInput(sql, args: {requestId; response; responder; outcome}): Promise<'resolved' | 'gone'>`, which also sends `NOTIFY scadbuddy_input, <requestId>`;
  - `grantInput(sql, args): Promise<{grant: string} | null>`.
- `InputEntry` is the existing type in `agent/src/gate/projection.ts`.

- [ ] **Step 1: Write the tests.** Port `agent-durable/tests/test_gate_store.py` case by case:
  - an open writes `ai_pending_input`, `approval.required`/`question.asked` and `input.requested`, and sets status `waiting_*`;
  - a re-open for another run cancels `reset`;
  - a resolve deletes the row, inserts `ai_input_responses`, writes the audit row for approvals and sets status `running`;
  - a second resolve is `gone`;
  - `NOTIFY` is received.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the SQL verbatim from `store.py`, and cite the lines in a header comment.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): the gate's projection writes in TypeScript`.

### Task 4.2: The workflow-side gate

**Files:**
- Create: `agent/src/temporal/session/gate.ts` (workflow code: entries, timers, `respond` validation through `gate/validate.ts`, `cancel_input`), `agent/src/temporal/session/names.ts`
- Test: `agent/test/sessionGate.temporal.test.ts`

**Interfaces:**
- Produces:
  - `names.ts`: `SESSION_WORKFLOW = 'AgentSession'`, `SESSION_QUEUE = 'agent-sessions'`, `SESSION_TURN_ACTIVITY = 'session_turn'`, `OPEN_INPUT_UPDATE = 'open_input'`, `OWNER_CHANGED_SIGNAL = 'owner_changed'`.
  - `class SessionGate`, used inside a workflow:
    - `open(entry: InputEntry): Promise<string>`;
    - `resolution(requestId: string): Promise<Resolution>`;
    - `cancelAll(reason: string): Promise<number>`;
    - `pending(): InputEntry[]`.
  - `type Resolution = { kind: 'approval'; outcome: 'approved' | 'denied' | 'expired' | 'cancelled' } | { kind: 'answer'; outcome: 'answered' | 'cancelled' | 'timed_out'; response?: unknown }`.
  - The timer semantics are `session/workflow.py:509-747`'s:
    - an approval expires at `approval_expiry_seconds`;
    - `ask_user` is cancelled at `question_expiry_seconds`;
    - any other kind times out at the entry's own timeout.

- [ ] **Step 1: Write the tests.** Run a test workflow that wraps `SessionGate`, with the dev server's time-skipping environment (`TestWorkflowEnvironment.createTimeSkipping`):
  - an approval `respond`ed `approve` → `approved`;
  - the expiry passes → `expired`;
  - a `respond` with the wrong role → `GateRefused:<code>`, using the vectors in `agent/test/fixtures/pending-input-vectors.json`;
  - `cancelAll` → `cancelled`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Activities `gate_open_input` and `gate_resolve_input` wrap Task 4.1 on `agent-tools`, with retry `RESOLVE_ON_TIMER` = 30 s per attempt, unlimited attempts and a 5 min maximum interval (`gate/activities.py:34-39`).
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): the session gate as workflow code`. Open PR 4.

---

## Phase 5: `AgentSession` behind a flag (PR 5, `feat(agent): AgentSession runs the classic turn durably (flag)`)

`ai_settings.session_runtime`: `'unified'` routes new **durable** sessions to `AgentSession`. Unset keeps today's behaviour. Evals run both until Phase 6.

### Task 5.1: The workflow: send, end, continue-as-new

**Files:**
- Create: `agent/src/temporal/session/workflow.ts`
- Modify: `agent/src/temporal/workflows.ts` (export `AgentSession`), `agent/src/temporal/worker.ts` (accept `workerDeploymentOptions`), `agent/src/main.ts` (a third worker on `agent-sessions`, deployment `scadbuddy-agent`, build id from `SCADBUDDY_BUILD_ID` or the image tag, `defaultVersioningBehavior: 'AUTO_UPGRADE'`), `agent/src/sessions/durable.ts` (workflow type and queue by `session_runtime`)
- Create: `agent/test/fixtures/agent_session_histories/` (recorded by the test below with `UPDATE_SESSION_HISTORIES=1`)
- Test: `agent/test/agentSession.temporal.test.ts`

**Interfaces:**
- Consumes: `SessionGate` (4.2).
- Produces:
  - `AgentSession(start: SessionStart)`. `SessionStart` is `session/models.py`'s shape: `{session_id, creator, owner, max_turns, system_append, turn?, interrupted, home?, continuation?}`.
  - The handlers in spec §5.1.
  - `type SessionTurnInput = { sessionId: string; turnId: string; message?: Message; continuation?: Continuation; traceparent?: string }`.
  - `type SessionTurnResult = { status: 'done' | 'released' | 'failed' | 'interrupted' | 'budget_exhausted'; hostQueue: string; parked?: string[]; error?: string }`.
  - `session_settings(): Promise<RuntimeSettings>`, a local activity reading `readRuntimeSettings` (Task 2.5). `local = proxyLocalActivities({ startToCloseTimeout: '10s' })`.
  - `type Continuation = { kind: 'restarted' } | { kind: 'parked'; calls: { requestId: string; toolUseId: string; tool: string; input: unknown; resolution: Resolution }[] }`.

- [ ] **Step 1: Write the tests.** Use a stub `session_turn` activity that returns `done`:
  - `send_message` starts the workflow and a turn;
  - a second `send_message` during the turn is refused `busy`;
  - `end` closes it;
  - after each turn the run id changes (continue-as-new);
  - the history replays from the fixture with `Worker.runReplayHistories`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Write the loop of spec §5.1. Then run `pnpm build`, which bundles the workflow.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): the AgentSession workflow`.

### Task 5.2: Session affinity (`acquire_worker`, `session_turn`, `release_worker`)

Spec §5.2: Go Worker Sessions, built from worker-specific task queues as
[docs.temporal.io/guides/worker-execution-affinity](https://docs.temporal.io/guides/worker-execution-affinity) prescribes.

**Files:**
- Create: `agent/src/temporal/session/turnActivity.ts`, `agent/src/temporal/session/affinity.ts` (the activities `acquire_worker` and `release_worker`), `agent/src/sessions/turnHost.ts`
- Modify: `agent/src/sessions/warm.ts` (`reserve(sessionId): 'reserved' | 'full'`, which evicts the LRU idle slot first; `release(sessionId)`), `agent/src/main.ts` (`acquire_worker` on the shared `agent-tools` worker; `session_turn` and `release_worker` on the host worker only, with `maxConcurrentActivityTaskExecutions = warm_max_per_replica + 2`), `agent/src/temporal/session/workflow.ts` (the recovery loop)
- Test: `agent/test/sessionAffinity.temporal.test.ts`

**Interfaces:**
- Produces:
  - `acquire_worker(input: { sessionId: string }): Promise<{ hostQueue: string; identity: string }>`. It throws `ApplicationFailure.retryable('NoCapacity')` only when every slot is running a turn.
  - `release_worker(input: { sessionId: string }): Promise<void>`.
  - `class TurnHost { constructor(deps: {runner: TurnRunner; warm: WarmSessions; sql; events; credentials; hostQueue: string}); run(input: SessionTurnInput, heartbeat: () => void, cancelled: AbortSignal): Promise<SessionTurnResult> }`.
  - `sessionTurnActivity(host: TurnHost)`, which wraps `run` with `Context.current().heartbeat()` every 5 s.
- The workflow's recovery loop, verbatim:

```ts
const acquire = proxyActivities<Affinity>({ taskQueue: TASK_QUEUE, scheduleToCloseTimeout: '60s',
  retry: { initialInterval: '1s', maximumInterval: '10s', maximumAttempts: 10 } })

async function runTurn(input: SessionTurnInput): Promise<SessionTurnResult> {
  const settings = await local.session_settings()    // RuntimeSettings (Task 2.5), read per turn
  let continuation = input.continuation
  for (let cycle = 0; cycle < 3; cycle++) {
    if (!state.home) state.home = (await acquire.acquire_worker({ sessionId: input.sessionId })).hostQueue
    const onHome = proxyActivities<Turn>({ taskQueue: state.home, scheduleToStartTimeout: '5s',
      heartbeatTimeout: '20s', startToCloseTimeout: `${settings.turn_max_seconds}s`, retry: { maximumAttempts: 1 } })
    try {
      return await onHome.session_turn({ ...input, ...(continuation ? { continuation } : {}) })
    } catch (err) {
      if (!movesWorker(err)) throw err          // BudgetExhausted, CredentialRefused, SessionGone
      state.home = undefined                     // the guide: never retry on the failed worker's queue
      continuation = mergeRestarted(continuation) // keeps a parked continuation's calls
    }
  }
  return { status: 'failed', hostQueue: '', error: 'the turn could not be placed on a worker' }
}
```

  `movesWorker(err)` is true for a `TimeoutFailure` of any type, and for an `ApplicationFailure` of type `ShuttingDown` or `NoCapacity`.

- [ ] **Step 1: Write the tests.** Use two host workers (A, B), the shared worker and the fake Anthropic:
  - `the first turn acquires and later turns stay on home`: `acquire_worker` runs once for two turns, and the second is warm (span `warm.outcome=hit`).
  - `a dead home is re-acquired after schedule-to-start`: stop A between turns. The next turn ends `done` on B within 5 s plus its run, and `acquire_worker` ran a second time.
  - `a worker killed mid-turn resumes on another`: kill A's Claude Code during a turn. The heartbeat times out at about 20 s, the turn resumes on B with the restart message, and the tool that was in flight ran once.
  - `release_worker frees the slot on end`: after `end`, A's `warm.size()` is 0.
  - `a full replica evicts its LRU idle slot, never a running one`.
  - `events match a classic turn`: the `type` lists in `ai_session_events` match `sessions.e2e.test.ts`'s for the same script.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** `TurnHost.run` does the following:
  1. Claim the turn (`durable_turn_id = turnId`).
  2. Assemble with `TurnRunner`.
  3. `warm.acquire`.
  4. Push the message.
  5. Write events with `sdkEvents`.
  6. `warm.settle`.
  7. Write cost and turns as `manager.ts:1944-2052` does.
  8. Return `done`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): session affinity in Go Sessions' shape on worker-specific queues`.

### Task 5.3: Parks open through the workflow and are held

**Files:**
- Modify: `agent/src/sessions/turnHost.ts` (slot gates call Update `open_input`, then wait on `ai_input_responses` by `LISTEN scadbuddy_input` plus a 1 s poll), `agent/src/temporal/session/workflow.ts` (`open_input` handler → `SessionGate.open`)
- Test: `agent/test/sessionPark.temporal.test.ts`

- [ ] **Step 1: Write the tests:**
  - an outward call parks: `GET /api/v1/ai/pending-input` lists it with id `durable:<s>:<run>:<toolu>`; `respond approve` → the call runs and the turn ends `done`; the process was never closed;
  - `AskUserQuestion` parks as `answer`, and the answer reaches the model as the tool result;
  - two outward calls in one assistant message both park, and both run once approved (parallel calls, spec §4 table).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): a session turn parks through its workflow and holds warm`.

### Task 5.4: Release and continuation

**Files:**
- Modify: `turnHost.ts` (hold timer `park_hold_seconds` → close and evict, return `released`; on a `parked` continuation, run approved calls with `runToolWithOutcome(..., gate: 'workflow')` and `callKey = requestId`, then push the outcome message), `workflow.ts` (wait for every released entry, then schedule with the continuation)
- Modify: `agent/src/tools/command.ts:148`, `agent/src/tools/customizer.ts:209`, `agent/src/tools/flows.ts:307` (`Idempotency-Key: ctx.callKey ?? randomUUID()`), `agent/src/tools/registry.ts` (`ToolContext.callKey?: string`, set from `toolUseID` as `call:<session>:<toolUseID>` by the in-process server)
- Test: `agent/test/sessionRelease.temporal.test.ts`, `agent/test/callKey.test.ts`

- [ ] **Step 1: Write the tests**, with `park_hold_seconds=1`:
  - `a released approval resumes and runs once`: approve after the release, then exactly one backend command with key `durable:<…>`, and the model's next request contains the outcome message;
  - `an approved released call runs once across an activity retry`: make the continuing activity's worker die after the tool ran, then the next recovery cycle's command is answered from the backend's operation record (msw/fake backend asserts 1 effect);
  - `a denied released call is reported, not run`;
  - `the closed process wrote a synthetic result`: assert the transcript holds an `is_error` tool_result for the parked `tool_use_id`. This verifies spec §12's unverified item.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): a long park releases the process; the next turn runs the decided call once`.

### Task 5.5: Restart mid-turn

**Files:**
- Modify: `turnHost.ts` (a `restarted` continuation pushes the restart message first; `mergeRestarted` keeps the calls of a `parked` continuation and runs them before the restart message)
- Test: `agent/test/sessionRestart.temporal.test.ts`

- [ ] **Step 1: Write the test.** A worker running a turn whose fake model hangs is stopped hard (`worker.shutdown()` plus killing the process through the spawn seam). The workflow re-acquires (Task 5.2) after the heartbeat timeout, and a second worker takes the turn. The tool that was in flight ran once, the model's request contains the restart message, and the turn ends `done`.
- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): a turn cut by a restart resumes on another replica`.

### Task 5.6: Interrupt, handoff, done, forget

**Files:**
- Modify: `turnHost.ts` (watch `interrupt_requested` on the 1 s tick and on `NOTIFY scadbuddy_interrupt`), `agent/src/sessions/manager.ts` (`interrupt` sets the flag and NOTIFYs, then signals; `handoff` signals `owner_changed`; done and forget evict), `workflow.ts` (`interrupt` → `cancelAll('interrupted')` + cancel the activity scope; `owner_changed`)
- Test: `agent/test/sessionInterrupt.temporal.test.ts`

- [ ] **Step 1: Write the tests:**
  - `interrupt while running ends within 2 s`;
  - `interrupt while held cancels the entry and keeps the process` (`warm.size()` is still 1, no new model call);
  - `handoff cancels parked entries and recycles`;
  - `forget evicts on every replica`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): interrupt, handoff and forget for AgentSession`. Open PR 5.

---

## Phase 6: One runtime (PR 6, `feat(agent)!: every session is an AgentSession`)

### Task 6.1: Route every new session to `AgentSession`; retire the mode switch

**Files:**
- Modify: `agent/src/sessions/manager.ts` (`startMode` and `sessionModeOf` removed; `send` always goes through `DurableTurns` with `AgentSession`; the lease, renew loop and `reapExpired` removed; `resumeApproved` removed), `agent/src/main.ts` (no reaper; sessions refuse `unavailable` without Temporal), `agent/src/routes/chat.ts:317`, `agent/src/routes/sessions.ts:108`, `agent/src/routes/pendingInput.ts` (drops its `session_mode` branch), `agent/src/sessions/clientProtocol.ts` (drops its `sessionModeOf` branch), `agent/src/tools/sessions.ts` (`mode` accepted and ignored), `agent/src/routes/sessionMode.ts` (deleted; its settings key ignored), `agent/src/temporal/flowSessions.ts:50` (no mode check)
- Modify: `frontend/src/components/assistant/*` (remove the Advanced mode select and the Durable badge; the `scadbuddy.assistant.mode` localStorage key is ignored), `frontend/src/mocks/features/*` (sessions)
- Test: `agent/test/sessions.e2e.test.ts` (every case now runs through `AgentSession` on the dev server), `frontend` vitest for the composer

With the lease retired, the claim is `durable_turn_id` alone (as it already is for a `DurableSession`, `plan:627`); `turn_id` and `lease_until` are never written again and stay `NULL`, so `ai_sessions`' `CHECK ((turn_id IS NULL) = (lease_until IS NULL))` (`20260928T0107Z_sessions.sql:36`) holds trivially until Phase 10 drops `lease_until` (Task 10.1) and the now-dead `turn_id` with it.

- [ ] **Step 1: Write the tests.** Rewrite `sessions.e2e.test.ts`'s setup to start a dev server and the three workers, and keep every assertion. Add:
  - `a session created with mode classic runs as AgentSession`;
  - `fork works for every session` (removing `manager.ts:2300-2305`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `cd frontend && pnpm lint && pnpm typecheck && pnpm test`.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent)!: one session runtime; the mode switch is retired`.

### Task 6.2: Evals through the one runtime

**Files:**
- Modify: `agent/evals/durable.ts` (drive `AgentSession`, not the Python worker), `agent/test/evals.durable.e2e.test.ts` (now the only replay), `agent/test/evals.test.ts` (removed, since its classic replay is now the same path), `docs/ai/evals.md` (`SCADBUDDY_EVAL_MODE` removed)
- [ ] **Step 1:** Run `pnpm test` with the scenario set and expect FAIL (the Python worker is no longer started).
- [ ] **Step 2:** Implement it, and confirm every scenario passes.
- [ ] **Step 3: Commit** with `test(agent): evals replay through AgentSession only`.

### Task 6.3: The parity guard

**Files:**
- Create: `.github/scripts/lint-one-runtime.sh`, `.github/scripts/lint-one-runtime.test.sh`
- Modify: `.github/workflows/ci.yml` (the `lint` job runs it)
- [ ] **Step 1: Write `lint-one-runtime.test.sh`.** It creates fixture trees that each must fail:
  - a `mode === 'durable'` in `agent/src/harness/x.ts`;
  - a second `query(` call site outside `agent/src/harness/run.ts` and `testConnection.ts`.

  The current tree must pass. (`agent-durable/` still exists at this point — PR 7 migrates its sessions and PR 8 removes it — so its own check is added in Task 8.1, once checking for the directory's absence is actually meaningful.)
- [ ] **Step 2: Run it.** `bash .github/scripts/lint-one-runtime.test.sh`. Expected: FAIL (no script).
- [ ] **Step 3: Implement.**

```bash
#!/usr/bin/env bash
# One agent runtime (spec 2026-10-13 §3 goal 7): fail if a second turn path appears.
set -euo pipefail
root="${1:-.}"
fail=0
if grep -rnE "mode ?={2,3} ?'(durable|classic)'|sessionModeOf|session_mode" "$root/agent/src" --include='*.ts' | grep -v '/db/migrations/'; then
  echo "a session mode branch is back: every session is an AgentSession" >&2; fail=1
fi
mapfile -t sites < <(grep -rlnE "\bquery\(\{" "$root/agent/src" --include='*.ts' || true)
for f in "${sites[@]}"; do
  case "$f" in */harness/run.ts|*/harness/testConnection.ts) ;; *) echo "query() outside the harness: $f" >&2; fail=1 ;; esac
done
exit "$fail"
```

- [ ] **Step 4: Run it.** Both scripts pass, and `shellcheck` is clean.
- [ ] **Step 5: Commit** with `ci: fail if a second agent turn path appears`. Open PR 6.

---

## Phase 7: Migrate open `DurableSession`s (PR 7, `feat(agent): migrate durable sessions to AgentSession`)

### Task 7.1: Verify the old transcript resumes under the TypeScript CLI (gate for the phase)

**Files:**
- Create: `agent/test/fixtures/durable_conversation/` (one conversation exported from a recorded `DurableSession` history in `agent-durable/tests/`, or from a dev run: `temporal workflow query --type __claude_agent_conversation`)
- Test: `agent/test/durableImport.sdk.test.ts`

- [ ] **Step 1: Write the test.** Load the entries into an `InMemorySessionStore`, `query({resume})` against the fake, and assert that the request carries the old turns.
- [ ] **Step 2: Run it.** If it FAILS because the format differs, **stop the phase and report to the user**, with the diff of an entry against a TypeScript-written one. Otherwise go on.
- [ ] **Step 3: Commit** the fixture and test as `test(agent): a DurableSession conversation resumes under the TS CLI`.

### Task 7.2: Lazy migration on send; the backfill script

**Files:**
- Create: `agent/src/sessions/migrateDurable.ts`, `agent/src/migrate-durable-sessions.ts` (built to `dist/`)
- Modify: `agent/src/sessions/durable.ts` (`send`: `describe('session-<id>')`; type `DurableSession` and open → `migrateDurable` first)
- Test: `agent/test/migrateDurable.temporal.test.ts`

**Interfaces:**
- Produces: `migrateDurable(deps: {client; store: PostgresSessionStore; sql}, sessionId: string): Promise<'migrated' | 'not-durable'>`.
- The steps are spec §10.2's: `cancel_input(migrated)`; query the conversation pages into `store.append`; Signal `end`; await the result; then the caller starts `AgentSession`.

- [ ] **Step 1: Write the tests.**
  - `a send migrates an open DurableSession first`: a fake `DurableSession` registered in the test worker answers `__claude_agent_conversation` from the fixture. It has one parked entry, which ends `cancelled` with reason `migrated`. The old workflow is closed, and the new turn's model request contains the old turns.
  - `the backfill migrates every open DurableSession and is idempotent`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** with `feat(agent): open DurableSessions migrate on next send or by backfill`. Open PR 7, and put the operator step in the body: run `node dist/migrate-durable-sessions.js` after deploy, and confirm it reports 0 open.

---

## Phase 8: Retire `agent-durable` (PR 8, `chore!: retire agent-durable`)

Merge this only after the PR 7 backfill reported 0 open `DurableSession`s in production. Link the run output in the PR.

### Task 8.1: Remove the Python service and its CI

**Files:**
- Delete: `agent-durable/`, `.github/scripts/agent-durable-pin.sh`, `.github/workflows/agent-durable-pin.yml`
- Modify: `.github/scripts/lint-one-runtime.sh` and `lint-one-runtime.test.sh` (add the `agent-durable/` directory check Task 6.3 deferred: a fixture tree containing the directory must fail), `Dockerfile` (remove the `agent-durable` stage), `.github/workflows/ci.yml` and `build-image.yml` (remove the `agent-durable` jobs and the `CI Summary` needs), `.hadolint.yaml` if it names the stage, `CLAUDE.md` (the Durable sessions command block and layout bullet), `README.md` ("Deploying")
- [ ] **Step 1:** Add `if [ -d "$root/agent-durable" ]; then echo "agent-durable/ is retired" >&2; fail=1; fi` to `lint-one-runtime.sh` and the matching fixture to its test. Run `bash .github/scripts/lint-one-runtime.test.sh`. Expected: PASS for the fixtures, FAIL for the real tree (`agent-durable/` still exists).
- [ ] **Step 2:** Delete `agent-durable/` and the rest of the Files list above.
- [ ] **Step 3:** Run `bash .github/scripts/lint-one-runtime.sh` and `lint-one-runtime.test.sh`. Both now pass. Run `actionlint` and `hadolint Dockerfile`. Expected: clean.
- [ ] **Step 4: Commit** with `chore!: retire agent-durable and the temporalio-claude-agent-sdk pin`.

### Task 8.2: Remove the agent's durable-only code

**Files:**
- Delete: `agent/src/temporal/turnPackages.ts`, `memoryActivities.ts`, `subagentToken.ts`, `sessionClients.ts`, `agent/src/routes/subagentMcp.ts`, `agent/src/sessions/durableSweep.ts`, `gate/sweep.ts`'s describe pass, `gate/classic.ts`, the session path of `ApprovalService.gate`, `agent/src/questions/` (folded into the gate), and their tests
- Modify: `agent/src/temporal/turnTools.ts`, `turnServers.ts` (kept for flows' `turn:` and `turn_tool_tier` only; `turn_tools` listing and `turn_tools_close` removed), `agent/src/main.ts` (workers and sweeps)
- [ ] **Step 1:** Run `pnpm lint && pnpm typecheck && pnpm test && pnpm build`. Expected: PASS, and `test/coverage.test.ts` is still green.
- [ ] **Step 2: Commit** with `refactor(agent): drop the durable-only turn plumbing`. Open PR 8. File the clusters PR that removes the `agent-durable` container from `applications/scadbuddy/scadbuddy.yaml`, and link it.

---

## Phase 9: Background tasks, Visibility, dashboard, docs (PR 9, `feat(agent): background tasks as session workflows; docs`)

### Task 9.1: The codec subject for a session's child workflows

**Files:**
- Modify: `agent/src/temporal/payloadCodec.ts:45-50` (pattern `^(session|flow)-<uuid>(-[a-z0-9-]+)?$`, subject = the `session-<uuid>` prefix)
- Test: `agent/test/payloadCodec.test.ts`
- [ ] **Step 1:** Write the tests: `session-<u>-cron-1` seals under `session-<u>`'s key, and `session-<u>x` passes through.
- [ ] **Step 2:** Implement it and run the tests. Expected: PASS.
- [ ] **Step 3: Commit** with `feat(agent): a session's child workflows seal under its key`.

### Task 9.2: Forget ends a session's child workflows

**Files:**
- Modify: `agent/src/sessions/forget.ts` (list `WorkflowId STARTS_WITH 'session-<u>-'` and terminate and delete each before the session's own)
- Test: `agent/test/forget.temporal.test.ts`
- [ ] **Step 1:** Write the test: a `session-<u>-wakeup-1` workflow is gone after `forgetSubject`. **If the dev server or the deployed Visibility store rejects `STARTS_WITH` on `WorkflowId`, stop and report it.** The alternative is a `ScadbuddySubject` search attribute on each child, already registered (`workflows.ts:50-52`).
- [ ] **Step 2:** Implement it and run the test. Expected: PASS.
- [ ] **Step 3: Commit** with `feat(agent): forgetting a session ends its background workflows`.

### Task 9.3: Dashboard panels

**Files:**
- Modify: `deploy/grafana/scadbuddy-agent.json` (warm hit ratio, spawn p50/p95 by `warm.outcome`, releases per hour, continued turns by kind, all from span metrics of `agent.turn.spawn`, `agent.turn.release` and `agent.turn.continue`)
- [ ] **Step 1:** Run `KUSTOMIZE=… .github/scripts/lint-dashboard.sh`. Expected: PASS (the span names are `*_SPAN` constants since Tasks 2.3 and 5.4).
- [ ] **Step 2: Commit** with `feat(grafana): warm-process and park panels`.

### Task 9.4: Docs

**Files:**
- Modify:
  - `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` (an "Amended by 2026-10-13" note at §6 and §6.6 "Classic sessions");
  - `docs/superpowers/plans/2026-10-11-durable-capability-parity.md` (a "Superseded" header);
  - `docs/ai/operating.md`, `docs/user-guide.md` (no mode picker; restart and park behaviour);
  - `CLAUDE.md` (agent layout: `sessions/turnRunner.ts`, `warm.ts`, `temporal/session/`).
- [ ] **Step 1:** Run `bash .github/scripts/lint-plugin.sh` if plugin docs are touched, and check the links.
- [ ] **Step 2: Commit** with `docs: the unified agent runtime`. Open PR 9.

---

## Phase 10: Drop retired columns and tables (PR 10, `chore(agent): drop the durable-era columns`)

### Task 10.1: Migration

**Files:**
- Create: `agent/src/db/migrations/<ts>_drop_session_mode.sql`:

```sql
-- Spec 2026-10-13 §10.2: one runtime. durable_offset was the Python follower's cursor;
-- mode was the switch; turn_id/lease_until was classic's claim, dead and NULL on every
-- row since Task 6.1 (the CHECK that paired them goes with the columns).
ALTER TABLE ai_sessions DROP COLUMN durable_offset;
ALTER TABLE ai_sessions DROP COLUMN mode;
ALTER TABLE ai_sessions DROP COLUMN turn_id;
ALTER TABLE ai_sessions DROP COLUMN lease_until;
DROP TABLE ai_questions;
DELETE FROM ai_settings WHERE key = 'session_mode';
```

- Modify: every reader the typecheck finds.
- [ ] **Step 1:** Run `pnpm test` against Postgres. Expected: PASS.
- [ ] **Step 2: Commit** with `chore(agent): drop the durable-era columns`. Open PR 10.

---

## Self-review notes

- **Spec coverage, section by section:**
  - §5.1 → 5.1
  - §5.2 → 5.2
  - §5.3 → 2.1–2.4
  - §5.4 → 5.3–5.4
  - §5.5 → 5.5
  - §5.6 → 5.6
  - §6.1 → 5.2
  - §6.2 → 3.1–3.2
  - §6.3 → 3.2, 6.1
  - §6.4–6.5 → 5.6
  - §6.6 → 2.2–2.3
  - §6.7 → 2.2 (credential recycle)
  - §6.8 → 6.1, 8.2
  - §6.9 → 2.3, 9.3
  - §6.10 → 5.2
  - §7 → 5.4
  - §8 → 9.1–9.2
  - §9 → 6.1 (`flowSessions.ts`)
  - §10 → 6.1, 7, 10
  - §11 → 8
  - §12's unverified items → 5.4, 7.1, 9.2
- **Decisions D1–D7** were made by the user on 2026-10-13 (spec §13):
  - D1 → Phase 6;
  - D2/D3/D5 → Task 2.5 settings;
  - D3 limit → Task 2.6;
  - D4 → 5.1;
  - D6 → Phase 11;
  - D7 → Phase 3.

---

## Phase 11: Versioned queues, then `RollingUpdate` (D6) (PR 11 plus a backend PR, then a clusters PR)

The ScadBuddy pod uses `Recreate` because the API polls the backend's `library` queue
unversioned. The manifest says moving to RollingUpdate needs that queue versioned first
(`eh-homelab/clusters` `applications/scadbuddy/scadbuddy.yaml:125-131`). After Phase 8,
the agent polls `agent-tools`, `agent-tools.<hostname>` and `agent-sessions`; only the last
is versioned (Task 5.1).

### Task 11.1: Design check (gate)

- [ ] **Write the check.** Write `docs/superpowers/plans/2026-10-13-unified-agent-runtime-phase-11.md`. It records how Worker Deployment versioning routes activity tasks on a queue that both of these schedule onto:
  - a versioned workflow (`AgentSession`, `AgentOperation` once versioned);
  - an unversioned one (`ProjectWorkflow` on `projects`, which runs tools on `agent-tools`).

  Cite Temporal's Worker Versioning docs. Measure it on the dev server: one old-build and one new-build worker on `agent-tools`, with a new activity name only on the new build. Show where tasks from each kind of caller land.
- [ ] **If the gate fails, stop and ask.** It fails if an unversioned caller's activity can reach an old-build worker that lacks the activity, and no documented setting prevents that.

### Task 11.2: Version the agent's queues

**Files:** `agent/src/temporal/worker.ts`, `agent/src/main.ts` (deployment `scadbuddy-agent` on every agent worker, the same build id), `agent/src/temporal/workflows.ts` (`AgentOperation` versioning behavior per Task 11.1's findings)
- [ ] **Test** (`agent/test/temporal.versioning.temporal.test.ts`): a task with a new activity name never fails as unregistered while an old-build worker is polling.
- [ ] **Implement, then run** the agent suite.
- [ ] **Commit** with `feat(agent): version the agent's task queues`, and open PR 11.

### Task 11.3: Version the backend's `library` queue

**Files:** `backend/scadbuddy/workflows/client.py`, `backend/scadbuddy/workflows/housekeeping.py` (the `library` worker gets a deployment and build id like the render worker's `make_current`)
- [ ] **Test:** the same property for a new sweep activity, in `backend/tests/test_library_versioning.py` (`requires_temporal`).
- [ ] **Implement, then run** the backend suite: `uv run --frozen pytest -n auto`, one suite at a time, against a database of its own.
- [ ] **Commit** with `feat(library): version the library queue`, and open its PR.

### Task 11.4: Clusters: `RollingUpdate`

**Files (eh-homelab/clusters):** `applications/scadbuddy/scadbuddy.yaml`
- [ ] Change `strategy` to `RollingUpdate` with `maxUnavailable: 1, maxSurge: 0`. Pod anti-affinity means no surge node is guaranteed; check that against the nodes. Set `terminationGracePeriodSeconds: 60`, so `stopTurns` (12 s) and the worker's force stop (15 s) finish. Rewrite the comment block at `:125-131` to say why.
- [ ] Open it only after both PRs from Tasks 11.2 and 11.3 have merged and deployed, then drive it to green and merge it by the clusters repo's process.
