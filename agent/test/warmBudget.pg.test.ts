import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import type { HarnessRun } from '../src/harness/run.js'
import { SETTING_SESSION_BUDGET_USD } from '../src/sessions/manager.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { agentA, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// One session's turns alternating between two replicas, each with a warm
// process of its own (spec 2026-10-13 §5.3 "budget", §6.6). Every spawn is
// capped at the budget less what the row says the session has spent; a process
// still warm on the other replica is recycled by the budget rule once the row
// moved; each turn's recorded cost is its own; and both replicas refuse a send
// once the budget is spent.

describe.skipIf(!TEST_DATABASE_URL)(`the budget across alternating replicas${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it('every spawn gets budget less the row’s spend; a warm process elsewhere is recycled; costs are per turn', async () => {
    const paths = await tempPaths()
    const settings = new SettingsStore(db.sql)
    await settings.set(SETTING_SESSION_BUDGET_USD, 0.1)
    // Each spawn serves one turn here. The real CLI reports total_cost_usd as the
    // transcript's last cost-state plus its own spend (spec §2.2); the totals are
    // given as the CLI would report them:
    //   A1: 0.03 (fresh)            B1: 0.03 (A1 still warm: no cost-state yet)
    //   A2: 0.03 carried + 0.03     B2: 0.03 carried (B1's, the last) + 0.03
    const totals = [0.03, 0.03, 0.06, 0.06]
    const seen: HarnessRun[] = []
    const next = (run: HarnessRun) => {
      if (!seen.includes(run)) seen.push(run)
      return { reply: 'ok', costUsd: totals[seen.indexOf(run)]! }
    }
    const a = scriptedRunner(next)
    const b = scriptedRunner(next)
    const A = manager({ sql: db.sql, paths, run: a.runner, settings })
    const B = manager({ sql: db.sql, paths, run: b.runner, settings })
    try {
      const { session } = await A.start(agentA, { origin: 'mcp' })
      const id = session.id
      const spend = async () => (await A.get(id, agentA)).costUsd

      const t1 = await (await A.send(id, agentA, 'one')).done
      expect(t1).toMatchObject({ kind: 'result', costUsd: expect.closeTo(0.03, 9) })
      expect(a.runs[0]!.maxBudgetUsd).toBeCloseTo(0.1, 9)
      expect(await spend()).toBeCloseTo(0.03, 9)

      const t2 = await (await B.send(id, agentA, 'two')).done
      expect(t2).toMatchObject({ kind: 'result', costUsd: expect.closeTo(0.06, 9) })
      expect(b.runs[0]!.maxBudgetUsd).toBeCloseTo(0.07, 9)
      expect(await spend()).toBeCloseTo(0.06, 9)
      // A1 is still warm, and has written no cost-state.
      expect(A.warm.size()).toBe(1)

      // Back on A: A1 is warm, but what the row has left (0.04) is not A1's cap less
      // its spend since it was spawned (0.07): recycled (warm.ts `mismatch`, budget).
      const t3 = await (await A.send(id, agentA, 'three')).done
      expect(t3).toMatchObject({ kind: 'result', costUsd: expect.closeTo(0.09, 9) })
      expect(a.runs).toHaveLength(2)
      expect(a.runs[1]!.maxBudgetUsd).toBeCloseTo(0.04, 9)
      expect(await spend()).toBeCloseTo(0.09, 9)

      // Back on B: B1 is warm, cap 0.07 less 0.03 = 0.04, not the 0.01 left: recycled.
      const t4 = await (await B.send(id, agentA, 'four')).done
      expect(t4).toMatchObject({ kind: 'result', costUsd: expect.closeTo(0.12, 9) })
      expect(b.runs).toHaveLength(2)
      expect(b.runs[1]!.maxBudgetUsd).toBeCloseTo(0.01, 9)

      await expect(A.send(id, agentA, 'five')).rejects.toMatchObject({ code: 'budget_exhausted' })
      await expect(B.send(id, agentA, 'five')).rejects.toMatchObject({ code: 'budget_exhausted' })

      // The transcript's cost-states are each recycled process's own total, which is
      // stale against the row; fine, since the baseline warm.ts reads (`restoredCostUsd`)
      // is the entry the process restored from, and a turn's cost is the difference.
      const entries = await new PostgresSessionStore(db.sql, { keys: testPayloadKeys(db.sql) }).load({ projectKey: '', sessionId: id })
      expect((entries ?? []).filter((e) => e.type === 'cost-state').map((e) => e.totalCostUSD)).toEqual([0.03, 0.03])
    } finally {
      await A.warm.close()
      await B.warm.close()
    }
  })
})
