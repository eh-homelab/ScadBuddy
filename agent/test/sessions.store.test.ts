import { randomUUID } from 'node:crypto'
import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { type PgPayloadKeys, SubjectForgotten } from '../src/temporal/payloadCodec.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { testPayloadKeys } from './support/payloadKeys.js'

describe.skipIf(!TEST_DATABASE_URL)(
  `PostgresSessionStore${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let store: PostgresSessionStore
    let keys: PgPayloadKeys

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      keys = testPayloadKeys(db.sql)
      store = new PostgresSessionStore(db.sql, { keys })
    })
    afterEach(async () => {
      await drop()
    })

    const projectKey = '-var-lib-scadbuddy-agent-work-sessions-x'
    const entry = (type: string, extra: Record<string, unknown> = {}): SessionStoreEntry => ({
      type,
      uuid: randomUUID(),
      timestamp: '2026-09-27T12:00:00.000Z',
      ...extra,
    })

    it('returns null for a session that was never written', async () => {
      expect(await store.load({ projectKey, sessionId: randomUUID() })).toBeNull()
      expect(await store.exists(randomUUID())).toBe(false)
    })

    it('round-trips entries deep-equal and in append order, across batches', async () => {
      const sessionId = randomUUID()
      const first = [
        { type: 'queue-operation', operation: 'enqueue', timestamp: '2026-09-27T12:00:00.000Z', sessionId },
        entry('user', { message: { role: 'user', content: [{ type: 'text', text: 'hi' }] }, parentUuid: null }),
        // Characters jsonb would refuse or normalise.
        entry('user', { message: { content: 'nul \u0000 byte, emoji 🧩, "quotes", \\ back' }, n: 1.5, z: null }),
      ]
      const second = [entry('assistant', { message: { content: [{ type: 'text', text: 'hello' }] } }), entry('cost-state')]
      await store.append({ projectKey, sessionId }, first)
      await store.append({ projectKey, sessionId }, second)
      expect(await store.load({ projectKey, sessionId })).toEqual([...first, ...second])
      expect(await store.exists(sessionId)).toBe(true)
    })

    it('treats uuid as an idempotency key and appends entries without one every time', async () => {
      const sessionId = randomUUID()
      const withId = entry('user')
      const noId: SessionStoreEntry = { type: 'last-prompt', text: 'again' }
      await store.append({ projectKey, sessionId }, [withId, noId])
      await store.append({ projectKey, sessionId }, [withId, noId])
      expect(await store.load({ projectKey, sessionId })).toEqual([withId, noId, noId])
    })

    it('keeps subagent transcripts apart and lists them as subkeys', async () => {
      const sessionId = randomUUID()
      const main = entry('user')
      const sub = entry('assistant')
      await store.append({ projectKey, sessionId }, [main])
      await store.append({ projectKey, sessionId, subpath: 'subagents/agent-1' }, [sub])
      expect(await store.load({ projectKey, sessionId })).toEqual([main])
      expect(await store.load({ projectKey, sessionId, subpath: 'subagents/agent-1' })).toEqual([sub])
      expect(await store.listSubkeys({ projectKey, sessionId })).toEqual(['subagents/agent-1'])
    })

    it('finds a session whatever projectKey the caller derives (per-session cwd, resume elsewhere)', async () => {
      const sessionId = randomUUID()
      const e = entry('user')
      await store.append({ projectKey: '-replica-a-work-sessions-1', sessionId }, [e])
      expect(await store.load({ projectKey: '-replica-b-work-sessions-2', sessionId })).toEqual([e])
    })

    it('lists a project’s sessions with integer millisecond mtimes', async () => {
      const a = randomUUID()
      const b = randomUUID()
      const before = Date.now() - 5_000
      await store.append({ projectKey, sessionId: a }, [entry('user')])
      await store.append({ projectKey, sessionId: b }, [entry('user')])
      await store.append({ projectKey: 'other', sessionId: randomUUID() }, [entry('user')])
      const listed = await store.listSessions(projectKey)
      expect(listed.map((s) => s.sessionId).sort()).toEqual([a, b].sort())
      for (const s of listed) {
        expect(Number.isInteger(s.mtime)).toBe(true)
        expect(s.mtime).toBeGreaterThan(before)
      }
    })

    it('deletes a main transcript with its subagents, or one subpath alone', async () => {
      const sessionId = randomUUID()
      await store.append({ projectKey, sessionId }, [entry('user')])
      await store.append({ projectKey, sessionId, subpath: 'subagents/a' }, [entry('user')])
      await store.append({ projectKey, sessionId, subpath: 'subagents/b' }, [entry('user')])
      await store.delete({ projectKey, sessionId, subpath: 'subagents/a' })
      expect(await store.listSubkeys({ projectKey, sessionId })).toEqual(['subagents/b'])
      await store.delete({ projectKey, sessionId })
      expect(await store.load({ projectKey, sessionId })).toBeNull()
      expect(await store.listSubkeys({ projectKey, sessionId })).toEqual([])
    })

    describe('sealing', () => {
      it('holds no plaintext in the row', async () => {
        const sessionId = randomUUID()
        await store.append({ projectKey, sessionId }, [entry('user', { text: 'kumquat' }), { type: 'last-prompt', text: 'kumquat' }])
        const rows = await db.sql<{ plain: boolean; clean: boolean; entry_type: string }[]>`
          SELECT entry IS NULL AS plain, position('kumquat' in encode(sealed, 'escape')) = 0 AS clean, entry_type
          FROM ai_session_entries WHERE session_id = ${sessionId} ORDER BY id`
        expect(rows.map((r) => [r.plain, r.clean, r.entry_type])).toEqual([
          [true, true, 'user'],
          [true, true, 'last-prompt'],
        ])
      })

      it('cannot load once its subject is forgotten', async () => {
        const sessionId = randomUUID()
        await store.append({ projectKey, sessionId }, [entry('user')])
        await db.sql`DELETE FROM ai_payload_keys WHERE subject = ${`session-${sessionId}`}`
        keys.forget(`session-${sessionId}`)
        await expect(store.load({ projectKey, sessionId })).rejects.toBeInstanceOf(SubjectForgotten)
      })

      it('reads legacy plaintext rows next to sealed ones', async () => {
        const sessionId = randomUUID()
        const old = entry('user', { text: 'before' })
        await db.sql`
          INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry)
          VALUES (${projectKey}, ${sessionId}, '', ${old.uuid as string}, ${JSON.stringify(old)})`
        const next = entry('assistant')
        await store.append({ projectKey, sessionId }, [next])
        expect(await store.load({ projectKey, sessionId })).toEqual([old, next])
      })

      it('reads legacy plaintext without keys, and refuses to append, naming the key file', async () => {
        const sessionId = randomUUID()
        const old = entry('user')
        await db.sql`
          INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry)
          VALUES (${projectKey}, ${sessionId}, '', ${old.uuid as string}, ${JSON.stringify(old)})`
        const bare = new PostgresSessionStore(db.sql)
        expect(await bare.load({ projectKey, sessionId })).toEqual([old])
        await expect(bare.append({ projectKey, sessionId }, [entry('user')])).rejects.toThrow(/SCADBUDDY_SECRET_KEY_FILE/)
        expect(await bare.load({ projectKey, sessionId })).toEqual([old])
      })

      it('binds a row to its place: two rows without a uuid cannot be swapped', async () => {
        const sessionId = randomUUID()
        await store.append({ projectKey, sessionId }, [{ type: 'last-prompt', text: 'one' }, { type: 'last-prompt', text: 'two' }])
        const ids = (await db.sql<{ id: string }[]>`SELECT id::text FROM ai_session_entries WHERE session_id = ${sessionId} ORDER BY id::bigint`).map((r) => r.id)
        await db.sql`UPDATE ai_session_entries SET id = -id WHERE id = ${ids[0]!}::bigint`
        await db.sql`UPDATE ai_session_entries SET id = ${ids[0]!}::bigint WHERE id = ${ids[1]!}::bigint`
        await db.sql`UPDATE ai_session_entries SET id = ${ids[1]!}::bigint WHERE id = -${ids[0]!}::bigint`
        await expect(store.load({ projectKey, sessionId })).rejects.toThrow()
      })

      it('binds a row to its session: one row moved to another session is refused', async () => {
        const sessionId = randomUUID()
        const other = randomUUID()
        await store.append({ projectKey, sessionId }, [entry('user'), entry('assistant')])
        await store.append({ projectKey, sessionId: other }, [entry('user')])
        await db.sql`
          UPDATE ai_session_entries SET session_id = ${other}
          WHERE id = (SELECT min(id) FROM ai_session_entries WHERE session_id = ${sessionId})`
        await expect(store.load({ projectKey, sessionId: other })).rejects.toThrow()
      })

      it('binds a row without a uuid by its id behind a #, so a uuid equal to that id is another binding', async () => {
        const sessionId = randomUUID()
        await store.append({ projectKey, sessionId }, [{ type: 'last-prompt', text: 'x' }])
        await db.sql`UPDATE ai_session_entries SET uuid = id::text WHERE session_id = ${sessionId}`
        await expect(store.load({ projectKey, sessionId })).rejects.toThrow()
      })

      it('stores entry_type only for a short token-like type', async () => {
        const sessionId = randomUUID()
        await store.append({ projectKey, sessionId }, [
          { type: 'x'.repeat(10000) },
          { type: 'Has Space' },
          { type: 'UPPER' },
          { type: 'cost-state' },
        ])
        const rows = await db.sql<{ entry_type: string | null }[]>`SELECT entry_type FROM ai_session_entries WHERE session_id = ${sessionId} ORDER BY id`
        expect(rows.map((r) => r.entry_type)).toEqual([null, null, null, 'cost-state'])
        expect(await store.lastOfType({ projectKey, sessionId }, 'cost-state')).toEqual({ type: 'cost-state' })
      })

      it('refuses to append, and to look up the last of a type, once the subject is forgotten', async () => {
        const sessionId = randomUUID()
        await store.append({ projectKey, sessionId }, [{ type: 'cost-state', totalCostUSD: 1 }])
        const subject = `session-${sessionId}`
        await db.sql`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
        await db.sql`INSERT INTO ai_forgotten_subjects (subject) VALUES (${subject})`
        keys.forget(subject)
        await expect(store.append({ projectKey, sessionId }, [entry('user')])).rejects.toBeInstanceOf(SubjectForgotten)
        await expect(store.lastOfType({ projectKey, sessionId }, 'cost-state')).rejects.toBeInstanceOf(SubjectForgotten)
      })

      it('keeps a mixed batch in order', async () => {
        const sessionId = randomUUID()
        const batch = [entry('user'), { type: 'last-prompt', text: 'x' }, entry('assistant'), { type: 'cost-state', totalCostUSD: 1 }]
        await store.append({ projectKey, sessionId }, batch)
        expect(await store.load({ projectKey, sessionId })).toEqual(batch)
      })

      it('keeps append-call order when calls overlap, the key lookup included', async () => {
        const sessionId = randomUUID()
        const batches = Array.from({ length: 8 }, (_, i) => [{ type: 'last-prompt', text: String(i) }, entry('user', { text: String(i) })])
        await Promise.all(batches.map((batch) => store.append({ projectKey, sessionId }, batch)))
        expect(await store.load({ projectKey, sessionId })).toEqual(batches.flat())
      })

      it('finds the last entry of a type, sealed or legacy', async () => {
        const sessionId = randomUUID()
        await db.sql`
          INSERT INTO ai_session_entries (project_key, session_id, subpath, entry)
          VALUES (${projectKey}, ${sessionId}, '', ${JSON.stringify({ type: 'cost-state', totalCostUSD: 1 })})`
        expect(await store.lastOfType({ projectKey, sessionId }, 'cost-state')).toEqual({ type: 'cost-state', totalCostUSD: 1 })
        await store.append({ projectKey, sessionId }, [{ type: 'cost-state', totalCostUSD: 2 }, entry('user')])
        expect(await store.lastOfType({ projectKey, sessionId }, 'cost-state')).toEqual({ type: 'cost-state', totalCostUSD: 2 })
        expect(await store.lastOfType({ projectKey, sessionId }, 'summary')).toBeNull()
      })
    })
  },
)
