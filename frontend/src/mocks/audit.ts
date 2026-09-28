/**
 * The agent service's audit log (#258) for vitest and the mocked build: the same
 * routes and shapes as `agent/src/routes/audit.ts`, over a fixed set of entries that
 * tell one story: the assistant read a README, was told by it to delete the model,
 * and the user denied that; an MCP client read a source; and the user approved a
 * settings change that then ran.
 */
import { HttpResponse, http } from 'msw'
import {
  AUDIT_KINDS,
  AUDIT_OUTCOMES,
  type AuditEntry,
  type AuditPage,
  MAX_RETENTION_DAYS,
  MIN_RETENTION_DAYS,
} from '../agent/audit'

const SESSION = '0b7f3c1e-2a44-4d5e-9c1a-5f0e6d7c8b9a'
const APPROVAL = '4c2d1e0f-9a8b-4c7d-8e6f-5a4b3c2d1e0f'
const APPROVAL_2 = '9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b'
const YOU = { kind: 'browser', id: 'browser', label: 'You' }

function entry(id: number, minutesAgo: number, fields: Partial<AuditEntry> & Pick<AuditEntry, 'kind' | 'action' | 'outcome'>): AuditEntry {
  const at = new Date(Date.UTC(2026, 8, 28, 9, 30) - minutesAgo * 60_000).toISOString()
  return {
    id: String(id),
    at,
    surface: 'harness',
    actor: YOU,
    client_ip: null,
    session_id: null,
    turn_id: null,
    tool_use_id: null,
    tier: null,
    input_hash: null,
    input_summary: null,
    approval_id: null,
    approved_by: null,
    detail: null,
    started_at: at,
    finished_at: at,
    duration_ms: null,
    ...fields,
  }
}

/** Oldest first; the route answers newest first. */
export const AUDIT_FIXTURES: readonly AuditEntry[] = [
  entry(1, 60, { kind: 'settings', action: 'model', surface: 'http', outcome: 'ok', detail: 'model = "claude-sonnet-4-5"', client_ip: '10.0.0.7' }),
  entry(2, 30, { kind: 'token', action: 'mint', surface: 'http', outcome: 'ok', detail: 'token 7d1e… "Claude Desktop" (write)' }),
  entry(3, 12, {
    kind: 'tool_call',
    action: 'list_models',
    surface: 'mcp',
    actor: { kind: 'bearer', id: 'token:7d1e', label: 'token:7d1e' },
    tier: 'read',
    outcome: 'ok',
    input_summary: '{}',
    duration_ms: 18,
  }),
  entry(4, 5, {
    kind: 'tool_call',
    action: 'mcp__scadbuddy__get_readme',
    session_id: SESSION,
    tier: 'read',
    outcome: 'ok',
    input_summary: '{"slug":"name-keychain"}',
    duration_ms: 42,
  }),
  entry(5, 4, {
    kind: 'approval',
    action: 'denied',
    surface: 'http',
    session_id: SESSION,
    tier: 'outward',
    approval_id: APPROVAL,
    outcome: 'denied',
    input_summary: '{"slug":"name-keychain"}',
    detail: 'mcp__scadbuddy__delete_model (requested by You)',
  }),
  entry(6, 4, {
    kind: 'tool_call',
    action: 'mcp__scadbuddy__delete_model',
    session_id: SESSION,
    tier: 'outward',
    approval_id: APPROVAL,
    outcome: 'denied',
    input_summary: '{"slug":"name-keychain"}',
    detail: 'The user denied mcp__scadbuddy__delete_model; it was not run.',
    duration_ms: 61_250,
  }),
  entry(7, 2, {
    kind: 'resource',
    action: 'read',
    surface: 'mcp',
    actor: { kind: 'bearer', id: 'token:7d1e', label: 'token:7d1e' },
    tier: 'read',
    outcome: 'ok',
    detail: 'scadbuddy://models/name-keychain/source',
  }),
  entry(8, 1, {
    kind: 'tool_call',
    action: 'mcp__scadbuddy__set_print_options',
    session_id: SESSION,
    tier: 'outward',
    approval_id: APPROVAL_2,
    approved_by: YOU,
    outcome: 'ok',
    input_summary: '{"supports":true}',
    duration_ms: 8_400,
  }),
]

let retention = 90

export function resetAuditMock(): void {
  retention = 90
}

function problem(status: number, detail: string) {
  return HttpResponse.json({ detail }, { status })
}

export const auditHandlers = [
  http.get('/api/v1/ai/audit', ({ request }) => {
    const url = new URL(request.url)
    const kind = url.searchParams.get('kind')
    const outcome = url.searchParams.get('outcome')
    const before = url.searchParams.get('before')
    const limit = Number(url.searchParams.get('limit') ?? '50')
    if (kind && !(AUDIT_KINDS as readonly string[]).includes(kind)) return problem(400, `kind: invalid "${kind}"`)
    if (outcome && !(AUDIT_OUTCOMES as readonly string[]).includes(outcome)) return problem(400, `outcome: invalid "${outcome}"`)
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) return problem(400, 'limit: must be 1 to 200')
    const matching = [...AUDIT_FIXTURES]
      .reverse()
      .filter((e) => (kind ? e.kind === kind : true))
      .filter((e) => (outcome ? e.outcome === outcome : true))
      .filter((e) => (before ? Number(e.id) < Number(before) : true))
    const page = matching.slice(0, limit)
    const body: AuditPage = {
      entries: page,
      next: matching.length > limit ? (page.at(-1)?.id ?? null) : null,
      retention_days: retention,
    }
    return HttpResponse.json(body)
  }),

  http.put('/api/v1/ai/audit/settings', async ({ request }) => {
    const body = (await request.json()) as { retention_days?: unknown }
    const days = body.retention_days
    if (typeof days !== 'number' || !Number.isInteger(days) || days < MIN_RETENTION_DAYS || days > MAX_RETENTION_DAYS) {
      return problem(400, `retention_days: must be a whole number from ${MIN_RETENTION_DAYS} to ${MAX_RETENTION_DAYS}`)
    }
    retention = days
    return HttpResponse.json({ retention_days: retention })
  }),
]
