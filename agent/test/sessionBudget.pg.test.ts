import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { AuditLog, type AuditRecord } from '../src/audit/log.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { AGENT_ACTOR_HEADER } from '../src/harness/headlessBrowser.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { SESSION_LIMITS_PATH } from '../src/routes/sessionLimits.js'
import {
  SETTING_SESSION_BUDGET_USD,
  SETTING_SESSION_MAX_TURNS,
  type SessionManager,
} from '../src/sessions/manager.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, type FakeTurn, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #790: the session limits in Settings (routes/sessionLimits.ts), raising one
// session's budget (POST /api/v1/ai/sessions/:id/budget, manager.ts
// raiseBudget) and the fork route's refusals (POST …/fork), against Postgres with the real settings store and audit log.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

/** The UI through the TLS ingress (routes/guard.ts). */
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const JSON_UI = { ...UI, 'content-type': 'application/json' }

describe.skipIf(skip !== undefined)(`session budget${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let audit: AuditLog
  let settings: SettingsStore
  let m: SessionManager
  let next: FakeTurn
  let runs: HarnessRun[]
  let app: ReturnType<typeof createApp>
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    audit = new AuditLog({ sql: db.sql, settings: () => settings, onError: (err) => failures.push(err) })
    settings = new SettingsStore(db.sql, audit)
    next = { reply: 'hello there', costUsd: 0.4 }
    const scripted = scriptedRunner(() => next)
    const { runner } = scripted
    runs = scripted.runs
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, settings, audit })
    const deps: AppDeps = {
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
      settings,
      audit,
    }
    app = createApp(deps)
  })
  afterEach(async () => {
    m.abortAll()
    expect(failures).toEqual([])
    await drop()
  })

  async function auditRows(action: string): Promise<AuditRecord[]> {
    return (await audit.list({ kind: 'settings', action })).entries
  }

  /** A browser-owned session that has spent its $1 budget over two turns. */
  async function spentSession(): Promise<string> {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    await turn!.done
    next = { reply: 'that is all of it', costUsd: 1.0160000001, subtype: 'error_max_budget_usd' }
    await (await m.send(session.id, browser, 'bigger')).done
    return session.id
  }

  const putLimits = (body: unknown, headers: Record<string, string> = JSON_UI) =>
    app.request(SESSION_LIMITS_PATH, { method: 'PUT', headers, body: JSON.stringify(body) })

  const raise = (id: string, body: unknown, headers: Record<string, string> = JSON_UI) =>
    app.request(`/api/v1/ai/sessions/${id}/budget`, { method: 'POST', headers, body: JSON.stringify(body) })

  describe('the session limits setting', () => {
    it('reads the defaults, then what Settings saved, audited as you with the client address', async () => {
      const before = await app.request(SESSION_LIMITS_PATH, { headers: UI_READ })
      expect(await before.json()).toEqual({ budget_usd: 1, max_turns: 25 })

      const res = await putLimits({ budget_usd: 2.499, max_turns: 40 })
      expect(res.status).toBe(200)
      // Stored in cents.
      expect(await res.json()).toEqual({ budget_usd: 2.5, max_turns: 40 })
      expect(await settings.get(SETTING_SESSION_BUDGET_USD)).toBe(2.5)
      expect(await settings.get(SETTING_SESSION_MAX_TURNS)).toBe(40)
      expect(await (await app.request(SESSION_LIMITS_PATH, { headers: UI_READ })).json()).toEqual({
        budget_usd: 2.5,
        max_turns: 40,
      })

      for (const [action, value] of [
        [SETTING_SESSION_BUDGET_USD, '2.5'],
        [SETTING_SESSION_MAX_TURNS, '40'],
      ] as const) {
        const [row] = await auditRows(action)
        expect(row).toMatchObject({
          kind: 'settings',
          surface: 'http',
          outcome: 'ok',
          actor: { kind: 'browser', id: 'browser', label: 'You' },
          client_ip: '10.0.0.7',
          detail: `${action} = ${value}`,
        })
      }
    })

    it('applies to new sessions only', async () => {
      const { session: old } = await m.start(browser, { origin: 'chat' })
      expect((await putLimits({ budget_usd: 3, max_turns: 9 })).status).toBe(200)
      const { session: fresh } = await m.start(browser, { origin: 'chat' })
      expect(fresh).toMatchObject({ budgetUsd: 3, maxTurns: 9 })
      expect(await m.get(old.id, browser)).toMatchObject({ budgetUsd: 1, maxTurns: 25 })
    })

    it.each([
      ['a zero budget', { budget_usd: 0, max_turns: 25 }],
      ['a negative budget', { budget_usd: -1, max_turns: 25 }],
      ['a budget over $100', { budget_usd: 100.01, max_turns: 25 }],
      ['no turns', { budget_usd: 1, max_turns: 0 }],
      ['fractional turns', { budget_usd: 1, max_turns: 2.5 }],
      ['more than 200 turns', { budget_usd: 1, max_turns: 201 }],
      ['a missing field', { budget_usd: 1 }],
      ['an unknown field', { budget_usd: 1, max_turns: 25, per_turn: 1 }],
      ['a string', { budget_usd: '1', max_turns: 25 }],
    ])('refuses %s with 400 and stores nothing', async (_label, body) => {
      const res = await putLimits(body)
      expect(res.status).toBe(400)
      expect(await settings.get(SETTING_SESSION_BUDGET_USD)).toBeUndefined()
      expect(await auditRows(SETTING_SESSION_BUDGET_USD)).toEqual([])
    })

    it('refuses a write that is not from the UI', async () => {
      const res = await putLimits({ budget_usd: 5, max_turns: 25 }, { ...JSON_UI, origin: 'https://evil.example' })
      expect(res.status).toBe(403)
      expect(await settings.get(SETTING_SESSION_BUDGET_USD)).toBeUndefined()
    })
  })

  describe('a spent session', () => {
    it('says so in cents, not the SDK’s float', async () => {
      const id = await spentSession()
      const events = (await m.events.read(id)).map((e) => e.event)
      const result = events.findLast((e) => e.type === 'session.result')
      expect(result).toMatchObject({ costUsd: 1.0160000001, budgetUsd: 1 })
      const error = events.findLast((e) => e.type === 'error')
      expect(error).toMatchObject({ code: 'error_max_budget_usd', message: 'this chat used its $1.00 budget ($1.02 spent)' })
      await expect(m.send(id, browser, 'more')).rejects.toMatchObject({
        code: 'budget_exhausted',
        message: `session ${id} has spent its budget ($1.02 of $1.00); continue in a new chat, raise its budget, or start a new one`,
        budget: { costUsd: 1.0160000001, budgetUsd: 1 },
      })
      await expectPanelAccepts(events)
    })
  })

  describe('a turn stopped mid-reply (#991)', () => {
    // claude-sonnet-4-5 at $3 / $15 / $0.30 cache read per MTok: 100k input +
    // 200k cache read + 1000 output (4000 streamed characters) = $0.375.
    const stall = {
      model: 'claude-sonnet-4-5',
      usage: { input_tokens: 100_000, cache_read_input_tokens: 200_000, output_tokens: 1 },
      text: 'x'.repeat(4000),
    }
    const CUT = 0.375

    /** Sends `text`, waits until the reply has started streaming, and stops it. */
    async function stopMidReply(id: string, text: string) {
      const turn = await m.send(id, browser, text)
      for (let i = 0; i < 200; i++) {
        if ((await m.events.read(id)).some((e) => e.event.type === 'assistant.text.delta' && e.seq > 4)) break
        await new Promise((r) => setTimeout(r, 10))
      }
      expect(await m.interrupt(id, browser)).toBe(true)
      expect(await turn.done).toEqual({ kind: 'interrupted' })
    }

    it('charges what the cut-off request used, shows it on the meter, and does not count it again on the next turn', async () => {
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
      await turn!.done
      expect((await m.get(session.id, browser)).costUsd).toBeCloseTo(0.4, 10)

      next = { stall }
      await stopMidReply(session.id, 'write a long essay')
      expect(await m.get(session.id, browser)).toMatchObject({ status: 'idle', turns: 1 })
      expect((await m.get(session.id, browser)).costUsd).toBeCloseTo(0.4 + CUT, 10)
      const events = (await m.events.read(session.id)).map((e) => e.event)
      expect(events.findLast((e) => e.type === 'session.result')).toMatchObject({ budgetUsd: 1 })
      expect((events.findLast((e) => e.type === 'session.result') as { costUsd: number }).costUsd).toBeCloseTo(0.4 + CUT, 10)
      expect(events.at(-2)).toMatchObject({ type: 'error', code: 'interrupted' })
      await expectPanelAccepts(events)

      // The next turn resumes, and its total restores the transcript's, which
      // never held the cut-off request: 0.4 before + 0.05 now.
      next = { reply: 'OK', costUsd: 0.45 }
      const after = await (await m.send(session.id, browser, 'reply OK')).done
      // Its SDK budget is what is left after the cut-off turn.
      expect(runs.at(-1)!.maxBudgetUsd).toBeCloseTo(1 - 0.4 - CUT, 10)
      expect(after).toMatchObject({ kind: 'result', subtype: 'success', turns: 2 })
      if (after.kind !== 'result') throw new Error('unreachable')
      expect(after.costUsd).toBeCloseTo(0.4 + CUT + 0.05, 10)
      expect((await m.get(session.id, browser)).costUsd).toBeCloseTo(0.4 + CUT + 0.05, 10)
    })

    it('counts it once when the interrupt still ends in a result', async () => {
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
      await turn!.done
      // The interrupt's `aborted_streaming` result: the resumed total, unchanged.
      next = { stall, resultCostUsd: 0.4 }
      await stopMidReply(session.id, 'write a long essay')
      expect(await m.get(session.id, browser)).toMatchObject({ status: 'idle', turns: 1 })
      expect((await m.get(session.id, browser)).costUsd).toBeCloseTo(0.4 + CUT, 10)
      const results = (await m.events.read(session.id)).map((e) => e.event).filter((e) => e.type === 'session.result')
      expect(results).toHaveLength(2)
      expect((results.at(-1) as { costUsd: number }).costUsd).toBeCloseTo(0.4 + CUT, 10)
    })

    it('counts a first turn stopped before anything else was spent', async () => {
      const { session } = await m.start(browser, { origin: 'chat' })
      next = { stall, resultCostUsd: 0 }
      await stopMidReply(session.id, 'write a long essay')
      expect((await m.get(session.id, browser)).costUsd).toBeCloseTo(CUT, 10)
    })

    it('spends the budget: a session whose stopped turns used it takes no more turns', async () => {
      const { session } = await m.start(browser, { origin: 'chat' })
      // 400k input at $3/MTok: $1.20 of the $1 budget.
      next = { stall: { model: 'claude-sonnet-4-5', usage: { input_tokens: 400_000 } } }
      await stopMidReply(session.id, 'write a long essay')
      expect((await m.get(session.id, browser)).costUsd).toBeCloseTo(1.2, 10)
      next = { reply: 'never', costUsd: 0.01 }
      const sent = runs.length
      await expect(m.send(session.id, browser, 'more')).rejects.toMatchObject({ code: 'budget_exhausted' })
      expect(runs).toHaveLength(sent)
    })
  })

  describe('raising one session’s budget', () => {
    it('adds to budget_usd, lets the session go on, announces it and audits it', async () => {
      const id = await spentSession()
      const res = await raise(id, { add_usd: 1.004 })
      expect(res.status).toBe(200)
      const { session } = (await res.json()) as { session: { budget_usd: number; cost_usd: number } }
      expect(session.budget_usd).toBe(2)
      expect((await m.get(id, browser)).budgetUsd).toBe(2)
      const last = (await m.events.read(id)).map((e) => e.event).at(-1)
      expect(last).toEqual({ v: 1, type: 'session.budget', sessionId: id, costUsd: 1.0160000001, budgetUsd: 2 })
      await expectPanelAccepts([last!])
      const [row] = await auditRows('session_budget_usd')
      expect(row).toMatchObject({
        outcome: 'ok',
        surface: 'http',
        session_id: id,
        actor: { kind: 'browser', id: 'browser' },
        client_ip: '10.0.0.7',
        detail: '$1.00 + $1.00 = $2.00 ($1.02 spent)',
      })
      next = { reply: 'more', costUsd: 1.2 }
      expect(await (await m.send(id, browser, 'go on')).done).toMatchObject({ kind: 'result', subtype: 'success' })
    })

    it('is owner-only: a session another principal controls is refused until you take it over', async () => {
      const { session } = await m.start(agentA, { origin: 'mcp' })
      const refused = await raise(session.id, { add_usd: 1 })
      expect(refused.status).toBe(403)
      expect((await m.get(session.id, browser)).budgetUsd).toBe(1)
      await m.handoff(session.id, browser, browser)
      expect((await raise(session.id, { add_usd: 1 })).status).toBe(200)
    })

    it('is user-only: the headless browser’s marker, an MCP principal and another origin are refused', async () => {
      const id = await spentSession()
      const marked = await raise(id, { add_usd: 1 }, { ...JSON_UI, [AGENT_ACTOR_HEADER]: id })
      expect(marked.status).toBe(403)
      expect(((await marked.json()) as { detail: string }).detail).toContain('only you can raise')
      await expect(m.raiseBudget(id, agentA, 1, { surface: 'mcp' })).rejects.toMatchObject({ code: 'forbidden' })
      expect((await raise(id, { add_usd: 1 }, { ...JSON_UI, origin: 'https://evil.example' })).status).toBe(403)
      expect((await m.get(id, browser)).budgetUsd).toBe(1)
      // Refused attempts are recorded too (app.ts auditWrites), never as an ok raise.
      const rows = await auditRows('session_budget_usd')
      expect(rows.length).toBeGreaterThan(0)
      expect(rows.every((r) => r.outcome === 'refused')).toBe(true)
    })

    it('is not a tool: nothing an agent can call reaches it', () => {
      expect(ALL_TOOLS.filter((t) => /budget/i.test(t.name) || t.routes.some((r) => r.includes('/ai/')))).toEqual([])
    })

    it.each([
      ['nothing', { add_usd: 0 }],
      ['a negative raise', { add_usd: -5 }],
      ['more than $100 at once', { add_usd: 101 }],
      ['no amount', {}],
    ])('refuses %s with 400', async (_label, body) => {
      const id = await spentSession()
      expect((await raise(id, body)).status).toBe(400)
      expect((await m.get(id, browser)).budgetUsd).toBe(1)
    })

    it('never takes a budget past $100', async () => {
      const id = await spentSession()
      expect((await raise(id, { add_usd: 99 })).status).toBe(200)
      const over = await raise(id, { add_usd: 0.01 })
      expect(over.status).toBe(400)
      expect(((await over.json()) as { detail: string }).detail).toBe("a chat's budget can be at most $100.00; this one is $100.00")
      expect(await auditRows('session_budget_usd')).toEqual([
        expect.objectContaining({ outcome: 'error' }),
        expect.objectContaining({ outcome: 'ok' }),
      ])
    })
  })

  // Forking needs a real transcript, so a fork that works is in
  // test/sessions.e2e.test.ts ("continues a spent session in a new chat").
  describe('continuing a spent session in a new chat', () => {
    it('refuses a session with no transcript yet, and another origin', async () => {
      const { session } = await m.start(browser, { origin: 'chat' })
      expect((await app.request(`/api/v1/ai/sessions/${session.id}/fork`, { method: 'POST', headers: UI })).status).toBe(400)
      const id = await spentSession()
      const evil = await app.request(`/api/v1/ai/sessions/${id}/fork`, {
        method: 'POST',
        headers: { ...UI, origin: 'https://evil.example' },
      })
      expect(evil.status).toBe(403)
    })
  })
})
