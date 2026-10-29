import { randomUUID } from 'node:crypto'
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { PostgresSessionTabStore } from '../src/bridge/sessionTabs.js'
import { PostgresTabOwnerStore } from '../src/bridge/tabOwners.js'
import type { Database } from '../src/db.js'
import { SubjectForgotten, sweepBridgeKeys } from '../src/temporal/payloadCodec.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The bridge's shared rows: which tab a chat session pairs with (#2086,
// bridge/sessionTabs.ts) and which replica's queue holds each tab (#4780,
// bridge/tabOwners.ts). The calls across replicas are bridgeRelay.temporal.test.ts.

describe.skipIf(!TEST_DATABASE_URL)(`the bridge's stores${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeAll(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  beforeEach(async () => {
    await db.sql`TRUNCATE ai_session_tabs, ai_tab_owners`
  })
  afterAll(async () => {
    await drop()
  })

  async function chatSession(): Promise<string> {
    const [row] = await db.sql<{ id: string }[]>`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (gen_random_uuid(), 'chat', 'browser', 'browser', 'you', 'browser', 'browser', 'idle', 10, 1)
      RETURNING id`
    return row!.id
  }

  it('writes a session tab only while it is still the one seen (#2271 review)', async () => {
    const store = new PostgresSessionTabStore(db.sql)
    const id = await chatSession()
    expect(await store.setIf(id, 'tab-a', undefined)).toBe(true)
    expect(await store.setIf(id, 'tab-b', undefined)).toBe(false) // a row now
    expect(await store.setIf(id, 'tab-b', 'tab-x')).toBe(false)
    expect(await store.get(id)).toBe('tab-a')
    expect(await store.setIf(id, 'tab-b', 'tab-a')).toBe(true)
    expect(await store.get(id)).toBe('tab-b')
  })

  it("pairs a session with no row whatever tab was seen: the seen tab may be this replica's map (#3478 review)", async () => {
    const store = new PostgresSessionTabStore(db.sql)
    const id = await chatSession()
    expect(await store.setIf(id, 'tab-a', 'tab-x')).toBe(true)
    expect(await store.get(id)).toBe('tab-a')
    expect(await store.setIf(id, 'tab-b', 'tab-x')).toBe(false) // a row now, naming another tab
    expect(await store.get(id)).toBe('tab-a')
  })

  it("names the latest replica to claim a tab, and drops it only for that replica's own release", async () => {
    const owners = new PostgresTabOwnerStore(db.sql)
    expect(await owners.ownerOf('tab-a')).toBeUndefined()
    await owners.claim('tab-a', 'agent-tools.b')
    expect((await owners.ownerOf('tab-a'))?.queue).toBe('agent-tools.b')
    // A reconnect to C, then B sees its old socket close.
    await owners.claim('tab-a', 'agent-tools.c')
    await owners.release('tab-a', 'agent-tools.b')
    expect((await owners.ownerOf('tab-a'))?.queue).toBe('agent-tools.c')
    await owners.release('tab-a', 'agent-tools.c')
    expect(await owners.ownerOf('tab-a')).toBeUndefined()
  })

  it("drops every tab of a replica's queue at once (its start, after a restart)", async () => {
    const owners = new PostgresTabOwnerStore(db.sql)
    await owners.claim('tab-a', 'agent-tools.b')
    await owners.claim('tab-b', 'agent-tools.b')
    await owners.claim('tab-c', 'agent-tools.c')
    await owners.releaseAll('agent-tools.b')
    expect(await owners.ownerOf('tab-a')).toBeUndefined()
    expect(await owners.ownerOf('tab-b')).toBeUndefined()
    expect((await owners.ownerOf('tab-c'))?.queue).toBe('agent-tools.c')
  })

  it("sweeps only the keys of browser calls older than an hour (#4780)", async () => {
    const old = `bridge-${randomUUID()}`
    const fresh = `bridge-${randomUUID()}`
    const session = `session-${randomUUID()}`
    await db.sql`
      INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id, created_at) VALUES
        (${old}, '\\x00', 'k', now() - interval '2 hours'),
        (${fresh}, '\\x00', 'k', now()),
        (${session}, '\\x00', 'k', now() - interval '2 hours')`
    expect(await sweepBridgeKeys(db.sql)).toBe(1)
    const left = await db.sql<{ subject: string }[]>`SELECT subject FROM ai_payload_keys WHERE subject IN (${old}, ${fresh}, ${session})`
    expect(left.map((r) => r.subject).sort()).toEqual([fresh, session].sort())
  })

  it("refreshes a replica's own rows and makes a lost one again, never taking another replica's (#4806 review)", async () => {
    const owners = new PostgresTabOwnerStore(db.sql)
    await owners.claim('tab-a', 'agent-tools.b')
    await owners.claim('tab-b', 'agent-tools.c') // reconnected to C; B's old socket not yet closed
    const before = (await owners.ownerOf('tab-a'))!.claimedAt
    await owners.refresh(['tab-a', 'tab-b', 'tab-lost'], 'agent-tools.b')
    expect((await owners.ownerOf('tab-a'))!.claimedAt > before).toBe(true)
    expect((await owners.ownerOf('tab-b'))?.queue).toBe('agent-tools.c')
    expect((await owners.ownerOf('tab-lost'))?.queue).toBe('agent-tools.b')
  })

  it('drops a row a missed call read only while nobody refreshed it since (#4806 review)', async () => {
    const owners = new PostgresTabOwnerStore(db.sql)
    await owners.claim('tab-a', 'agent-tools.b')
    const read = (await owners.ownerOf('tab-a'))!.claimedAt
    await owners.refresh(['tab-a'], 'agent-tools.b') // the live owner, after the read
    await owners.release('tab-a', 'agent-tools.b', read)
    expect((await owners.ownerOf('tab-a'))?.queue).toBe('agent-tools.b')
    // The row as read, with microseconds: no later write, so it goes.
    const now = (await owners.ownerOf('tab-a'))!.claimedAt
    await owners.release('tab-a', 'agent-tools.b', now)
    expect(await owners.ownerOf('tab-a')).toBeUndefined()
  })

  it("never caches a browser call's payload key, so deleting the row shreds it everywhere (#4806 review)", async () => {
    const keys = testPayloadKeys(db.sql)
    const subject = `bridge-${randomUUID()}`
    await keys.keyFor(subject, true)
    await keys.keyFor(subject, false) // read again: a cached copy would now outlive the row
    await db.sql`DELETE FROM ai_payload_keys WHERE subject = ${subject}` // another replica's dropPayloadKey
    await expect(keys.keyFor(subject, false)).rejects.toBeInstanceOf(SubjectForgotten)
  })

  it('stops a read with the call it is for', async () => {
    const owners = new PostgresTabOwnerStore(db.sql)
    const stop = new AbortController()
    stop.abort()
    await expect(owners.ownerOf('tab-a', stop.signal)).rejects.toBeDefined()
  })
})
