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
import { type DescribeSession, DurableRunningSweep, durableDescriber } from '../src/sessions/durableSweep.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import type { UserImage } from '../src/sessions/images.js'
import { type SessionManager, type SessionRecord, SETTING_SESSION_MODE } from '../src/sessions/manager.js'
import type { ServerEvent } from '../src/sessions/protocol.js'
import { frontendClientMessages } from './support/frontendProtocol.js'
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

  it('is durable by default and with any value but classic, so it falls back to classic without Temporal', async () => {
    for (const value of [undefined, 'durable', 'nonsense']) {
      const m = manager({ sql: db.sql, paths: await tempPaths(), settings: mode(value) })
      const { session, modeFallback } = await m.start(browser, { origin: 'chat', title: 't' })
      expect(session.mode).toBe('classic')
      expect(modeFallback).toMatch(/durable sessions need Temporal/)
      // Said where the panel reads it, so the user sees which mode ran.
      const [started] = await m.events.read(session.id, 0)
      expect(started?.event).toMatchObject({ type: 'session.started', mode: 'classic', modeFallback })
    }
  })

  it('is classic with no fallback when the setting says classic', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), settings: mode('classic') })
    const { session, modeFallback } = await m.start(browser, { origin: 'chat', title: 't' })
    expect(session.mode).toBe('classic')
    expect(modeFallback).toBeUndefined()
    const [started] = await m.events.read(session.id, 0)
    expect(started?.event).toMatchObject({ type: 'session.started', mode: 'classic' })
    expect(started?.event).not.toHaveProperty('modeFallback')
  })

  it('refuses a durable start the caller asked for without Temporal or the key, and inserts nothing', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), settings: mode('durable') })
    await expect(m.start(browser, { origin: 'chat', title: 't', mode: 'durable' })).rejects.toMatchObject({
      code: 'unavailable',
      status: 503,
    })
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

  it("hands off, then ends the old owner's parked calls with cancel_input, or the interrupt Signal when it goes unanswered", async () => {
    const m = await durableManager({ cancelTimeoutMs: 1_000 })
    const { session } = await m.start(agentA, { origin: 'mcp', prompt: 'hello' })
    const taken = await m.handoff(session.id, browser, browser)
    expect(taken.owner).toEqual(browser)
    expect((await recorded(session.id)).calls).toEqual(['cancel_input:the session was handed off to You'])

    const other = (await m.start(agentA, { origin: 'mcp', prompt: 'hello' })).session
    await env.client.workflow.getHandle(sessionWorkflowId(other.id)).signal('hold_cancel')
    const handing = m.handoff(other.id, browser, browser)
    // The owner change is committed before cancel_input is sent: while it waits, the
    // row is the new owner's and holds no lock (NOWAIT throws if locked).
    await new Promise((r) => setTimeout(r, 300))
    const [row] = await db.sql.begin((tx) => tx`SELECT owner_id FROM ai_sessions WHERE id = ${other.id} FOR UPDATE NOWAIT`)
    expect(row?.owner_id).toBe(browser.id)
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

  it('falls back from the default, and refuses an asked-for durable start, while no durable worker polls', async () => {
    const m = await durableManager()
    m.durableTurns = new DurableTurns({ client: env.client, sql: db.sql, events: m.events, taskQueue: `nobody-${randomUUID()}` })
    const { session, modeFallback } = await m.start(browser, { origin: 'chat', title: 't' })
    expect(session.mode).toBe('classic')
    expect(modeFallback).toMatch(/no durable session worker/)
    await expect(m.start(browser, { origin: 'chat', title: 't', mode: 'durable' })).rejects.toMatchObject({
      code: 'unavailable',
    })
    // With the stand-in polling its queue, the default is durable.
    const durable = await durableManager()
    expect((await durable.start(browser, { origin: 'chat', title: 't' })).session.mode).toBe('durable')
  }, 60_000)
})

describe("a durable handoff whose workflow cannot be reached", () => {
  it('warns, with no payload, when even the interrupt Signal is not sent', async () => {
    const down = () => Promise.reject(new Error('connection refused'))
    const client = {
      withDeadline: (_deadline: number, fn: () => Promise<unknown>) => fn(),
      workflow: { getHandle: () => ({ executeUpdate: down, signal: down }) },
    } as unknown as Client
    const turns = new DurableTurns({ client, sql: {} as Sql, events: {} as EventLog })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      await turns.handoff({ id: 'f0e1d2c3-0000-4000-8000-000000000000' } as SessionRecord, 'the session was handed off to You')
      expect(warn).toHaveBeenCalledTimes(1)
      const [line] = warn.mock.calls[0] as [string]
      expect(line).toContain('f0e1d2c3-0000-4000-8000-000000000000')
      expect(line).not.toContain('handed off')
    } finally {
      warn.mockRestore()
    }
  })
})
