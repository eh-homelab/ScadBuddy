import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { ChatConnection } from '../src/routes/chat.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { frontendClientMessages } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

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
