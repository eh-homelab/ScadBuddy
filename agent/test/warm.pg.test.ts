import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, type Throwaway, throwawayDatabase } from './support/postgres.js'
import { warmHarness } from './support/warm.js'

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
