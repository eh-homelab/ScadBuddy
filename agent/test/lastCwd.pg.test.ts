import { randomUUID } from 'node:crypto'
import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import type { PgPayloadKeys } from '../src/temporal/payloadCodec.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// `PostgresSessionStore.lastCwd` alone (#5867, the review of #5884): the directory the main
// transcript's newest entry ran in, which decides whether a turn is told its directory moved
// (sessions/directoryNote.ts). Its fallbacks: past the session's own newest rows, and through a
// fork's parent, or its copied range once that parent is forgotten (`inherited` rows).

describe.skipIf(!TEST_DATABASE_URL)(`lastCwd${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
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

  const session = async (id: string, fork?: { parent: string; cut: number }): Promise<void> => {
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut)
      VALUES (${id}, 'mcp', 'agent', 'a', 'a', 'agent', 'a', 'idle', 10, 1, ${fork?.parent ?? null}, ${fork?.cut ?? null})`
  }
  const user = (cwd?: string): SessionStoreEntry => ({ type: 'user', uuid: randomUUID(), ...(cwd === undefined ? {} : { cwd }) })
  const assistant = (cwd?: string): SessionStoreEntry => ({ type: 'assistant', uuid: randomUUID(), ...(cwd === undefined ? {} : { cwd }) })
  const append = (id: string, entries: SessionStoreEntry[]) => store.append({ projectKey: 'p', sessionId: id }, entries)

  it("is the newest entry's cwd among the session's own rows", async () => {
    const id = randomUUID()
    await session(id)
    await append(id, [user('/old'), assistant('/old'), { type: 'last-prompt' }, user('/new'), { type: 'cost-state' }])
    expect(await store.lastCwd(id)).toBe('/new')
  })

  it('reads the whole transcript when none of the newest 20 own rows names a cwd', async () => {
    const id = randomUUID()
    await session(id)
    await append(id, [user('/there'), ...Array.from({ length: 25 }, () => assistant())])
    expect(await store.lastCwd(id)).toBe('/there')
  })

  it('is undefined when no entry names one', async () => {
    const id = randomUUID()
    await session(id)
    await append(id, [user(), assistant()])
    expect(await store.lastCwd(id)).toBeUndefined()
  })

  it("is a fork's parent's through the cut before its first turn, its own after, and the copied range's once the parent is forgotten", async () => {
    const parent = randomUUID()
    await session(parent)
    await append(parent, [user('/parent'), assistant('/parent')])
    const child = randomUUID()
    await session(child, { parent, cut: 2 })
    // The parent goes on in another directory past the cut: the child reads none of it.
    await append(parent, [user('/parent-later')])
    expect(await store.lastCwd(child)).toBe('/parent')

    // Forgotten, the parent's range is the child's `inherited` rows, and the child has none of its own.
    const other = randomUUID()
    await session(other, { parent, cut: 2 })
    await forgetSubject({ sql: db.sql, keys }, `session-${parent}`)
    const rows = await db.sql<{ inherited: boolean }[]>`SELECT inherited FROM ai_session_entries WHERE session_id = ${other}`
    expect(rows.length).toBeGreaterThan(0)
    expect(rows.every((r) => r.inherited)).toBe(true)
    expect(await store.lastCwd(other)).toBe('/parent')

    // Its own first turn's rows come first from then on.
    await append(other, [user('/other'), assistant('/other')])
    expect(await store.lastCwd(other)).toBe('/other')
  })
})
