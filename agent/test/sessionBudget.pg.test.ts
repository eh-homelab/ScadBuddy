import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { AuditLog, type AuditRecord } from '../src/audit/log.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { AGENT_ACTOR_HEADER } from '../src/harness/headlessBrowser.js'
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
  let runs: ReturnType<typeof scriptedRunner>['runs']
  let app: ReturnType<typeof createApp>
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    audit = new AuditLog({ sql: db.sql, settings: () => settings, onError: (err) => failures.push(err) })
    settings = new SettingsStore(db.sql, audit)
    next = { reply: 'hello there', costUsd: 0.4 }
    const { runner, runs: made } = scriptedRunner(() => next)
    runs = made
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

  // #823: a fork is not a way round the user-only raise. A fork spends from its
  // parent's budget: nothing is copied or moved, and a turn in any session of the
  // lineage uses up the one budget. Only the panel's "continue in a new chat" (the
  // route, without the headless browser's marker) gives the child a budget of its own.
  describe('a fork that is not the user’s spends from the parent’s budget', () => {
    /** The scripted runner writes no SDK transcript; a fork needs one. */
    const transcript = (id: string) => m.store.append({ projectKey: 'p', sessionId: id }, [{ type: 'user', uuid: 'u1', message: {} }])
    const fork = (id: string, headers: Record<string, string>) =>
      app.request(`/api/v1/ai/sessions/${id}/fork`, { method: 'POST', headers })
    /** A turn whose session's own spend comes to `ownUsd` in all (the scripted SDK total). */
    const spend = async (id: string, by: typeof browser, ownUsd: number) => {
      next = { reply: 'done', costUsd: ownUsd }
      await (await m.send(id, by, 'go on')).done
      return runs.at(-1)!.maxBudgetUsd
    }
    /** A session of `owner`'s that has spent $0.40 of its $1, with a transcript to fork. */
    const started = async (owner: typeof browser = browser) => {
      const { session, turn } = await m.start(owner, { origin: owner === browser ? 'chat' : 'mcp', prompt: 'make a box' })
      await turn!.done
      await transcript(session.id)
      return session.id
    }

    it('shares one budget: a fork’s spend is the parent’s, and the parent’s is the fork’s', async () => {
      const parent = await started()
      const child = await m.fork(parent, browser)
      // Neither copied nor moved: both see the one $1, $0.40 of it spent.
      expect(child).toMatchObject({ budgetUsd: 1, costUsd: 0.4, ownCostUsd: 0 })
      expect(await m.get(parent, browser)).toMatchObject({ budgetUsd: 1, costUsd: 0.4 })

      // The fork's turn is given what the lineage has left, and its spend is the parent's.
      expect(await spend(child.id, browser, 0.3)).toBeCloseTo(0.6, 9)
      expect((await m.get(parent, browser)).costUsd).toBeCloseTo(0.7, 9)
      // And the other way round.
      expect(await spend(parent, browser, 0.6)).toBeCloseTo(0.3, 9)
      const after = await m.get(child.id, browser)
      expect(after).toMatchObject({ budgetUsd: 1, ownCostUsd: 0.3 })
      expect(after.costUsd).toBeCloseTo(0.9, 9)
    })

    it('shares it with a fork of a fork', async () => {
      const parent = await started()
      const child = await m.fork(parent, browser)
      await transcript(child.id)
      const grandchild = await m.fork(child.id, browser)
      expect(grandchild).toMatchObject({ budgetUsd: 1, costUsd: 0.4 })
      expect(await spend(grandchild.id, browser, 0.7)).toBeCloseTo(0.6, 9)
      // $0.40 + $0.70 of the one $1: the whole lineage is spent.
      for (const id of [parent, child.id, grandchild.id]) {
        await expect(m.send(id, browser, 'more')).rejects.toMatchObject({ code: 'budget_exhausted' })
      }
    })

    it('creates no budget: forks together spend at most what the parent had', async () => {
      const parent = await started()
      const forks = await Promise.all([m.fork(parent, browser), m.fork(parent, browser), m.fork(parent, browser)])
      expect(forks.map((f) => f.budgetUsd)).toEqual([1, 1, 1])
      // The parent still has its $1 and nothing more.
      expect(await m.get(parent, browser)).toMatchObject({ budgetUsd: 1, costUsd: 0.4 })

      expect(await spend(forks[0]!.id, browser, 0.3)).toBeCloseTo(0.6, 9)
      expect(await spend(forks[1]!.id, browser, 0.3)).toBeCloseTo(0.3, 9)
      // $0.40 + $0.30 + $0.30: the third fork and the parent have nothing left.
      await expect(m.send(forks[2]!.id, browser, 'more')).rejects.toMatchObject({ code: 'budget_exhausted' })
      await expect(m.send(parent, browser, 'more')).rejects.toMatchObject({ code: 'budget_exhausted' })
      // A spent lineage's fork is refused up front.
      await expect(m.fork(parent, browser)).rejects.toMatchObject({ code: 'budget_exhausted' })
    })

    it('a raise on either session raises the one budget', async () => {
      const parent = await started()
      const child = await m.fork(parent, browser)
      expect((await raise(child.id, { add_usd: 1 })).status).toBe(200)
      expect(await m.get(parent, browser)).toMatchObject({ budgetUsd: 2, costUsd: 0.4 })
      expect((await raise(parent, { add_usd: 0.5 })).status).toBe(200)
      expect(await m.get(child.id, browser)).toMatchObject({ budgetUsd: 2.5, costUsd: 0.4 })
      // Still user-only, on a fork as on any session.
      expect((await raise(child.id, { add_usd: 1 }, { ...JSON_UI, [AGENT_ACTOR_HEADER]: child.id })).status).toBe(403)
      expect((await m.get(parent, browser)).budgetUsd).toBe(2.5)
    })

    // #1451: a fork used to move what the parent had left into the forker's session.
    it('lets no one who merely sees a session move its budget away by forking it', async () => {
      const parent = await started(agentA)
      // The browser sees every session but does not own agentA's; this is sessions_fork's path.
      const child = await m.fork(parent, browser)
      expect(child.owner).toEqual(browser)
      // The owner keeps its whole budget and can go on spending it.
      expect(await m.get(parent, agentA)).toMatchObject({ budgetUsd: 1, costUsd: 0.4 })
      expect(await spend(parent, agentA, 0.5)).toBeCloseTo(0.6, 9)
      // The fork spends the same budget, not one of its own (an open question on #1438).
      expect((await m.get(child.id, browser)).costUsd).toBeCloseTo(0.5, 9)
    })

    // #1447: the manager holds the user-only rule itself, as raiseBudget does, not just the route.
    it('gives a budget of its own only to the browser user, without the agent-actor marker', async () => {
      const parent = await started(agentA)
      const asked = await m.fork(parent, agentA, { freshBudget: true })
      expect(asked).toMatchObject({ budgetUsd: 1, costUsd: 0.4 })
      expect(await spend(asked.id, agentA, 0.3)).toBeCloseTo(0.6, 9)
      expect((await m.get(parent, agentA)).costUsd).toBeCloseTo(0.7, 9)
      const marked = await m.fork(parent, browser, { freshBudget: true, agentActor: true })
      expect(marked).toMatchObject({ budgetUsd: 1 })
      expect(marked.costUsd).toBeCloseTo(0.7, 9)
      // The user's own gets its own $1, nothing spent.
      expect(await m.fork(parent, browser, { freshBudget: true })).toMatchObject({ budgetUsd: 1, costUsd: 0 })
    })

    it('refuses a spent session’s fork unless it is the user’s, which gets a budget of its own', async () => {
      const id = await spentSession()
      await transcript(id)
      await expect(m.fork(id, browser)).rejects.toMatchObject({ code: 'budget_exhausted' })
      const marked = await fork(id, { ...UI, [AGENT_ACTOR_HEADER]: id })
      expect(marked.status).toBe(409)
      expect(((await marked.json()) as { detail: string }).detail).toContain('only you can continue it')

      const ui = await fork(id, UI)
      expect(ui.status).toBe(201)
      const own = ((await ui.json()) as { session: { id: string; budget_usd: number; cost_usd: number } }).session
      expect(own).toMatchObject({ budget_usd: 1, cost_usd: 0 })
      // Its own: its turn is given the whole of it, and the spent parent stays spent.
      expect(await spend(own.id, browser, 0.2)).toBe(1)
      expect((await m.get(id, browser)).costUsd).toBeGreaterThan(1)
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
