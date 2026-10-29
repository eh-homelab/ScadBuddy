# Unified agent runtime Implementation Plan

> **Phases 6–9 rewritten 2026-10-29** against spec §14 (PR #4771), spec §14.7 and the
> requirements R1–R7 of #4773. Phases 1–5 are merged and stand. The user reviews this plan,
> and the decisions Q1–Q11 under "Phases 6–9", before any task in Phases 6–9 starts.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every agent session runs the classic TypeScript turn, as an activity of a TypeScript `AgentSession` workflow. The activity lands on the replica that keeps the session's Claude Code process warm, so there is one tool and permission layer and no second mode. The turns run in a worker Deployment of their own, `scadbuddy-agent-worker`, which scales apart from the API (spec §14). `agent-durable/` stays as the Python worker; only its turn code retires (spec §14.4).

**Architecture:**
- Session affinity follows Go Worker Sessions, built on worker-specific queues (spec §5.2).
- `runTurn`'s assembly becomes `TurnRunner`. A per-replica `WarmSessions` cache keeps one streaming-input `query()` per session, reused only while a fingerprint, the transcript tail and the remaining budget match.
- `AgentSession` (workflow id `session-<id>`, queue `agent-sessions`) schedules `session_turn` on its home queue `agent-sessions.<hostname>` and re-acquires through `agent-tools` (spec §5.2). The hostname is a worker pod's (spec §14.3).
- Parks go through the workflow (Update `open_input`, `ai_pending_input`). The activity holds a park for up to `park_hold_seconds`, then releases it. The next activity runs the approved call once and resumes.
- The transcript stays in `ai_session_entries`, now sealed under the session's data key.
- The API pod's `agent` container serves HTTP only and runs no turn (spec §14.3, Step 6.1).

**Tech Stack:** TypeScript (agent, Claude Agent SDK 0.3.296 / Claude Code 2.1.296, `@temporalio/*` 1.24.0, postgres.js, vitest), Postgres, Temporal. Python only for reading old `DurableSession` histories during migration.

**Spec:** `docs/superpowers/specs/2026-10-13-unified-agent-runtime-design.md` (read it first; §2 holds the measured facts, §13 the user's decisions D1–D7, §14 the 2026-10-26 amendment).

## The epic's phases (#2726)

One PR per step. A clusters step is a PR on `eh-homelab/clusters`.

- [x] 1. Pin the SDK facts as tests; extract `TurnRunner` (#2809)
- [x] 2. Warm processes for every turn (#3558)
- [x] 3. Seal `ai_session_entries` (#3560)
- [x] 4. The gate's workflow half in TypeScript (#3561)
- [x] 5. `AgentSession` and `session_turn` behind a flag
- [ ] 6. Turns on a worker Deployment of their own; one runtime (#4773)
  - 6.1 a worker-only agent process (needs #4806)
  - 6.2 clusters: Deployment `scadbuddy-agent-worker`
  - 6.3 every session is an `AgentSession`; the mode switch retires
  - 6.4 clusters: the API's agent container at its HTTP-only size
  - 6.5 forks are copy-on-write for the transcript (Q8)
  - 6.6 evals through the one runtime; the parity guard
- [ ] 7. Migrate open `DurableSession`s
  - 7.1 gate: the old transcript resumes under the TypeScript CLI
  - 7.2 lazy migration on send, and the backfill (closes #4772 on its zero report)
- [ ] 8. Retire the Python turn; `agent-durable/` stays
  - 8.1 remove `session/` and the plugin pins (closes #2320, #2660, #2745)
  - 8.2 remove the agent's durable-only code
  - 8.3 clusters: `scadbuddy-agent-durable` at zero (Q9)
- [ ] 9. Sessions live outside the pod (#4773 R1–R6); background tasks; dashboard and docs
  - 9.1 a session's own workflows seal under its key
  - 9.2 ScadBuddy's tools run as activities (R1, Q1)
  - 9.3 plugin, package and `http_request` tools run as activities (R1)
  - 9.4 a loopback gateway keeps the key out of Claude Code (R4, Q6)
  - 9.5 a session's directory on the NAS share, plus a clusters PR (R3, Q5; needs #4812)
  - 9.6 Claude Code's own built-ins (R2, Q2–Q4)
  - 9.7 `ai_pending_input` is the gate's record (R5, Q7)
  - 9.8 cron jobs as Temporal Schedules (R6, Q11)
  - 9.9 dashboard and docs
- [ ] 10. Drop the durable-era columns
- [ ] 11. Versioned queues (`RollingUpdate` is already in place, §2.4)

R7 (workspace bytes in the blob store) is #4812 (#4789), not a step of this plan.

## Global Constraints

- **One turn path.** Nothing under `agent/src/harness/` or `agent/src/sessions/turnRunner.ts` branches on a session mode. Step 6.6's `lint-one-runtime.sh` enforces it from Phase 6 on.
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
- **Shared database.** If the Postgres at `SCADBUDDY_TEST_DATABASE_URL` is shared with other runs, run one suite at a time and point it at a database of your own (CLAUDE.md: concurrent runs on one database hear each other's `NOTIFY`).
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

1. **A warm process recycled while a Playwright page is open.** Its upstream MCP session must be deleted, not leaked; this includes a dead pod's sessions after a deploy. Test: Task 2.4.
2. **A warm process after a credential is disabled.** The next turn must spawn with the next credential and never reuse the old env. Test: Task 2.2, `recycles when the credential row changes`.
3. **A turn that ran on another replica while this one stayed warm** (a fallback and back). The stale process must not answer from an old transcript. Test: Task 2.2, `recycles when the transcript tail moved`.
4. **An approval granted after the hold released the process.** The call runs exactly once, with the request id as its Idempotency-Key, even if the continuing activity is retried. Test: Task 5.4, `an approved released call runs once across an activity retry`.
5. **An interrupt while parked and held.** The entry is cancelled, the turn ends `interrupted`, the process survives, and no further model call is made. Test: Task 5.6, `interrupt while held cancels the entry and keeps the process`.
6. **A send to a session whose `DurableSession` is still open.** It is migrated before the turn runs, its parked entries are cancelled `migrated`, and the old workflow closes. Test: Step 7.2, `a send migrates an open DurableSession first`.

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

`ai_settings.unified_sessions`: `true` routes new **durable** sessions to `AgentSession`. Unset keeps today's behaviour. Evals run both until Phase 6.

### Task 5.1: The workflow: send, end, continue-as-new

**Files:**
- Create: `agent/src/temporal/session/workflow.ts`
- Modify: `agent/src/temporal/workflows.ts` (export `AgentSession`), `agent/src/temporal/worker.ts` (accept `workerDeploymentOptions`), `agent/src/main.ts` (a third worker on `agent-sessions`, deployment `scadbuddy-agent`, build id from `SCADBUDDY_BUILD_ID` or the image tag, `defaultVersioningBehavior: 'AUTO_UPGRADE'`), `agent/src/sessions/durable.ts` (workflow type and queue by `unified_sessions`)
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

  As built (#3443, spec §5.2 "Arrival re-reserves"): `home` outlives its reservation (`warm_idle_seconds`), so `TurnHost.run` first calls `warm.reserve(sessionId)`, before it claims the turn or spawns, and throws `ApplicationFailure` `NoCapacity` when every slot there runs a turn (`WarmSessions.acquire` alone would spawn past `warm_max_per_replica`: its trim evicts only idle entries). The workflow moves the turn; a refusal from a home kept from an earlier turn adds one placement (`FULL_HOME_PATCH`), once per loop, since only the first cycle's home is kept.

- [ ] **Step 1: Write the tests.** Use two host workers (A, B), the shared worker and the fake Anthropic:
  - `the first turn acquires and later turns stay on home`: `acquire_worker` runs once for two turns, and the second is warm (span `warm.outcome=hit`).
  - `a dead home is re-acquired after schedule-to-start`: stop A between turns. The next turn ends `done` on B within 5 s plus its run, and `acquire_worker` ran a second time.
  - `a worker killed mid-turn resumes on another`: kill A's Claude Code during a turn. The heartbeat times out at about 20 s, the turn resumes on B with the restart message, and the tool that was in flight ran once.
  - `release_worker frees the slot on end`: after `end`, A's `warm.size()` is 0.
  - `a full replica evicts its LRU idle slot, never a running one`.
  - `a kept home full of running turns as the next turn arrives refuses NoCapacity, and the turn moves`: A (one slot) runs another session's held turn; the kept session's next turn is refused on A as it arrives, nothing is spawned there past the cap, and B runs it with the message itself (`sessionAffinity.temporal.test.ts`). The workflow side, with stub activities (`agentSession.temporal.test.ts`): a kept home's refusal costs no placement (four placements, then `failed`), a home acquired for the turn costs one (three), and `full_home.json` replays the patch. The cap itself: `warmReserve.test.ts`.
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

## Phases 6–9 (rewritten 2026-10-29 against spec §14 and #4773 R1–R7)

The text these phases replace (the 2026-10-13 Phases 6–9, which put every turn in the API pod
and deleted `agent-durable/`) is in git history at `4c130e08b`. What changed, and why, is spec
§14 (PR #4771) and spec §14.7 (this rewrite).

**Built on, not redone.** Three open PRs by session `92109e29` carry part of this work. Each
step below that needs one says so under **Depends on**.

| PR | Issue | What it gives this plan |
|---|---|---|
| #4806 (draft) | #4780 | A `browser_*` call reaches the tab's pod through Temporal: a `BridgeCall` workflow whose one activity, `bridge.tab`, runs on the queue of the pod that holds the tab (`ai_tab_owners`). A turn on a worker pod can then drive a tab connected to an API pod. |
| #4868 (issue, after #4806 ships for one release) | #4868 | Drops `PgTabRelay` and `ai_bridge_messages`. Nothing here waits for it. |
| #4812 (draft) | #4789 | Workspace file bytes in the blob store, sealed per session, with per-file keys wrapped under each session's key, so a fork copies no bytes. R7 of #4773 is this PR. |

**Where the four open issues close.**

| Issue | Closed by | Pinned by |
|---|---|---|
| #2320 (capability parity) | Step 8.1, which merges only after the Step 7.2 backfill reports zero open `DurableSession`s | Step 6.3's parity tests: a session with a remote plugin, a package, `http_request`, the browser, Hindsight and subagents gets each in its turn |
| #2660 (subagents get the turn's plugin, `http_request` and browser tools) | Step 8.1 | Step 6.3's `a subagent gets the turn's plugin, http_request and browser tools, and its outward call parks` |
| #2745 (tiers and manifest from the agent) | Step 8.1, which removes `SCADBUDDY_DURABLE_TOOLS_JSON`, its manifest load in `agent-durable/.../worker.py` and the image's `tools.json` copy | Step 8.1's `agent-durable` start test without the variable |
| #4772 (a durable session's conversation is only in its workflow) | Step 7.2, closed by hand with the backfill's zero report linked (the PR says `Refs #4772`, because the fix is complete only once the backfill has run) | Step 6.3 stops new `DurableSession`s; Step 7.2's migration tests |

### Decisions the user must make

Each has a recommendation. A step that depends on one says so; nothing that depends on an
open decision starts before it is made.

- **Q1. How a ScadBuddy tool call reaches its activity (R1).** R1 says the turn sends each
  call to its `AgentSession` as an Update, and the workflow schedules the activity. Temporal
  allows "a maximum of 10 in-flight Updates and 2000 total Updates in History" per Workflow
  Execution ([docs.temporal.io/cloud/limits](https://docs.temporal.io/cloud/limits), "Per
  Workflow Execution Update limits"; the self-hosted server has the same defaults as dynamic
  config, which Step 9.2 measures on the deployed server first). A turn runs inside one
  workflow run (it cannot continue-as-new while `session_turn` runs), and its parks are
  `open_input` Updates too. So a turn with more than 10 parallel calls (subagents in parallel
  reach that) would be refused, and a long turn could exhaust the 2000. The history also
  carries every result (51,200 events or 50 MB, [docs.temporal.io/workflow-execution/limits](https://docs.temporal.io/workflow-execution/limits)).
  - **Recommended:** one short workflow per call, `ToolCall`, id
    `session-<uuid>-call-<tool_use_id>` (so a re-send after a lost answer finds the same
    execution), on `agent-tools`, sealed under the session's key (the §8 subject pattern,
    Step 9.1), whose one activity runs the tool on the right queue. This is #4806's
    `BridgeCall` shape. The session's own history stays small, and each call is an
    execution of its own, so neither Update limit applies to it.
  - Alternative: R1 as written, with a turn refusing its 11th parallel call and ending at a
    call budget below 2000.
- **Q2. WebFetch and WebSearch (R2).** R2 asks for Claude Code's own built-ins. Claude Code's
  own WebFetch skips the egress check: measured in #2684, a call to `127.0.0.1:9` tried to
  connect (`agent/src/builtins/web.ts` header). **Recommended:** keep ScadBuddy's egress-checked
  `WebFetch` and `WebSearch` (`builtins/web.ts`, `nested.ts`) as the one exception, until a
  test shows Claude Code's WebFetch goes through an egress proxy the worker controls
  (`HTTPS_PROXY`: "Claude Code respects standard proxy environment variables",
  [code.claude.com/docs/en/network-config](https://code.claude.com/docs/en/network-config);
  **unverified** that WebFetch's own fetch takes it). The same page says Claude Code's WebFetch
  also calls `api.anthropic.com` for a domain safety check unless `skipWebFetchPreflight` is
  set, which a gateway credential (R4) would have to allow for.
- **Q3. Which native built-ins every session gets, at what default access (R2).**
  **Recommended:** a new access source `builtin:claude-code` in #2171's model (spec
  2026-10-18 §4.2) with `Read`, `Glob`, `Grep` and the task list at `read`; `Write`, `Edit`
  and `NotebookEdit` at `write`, confined to the session directory by the `PreToolUse` seam;
  `Bash` and `PowerShell` at `outward` (each call waits for a person). An `allow_refused`
  package's `builtinTools` then only switch items on; they no longer decide what exists.
- **Q4. Bash isolation (R2).** Native `Bash` runs in the worker pod. Its environment is only
  what the SDK's `env` gives it (`run.ts`: `env` "REPLACES the subprocess environment
  entirely"), but its file system holds the KEK mount (`/etc/scadbuddy/kek`) and, with R3,
  every session's directory. Claude Code's own sandbox
  ([code.claude.com/docs/en/sandboxing](https://code.claude.com/docs/en/sandboxing)) uses
  bubblewrap on Linux. Its default "still allows reading credential files", so the KEK path
  needs a deny rule. And "in an unprivileged container, [bubblewrap] can't mount a fresh
  `/proc` filesystem" without `enableWeakerNestedSandbox`, of which the page says: "Only use
  this setting when the outer container already provides the isolation boundary you need." **Recommended:** before
  Step 9.6 turns `Bash` on, measure that sandbox in the worker image, with reads denied
  outside the session directory. If it cannot run in the worker pod, `Bash` and `PowerShell`
  stay `off` by default, and a sandbox pod (#779) is the follow-up.
- **Q5. The session directory (R3) against the blob store (R7).** **Recommended:**
  `SessionFiles` (blob store, #4812) stays the record; the NFS directory
  `Scadbuddy/sessions/<id>/` is a working copy, filled from `SessionFiles` when a placement
  starts and written back (by sha256 against the row) at every turn end, release and
  shutdown. A panel upload or a `workspace_*` write goes to `SessionFiles` and marks the
  directory stale; the next placement refreshes it. So a pod that dies mid-turn loses at
  most that turn's unsynced writes, which the restart message already tells the model to
  check.
- **Q6. The local gateway (R4).** **Recommended:** a loopback proxy in the worker process
  (`agent/src/gateway/`). Each spawn gets `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>/t/<token>`
  and `ANTHROPIC_AUTH_TOKEN=<token>`, a random token per process that the proxy maps to the
  turn's credential and that dies with the process. The proxy adds the real key and meters
  each request's spend against the session, so a request made by a `Bash` command with the
  token is still counted in the budget. The credential never enters Claude Code's
  environment.
- **Q7. `ai_pending_input` as the record (R5).** **Recommended:** yes, keeping the gate's
  public shape (spec §3 non-goals). `open_input` writes the row first; `respond` writes the
  decision row in its transaction and then sends the `respond` Update as a wake-up; the
  workflow keeps only request ids and timers, and on each wake or timer reads the rows
  through an activity. A lost Update is recovered by the timer's read.
- **Q8. Fork (#4212 against spec §6.3).** Spec §6.3 copies the transcript into the child,
  re-sealed. The user's rule is copy-on-write: a fork points at its parent and copies on
  write, never duplicating transcripts or events. **Recommended:** Step 6.5: the child's
  `PostgresSessionStore.load` reads the parent's entries up to the cut, under the parent's
  key, then its own; forgetting a parent that has live forks first copies the referenced
  range into each child (the parent's deletion is the write). The events and workspace parts
  stay #4212's and #4812's.
- **Q9. `scadbuddy-agent-durable` after Phase 8.** With `session/` gone the Python worker
  serves nothing. **Recommended:** keep `agent-durable/` and its CI job (its reusable parts
  stay tested, spec §14.4), and scale the clusters Deployment to zero in Step 8.3, until a
  Python workload exists. The vector files stay while `codec.py`, `secrets.py` and `gate/`
  read them.
- **Q10. #2171 delivery step 6 (PR #4201 spec §4.5).** Its "agent + agent-durable" half
  changes the loopback `routes/subagentMcp.ts`, which Step 8.2 deletes. **Recommended:** drop
  that half; #2660 closes through this epic (Step 8.1), and §4.5's "subagents park like the
  main agent" already holds for an in-process subagent, whose calls pass `canUseTool`.
- **Q11. Existing cron tasks (R6).** **Recommended:** a start-time pass converts every active
  cron row's `BackgroundTask` workflow into a Schedule (create the Schedule by the task's id,
  then terminate the old workflow; idempotent), rather than waiting up to
  `CRON_EXPIRY_MS` (7 days, `background/schedule.ts`) for them to lapse.

---

## Phase 6: Turns on a worker Deployment of their own; one runtime

### Step 6.1: A worker-only agent process (PR, `feat(agent): a worker-only agent process`)

Spec §14.3: same image, two entrypoints. #4773's audit (§14.5) is answered in this PR.

**Files:**
- Modify: `agent/src/config.ts`. Add `SCADBUDDY_AGENT_ROLE` to `ENV_VARS`: `all` (the default:
  dev, tests, today's pod), `api` or `worker`. Any other value exits 1, naming it.
- Create: `agent/src/runtime/workers.ts` (the worker wiring moved out of `main.ts`:
  `temporalWorker` on `agent-tools`, `sessionWorker` on `agent-sessions`, `startHostWorkers`
  for `agent-tools.<hostname>` and `agent-sessions.<hostname>`, `TurnHost`, `WarmSessions`,
  the warm-evict and interrupt listeners, the plugin forwarder and `turnClients` the turns
  use, memory retains), `agent/src/runtime/http.ts` (the app, the chat and bridge sockets,
  `/mcp`, the routes and the sweeps only HTTP needs), `agent/src/runtime/health.ts` (the
  worker's `/healthz`, a body like the agent's, and `/readyz`, 200 once its three queues have
  pollers, as the print worker's `/readyz` does).
- Modify: `agent/src/main.ts` (the shared services, then the role switch, then `stop()` per
  role).
  - `api` builds the HTTP half and a Temporal client. It also runs the one host worker #4806
    needs: `bridge.tab` on its own `agent-tools.<identity>`, since the tab's socket is in this
    process. It polls neither `agent-sessions`, nor the shared `agent-tools`, nor a session
    host queue, so it runs no `AgentSession` turn.
  - Classic sessions still run in-process wherever `SessionManager.send` runs, until Step 6.3.
  - Until Step 8.1, `api`'s host worker also registers the pinned `turn:` activities
    (`turnToolActivities`, `default`). A `DurableSession` turn pins each call to the
    `agent-tools.<hostname>` of the replica that listed its servers (`turnTools.ts`
    `pinned`). One that listed them on an API pod before the role switch would otherwise
    schedule an activity that nobody there serves. New listings run on worker pods, so the
    API pods' share falls to nothing as those turns end.
  - `worker` builds the worker half and serves only `runtime/health.ts`.
- Modify: `agent/src/sessions/durable.ts` (`unready`: an `api` process has no session host
  queue of its own; a unified session is ready when `agent-sessions` and `agent-tools` have
  fresh pollers and `liveHostQueues` names at least one session host).
- Modify: `docs/superpowers/specs/2026-10-13-unified-agent-runtime-design.md` §14.5 items 2
  and 3 only: the audit's answers, each with the test that shows it. (#4806 already edits
  item 1.)
- Modify: `docs/ai/operating.md` (the roles), `README.md` ("Deploying"), and the agent
  bullet in `CLAUDE.md`.

**The audit (spec §14.5), as the tests below check it:**
- the browser bridge: #4806's `BridgeCall`;
- tab-reconnect waits: they resolve through `ai_questions` `reconnected`
  (`questions/service.ts`), written by the pod whose hub sees the tab (#4773, comment of
  2026-10-27). For an `AgentSession` park (`tab_disconnected`, spec §5.4), the hub's pod sends
  `respond` to the session's workflow;
- interrupt: the row flag plus `NOTIFY` (spec §5.6) crosses pods;
- forget: `NOTIFY scadbuddy_warm_evict` crosses pods;
- the plugin forwarder's loopback is per process, so the worker builds its own;
- `/internal/durable/subagent-mcp` and `GET /internal/flows/tools` stay on `api`: the
  backend and `agent-durable` reach them as they do now.

**Tests:**
- `agent/test/agentRole.test.ts`: which workers and routes each role starts. `api` starts no
  `agent-sessions`, shared `agent-tools` or session host worker; `worker` answers 404 on
  `/api/v1/ai/*`.
- `agent/test/agentRole.temporal.test.ts`, with one `api` and one `worker` process on one dev
  server and Postgres:
  - a unified session sent through `api` runs its turn on `worker`;
  - a `browser_*` call from that turn reaches a tab connected to `api`;
  - an interrupt from `api` ends the turn within 2 s;
  - a forget from `api` closes `worker`'s warm process;
  - a tab that drops and comes back resolves the turn's `tab_disconnected` park.

**Rollout:** the default is `all`, so nothing changes in prod until Step 6.2.

**Depends on:** #4806 (the `bridge.tab` host worker and the cross-pod browser test).
**Closes:** none. It is #4773's code half.

### Step 6.2: Clusters: the `scadbuddy-agent-worker` Deployment (eh-homelab/clusters PR, `feat(scadbuddy): the agent's Temporal workers in a Deployment of their own`)

**Files (eh-homelab/clusters):**
- Create: `applications/scadbuddy/scadbuddy-agent-worker.yaml`. It is a Deployment with
  container `agent-worker`, the `scadbuddy-agent` image the API pod's `agent` container runs
  (hand-pinned, #738), and `SCADBUDDY_AGENT_ROLE=worker`. It carries:
  - the agent container's database, Temporal, `SCADBUDDY_PAYLOAD_DIR` and backend
    variables;
  - the `agent-kek` Secret mount;
  - `nas-share` at subPath `Scadbuddy/payloads` (never the share root, clusters#1877);
  - the `agent-state` and `/tmp` emptyDirs (Chromium for the Playwright plugin);
  - D3's memory request 2Gi and limit 8Gi, which move here from the API's `agent` container;
  - `replicas: 3`, `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0` (spec §5.5);
  - `terminationGracePeriodSeconds: 30` or more, covering the turns' 17 s;
  - a topology spread across nodes, and `/readyz` as readiness.
- Create: `applications/scadbuddy/pdb-agent-worker.yaml` and
  `networkpolicy-agent-worker.yaml`. Egress: Postgres, the Temporal frontend, the backend's
  internal URL, the Anthropic endpoint or gateway, the plugin hosts and `playwright-mcp`. No
  ingress except the probes.
- Modify: `kustomization.yaml`; `scadbuddy.yaml` (the API pod's `agent` container gets
  `SCADBUDDY_AGENT_ROLE=api`); `playwright-mcp.yaml`'s policy (allow the worker);
  `networkpolicy-temporal-v2-frontend.yaml` (allow the worker).
- Before the PR: read the nodes' allocatable memory for 3 × 2Gi requests beside the API's (spec §12).

**Rollout:** open it once Step 6.1's image is the pinned one, and merge it by the clusters
repo's process. It is the step that makes turns scale apart from the API (#4773). From
here, `DurableSession`'s tool activities on `agent-tools` run on worker pods too.
**Depends on:** Step 6.1, and #4806 deployed (a worker's turn reaches an API pod's tab).
**Closes:** none. It is #4773's clusters half.

### Step 6.3: Every session is an `AgentSession` (PR, `feat(agent)!: every session is an AgentSession`)

This is the old Task 6.1, now on the worker Deployment. From this release on, no new
`DurableSession` is made, so #4772 stops growing.

**Files:**
- Modify: `agent/src/sessions/manager.ts`:
  - `startMode` and `sessionModeOf` are removed;
  - `send` always goes through `DurableTurns` with `AgentSession`;
  - the lease, the renew loop, `reapExpired` and `resumeApproved` are removed;
  - fork drops its `durable` refusal for `AgentSession` (an open `DurableSession` still
    forks as #2688 made it, until Phase 7).
- Modify: `agent/src/sessions/durable.ts` (the `unified_sessions` flag goes; a session
  whose `session-<id>` is an open `DurableSession` keeps going to it until Step 7.2).
- Modify: `agent/src/main.ts` and `agent/src/runtime/*` (no reaper; without Temporal,
  sessions refuse `unavailable`, spec §10.1), `agent/src/routes/chat.ts`,
  `agent/src/routes/sessions.ts`, `agent/src/routes/pendingInput.ts` (no `session_mode`
  branch), `agent/src/sessions/clientProtocol.ts`, `agent/src/tools/sessions.ts` (`mode`
  accepted and ignored), `agent/src/routes/sessionMode.ts` (deleted; the key is ignored),
  `agent/src/temporal/flowSessions.ts` (no mode check), `agent/src/tools/coverage.ts` (the
  deleted route).
- Modify: `frontend/src/components/assistant/*` (the mode select and the Durable badge go;
  `scadbuddy.assistant.mode` in localStorage is ignored), `frontend/src/mocks/features/*`
  (sessions).

**Tests:**
- `agent/test/sessions.e2e.test.ts`: every case runs through `AgentSession` on a dev server,
  with the `api` and `worker` roles of Step 6.1. Every assertion stays. New cases:
  - `a session created with mode classic runs as AgentSession`;
  - `a classic session's next send resumes its old turns` (the model request carries
    them);
  - `fork works for every AgentSession`.
- `agent/test/parity.temporal.test.ts` (new) pins #2320 and #2660. One `AgentSession` turn,
  with a remote plugin, an approved package, `http_request`, a paired tab and Hindsight on,
  sees each tool, and Hindsight's recall and retain run. Also:
  - `a subagent gets the turn's plugin, http_request and browser tools, and its outward call
    parks`;
  - an `allow_refused` package's named built-ins are all offered, not only `Bash`.
- `frontend` vitest for the composer.

**Rollout:** a breaking change for the UI's mode picker only. Prod's `session_mode` stops
being read. The API pod runs no turn after this release, so Step 6.4 follows.
**Depends on:** Step 6.2 deployed (a turn needs a worker pod).
**Closes:** none outright. It pins #2320 and #2660, which close in Step 8.1 once no session
runs the Python turn. It may also close #2377 (durable sessions have no traces), since
`TurnTrace` wraps every turn: check its acceptance in the PR.

### Step 6.4: Clusters: the API's agent container at its HTTP-only size (eh-homelab/clusters PR, `chore(scadbuddy): size the agent sidecar for HTTP only`)

**Files (eh-homelab/clusters):** `applications/scadbuddy/scadbuddy.yaml`. The `agent`
container's memory comes back from D3's 2Gi/8Gi to what one release of Step 6.3 measures
(p50/p95/p99/max from Grafana, written in the comment as the existing block does). Its
`nas-share` mount stays while `SCADBUDDY_PAYLOAD_DIR` is read there (the payload codec,
`api` role).

**Rollout:** after Step 6.3 has run for one release.
**Depends on:** Step 6.3. **Closes:** none.

### Step 6.5: Forks are copy-on-write for the transcript (PR, `feat(agent): a fork reads its parent's transcript through the cut`)

Decision Q8. Without it, Step 6.3 extends classic's copying fork to every session.

**Files:**
- Create: `agent/src/db/migrations/<ts>_session_fork_cut.sql` (`ai_sessions.fork_parent_id`
  and `fork_cut`, the parent's last entry `uuid` or `id` at the cut; a NEW file).
- Modify: `agent/src/sessions/store.ts` `PostgresSessionStore.load` (the parent's entries
  through the cut, opened under the parent's key, then the child's own),
  `agent/src/sessions/manager.ts` `fork` (it records the cut instead of
  `sdkForkSession`'s copy), `agent/src/sessions/forget.ts` (before a parent's key row goes,
  copy the range each live child references into that child, sealed under the child's key).

**Tests:** `agent/test/forkCow.pg.test.ts`:
- a fork writes no entry rows until its first turn;
- the child's first turn's model request holds the parent's turns through the cut, and
  nothing after it;
- forgetting the parent leaves the child resumable, and its copied rows open only under the
  child's key;
- a fork of a fork reads both cuts.

**Rollout:** new forks only. Forks made before this keep their copies.
**Depends on:** Step 6.3; Q8. #4812 for the workspace's own copy-on-write.
**Closes:** the transcript part of #4212 (it says `Refs #4212`).

### Step 6.6: Evals and the parity guard (PR, `test(agent): evals through the one runtime; fail on a second turn path`)

The old Tasks 6.2 and 6.3, unchanged in substance.

**Files:**
- Modify: `agent/evals/durable.ts` (drives `AgentSession` with the `api` and `worker`
  roles, not the Python worker), `agent/test/evals.durable.e2e.test.ts` (the only replay),
  `agent/test/evals.test.ts` (removed), `docs/ai/evals.md` (`SCADBUDDY_EVAL_MODE` removed).
- Create: `.github/scripts/lint-one-runtime.sh` and `lint-one-runtime.test.sh`, with the
  script and fixtures of the old Task 6.3 (at `4c130e08b`). Until Step 7.2,
  `agent/src/sessions/durable.ts` still routes open `DurableSession`s. Run the guard on that
  tree first; if it trips there, allowlist that one file, with a comment naming Step 7.2,
  and remove the allowlist in Step 7.2. Modify:
  `.github/workflows/ci.yml` (the `lint` job runs both).

**Tests:** every scenario passes through `AgentSession`; the guard's fixture trees fail and
pass as the old Task 6.3 lists; the real tree passes; `shellcheck` is clean.
**Rollout:** CI only. **Depends on:** Step 6.3. **Closes:** none.

---

## Phase 7: Migrate open `DurableSession`s

Unchanged from the 2026-10-13 plan, except where its backfill runs. Spec §10.2 and #4772's
comment of 2026-10-27 hold. Namespace retention applies only to closed executions
([docs.temporal.io](https://docs.temporal.io/temporal-service/temporal-server#retention-period)),
so an open `DurableSession` is not at risk. Nothing may send `end` to one before it is
migrated.

### Step 7.1: The old transcript resumes under the TypeScript CLI (gate) (PR, `test(agent): a DurableSession conversation resumes under the TS CLI`)

**Files:** create `agent/test/fixtures/durable_conversation/` (one conversation from a recorded
`DurableSession` history: `temporal workflow query --type __claude_agent_conversation`) and
`agent/test/durableImport.sdk.test.ts`.
**Test:** load the entries into an `InMemorySessionStore`, `query({resume})` against the fake,
and assert that the request carries the old turns. If the format differs, **stop the phase and
report to the user** with the diff of one entry against a TypeScript-written one. A
converter is then its own step before Step 7.2.
**Rollout:** test only. **Depends on:** nothing. It can run beside Phase 6. **Closes:** none.

### Step 7.2: Lazy migration on send, and the backfill (PR, `feat(agent): migrate DurableSessions to AgentSession`)

**Files:**
- Create: `agent/src/sessions/migrateDurable.ts` and `agent/src/migrate-durable-sessions.ts`
  (built to `dist/`).
- Modify: `agent/src/sessions/durable.ts` (`send`: `describe('session-<id>')`; an open
  `DurableSession` is migrated first).
- Steps (spec §10.2):
  1. `cancel_input(migrated)`.
  2. Page the conversation Query into `PostgresSessionStore.append`, sealed and idempotent
     by `uuid`.
  3. Signal `end` and await the close.
  4. Start `AgentSession`.

**Tests:** `agent/test/migrateDurable.temporal.test.ts`:
- `a send migrates an open DurableSession first`: a fake `DurableSession` answers the Query
  from Step 7.1's fixture. Its parked entry ends `cancelled`, reason `migrated`, the old
  workflow is closed, and the new turn's request carries the old turns;
- `the backfill migrates every open DurableSession and is idempotent`;
- `a migrated session's events stay readable in the panel`.

**Rollout:** the operator step is in the PR body. After the deploy, run
`node dist/migrate-durable-sessions.js` from one `scadbuddy-agent-worker` pod (it needs the
KEK, Postgres and Temporal, all of which that pod has), and confirm that it reports 0 open
(66 on 2026-10-26, spec §14.2). Paste the report on #4772 and close it.
**Depends on:** Steps 6.3 and 7.1.
**Closes:** #4772, by hand on the zero report (the PR says `Refs #4772`).

---

## Phase 8: Retire the Python turn; `agent-durable/` stays

Spec §14.4. Merge Step 8.1 only after Step 7.2's backfill reported 0 open `DurableSession`s,
and link the report in its PR.

### Step 8.1: Remove the Python turn and its pins (PR, `chore(agent-durable)!: retire the Python turn`)

**Files:**
- Delete: `agent-durable/src/scadbuddy_durable/session/`,
  `agent-durable/src/scadbuddy_durable/check_cli_version.py`, `check_manifest.py`, their
  tests, `.github/scripts/agent-durable-pin.sh` and `.github/workflows/agent-durable-pin.yml`.
- Modify: `agent-durable/pyproject.toml` and `uv.lock` (drop `temporalio-claude-agent-sdk`
  and `claude-agent-sdk`); `agent-durable/src/scadbuddy_durable/worker.py` (no manifest load,
  no `SCADBUDDY_DURABLE_TOOLS_JSON`, no `DurableSession`. It starts health only and says why,
  as it does without its variables today).
- Keep: `__main__.py`, `health.py`, `codec.py`, `secrets.py`, `credentials.py`,
  `payload_store.py`, `gate/` and their tests and vectors.
- Decide `background.py` with Q4. It runs a durable session's Monitor source; once Step 9.6
  runs `Bash` in the worker, a Monitor runs there too, and `background.py` loses its caller.
  Until then it stays.
- Modify: `Dockerfile` (the `agent-durable` stage stops copying `tools.json` and checking the
  bundled CLI), `.github/workflows/ci.yml` (the `agent-durable` job drops the pin script),
  `.github/scripts/lint-one-runtime.sh` (fail if `agent-durable/` imports
  `claude_agent_sdk` or names `DurableSession`), and `CLAUDE.md` (the durable sessions block
  and layout bullet).

**Tests:**
- `agent-durable/tests/test_worker.py`: the worker starts without `SCADBUDDY_DURABLE_TOOLS_JSON`
  and registers no workflow;
- the codec, secrets and gate vector tests still pass;
- `lint-one-runtime.test.sh` gains a fixture that fails.

**Rollout:** the clusters change is Step 8.3.
**Depends on:** Step 7.2's zero report.
**Closes:** #2320, #2660 and #2745 (`Fixes` all three). No session runs the Python turn, and
the tests of Step 6.3 pin the parity.

### Step 8.2: Remove the agent's durable-only code (PR, `refactor(agent): drop the durable-only turn plumbing`)

**Files:**
- Delete: `agent/src/temporal/turnPackages.ts`, `memoryActivities.ts`, `subagentToken.ts`,
  `agent/src/routes/subagentMcp.ts`, `agent/src/sessions/durableSweep.ts`,
  `agent/src/tools/answerTools.ts` and the manifest-only projection
  (`agent/src/tools/manifest.ts`, `export-tools.ts`, and the `dist/tools.json` step of
  `pnpm build`), `gate/sweep.ts`'s describe pass, `gate/classic.ts`, the session path of
  `ApprovalService.gate`, `agent/src/questions/` (folded into the gate), and their tests.
- Keep: `temporal/turnTools.ts`, `turnServers.ts` and `sessionClients.ts`. Flows use them,
  and so do sessions once R1 lands (Step 9.3; this reverses spec §11's list for these three,
  as spec §14.7 says).
- Modify: `agent/src/runtime/*` (workers and sweeps) and `agent/src/tools/coverage.ts`.

**Tests:** `pnpm lint && pnpm typecheck && pnpm test && pnpm build`; `test/coverage.test.ts`
stays green.
**Rollout:** with or after Step 8.1. **Depends on:** Step 8.1. **Closes:** none.

### Step 8.3: Clusters: `scadbuddy-agent-durable` at zero (eh-homelab/clusters PR, `chore(scadbuddy): scale agent-durable to zero`)

Decision Q9. **Files (eh-homelab/clusters):** `applications/scadbuddy/scadbuddy-agent-durable.yaml`
(`replicas: 0`, with a comment naming spec §14.4), and `pdb-agent-durable.yaml` (removed,
since a PDB over zero pods only blocks drains). `networkpolicy-agent-durable.yaml` stays for
the day it scales up again.
**Depends on:** Step 8.1 deployed. **Closes:** none.

---

## Phase 9: Sessions live outside the pod (#4773 R1–R6), background tasks, dashboard, docs

Each step keeps one turn path. A workflow change goes behind `patched()`, and
`agent/test/fixtures/agent_session_histories/` replays.

### Step 9.1: A session's own workflows seal under its key (PR, `feat(agent): a session's child workflows seal under its key`)

The old Task 9.1, needed by Steps 9.2 and 9.7.

**Files:** `agent/src/temporal/payloadCodec.ts` `SUBJECT` and `subjectOf`. The pattern
becomes `^(session|flow)-<uuid>(-[A-Za-z0-9_-]+)?$` with subject `session-<uuid>`, beside
#4806's `bridge-<uuid>`. The suffix takes capitals and `_`, because an Anthropic
`tool_use_id` has both (`toolu_01Ab…`), and Step 9.2's `ToolCall` id carries one. Keep the
existing `BACKGROUND_TASK` mapping (`bgtask-<session uuid>-<8 hex>` already seals under
`session-<session>`).
**Tests:** `agent/test/payloadCodec.test.ts`:
- `session-<u>-call-toolu_01AbC_d` seals under `session-<u>`'s key. This test must stay as
  written: a narrower pattern would send every tool call's input and result to history
  unsealed;
- `bgtask-<u>-0a1b2c3d` still seals under `session-<u>`'s key;
- `session-<u>x` passes through unsealed;
- `payload-vectors.json` is regenerated (`UPDATE_PAYLOAD_VECTORS=1`), and
  `agent-durable/tests/test_codec.py` still opens every vector.

**Rollout:** new workflows only; old `bgtask-` histories stay readable (unsealed payloads
pass through). **Depends on:** #4806 (it edits the same pattern; rebase on it).
**Closes:** none.

### Step 9.2: ScadBuddy's own tools run as activities (R1) (PR, `feat(agent): a turn's ScadBuddy tool calls run as activities`)

Decision Q1. The recommended shape is described here; the alternative (an Update on
`AgentSession`) changes only the transport.

**Files:**
- Create: `agent/src/temporal/toolCall.ts`, the `ToolCall` workflow (in the bundle). Its id is
  `session-<uuid>-call-<tool_use_id>`. Its one activity is `tool:<name>`, the existing
  `toolActivities(ALL_TOOLS)`, with `runToolWithOutcome(..., gate: 'workflow')`, which
  re-gates by input hash and runs the call with `Idempotency-Key: call:<session>:<tool_use_id>`
  (spec §7).
- Create: `agent/src/tools/activityProjection.ts`. The in-process SDK MCP server the turn
  gets keeps `tools/projections.ts`'s names, schemas and descriptions, so the model and
  Settings see no change. Its handler starts or attaches to the call's `ToolCall` and returns
  its result.
- Modify: `agent/src/sessions/turnRunner.ts` (the turn's ScadBuddy server is the activity
  projection), `agent/src/sessions/warm.ts` (`fingerprint` includes `tools_as_activities`,
  because a warm process keeps the MCP servers it was spawned with; without it a flip would
  reach only new processes), `agent/src/sessions/turnHost.ts` (a `restarted` continuation fetches the
  results of calls whose `ToolCall` completed and states them in the restart message, in
  place of "unknown"), `agent/src/sessions/forget.ts` (terminate the session's open
  `ToolCall`s, listed from `ai_session_events`' `tool.call` rows that have no result).
- Images and large results move as blobs plus pointers (`SessionBlobs`), never inline in a
  payload.
- Before the PR: measure the deployed Temporal's `history.maxInFlightUpdates` and
  `history.maxTotalUpdates` (Q1). Record them in spec §2.3.

**Tests:** `agent/test/toolCall.temporal.test.ts`:
- a read call runs once, as an activity, on a worker other than the turn's;
- 12 parallel calls in one assistant message all run;
- a turn whose pod dies after a call completed resumes with that call's result in the
  restart message, and the call ran once;
- an outward call parks (spec §5.4) and its approved run carries the request id's key;
- `test/coverage.test.ts` and the tier tests (#2088, #2100) stay green.

**Rollout:** a setting, `tools_as_activities` (an `ai_settings` key read per turn, default
off for one release, then on, then removed in Step 9.9). The turn path stays one:
the flag picks the projection, not a second turn.
**Depends on:** Step 6.3; Step 9.1; Q1. **Closes:** none.

### Step 9.3: Remote plugins, package MCP servers and `http_request` as activities (R1) (PR, `feat(agent): a turn's plugin and http tools run as activities`)

**Files:**
- Modify: `agent/src/sessions/turnRunner.ts`. The turn's remote plugins, package MCP servers
  and `http_request` come from `temporal/turnServers.ts`'s listing, which flows and
  `DurableSession` already use, as one in-process server per source that keeps the
  `mcp__<server>__<tool>` names (#2660 question 1). Each call is a `ToolCall` whose activity
  is `turn:<server>__<tool>`.
- A server whose state lives on a pod (`http_request`'s, a kept plugin session such as
  Playwright's, `sessionClients.ts`) is pinned. Its calls run on the turn's own
  `agent-tools.<hostname>`, the worker pod whose warm process runs the turn. The browser
  stays #4806's `BridgeCall`.
- Modify: `agent/src/harness/run.ts` (no remote `mcpServers` handed to Claude Code; the
  loopback forwarder then serves only the activities).

**Tests:** `agent/test/turnToolCall.temporal.test.ts`:
- a remote plugin's call runs as `turn:<server>__<tool>`;
- a Playwright-style kept session keeps its state across two calls of one turn, on the pod
  the turn runs on;
- a subagent's plugin call takes the same path and parks when outward;
- the flow `turn:` tests stay green.

**Rollout:** the same `tools_as_activities` setting.
**Depends on:** Step 9.2. **Closes:** none.

### Step 9.4: The Anthropic key never enters Claude Code's process (R4) (PR, `feat(agent): a loopback gateway holds the turn's credential`)

Decision Q6.

**Files:**
- Create: `agent/src/gateway/` (`server.ts`: the loopback proxy on `127.0.0.1`, with a token
  per process, the credential's real header added upstream, streaming SSE passed through,
  and each request's usage priced and added to the session's spend as `spent()` does;
  `tokens.ts`).
- Modify: `agent/src/harness/run.ts` `credentialEnv` (it returns the proxy's
  `ANTHROPIC_BASE_URL` and token; the real secret stays in the worker),
  `agent/src/harness/fallback.ts` (a fallback swaps the proxy's mapping, then respawns, as
  now), `agent/src/sessions/warm.ts` (the fingerprint names the proxy token's credential row,
  so a disabled credential still recycles the process, Review Focus 2).
- The gateway credential kind keeps its own `ANTHROPIC_BASE_URL` upstream.

**Tests:**
- `agent/test/gateway.test.ts`: the key never appears in the spawned process's environment
  (read through the spawn seam);
- a request with a wrong token is refused;
- spend through the proxy reaches `cost_usd`;
- `agent/test/run.test.ts` against the fake Anthropic endpoint, through the proxy, for every
  credential kind.

**Rollout:** a release on its own. No flag: the proxy is the only path.
**Depends on:** Step 6.3. **Closes:** none.

### Step 9.5: A session's directory on the shared volume (R3) (PR plus an eh-homelab/clusters PR, `feat(agent): a session's working directory on the NAS share`)

Decision Q5.

**Files:**
- Create: `agent/src/workspace/directory.ts`. It fills `Scadbuddy/sessions/<session-id>/`
  from `SessionFiles` at a placement, writes it back by sha256 at a turn end, a release and
  a shutdown, and marks it stale on a panel or `workspace_*` write. The sweep removes a
  directory whose session is gone.
- Modify: `agent/src/sessions/turnHost.ts` (the warm process's `cwd` is the directory),
  `agent/src/sessions/forget.ts` (remove the directory), `agent/src/config.ts`
  (`SCADBUDDY_SESSION_DIR`).
- Clusters PR: in `scadbuddy-agent-worker.yaml`, `nas-share` at subPath `Scadbuddy/sessions`
  (never the share root, clusters#1877), `SCADBUDDY_SESSION_DIR` set, and the directory in
  Velero's excludes as `nas-share` already is.

**Tests:** `agent/test/sessionDirectory.pg.test.ts`:
- a file written in turn 1 on pod A is there in turn 2 on pod B;
- a panel upload between turns is in the directory at the next placement;
- a pod killed mid-turn loses only that turn's unsynced writes;
- a forgotten session's directory is gone;
- a fork's directory starts from the parent's files as of the cut (#4812's copy).

**Rollout:** the directory is filled and synced, but no tool uses it until Step 9.6.
**Depends on:** #4812 merged; Q5. **Closes:** none. It is #1399's per-agent-session
directory (`Refs #1399`).

### Step 9.6: Claude Code's own built-ins (R2) (PR, `feat(agent)!: Claude Code's own built-ins in every session`)

Decisions Q2, Q3 and Q4.

**Files:**
- Modify: `agent/src/harness/options.ts` (`tools` lists the native built-ins Q3 offers, in
  place of `[]`), `agent/src/harness/permissions.ts` (their tiers from `AccessResolver`
  under `builtin:claude-code`; the `PreToolUse` seam refuses a path outside the session
  directory for the file tools), `agent/src/access/model.ts` (the new source).
- Delete: `agent/src/builtins/files.ts`, `session.ts`, `schemas.json` and
  `agent/test/builtins.sdk.test.ts`. Keep `web.ts` and `nested.ts` if Q2 keeps ScadBuddy's
  WebFetch, and `background.ts` (the Cron, Wakeup, Monitor, Workflow and TaskStop tools stay
  ScadBuddy's, spec §8).
- Modify: `agent/src/builtins/server.ts` (only what stays), `agent/src/temporal/turnServers.ts`
  (no `builtin__` file tools), `docs/ai/operating.md` (the built-ins section).
- If Q4's sandbox holds: the Claude Code sandbox settings for `Bash` and `PowerShell`. If it
  does not: their default access is `off`.

**Tests:**
- `agent/test/nativeBuiltins.sdk.test.ts` (the real binary): `Read`, `Write`, `Edit`, `Glob`
  and `Grep` work in the session directory and are refused outside it;
- the task list survives a release and a move to another pod (Claude Code keeps it in the
  transcript, or the directory: measure which and say so);
- `Bash` parks as `outward`, and with the sandbox it cannot read `/etc/scadbuddy/kek`;
- the evals pass.

**Rollout:** breaking for package authors who relied on `builtin__<Name>` names; the
operating notes say so.
**Depends on:** Steps 9.4 and 9.5; Q2, Q3 and Q4. **Closes:** none.

### Step 9.7: `ai_pending_input` is the record of an open request (R5) (PR, `feat(agent): the gate's record is ai_pending_input`)

Decision Q7.

**Files:**
- Modify: `agent/src/temporal/session/gate.ts` and `workflow.ts` (entries become request ids
  plus timers, behind `patched('gate-record-r5')`), `agent/src/gate/store.ts` (the row first,
  then the wake-up Update), `agent/src/routes/pendingInput.ts` (`respond` writes the
  decision in its transaction, then sends the Update), and a new
  `gate_read` activity in `agent/src/temporal/session/gateActivities.ts`.
- The `pending_input` Query answers from the rows; its shape is unchanged.

**Tests:**
- `agent/test/sessionGateRecord.temporal.test.ts`: a `respond` whose Update is lost is picked
  up by the timer's read;
- a restart of every worker while a park is open keeps it, and its resolution;
- `pending-input-vectors.json` is unchanged, and `agent-durable/tests` still open it;
- the old `agent_session_histories/` fixtures replay.

**Rollout:** `patched()`; old runs keep their entries.
**Depends on:** Step 6.3; Q7. **Closes:** none.

### Step 9.8: Recurring cron jobs are Temporal Schedules (R6) (PR, `feat(agent): cron jobs as Temporal Schedules`)

This replaces the old Task 9.2. Forget already finds a session's background workflows by
their rows (`sessions/forget.ts` step 0), so the Visibility `STARTS_WITH` check of spec §8
is no longer needed. Spec §14.7 records that.

**Files:**
- Modify: `agent/src/background/temporal.ts` (`CronCreate` makes a Schedule
  `bgtask-<session>-<id>` whose action starts a short `BackgroundFire` workflow; `CronDelete`
  and `TaskStop` delete it), `agent/src/temporal/backgroundTask.ts` (wakeups and monitors stay
  timer and poll workflows, behind `patched()` for the fire path), `agent/src/sessions/forget.ts`
  (delete the session's Schedules), and the start-time conversion pass of Q11.

**Tests:** `agent/test/backgroundSchedule.temporal.test.ts`:
- a cron fires on schedule as the session's next message;
- a busy session is asked again (`BUSY_RETRY_MS`);
- forget deletes the Schedule;
- the conversion pass is idempotent;
- a `ScheduleWakeup` still fires once.

**Rollout:** new crons are Schedules from this release on; the pass converts the rest.
**Depends on:** Step 9.1; Q11. **Closes:** none.

### Step 9.9: Dashboard and docs (PR, `docs: the unified agent runtime`)

The old Tasks 9.3 and 9.4.

**Files:**
- `deploy/grafana/scadbuddy-agent.json`: warm hit ratio, spawn p50/p95 by `warm.outcome`,
  releases per hour, continued turns by kind, and tool activities by queue, all from span
  metrics. Check with `KUSTOMIZE=… .github/scripts/lint-dashboard.sh`.
- Docs:
  - `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` ("Amended by
    2026-10-13" at §6 and §6.6);
  - `docs/superpowers/plans/2026-10-11-durable-capability-parity.md` ("Superseded");
  - `docs/ai/operating.md` and `docs/user-guide.md` (no mode picker; restart, park and
    worker behaviour);
  - `CLAUDE.md` (the agent layout: `runtime/`, `sessions/turnRunner.ts`, `warm.ts`,
    `temporal/session/`, `temporal/toolCall.ts`, `gateway/`).
- Remove `tools_as_activities` (Step 9.2): the activity projection is the only one.

**Tests:** `lint-dashboard.sh`, `link-check` on the touched docs, and the agent suite.
**Rollout:** docs. **Depends on:** Steps 9.2–9.8. **Closes:** none.

---

## Phase 10: Drop retired columns and tables (PR 10, `chore(agent): drop the durable-era columns`)

### Task 10.1: Migration

The agent migrates at start, and once Phase 11 moves it to `RollingUpdate` (D6) a release's old pods run beside the new one while it rolls out, so the new pod's migration must not take away what they still read. This migration therefore ships in a release AFTER the one that removed the columns' last readers (Step 6.3 and Phase 8): never in Step 6.3's or Phase 8's release, and only once that release is the one running everywhere. Before opening PR 10, grep the running release's tag for the dropped names (`git grep -nE 'durable_offset|lease_until|turn_id|ai_questions|session_mode' <tag> -- agent/src agent-durable`, plus every read of `ai_sessions.mode`) and expect only migrations.

**Files:**
- Create: `agent/src/db/migrations/<ts>_drop_session_mode.sql`:

```sql
-- Spec 2026-10-13 §10.2: one runtime. durable_offset was the Python follower's cursor;
-- mode was the switch; turn_id/lease_until was classic's claim, dead and NULL on every
-- row since Step 6.3 (the CHECK that paired them goes with the columns).
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

## Phase 11: Versioned queues, then `RollingUpdate` (D6) (PR 11 plus a backend PR, then a clusters PR)

When this plan was written the ScadBuddy pod used `Recreate`, because the API polled the
backend's `library` queue unversioned. Measured on 2026-10-23 (read-only, `kubectl --context
primary -n bambuddy get deploy scadbuddy -o jsonpath='{.spec.strategy}'`) it is
`{"rollingUpdate":{"maxSurge":1,"maxUnavailable":0},"type":"RollingUpdate"}`, so the gap
between the old pods stopping and the new ones polling, which a moved turn's 60 s acquire
could not span (spec §5.5), is gone; Task 11.4's `maxUnavailable: 1, maxSurge: 0` was not
what landed, and `terminationGracePeriodSeconds` is still the default 30 (the same read).
The manifest said moving to RollingUpdate needed that queue versioned first
(`eh-homelab/clusters` `applications/scadbuddy/scadbuddy.yaml:125-131`). After Phase 8,
the worker Deployment `scadbuddy-agent-worker` (Step 6.2) polls `agent-tools`, `agent-sessions`
and its pods' `agent-tools.<hostname>` and `agent-sessions.<hostname>`; the API pod's `agent`
container polls only its own `agent-tools.<identity>`, for `bridge.tab` (#4806). Only
`agent-sessions` is versioned (Task 5.1). Task 11.2 versions the worker Deployment's queues;
that Deployment is `RollingUpdate` from Step 6.2 on.

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

---

## Self-review notes

- **Spec coverage, section by section:**
  - §5.1 → 5.1
  - §5.2 → 5.2; on a worker Deployment → 6.1–6.2
  - §5.3 → 2.1–2.4
  - §5.4 → 5.3–5.4; R5 → Step 9.7
  - §5.5 → 5.5
  - §5.6 → 5.6
  - §6.1 → 5.2
  - §6.2 → 3.1–3.2
  - §6.3 → 3.2, Step 6.3; copy-on-write (§14.7, Q8) → Step 6.5
  - §6.4–6.5 → 5.6
  - §6.6 → 2.2–2.3
  - §6.7 → 2.2 (credential recycle); R4 → Step 9.4
  - §6.8 → Steps 6.3, 8.2; R2 → Step 9.6
  - §6.9 → 2.3, Step 9.9
  - §6.10 → 5.2, Steps 6.2 and 6.4
  - §7 → 5.4; R1's call keys → Step 9.2
  - §8 → Steps 9.1 and 9.8
  - §9 → Step 6.3 (`flowSessions.ts`)
  - §10 → Steps 6.3, 7.1–7.2, Phase 10
  - §11 → Phase 8 (as amended by §14.4 and §14.7)
  - §12's unverified items → 5.4, Step 7.1; the Visibility `STARTS_WITH` item is no longer needed (Step 9.8)
  - §14.3 → Steps 6.1–6.4; §14.4 → Phase 8; §14.5 → Step 6.1 (and #4806 for item 1)
  - §14.7 R1 → 9.2–9.3; R2 → 9.6; R3 → 9.5; R4 → 9.4; R5 → 9.7; R6 → 9.8; R7 → #4812
- **Decisions D1–D7** were made by the user on 2026-10-13 (spec §13):
  - D1 → Step 6.3;
  - D2/D3/D5 → Task 2.5 settings;
  - D3 limit → Task 2.6, moved to the worker container by Step 6.2 (spec §14.7);
  - D4 → 5.1;
  - D6 → Phase 11;
  - D7 → Phase 3.
- **Decisions Q1–Q11** are open (under "Phases 6–9"); the steps that depend on each name it.
- **The user's standing rules, checked:** turns run in a worker Deployment of their own (Steps 6.1–6.2);
  `agent-durable/` stays and only its turn retires (Phase 8); every tool works in every
  session, and nothing is documented as a gap (Steps 6.3 and 9.2–9.6); forks are
  copy-on-write (Step 6.5, #4812); images and large results move as blobs plus pointers
  (Step 9.2); state in Postgres, bytes in the blob store (Steps 9.5 and 9.7, #4812).
