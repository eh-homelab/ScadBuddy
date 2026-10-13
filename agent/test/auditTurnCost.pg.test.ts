import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AuditLog, type AuditRecord } from '../src/audit/log.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, type FakeTurn, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #1922: each turn's cost is a `turn` row in the AI audit log, against Postgres
// with the real audit log. Cost is known per turn (the SDK result's
// `total_cost_usd`), so it is the turn's own share of the session's spend; a
// turn Claude Code never priced (no result, sessions/unpricedSpend.ts) is
// flagged `cost_priced: false`, with ScadBuddy's estimate of the request it
// was cut off in, never shown as a priced zero.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

/**
 * The cut-off request ScadBuddy prices, at claude-sonnet-4-5's $3/$15 per MTok:
 * 20k input ($0.06) and the 16 characters streamed, 4 output tokens at 4 a token.
 */
const stall = { model: 'claude-sonnet-4-5', usage: { input_tokens: 20_000 }, text: 'Once upon a time' }
const CUT = 0.06 + (4 * 15) / 1_000_000

describe.skipIf(skip !== undefined)(`turn cost in the audit log${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let audit: AuditLog
  let settings: SettingsStore
  let m: SessionManager
  let next: FakeTurn
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    audit = new AuditLog({ sql: db.sql, settings: () => settings, onError: (err) => failures.push(err) })
    settings = new SettingsStore(db.sql, audit)
    next = { reply: 'hello there', costUsd: 0.4 }
    const { runner } = scriptedRunner(() => next)
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, settings, audit })
  })
  afterEach(async () => {
    m.abortAll()
    expect(failures).toEqual([])
    await drop()
  })

  async function turnRows(sessionId: string): Promise<AuditRecord[]> {
    return (await audit.list({ kind: 'turn', sessionId })).entries.reverse()
  }

  async function stopMidReply(id: string, text: string) {
    const turn = await m.send(id, browser, text)
    for (let i = 0; i < 200; i++) {
      if ((await m.events.read(id)).some((e) => e.event.type === 'assistant.text.delta' && e.seq > 4)) break
      await new Promise((r) => setTimeout(r, 10))
    }
    expect(await m.interrupt(id, browser)).toBe(true)
    expect(await turn.done).toEqual({ kind: 'interrupted' })
  }

  it("records each turn's own cost, not the session's running total", async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    await turn!.done
    // A resumed query's total includes the earlier turns (manager.ts finish).
    next = { reply: 'bigger', costUsd: 0.65 }
    await (await m.send(session.id, browser, 'bigger')).done

    const rows = await turnRows(session.id)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({
      kind: 'turn',
      action: 'success',
      surface: 'harness',
      outcome: 'ok',
      actor: { kind: 'browser', id: 'browser' },
      session_id: session.id,
      cost_priced: true,
      cost_estimated_usd: 0,
    })
    expect(rows[0]!.turn_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(rows[0]!.cost_usd).toBeCloseTo(0.4, 10)
    expect(rows[1]!.cost_usd).toBeCloseTo(0.25, 10)
    expect(rows[1]!.turn_id).not.toBe(rows[0]!.turn_id)
  })

  it('records a turn that stopped on its budget as an error, with what it cost', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    next = { reply: 'too much', costUsd: 1.01, subtype: 'error_max_budget_usd' }
    await (await m.send(session.id, browser, 'go')).done
    const [row] = await turnRows(session.id)
    expect(row).toMatchObject({ action: 'error_max_budget_usd', outcome: 'error', cost_priced: true })
    expect(row!.cost_usd).toBeCloseTo(1.01, 10)
  })

  it('flags a turn stopped mid-reply with no result as unpriced, with the estimate it was charged', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    next = { stall }
    await stopMidReply(session.id, 'write a long essay')
    const [row] = await turnRows(session.id)
    expect(row).toMatchObject({ action: 'interrupted', outcome: 'refused', cost_priced: false })
    expect(row!.cost_usd).toBeCloseTo(CUT, 10)
    expect(row!.cost_estimated_usd).toBeCloseTo(CUT, 10)
  })

  it('keeps the estimate apart when the interrupt still ends in a result', async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    await turn!.done
    next = { stall, resultCostUsd: 0.4 }
    await stopMidReply(session.id, 'write a long essay')
    const rows = await turnRows(session.id)
    expect(rows[1]).toMatchObject({ action: 'interrupted', cost_priced: true })
    expect(rows[1]!.cost_usd).toBeCloseTo(CUT, 10)
    expect(rows[1]!.cost_estimated_usd).toBeCloseTo(CUT, 10)
  })

  it('flags a turn that failed before any result as unpriced, with its reason', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    next = { throws: 'Claude Code process exited with code 1' }
    await (await m.send(session.id, browser, 'go')).done
    const [row] = await turnRows(session.id)
    expect(row).toMatchObject({ action: 'failed', outcome: 'error', cost_usd: 0, cost_priced: false, cost_estimated_usd: 0 })
    expect(row!.detail).toMatch(/exited with code 1/)
  })

  it('leaves the cost fields empty on every other kind of row', async () => {
    await settings.set('anything', 1, { actor: browser, surface: 'http' })
    const [row] = (await audit.list({ kind: 'settings' })).entries
    expect(row).toMatchObject({ cost_usd: null, cost_priced: null, cost_estimated_usd: null })
  })
})
