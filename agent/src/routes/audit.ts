import type { Hono } from 'hono'
import { z } from 'zod'
import {
  AUDIT_KINDS,
  AUDIT_OUTCOMES,
  AUDIT_SURFACES,
  type AuditRepo,
  MAX_AUDIT_RETENTION_DAYS,
  MAX_PAGE,
  MIN_AUDIT_RETENTION_DAYS,
} from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/audit (#258): the audit log of AI actions, for Settings.
//
//   GET /api/v1/ai/audit?kind=&outcome=&surface=&action=&session=&principal=&since=&until=&before=&limit=
//       → { entries: AuditRecord[], next: string | null, retention_days: number }
//       Newest first. `next` is the cursor for the following (older) page:
//       pass it back as `before`. `since`/`until` are ISO timestamps.
//   PUT /api/v1/ai/audit/settings   { "retention_days": 1..3650 }
//       → { retention_days }. An ai_settings write, itself audited
//       (credentials.ts SettingsStore.set).
//
// The read passes guard.ts `uiReadProblem` (the UI's origin through the HTTPS
// ingress, as far as a same-origin GET shows it); the write passes
// `uiRequestProblem`, like every other Settings write. Entries hold scrubbed
// summaries and keyed hashes only (audit/log.ts), never a tool's full input.
// Error bodies use `{ detail }`, the backend's FastAPI shape.

export type AuditRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  audit: AuditRepo | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

const Instant = z.iso.datetime({ offset: true }).transform((s) => new Date(s))

const Query = z.strictObject({
  kind: z.enum(AUDIT_KINDS).optional(),
  outcome: z.enum(AUDIT_OUTCOMES).optional(),
  surface: z.enum(AUDIT_SURFACES).optional(),
  action: z.string().min(1).max(200).optional(),
  session: z.uuid().optional(),
  principal: z.string().min(1).max(200).optional(),
  since: Instant.optional(),
  until: Instant.optional(),
  before: z.string().regex(/^[1-9][0-9]{0,18}$/, 'must be a cursor from a previous page').optional(),
  limit: z.coerce.number().int().min(1).max(MAX_PAGE).optional(),
})

const SettingsBody = z.strictObject({
  retention_days: z.number().int().min(MIN_AUDIT_RETENTION_DAYS).max(MAX_AUDIT_RETENTION_DAYS),
})

function issues(err: z.ZodError): string {
  return err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
}

export function registerAuditRoutes(app: Hono, deps: AuditRouteDeps): void {
  const base = '/api/v1/ai/audit'

  async function repo(): Promise<AuditRepo | string> {
    if (!deps.audit) return NO_DATABASE
    return (await deps.ready()) ? deps.audit : NOT_READY
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'audit log reads')
    if (problem) return c.json({ detail: problem }, 403)
    const audit = await repo()
    if (typeof audit === 'string') return c.json({ detail: audit }, 503)
    const parsed = Query.safeParse(c.req.query())
    if (!parsed.success) return c.json({ detail: issues(parsed.error) }, 400)
    const q = parsed.data
    const [page, retention] = await Promise.all([
      audit.list({
        ...(q.kind ? { kind: q.kind } : {}),
        ...(q.outcome ? { outcome: q.outcome } : {}),
        ...(q.surface ? { surface: q.surface } : {}),
        ...(q.action ? { action: q.action } : {}),
        ...(q.session ? { sessionId: q.session } : {}),
        ...(q.principal ? { principal: q.principal } : {}),
        ...(q.since ? { since: q.since } : {}),
        ...(q.until ? { until: q.until } : {}),
        ...(q.before ? { before: q.before } : {}),
        ...(q.limit ? { limit: q.limit } : {}),
      }),
      audit.retentionDays(),
    ])
    return c.json({ entries: page.entries, next: page.next, retention_days: retention })
  })

  app.put(`${base}/settings`, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'audit settings changes')
    if (problem) return c.json({ detail: problem }, 403)
    const audit = await repo()
    if (typeof audit === 'string') return c.json({ detail: audit }, 503)
    let body: z.infer<typeof SettingsBody>
    try {
      body = SettingsBody.parse(await c.req.json())
    } catch (err) {
      return c.json({ detail: err instanceof z.ZodError ? issues(err) : 'body is not valid JSON' }, 400)
    }
    const days = await audit.setRetentionDays(body.retention_days, {
      actor: UI_ACTOR,
      surface: 'http',
      clientIp: deps.remoteAddress(c),
    })
    return c.json({ retention_days: days })
  })
}

/**
 * The audit log routes (#258). `audit` is declared in `AppDeps` itself (app.ts), since
 * the write-auditing middleware there uses it too.
 */
export const route: RouteModule = {
  register(app, deps) {
    registerAuditRoutes(app, {
      audit: deps.audit,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
