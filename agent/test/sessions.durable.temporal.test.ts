import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { createHash, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import type { Client } from '@temporalio/client'
import type { Sql } from 'postgres'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { AttachmentStore } from '../src/attachments/store.js'
import type { Database } from '../src/db.js'
import { ChatConnection } from '../src/routes/chat.js'
import { sessionWorkflowId } from '../src/gate/durable.js'
import { UNTRUSTED_CONTENT_POLICY } from '../src/safety/untrusted.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { AGENT_SESSIONS_SKIP, agentSessions, withoutTemporal } from './support/agentSessions.js'
import { DurableSessionTurns } from './support/durableSessionTurns.js'
import { SessionFiles } from '../src/workspace/store.js'
import { type DescribeSession, DurableRunningSweep, durableDescriber } from '../src/sessions/durableSweep.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import type { UserImage } from '../src/sessions/images.js'
import { type SessionManager, type SessionRecord } from '../src/sessions/manager.js'
import type { ServerEvent } from '../src/sessions/protocol.js'
import { frontendClientMessages } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// The agent service's dispatch of a durable session (plan 5c PR 3, Rulings 7, 8, 13):
// the mode set at insert, a send as claim + blobs + `user.turn` + update-with-start of
// `send_message`, `done` read from the event log, interrupt and handoff through the
// gate, and a fork (#2688). Against a stand-in DurableSession (support/durableSessionWorkflows.ts);
// test/durable.e2e.test.ts runs the real one.

const PG_SKIP = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`
const QUEUE = 'durable-standin'
const WORKFLOWS = fileURLToPath(new URL('./support/durableSessionWorkflows.ts', import.meta.url))

const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
const IMAGE: UserImage = {
  mediaType: 'image/png',
  bytes: Buffer.from(PNG, 'base64'),
  preview: { mediaType: 'image/png', bytes: Buffer.from(PNG, 'base64') },
}
const PNG_NAME = `${createHash('sha256').update(Buffer.from(PNG, 'base64')).digest('hex')}.png`

type Recorded = { start: Record<string, unknown>; messages: Record<string, unknown>[]; calls: string[]; forkPointPage0Queries: number }

/** A stored `session_mode`, which nothing reads since plan Step 6.3. */
const mode = (value: string | undefined) => ({
  get: <T>(key: string) => Promise.resolve((key === 'session_mode' ? value : undefined) as T),
})

describe.skipIf(!TEST_DATABASE_URL || AGENT_SESSIONS_SKIP)(`a session's mode at insert${PG_SKIP}${AGENT_SESSIONS_SKIP}`, () => {
  agentSessions()
  let db: Database
  let drop: () => Promise<void>
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it('is an AgentSession whatever `session_mode` says and whatever mode the caller names (plan Step 6.3)', async () => {
    for (const value of [undefined, 'durable', 'classic', 'nonsense']) {
      for (const asked of [undefined, 'classic', 'durable'] as const) {
        const m = manager({ sql: db.sql, paths: await tempPaths(), settings: mode(value), newSessions: { max: 100, windowMs: 60_000 } })
        const { session } = await m.start(browser, { origin: 'chat', title: 't', ...(asked ? { mode: asked } : {}) })
        expect(session.mode).toBe('durable')
        const [row] = await db.sql<{ runtime: string | null }[]>`SELECT runtime FROM ai_sessions WHERE id = ${session.id}`
        expect(row?.runtime).toBe('unified')
        const [started] = await m.events.read(session.id, 0)
        expect(started?.event).toMatchObject({ type: 'session.started', mode: 'durable' })
        expect(started?.event).not.toHaveProperty('modeFallback')
      }
    }
  })

  it('refuses a new session `unavailable` without Temporal or the key, and inserts nothing', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), settings: mode('classic') })
    withoutTemporal(m)
    await expect(m.start(browser, { origin: 'chat', title: 't', mode: 'classic' })).rejects.toMatchObject({
      code: 'unavailable',
      status: 503,
    })
    expect(await db.sql`SELECT id FROM ai_sessions`).toHaveLength(0)
  })
})

describe.skipIf(!TEMPORAL_CLI || !TEST_DATABASE_URL)(`a durable session's dispatch${TEMPORAL_SKIP || PG_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let worker: Worker
  let running: Promise<void>
  let db: Database
  let drop: () => Promise<void>
  beforeAll(async () => {
    env = await localTemporal()
    // A cache that never evicts: the stand-in's `owed_reads` count lives in a Query, in no
    // history, and would start again on a replay (support/durableSessionWorkflows.ts). The
    // number need only exceed the workflows this file opens; lowered below that, the reset
    // comes back.
    worker = await Worker.create({ connection: env.nativeConnection, taskQueue: QUEUE, workflowsPath: WORKFLOWS, maxCachedWorkflows: 10_000 })
    running = worker.run()
  }, 120_000)
  afterAll(async () => {
    worker?.shutdown()
    await running?.catch(() => {})
    await env?.teardown()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  async function durableManager(
    options: { cancelTimeoutMs?: number; sendTimeoutMs?: number; taskQueue?: string } = {},
  ): Promise<SessionManager> {
    const m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      settings: mode('durable'),
      // A durable turn never runs here: the classic runner would fail the test.
      run: scriptedRunner(() => ({ throws: 'a durable turn ran in the classic harness' })).runner,
    })
    m.durableTurns = new DurableSessionTurns({
      client: env.client,
      sql: db.sql,
      events: m.events,
      taskQueue: options.taskQueue ?? QUEUE,
      ...(options.cancelTimeoutMs ? { cancelTimeoutMs: options.cancelTimeoutMs } : {}),
      ...(options.sendTimeoutMs ? { sendTimeoutMs: options.sendTimeoutMs } : {}),
    })
    return m
  }

  const recorded = (sessionId: string) =>
    env.client.workflow.getHandle(sessionWorkflowId(sessionId)).query<Recorded>('recorded')

  async function finishTurn(sessionId: string, tail: Record<string, unknown>[]) {
    await db.sql`UPDATE ai_sessions SET status = 'idle' WHERE id = ${sessionId}`
    // As finish_turn writes them (agent-durable session/activities.py).
    await db.sql.begin(async (tx) => {
      for (const e of tail) {
        await tx`
          WITH s AS (UPDATE ai_sessions SET event_seq = event_seq + 1 WHERE id = ${sessionId} RETURNING event_seq)
          INSERT INTO ai_session_events (session_id, seq, event) SELECT ${sessionId}, event_seq, ${JSON.stringify({ v: 1, sessionId, ...e })} FROM s`
      }
    })
    await env.client.workflow.getHandle(sessionWorkflowId(sessionId)).signal('end_turn')
  }

  it('starts the workflow with the first message and sends it, with the turn written first', async () => {
    const m = await durableManager()
    const { session, turn } = await m.start(browser, {
      origin: 'chat',
      prompt: 'make me a bracket',
      context: '[page: /models/bracket]',
      images: [IMAGE],
    })
    expect(session.mode).toBe('durable')
    expect(turn).toBeDefined()
    const { start, messages } = await recorded(session.id)
    expect(start).toMatchObject({
      session_id: session.id,
      owner: { kind: 'browser', id: 'browser', label: 'You' },
      creator: { kind: 'browser', id: 'browser' },
      max_turns: session.maxTurns,
    })
    // What a classic turn appends to Claude Code's prompt (Ruling 15).
    expect(start.system_append).toContain(UNTRUSTED_CONTENT_POLICY)
    expect(messages).toEqual([
      {
        turn_id: turn!.turnId,
        text: 'make me a bracket\n\n[page: /models/bracket]',
        author: browser,
        images: [{ name: PNG_NAME, mediaType: 'image/png' }],
        // Each turn carries the append as Settings stand when it is sent (#2525).
        system_append: start.system_append,
      },
    ])
    // The blob the worker reads, by the name the message carries (Ruling 5).
    const [blob] = await db.sql<{ name: string }[]>`SELECT name FROM ai_session_blobs WHERE session_id = ${session.id}`
    expect(blob?.name).toBe(PNG_NAME)
    const log = await m.events.read(session.id)
    const userTurn = log.find((e) => e.event.type === 'user.turn')?.event
    // The page context goes to the model, not the transcript; the images go by name (#2479).
    expect(userTurn).toMatchObject({
      text: 'make me a bracket',
      turnId: turn!.turnId,
      author: browser,
      images: [{ name: PNG_NAME, mediaType: 'image/png', preview: { name: PNG_NAME, mediaType: 'image/png' } }],
    })
    expect(JSON.stringify(log)).not.toContain(PNG.slice(0, 40))
    expect(log.at(-1)?.event).toMatchObject({ type: 'session.status', status: 'running' })
    const [row] = await db.sql<{ status: string; turn_id: string | null }[]>`SELECT status, turn_id FROM ai_sessions WHERE id = ${session.id}`
    // Durable turns take no turn_id/lease claim (Ruling 7).
    expect(row).toEqual({ status: 'running', turn_id: null })
  }, 60_000)

  it("marks a turn a flow run sent with the run and its step, and no other turn (#2481)", async () => {
    const m = await durableManager()
    const flow = { run_id: '7e2a1c3b-4d5e-4f60-8a9b-0c1d2e3f4a5b', call_id: 'c1' }
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'from a flow', flow })
    const { messages } = await recorded(session.id)
    expect(messages).toEqual([expect.objectContaining({ flow })])
    const { session: plain } = await m.start(browser, { origin: 'chat', prompt: 'from a person' })
    const { messages: theirs } = await recorded(plain.id)
    expect(theirs).toHaveLength(1)
    expect(theirs[0]).not.toHaveProperty('flow')
  }, 60_000)

  it("records the tiers a turn was sent with, for its tool activities, and none for a browser turn (#2320)", async () => {
    const m = await durableManager()
    const token = { kind: 'bearer' as const, id: 'token:t', label: 'MCP token' }
    const { session } = await m.start(token, { origin: 'mcp', prompt: 'hi', tiers: ['read', 'write'] })
    const tiersOf = async (id: string) =>
      (await db.sql<{ turn_tiers: unknown }[]>`SELECT turn_tiers FROM ai_sessions WHERE id = ${id}`)[0]?.turn_tiers
    // With who sent them: they apply only while that sender owns the session.
    expect(await tiersOf(session.id)).toEqual({ by: { kind: 'bearer', id: 'token:t' }, tiers: ['read', 'write'] })
    const { session: own } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    expect(await tiersOf(own.id)).toBeNull()
  }, 60_000)

  it("takes a turn's images from the chat socket's attachments, by name (#1959: the only way a panel sends them)", async () => {
    const m = await durableManager()
    const attachments = new AttachmentStore(db.sql)
    const { id } = await attachments.put(browser, IMAGE)
    const out: ServerEvent[] = []
    const connection = new ChatConnection(m, (e) => out.push(e), { attachments, snapshotMs: 60_000 })
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(
      JSON.stringify(
        clientMessage({ type: 'user.message', text: 'look', context: { route: '/' }, images: [{ kind: 'attachment', id }] }),
      ),
    )
    connection.close()
    expect(out.filter((e) => e.type === 'error')).toEqual([])
    // receive() returns once the turn has started; its events reach `out` later, so the row names the session.
    const [session] = await db.sql<{ id: string; mode: string }[]>`SELECT id, mode FROM ai_sessions`
    expect(session?.mode).toBe('durable')
    const { messages } = await recorded(session!.id)
    expect(messages).toEqual([expect.objectContaining({ images: [{ name: PNG_NAME, mediaType: 'image/png' }] })])
    const [blob] = await db.sql<{ name: string }[]>`SELECT name FROM ai_session_blobs WHERE session_id = ${session!.id}`
    expect(blob?.name).toBe(PNG_NAME)
    // Moved into the session (attachments/store.ts `claim`).
    expect(await db.sql`SELECT 1 FROM ai_attachments WHERE id = ${id}`).toHaveLength(0)
  }, 60_000)

  it("reads the turn's end from the log, and refuses a send while a turn runs", async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    const turn = await m.send(session.id, browser, 'hello')
    await expect(m.send(session.id, browser, 'again')).rejects.toMatchObject({ code: 'busy' })
    await finishTurn(session.id, [
      { type: 'session.result', costUsd: 0.25, turns: 2, budgetUsd: 5 },
      { type: 'session.status', status: 'idle' },
    ])
    expect(await turn.done).toEqual({ kind: 'result', subtype: 'success', costUsd: 0.25, turns: 2 })

    const second = await m.send(session.id, browser, 'and then')
    await finishTurn(session.id, [
      { type: 'session.result', costUsd: 0.3, turns: 3, budgetUsd: 5 },
      { type: 'error', code: 'interrupted', message: 'the turn was interrupted' },
      { type: 'session.status', status: 'idle' },
    ])
    expect(await second.done).toEqual({ kind: 'interrupted' })

    const third = await m.send(session.id, browser, 'once more')
    await finishTurn(session.id, [
      { type: 'session.result', costUsd: 0.3, turns: 3, budgetUsd: 5 },
      { type: 'error', code: 'turn_failed', message: 'the engine died' },
      { type: 'session.status', status: 'failed' },
    ])
    expect(await third.done).toEqual({ kind: 'failed', message: 'the engine died' })
    expect((await recorded(session.id)).messages.map((x) => x.text)).toEqual(['hello', 'and then', 'once more'])
  }, 60_000)

  it("gives the claim back when the workflow refuses the message, and only the owner sends", async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await m.send(session.id, browser, 'hello')
    // The row says idle, the workflow still runs the turn: its validator refuses.
    await db.sql`UPDATE ai_sessions SET status = 'idle' WHERE id = ${session.id}`
    await expect(m.send(session.id, browser, 'again')).rejects.toMatchObject({ code: 'busy' })
    expect((await m.get(session.id, browser)).status).toBe('idle')
    expect((await m.events.read(session.id)).at(-1)?.event).toMatchObject({ type: 'session.status', status: 'idle' })
    await expect(m.send(session.id, agentA, 'mine now')).rejects.toMatchObject({ code: 'not_found' })
  }, 60_000)

  it('keeps the claim when no worker takes the message in time, and the turn runs when one does', async () => {
    const queue = `durable-late-${randomUUID()}`
    const m = await durableManager({ sendTimeoutMs: 1_000, taskQueue: queue })
    // No worker polls this queue yet; the start must still be durable (5d would fall back).
    m.durableTurns!.unready = () => Promise.resolve(undefined)
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    expect(session.mode).toBe('durable')
    // No worker on the queue: the Update is admitted with the start but never accepted.
    // Its outcome is unknown, so the claim stays and the turn is handed back.
    const turn = await m.send(session.id, browser, 'hello')
    expect((await m.get(session.id, browser)).status).toBe('running')
    expect((await m.events.read(session.id)).at(-1)?.event).toMatchObject({ type: 'session.status', status: 'running' })
    await expect(m.send(session.id, browser, 'again')).rejects.toMatchObject({ code: 'busy' })

    const late = await Worker.create({ connection: env.nativeConnection, taskQueue: queue, workflowsPath: WORKFLOWS })
    await late.runUntil(async () => {
      // The workflow took the very message the row is running.
      const handle = env.client.workflow.getHandle(sessionWorkflowId(session.id))
      let messages: Record<string, unknown>[] = []
      for (let i = 0; i < 100 && messages.length === 0; i++) {
        messages = (await handle.query<Recorded>('recorded')).messages
        if (messages.length === 0) await new Promise((r) => setTimeout(r, 100))
      }
      expect(messages).toMatchObject([{ turn_id: turn.turnId, text: 'hello' }])
      expect((await m.get(session.id, browser)).status).toBe('running')
      await finishTurn(session.id, [
        { type: 'session.result', costUsd: 0.1, turns: 1, budgetUsd: 5 },
        { type: 'session.status', status: 'idle' },
      ])
      expect(await turn.done).toEqual({ kind: 'result', subtype: 'success', costUsd: 0.1, turns: 1 })
    })
  }, 60_000)

  it('gives back the status the row had before the claim, so a refused send keeps `failed`', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await m.send(session.id, browser, 'hello')
    // The row says failed (a turn failed), the workflow still runs one: its validator refuses.
    await db.sql`UPDATE ai_sessions SET status = 'failed' WHERE id = ${session.id}`
    await expect(m.send(session.id, browser, 'again')).rejects.toMatchObject({ code: 'busy' })
    expect((await m.get(session.id, browser)).status).toBe('failed')
    expect((await m.events.read(session.id)).at(-1)?.event).toMatchObject({ type: 'session.status', status: 'failed' })
  }, 60_000)

  it('rethrows the refusal, not the give-back\'s own failure', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await db.sql`ALTER TABLE ai_session_blobs RENAME TO ai_session_blobs_gone`
    // The give-back cannot write either.
    await db.sql.unsafe(`
      CREATE FUNCTION keep_running() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'give-back refused'; END $$;
      CREATE TRIGGER keep_running BEFORE UPDATE OF status ON ai_sessions
        FOR EACH ROW WHEN (OLD.status = 'running' AND NEW.status <> 'running') EXECUTE FUNCTION keep_running();`)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      await expect(m.send(session.id, browser, 'hello', { images: [IMAGE] })).rejects.toMatchObject({
        name: 'SessionError',
        code: 'unavailable',
      })
      // The give-back's failure is said once, payload-free: the session id, no prompt, no cause.
      expect(warn).toHaveBeenCalledTimes(1)
      const [line] = warn.mock.calls[0] as [string]
      expect(line).toContain(session.id)
      expect(line).not.toContain('hello')
      expect(line).not.toContain('give-back refused')
    } finally {
      warn.mockRestore()
    }
  }, 60_000)

  it("gives the claim back, as `unavailable`, when the turn's images cannot be stored", async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await db.sql`ALTER TABLE ai_session_blobs RENAME TO ai_session_blobs_gone`
    await expect(m.send(session.id, browser, 'hello', { images: [IMAGE] })).rejects.toMatchObject({
      name: 'SessionError',
      code: 'unavailable',
    })
    expect((await m.get(session.id, browser)).status).toBe('idle')
  }, 60_000)

  it('interrupts with cancel_input, then the interrupt Signal', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'hello' })
    expect(await m.interrupt(session.id, browser)).toBe(true)
    expect((await recorded(session.id)).calls).toEqual(['cancel_input:interrupted by You', 'interrupt:interrupted by You'])
    // No turn: nothing to stop.
    await finishTurn(session.id, [{ type: 'session.status', status: 'idle' }])
    expect(await m.interrupt(session.id, browser)).toBe(false)
  }, 60_000)

  it('leaves a fresh first claim whose workflow is not started yet to the sweep, on Stop (#4131 review)', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    // A first send's claim, its update-with-start possibly still in flight.
    await db.sql`UPDATE ai_sessions SET status = 'running', updated_at = now() WHERE id = ${session.id}`
    expect(await m.interrupt(session.id, browser)).toBe(false)
    expect(await status(session.id)).toBe('running')
    expect((await m.events.read(session.id)).some((e) => e.event.type === 'error')).toBe(false)
  }, 60_000)

  it('ends at once, when Stop finds no workflow past the grace, a running session the sweep would end later (#4018)', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    // A kept claim whose workflow never started, past RUNNING_GRACE_MS.
    await db.sql`UPDATE ai_sessions SET status = 'running', updated_at = now() - interval '5 minutes' WHERE id = ${session.id}`
    expect(await m.interrupt(session.id, browser)).toBe(true)
    expect(await status(session.id)).toBe('idle')
    expect((await m.events.read(session.id)).slice(-2).map((e) => e.event)).toMatchObject([
      { type: 'error', code: 'turn_failed', message: expect.stringMatching(/could not be found/) },
      { type: 'session.status', status: 'idle' },
    ])
    // Once ended, Stop has nothing left to stop.
    expect(await m.interrupt(session.id, browser)).toBe(false)
  }, 60_000)

  it('ends, as ended, a running session whose workflow was terminated, on Stop (#4018)', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'hello' })
    await env.client.workflow.getHandle(sessionWorkflowId(session.id)).terminate('ended')
    expect(await status(session.id)).toBe('running')
    expect(await m.interrupt(session.id, browser)).toBe(true)
    expect(await status(session.id)).toBe('idle')
    expect((await m.events.read(session.id)).at(-2)?.event).toMatchObject({
      type: 'error',
      code: 'turn_failed',
      message: expect.stringMatching(/workflow has ended/),
    })
  }, 60_000)

  it('ends a session parked on a call whose workflow is gone on Stop, and cancels the entry no run will resolve (#4018)', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'print it' })
    // The turn parked on an approval, then its workflow ended without resolving it.
    const rid = `durable:${session.id}:run-1:toolu_p`
    await db.sql`
      INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, input_hash, prompt,
                                    requested_by, responders, expires_at)
      VALUES (${rid}, ${session.id}, ${sessionWorkflowId(session.id)}, 'run-1', 'approval', 'print_output', 'print box', ${'a'.repeat(64)}, '',
              ${db.sql.json({ kind: 'browser', id: 'browser', label: 'You' })}, ${['browser']}, now() + interval '1 hour')`
    await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${session.id}`
    await env.client.workflow.getHandle(sessionWorkflowId(session.id)).terminate('ended')
    expect(await m.interrupt(session.id, browser)).toBe(true)
    expect(await status(session.id)).toBe('idle')
    expect(await db.sql`SELECT 1 FROM ai_pending_input WHERE session_id = ${session.id}`).toHaveLength(0)
    const [response] = await db.sql<{ outcome: string }[]>`SELECT outcome FROM ai_input_responses WHERE request_id = ${rid}`
    expect(response?.outcome).toBe('cancelled')
    const log = (await m.events.read(session.id)).map((e) => e.event)
    expect(log).toContainEqual(expect.objectContaining({ type: 'error', code: 'turn_failed', message: expect.stringMatching(/workflow has ended/) }))
    expect(log).toContainEqual(expect.objectContaining({ type: 'approval.resolved', id: rid, decision: 'cancelled' }))
    // Once ended, Stop has nothing left to stop.
    expect(await m.interrupt(session.id, browser)).toBe(false)
  }, 60_000)

  it('writes nothing on Stop when the gone workflow is open again by the time it is described (#4131 review)', async () => {
    const m = await durableManager()
    m.durableTurns = new DurableSessionTurns({ client: env.client, sql: db.sql, events: m.events, taskQueue: QUEUE, describe: async () => 'open' })
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await db.sql`UPDATE ai_sessions SET status = 'running', updated_at = now() - interval '5 minutes' WHERE id = ${session.id}`
    expect(session.mode).toBe('durable')
    expect(await m.interrupt(session.id, browser)).toBe(false)
    expect(await status(session.id)).toBe('running')
    expect((await m.events.read(session.id)).some((e) => e.event.type === 'error')).toBe(false)
  }, 60_000)

  it.each([
    ['ends it past the grace', 5, true, 'idle'],
    ['leaves a fresh claim to the sweep', 0, false, 'running'],
  ] as const)('takes a describe that fails as not found on Stop: %s (#4131 review)', async (_name, minutesAgo, ended, after) => {
    const m = await durableManager()
    const failing: DescribeSession = async () => {
      throw new Error('describe failed')
    }
    m.durableTurns = new DurableSessionTurns({ client: env.client, sql: db.sql, events: m.events, taskQueue: QUEUE, describe: failing })
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await db.sql`UPDATE ai_sessions SET status = 'running', updated_at = now() - make_interval(mins => ${minutesAgo}) WHERE id = ${session.id}`
    expect(session.mode).toBe('durable')
    expect(await m.interrupt(session.id, browser)).toBe(ended)
    expect(await status(session.id)).toBe(after)
    // Past the grace it is ended as not found, which the failed describe stood for.
    const errors = (await m.events.read(session.id)).filter((e) => e.event.type === 'error').map((e) => e.event)
    expect(errors).toEqual(ended ? [expect.objectContaining({ code: 'turn_failed', message: expect.stringMatching(/could not be found/) })] : [])
  }, 60_000)

  it("hands off, then ends the old owner's parked calls with cancel_input, or the interrupt Signal when it goes unanswered", async () => {
    const m = await durableManager({ cancelTimeoutMs: 1_000 })
    const { session } = await m.start(agentA, { origin: 'mcp', prompt: 'hello' })
    const taken = await m.handoff(session.id, browser, browser)
    expect(taken.owner).toEqual(browser)
    expect((await recorded(session.id)).calls).toEqual(['cancel_input:the session was handed off to You'])

    const other = (await m.start(agentA, { origin: 'mcp', prompt: 'hello' })).session
    await env.client.workflow.getHandle(sessionWorkflowId(other.id)).signal('hold_cancel')
    const handing = m.handoff(other.id, browser, browser)
    let settled = false
    void handing.then(
      () => (settled = true),
      () => (settled = true),
    )
    // The owner change is committed before cancel_input is sent: while it waits (1 s,
    // unanswered), the row is the new owner's and the handoff holds no lock on it. NOWAIT
    // throws if locked; a probe may still meet the row's own turn appending an event for a
    // few ms, so only a lock held across every probe of the wait counts.
    await new Promise((r) => setTimeout(r, 300))
    let row: { owner_id: string } | undefined
    // Up to 10 probes, a refused one waiting 50 ms: the budget fits inside the 1 s cancel
    // wait (from 300 ms in), so a free row is found while the handoff still waits. Raise
    // either number and a probe can land after the wait, failing the check below.
    for (let probe = 0; probe < 10 && !row; probe++) {
      ;[row] = await db.sql
        .begin((tx) => tx<{ owner_id: string }[]>`SELECT owner_id FROM ai_sessions WHERE id = ${other.id} FOR UPDATE NOWAIT`)
        .catch(async (err: { code?: string }) => {
          if (err.code !== '55P03') throw err
          await new Promise((r) => setTimeout(r, 50))
          return []
        })
    }
    expect(row?.owner_id).toBe(browser.id)
    // Taken inside the wait: a probe that only got through once the handoff ended, as on
    // a slow runner, would pass even if the handoff held the lock through the wait.
    expect(settled, 'a probe got the row only after the handoff ended, not inside its wait').toBe(false)
    // A cancel that goes unanswered neither fails nor undoes the handoff.
    expect((await handing).owner).toEqual(browser)
    expect((await recorded(other.id)).calls).toEqual([
      'cancel_input:the session was handed off to You',
      'interrupt:the session was handed off to You',
    ])
  }, 60_000)

  it('sends nothing to the workflow when the owner changed meanwhile', async () => {
    const m = await durableManager()
    const { session } = await m.start(agentA, { origin: 'mcp', prompt: 'hello' })
    const get = m.get.bind(m)
    m.get = async (...args: Parameters<SessionManager['get']>) => {
      const read = await get(...args)
      // Another handoff applies between the read and this one's UPDATE.
      await db.sql`UPDATE ai_sessions SET owner_kind = 'bearer', owner_id = 'token:b' WHERE id = ${session.id}`
      return read
    }
    await expect(m.handoff(session.id, browser, browser)).rejects.toMatchObject({ code: 'busy' })
    expect((await recorded(session.id)).calls).toEqual([])
  }, 60_000)

  it('hands off a durable session that never ran a turn (no workflow yet)', async () => {
    const m = await durableManager()
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    expect((await m.handoff(session.id, browser, browser)).owner).toEqual(browser)
  }, 60_000)

  // A durable parent's conversation as its workflow holds it (Claude Code transcript entries
  // as JSON text), and the replies its log names (`durable-<offset>`, session/events.py).
  const entry = (uuid: string, type: string, content: unknown) => ({ type, uuid, sessionId: 'claude-1', message: { role: type, content } })
  const conversation = [
    entry('u1', 'user', 'make a box'),
    entry('a1', 'assistant', [{ type: 'text', text: 'a box' }]),
    entry('u2', 'user', 'taller'),
    entry('a2', 'assistant', [{ type: 'text', text: 'a taller box' }]),
  ]
  const reply = (messageId: string, delta: string) => [
    { type: 'assistant.text.delta', messageId, delta },
    { type: 'assistant.text.done', messageId },
  ]

  /** A durable session with two finished turns: its workflow holds `conversation`, its log the replies. */
  async function twoTurns(m: SessionManager, options: { busy?: boolean; owed?: Record<string, unknown> } = {}) {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    await finishTurn(session.id, [...reply('durable-3', 'a box'), { type: 'session.status', status: 'idle' }])
    await turn!.done
    const second = await m.send(session.id, browser, 'taller')
    await finishTurn(session.id, [...reply('durable-7', 'a taller box'), { type: 'session.status', status: 'idle' }])
    await second.done
    await env.client.workflow.getHandle(sessionWorkflowId(session.id)).signal('set_conversation', {
      session_id: 'claude-1',
      checkpoint: 'a2',
      conversation: conversation.map((e) => JSON.stringify(e)),
      ...(options.busy ? { busy: true } : {}),
      ...options.owed,
    })
    return session
  }

  const conversationOf = (start: Record<string, unknown>) =>
    ((start.agent as { conversation: string[] }).conversation ?? []).map((t) => (JSON.parse(t) as { uuid: string }).uuid)

  it("forks a durable session into a durable one whose workflow starts with the parent's conversation, workspace and lineage", async () => {
    const m = await durableManager()
    const parent = await twoTurns(m)
    const files = new SessionFiles(db.sql)
    await files.write({ kind: 'session', sessionId: parent.id }, 'notes.txt', Buffer.from('hi'))
    const child = await m.fork(parent.id, browser)
    expect(child).toMatchObject({ mode: 'durable', parentId: parent.id, owner: browser, title: `${parent.title} (fork)` })
    expect(child.id).not.toBe(parent.id)
    // Spends from the parent's budget (#823).
    const [lineage] = await db.sql<{ budget_root_id: string }[]>`SELECT budget_root_id FROM ai_sessions WHERE id = ${child.id}`
    expect(lineage?.budget_root_id).toBe(parent.id)
    // Its workflow runs already, with the parent's conversation as its agent's state.
    const { start, messages } = await recorded(child.id)
    expect(messages).toEqual([])
    expect(start).toMatchObject({ session_id: child.id, owner: { kind: 'browser', id: 'browser' }, agent: { session_id: 'claude-1', checkpoint: 'a2' } })
    expect(conversationOf(start)).toEqual(['u1', 'a1', 'u2', 'a2'])
    // The panel's history, the workspace and the audit row, as a classic fork.
    const log = (await m.events.read(child.id)).map((e) => e.event)
    expect(log[0]).toMatchObject({ type: 'session.started', mode: 'durable' })
    expect(log.filter((e) => e.type === 'user.turn').map((e) => (e as { text: string }).text)).toEqual(['make a box', 'taller'])
    expect(log.filter((e) => e.type === 'assistant.text.delta').map((e) => (e as { delta: string }).delta)).toEqual(['a box', 'a taller box'])
    expect(log.at(-1)).toMatchObject({ type: 'session.status', status: 'idle' })
    expect((await files.list({ kind: 'session', sessionId: child.id })).map((f) => f.path)).toEqual(['notes.txt'])
    // Its first message goes to the workflow the fork started (update-with-start, USE_EXISTING).
    const turn = await m.send(child.id, browser, 'wider')
    expect(turn.turnId).toBeTruthy()
    const after = await recorded(child.id)
    expect(after.messages.map((msg) => (msg as { text: string }).text)).toEqual(['wider'])
    expect(conversationOf(after.start as Record<string, unknown>)).toEqual(['u1', 'a1', 'u2', 'a2'])
    // A fork of the fork reads the conversation the fork's workflow holds.
    await finishTurn(child.id, [{ type: 'session.status', status: 'idle' }])
    await turn.done
    const grandchild = await m.fork(child.id, browser)
    expect(conversationOf((await recorded(grandchild.id)).start as Record<string, unknown>)).toEqual(['u1', 'a1', 'u2', 'a2'])
  }, 60_000)

  it('forks a durable session up to a reply, and a running one at its last finished reply', async () => {
    const m = await durableManager()
    const parent = await twoTurns(m)
    const child = await m.fork(parent.id, browser, { upTo: 'durable-3' })
    expect(conversationOf((await recorded(child.id)).start as Record<string, unknown>)).toEqual(['u1', 'a1'])
    expect((await recorded(child.id)).start).toMatchObject({ agent: { checkpoint: 'a1' } })
    const log = (await m.events.read(child.id)).map((e) => e.event)
    expect(log.filter((e) => e.type === 'user.turn')).toHaveLength(1)
    await expect(m.fork(parent.id, browser, { upTo: 'durable-99' })).rejects.toMatchObject({ code: 'invalid' })

    const running = await twoTurns(m, { busy: true })
    const atLast = await m.fork(running.id, browser)
    expect((await recorded(atLast.id)).start).toMatchObject({ agent: { checkpoint: 'a2' } })
  }, 60_000)

  it('hands a fork at the checkpoint what the parent still owes there, and a fork at an earlier reply none of it (#4173)', async () => {
    const m = await durableManager()
    // A turn stopped at a paused call: its result waits for the agent's next segment.
    const outcome = { content: 'stopped', is_error: true, blocks: null }
    const owed = { pending: { toolu_1: outcome }, fork_next: true, recent_call_ids: ['toolu_0'] }
    const parent = await twoTurns(m, { owed })
    const child = await m.fork(parent.id, browser)
    expect((await recorded(child.id)).start).toMatchObject({ agent: { checkpoint: 'a2', ...owed } })
    const earlier = await m.fork(parent.id, browser, { upTo: 'durable-3' })
    expect((await recorded(earlier.id)).start).toMatchObject({
      agent: { checkpoint: 'a1', pending: {}, fork_next: false, recent_call_ids: ['toolu_0'] },
    })
  }, 60_000)

  it('forks a parent paused at a call whose result its workflow holds at the call, with that result and its history through the call (#4173)', async () => {
    const m = await durableManager()
    const parent = await twoTurns(m)
    // A third turn paused on list_models, got its result, streamed a reply and was stopped: its
    // last segment never committed, so the checkpoint is the paused call and the result is pending.
    const third = await m.send(parent.id, browser, 'paint it')
    await finishTurn(parent.id, [
      { type: 'tool.call', id: 'toolu_p', name: 'list_models', input: {}, risk: 'read' },
      { type: 'tool.result', id: 'toolu_p', ok: true, summary: 'done' },
      ...reply('durable-13', 'painting it now'),
      { type: 'session.status', status: 'idle' },
    ])
    await third.done
    const pending = { toolu_p: { content: [], is_error: false, blocks: null } }
    await env.client.workflow.getHandle(sessionWorkflowId(parent.id)).signal('set_conversation', {
      session_id: 'claude-1',
      checkpoint: 'a3',
      conversation: [
        ...conversation,
        entry('u3', 'user', 'paint it'),
        entry('a3', 'assistant', [{ type: 'tool_use', id: 'toolu_p', name: 'mcp__scadbuddy__list_models', input: {} }]),
      ].map((e) => JSON.stringify(e)),
      pending,
      fork_next: true,
      recent_call_ids: ['toolu_p'],
    })
    const child = await m.fork(parent.id, browser)
    const { start } = await recorded(child.id)
    expect(start).toMatchObject({ agent: { checkpoint: 'a3', pending, fork_next: true, recent_call_ids: ['toolu_p'] } })
    expect(conversationOf(start as Record<string, unknown>)).toEqual(['u1', 'a1', 'u2', 'a2', 'u3', 'a3'])
    // The child shows the call and its result, which its agent gets first; not the uncommitted reply.
    const log = (await m.events.read(child.id)).map((e) => e.event)
    expect(log.filter((e) => e.type === 'tool.result').map((e) => (e as { id: string }).id)).toEqual(['toolu_p'])
    expect(log.filter((e) => e.type === 'assistant.text.delta').map((e) => (e as { delta: string }).delta)).toEqual(['a box', 'a taller box'])
  }, 60_000)

  it('forks a paused parent before its call when the log has no event of the call, though the result is held', async () => {
    const m = await durableManager()
    const parent = await twoTurns(m)
    const third = await m.send(parent.id, browser, 'paint it')
    // The turn's reply reached the log, its call did not: nothing says where the committed part ends.
    await finishTurn(parent.id, [...reply('durable-13', 'painting it now'), { type: 'session.status', status: 'idle' }])
    await third.done
    await env.client.workflow.getHandle(sessionWorkflowId(parent.id)).signal('set_conversation', {
      session_id: 'claude-1',
      checkpoint: 'a3',
      conversation: [
        ...conversation,
        entry('u3', 'user', 'paint it'),
        entry('a3', 'assistant', [{ type: 'tool_use', id: 'toolu_p', name: 'mcp__scadbuddy__list_models', input: {} }]),
      ].map((e) => JSON.stringify(e)),
      pending: { toolu_p: { content: [], is_error: false, blocks: null } },
      fork_next: true,
    })
    const child = await m.fork(parent.id, browser)
    expect((await recorded(child.id)).start).toMatchObject({ agent: { checkpoint: 'a2', pending: {}, fork_next: false } })
    const log = (await m.events.read(child.id)).map((e) => e.event)
    expect(log.filter((e) => e.type === 'assistant.text.delta').map((e) => (e as { delta: string }).delta)).toEqual(['a box', 'a taller box'])
  }, 60_000)

  it("carries the parent's state to a fork up to the reply its checkpoint is (a stopped turn's fork_next)", async () => {
    const m = await durableManager()
    const parent = await twoTurns(m, { owed: { fork_next: true, recent_call_ids: ['toolu_0'] } })
    const child = await m.fork(parent.id, browser, { upTo: 'durable-7' })
    expect((await recorded(child.id)).start).toMatchObject({ agent: { checkpoint: 'a2', pending: {}, fork_next: true, recent_call_ids: ['toolu_0'] } })
  }, 60_000)

  it('reads a parent over several pages again when what it owes changed between them, and not for a key order', async () => {
    const m = await durableManager()
    const outcome = (content: string) => ({ content, is_error: false, blocks: null })
    // One entry a page. The owed state changes after the first read (a turn stopped meanwhile):
    // the read starts again and the child gets the later state, read the same at both ends.
    const changed = await twoTurns(m, {
      owed: { page_chars: 1, owed_reads: [{ fork_next: false }, { fork_next: true, recent_call_ids: ['toolu_9'] }] },
    })
    const child = await m.fork(changed.id, browser)
    expect((await recorded(child.id)).start).toMatchObject({ agent: { checkpoint: 'a2', fork_next: true, recent_call_ids: ['toolu_9'] } })
    // First page, re-read (changed), then both again: four, or the states went to other reads.
    // An unexpected count may be an evicted stand-in, not a regression: see its header.
    expect((await recorded(changed.id)).forkPointPage0Queries).toBe(4)
    // The same map in another key order is no change: no second read (which would answer the third state).
    const reordered = await twoTurns(m, {
      owed: {
        page_chars: 1,
        owed_reads: [
          { pending: { toolu_a: outcome('a'), toolu_b: outcome('b') } },
          // Reordered at both levels: the calls, and the keys of each result.
          {
            pending: {
              toolu_b: { blocks: null, is_error: false, content: 'b' },
              toolu_a: { is_error: false, blocks: null, content: 'a' },
            },
          },
          { fork_next: true },
        ],
      },
    })
    const same = await m.fork(reordered.id, browser)
    // The first read's state: both results, and not the third read's fork_next.
    expect((await recorded(same.id)).start).toMatchObject({
      agent: { fork_next: false, pending: { toolu_a: outcome('a'), toolu_b: outcome('b') } },
    })
    expect((await recorded(reordered.id)).forkPointPage0Queries).toBe(2)
  }, 60_000)

  it('says the log lacks the call when a paused parent with its result held has no reply before it', async () => {
    const m = await durableManager()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'list them' })
    await finishTurn(session.id, [{ type: 'session.status', status: 'idle' }])
    await turn!.done
    await env.client.workflow.getHandle(sessionWorkflowId(session.id)).signal('set_conversation', {
      session_id: 'claude-2',
      checkpoint: 'a1',
      conversation: [entry('u1', 'user', 'list them'), entry('a1', 'assistant', [{ type: 'tool_use', id: 'toolu_q', name: 'x', input: {} }])].map((e) =>
        JSON.stringify(e),
      ),
      pending: { toolu_q: { content: [], is_error: false, blocks: null } },
    })
    await expect(m.fork(session.id, browser)).rejects.toMatchObject({ code: 'no_transcript', message: expect.stringContaining('no record of that call') })
  }, 60_000)

  it('forks a parent paused at a call whose result its worker did not send before that call, with its history through the reply (#4173)', async () => {
    const m = await durableManager()
    const parent = await twoTurns(m)
    // A third turn paused on a call, then streamed a reply and was stopped: its last segment
    // never committed, so the workflow's checkpoint is the paused call, and the reply is only in the log.
    // Its worker (from before #4173's hand-off) sends no `pending`, so nothing answers the call.
    const third = await m.send(parent.id, browser, 'paint it')
    await finishTurn(parent.id, [...reply('durable-11', 'painting it now'), { type: 'session.status', status: 'idle' }])
    await third.done
    const paused = [
      ...conversation,
      entry('u3', 'user', 'paint it'),
      entry('a3', 'assistant', [{ type: 'tool_use', id: 'toolu_p', name: 'mcp__scadbuddy__list_models', input: {} }]),
    ]
    const setConversation = (entries: unknown[], checkpoint: string) =>
      env.client.workflow.getHandle(sessionWorkflowId(parent.id)).signal('set_conversation', {
        session_id: 'claude-1',
        checkpoint,
        conversation: entries.map((e) => JSON.stringify(e)),
      })
    await setConversation(paused, 'a3')
    const child = await m.fork(parent.id, browser)
    expect((await recorded(child.id)).start).toMatchObject({ agent: { checkpoint: 'a2' } })
    expect(conversationOf((await recorded(child.id)).start as Record<string, unknown>)).toEqual(['u1', 'a1', 'u2', 'a2'])
    // The child shows the conversation its agent has: not the stopped turn's uncommitted reply.
    const log = (await m.events.read(child.id)).map((e) => e.event)
    expect(log.filter((e) => e.type === 'assistant.text.delta').map((e) => (e as { delta: string }).delta)).toEqual(['a box', 'a taller box'])

    // Once the call is answered and committed, the whole conversation forks.
    await setConversation(
      [...paused, entry('r3', 'user', [{ type: 'tool_result', tool_use_id: 'toolu_p', content: '[]' }])],
      'r3',
    )
    const later = await m.fork(parent.id, browser)
    expect((await recorded(later.id)).start).toMatchObject({ agent: { checkpoint: 'r3' } })

    // A paused call with no finished reply before it has nothing to fork.
    const { session: fresh, turn } = await m.start(browser, { origin: 'chat', prompt: 'list them' })
    await finishTurn(fresh.id, [{ type: 'session.status', status: 'idle' }])
    await turn!.done
    await env.client.workflow.getHandle(sessionWorkflowId(fresh.id)).signal('set_conversation', {
      session_id: 'claude-2',
      checkpoint: 'a1',
      conversation: [entry('u1', 'user', 'list them'), entry('a1', 'assistant', [{ type: 'tool_use', id: 'toolu_q', name: 'x', input: {} }])].map((e) =>
        JSON.stringify(e),
      ),
    })
    // Its result is not held: the advice is to wait for the answer, not the missing-record one.
    const refused = await m.fork(fresh.id, browser).catch((e: unknown) => e)
    expect(refused).toMatchObject({ code: 'no_transcript', message: expect.stringContaining('once that call is answered') })
    expect(refused).toMatchObject({ message: expect.not.stringContaining('no record') })
  }, 60_000)

  it('refuses to fork a durable session with no conversation yet, and leaves no child', async () => {
    const m = await durableManager()
    // No turn: no workflow.
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await expect(m.fork(session.id, browser)).rejects.toMatchObject({ code: 'no_transcript' })
    // A workflow, but nothing committed.
    const { session: started } = await m.start(browser, { origin: 'chat', prompt: 'hello' })
    await expect(m.fork(started.id, browser)).rejects.toMatchObject({ code: 'no_transcript' })
    expect(await db.sql`SELECT id FROM ai_sessions WHERE parent_id IS NOT NULL`).toHaveLength(0)
  }, 60_000)

  it("deletes a durable fork whose workflow could not be started", async () => {
    const m = await durableManager()
    const parent = await twoTurns(m)
    // A queue no worker reads still starts; a client that cannot reach Temporal does not.
    const turns = m.durableTurns!
    vi.spyOn(turns, 'startFork').mockRejectedValueOnce(Object.assign(new Error('unreachable'), { code: 'unavailable' }))
    await expect(m.fork(parent.id, browser)).rejects.toThrow('unreachable')
    expect(await db.sql`SELECT id FROM ai_sessions WHERE parent_id = ${parent.id}`).toHaveLength(0)
  }, 60_000)

  it("refuses a send to a durable session when this service has no Temporal", async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    m.durableTurns = undefined
    await expect(m.send(session.id, browser, 'hello')).rejects.toMatchObject({ code: 'unavailable' })
    expect((await m.get(session.id, browser)).status).toBe('idle')
  }, 60_000)

  const sweepOf = (m: SessionManager, options: { graceMs?: number; describe?: DescribeSession; batch?: number } = {}) =>
    new DurableRunningSweep({
      sql: db.sql,
      events: m.events,
      describe: options.describe ?? durableDescriber(env.client),
      graceMs: options.graceMs ?? 0,
      ...(options.batch ? { batch: options.batch } : {}),
    })

  const status = async (id: string) =>
    (await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${id}`)[0]?.status

  it("resets a running session whose workflow was terminated mid-turn, as finish_turn would, and leaves a live one", async () => {
    const m = await durableManager()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hello' })
    const live = (await m.start(browser, { origin: 'chat', prompt: 'still going' })).session
    expect(await status(session.id)).toBe('running')
    await env.client.workflow.getHandle(sessionWorkflowId(session.id)).terminate('gone')

    expect(await sweepOf(m).sweep()).toEqual([session.id])
    expect(await status(session.id)).toBe('idle')
    expect((await m.events.read(session.id)).slice(-2).map((e) => e.event)).toMatchObject([
      { type: 'error', code: 'turn_failed', message: expect.stringMatching(/workflow has ended/) },
      { type: 'session.status', status: 'idle' },
    ])
    expect(await turn!.done).toMatchObject({ kind: 'failed' })
    // A running workflow's turn is its own to end.
    expect(await status(live.id)).toBe('running')
    // Nothing is written twice.
    expect(await sweepOf(m).sweep()).toEqual([])
  }, 60_000)

  it('resets a parked session whose workflow is gone, and leaves one whose workflow holds its turn (#4133)', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'hello' })
    const live = (await m.start(browser, { origin: 'chat', prompt: 'still going' })).session
    // Both parked on a call: one asking for input, one for an approval.
    await db.sql`UPDATE ai_sessions SET status = 'waiting_input' WHERE id = ${session.id}`
    await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${live.id}`
    await env.client.workflow.getHandle(sessionWorkflowId(session.id)).terminate('gone')

    expect(await sweepOf(m).sweep()).toEqual([session.id])
    expect(await status(session.id)).toBe('idle')
    expect((await m.events.read(session.id)).slice(-2).map((e) => e.event)).toMatchObject([
      { type: 'error', code: 'turn_failed', message: expect.stringMatching(/workflow has ended/) },
      { type: 'session.status', status: 'idle' },
    ])
    expect(await status(live.id)).toBe('waiting_approval')
  }, 60_000)

  it('resets a running session whose workflow was never started (not found), only past the grace period', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    // A first send whose update-with-start never reached Temporal keeps the claim (Ruling 16).
    await db.sql`UPDATE ai_sessions SET status = 'running', updated_at = now() WHERE id = ${session.id}`
    // Within the grace period a start may still be in flight: not described, not reset.
    const described: string[] = []
    const watch: DescribeSession = (id) => {
      described.push(id)
      return durableDescriber(env.client)(id)
    }
    expect(await sweepOf(m, { graceMs: 60_000, describe: watch }).sweep()).toEqual([])
    expect(described).toEqual([])
    expect(await status(session.id)).toBe('running')

    expect(await sweepOf(m, { describe: watch }).sweep()).toEqual([session.id])
    expect(described).toEqual([sessionWorkflowId(session.id)])
    expect(await status(session.id)).toBe('idle')
    expect((await m.events.read(session.id)).slice(-2).map((e) => e.event)).toMatchObject([
      { type: 'error', code: 'turn_failed', message: expect.stringMatching(/never started/) },
      { type: 'session.status', status: 'idle' },
    ])
    // The next send starts the workflow afresh.
    await m.send(session.id, browser, 'hello again')
    expect((await recorded(session.id)).messages.map((x) => x.text)).toEqual(['hello again'])
  }, 60_000)

  it('gives back a kept claim whose workflow is open between turns, and leaves one whose workflow holds the turn (#2078)', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'hello' })
    await finishTurn(session.id, [
      { type: 'session.result', costUsd: 0.1, turns: 1, budgetUsd: 5 },
      { type: 'session.status', status: 'idle' },
    ])
    const live = (await m.start(browser, { origin: 'chat', prompt: 'still going' })).session
    // A kept claim (Ruling 16) whose Update the open workflow never took: it holds no turn.
    await db.sql`UPDATE ai_sessions SET status = 'running' WHERE id = ${session.id}`

    expect(await sweepOf(m).sweep()).toEqual([session.id])
    expect(await status(session.id)).toBe('idle')
    expect((await m.events.read(session.id)).slice(-2).map((e) => e.event)).toMatchObject([
      { type: 'error', code: 'turn_failed', message: expect.stringMatching(/never reached/) },
      { type: 'session.status', status: 'idle' },
    ])
    // `live` has a workflow of its own that holds its turn: that turn's end is its own.
    expect(await status(live.id)).toBe('running')
    // The next send reaches the same workflow.
    await m.send(session.id, browser, 'hello again')
    expect((await recorded(session.id)).messages.map((x) => x.text)).toEqual(['hello', 'hello again'])
  }, 60_000)

  it('refuses `closed`, and never starts an empty workflow, for a session whose workflow ran turns and was removed (#2078)', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    // What the row keeps of a workflow that ran a turn (follow_session moved the offset
    // past its output), then ended and was removed by namespace retention: Temporal no
    // longer knows it. The dev server deletes too slowly to wait for here.
    await db.sql`UPDATE ai_sessions SET durable_offset = 3 WHERE id = ${session.id}`
    const workflowId = sessionWorkflowId(session.id)
    const describe = durableDescriber(env.client)
    expect(await describe(workflowId)).toBe('not_found')

    await expect(m.send(session.id, browser, 'hello again')).rejects.toMatchObject({
      code: 'closed',
      message: expect.stringMatching(/has ended; continue in a new chat/),
    })
    expect(await describe(workflowId)).toBe('not_found')
    expect(await status(session.id)).toBe('idle')

    // A kept claim on it: the sweep says the workflow is gone, not that it never started.
    await db.sql`UPDATE ai_sessions SET status = 'running' WHERE id = ${session.id}`
    expect(await sweepOf(m).sweep()).toEqual([session.id])
    expect((await m.events.read(session.id)).slice(-2).map((e) => e.event)).toMatchObject([
      { type: 'error', code: 'turn_failed', message: expect.stringMatching(/workflow is gone.*new chat/) },
      { type: 'session.status', status: 'idle' },
    ])
    await expect(m.send(session.id, browser, 'and again')).rejects.toMatchObject({ code: 'closed' })
    expect(await describe(workflowId)).toBe('not_found')
  }, 60_000)

  it('refuses `closed`, as ended, a session whose workflow ran turns and ended but is still retained (#2078)', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'hello' })
    await finishTurn(session.id, [
      { type: 'session.result', costUsd: 0.1, turns: 1, budgetUsd: 5 },
      { type: 'session.status', status: 'idle' },
    ])
    await db.sql`UPDATE ai_sessions SET durable_offset = 3 WHERE id = ${session.id}`
    await env.client.workflow.getHandle(sessionWorkflowId(session.id)).terminate('ended')
    // A plain Update to an ended workflow is NOT_FOUND too: the message must not say "removed".
    const refused = await m.send(session.id, browser, 'hello again').catch((err: unknown) => err)
    expect(refused).toMatchObject({ code: 'closed', message: expect.stringMatching(/has ended; continue in a new chat/) })
    expect((refused as Error).message).not.toMatch(/removed/)
  }, 60_000)

  it("leaves a turn that was claimed again while its workflow was described", async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await db.sql`UPDATE ai_sessions SET status = 'running' WHERE id = ${session.id}`
    // While the sweep waits on Temporal, the old claim ends and a new turn claims the row.
    const racing: DescribeSession = async () => {
      await db.sql`UPDATE ai_sessions SET status = 'idle' WHERE id = ${session.id}`
      await m.send(session.id, browser, 'a new turn')
      return 'closed'
    }
    expect(await sweepOf(m, { describe: racing }).sweep()).toEqual([])
    expect(await status(session.id)).toBe('running')
    expect((await m.events.read(session.id)).at(-1)?.event).toMatchObject({ type: 'session.status', status: 'running' })
  }, 60_000)

  it('pages past rows it cannot resolve, so they never starve the rest, and a hung describe does not stall the pass', async () => {
    const m = await durableManager()
    const ids: string[] = []
    for (let i = 0; i < 4; i++) ids.push((await m.start(browser, { origin: 'chat', title: `t${i}` })).session.id)
    await db.sql`UPDATE ai_sessions SET status = 'running' WHERE id = ANY(${ids})`
    const sorted = [...ids].sort()
    // The first two (in the order a page reads them) are long, live turns; the third
    // never answers; only the last can be resolved.
    const described: string[] = []
    const describe: DescribeSession = (workflowId) => {
      described.push(workflowId)
      if (workflowId === sessionWorkflowId(sorted[2]!)) return new Promise(() => {})
      return Promise.resolve(workflowId === sessionWorkflowId(sorted[3]!) ? 'not_found' : 'open')
    }
    const sweep = new DurableRunningSweep({ sql: db.sql, events: m.events, describe, graceMs: 0, batch: 2, describeTimeoutMs: 200 })
    expect(await sweep.sweep()).toEqual([])
    expect(await sweep.sweep()).toEqual([sorted[3]])
    expect(described).toEqual(sorted.map((id) => sessionWorkflowId(id)))
    // Past the end, the next pass starts over.
    await sweep.sweep()
    expect(described.slice(4)).toEqual(sorted.slice(0, 2).map((id) => sessionWorkflowId(id)))
  }, 60_000)

  it('refuses a new session `unavailable` while no session worker polls, and inserts nothing (plan Step 6.3)', async () => {
    const m = await durableManager()
    m.durableTurns = new DurableTurns({
      client: env.client,
      sql: db.sql,
      events: m.events,
      sessionQueue: `nobody-${randomUUID()}`,
    })
    await expect(m.start(browser, { origin: 'chat', title: 't' })).rejects.toMatchObject({
      code: 'unavailable',
      message: expect.stringMatching(/no session workflow worker/),
    })
    expect(await db.sql`SELECT id FROM ai_sessions`).toHaveLength(0)
  }, 60_000)
})

describe("a durable handoff whose workflow cannot be reached", () => {
  it('warns, with no payload, when even the interrupt Signal is not sent', async () => {
    const down = () => Promise.reject(new Error('connection refused'))
    const client = {
      withDeadline: (_deadline: number, fn: () => Promise<unknown>) => fn(),
      workflow: { getHandle: () => ({ executeUpdate: down, signal: down }) },
    } as unknown as Client
    // handoff reads the row's runtime first: no row, so a classic durable session.
    const turns = new DurableTurns({ client, sql: (() => Promise.resolve([])) as unknown as Sql, events: {} as EventLog })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      await turns.handoff({ id: 'f0e1d2c3-0000-4000-8000-000000000000' } as SessionRecord, 'the session was handed off to You', { kind: 'browser', id: 'browser', label: 'You' })
      expect(warn).toHaveBeenCalledTimes(1)
      const [line] = warn.mock.calls[0] as [string]
      expect(line).toContain('f0e1d2c3-0000-4000-8000-000000000000')
      expect(line).not.toContain('handed off')
    } finally {
      warn.mockRestore()
    }
  })
})

describe('a durable fork whose workflow cannot be read (#3739)', () => {
  const id = 'a1b2c3d4-0000-4000-8000-000000000000'
  function turnsFailing(err: Error): DurableTurns {
    const client = {
      withDeadline: (_deadline: number, fn: () => Promise<unknown>) => fn(),
      workflow: { getHandle: () => ({ query: () => Promise.reject(err) }) },
    } as unknown as Client
    // forkSource reads the row's runtime first: no row, so a classic durable session.
    const sql = (() => Promise.resolve([])) as unknown as Sql
    return new DurableTurns({ client, sql, events: {} as EventLog })
  }

  it('logs why, and says when no worker answered in time', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      // A gRPC DEADLINE_EXCEEDED (code 4), as the client raises it.
      const deadline = Object.assign(new Error('4 DEADLINE_EXCEEDED: query timed out'), { code: 4, details: 'query timed out', metadata: {} })
      await expect(turnsFailing(deadline).forkSource(id)).rejects.toMatchObject({
        code: 'unavailable',
        message: expect.stringContaining('no durable session worker answered'),
      })
      await expect(turnsFailing(new Error('nondeterminism in replay')).forkSource(id)).rejects.toMatchObject({
        code: 'unavailable',
        message: expect.stringContaining('could not be read to fork it'),
      })
      const lines = warn.mock.calls.map(([line]) => String(line))
      expect(lines).toHaveLength(2)
      expect(lines[0]).toContain(id)
      expect(lines[0]).toContain('DEADLINE_EXCEEDED')
      expect(lines[1]).toContain('nondeterminism in replay')
    } finally {
      warn.mockRestore()
    }
  })
})
