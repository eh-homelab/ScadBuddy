import { randomBytes } from 'node:crypto'
import { WorkflowUpdateFailedError, WorkflowUpdateRPCTimeoutOrCancelledError } from '@temporalio/client'
import { ApplicationFailure } from '@temporalio/common'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SETTING_APPROVAL_EXPIRY_SECONDS } from '../src/approvals/service.js'
import type { Database } from '../src/db.js'
import { DurableRefused, DurableUnavailable, TemporalDurableSessions } from '../src/durable/client.js'
import { ChatConnection } from '../src/routes/chat.js'
import { kekFromBase64 } from '../src/secrets.js'
import {
  DURABLE_NEEDS_TEMPORAL,
  DURABLE_UNREACHABLE,
  DURABLE_WAITING,
  DURABLE_WAITING_CODE,
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

  it('sends to the workflow with the session\'s limits and the page context, and logs the turn', async () => {
    const durable = new FakeDurable()
    const m = await durableManager(durable)
    values.set(SETTING_APPROVAL_EXPIRY_SECONDS, 120)
    values.set(SETTING_MODEL, 'claude-test-model')
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const turn = await m.send(session.id, browser, ' hello ', { context: 'route: /' })
    expect(durable.sends).toEqual([
      {
        input: { session_id: session.id, max_turns: session.maxTurns, approval_expiry_seconds: 120, model: 'claude-test-model' },
        message: { text: 'hello', context: 'route: /' },
      },
    ])
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
    expect(durable.sends.map((s) => s.message)).toEqual([{ text: 'first', context: 'ctx' }])
    expect(session.status).toBe('running')
    expect(classicRuns).toBe(0)
  })

  it('refuses a send while the session runs or waits, without asking Temporal', async () => {
    const durable = new FakeDurable()
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    await m.send(session.id, browser, 'one')
    await expect(m.send(session.id, browser, 'two')).rejects.toMatchObject({ code: 'busy' })
    await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${session.id}`
    await expect(m.send(session.id, browser, 'three')).rejects.toMatchObject({ code: 'busy' })
    await expect(m.send(session.id, agentA, 'mine')).rejects.toMatchObject({ code: 'not_found' })
    expect(durable.sends).toHaveLength(1)
  })

  it('turns "the session is busy" into busy, and gives the claim back', async () => {
    const durable = new FakeDurable()
    durable.sendError = new DurableRefused('the session is busy')
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    await expect(m.send(session.id, browser, 'hi')).rejects.toMatchObject({ code: 'busy', message: 'the session is busy' })
    expect(await status(session.id)).toBe('idle')
    // The sender is told; the log records only the release, so the error shows once.
    const log = (await m.events.read(session.id)).map((e) => e.event)
    expect(log.at(-1)).toEqual({ v: 1, type: 'session.status', sessionId: session.id, status: 'idle' })
    expect(log.map((e) => e.type)).not.toContain('error')
  })

  it('shows a send refused on the socket once', async () => {
    const durable = new FakeDurable()
    durable.sendError = new DurableRefused('the session is busy')
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const out: { type: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(
      JSON.stringify(clientMessage({ type: 'user.message', sessionId: session.id, text: 'hi', context: { route: '/' } })),
    )
    await expect.poll(() => out.some((e) => e.type === 'session.status' && (e as { status?: string }).status === 'idle')).toBe(true)
    expect(out.filter((e) => e.type === 'error')).toHaveLength(1)
    connection.close()
  })

  it('a Temporal that is away after the claim: back to idle, and the log says so', async () => {
    const durable = new FakeDurable()
    durable.sendError = new DurableUnavailable('14 UNAVAILABLE')
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    await expect(m.send(session.id, browser, 'hi')).rejects.toMatchObject({ code: 'busy', message: DURABLE_UNREACHABLE })
    expect(await status(session.id)).toBe('idle')
    expect(await types(m, session.id)).not.toContain('error')
  })

  it('a Temporal that is away before the claim: refused, and nothing is logged', async () => {
    const durable = new FakeDurable()
    durable.describeError = new DurableUnavailable('14 UNAVAILABLE')
    const m = await durableManager(durable)
    const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
    const before = await types(m, session.id)
    await expect(m.send(session.id, browser, 'hi')).rejects.toMatchObject({ code: 'busy', message: DURABLE_UNREACHABLE })
    expect(await types(m, session.id)).toEqual(before)
    expect(await status(session.id)).toBe('idle')
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
    const errors = (await m.events.read(session.id)).map((e) => e.event).filter((e) => e.type === 'error')
    expect(errors).toEqual([
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
    durable.sendError = new DurableRefused('the session is busy')
    accept()
    await expect.poll(() => status(session.id)).toBe('idle')
    const log = (await m.events.read(session.id)).map((e) => e.event)
    expect(log.slice(-2)).toEqual([
      { v: 1, type: 'error', sessionId: session.id, code: 'busy', message: 'the session is busy' },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
    expect(await turn.done).toEqual({ kind: 'failed', message: 'the session is busy' })
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
    expect(await m.interrupt(session.id, browser)).toBe(false)
    expect(await status(session.id)).toBe('idle')
    expect((await m.events.read(session.id)).at(-1)?.event).toEqual({
      v: 1,
      type: 'session.status',
      sessionId: session.id,
      status: 'idle',
    })
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
        null,
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
        await m.send(sid, browser, 'go on', { context: 'route: /' })
        const update = fake.updates[0]!
        const [input, startState, inbox] = update.options.startWorkflowOperation.options.args as unknown[]
        expect(input).toMatchObject({ session_id: sid, restored: { in_flight: inFlight } })
        expect(startState).toEqual(state)
        expect(inbox).toBeNull()
        expect(update.options.args).toEqual([
          {
            text: 'go on',
            context:
              "route: /\n\nThese tool calls ran after this session's last saved point, and their results were lost: " +
              'save_preset (toolu_lost)',
          },
        ])
        expect(at.seen).toEqual([{ offset: '0', status: 'running' }])
      },
    )

    it('a running execution that closed before the start: the new run gets the snapshot, and its offset 0', async () => {
      // describe says RUNNING; by the time the start arrives a Stop or a terminate closed it,
      // so the start makes a new run (a new chain) from the arguments it carried.
      const { m, sid, fake, at, state, inFlight } = await snapshotSession('RUNNING', { chain: 'run-1', chainAfterUpdate: 'run-2' })
      await auditCall(sid, 'toolu_lost', 'save_preset', '10 minutes')
      await m.send(sid, browser, 'go on')
      const [input, startState] = fake.updates[0]!.options.startWorkflowOperation.options.args as unknown[]
      expect(input).toMatchObject({ restored: { in_flight: inFlight } })
      expect(startState).toEqual(state)
      expect(fake.updates[0]!.options.args).toEqual([{ text: 'go on', context: expect.stringContaining('save_preset (toolu_lost)') }])
      // Claimed as an attach: the offset is kept, and the projector reads the new run's
      // chain from 0 by itself (projector.py `_drain`), so nothing resets it after the start.
      expect(at.seen).toEqual([{ offset: '9', status: 'running' }])
      expect(await offset(sid)).toBe('9')
    })

    it('marks the send in flight until its update-with-start answers, for the projector', async () => {
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
      await expect.poll(() => sending(sid)).toBeNull()
      // A new run: the offset and its chain were reset with the claim.
      // The chain is a sentinel no run has (the turn id), never NULL: a follower that read
      // (0, NULL) before its first commit cannot match the reset row (projector.py append_batch).
      expect(await db.sql`SELECT next_offset::int AS n, chain FROM ai_durable_streams WHERE session_id = ${sid}`).toEqual([
        { n: 0, chain: turn.turnId },
      ])
    })

    it('keeps the mark when the start RPC timed out, since the start may still land', async () => {
      const fake = fakeTemporalClient({
        status: 'RUNNING',
        updateError: new WorkflowUpdateRPCTimeoutOrCancelledError('Workflow update call timeout or cancelled'),
      })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      const turn = await m.send(session.id, browser, 'hi')
      // Whatever the manager does after the answer has run by the time the log is read twice.
      await m.events.read(session.id)
      await m.events.read(session.id)
      expect(await sending(session.id)).toBe(turn.turnId)
      expect(await status(session.id)).toBe('running')
    })

    it('clears the mark when the start fails too', async () => {
      const fake = fakeTemporalClient({ status: 'RUNNING', updateError: new DurableUnavailable('14 UNAVAILABLE') })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      await expect(m.send(session.id, browser, 'hi')).rejects.toMatchObject({ code: 'busy' })
      await expect.poll(() => sending(session.id)).toBeNull()
    })

    it('a real attach carries the snapshot too, which the running execution ignores, and keeps the offset', async () => {
      const { m, sid, fake } = await snapshotSession('RUNNING', { chain: 'run-1' })
      await m.send(sid, browser, 'go on')
      expect((fake.updates[0]!.options.startWorkflowOperation.options.args as unknown[])[1]).toMatchObject({ checkpoint: 'e-2' })
      expect(await offset(sid)).toBe('9')
    })

    it('a snapshot with nothing lost adds no line', async () => {
      const { m, sid, fake } = await snapshotSession('TERMINATED')
      await auditCall(sid, 'toolu_done', 'get_model', '5 minutes')
      await m.send(sid, browser, 'go on')
      expect(fake.updates[0]?.options.args).toEqual([{ text: 'go on', context: null }])
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
        null,
      ])
      const log = (await m.events.read(sid)).map((e) => e.event)
      expect(log.slice(-3).map((e) => e.type)).toEqual(['error', 'user.turn', 'session.status'])
      expect(log.at(-3)).toMatchObject({ type: 'error', code: 'resumed_fresh' })
      expect(at.seen).toEqual([{ offset: '0', status: 'running' }])
    })

    it('a refused update-with-start leaves the session idle', async () => {
      const refused = new WorkflowUpdateFailedError('Workflow Update failed', ApplicationFailure.create({ message: 'the session is busy' }))
      const fake = fakeTemporalClient({ status: 'RUNNING', updateError: refused })
      const m = await durableManager(new TemporalDurableSessions(fake.client, db.sql))
      const { session } = await m.start(browser, { origin: 'chat', mode: 'durable' })
      await expect(m.send(session.id, browser, 'hi')).rejects.toMatchObject({ code: 'busy', message: 'the session is busy' })
      expect(await status(session.id)).toBe('idle')
    })
  })
})
