import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { CrudAudit } from '../src/audit/crudAudit.js'
import { type AuditEntry, AuditLog, DETAIL_MAX, SUMMARY_MAX } from '../src/audit/log.js'

// The audit log through the API's CRUD routes (#5299 step 10, area `audit`), against a fake API
// that answers as backend api/crud_agent_audit.py does (its tests,
// tests/crud/test_crud_agent_audit.py, cover the SQL, the approver's copy and the prune's trigger
// on Postgres). AuditLog takes no SQL once the area is listed; every text is capped before it is
// sent; an id that is no UUID goes as null, as the SQL wrote it; and a failed write is reported,
// never thrown.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SID = '11111111-1111-4111-8111-111111111111'

type Seen = { path: string; body: Record<string, unknown> | undefined }

class FakeApi {
  seen: Seen[] = []
  answers: unknown[] = []

  readonly fetch: typeof fetch = (input, init) => {
    const url = new URL(String(input))
    const raw = init?.body
    this.seen.push({ path: url.pathname.slice(PREFIX.length) + url.search, body: typeof raw === 'string' ? (JSON.parse(raw) as Record<string, unknown>) : undefined })
    const answer = this.answers.shift()
    if (answer instanceof Response) return Promise.resolve(answer)
    return Promise.resolve(new Response(JSON.stringify(answer), { status: 200, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }))
  }
}

const noSql = new Proxy(() => undefined, {
  apply: () => {
    throw new Error('ran SQL')
  },
  get: () => {
    throw new Error('ran SQL')
  },
}) as unknown as Sql

function log(api: FakeApi, errors: unknown[] = []): AuditLog {
  return new AuditLog({
    sql: noSql,
    routes: { crud: new CrudAudit({ baseUrl: BASE, fetch: api.fetch }), use: () => true },
    settings: () => ({ get: <T>() => Promise.resolve(30 as T), set: () => Promise.resolve() }),
    onError: (err) => errors.push(err),
  })
}

const ENTRY: AuditEntry = {
  kind: 'tool_call',
  action: 'print_output',
  surface: 'harness',
  actor: { kind: 'browser', id: 'browser', label: 'You' },
  outcome: 'ok',
  sessionId: SID,
  turnId: 'not-a-uuid',
  toolUseId: 'toolu_1',
  tier: 'outward',
  inputHash: 'a'.repeat(64),
  inputSummary: 'x'.repeat(SUMMARY_MAX + 10),
  approvalId: '../x',
  requestId: `durable:${SID}:run-1:toolu_1`,
  detail: 'd'.repeat(DETAIL_MAX + 10),
  startedAt: new Date('2026-11-15T10:00:00.000Z'),
  finishedAt: new Date('2026-11-15T10:00:01.250Z'),
}

describe('the audit log through the CRUD routes', () => {
  it('records a row with its texts capped and malformed ids dropped', async () => {
    const api = new FakeApi()
    api.answers.push({ done: true })
    await log(api).record(ENTRY)
    const [seen] = api.seen
    expect(seen?.path).toBe('/audit/record')
    expect(seen?.body).toMatchObject({
      kind: 'tool_call',
      session_id: SID,
      turn_id: null,
      approval_id: null,
      request_id: ENTRY.requestId,
      started_at: '2026-11-15T10:00:00.000Z',
      finished_at: '2026-11-15T10:00:01.250Z',
    })
    expect((seen?.body?.input_summary as string).length).toBe(SUMMARY_MAX)
    expect((seen?.body?.detail as string).length).toBe(DETAIL_MAX)
  })

  it('reports a write the API refused, and never throws', async () => {
    const api = new FakeApi()
    const errors: unknown[] = []
    api.answers.push(new Response('{"detail":"no"}', { status: 422, headers: { 'x-scadbuddy-crud': '1' } }))
    await expect(log(api, errors).record(ENTRY)).resolves.toBeUndefined()
    expect(errors).toHaveLength(1)
  })

  it('lists a page in the shape the SQL read', async () => {
    const api = new FakeApi()
    const row = {
      id: '42',
      at: '2026-11-15T10:00:02.123456+00:00',
      kind: 'approval',
      action: 'approved',
      surface: 'http',
      principal_kind: 'browser',
      principal_id: 'browser',
      principal_label: 'You',
      client_ip: null,
      session_id: SID,
      turn_id: null,
      tool_use_id: null,
      tier: 'outward',
      input_hash: null,
      input_summary: null,
      approval_id: null,
      approved_by_kind: 'browser',
      approved_by_id: 'browser',
      approved_by_label: 'You',
      outcome: 'ok',
      detail: null,
      started_at: null,
      finished_at: null,
      duration_ms: null,
      cost_usd: null,
      cost_priced: null,
      cost_estimated_usd: null,
    }
    api.answers.push({ rows: [row], next: '42' })
    const page = await log(api).list({ kind: 'approval', before: '100', since: new Date('2026-11-01T00:00:00Z') })
    expect(api.seen[0]?.body).toMatchObject({ kind: 'approval', before: 100, since: '2026-11-01T00:00:00.000Z', limit: null })
    expect(page.next).toBe('42')
    expect(page.entries[0]).toMatchObject({
      id: '42',
      at: '2026-11-15T10:00:02.123Z',
      actor: { kind: 'browser', id: 'browser', label: 'You' },
      approved_by: { kind: 'browser', id: 'browser', label: 'You' },
    })
  })

  it('answers no page for a cursor that is no row id, with no call', async () => {
    const api = new FakeApi()
    expect(await log(api).list({ before: '../1' })).toEqual({ entries: [], next: null })
    expect(api.seen).toEqual([])
  })

  it('prunes past the retention on the API', async () => {
    const api = new FakeApi()
    api.answers.push({ deleted: 3 })
    expect(await log(api).prune()).toBe(3)
    expect(api.seen).toEqual([{ path: '/audit/prune', body: { days: 30 } }])
  })
})
