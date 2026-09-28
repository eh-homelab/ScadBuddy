import { randomUUID } from 'node:crypto'
import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

describe.skipIf(!TEST_DATABASE_URL)(
  `PostgresSessionStore${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let store: PostgresSessionStore

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      store = new PostgresSessionStore(db.sql)
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
  },
)
