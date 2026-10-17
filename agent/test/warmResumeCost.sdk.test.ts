import type { SDKMessage, SDKResultMessage, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { HarnessRun } from '../src/harness/run.js'
import { warmAttempts } from '../src/sessions/warmTurn.js'
import { REPLY_COST_USD } from './support/fakeAnthropic.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, type Throwaway, throwawayDatabase } from './support/postgres.js'
import { warmHarness } from './support/warm.js'

// What a resumed warm process's first turn is priced at (#3695), against the
// bundled Claude Code and the fake endpoint. The resumed process's
// `total_cost_usd` holds the transcript's carried spend only when Claude Code
// restores the session's `cost-state`, and it does not restore every one it is
// handed: measured on Claude Code 2.1.296 (SDK 0.3.296), it restores the last
// `cost-state` whose `sessionId` is the resumed session's and that its schema
// accepts, and starts from 0 otherwise. Each case seeds a session with one real
// turn (its `cost-state` carries R, one reply's cost), rewrites that
// `cost-state`, resumes, and records the total the CLI reports. The resumed
// turn makes one request too, so its own spend equals R: a total of R alone
// does not tell "restored, spent 0" from "not restored, spent R" (`carries`).
// The price is `warmAttempts`' `spentUsd`, the `turnUsd` the manager adds to
// `ai_sessions.cost_usd` (manager.ts `finish`).

let skip: string | undefined
try {
  bundledCliPath()
} catch (err) {
  skip = (err as Error).message
}
if (!TEST_DATABASE_URL) skip ??= `${TEST_DATABASE_URL_ENV} is not set`

type CostState = SessionStoreEntry & { type: 'cost-state'; sessionId: string; totalCostUSD: number }

describe.skipIf(skip !== undefined)(`a resumed warm process's first turn${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Throwaway
  let h: Awaited<ReturnType<typeof warmHarness>>
  beforeEach(async () => {
    db = await throwawayDatabase()
    h = await warmHarness(db)
  })
  afterEach(async () => {
    await h.close()
    await db.drop()
  })

  /** A session with one finished turn on a process that has ended: its transcript holds the `cost-state` that process wrote. */
  async function seeded() {
    const id = h.newSession()
    const one = await h.turn(id, h.assembled(), 1)
    expect(one.result.total_cost_usd).toBeCloseTo(REPLY_COST_USD, 12)
    await h.warm.evict(id, 'test')
    await expect
      .poll(async () => (await db.db.sql`SELECT 1 FROM ai_session_entries WHERE session_id = ${id} AND entry_type = 'cost-state'`).length)
      .toBeGreaterThan(0)
    const state = (await h.store.lastOfType({ projectKey: '', sessionId: id }, 'cost-state')) as CostState
    expect(state).toMatchObject({ type: 'cost-state', sessionId: id, totalCostUSD: one.result.total_cost_usd })
    return { id, r: state.totalCostUSD, state }
  }

  /** The session's `cost-state` rows replaced by `entries`, appended in order (sealed, as the mirror writes them). */
  async function costStates(id: string, entries: SessionStoreEntry[]) {
    await db.db.sql`DELETE FROM ai_session_entries WHERE session_id = ${id} AND entry_type = 'cost-state'`
    if (entries.length > 0) await h.store.append({ projectKey: '', sessionId: id }, entries)
  }

  /** One turn on a fresh (resumed) process, as the manager runs it; what it reported and how it was priced. */
  async function resumedTurn(id: string) {
    const calls = h.fake.messageCalls().length
    const attempts = warmAttempts({
      warm: h.warm,
      sessionId: id,
      first: h.assembled(),
      assemble: async () => h.assembled(),
      stop: new AbortController().signal,
      acquired: (acquired) => expect(acquired.outcome).toBe('miss'),
      trace: { warm: (f) => f() },
    })
    const attempt = { prompt: 'hello again', credential: h.credential(), maxBudgetUsd: 1 } as Partial<HarnessRun> as HarnessRun
    let result: SDKResultMessage | undefined
    for await (const message of attempts.run(attempt) as AsyncIterable<SDKMessage>) if (message.type === 'result') result = message
    expect(result?.subtype).toBe('success')
    const own = (h.fake.messageCalls().length - calls) * REPLY_COST_USD
    return { total: result!.total_cost_usd, own, priced: attempts.spentUsd() }
  }

  it('restores a cost-state it accepts: the total is R plus the turn, and the turn is priced at its own spend', async () => {
    const { id, r } = await seeded()
    const turn = await resumedTurn(id)
    expect(turn.own).toBeCloseTo(r, 12)
    expect(turn.total).toBeCloseTo(r + turn.own, 12)
    expect(turn.priced).toBeCloseTo(turn.own, 12)
  }, 120_000)

  it('a cost-state with a field Claude Code does not know is still restored', async () => {
    const { id, r, state } = await seeded()
    await costStates(id, [{ ...state, laterField: 1 }])
    const turn = await resumedTurn(id)
    expect(turn.total).toBeCloseTo(r + turn.own, 12)
    expect(turn.priced).toBeCloseTo(turn.own, 12)
  }, 120_000)

  // The restore does not happen: the total is the turn's own (R), the turn costs R.
  const unrestored: [string, (state: CostState) => SessionStoreEntry[]][] = [
    ['no cost-state at all', () => []],
    ['a cost-state whose totalCostUSD is not a number', (state) => [{ ...state, totalCostUSD: String(state.totalCostUSD) }]],
    [
      'an older cost-state, without a field the schema now requires (totalAPIDurationWithoutRetries)',
      ({ totalAPIDurationWithoutRetries: _, ...older }) => [older],
    ],
    ['a cost-state with a negative duration', (state) => [{ ...state, totalToolDuration: -1 }]],
    ["another session's cost-state (its sessionId is not the resumed one's)", (state) => [{ ...state, sessionId: h.newSession() }]],
  ]
  for (const [name, rewrite] of unrestored) {
    it(`does not restore ${name}: the turn is priced at its own spend`, async () => {
      const { id, r, state } = await seeded()
      await costStates(id, rewrite(state))
      const turn = await resumedTurn(id)
      expect(turn.own).toBeCloseTo(r, 12)
      expect(turn.total).toBeCloseTo(turn.own, 12)
      expect(turn.priced).toBeCloseTo(turn.own, 12)
    }, 120_000)
  }

  it('restores the last cost-state it accepts, not the last one: an unacceptable later one is passed over', async () => {
    const { id, r, state } = await seeded()
    const later: SessionStoreEntry = { ...state, totalCostUSD: 2 * r }
    delete later.totalAPIDurationWithoutRetries
    await costStates(id, [state, later])
    const turn = await resumedTurn(id)
    expect(turn.total).toBeCloseTo(r + turn.own, 12)
    expect(turn.priced).toBeCloseTo(turn.own, 12)
  }, 120_000)
})
