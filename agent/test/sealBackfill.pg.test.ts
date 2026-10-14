import { randomUUID } from 'node:crypto'
import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { sealBackfill } from '../src/sessions/sealBackfill.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import { costStateUsd } from '../src/sessions/warm.js'
import type { PgPayloadKeys } from '../src/temporal/payloadCodec.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

describe.skipIf(!TEST_DATABASE_URL)(
  `sealBackfill${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let keys: PgPayloadKeys
    let store: PostgresSessionStore

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      keys = testPayloadKeys(db.sql)
      store = new PostgresSessionStore(db.sql, { keys })
    })
    afterEach(async () => {
      await drop()
    })

    const projectKey = 'p'

    const session = async (id: string): Promise<void> => {
      await db.sql`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
        VALUES (${id}, 'mcp', 'agent', 'a', 'a', 'agent', 'a', 'idle', 10, 1) ON CONFLICT DO NOTHING`
    }

    /** Plaintext rows as before sealing: a uuid on every other entry. The session exists unless `orphan`. */
    async function plain(sessionId: string, n: number, subpath = '', orphan = false): Promise<SessionStoreEntry[]> {
      if (!orphan) await session(sessionId)
      const entries: SessionStoreEntry[] = Array.from({ length: n }, (_, i) =>
        i % 2 === 0 ? { type: 'user', uuid: randomUUID(), text: `t${i}` } : { type: 'last-prompt', text: `t${i}` },
      )
      for (const e of entries) {
        await db.sql`
          INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry, entry_type)
          VALUES (${projectKey}, ${sessionId}, ${subpath}, ${typeof e.uuid === 'string' ? e.uuid : null}, ${JSON.stringify(e)}, 'free-text secret')`
      }
      return entries
    }
    const counts = async () =>
      (
        await db.sql<{ plain: string; sealed: string }[]>`
          SELECT count(*) FILTER (WHERE entry IS NOT NULL)::text AS plain, count(*) FILTER (WHERE sealed IS NOT NULL)::text AS sealed
          FROM ai_session_entries`
      )[0]!

    it('seals plaintext rows, keeps load identical, and a second run changes nothing', async () => {
      const a = randomUUID()
      const b = randomUUID()
      const ea = await plain(a, 5)
      const sub = await plain(a, 2, 'subagents/x')
      const eb = await plain(b, 3)
      expect(await sealBackfill(db.sql, keys)).toEqual({ sealed: 10, deleted: 0, malformed: 0 })
      expect(await counts()).toEqual({ plain: '0', sealed: '10' })
      expect(await store.load({ projectKey, sessionId: a })).toEqual(ea)
      expect(await store.load({ projectKey, sessionId: a, subpath: 'subagents/x' })).toEqual(sub)
      expect(await store.load({ projectKey, sessionId: b })).toEqual(eb)
      const types = await db.sql`SELECT DISTINCT entry_type FROM ai_session_entries ORDER BY 1`
      expect(types.map((r) => r.entry_type)).toEqual(['last-prompt', 'user'])
      const before = await db.sql`SELECT id, sealed FROM ai_session_entries ORDER BY id`
      expect(await sealBackfill(db.sql, keys)).toEqual({ sealed: 0, deleted: 0, malformed: 0 })
      expect(await db.sql`SELECT id, sealed FROM ai_session_entries ORDER BY id`).toEqual(before)
    })

    it('leaves already sealed rows alone and appends next to backfilled ones', async () => {
      const id = randomUUID()
      const old = await plain(id, 2)
      await store.append({ projectKey, sessionId: id }, [{ type: 'user', uuid: randomUUID() }])
      expect(await sealBackfill(db.sql, keys)).toEqual({ sealed: 2, deleted: 0, malformed: 0 })
      expect(await store.load({ projectKey, sessionId: id })).toHaveLength(old.length + 1)
    })

    it('deletes the plaintext rows of a forgotten session and never seals them', async () => {
      const gone = randomUUID()
      const kept = randomUUID()
      await plain(gone, 3)
      await forgetSubject({ sql: db.sql, keys }, `session-${gone}`)
      // forgetSubject deletes the entries too; they are back, as a plaintext copy that survived (#3316).
      await plain(gone, 3)
      await session(gone)
      const ek = await plain(kept, 2)
      expect(await sealBackfill(db.sql, keys)).toEqual({ sealed: 2, deleted: 3, malformed: 0 })
      expect((await db.sql`SELECT 1 FROM ai_session_entries WHERE session_id = ${gone}`).length).toBe(0)
      expect((await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${`session-${gone}`}`).length).toBe(0)
      expect(await store.load({ projectKey, sessionId: kept })).toEqual(ek)
    })

    it('deletes the rows of a session with no ai_sessions row and makes no key for it', async () => {
      const orphan = randomUUID()
      const kept = randomUUID()
      await plain(orphan, 3, '', true)
      await plain(kept, 2)
      expect(await sealBackfill(db.sql, keys)).toEqual({ sealed: 2, deleted: 3, malformed: 0 })
      expect((await db.sql`SELECT 1 FROM ai_session_entries WHERE session_id = ${orphan}`).length).toBe(0)
      expect((await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${`session-${orphan}`}`).length).toBe(0)
    })

    it('seals a row that is not JSON as it is, and load fails on it as on the plaintext row', async () => {
      const id = randomUUID()
      const legacy = randomUUID()
      for (const sid of [id, legacy]) {
        await plain(sid, 2)
        await db.sql`INSERT INTO ai_session_entries (project_key, session_id, subpath, entry) VALUES (${projectKey}, ${sid}, '', 'not json kumquat {')`
      }
      await expect(store.load({ projectKey, sessionId: legacy })).rejects.toBeInstanceOf(SyntaxError)
      await plain(randomUUID(), 3)
      expect(await sealBackfill(db.sql, keys, { batchSize: 2 })).toEqual({ sealed: 9, deleted: 0, malformed: 2 })
      expect(await counts()).toEqual({ plain: '0', sealed: '9' })
      const clean = await db.sql`SELECT 1 FROM ai_session_entries WHERE position('kumquat' in encode(sealed, 'escape')) > 0`
      expect(clean).toHaveLength(0)
      expect((await db.sql`SELECT entry_type FROM ai_session_entries WHERE entry_type IS NULL`).length).toBe(2)
      await expect(store.load({ projectKey, sessionId: id })).rejects.toBeInstanceOf(SyntaxError)
      expect(await sealBackfill(db.sql, keys)).toEqual({ sealed: 0, deleted: 0, malformed: 0 })
    })

    it('crosses batch boundaries', async () => {
      const id = randomUUID()
      const other = randomUUID()
      const e = await plain(id, 7)
      const o = await plain(other, 4)
      expect(await sealBackfill(db.sql, keys, { batchSize: 3 })).toEqual({ sealed: 11, deleted: 0, malformed: 0 })
      expect(await store.load({ projectKey, sessionId: id })).toEqual(e)
      expect(await store.load({ projectKey, sessionId: other })).toEqual(o)
    })

    it('seals more than the default 500 rows in batches', async () => {
      const id = randomUUID()
      await session(id)
      await db.sql`
        INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry)
        SELECT ${projectKey}, ${id}, '', NULL, '{"type":"user","n":' || g || '}' FROM generate_series(1, 1203) g`
      expect(await sealBackfill(db.sql, keys)).toEqual({ sealed: 1203, deleted: 0, malformed: 0 })
      const loaded = await store.load({ projectKey, sessionId: id })
      expect(loaded).toHaveLength(1203)
      expect(loaded![1202]).toEqual({ type: 'user', n: 1203 })
    })

    it('does not double-seal when two runs are concurrent', async () => {
      const id = randomUUID()
      const e = await plain(id, 9)
      const other = testPayloadKeys(db.sql)
      const results = await Promise.all([sealBackfill(db.sql, keys, { batchSize: 2 }), sealBackfill(db.sql, other, { batchSize: 2 })])
      expect(results.reduce((n, r) => n + r.sealed, 0)).toBe(9)
      expect(await store.load({ projectKey, sessionId: id })).toEqual(e)
    })

    it("still gives warm.ts's cost-state baseline from a backfilled row", async () => {
      const id = randomUUID()
      await session(id)
      await db.sql`
        INSERT INTO ai_session_entries (project_key, session_id, subpath, entry)
        VALUES (${projectKey}, ${id}, '', ${JSON.stringify({ type: 'user', text: 'x' })}),
               (${projectKey}, ${id}, '', ${JSON.stringify({ type: 'cost-state', totalCostUSD: 0.42 })})`
      await sealBackfill(db.sql, keys)
      const last = await store.lastOfType({ projectKey: '', sessionId: id }, 'cost-state')
      expect(costStateUsd(JSON.stringify(last), () => {})).toBe(0.42)
    })
  },
)
