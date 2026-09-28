/**
 * The agent service's audit log of AI actions (#258): `GET /api/v1/ai/audit` and
 * `PUT /api/v1/ai/audit/settings`, served by the agent (`agent/src/routes/audit.ts`),
 * not the backend, so these are not in `openapi.json`. The shapes mirror
 * `agent/src/audit/log.ts` `AuditRecord`.
 */

export const AUDIT_KINDS = ['tool_call', 'resource', 'approval', 'credential', 'plugin', 'settings', 'token'] as const
export type AuditKind = (typeof AUDIT_KINDS)[number]
export const AUDIT_OUTCOMES = ['ok', 'error', 'refused', 'denied'] as const
export type AuditOutcome = (typeof AUDIT_OUTCOMES)[number]

export const MIN_RETENTION_DAYS = 1
export const MAX_RETENTION_DAYS = 3650

export interface AuditActor {
  kind: string
  id: string
  label: string
}

export interface AuditEntry {
  id: string
  at: string
  kind: AuditKind
  /** The tool name for a tool call, otherwise the verb (approved, mint, save …). */
  action: string
  surface: 'harness' | 'mcp' | 'http' | 'system'
  actor: AuditActor
  client_ip: string | null
  session_id: string | null
  turn_id: string | null
  tool_use_id: string | null
  tier: 'read' | 'write' | 'outward' | null
  input_hash: string | null
  /** Scrubbed by the agent: secrets are never in it. */
  input_summary: string | null
  approval_id: string | null
  /** For a tool call that ran on an approval: who approved it. */
  approved_by: AuditActor | null
  outcome: AuditOutcome
  detail: string | null
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
}

export interface AuditPage {
  entries: AuditEntry[]
  /** Pass back as `before` for the next (older) page; null on the last. */
  next: string | null
  retention_days: number
}

export interface AuditQuery {
  kind?: AuditKind
  outcome?: AuditOutcome
  before?: string
  limit?: number
}

export class AuditError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'AuditError'
    this.status = status
  }
}

async function failure(res: Response): Promise<AuditError> {
  let detail = `HTTP ${res.status}`
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') detail = body.detail
  } catch {
    // Not JSON: keep the status.
  }
  return new AuditError(res.status, detail)
}

export async function fetchAudit(query: AuditQuery = {}, signal?: AbortSignal): Promise<AuditPage> {
  const params = new URLSearchParams()
  if (query.kind) params.set('kind', query.kind)
  if (query.outcome) params.set('outcome', query.outcome)
  if (query.before) params.set('before', query.before)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  const res = await fetch(`/api/v1/ai/audit${search ? `?${search}` : ''}`, {
    headers: { accept: 'application/json' },
    ...(signal ? { signal } : {}),
  })
  if (!res.ok) throw await failure(res)
  return (await res.json()) as AuditPage
}

export async function saveAuditRetention(days: number): Promise<number> {
  const res = await fetch('/api/v1/ai/audit/settings', {
    method: 'PUT',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify({ retention_days: days }),
  })
  if (!res.ok) throw await failure(res)
  return ((await res.json()) as { retention_days: number }).retention_days
}
