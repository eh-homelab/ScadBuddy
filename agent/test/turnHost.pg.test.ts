import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import { TurnHost } from '../src/sessions/turnHost.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// An AgentSession placement's spend (sessions/turnHost.ts) when the session's transcript ends
// in a `cost-state` the process it spawned may or may not count in its own total (#3695). The
// real CLI restores the cost-state of the transcript it resumes, which the SDK loads through the
// session store (sdk.mjs `sessionStore.load` before Claude Code starts; test/warm.pg.test.ts
// "counts a resumed process from the total its transcript carried in": 2c after a first c;
// harness/fallback.ts, measured on 2.1.283 and 2.1.287). Until #3695 the spawn read the baseline
// from the table apart from that load, so a process that carried nothing in, whose total is its
// own R, read as R carried plus nothing: its turn priced at 0. The scripted runner's totals are
// the script's (support/sessions.ts `costUsd`); `loads` says whether it loaded the transcript.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const R = 0.000105

describe.skipIf(skip !== undefined)(`a placement's spend over a carried cost-state${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    m?.abortAll()
    await m?.warm.close()
    await drop()
  })

  /** `sql`, recording the increments of every spend write of the session row (`cost_usd = cost_usd + …`). */
  function recording(sql: Sql, deltas: number[]): Sql {
    return new Proxy(sql, {
      apply(target, self, args: unknown[]) {
        const strings = args[0]
        if (Array.isArray(strings) && strings.join('').includes('SET cost_usd = cost_usd +')) deltas.push(Number(args[1]))
        return Reflect.apply(target, self, args)
      },
    })
  }

  /**
   * A session whose earlier placement spent R (on the row) and left a `cost-state` of R in the
   * transcript, then a placement of its turn here whose process reports `reported` as its total.
   */
  async function placeAfterCarried(reported: number, loads: boolean) {
    const scripted = scriptedRunner(() => ({ reply: 'carried on', costUsd: reported }), { loads })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scripted.runner, audit: new AuditLog({ sql: db.sql }) })
    const { session } = await m.start(browser, { origin: 'chat' })
    const id = session.id
    await new PostgresSessionStore(db.sql, { keys: testPayloadKeys(db.sql) }).append({ projectKey: '', sessionId: id }, [
      { type: 'user', uuid: randomUUID(), message: { role: 'user', content: 'outlast the restart' } },
      { type: 'cost-state', totalCostUSD: R },
    ])
    const turnId = randomUUID()
    await db.sql`
      UPDATE ai_sessions SET status = 'running', mode = 'durable', runtime = 'unified', durable_turn_id = ${turnId},
        cost_usd = ${R}, updated_at = now()
      WHERE id = ${id}`
    const deltas: number[] = []
    const host = new TurnHost({ core: m.turnCore, sql: recording(db.sql, deltas), images: new SessionBlobs(db.sql), hostQueue: 'agent-sessions.pod-a' })
    const result = await host.run(
      { sessionId: id, turnId, attempt: 'run:1', message: { turn_id: turnId, author: browser, text: 'outlast the restart', images: [], system_append: null } },
      { heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' },
    )
    const [row] = await db.sql<{ cost_usd: string }[]>`SELECT cost_usd FROM ai_sessions WHERE id = ${id}`
    const audit = await db.sql<{ cost_usd: string }[]>`SELECT cost_usd FROM ai_audit WHERE session_id = ${id} AND kind = 'turn'`
    await host.stop({ graceMs: 0, abortWaitMs: 0 })
    return { result, costUsd: Number(row!.cost_usd), audit: audit.map((a) => Number(a.cost_usd)), deltas, runs: scripted.runs }
  }

  it('a process that carried nothing in: its own R is its spend, 2R in all, no write negative', async () => {
    const { result, costUsd, audit, deltas, runs } = await placeAfterCarried(R, false)
    expect(runs[0]!.resume).toBeDefined()
    expect(result).toMatchObject({ status: 'done' })
    expect.soft(costUsd).toBeCloseTo(2 * R, 12)
    expect.soft(audit).toEqual([expect.closeTo(R, 12)])
    expect.soft(deltas.every((d) => d >= 0)).toBe(true)
  })

  it('a process that restored the carried total: what it added past it, R, 2R in all', async () => {
    const { result, costUsd, audit, deltas } = await placeAfterCarried(2 * R, true)
    expect(result).toMatchObject({ status: 'done' })
    expect.soft(costUsd).toBeCloseTo(2 * R, 12)
    expect.soft(audit).toEqual([expect.closeTo(R, 12)])
    expect.soft(deltas.every((d) => d >= 0)).toBe(true)
  })
})
