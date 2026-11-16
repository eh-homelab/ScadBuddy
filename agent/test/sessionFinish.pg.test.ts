import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { SessionBlobs, sessionImage } from '../src/sessions/blobs.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { QUEUED_DROPPED } from '../src/temporal/session/finishActivity.js'
import type { QueuedMessage } from '../src/temporal/session/models.js'
import { SESSION_BEGIN_TURN_ACTIVITY, SESSION_FINISH_ACTIVITY, SESSION_QUEUE_CHANGED_ACTIVITY } from '../src/temporal/session/names.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'

// `session.finish_turn` (temporal/session/finishActivity.ts) writes a turn's end only while the
// row still runs that turn. AgentSession takes a message once the row is idle, before it has
// heard the activity's answer (Task 5.3, no busy window), so a retried finish of the turn before
// must never end the turn that claimed the row since.

describe.skipIf(!TEST_DATABASE_URL)(`session.finish_turn${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it('writes nothing for a turn the row no longer runs: a retry after the next turn claimed it', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths() })
    const { session } = await m.start(browser, { origin: 'chat', title: 'two turns' })
    const finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
    const [t1, t2] = [randomUUID(), randomUUID()]
    // The next turn has claimed the row (DurableTurns.send) after t1's end was written.
    await db.sql`UPDATE ai_sessions SET status = 'running', durable_turn_id = ${t2} WHERE id = ${session.id}`
    const before = (await m.events.read(session.id, 0, 1000)).length
    expect(await finish({ sessionId: session.id, turnId: t1, outcome: 'done' })).toBe(false)
    const [row] = await db.sql<{ status: string; durable_turn_id: string | null }[]>`SELECT status, durable_turn_id FROM ai_sessions WHERE id = ${session.id}`
    expect(row).toEqual({ status: 'running', durable_turn_id: t2 })
    expect((await m.events.read(session.id, 0, 1000)).length).toBe(before)
    // Its own turn's end is written.
    expect(await finish({ sessionId: session.id, turnId: t2, outcome: 'done' })).toBe(true)
    const [after] = await db.sql<{ status: string; durable_turn_id: string | null }[]>`SELECT status, durable_turn_id FROM ai_sessions WHERE id = ${session.id}`
    expect(after).toEqual({ status: 'idle', durable_turn_id: null })
  })

  it("reports the session's order costs once its end is written, and on a retry (#5330)", async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths() })
    const { session } = await m.start(browser, { origin: 'chat', title: 'order work' })
    const reported: string[] = []
    // A report that never ends holds up neither the turn's end nor the next turn.
    const finish = sessionFinishActivities(db.sql, m.events, {
      report: (id) => {
        reported.push(id)
        return new Promise<void>(() => {})
      },
    })[SESSION_FINISH_ACTIVITY]
    const turn = randomUUID()
    await db.sql`UPDATE ai_sessions SET status = 'running', durable_turn_id = ${turn} WHERE id = ${session.id}`
    expect(await finish({ sessionId: session.id, turnId: turn, outcome: 'done' })).toBe(true)
    // A retried finish writes nothing, but a report that failed the first time is sent again.
    expect(await finish({ sessionId: session.id, turnId: turn, outcome: 'done' })).toBe(false)
    expect(reported).toEqual([session.id, session.id])
  })

  describe('the queue (#6386)', () => {
    const next = (turnId: string, extra: Partial<QueuedMessage> = {}): QueuedMessage => ({
      turn_id: turnId,
      text: 'and a lid\n\n[page: /]',
      words: 'and a lid',
      author: browser,
      images: [{ name: 'a'.repeat(64), mediaType: 'image/png' }],
      sent: [{ name: 'a'.repeat(64), mediaType: 'image/png', preview: { name: 'b'.repeat(64), mediaType: 'image/webp' } }],
      system_append: null,
      tiers: ['read', 'write'],
      bare: true,
      queued_at: '2026-11-15T00:00:00.000Z',
      ...extra,
    })
    const rowOf = async (id: string) =>
      (
        await db.sql<{ status: string; durable_turn_id: string | null; turn_tiers: unknown; turn_flow: boolean }[]>`
          SELECT status, durable_turn_id, turn_tiers, turn_flow FROM ai_sessions WHERE id = ${id}`
      )[0]
    const types = async (id: string, after: number) => (await db.sql<{ event: string }[]>`SELECT event FROM ai_session_events WHERE session_id = ${id} AND seq > ${after} ORDER BY seq`).map((r) => JSON.parse(r.event) as { type: string; status?: string; code?: string; text?: string; turnId?: string })

    it("starts the queue's head in the turn's end: no idle row a send could take, its user.turn after the end's events", async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths() })
      const { session } = await m.start(browser, { origin: 'chat', title: 'queued' })
      const activities = sessionFinishActivities(db.sql, m.events)
      const [t1, q1] = [randomUUID(), randomUUID()]
      await db.sql`UPDATE ai_sessions SET status = 'running', durable_turn_id = ${t1} WHERE id = ${session.id}`
      const before = await m.events.lastSeq(session.id)
      expect(await activities[SESSION_FINISH_ACTIVITY]({ sessionId: session.id, turnId: t1, outcome: 'interrupted', next: next(q1) })).toEqual({ written: true, next: 'started' })
      expect(await rowOf(session.id)).toEqual({
        status: 'running',
        durable_turn_id: q1,
        turn_tiers: { by: { kind: browser.kind, id: browser.id }, tiers: ['read', 'write'] },
        turn_flow: true,
      })
      const written = await types(session.id, before)
      expect(written.map((e) => e.type)).toEqual(['session.result', 'error', 'session.status', 'user.turn', 'session.status'])
      expect(written[3]).toMatchObject({ turnId: q1, text: 'and a lid', images: [{ preview: { name: 'b'.repeat(64) } }] })
      expect(written.at(-1)).toMatchObject({ status: 'running' })
      // A retry writes nothing and still says it started.
      expect(await activities[SESSION_FINISH_ACTIVITY]({ sessionId: session.id, turnId: t1, outcome: 'interrupted', next: next(q1) })).toEqual({ written: false, next: 'started' })
      expect(await types(session.id, before)).toHaveLength(5)
    })

    it('begin_turn: busy while another turn has the row, started once it is free, dropped with its reason when the session cannot run it', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths() })
      const { session } = await m.start(browser, { origin: 'chat', title: 'queued' })
      const begin = sessionFinishActivities(db.sql, m.events)[SESSION_BEGIN_TURN_ACTIVITY]
      const other = randomUUID()
      await db.sql`UPDATE ai_sessions SET status = 'running', durable_turn_id = ${other} WHERE id = ${session.id}`
      expect(await begin({ sessionId: session.id, next: next(randomUUID()) })).toBe('busy')
      await db.sql`UPDATE ai_sessions SET status = 'idle', durable_turn_id = NULL WHERE id = ${session.id}`
      const q1 = randomUUID()
      expect(await begin({ sessionId: session.id, next: next(q1) })).toBe('started')
      expect((await rowOf(session.id))?.durable_turn_id).toBe(q1)
      // Its author no longer owns the session: dropped, and said.
      await db.sql`UPDATE ai_sessions SET status = 'idle', durable_turn_id = NULL WHERE id = ${session.id}`
      const before = await m.events.lastSeq(session.id)
      const stranger = { kind: 'bearer', id: 'token:other', label: 'Other' } as const
      expect(await begin({ sessionId: session.id, next: next(randomUUID(), { author: stranger }) })).toBe('dropped')
      // Spent: dropped.
      await db.sql`UPDATE ai_sessions SET cost_usd = budget_usd WHERE id = ${session.id}`
      expect(await begin({ sessionId: session.id, next: next(randomUUID()) })).toBe('dropped')
      expect((await types(session.id, before)).map((e) => [e.type, e.code])).toEqual([
        ['error', QUEUED_DROPPED],
        ['error', QUEUED_DROPPED],
      ])
      expect((await rowOf(session.id))?.status).toBe('idle')
      // A session that is gone: dropped, nothing written.
      expect(await begin({ sessionId: randomUUID(), next: next(randomUUID()) })).toBe('dropped')
    })

    it('queue_changed writes the queue as a session.queue event, and nothing for a session that is gone', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths() })
      const { session } = await m.start(browser, { origin: 'chat', title: 'queued' })
      const changed = sessionFinishActivities(db.sql, m.events)[SESSION_QUEUE_CHANGED_ACTIVITY]
      const queue = [{ id: 'q1', text: 'and a lid', author: browser, queuedAt: '2026-11-15T00:00:00.000Z' }]
      expect(await changed({ sessionId: session.id, version: 3, queue })).toBe(true)
      expect((await m.events.read(session.id, 0, 1000)).at(-1)?.event).toMatchObject({ type: 'session.queue', version: 3, queue })
      expect(await m.queued(session.id, browser)).toEqual({ version: 3, queue })
      expect(await changed({ sessionId: randomUUID(), version: 1, queue })).toBe(false)
    })

    it('queue_changed deletes the images of the messages that left the queue unrun, after the event (#6413)', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths() })
      const { session } = await m.start(browser, { origin: 'chat', title: 'queued' })
      const blobs = new SessionBlobs(db.sql)
      const image = sessionImage('image/gif', Buffer.from('GIF89a-queued'))
      const made = await blobs.putMade(session.id, [image])
      expect(made).toHaveLength(1)
      const deleted: string[] = []
      const changed = sessionFinishActivities(db.sql, m.events, undefined, {
        delete: async (id, dropped) => {
          deleted.push(...(await blobs.delete(id, dropped)))
          return deleted
        },
      })[SESSION_QUEUE_CHANGED_ACTIVITY]
      expect(await changed({ sessionId: session.id, version: 1, queue: [], drop: made })).toBe(true)
      expect(deleted).toEqual([image.name])
      expect(await blobs.get(session.id, image.name)).toBeUndefined()
      // A delete that fails loses only the images: the event is written.
      const failing = sessionFinishActivities(db.sql, m.events, undefined, { delete: () => Promise.reject(new Error('down')) })[SESSION_QUEUE_CHANGED_ACTIVITY]
      expect(await failing({ sessionId: session.id, version: 2, queue: [], drop: made })).toBe(true)
      expect((await m.events.read(session.id, 0, 1000)).at(-1)?.event).toMatchObject({ type: 'session.queue', version: 2 })
    })
  })
})
