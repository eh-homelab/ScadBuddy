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

// An AgentSession placement's spend (sessions/turnHost.ts) over a `cost-state` its process
// carries in. The real SDK loads the transcript a resume restores from through the session
// store before Claude Code starts (sdk.mjs `sessionStore.load({projectKey, sessionId: resume})`),
// and the CLI restores that cost-state's total (test/warm.pg.test.ts "counts a resumed process
// from the total its transcript carried in": 2c after a first c; harness/fallback.ts, measured
// on 2.1.283 and 2.1.287). The spawn's baseline is that loaded cost-state (warm.ts
// `MirroredTranscript.loadedCostUsd`). Until 627eb4f05 it was a read of the table at spawn,
// before the load: a cost-state that landed in between (a previous process's last append) was
// restored by the process but not in its baseline, and its turn was over-priced by it. The
// scripted runner's totals are the script's (support/sessions.ts `costUsd`); it loads as the
// SDK does, except in the last case, a guard only: a resume that loads nothing is not a state
// the real SDK produces, and that case is not a reproduction of #3695.

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
   * A session whose earlier placement spent R (on the row), its transcript one user entry, then a
   * placement of its turn here whose process reports `reported` as its total. `carried`: the
   * cost-state of R is in the transcript `before` the spawn, lands `between` the spawn's start
   * and the process's load, or is not there (`none`).
   */
  async function place(reported: number, { carried, loads = true }: { carried: 'before' | 'between' | 'none'; loads?: boolean }) {
    let id = ''
    const store = new PostgresSessionStore(db.sql, { keys: testPayloadKeys(db.sql) })
    const costState = () => store.append({ projectKey: '', sessionId: id }, [{ type: 'cost-state', totalCostUSD: R }])
    const scripted = scriptedRunner(() => ({ reply: 'carried on', costUsd: reported }), {
      loads,
      ...(carried === 'between' ? { beforeLoad: costState } : {}),
    })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scripted.runner, audit: new AuditLog({ sql: db.sql }) })
    const { session } = await m.start(browser, { origin: 'chat' })
    id = session.id
    await store.append({ projectKey: '', sessionId: id }, [{ type: 'user', uuid: randomUUID(), message: { role: 'user', content: 'outlast the restart' } }])
    if (carried === 'before') await costState()
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

  // Each of the three places on its own (soft): the row, the audit row, every increment.
  const pricedAtR = ({ result, costUsd, audit, deltas }: Awaited<ReturnType<typeof place>>) => {
    expect(result).toMatchObject({ status: 'done' })
    expect.soft(costUsd).toBeCloseTo(2 * R, 12)
    expect.soft(audit).toEqual([expect.closeTo(R, 12)])
    expect.soft(deltas.every((d) => d >= 0)).toBe(true)
  }

  it('a cost-state that lands after the spawn and before the load: the process restores it, and its turn is priced past it, R', async () => {
    const placed = await place(2 * R, { carried: 'between' })
    expect(placed.runs[0]!.resume).toBeDefined()
    pricedAtR(placed)
  })

  it('a cost-state there before the spawn: the process restores it, and its turn is priced past it, R', async () => {
    pricedAtR(await place(2 * R, { carried: 'before' }))
  })

  it('no cost-state: the process counts from 0, R', async () => {
    pricedAtR(await place(R, { carried: 'none' }))
  })

  it('guard, not a state the real SDK produces: a resume that loads nothing is priced at what it reports, R', async () => {
    pricedAtR(await place(R, { carried: 'before', loads: false }))
  })
})
