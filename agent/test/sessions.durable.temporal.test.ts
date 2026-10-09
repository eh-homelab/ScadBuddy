import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { createHash, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { sessionWorkflowId } from '../src/gate/durable.js'
import { UNTRUSTED_CONTENT_POLICY } from '../src/safety/untrusted.js'
import { DurableTurns } from '../src/sessions/durable.js'
import type { UserImage } from '../src/sessions/images.js'
import { type SessionManager, SETTING_SESSION_MODE } from '../src/sessions/manager.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// The agent service's dispatch of a durable session (plan 5c PR 3, Rulings 7, 8, 13):
// the mode set at insert, a send as claim + blobs + `user.turn` + update-with-start of
// `send_message`, `done` read from the event log, interrupt and handoff through the
// gate, fork refused. Against a stand-in DurableSession (support/durableSessionWorkflows.ts);
// test/durable.e2e.test.ts runs the real one.

const PG_SKIP = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`
const QUEUE = 'durable-standin'
const WORKFLOWS = fileURLToPath(new URL('./support/durableSessionWorkflows.ts', import.meta.url))

const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
const IMAGE: UserImage = { mediaType: 'image/png', data: PNG, preview: { mediaType: 'image/png', data: PNG } }
const PNG_NAME = `${createHash('sha256').update(Buffer.from(PNG, 'base64')).digest('hex')}.png`

type Recorded = { start: Record<string, unknown>; messages: Record<string, unknown>[]; calls: string[] }

const mode = (value: string | undefined) => ({
  get: <T>(key: string) => Promise.resolve((key === SETTING_SESSION_MODE ? value : undefined) as T),
})

describe.skipIf(!TEST_DATABASE_URL)(`a session's mode at insert${PG_SKIP}`, () => {
  let db: Database
  let drop: () => Promise<void>
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it('is classic by default and with any other value', async () => {
    for (const value of [undefined, 'classic', 'nonsense']) {
      const m = manager({ sql: db.sql, paths: await tempPaths(), settings: mode(value) })
      const { session } = await m.start(browser, { origin: 'chat', title: 't' })
      expect(session.mode).toBe('classic')
    }
  })

  it('refuses a durable start without Temporal or the key, and inserts nothing', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), settings: mode('durable') })
    await expect(m.start(browser, { origin: 'chat', title: 't' })).rejects.toMatchObject({ code: 'unavailable', status: 503 })
    await expect(m.start(browser, { origin: 'chat', title: 't', mode: 'durable' })).rejects.toMatchObject({ code: 'unavailable' })
    expect(await db.sql`SELECT id FROM ai_sessions`).toHaveLength(0)
  })

  it('lets StartOptions.mode pick classic over the setting', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), settings: mode('durable') })
    const { session } = await m.start(browser, { origin: 'chat', title: 't', mode: 'classic' })
    expect(session.mode).toBe('classic')
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
    worker = await Worker.create({ connection: env.nativeConnection, taskQueue: QUEUE, workflowsPath: WORKFLOWS })
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
    m.durableTurns = new DurableTurns({
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
      },
    ])
    // The blob the worker reads, by the name the message carries (Ruling 5).
    const [blob] = await db.sql<{ name: string }[]>`SELECT name FROM ai_session_blobs WHERE session_id = ${session.id}`
    expect(blob?.name).toBe(PNG_NAME)
    const log = await m.events.read(session.id)
    const userTurn = log.find((e) => e.event.type === 'user.turn')?.event
    // The page context goes to the model, not the transcript.
    expect(userTurn).toMatchObject({ text: 'make me a bracket', turnId: turn!.turnId, author: browser })
    expect(log.at(-1)?.event).toMatchObject({ type: 'session.status', status: 'running' })
    const [row] = await db.sql<{ status: string; turn_id: string | null }[]>`SELECT status, turn_id FROM ai_sessions WHERE id = ${session.id}`
    // Durable turns take no turn_id/lease claim (Ruling 7).
    expect(row).toEqual({ status: 'running', turn_id: null })
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
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
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

  it("hands off after cancel_input, and refuses (retryable) while it goes unanswered", async () => {
    const m = await durableManager({ cancelTimeoutMs: 1_000 })
    const { session } = await m.start(agentA, { origin: 'mcp', prompt: 'hello' })
    const taken = await m.handoff(session.id, browser, browser)
    expect(taken.owner).toEqual(browser)
    expect((await recorded(session.id)).calls).toEqual(['cancel_input:the session was handed off to You'])

    const other = (await m.start(agentA, { origin: 'mcp', prompt: 'hello' })).session
    await env.client.workflow.getHandle(sessionWorkflowId(other.id)).signal('hold_cancel')
    await expect(m.handoff(other.id, browser, browser)).rejects.toMatchObject({ code: 'unavailable', status: 503 })
    expect((await m.get(other.id, browser)).owner).toEqual(agentA)
  }, 60_000)

  it('sends no cancel_input when the owner changed meanwhile', async () => {
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

  it('refuses to fork a durable session', async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', prompt: 'hello' })
    await expect(m.fork(session.id, browser)).rejects.toMatchObject({ code: 'invalid' })
  }, 60_000)

  it("refuses a send to a durable session when this service has no Temporal", async () => {
    const m = await durableManager()
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    m.durableTurns = undefined
    await expect(m.send(session.id, browser, 'hello')).rejects.toMatchObject({ code: 'unavailable' })
    expect((await m.get(session.id, browser)).status).toBe('idle')
  }, 60_000)
})
