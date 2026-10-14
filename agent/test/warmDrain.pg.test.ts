import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Database } from '../src/db.js'
import { userMessage } from '../src/harness/inbox.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import type { AssembledTurn } from '../src/sessions/turnRunner.js'
import { createTurnSlot } from '../src/sessions/turnSlot.js'
import { WarmSessions } from '../src/sessions/warm.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { scriptedRunner, tempPaths } from './support/sessions.js'
import { nextResult } from './support/warm.js'

// How the cache closes a process (sessions/warm.ts `drop`): a live one is let
// end on its own, so what Claude Code writes as it exits (`last-prompt`,
// `cost-state`; measured, DRAIN_MS) lands before the next process loads the
// transcript; a dead or aborted one is closed at once. Over the scripted
// process, which writes those on stdin EOF and nothing after `close()`, as the
// real one. The real Claude Code is test/warm.pg.test.ts.

const credential = { kind: 'anthropic_api_key' as const, secret: 'sk-ant-test' }

describe.skipIf(!TEST_DATABASE_URL)(`closing a warm process${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let warm: WarmSessions | undefined
  let store: PostgresSessionStore
  let scripted: ReturnType<typeof scriptedRunner>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new PostgresSessionStore(db.sql)
    scripted = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.02 }))
    warm = new WarmSessions({ sql: db.sql, maxEntries: 8, idleMs: 600_000, start: scripted.runner })
  })
  afterEach(async () => {
    await warm?.close()
    await drop()
  })

  async function assembled(): Promise<AssembledTurn> {
    return {
      run: { paths: await tempPaths(), sessionStore: store, maxTurns: 5 },
      candidates: [],
      fingerprintParts: {
        credentialId: 'c1',
        credentialUpdatedAt: '1',
        model: null,
        ownPlugin: false,
        plugins: [],
        packageHashes: [],
        allowedPackageHashes: [],
        tiers: null,
        owner: 'user:alice',
        systemAppend: '',
        disabledTools: [],
        raisedTiers: [],
        httpRequest: false,
        builtinTools: [],
        recallMaxQueryChars: null,
        transientRetries: null,
      },
      slot: createTurnSlot(),
      secrets: [],
      eventTierOf: () => undefined,
      shownTierOf: () => undefined,
      observe: () => Promise.resolve(),
      terminate: vi.fn(async () => {}),
      release: vi.fn(),
    }
  }

  const types = async (id: string) =>
    (await db.sql<{ entry: string }[]>`SELECT entry FROM ai_session_entries WHERE session_id = ${id} ORDER BY id`).map(
      (r) => (JSON.parse(r.entry) as { type: string }).type,
    )

  /** One turn: acquire, push, read to the result, settle. */
  async function turn(id: string, remainingUsd = 1) {
    const acquired = await warm!.acquire(id, await assembled(), credential, remainingUsd)
    acquired.entry.inbox.push(userMessage('hello'))
    const result = await nextResult(acquired.entry.iterator)
    await warm!.settle(acquired.entry, result)
    return { ...acquired, result }
  }

  it('a recycled process ends on its own first: its exit writes land, and the next one carries its total', async () => {
    const id = randomUUID()
    const one = await turn(id)
    expect(one.outcome).toBe('miss')
    // Another write moved the tail: the next turn recycles.
    await store.append({ projectKey: 'elsewhere', sessionId: id }, [{ type: 'user', uuid: randomUUID() }])
    const two = await turn(id, 1 - 0.02)
    expect(two).toMatchObject({ outcome: 'recycle', reason: 'tail' })
    expect(await types(id)).toEqual(['user', 'last-prompt', 'cost-state'])
    expect(two.entry.costAtSpawnUsd).toBeCloseTo(0.02, 10)
    expect(scripted.runs).toHaveLength(2)
  })

  it('an eviction lets the process end on its own, and settles once it is closed', async () => {
    const id = randomUUID()
    const one = await turn(id)
    await warm!.evict(id, 'the test')
    expect(warm!.size()).toBe(0)
    expect(one.entry.turn.release).toHaveBeenCalledTimes(1)
    expect(await types(id)).toEqual(['last-prompt', 'cost-state'])
  })

  it('a process the fallback left for another credential ends on its own; a dead one is closed at once', async () => {
    const left = randomUUID()
    const one = await turn(left)
    await warm!.abandon(one.entry, 'credential', 'drain')
    expect(await types(left)).toEqual(['last-prompt', 'cost-state'])

    const dead = randomUUID()
    const two = await turn(dead)
    await warm!.abandon(two.entry, 'the query threw')
    expect(await types(dead)).toEqual([])
    expect(warm!.size()).toBe(0)
  })

  it('a turn that pushed nothing leaves the entry idle: the next acquire hits', async () => {
    const id = randomUUID()
    const one = await turn(id)
    const held = await warm!.acquire(id, await assembled(), credential, 1 - 0.02)
    expect(held.outcome).toBe('hit')
    warm!.leave(held.entry)
    const next = await turn(id, 1 - 0.02)
    expect(next.outcome).toBe('hit')
    expect(next.entry).toBe(one.entry)
    expect(scripted.runs).toHaveLength(1)
  })

  it('a turn that pushed nothing into an entry evicted meanwhile closes it', async () => {
    const id = randomUUID()
    await turn(id)
    const held = await warm!.acquire(id, await assembled(), credential, 1 - 0.02)
    void warm!.evict(id, 'the credential changed')
    expect(warm!.size()).toBe(1)
    warm!.leave(held.entry)
    expect(warm!.size()).toBe(0)
  })
})
