import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { WorkflowNotFoundError } from '@temporalio/client'
import { Worker } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { sessionWorkflowId } from '../src/gate/durable.js'
import { durableRequestId } from '../src/gate/ids.js'
import { openInput } from '../src/gate/store.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { backfill, DurableMigration, MIGRATED_REASON } from '../src/sessions/migrateDurable.js'
import { event } from '../src/sessions/protocol.js'
import { SESSION_QUEUE, SESSION_WORKFLOW } from '../src/temporal/session/names.js'
import { type FakeAnthropic, type RecordedRequest, startFakeAnthropic } from './support/fakeAnthropic.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { AGENT_SESSIONS_SKIP, agentSessions, agentSessionsEnv } from './support/agentSessions.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, stopManagers, tempPaths } from './support/sessions.js'

// Plan 2026-10-13 Step 7.2 (spec §10.2, #4772): a DurableSession is moved to AgentSession at its
// next send, and the backfill moves every open one. The DurableSession is the stand-in
// (support/durableSessionWorkflows.ts) holding Step 7.1's recorded conversation
// (fixtures/durable_conversation/, a real DurableSession's `fork_point` pages); the AgentSession
// turn after it is the real one (support/agentSessions.ts), on the real SDK and its bundled
// Claude Code against the fake Anthropic endpoint.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const STANDIN = fileURLToPath(new URL('./support/durableSessionWorkflows.ts', import.meta.url))
const STANDIN_QUEUE = `durable-standin-${randomUUID().slice(0, 8)}`
const FIXTURE = new URL('./fixtures/durable_conversation/', import.meta.url)
const TOKEN = 'gw-migrate-durable-token-12123434565678'
const NEXT = 'And what name is on it?'
const FIRST_PROMPT = 'On the name-keychain model, change the name to Ada'

type Meta = { session_id: string; checkpoint: string; total: number }

async function fixture(): Promise<{ texts: string[]; meta: Meta }> {
  const texts = (await readFile(new URL('entries.jsonl', FIXTURE), 'utf8')).split('\n').filter((l) => l.trim())
  return { texts, meta: JSON.parse(await readFile(new URL('meta.json', FIXTURE), 'utf8')) as Meta }
}

const said = (r: RecordedRequest | undefined) => JSON.stringify(r?.body?.messages ?? [])

describe.skipIf(skip !== undefined || AGENT_SESSIONS_SKIP !== '')(
  `migrating a DurableSession to AgentSession${skip ? ` (skipped: ${skip})` : ''}${AGENT_SESSIONS_SKIP}`,
  { timeout: 180_000 },
  () => {
    agentSessions()
    let standin: Worker
    let standinRun: Promise<void>
    let fake: FakeAnthropic
    let db: Database
    let drop: () => Promise<void>
    let mine: Set<string>

    beforeAll(async () => {
      standin = await Worker.create({
        connection: agentSessionsEnv().nativeConnection,
        taskQueue: STANDIN_QUEUE,
        workflowsPath: STANDIN,
        // The stand-in's page counter is not in its history (durableSessionWorkflows.ts).
        maxCachedWorkflows: 100,
      })
      standinRun = standin.run()
    }, 120_000)
    afterAll(async () => {
      standin?.shutdown()
      await standinRun
    })
    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      fake = await startFakeAnthropic(() => ({ text: 'The name is Ada.' }))
      mine = new Set()
    })
    afterEach(async () => {
      await stopManagers()
      for (const id of mine) {
        await agentSessionsEnv()
          .client.workflow.getHandle(sessionWorkflowId(id))
          .terminate('the test ended')
          .catch((err: unknown) => {
            if (!(err instanceof WorkflowNotFoundError)) throw err
          })
      }
      await fake.close()
      await drop()
    })

    async function agent(): Promise<SessionManager> {
      const paths = await tempPaths()
      await ensureStateDirs(paths)
      return manager({
        sql: db.sql,
        paths,
        credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
        settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      })
    }

    /** A session whose DurableSession (the stand-in) has run Step 7.1's two turns, as a release before Step 7.2 left it. */
    async function durableSession(m: SessionManager, options: { busy?: boolean } = {}): Promise<string> {
      const { session } = await m.start(browser, { origin: 'chat', title: 'keychain' })
      mine.add(session.id)
      await db.sql`UPDATE ai_sessions SET runtime = 'durable', durable_offset = 14 WHERE id = ${session.id}`
      const { texts, meta } = await fixture()
      const handle = await agentSessionsEnv().client.workflow.start('DurableSession', {
        workflowId: sessionWorkflowId(session.id),
        taskQueue: STANDIN_QUEUE,
        args: [{}],
      })
      await handle.signal('set_conversation', { session_id: meta.session_id, checkpoint: meta.checkpoint, conversation: texts })
      // Its data key, which its payloads were sealed under (the stand-in's client seals nothing).
      await testPayloadKeys(db.sql).keyFor(sessionWorkflowId(session.id), true)
      if (options.busy) await handle.executeUpdate('send_message', { args: [{ turn_id: 't-old', text: 'still going', author: browser, images: [] }] })
      return session.id
    }

    /**
     * Parks a call of the running turn, as DurableSession does (its `open_input` writes the row and
     * the session waits): the request id. The stand-in models no park, so `cancel_input` resolves
     * nothing; once the workflow is closed the migration ends the row itself.
     */
    async function park(id: string): Promise<string> {
      const { runId } = await agentSessionsEnv().client.workflow.getHandle(sessionWorkflowId(id)).describe()
      const requestId = durableRequestId(id, runId, 'toolu_parked')
      await openInput(db.sql, {
        requestId,
        sessionId: id,
        workflowId: sessionWorkflowId(id),
        workflowRunId: runId,
        kind: 'approval',
        tool: 'print_output',
        toolUseId: 'toolu_parked',
        summary: '{"output":"keychain"}',
        inputHash: 'a'.repeat(64),
        prompt: '',
        requestedBy: browser,
        responders: ['browser', 'grant'],
        expiresAt: new Date(Date.now() + 600_000).toISOString(),
      })
      await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${id}`
      return requestId
    }

    const migration = (m: SessionManager) =>
      new DurableMigration({ client: agentSessionsEnv().client, sql: db.sql, store: m.store, events: m.events, sessionQueue: SESSION_QUEUE, pollMs: 50, settleTimeoutMs: 20_000 })

    const runs = async (id: string) => {
      const out: { type: string; status: string }[] = []
      for await (const w of agentSessionsEnv().client.workflow.list({ query: `WorkflowId = '${sessionWorkflowId(id)}'` })) {
        out.push({ type: w.type, status: w.status.name })
      }
      return out
    }

    /** Temporal's visibility catches up a moment after a start or a close. */
    async function eventually<T>(read: () => Promise<T>, ok: (v: T) => boolean): Promise<T> {
      const until = Date.now() + 20_000
      for (;;) {
        const v = await read()
        if (ok(v) || Date.now() > until) return v
        await new Promise((resolve) => setTimeout(resolve, 200))
      }
    }

    it('a send migrates an open DurableSession first', async () => {
      const m = await agent()
      const id = await durableSession(m)
      const old = await agentSessionsEnv().client.workflow.getHandle(sessionWorkflowId(id)).describe()
      const turn = await m.send(id, browser, NEXT)
      expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })

      // The old workflow got `cancel_input(migrated)`, then `end`, and closed.
      const recorded = await agentSessionsEnv()
        .client.workflow.getHandle(sessionWorkflowId(id), old.runId)
        .query<{ calls: string[]; messages: unknown[] }>('recorded')
      expect(recorded.calls).toEqual([`cancel_input:${MIGRATED_REASON}`, 'end'])
      expect(recorded.messages).toEqual([])
      expect((await agentSessionsEnv().client.workflow.getHandle(sessionWorkflowId(id), old.runId).describe()).status.name).toBe('COMPLETED')
      expect((await agentSessionsEnv().client.workflow.getHandle(sessionWorkflowId(id)).describe()).type).toBe(SESSION_WORKFLOW)
      // The new turn's request carries the old turns, their tool calls and results, then the prompt.
      const request = said(fake.messageCalls().at(-1))
      for (const part of [FIRST_PROMPT, 'toolu_000001', 'toolu_000003', 'Thanks. Which colour did you use for the letters?', 'I used red (#ff0000)', NEXT]) {
        expect(request).toContain(part)
      }
      expect(request.indexOf(FIRST_PROMPT)).toBeLessThan(request.indexOf(NEXT))
      const [row] = await db.sql<{ runtime: string; status: string }[]>`SELECT runtime, status FROM ai_sessions WHERE id = ${id}`
      expect(row).toEqual({ runtime: 'unified', status: 'idle' })
      // The transcript is the session's own now, written under its id.
      const { texts } = await fixture()
      const stored = (await m.store.load({ projectKey: '', sessionId: id }))!
      expect(stored.length).toBeGreaterThan(texts.length)
      expect(new Set(stored.map((e) => (e as { sessionId?: string }).sessionId).filter(Boolean))).toEqual(new Set([id]))
      // A second turn goes to the same AgentSession.
      const second = await m.send(id, browser, 'Thanks.')
      expect(await second.done).toMatchObject({ kind: 'result' })
      expect(said(fake.messageCalls().at(-1))).toContain(FIRST_PROMPT)
    })

    it('the backfill migrates every open DurableSession and is idempotent', async () => {
      const m = await agent()
      const idle = await durableSession(m)
      const other = await durableSession(m)
      // One whose turn still runs, parked on an approval: the backfill ends the park and stops the turn first.
      const busy = await durableSession(m, { busy: true })
      const requestId = await park(busy)
      const only = (w: string) => [idle, other, busy].some((id) => w === sessionWorkflowId(id))
      await eventually(
        async () => (await backfill(agentSessionsEnv().client, db.sql, migration(m), { dryRun: true, only })).lines.length,
        (n) => n === 3,
      )

      // The dry run lists them and changes nothing.
      const dry = await backfill(agentSessionsEnv().client, db.sql, migration(m), { dryRun: true, only })
      const { meta } = await fixture()
      expect(dry.counts).toEqual({ idle: 2, busy: 1 })
      expect(dry.lines.every((l) => l.entries === meta.total && l.versions?.join() === '2.1.292')).toBe(true)
      expect(dry.shapes.assistant).toContain('message')
      expect(dry.open).toBe(3)
      expect(await db.sql`SELECT 1 FROM ai_session_entries`).toHaveLength(0)
      for (const id of [idle, other, busy]) expect((await runs(id)).map((r) => r.status)).toEqual(['RUNNING'])

      const report = await backfill(agentSessionsEnv().client, db.sql, migration(m), { only })
      expect(report.counts).toEqual({ migrated: 3 })
      expect(report.failed).toBe(0)
      const counts = async () =>
        (await db.sql<{ session_id: string; n: number }[]>`SELECT session_id, count(*)::int AS n FROM ai_session_entries GROUP BY session_id ORDER BY session_id`).map((r) => [r.session_id, r.n])
      const written = await counts()
      expect(written).toHaveLength(3)
      for (const id of [idle, other, busy]) {
        const { runtime } = (await db.sql<{ runtime: string }[]>`SELECT runtime FROM ai_sessions WHERE id = ${id}`)[0]!
        expect(runtime).toBe('unified')
        expect(
          (await eventually(
            () => runs(id),
            (r) => r.length === 2 && r.some((x) => x.type === 'DurableSession' && x.status === 'COMPLETED'),
          )).map((r) => `${r.type}:${r.status}`).sort(),
        ).toEqual(['AgentSession:RUNNING', 'DurableSession:COMPLETED'])
      }
      // The stopped turn got the interrupt.
      const runsOfBusy: string[] = []
      for await (const w of agentSessionsEnv().client.workflow.list({ query: `WorkflowId = '${sessionWorkflowId(busy)}' AND WorkflowType = 'DurableSession'` })) runsOfBusy.push(w.runId)
      const { calls } = await agentSessionsEnv().client.workflow.getHandle(sessionWorkflowId(busy), runsOfBusy[0]).query<{ calls: string[] }>('recorded')
      expect(calls).toEqual([`cancel_input:${MIGRATED_REASON}`, `interrupt:${MIGRATED_REASON}`, 'end'])
      // Its parked entry ended `cancelled`, reason `migrated`, and the session is idle.
      expect(await db.sql`SELECT 1 FROM ai_pending_input WHERE session_id = ${busy}`).toHaveLength(0)
      const [response] = await db.sql<{ outcome: string; reason: string }[]>`SELECT outcome, reason FROM ai_input_responses WHERE request_id = ${requestId}`
      expect(response).toEqual({ outcome: 'cancelled', reason: MIGRATED_REASON })
      expect((await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${busy}`)[0]?.status).toBe('idle')

      // Again: nothing is open, nothing is written twice, and a migration asked again says so.
      const again = await eventually(
        () => backfill(agentSessionsEnv().client, db.sql, migration(m), { only }),
        (r) => r.lines.length === 0,
      )
      expect(again).toMatchObject({ lines: [], failed: 0, open: 0 })
      for (const id of [idle, other, busy]) expect(await migration(m).migrate(id)).toBe('agent_session')
      expect(await counts()).toEqual(written)
    })

    it("a migrated session's events stay readable in the panel", async () => {
      const m = await agent()
      const id = await durableSession(m)
      // What DurableSession's follow_session wrote for its turns (agent-durable session/events.py).
      await m.events.append(id, [
        event({ type: 'user.turn', sessionId: id, turnId: 't-1', text: FIRST_PROMPT, author: browser }),
        event({ type: 'session.status', sessionId: id, status: 'running' }),
        event({ type: 'assistant.text.delta', sessionId: id, messageId: 'durable-12', delta: 'Rendered name-keychain' }),
        event({ type: 'assistant.text.done', sessionId: id, messageId: 'durable-12' }),
        event({ type: 'session.status', sessionId: id, status: 'idle' }),
      ])
      const before = (await m.events.read(id)).map((e) => e.event)
      const turn = await m.send(id, browser, NEXT)
      await turn.done
      const after = (await m.events.read(id)).map((e) => e.event)
      expect(after.slice(0, before.length)).toEqual(before)
      const texts = after.flatMap((e) => (e.type === 'user.turn' ? [e.text] : []))
      expect(texts).toEqual([FIRST_PROMPT, NEXT])
      expect(after.some((e) => e.type === 'assistant.text.delta' && e.messageId === 'durable-12')).toBe(true)
      expect(after.at(-1)).toMatchObject({ type: 'session.status', status: 'idle' })
    })
  },
)
