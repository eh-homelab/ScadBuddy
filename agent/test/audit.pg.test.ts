import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { ApprovalService } from '../src/approvals/service.js'
import { AuditLog, DEFAULT_AUDIT_RETENTION_DAYS, SETTING_AUDIT_RETENTION_DAYS, SYSTEM_ACTOR } from '../src/audit/log.js'
import { TurnAuditor } from '../src/audit/turn.js'
import { auditedTokenStore, UI_ACTOR } from '../src/audit/writes.js'
import { PostgresTokenStore } from '../src/auth/tokens.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { MemoryEventSource } from '../src/events/bus.js'
import type { ApprovalVerdict } from '../src/harness/permissions.js'
import { ResourceHub } from '../src/resources/hub.js'
import { EventLog } from '../src/sessions/eventLog.js'
import { event } from '../src/sessions/protocol.js'
import { BACKEND, connect, testApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser } from './support/sessions.js'

// The audit log (#258, src/audit/log.ts) against Postgres: what is recorded,
// that the table is append-only, retention, and the read route.

const HASH_KEY = Buffer.alloc(32, 7)
const backend = setupServer()
beforeAll(() => backend.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => backend.resetHandlers())
afterAll(() => backend.close())

const UI_READ = { host: 'scadbuddy.test', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

describe.skipIf(!TEST_DATABASE_URL)(`the audit log in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let audit: AuditLog
  let settings: SettingsStore
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    audit = new AuditLog({ sql: db.sql, settings: () => settings, hashKey: HASH_KEY, onError: (err) => failures.push(err) })
    settings = new SettingsStore(db.sql, audit)
  })
  afterEach(async () => {
    expect(failures).toEqual([])
    await drop()
  })

  it('records an entry with who, what, the hash and summary, the outcome and timings', async () => {
    const input = { slug: 'keychain', api_key: 'sk-live-should-not-be-stored' }
    const started = new Date(Date.now() - 250)
    await audit.record({
      kind: 'tool_call',
      action: 'mcp__scadbuddy__delete_model',
      surface: 'harness',
      actor: browser,
      sessionId: '11111111-1111-4111-8111-111111111111',
      turnId: '22222222-2222-4222-8222-222222222222',
      toolUseId: 'toolu_1',
      tier: 'outward',
      inputHash: audit.hash('mcp__scadbuddy__delete_model', input),
      inputSummary: audit.summarise('mcp__scadbuddy__delete_model', input, ['sk-live-should-not-be-stored']),
      approvalId: '33333333-3333-4333-8333-333333333333',
      outcome: 'denied',
      detail: 'The user denied it',
      startedAt: started,
      finishedAt: new Date(started.getTime() + 250),
    })
    const { entries, next } = await audit.list()
    expect(next).toBeNull()
    expect(entries).toEqual([
      expect.objectContaining({
        kind: 'tool_call',
        action: 'mcp__scadbuddy__delete_model',
        surface: 'harness',
        actor: browser,
        session_id: '11111111-1111-4111-8111-111111111111',
        turn_id: '22222222-2222-4222-8222-222222222222',
        tool_use_id: 'toolu_1',
        tier: 'outward',
        input_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
        input_summary: '{"slug":"keychain","api_key":"[redacted]"}',
        approval_id: '33333333-3333-4333-8333-333333333333',
        outcome: 'denied',
        detail: 'The user denied it',
        duration_ms: 250,
      }),
    ])
    expect(JSON.stringify(entries)).not.toContain('sk-live')
  })

  it('is append-only: UPDATE, DELETE and TRUNCATE are refused', async () => {
    await audit.record({ kind: 'settings', action: 'model', surface: 'system', actor: SYSTEM_ACTOR, outcome: 'ok' })
    await expect(db.sql`UPDATE ai_audit SET outcome = 'error'`).rejects.toThrow(/append-only/)
    await expect(db.sql`DELETE FROM ai_audit`).rejects.toThrow(/append-only/)
    await expect(db.sql`TRUNCATE ai_audit`).rejects.toThrow(/append-only/)
    // The prune flag is transaction-local: set in one, it does not leak into the next statement.
    await db.sql.begin(async (tx) => {
      await tx`SELECT set_config('scadbuddy.audit_prune', 'on', true)`
    })
    await expect(db.sql`DELETE FROM ai_audit`).rejects.toThrow(/append-only/)
    expect((await audit.list()).entries).toHaveLength(1)
  })

  it('pages newest first and filters', async () => {
    for (let i = 0; i < 5; i += 1) {
      await audit.record({
        kind: i % 2 ? 'tool_call' : 'approval',
        action: `a${i}`,
        surface: 'mcp',
        actor: { kind: 'bearer', id: `token:${i % 2}`, label: 't' },
        outcome: i === 4 ? 'refused' : 'ok',
      })
    }
    const first = await audit.list({ limit: 2 })
    expect(first.entries.map((e) => e.action)).toEqual(['a4', 'a3'])
    const second = await audit.list({ limit: 2, before: first.next! })
    expect(second.entries.map((e) => e.action)).toEqual(['a2', 'a1'])
    const third = await audit.list({ limit: 2, before: second.next! })
    expect(third).toMatchObject({ next: null })
    expect(third.entries.map((e) => e.action)).toEqual(['a0'])

    expect((await audit.list({ kind: 'tool_call' })).entries.map((e) => e.action)).toEqual(['a3', 'a1'])
    expect((await audit.list({ outcome: 'refused' })).entries.map((e) => e.action)).toEqual(['a4'])
    expect((await audit.list({ principal: 'token:1', action: 'a1' })).entries.map((e) => e.action)).toEqual(['a1'])
    expect((await audit.list({ since: new Date(Date.now() + 60_000) })).entries).toEqual([])
  })

  it('prunes past the retention setting, and records the setting change', async () => {
    expect(await audit.retentionDays()).toBe(DEFAULT_AUDIT_RETENTION_DAYS)
    await audit.record({ kind: 'token', action: 'mint', surface: 'http', actor: UI_ACTOR, outcome: 'ok' })
    await db.sql`
      INSERT INTO ai_audit (at, kind, action, surface, principal_kind, principal_id, principal_label, outcome)
      VALUES (now() - interval '40 days', 'token', 'old', 'http', 'browser', 'browser', 'You', 'ok')`
    expect(await audit.prune()).toBe(0)

    expect(await audit.setRetentionDays(30, { actor: UI_ACTOR, surface: 'http', clientIp: '10.0.0.7' })).toBe(30)
    expect(await settings.get(SETTING_AUDIT_RETENTION_DAYS)).toBe(30)
    expect(await audit.prune()).toBe(1)
    const left = (await audit.list()).entries
    expect(left.map((e) => e.action)).toEqual([SETTING_AUDIT_RETENTION_DAYS, 'mint'])
    expect(left[0]).toMatchObject({
      kind: 'settings',
      actor: UI_ACTOR,
      client_ip: '10.0.0.7',
      outcome: 'ok',
      detail: `${SETTING_AUDIT_RETENTION_DAYS} = 30`,
    })
    // Out-of-range values are clamped, not stored as given.
    expect(await audit.setRetentionDays(0, { actor: UI_ACTOR, surface: 'http' })).toBe(1)
  })

  it('records token mint and revoke, never the token itself', async () => {
    const tokens = auditedTokenStore(new PostgresTokenStore(db.sql), audit)
    const { token, record } = await tokens.mint({ name: 'Claude Desktop', tier: 'write' })
    expect(await tokens.revoke(record.id)).toBe(true)
    expect(await tokens.revoke(record.id)).toBe(false)
    const entries = (await audit.list({ kind: 'token' })).entries
    expect(entries.map((e) => [e.action, e.outcome])).toEqual([
      ['revoke', 'error'],
      ['revoke', 'ok'],
      ['mint', 'ok'],
    ])
    expect(entries[2]?.detail).toBe(`token ${record.id} "Claude Desktop" (write)`)
    expect(JSON.stringify(entries)).not.toContain(token)
  })

  it('records every approval decision: approved, denied, expired, voided', async () => {
    const approvals = new ApprovalService({ sql: db.sql, events: new EventLog(db.sql), hashKey: HASH_KEY, audit })
    const ask = (toolUseId: string) =>
      approvals.create({
        sessionId: null,
        turnId: null,
        toolUseId,
        tool: 'mcp__scadbuddy__print_output',
        input: { output_id: 'abc', token: 'secret-token-value' },
        tier: 'outward',
        requestedBy: { kind: 'bearer', id: 'token:a', label: 'Agent A' },
      })
    const approved = await ask('t1')
    await approvals.decide(browser, approved.id, true, { clientIp: '10.0.0.9' })
    const denied = await ask('t2')
    await approvals.decide(browser, denied.id, false)
    const lapsed = await ask('t3')
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${lapsed.id}`
    expect(await approvals.expireDue()).toBe(1)

    const entries = (await audit.list({ kind: 'approval' })).entries
    expect(entries.map((e) => [e.action, e.outcome, e.approval_id])).toEqual([
      ['expired', 'refused', lapsed.id],
      ['denied', 'denied', denied.id],
      ['approved', 'ok', approved.id],
    ])
    expect(entries[2]).toMatchObject({
      actor: browser,
      surface: 'http',
      client_ip: '10.0.0.9',
      tier: 'outward',
      input_hash: approved.inputHash,
      input_summary: '{"output_id":"abc","token":"[redacted]"}',
    })
    expect(entries[2]?.detail).toContain('requested by Agent A')
    expect(entries[0]).toMatchObject({ actor: SYSTEM_ACTOR, surface: 'system' })
  })

  it('records /mcp calls, with the client address, and serves them on GET /api/v1/ai/audit', async () => {
    const { app } = testApp({ settings: { mode: 'disabled' }, deps: { audit }, mcp: { audit } })
    const client = await connect(app, { address: '127.0.0.1' })
    await client.callTool({ name: 'list_pending_actions', arguments: {} })
    const prepared = await client.callTool({ name: 'delete_model', arguments: { slug: 'keychain' } })
    expect(prepared.isError).toBeFalsy()
    await client.close()

    const res = await app.request('/api/v1/ai/audit?kind=tool_call', { headers: { ...UI_READ, host: '127.0.0.1:8081' } }, {
      incoming: { socket: { remoteAddress: '127.0.0.1' } },
    })
    expect(res.status).toBe(200)
    const body = (await res.json()) as { entries: Record<string, unknown>[]; retention_days: number }
    expect(body.retention_days).toBe(DEFAULT_AUDIT_RETENTION_DAYS)
    expect(body.entries.map((e) => [e.action, e.tier, e.outcome, e.surface])).toEqual([
      ['delete_model', 'outward', 'refused', 'mcp'],
      ['list_pending_actions', 'read', 'ok', 'mcp'],
    ])
    expect(body.entries[0]).toMatchObject({
      actor: { kind: 'anonymous' },
      client_ip: '127.0.0.1',
      input_summary: '{"slug":"keychain","force":false}',
      detail: expect.stringContaining('waiting for approval'),
    })
  })

  it('names who approved an executed call on its tool_call row, from ai_approvals (and nobody for a denied one)', async () => {
    const approvals = new ApprovalService({ sql: db.sql, events: new EventLog(db.sql), hashKey: HASH_KEY, audit })
    const ask = (toolUseId: string) =>
      approvals.create({
        sessionId: null,
        turnId: null,
        toolUseId,
        tool: 'mcp__scadbuddy__delete_model',
        input: { slug: 'keychain' },
        tier: 'outward',
        requestedBy: { kind: 'bearer', id: 'token:a', label: 'Agent A' },
      })
    const yes = await ask('t1')
    await approvals.decide(browser, yes.id, true)
    const no = await ask('t2')
    await approvals.decide(browser, no.id, false)

    // The harness side: a TurnAuditor sees the call, its verdict and its result.
    const turn = new TurnAuditor(audit, {
      sessionId: '11111111-1111-4111-8111-111111111111',
      turnId: '22222222-2222-4222-8222-222222222222',
      actor: { kind: 'bearer', id: 'token:a', label: 'Agent A' },
      tierOf: () => 'outward',
      secrets: () => [],
    })
    const verdicts: Record<string, ApprovalVerdict> = {
      t1: { approved: true, input: { slug: 'keychain' }, approvalId: yes.id, decision: 'approved' },
      t2: { approved: false, message: 'denied', approvalId: no.id, decision: 'denied' },
    }
    const gate = turn.gate(async (r) => verdicts[r.toolUseId]!)
    for (const id of ['t1', 't2']) {
      await turn.observe(event({ type: 'tool.call', sessionId: 's', id, name: 'mcp__scadbuddy__delete_model', input: { slug: 'keychain' }, risk: 'outward' }))
      await gate({ toolName: 'mcp__scadbuddy__delete_model', input: { slug: 'keychain' }, toolUseId: id, tier: 'outward', signal: new AbortController().signal })
      await turn.observe(event({ type: 'tool.result', sessionId: 's', id, ok: id === 't1', summary: id === 't1' ? 'deleted' : 'The user denied it' }))
    }
    const rows = (await audit.list({ kind: 'tool_call' })).entries
    expect(rows.map((r) => [r.tool_use_id, r.outcome, r.actor.label, r.approved_by])).toEqual([
      ['t2', 'denied', 'Agent A', null],
      ['t1', 'ok', 'Agent A', browser],
    ])
    // Approval rows are the decider's own; they carry no approved_by.
    expect((await audit.list({ kind: 'approval' })).entries.every((r) => r.approved_by === null)).toBe(true)
  })

  it('records /mcp resource reads, refusals and subscriptions', async () => {
    backend.use(http.get(`${BACKEND}/api/v1/models/keychain/source`, () => HttpResponse.text('cube(1);')))
    const hub = new ResourceHub(new MemoryEventSource())
    const t = testApp({ deps: { audit }, mcp: { audit, resources: hub } })
    const { token } = await t.tokens.mint({ name: 'reader', tier: 'read' })
    const client = await connect(t.app, { headers: { authorization: `Bearer ${token}` } })
    await client.readResource({ uri: 'scadbuddy://models/keychain/source' })
    await expect(client.readResource({ uri: 'scadbuddy://settings' })).rejects.toThrow(/tier/)
    await client.subscribeResource({ uri: 'scadbuddy://models/keychain/source' })
    await client.unsubscribeResource({ uri: 'scadbuddy://models/keychain/source' })
    await client.close()
    const rows = (await audit.list({ kind: 'resource' })).entries.reverse()
    expect(rows.map((r) => [r.action, r.outcome, r.surface, r.actor.kind])).toEqual([
      ['read', 'ok', 'mcp', 'bearer'],
      ['read', 'refused', 'mcp', 'bearer'],
      ['subscribe', 'ok', 'mcp', 'bearer'],
      ['unsubscribe', 'ok', 'mcp', 'bearer'],
    ])
    expect(rows[0]).toMatchObject({ tier: 'read', detail: 'scadbuddy://models/keychain/source' })
    expect(rows[1]?.detail).toContain('needs the "write" tier')
  })

  it('token routes: a mint is recorded once by the audited store; a refused one by the route', async () => {
    const tokens = auditedTokenStore(new PostgresTokenStore(db.sql), audit)
    const t = testApp({ tokens, deps: { audit, tokens } })
    const ui = { host: 'scadbuddy.test', origin: 'https://scadbuddy.test', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
    const env = { incoming: { socket: { remoteAddress: '10.0.0.7' } } }
    const body = JSON.stringify({ name: 'Desk', tier: 'read' })
    const ok = await t.app.request('/api/v1/ai/mcp-tokens', { method: 'POST', headers: ui, body }, env)
    expect(ok.status).toBe(201)
    const { token } = (await ok.json()) as { token: string }
    const refused = await t.app.request('/api/v1/ai/mcp-tokens', { method: 'POST', headers: { ...ui, origin: 'https://evil.example' }, body }, env)
    expect(refused.status).toBe(403)
    const rows = (await audit.list({ kind: 'token' })).entries.reverse()
    expect(rows.map((r) => [r.action, r.outcome, r.actor.kind])).toEqual([
      ['mint', 'ok', 'browser'],
      ['mint', 'refused', 'anonymous'],
    ])
    expect(rows[1]).toMatchObject({ client_ip: '10.0.0.7', surface: 'http' })
    expect(JSON.stringify(rows)).not.toContain(token)
  })
})
