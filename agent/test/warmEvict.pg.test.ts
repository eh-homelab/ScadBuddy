import { randomBytes, randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CredentialStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { PluginStore } from '../src/plugins/registry.js'
import { kekFromBase64 } from '../src/secrets.js'
import { forgetSubject } from '../src/sessions/forget.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { evictOnNotify, WARM_EVICT_CHANNEL } from '../src/sessions/warmEvict.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// The warm process cache's eviction notifications (unified runtime spec
// 2026-10-13 §5.3, warmEvict.ts): what each writer sends on the channel, and
// what the LISTEN consumer does with a payload. The channel is per database,
// so a `*` here may also be another test's: the assertions look for the
// payload expected, never for silence.

describe('evictOnNotify', () => {
  it('evicts every entry on `*` and one session on its id, and ignores anything else', () => {
    const evict = vi.fn()
    const notified = evictOnNotify({ evict })
    const id = randomUUID()
    notified('*')
    notified(id)
    notified('')
    notified('all')
    notified(`${id}; DROP TABLE ai_sessions`)
    expect(evict.mock.calls).toEqual([
      ['*', 'notified'],
      [id, 'notified'],
    ])
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`warm eviction notifications${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager | undefined
  const kek = kekFromBase64(randomBytes(32).toString('base64'))
  let received: string[]
  let unlisten: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    received = []
    const listener = await db.sql.listen(WARM_EVICT_CHANNEL, (payload) => {
      received.push(payload)
    })
    unlisten = () => listener.unlisten()
  })
  afterEach(async () => {
    await m?.warm.close()
    m = undefined
    await unlisten()
    await drop()
  })

  it('a credential write tells every replica to evict everything', async () => {
    const store = new CredentialStore(db.sql)
    await store.put({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-first' }, kek)
    await expect.poll(() => received).toContain('*')
  })

  it('a plugin registry write tells every replica to evict everything', async () => {
    const store = new PluginStore(db.sql)
    await store.create({ name: 'mem', url: 'https://hs.example/mcp' }, kek)
    await expect.poll(() => received).toContain('*')
  })

  it('marking a session done evicts its process here and names it to every replica', async () => {
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    expect(m.warm.size()).toBe(1)
    await m.edits.update(session.id, browser, { done: true })
    expect(m.warm.size()).toBe(0)
    await expect.poll(() => received).toContain(session.id)
  })

  it('archiving a session does the same', async () => {
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    expect(m.warm.size()).toBe(1)
    await m.edits.update(session.id, browser, { archived: true })
    expect(m.warm.size()).toBe(0)
    await expect.poll(() => received).toContain(session.id)
  })

  it('a handoff evicts the process here and names the session to every replica', async () => {
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
    const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
    await turn!.done
    expect(m.warm.size()).toBe(1)
    await m.handoff(session.id, browser, browser)
    expect(m.warm.size()).toBe(0)
    await expect.poll(() => received).toContain(session.id)
  })

  it('forgetting a session names it to every replica', async () => {
    const id = randomUUID()
    await forgetSubject({ sql: db.sql }, `session-${id}`)
    await expect.poll(() => received).toContain(id)
  })

  it('a replica that hears its session named lets the process go', async () => {
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
    const other = await db.sql.listen(WARM_EVICT_CHANNEL, evictOnNotify(m.warm))
    try {
      const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
      await turn!.done
      expect(m.warm.size()).toBe(1)
      await db.sql`SELECT pg_notify(${WARM_EVICT_CHANNEL}, ${session.id})`
      await expect.poll(() => m?.warm.size()).toBe(0)
    } finally {
      await other.unlisten()
    }
  })
})
