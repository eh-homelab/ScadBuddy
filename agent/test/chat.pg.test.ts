import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { ChatConnection } from '../src/routes/chat.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { event } from '../src/sessions/protocol.js'
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

  it('does not send input.* on the socket; the cards keep their own events (spec §6.6)', async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    const entry = {
      id: 'question:00000000-0000-4000-8000-000000000001',
      kind: 'answer' as const,
      session_id: session.id,
      tool: 'AskUserQuestion',
      summary: '',
      input_hash: null,
      prompt: 'Which colour?',
      requested_by: null,
      responders: ['browser' as const],
      created_at: new Date().toISOString(),
      expires_at: null,
    }
    await m.events.append(session.id, [
      event({ type: 'input.requested', sessionId: session.id, entry }),
      event({ type: 'question.asked', sessionId: session.id, id: '00000000-0000-4000-8000-000000000001', tool: 't', questions: [] }),
      event({ type: 'input.resolved', sessionId: session.id, id: entry.id, kind: 'answer', outcome: 'cancelled' }),
    ])
    const out: { type?: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e as { type?: string }))
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(JSON.stringify(clientMessage({ type: 'session.attach', sessionId: session.id })))
    await expect.poll(() => out.some((e) => e.type === 'question.asked')).toBe(true)
    await settle()
    connection.close()
    expect(out.filter((e) => e.type?.startsWith('input.'))).toEqual([])
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

  it("passes a new chat's mode, and refuses one on an existing session (plan 5d)", async () => {
    const out: { type: string; sessionId?: string; mode?: string; code?: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e as never))
    await connection.open()
    const { clientMessage } = await frontendClientMessages()
    await connection.receive(JSON.stringify(clientMessage({ type: 'user.message', text: 'hi', context: { route: '/' }, mode: 'classic' })))
    await settle()
    const started = out.find((e) => e.type === 'session.started') as { sessionId: string; mode?: string; modeFallback?: string }
    expect(started).toMatchObject({ mode: 'classic' })
    expect(started).not.toHaveProperty('modeFallback')
    // Asked for, durable is refused here (no Temporal), not run as classic.
    await connection.receive(JSON.stringify(clientMessage({ type: 'user.message', text: 'hi', context: { route: '/' }, mode: 'durable' })))
    const errors = () => out.filter((e) => e.type === 'error')
    expect(errors()).toEqual([expect.objectContaining({ code: 'unavailable' })])
    await connection.receive(
      JSON.stringify(
        clientMessage({ type: 'user.message', sessionId: started.sessionId, text: 'more', context: { route: '/' }, mode: 'classic' }),
      ),
    )
    expect(errors().at(-1)).toMatchObject({ code: 'invalid', sessionId: started.sessionId })
    connection.close()
  })
})
