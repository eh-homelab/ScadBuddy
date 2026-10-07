import { randomBytes } from 'node:crypto'
import { WorkflowUpdateFailedError, WorkflowUpdateRPCTimeoutOrCancelledError } from '@temporalio/client'
import { ApplicationFailure } from '@temporalio/common'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SETTING_APPROVAL_EXPIRY_SECONDS } from '../src/approvals/service.js'
import type { Database } from '../src/db.js'
import {
  DURABLE_ABANDONED,
  DURABLE_BUSY,
  DURABLE_STOPPING,
  DURABLE_UNKNOWN_INPUT,
  DurableRefused,
  TemporalDurableSessions,
} from '../src/durable/client.js'
import { ChatConnection } from '../src/routes/chat.js'
import { kekFromBase64 } from '../src/secrets.js'
import {
  DURABLE_NEEDS_TEMPORAL,
  DURABLE_QUEUED,
  DURABLE_WAITING,
  DURABLE_WAITING_CODE,
  NOT_DELIVERED_CODE,
  notDelivered,
  RESUMED_FRESH,
  STOPPED_BEFORE_IT_RAN,
  SETTING_MODEL,
  SETTING_SESSION_MODE,
  type SessionManager,
  type SessionManagerDeps,
} from '../src/sessions/manager.js'
import { PgPayloadKeys } from '../src/temporal/payloadKeys.js'
import { FakeDurable, type FakeUpdate, fakeTemporalClient } from './support/fakeDurable.js'
import { frontendClientMessages } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// ChatConnection against a real SessionManager and event log, for what the
// socket-level tests cannot time: a socket that closes while the connection
// is still waiting on the database must leave no follower behind.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

/** Resolves when released; the stand-in for a slow database call. */
function gate(): { wait: Promise<void>; release: () => void } {
  let release = () => {}
  const wait = new Promise<void>((resolve) => {
    release = resolve
  })
  return { wait, release }
}

const settle = () => new Promise((r) => setTimeout(r, 150))

/** expect.poll's default 1 s is too short on a loaded host (#1056): every poll here takes this. */
const POLL = { timeout: 15_000 }

describe.skipIf(skip !== undefined)(`ChatConnection${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, pollMs: 20 })
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  it('a socket closed while a new chat is being started leaves no follower', async () => {
    const slow = gate()
    const start = m.start.bind(m)
    m.start = async (...args) => {
      const started = await start(...args)
      await slow.wait
      return started
    }
    const out: unknown[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    const handled = connection.receive(JSON.stringify(clientMessage({ type: 'user.message', text: 'hi', context: { route: '/' } })))
    await settle()
    connection.close()
    slow.release()
    await handled
    await settle()
    expect(connection.following()).toEqual([])
    expect(m.events.watchedSessions()).toBe(0)
    // Nothing reached the closed socket after the snapshot.
    expect(out).toHaveLength(1)
  })

  it('a socket closed while an attach is checking the session leaves no follower', async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    const slow = gate()
    const get = m.get.bind(m)
    m.get = async (...args) => {
      const found = await get(...args)
      await slow.wait
      return found
    }
    const connection = new ChatConnection(m, () => {})
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    const handled = connection.receive(JSON.stringify(clientMessage({ type: 'session.attach', sessionId: session.id })))
    await settle()
    connection.close()
    slow.release()
    await handled
    await settle()
    expect(connection.following()).toEqual([])
    expect(m.events.watchedSessions()).toBe(0)
  })

  it('an open connection follows, and close() stops the follower', async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    const connection = new ChatConnection(m, () => {})
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(JSON.stringify(clientMessage({ type: 'session.attach', sessionId: session.id })))
    await settle()
    expect(connection.following()).toEqual([session.id])
    expect(m.events.watchedSessions()).toBe(1)
    connection.close()
    await settle()
    expect(m.events.watchedSessions()).toBe(0)
  })

  it('refuses a mode on a message to an existing session, and sends nothing (#1056)', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    const out: { type: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    await connection.receive(
      JSON.stringify({ v: 1, type: 'user.message', sessionId: session.id, mode: 'durable', text: 'more', context: { route: '/' } }),
    )
    expect(out.filter((e) => e.type === 'error')).toEqual([
      {
        v: 1,
        type: 'error',
        sessionId: session.id,
        code: 'invalid',
        message: 'mode is chosen when a session starts and cannot change',
      },
    ])
    expect((await m.get(session.id, browser)).turns).toBe(0)
    expect((await m.events.read(session.id)).map((e) => e.event.type)).not.toContain('user.turn')
    connection.close()
  })

  it('starts a new chat in the mode its first message chooses (#1056)', async () => {
    const out: { type: string; message?: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    await connection.receive(JSON.stringify({ v: 1, type: 'user.message', mode: 'classic', text: 'hi', context: { route: '/' } }))
    const [session] = await m.list(browser)
    expect(session?.mode).toBe('classic')
    // No payload keys here, so a durable start is refused by the manager: the mode reached it.
    await connection.receive(JSON.stringify({ v: 1, type: 'user.message', mode: 'durable', text: 'hi', context: { route: '/' } }))
    expect(out.filter((e) => e.type === 'error')).toEqual([
      expect.objectContaining({ code: 'invalid', message: 'durable sessions need SCADBUDDY_SECRET_KEY_FILE' }),
    ])
    connection.close()
  })

  it('answers a send to a spent session with its numbers, then the refusal (#790)', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    await db.sql`UPDATE ai_sessions SET cost_usd = 1.0160000001 WHERE id = ${session.id}`
    const out: { type: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(
      JSON.stringify(clientMessage({ type: 'user.message', sessionId: session.id, text: 'more', context: { route: '/' } })),
    )
    const answered = out.filter((e) => e.type === 'session.budget' || e.type === 'error')
    expect(answered).toEqual([
      { v: 1, type: 'session.budget', sessionId: session.id, costUsd: 1.0160000001, budgetUsd: 1 },
      expect.objectContaining({ type: 'error', sessionId: session.id, code: 'budget_exhausted', message: expect.stringContaining('($1.02 of $1.00)') }),
    ])
    connection.close()
  })
})

// A durable session's sends, Stop and resumes in the manager (#1056, plan task 12), with
// DurableSessions faked, or TemporalDurableSessions over a fake Client where its choice of
// state (a Stop's hand-over, a snapshot, nothing) and the offset reset need Postgres.
describe.skipIf(skip !== undefined)(`durable sessions in the manager${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let values: Map<string, unknown>
  let classicRuns: number

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    values = new Map()
    classicRuns = 0
  })
  afterEach(async () => {
    await drop()
  })

  async function durableManager(durable: SessionManagerDeps['durable'], deps: Partial<SessionManagerDeps> = {}) {
    const { runner } = scriptedRunner(() => {
      classicRuns += 1
      return { reply: 'classic' }
    })
    return manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: runner,
      pollMs: 20,
      settings: { get: <T>(key: string) => Promise.resolve(values.get(key) as T) },
      payloadKeys: new PgPayloadKeys(db.sql, { current: kekFromBase64(randomBytes(32).toString('base64')) }),
      ...(durable ? { durable } : {}),
      ...deps,
    })
  }

  const types = async (m: SessionManager, id: string) => (await m.events.read(id)).map((e) => e.event.type)
  const status = async (id: string) => (await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${id}`)[0]?.status

  const inputs = async (id: string) =>
    db.sql<{ id: string; text: string; context: string | null; note: string | null; status: string }[]>`
      SELECT id, text, context, note, status FROM ai_durable_inputs WHERE session_id = ${id} ORDER BY seq`
  const errors = async (m: SessionManager, id: string) =>
    (await m.events.read(id)).map((e) => e.event).filter((e) => e.type === 'error')
  const unavailable = () => Object.assign(new Error('14 UNAVAILABLE: Connection dropped'), { code: 14 })

  it('commits the message before Temporal, sends its id with the session\'s limits, and logs the turn', async () => {
    const durable = new FakeDurable()
    const m = await durableManager(durable)
    values.set(SETTING_APPROVAL_EXPIRY_SECONDS, 120)
    values.set(SETTING_MODEL, 'claude-test-model')
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    let committed: unknown[] = []
    const send = durable.send.bind(durable)
    durable.send = async (...args) => {
      committed = await inputs(session.id)
      return send(...args)
    }
    const turn = await m.send(session.id, browser, ' hello ', { context: 'route: /' })
    expect(durable.sends).toEqual([
      {
        input: { session_id: session.id, max_turns: session.maxTurns, approval_expiry_seconds: 120, model: 'claude-test-model' },
        messageId: turn.turnId,
      },
    ])
    // Committed before the Update was sent: the workflow loads it from here.
    expect(committed).toEqual([{ id: turn.turnId, text: 'hello', context: 'route: /', note: null, status: 'pending' }])
    const log = (await m.events.read(session.id)).map((e) => e.event)
    expect(log.slice(-2)).toEqual([
      { v: 1, type: 'user.turn', sessionId: session.id, turnId: turn.turnId, text: 'hello', author: browser },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'running' },
    ])
    expect(await status(session.id)).toBe('running')
    expect(classicRuns).toBe(0)

    // The projector's events end the turn: its outcome comes from the row.
    await db.sql`UPDATE ai_sessions SET cost_usd = 0.25, turns = 2 WHERE id = ${session.id}`
    await m.events.append(session.id, [
      { v: 1, type: 'session.result', sessionId: session.id, costUsd: 0.25, turns: 2 },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
    expect(await turn.done).toEqual({ kind: 'result', subtype: 'success', costUsd: 0.25, turns: 2 })
  })

  it('a durable start with a prompt takes the durable path', async () => {
    const durable = new FakeDurable()
    const m = await durableManager(durable)
    const { session, turn } = await m.start(browser, { origin: 'chat', mode: 'durable', prompt: 'first', context: 'ctx' })
    expect(turn).toBeDefined()
    expect(durable.sends.map((s) => s.messageId)).toEqual([turn!.turnId])
    expect((await inputs(session.id)).map((i) => [i.text, i.context])).toEqual([['first', 'ctx']])
    expect(session.status).toBe('running')
    expect(classicRuns).toBe(0)
  })

  it('refuses a send while the session runs or waits, without asking Temporal or committing it', async () => {
    const durable = new FakeDurable()
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    await m.send(session.id, browser, 'one')
    await expect(m.send(session.id, browser, 'two')).rejects.toMatchObject({ code: 'busy' })
    await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${session.id}`
    await expect(m.send(session.id, browser, 'three')).rejects.toMatchObject({ code: 'busy' })
    await expect(m.send(session.id, agentA, 'mine')).rejects.toMatchObject({ code: 'not_found' })
    expect(durable.sends).toHaveLength(1)
    expect((await inputs(session.id)).map((i) => i.text)).toEqual(['one'])
  })

  it.each([DURABLE_STOPPING, DURABLE_ABANDONED])(
    'a message a Stop refused (%s) is abandoned, and the log says it was not delivered: the turn stays in the transcript',
    async (refusal) => {
      const durable = new FakeDurable()
      durable.sendError = new DurableRefused(refusal)
      const m = await durableManager(durable)
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      const turn = await m.send(session.id, browser, 'hi')
      expect(await status(session.id)).toBe('idle')
      expect(await inputs(session.id)).toMatchObject([{ id: turn.turnId, status: 'abandoned' }])
      const log = (await m.events.read(session.id)).map((e) => e.event)
      expect(log.slice(-4)).toEqual([
        { v: 1, type: 'user.turn', sessionId: session.id, turnId: turn.turnId, text: 'hi', author: browser },
        { v: 1, type: 'session.status', sessionId: session.id, status: 'running' },
        { v: 1, type: 'error', sessionId: session.id, code: NOT_DELIVERED_CODE, message: notDelivered(refusal) },
        { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
      ])
      expect(await turn.done).toEqual({ kind: 'failed', message: notDelivered(refusal) })
    },
  )

  it.each([DURABLE_BUSY, DURABLE_UNKNOWN_INPUT])(
    'a message refused for now (%s) stays queued: never abandoned, and the user is told it will run',
    async (refusal) => {
      // Lead ruling: only a Stop or a forget gives a message up; the run it is queued in,
      // or the next one, starts it.
      const durable = new FakeDurable()
      durable.sendError = new DurableRefused(refusal)
      const m = await durableManager(durable)
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      const turn = await m.send(session.id, browser, 'hi')
      await expect.poll(async () => (await errors(m, session.id)).length, POLL).toBe(1)
      expect(await status(session.id)).toBe('running')
      expect(await inputs(session.id)).toMatchObject([{ id: turn.turnId, status: 'pending' }])
      expect(await errors(m, session.id)).toEqual([
        { v: 1, type: 'error', sessionId: session.id, code: DURABLE_WAITING_CODE, message: DURABLE_QUEUED },
      ])
    },
  )

  it('shows a send refused on the socket once', async () => {
    const durable = new FakeDurable()
    durable.sendError = new DurableRefused(DURABLE_STOPPING)
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const out: { type: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(
      JSON.stringify(clientMessage({ type: 'user.message', sessionId: session.id, text: 'hi', context: { route: '/' } })),
    )
    await expect
      .poll(() => out.some((e) => e.type === 'session.status' && (e as { status?: string }).status === 'idle'), POLL)
      .toBe(true)
    expect(out.filter((e) => e.type === 'error')).toHaveLength(1)
    connection.close()
  })

  it.each([
    ['UNAVAILABLE', unavailable],
    ['DEADLINE_EXCEEDED', () => Object.assign(new Error('4 DEADLINE_EXCEEDED: Deadline exceeded'), { code: 4 })],
    ['an update RPC timeout', () => new WorkflowUpdateRPCTimeoutOrCancelledError('Workflow update call timeout or cancelled')],
  ])('a Temporal that is away (%s): the message stays committed, the user is told it waits, and it is sent again with its id', async (_name, error) => {
    const durable = new FakeDurable()
    durable.attemptError = error()
    durable.failures = 3
    const m = await durableManager(durable, { durableAcceptWaitMs: 50 })
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await m.send(session.id, browser, 'hi')
    // Never released as if nothing was sent: still running, the message still pending.
    expect(await status(session.id)).toBe('running')
    await expect.poll(() => durable.sends.length, POLL).toBe(1)
    expect(durable.sends[0]!.messageId).toBe(turn.turnId)
    expect(durable.attempts).toBe(4)
    expect(await errors(m, session.id)).toEqual([
      { v: 1, type: 'error', sessionId: session.id, code: DURABLE_WAITING_CODE, message: DURABLE_WAITING },
    ])
    expect(await status(session.id)).toBe('running')
  })

  it('a Temporal unreachable before anything was sent: the turn is logged and queued, never refused silently', async () => {
    // describe fails first: nothing reached Temporal, and the message is already committed.
    const fake = fakeTemporalClient({ status: 'RUNNING', failDescribes: [1] })
    const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql), { durableAcceptWaitMs: 50 })
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await m.send(session.id, browser, 'hi')
    expect((await m.events.read(session.id)).map((e) => e.event)).toContainEqual(
      expect.objectContaining({ type: 'user.turn', turnId: turn.turnId, text: 'hi' }),
    )
    await expect.poll(() => fake.updates.length, POLL).toBe(1)
    expect(fake.updates[0]!.options.updateId).toBe(turn.turnId)
    expect(await status(session.id)).toBe('running')
  })

  it('Review Focus 1: with no worker to accept the Update, the send answers and the session stays running', async () => {
    const durable = new FakeDurable()
    durable.accepted = new Promise(() => {})
    const m = await durableManager(durable, { durableAcceptWaitMs: 50 })
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await m.send(session.id, browser, 'hi')
    expect(turn.turnId).toBeTruthy()
    expect(await status(session.id)).toBe('running')
    // A notice, not a failure: the turn runs when a worker starts.
    expect(await errors(m, session.id)).toEqual([
      { v: 1, type: 'error', sessionId: session.id, code: DURABLE_WAITING_CODE, message: DURABLE_WAITING },
    ])
  })

  it('a send refused after the wait is logged, since nobody else is told', async () => {
    const durable = new FakeDurable()
    let accept = () => {}
    durable.accepted = new Promise((resolve) => {
      accept = resolve
    })
    const m = await durableManager(durable, { durableAcceptWaitMs: 50 })
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await m.send(session.id, browser, 'hi')
    durable.sendError = new DurableRefused(DURABLE_STOPPING)
    accept()
    await expect.poll(() => status(session.id), POLL).toBe('idle')
    const log = (await m.events.read(session.id)).map((e) => e.event)
    expect(log.slice(-2)).toEqual([
      { v: 1, type: 'error', sessionId: session.id, code: NOT_DELIVERED_CODE, message: notDelivered(DURABLE_STOPPING) },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
    expect(await turn.done).toEqual({ kind: 'failed', message: notDelivered(DURABLE_STOPPING) })
  })

  it('a Stop and an approval sent while a send waits for a stopped run are taken at once', async () => {
    const durable = new FakeDurable()
    // The stopped run never closes: only the Stop ends the send's wait.
    durable.closing = new Promise(() => {})
    durable.running = false
    const m = await durableManager(durable, { durableAcceptWaitMs: 600_000 })
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const out: { type: string; code?: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    const frame = (body: Record<string, unknown> & { type: string }) => JSON.stringify(clientMessage(body))
    // Each receive resolves once its frame was handled: held behind the send, none would.
    await connection.receive(frame({ type: 'user.message', sessionId: session.id, text: 'hi', context: { route: '/' } }))
    expect(await status(session.id)).toBe('running')
    await connection.receive(
      frame({ type: 'approval.decision', sessionId: session.id, id: `durable:${session.id}:toolu_1`, approve: false }),
    )
    expect(durable.reviews).toEqual([{ sessionId: session.id, toolUseId: 'toolu_1', approved: false, approver: 'browser:browser' }])
    await connection.receive(frame({ type: 'session.interrupt', sessionId: session.id }))
    // The send gave up before reaching Temporal, and its message will never run.
    await expect.poll(() => status(session.id), POLL).toBe('idle')
    expect(durable.sends).toEqual([])
    expect((await inputs(session.id)).map((i) => i.status)).toEqual(['abandoned'])
    const log = (await m.events.read(session.id)).map((e) => e.event)
    expect(log.slice(-2)).toEqual([
      { v: 1, type: 'error', sessionId: session.id, code: 'interrupted', message: STOPPED_BEFORE_IT_RAN },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
    // The Stop is not reported to the panel as a failed send.
    expect(out.filter((e) => e.type === 'error' && e.code !== 'interrupted')).toEqual([])
    connection.close()
  })

  it('a Stop on another replica while a send waits for a stopped run: the send is refused as stopped', async () => {
    // Replica A's send waits for the previous run to close; replica B (another manager on
    // the same database) handles the Stop. A must not start a new run with the message.
    const a = new FakeDurable()
    let close = () => {}
    a.closing = new Promise((resolve) => {
      close = resolve
    })
    const b = new FakeDurable()
    b.running = false
    const replicaA = await durableManager(a, { durableAcceptWaitMs: 50 })
    const replicaB = await durableManager(b)
    const { session } = await replicaA.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await replicaA.send(session.id, browser, 'hi')
    expect(await replicaB.interrupt(session.id, browser)).toBe(true)
    expect(await status(session.id)).toBe('idle')
    close()
    // A's look at the message once the run closed finds it abandoned: its delivery ends,
    // having sent nothing.
    await expect
      .poll(async () => (await db.sql`SELECT sending FROM ai_durable_streams WHERE session_id = ${session.id}`)[0]?.sending, POLL)
      .toBeNull()
    expect(a.sends).toEqual([])
    expect(await inputs(session.id)).toMatchObject([{ id: turn.turnId, status: 'abandoned' }])
    expect(await errors(replicaA, session.id)).toContainEqual(
      expect.objectContaining({ type: 'error', code: 'interrupted', message: STOPPED_BEFORE_IT_RAN }),
    )
    expect(await turn.done).toEqual({ kind: 'interrupted' })
  })

  it("another replica takes over a committed message whose sender died, with the message's own id", async () => {
    const dead = new FakeDurable()
    dead.accepted = new Promise(() => {}) // its process died mid-send: it never answers or beats again
    const replicaA = await durableManager(dead, { durableAcceptWaitMs: 50 })
    const { session } = await replicaA.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await replicaA.send(session.id, browser, 'hi')
    const live = new FakeDurable()
    const [replicaB, replicaC] = [
      await durableManager(live, { durableTakeoverMs: 100 }),
      await durableManager(live, { durableTakeoverMs: 100 }),
    ]
    // The first sweep only notes the heartbeat; it must stand still for durableTakeoverMs.
    expect(await replicaB.resumeDurableSends()).toEqual([])
    expect(await replicaC.resumeDurableSends()).toEqual([])
    await new Promise((resolve) => setTimeout(resolve, 200))
    const taken = [...(await Promise.all([replicaB.resumeDurableSends(), replicaC.resumeDurableSends()]))].flat()
    expect(taken).toEqual([turn.turnId]) // one replica wins the compare-and-set
    await expect.poll(() => live.sends.map((x) => x.messageId), POLL).toEqual([turn.turnId])
  })

  it('a live sender is never taken over while it beats', async () => {
    const durable = new FakeDurable()
    durable.attemptError = unavailable()
    durable.failures = 1_000_000
    const sender = await durableManager(durable, { durableAcceptWaitMs: 50 })
    const { session } = await sender.start(browser, { origin: 'chat', mode: 'durable' })
    await sender.send(session.id, browser, 'hi')
    const other = await durableManager(new FakeDurable(), { durableTakeoverMs: 2_000 })
    expect(await other.resumeDurableSends()).toEqual([])
    await new Promise((resolve) => setTimeout(resolve, 2_500))
    // Beaten every few ms by the sender's attempts: never still for 2 s.
    expect(await other.resumeDurableSends()).toEqual([])
    await sender.interrupt(session.id, browser)
  })

  it('Stop cancels the running execution of a running turn', async () => {
    const durable = new FakeDurable()
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable', prompt: 'hi' })
    expect(await m.interrupt(session.id, browser)).toBe(true)
    await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${session.id}`
    expect(await m.interrupt(session.id, browser)).toBe(true)
    expect(durable.cancels).toEqual([session.id, session.id])
  })

  it('Stop with no turn running answers false and leaves the idle run alone, as classic does', async () => {
    const durable = new FakeDurable()
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    expect(await m.interrupt(session.id, browser)).toBe(false)
    expect(durable.cancels).toEqual([])
  })

  it('Stop of a session that says it runs, with no run left: back to idle, never running forever', async () => {
    const durable = new FakeDurable()
    durable.running = false
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable', prompt: 'hi' })
    // A run took its message, then was terminated.
    await db.sql`UPDATE ai_durable_inputs SET status = 'run' WHERE session_id = ${session.id}`
    expect(await m.interrupt(session.id, browser)).toBe(false)
    expect(await status(session.id)).toBe('idle')
    expect((await m.events.read(session.id)).at(-1)?.event).toEqual({
      v: 1,
      type: 'session.status',
      sessionId: session.id,
      status: 'idle',
    })
  })

  it('Stop of a session whose message never reached a run: abandoned, with an error, and idle', async () => {
    const durable = new FakeDurable()
    durable.running = false
    durable.accepted = new Promise(() => {})
    const m = await durableManager(durable, { durableAcceptWaitMs: 50 })
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await m.send(session.id, browser, 'hi')
    expect(await m.interrupt(session.id, browser)).toBe(true)
    expect(await status(session.id)).toBe('idle')
    expect(await inputs(session.id)).toMatchObject([{ id: turn.turnId, status: 'abandoned' }])
    expect((await m.events.read(session.id)).slice(-2).map((e) => e.event)).toEqual([
      { v: 1, type: 'error', sessionId: session.id, code: 'interrupted', message: STOPPED_BEFORE_IT_RAN },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
  })

  it('a classic session never touches the durable workflows', async () => {
    const durable = new FakeDurable()
    const m = await durableManager(durable)
    const { session, turn } = await m.start(browser, { origin: 'chat', mode: 'classic', prompt: 'hi' })
    await turn!.done
    await (await m.send(session.id, browser, 'again')).done
    await m.interrupt(session.id, browser)
    expect(classicRuns).toBe(2)
    expect(durable.sends).toEqual([])
    expect(durable.cancels).toEqual([])
  })

  it('an omitted mode is classic while durable sessions cannot start; an explicit one is refused', async () => {
    values.set(SETTING_SESSION_MODE, 'durable')
    const m = await durableManager(undefined)
    expect((await m.start(browser, { origin: 'chat' })).session.mode).toBe('classic')
    await expect(m.start(browser, { origin: 'chat', mode: 'durable' })).rejects.toMatchObject({
      code: 'invalid',
      message: DURABLE_NEEDS_TEMPORAL,
    })
    const ready = await durableManager(new FakeDurable())
    expect((await ready.start(browser, { origin: 'chat' })).session.mode).toBe('durable')
  })

  it('a turn that resumed fresh still succeeds: that notice is not its failure', async () => {
    const durable = new FakeDurable()
    durable.result = { started: 'fresh', resumedFresh: true }
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await m.send(session.id, browser, 'hi')
    expect(await errors(m, session.id)).toEqual([expect.objectContaining({ code: 'resumed_fresh', message: RESUMED_FRESH })])
    await m.events.append(session.id, [
      { v: 1, type: 'session.result', sessionId: session.id, costUsd: 0, turns: 1 },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
  })

  describe('which execution takes the message', () => {
    const sending = async (id: string) =>
      (await db.sql<{ sending: string | null }[]>`SELECT sending FROM ai_durable_streams WHERE session_id = ${id}`)[0]
        ?.sending
    const offset = async (id: string) =>
      (await db.sql<{ next_offset: string }[]>`SELECT next_offset FROM ai_durable_streams WHERE session_id = ${id}`)[0]
        ?.next_offset
    /** The stream offset and status the update-with-start was sent with. */
    function recordAtUpdate(id: () => string) {
      const seen: { offset: string | undefined; status: string | undefined }[] = []
      return {
        seen,
        onUpdate: async (_update: FakeUpdate) => {
          seen.push({ offset: await offset(id()), status: await status(id()) })
        },
      }
    }

    it('after a Stop, starts the next execution from the last result, with the stream offset reset first', async () => {
      const state = { session_id: 'claude-1', checkpoint: 'e-4', recent_call_ids: [], pending: {} }
      let sid = ''
      const at = recordAtUpdate(() => sid)
      const fake = fakeTemporalClient({ status: 'COMPLETED', result: state, onUpdate: at.onUpdate })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      sid = session.id
      await db.sql`INSERT INTO ai_durable_streams (session_id, next_offset) VALUES (${sid}, 41)`
      await m.send(sid, browser, 'again')
      expect(fake.updates[0]?.options.startWorkflowOperation.options.args).toEqual([
        expect.not.objectContaining({ restored: expect.anything() }),
        state,
      ])
      expect(at.seen).toEqual([{ offset: '0', status: 'running' }])
      expect(await types(m, sid)).not.toContain('error')
    })

    it('attaching to a running execution keeps the stream offset', async () => {
      let sid = ''
      const at = recordAtUpdate(() => sid)
      const fake = fakeTemporalClient({ status: 'RUNNING', onUpdate: at.onUpdate })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      sid = session.id
      await db.sql`INSERT INTO ai_durable_streams (session_id, next_offset) VALUES (${sid}, 41)`
      await m.send(sid, browser, 'again')
      expect(at.seen).toEqual([{ offset: '41', status: 'running' }])
    })

    async function snapshotSession(status: string | undefined, chains: { chain?: string; chainAfterUpdate?: string } = {}) {
      let sid = ''
      const at = recordAtUpdate(() => sid)
      const fake = fakeTemporalClient({ ...(status ? { status } : {}), ...chains, onUpdate: at.onUpdate })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      sid = session.id
      await db.sql`INSERT INTO ai_durable_streams (session_id, next_offset) VALUES (${sid}, 9)`
      const state = { session_id: 'claude-1', checkpoint: 'e-2', recent_call_ids: ['toolu_done'], pending: {} }
      const inFlight = [{ id: 'toolu_running', name: 'render_model', status: 'started' }]
      // Explicit times: the host's wall clock jumps, so nothing here leans on now() margins.
      await db.sql`
        INSERT INTO ai_durable_snapshots (session_id, version, state, in_flight, saved_at)
        VALUES (${sid}, 3, ${JSON.stringify(state)}, ${db.sql.json(inFlight)}, now() - interval '1 hour')`
      return { m, sid, fake, at, state, inFlight }
    }

    async function auditCall(sid: string, toolUseId: string, action: string, ago: string) {
      await db.sql`
        INSERT INTO ai_audit (at, kind, action, surface, principal_kind, principal_id, principal_label,
                              session_id, tool_use_id, outcome)
        VALUES (now() - ${ago}::interval, 'tool_call', ${action}, 'harness', 'browser', 'browser', 'You',
                ${sid}, ${toolUseId}, 'ok')`
    }

    it.each([['TERMINATED'], ['FAILED'], [undefined]])(
      'an execution closed without a hand-over (%s) resumes from the snapshot, told of results it lost',
      async (closed) => {
        const { m, sid, fake, at, state, inFlight } = await snapshotSession(closed)
        // After the snapshot: one call the snapshot knows (in flight), one it does not.
        await auditCall(sid, 'toolu_running', 'render_model', '10 minutes')
        await auditCall(sid, 'toolu_lost', 'save_preset', '10 minutes')
        await auditCall(sid, 'toolu_lost', 'save_preset', '9 minutes')
        // Before it: already in the snapshot's state.
        await auditCall(sid, 'toolu_old', 'get_model', '2 hours')
        const turn = await m.send(sid, browser, 'go on', { context: 'route: /' })
        const update = fake.updates[0]!
        const args = update.options.startWorkflowOperation.options.args as unknown[]
        expect(args).toHaveLength(2)
        expect(args[0]).toMatchObject({ session_id: sid, restored: { in_flight: inFlight } })
        expect(args[1]).toEqual(state)
        expect(update.options.args).toEqual([{ id: turn.turnId }])
        // The line is the message's model-only note, which the run renders after the context.
        expect(await inputs(sid)).toMatchObject([
          {
            text: 'go on',
            context: 'route: /',
            note: "These tool calls ran after this session's last saved point, and their results were lost: save_preset (toolu_lost)",
          },
        ])
        expect(at.seen).toEqual([{ offset: '0', status: 'running' }])
      },
    )

    it('a running execution that closed before the start: the new run gets the snapshot', async () => {
      // describe says RUNNING; by the time the start arrives a Stop or a terminate closed it,
      // so the start makes a new run (a new chain) from the arguments it carried.
      const { m, sid, fake, at, state, inFlight } = await snapshotSession('RUNNING', { chain: 'run-1', chainAfterUpdate: 'run-2' })
      await auditCall(sid, 'toolu_lost', 'save_preset', '10 minutes')
      await m.send(sid, browser, 'go on')
      const [input, startState] = fake.updates[0]!.options.startWorkflowOperation.options.args as unknown[]
      expect(input).toMatchObject({ restored: { in_flight: inFlight } })
      expect(startState).toEqual(state)
      // Taken for an attach: no lost-results note (a live run lost nothing), and the offset
      // is kept; the projector reads the new run's chain from 0 by itself (projector.py `_drain`).
      expect((await inputs(sid))[0]!.note).toBeNull()
      expect(at.seen).toEqual([{ offset: '9', status: 'running' }])
      expect(await offset(sid)).toBe('9')
    })

    it('marks the delivery until it ends, and resets a new run\'s stream to a chain of its own', async () => {
      let sid = ''
      const marks: (string | null | undefined)[] = []
      const fake = fakeTemporalClient({
        status: 'COMPLETED',
        result: {},
        onUpdate: async () => {
          marks.push(await sending(sid))
        },
      })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      sid = session.id
      await db.sql`INSERT INTO ai_durable_streams (session_id, next_offset, chain) VALUES (${sid}, 41, 'old-chain')`
      const turn = await m.send(sid, browser, 'again')
      expect(marks).toEqual([turn.turnId])
      await expect.poll(() => sending(sid), POLL).toBeNull()
      // The chain is a sentinel no run has (the turn id), never NULL: a follower that read
      // (0, NULL) before its first commit cannot match the reset row (projector.py append_batch).
      expect(await db.sql`SELECT next_offset::int AS n, chain FROM ai_durable_streams WHERE session_id = ${sid}`).toEqual([
        { n: 0, chain: turn.turnId },
      ])
    })

    it('an update RPC that timed out is sent again with the same id, never released as unsent', async () => {
      const fake = fakeTemporalClient({
        status: 'RUNNING',
        updateErrors: [new WorkflowUpdateRPCTimeoutOrCancelledError('Workflow update call timeout or cancelled')],
      })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql), { durableAcceptWaitMs: 50 })
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      const turn = await m.send(session.id, browser, 'hi')
      await expect.poll(() => fake.updates.length, POLL).toBe(2)
      expect(fake.updates.map((u) => u.options.updateId)).toEqual([turn.turnId, turn.turnId])
      await expect.poll(() => sending(session.id), POLL).toBeNull()
      expect(await status(session.id)).toBe('running')
      expect((await inputs(session.id))[0]!.status).toBe('pending')
    })

    it('a nudge a Stop refused clears the mark, abandons the message and leaves the session idle', async () => {
      const refused = new WorkflowUpdateFailedError('Workflow Update failed', ApplicationFailure.create({ message: DURABLE_STOPPING }))
      const fake = fakeTemporalClient({ status: 'RUNNING', updateError: refused })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      await m.send(session.id, browser, 'hi')
      await expect.poll(() => sending(session.id), POLL).toBeNull()
      expect(await status(session.id)).toBe('idle')
      expect((await inputs(session.id))[0]!.status).toBe('abandoned')
      expect(await errors(m, session.id)).toEqual([
        expect.objectContaining({ code: NOT_DELIVERED_CODE, message: notDelivered(DURABLE_STOPPING) }),
      ])
    })

    it('a busy nudge clears the mark and keeps the message queued, the session running', async () => {
      const refused = new WorkflowUpdateFailedError('Workflow Update failed', ApplicationFailure.create({ message: DURABLE_BUSY }))
      const fake = fakeTemporalClient({ status: 'RUNNING', updateError: refused })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      await m.send(session.id, browser, 'hi')
      await expect.poll(() => sending(session.id), POLL).toBeNull()
      expect(await status(session.id)).toBe('running')
      expect((await inputs(session.id))[0]!.status).toBe('pending')
      expect(await errors(m, session.id)).toEqual([expect.objectContaining({ code: DURABLE_WAITING_CODE, message: DURABLE_QUEUED })])
    })

    it('a real attach carries the snapshot too, which the running execution ignores, and keeps the offset', async () => {
      const { m, sid, fake } = await snapshotSession('RUNNING', { chain: 'run-1' })
      await m.send(sid, browser, 'go on')
      expect((fake.updates[0]!.options.startWorkflowOperation.options.args as unknown[])[1]).toMatchObject({ checkpoint: 'e-2' })
      expect(await offset(sid)).toBe('9')
    })

    it('a snapshot with nothing lost adds no note', async () => {
      const { m, sid } = await snapshotSession('TERMINATED')
      await auditCall(sid, 'toolu_done', 'get_model', '5 minutes')
      await m.send(sid, browser, 'go on')
      expect((await inputs(sid))[0]!.note).toBeNull()
    })

    it('with no snapshot at all, starts from nothing and the log says the conversation is lost', async () => {
      let sid = ''
      const at = recordAtUpdate(() => sid)
      const fake = fakeTemporalClient({ status: 'TERMINATED', onUpdate: at.onUpdate })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      sid = session.id
      await m.send(sid, browser, 'hello?')
      expect(fake.updates[0]?.options.startWorkflowOperation.options.args).toEqual([
        { session_id: sid, max_turns: session.maxTurns, approval_expiry_seconds: 600, model: null },
        null,
      ])
      const log = (await m.events.read(sid)).map((e) => e.event)
      expect(log.slice(-3).map((e) => e.type)).toEqual(['user.turn', 'session.status', 'error'])
      expect(log.at(-1)).toMatchObject({ type: 'error', code: 'resumed_fresh' })
      expect(at.seen).toEqual([{ offset: '0', status: 'running' }])
    })
  })
})
