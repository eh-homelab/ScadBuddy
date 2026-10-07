import { randomUUID } from 'node:crypto'
import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { ResourceUpdatedNotificationSchema } from '@modelcontextprotocol/sdk/types.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import type { BusEvent } from '../src/events/bus.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { ResourceHub } from '../src/resources/hub.js'
import { BACKEND, connectWatching, testApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The agent's LISTEN on `scadbuddy_events` against a real Postgres (#264,
// spec §7), and the MCP path end to end on it: the MCP SDK client subscribes,
// the "backend" publishes the way backend/scadbuddy/core/pg_events.py does (a
// row in `events` and pg_notify in one transaction), and the client receives
// `notifications/resources/updated`.
//
// The `events` table is the backend's
// (backend/scadbuddy/migrations/20260928T0630Z_events.sql), created here in a
// throwaway schema. NOTIFY channels
// are per database, not per schema, so every test's listener hears every
// other test's events: assertions look only at this test's own event ids.

const EVENTS_DDL = `
  CREATE TABLE events (
      seq        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      event_id   text NOT NULL UNIQUE,
      kind       text NOT NULL,
      at         timestamptz NOT NULL,
      logged_at  timestamptz NOT NULL DEFAULT now(),
      payload    jsonb NOT NULL
  );
  CREATE INDEX events_logged_at ON events (logged_at);
`

function event(fields: { kind: string } & Record<string, string>): BusEvent {
  return { id: randomUUID().replaceAll('-', ''), at: new Date().toISOString(), ...fields }
}

/** What the backend's PgNotifyEventBus does: log row and NOTIFY in one transaction. */
async function publish(
  db: Database,
  e: BusEvent,
  { notify = true, loggedAt }: { notify?: boolean; loggedAt?: Date } = {},
): Promise<void> {
  const payload = JSON.stringify(e)
  await db.sql.begin(async (tx) => {
    await tx`
      INSERT INTO events (event_id, kind, at, payload, logged_at)
      VALUES (${e.id}, ${e.kind}, ${e.at!}, ${payload}::jsonb, coalesce(${loggedAt ?? null}::timestamptz, now()))`
    if (notify) await tx`SELECT pg_notify('scadbuddy_events', ${payload})`
  })
}

async function until(check: () => boolean, what: string, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!check()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`)
    await new Promise((r) => setTimeout(r, 10))
  }
}

describe.skipIf(!TEST_DATABASE_URL)(
  `the event bus listener in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let schema: string
    let drop: () => Promise<void>
    const listeners: PgEventListener[] = []

    beforeEach(async () => {
      ;({ db, schema, drop } = await throwawayDatabase())
      await db.sql.unsafe(EVENTS_DDL)
    })
    afterEach(async () => {
      await Promise.all(listeners.splice(0).map((l) => l.close()))
      await drop()
    })

    async function listen(options: { replayLimit?: number } = {}) {
      const logs: string[] = []
      const listener = new PgEventListener(TEST_DATABASE_URL!, {
        searchPath: schema,
        retryMinMs: 20,
        retryMaxMs: 100,
        checkIntervalMs: 60_000,
        log: (m) => logs.push(m),
        ...options,
      })
      listeners.push(listener)
      const heard: BusEvent[] = []
      let resyncs = 0
      listener.follow({ onEvent: (e) => heard.push(e), onResync: () => resyncs++ })
      listener.start()
      await listener.ready()
      return { listener, heard, logs, resyncs: () => resyncs }
    }

    it('hears an event the backend publishes, once', async () => {
      const { heard } = await listen()
      const e = event({ kind: 'job.done', job_id: 'j1', slug: 'k' })
      await publish(db, e)
      await until(() => heard.some((h) => h.id === e.id), 'the event')
      expect(heard.find((h) => h.id === e.id)).toMatchObject({ kind: 'job.done', job_id: 'j1', slug: 'k' })
      expect(heard.filter((h) => h.id === e.id)).toHaveLength(1)
    })

    it('hears nothing from a rolled-back transaction', async () => {
      const { heard } = await listen()
      const lost = event({ kind: 'model.created', slug: 'never' })
      await db.sql
        .begin(async (tx) => {
          await tx`SELECT pg_notify('scadbuddy_events', ${JSON.stringify(lost)})`
          throw new Error('roll back')
        })
        .catch(() => {})
      const after = event({ kind: 'model.created', slug: 'after' })
      await publish(db, after)
      await until(() => heard.some((h) => h.id === after.id), 'the later event')
      expect(heard.some((h) => h.id === lost.id)).toBe(false)
    })

    it('after a dropped connection, replays the gap from the events log by seq', async () => {
      const { listener, heard } = await listen()
      const before = event({ kind: 'font.installed', family: 'A' })
      await publish(db, before)
      await until(() => heard.some((h) => h.id === before.id), 'the live event')

      // Events whose NOTIFY this listener never heard: logged, not notified,
      // exactly what a NOTIFY during a disconnect looks like from here.
      const missed = [event({ kind: 'job.running', job_id: 'j2', slug: 'k' }), event({ kind: 'job.done', job_id: 'j2', slug: 'k' })]
      for (const e of missed) await publish(db, e, { notify: false })
      await listener.dropConnectionForTest()
      await until(() => missed.every((m) => heard.some((h) => h.id === m.id)), 'the replayed events')

      const ids = heard.map((h) => h.id)
      // In log order, after the live one, and the live one is not repeated.
      expect(ids.indexOf(missed[0]!.id)).toBeLessThan(ids.indexOf(missed[1]!.id))
      expect(ids.filter((id) => id === before.id)).toHaveLength(1)
      expect(listener.replayed).toBeGreaterThanOrEqual(2)
      // Short transactions: the long-transaction guard stays quiet.
      expect(listener.longTransactions).toBe(0)

      // And it listens again.
      const later = event({ kind: 'settings.changed', section: 'connection' })
      await publish(db, later)
      await until(() => heard.some((h) => h.id === later.id), 'an event after reconnecting')
    })

    // #893: seq is selected as text; ordering by that text replayed "10" before "9".
    it('replays a gap that crosses a digit boundary in numeric seq order', async () => {
      // seq 1..7 before the listener starts, so the gap it replays is 8..12.
      await db.sql`
        INSERT INTO events (event_id, kind, at, payload)
        SELECT ${randomUUID()} || g, 'print.progress', now(), '{}'::jsonb FROM generate_series(1, 7) g`
      const { listener, heard } = await listen()
      const missed = Array.from({ length: 5 }, (_, i) => event({ kind: 'print.progress', output_id: `o${i}`, slug: 'k' }))
      for (const e of missed) await publish(db, e, { notify: false })
      await listener.dropConnectionForTest()
      await until(() => missed.every((m) => heard.some((h) => h.id === m.id)), 'the replayed events')
      const mine = new Set(missed.map((m) => m.id))
      expect(heard.filter((h) => mine.has(h.id)).map((h) => h.id)).toEqual(missed.map((m) => m.id))
    })

    // #893: past the replay limit the listener adopts the newest seq; sorted as
    // text it adopted "9" of 8..12, and the next drop replayed 10..12 again.
    it('after a gap over the replay limit that crosses a digit boundary, resumes from the newest seq', async () => {
      await db.sql`
        INSERT INTO events (event_id, kind, at, payload)
        SELECT ${randomUUID()} || g, 'print.progress', now(), '{}'::jsonb FROM generate_series(1, 7) g`
      const { listener, heard, resyncs } = await listen({ replayLimit: 3 })
      const missed = Array.from({ length: 5 }, (_, i) => event({ kind: 'print.progress', output_id: `o${i}`, slug: 'k' }))
      for (const e of missed) await publish(db, e, { notify: false })
      await listener.dropConnectionForTest()
      await until(() => resyncs() === 1, 'the resync')
      expect(listener.replayed).toBe(0)

      const later = event({ kind: 'print.progress', output_id: 'later', slug: 'k' })
      await publish(db, later, { notify: false })
      await listener.dropConnectionForTest()
      await until(() => heard.some((h) => h.id === later.id), 'the event after the resync')
      expect(listener.replayed).toBe(1)
      expect(resyncs()).toBe(1)
      const stale = new Set(missed.map((m) => m.id))
      expect(heard.filter((h) => stale.has(h.id))).toEqual([])
    })

    it('resyncs after replaying a row whose transaction was open longer than the check interval', async () => {
      const { listener, heard, resyncs, logs } = await listen()
      // logged_at is the transaction's start (DEFAULT now()): an hour before the
      // listener read its place, far beyond the 60 s check interval here.
      const late = event({ kind: 'job.done', job_id: 'slow', slug: 'k' })
      await publish(db, late, { notify: false, loggedAt: new Date(Date.now() - 3_600_000) })
      await listener.dropConnectionForTest()
      await until(() => resyncs() === 1, 'the resync')
      expect(heard.some((h) => h.id === late.id)).toBe(true)
      expect(listener.longTransactions).toBe(1)
      expect(logs.some((l) => l.includes('may have been skipped'))).toBe(true)
    })

    it('resyncs followers instead when the gap is larger than the replay limit', async () => {
      const { listener, heard, resyncs } = await listen({ replayLimit: 3 })
      for (let i = 0; i < 5; i++) await publish(db, event({ kind: 'print.progress', output_id: 'o', slug: 'k' }), { notify: false })
      await listener.dropConnectionForTest()
      await until(() => resyncs() === 1, 'the resync')
      expect(listener.replayed).toBe(0)
      expect(heard.filter((h) => h.kind === 'print.progress' && h.output_id === 'o')).toHaveLength(0)
    })

    it('without a readable log, still hears live events and resyncs after a drop', async () => {
      await db.sql`DROP TABLE events`
      const { listener, heard, resyncs } = await listen()
      const e = event({ kind: 'model.created', slug: 'x' })
      await db.sql`SELECT pg_notify('scadbuddy_events', ${JSON.stringify(e)})`
      await until(() => heard.some((h) => h.id === e.id), 'the live event')
      await listener.dropConnectionForTest()
      await until(() => resyncs() === 1, 'the resync')
    })
  },
)

describe.skipIf(!TEST_DATABASE_URL)(
  `MCP resource subscriptions over the Postgres bus${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    const backend = setupServer(http.get(`${BACKEND}/api/v1/models`, () => HttpResponse.json([])))
    beforeAll(() => backend.listen({ onUnhandledRequest: 'error' }))
    afterAll(() => backend.close())

    let db: Database
    let schema: string
    let drop: () => Promise<void>
    let listener: PgEventListener
    let client: Client | undefined

    beforeEach(async () => {
      ;({ db, schema, drop } = await throwawayDatabase())
      await db.sql.unsafe(EVENTS_DDL)
      listener = new PgEventListener(TEST_DATABASE_URL!, { searchPath: schema, retryMinMs: 20, log: () => {} })
      listener.start()
      await listener.ready()
    })
    afterEach(async () => {
      await client?.close()
      client = undefined
      await listener.close()
      await drop()
    })

    it('subscribe, publish on the bus, receive notifications/resources/updated', async () => {
      const hub = new ResourceHub(listener, { minIntervalMs: 0 })
      const t = testApp({ mcp: { resources: hub } })
      const { token } = await t.tokens.mint({ name: 'pg', tier: 'read' })
      const watching = await connectWatching(t.app, { headers: { authorization: `Bearer ${token}` } })
      client = watching.client
      const updated: string[] = []
      client.setNotificationHandler(ResourceUpdatedNotificationSchema, (n) => {
        updated.push(n.params.uri)
      })
      await watching.streamOpen()

      const jobId = randomUUID().replaceAll('-', '')
      await client.subscribeResource({ uri: `scadbuddy://jobs/${jobId}` })
      await publish(db, event({ kind: 'job.running', job_id: jobId, slug: 'keychain' }))
      await until(() => updated.includes(`scadbuddy://jobs/${jobId}`), 'the resource update')

      // An event missed while the agent's LISTEN connection was down still
      // reaches the subscriber, from the replay log.
      await publish(db, event({ kind: 'job.done', job_id: jobId, slug: 'keychain' }), { notify: false })
      await listener.dropConnectionForTest()
      await until(() => updated.filter((u) => u === `scadbuddy://jobs/${jobId}`).length === 2, 'the replayed update')
      hub.close()
    })
  },
)
