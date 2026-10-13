import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, type Throwaway, throwawayDatabase } from './support/postgres.js'
import type { Sql } from 'postgres'
import { warmHarness } from './support/warm.js'

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** A Postgres client whose next transcript-tail read waits until the test lets it go. */
function gatedSql(real: Sql) {
  let gate: { reached: () => void; release: Promise<void> } | undefined
  const sql = ((strings: TemplateStringsArray, ...values: never[]) => {
    const held = gate
    if (held && strings[0]?.trim().startsWith('SELECT uuid FROM ai_session_entries')) {
      gate = undefined
      held.reached()
      return held.release.then(() => real(strings, ...values))
    }
    return real(strings, ...values)
  }) as unknown as Sql
  const hold = () => {
    let reached!: () => void
    let release!: () => void
    const at = new Promise<void>((r) => (reached = r))
    gate = { reached, release: new Promise<void>((r) => (release = r)) }
    return { reached: at, release }
  }
  return { sql, hold }
}

// WarmSessions' transcript tail and budget rules (spec 2026-10-13 §5.3 "Reuse
// rule"), against the bundled Claude Code mirroring into ai_session_entries.

let skip: string | undefined
try {
  bundledCliPath()
} catch (err) {
  skip = (err as Error).message
}
if (!TEST_DATABASE_URL) skip ??= `${TEST_DATABASE_URL_ENV} is not set`

describe.skipIf(skip !== undefined)(`WarmSessions' tail and budget rules${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Throwaway
  let h: Awaited<ReturnType<typeof warmHarness>> | undefined
  beforeEach(async () => {
    db = await throwawayDatabase()
  })
  afterEach(async () => {
    await h?.close()
    h = undefined
    await db.drop()
  })

  it('recycles when the transcript tail moved', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    const one = await h.turn(id, h.assembled(), 1)
    // A turn ran elsewhere (a fallback, another replica): the transcript moved past this process.
    await h.store.append({ projectKey: 'elsewhere', sessionId: id }, [{ type: 'user', uuid: randomUUID() }])
    const two = await h.turn(id, h.assembled(), 1 - one.result.total_cost_usd)
    expect(two).toMatchObject({ outcome: 'recycle', reason: 'tail' })
    expect(h.spawns).toHaveLength(2)
  }, 60_000)

  it('does not count its own late transcript writes as a moved tail', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    let spent = 0
    for (let i = 0; i < 3; i++) {
      const t = await h.turn(id, h.assembled(), 1 - spent)
      spent = t.result.total_cost_usd
      expect(t.outcome).toBe(i === 0 ? 'miss' : 'hit')
    }
    expect(h.spawns).toHaveLength(1)
  }, 60_000)

  it('keeps the entry it is checking from an idle expiry during the tail read', async () => {
    let now = 1_000
    const gated: { hold?: ReturnType<typeof gatedSql>['hold'] } = {}
    h = await warmHarness(db, {
      idleMs: 200,
      now: () => now,
      sql: (real) => {
        const g = gatedSql(real)
        gated.hold = g.hold
        return g.sql
      },
    })
    const id = h.newSession()
    const one = await h.turn(id, h.assembled(), 1)
    await sleep(300) // the process's late transcript writes land
    const held = gated.hold!()
    const acquiring = h.warm.acquire(id, h.assembled(), h.credential(), 1 - one.result.total_cost_usd)
    await held.reached
    // Idle by the clock, and the idle timer fires while the read waits.
    now += 1_000
    await sleep(500)
    held.release()
    const two = await h.run(await acquiring)
    expect(two.outcome).toBe('hit')
    expect(two.result.subtype).toBe('success')
    expect(h.warm.size()).toBe(1)
    expect(h.spawns).toHaveLength(1)
  }, 60_000)

  it('recycles an entry evicted during the tail read', async () => {
    const gated: { hold?: ReturnType<typeof gatedSql>['hold'] } = {}
    h = await warmHarness(db, {
      sql: (real) => {
        const g = gatedSql(real)
        gated.hold = g.hold
        return g.sql
      },
    })
    const id = h.newSession()
    const first = h.assembled()
    const one = await h.turn(id, first, 1)
    await sleep(300)
    const held = gated.hold!()
    const acquiring = h.warm.acquire(id, h.assembled(), h.credential(), 1 - one.result.total_cost_usd)
    await held.reached
    h.warm.evict(id, 'the credential changed')
    held.release()
    const two = await h.run(await acquiring)
    expect(two).toMatchObject({ outcome: 'recycle', reason: 'evicted' })
    expect(two.result.subtype).toBe('success')
    expect(h.spawns).toHaveLength(2)
    expect(first.release).toHaveBeenCalledTimes(1)
    expect(h.warm.size()).toBe(1)
  }, 60_000)

  it('recycles when another write landed before one of its own', async () => {
    let foreign: (() => Promise<void>) | undefined
    h = await warmHarness(db, { onRequest: () => foreign?.() ?? Promise.resolve() })
    const id = h.newSession()
    const one = await h.turn(id, h.assembled(), 1)
    // During turn two's model call, another process writes; this one's own writes follow it.
    foreign = async () => {
      foreign = undefined
      await h?.store.append({ projectKey: 'elsewhere', sessionId: id }, [{ type: 'user', uuid: randomUUID() }])
    }
    const two = await h.turn(id, h.assembled(), 1 - one.result.total_cost_usd)
    expect(two.outcome).toBe('hit')
    await sleep(300)
    const three = await h.turn(id, h.assembled(), 1 - two.result.total_cost_usd)
    expect(three).toMatchObject({ outcome: 'recycle', reason: 'tail' })
  }, 60_000)

  it('recycles when the remaining budget moved', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    const one = await h.turn(id, h.assembled(), 1)
    // A fork sibling spent, or the budget was raised: the remaining amount is not this process's cap minus its spend.
    const two = await h.turn(id, h.assembled(), 1 - one.result.total_cost_usd + 0.5)
    expect(two).toMatchObject({ outcome: 'recycle', reason: 'budget' })
    expect(h.spawns).toHaveLength(2)
  }, 60_000)

  it('counts a resumed process from the total its transcript carried in', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    const c = (await h.turn(id, h.assembled(), 1)).result.total_cost_usd
    // A streaming process writes its `cost-state` as it ends; the resume restores it.
    h.warm.evict(id, 'test')
    await expect
      .poll(async () => (await db.db.sql`SELECT 1 FROM ai_session_entries WHERE session_id = ${id} AND entry LIKE '%"cost-state"%'`).length)
      .toBeGreaterThan(0)
    const two = await h.turn(id, h.assembled(), 1 - c)
    expect(two.outcome).toBe('miss')
    // The resumed process's total holds the first process's spend too (§2.2).
    expect(two.result.total_cost_usd).toBeCloseTo(2 * c, 9)
    expect(two.entry.costAtSpawnUsd).toBeCloseTo(c, 9)
    const three = await h.turn(id, h.assembled(), 1 - 2 * c)
    expect(three.outcome).toBe('hit')
    expect(h.spawns).toHaveLength(2)
  }, 60_000)
})
