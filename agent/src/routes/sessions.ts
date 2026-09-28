import type { Context, Hono } from 'hono'
import { streamSSE } from 'hono/streaming'
import { z } from 'zod'
import type { OriginPolicy } from '../http/origins.js'
import { MESSAGE_MAX } from '../sessions/clientProtocol.js'
import { type SessionManager, SessionError, type SessionRecord } from '../sessions/manager.js'
import { SESSION_STATUSES } from '../sessions/protocol.js'
import { BROWSER_USER } from './approvals.js'
import { jsonBodyLimit, type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'

// /api/v1/ai/sessions (#300): the same sessions as the chat socket
// (routes/chat.ts), over plain HTTP, for anything that is not the panel: a
// script on the pod, a test, a future Settings view. Every route acts as the
// browser user, guarded like the approval routes (guard.ts): writes need the
// UI's origin through the HTTPS ingress, reads the UI's origin as far as a
// same-origin GET shows it. Approvals are decided through
// /api/v1/ai/approvals (routes/approvals.ts, #258).
//
//   GET  /api/v1/ai/sessions[?status=&limit=]     list, newest first
//   POST /api/v1/ai/sessions                      {title?, prompt?} → 201 {session, turn_id?}
//   GET  /api/v1/ai/sessions/:id                  one session
//   POST /api/v1/ai/sessions/:id/messages         {text} → 202 {turn_id}; 409 while a turn runs
//   GET  /api/v1/ai/sessions/:id/events           Server-Sent Events: the panel-protocol
//                                                 events, replayed from `?after=` or
//                                                 `Last-Event-ID`, then live; `id:` is the seq,
//                                                 and every event is an unnamed `message`
//                                                 (its `type` is in the JSON)
//   POST /api/v1/ai/sessions/:id/interrupt        {interrupted}
//   POST /api/v1/ai/sessions/:id/handoff          take the session over as the browser user
//
// Error bodies are `{ detail }`, the backend's FastAPI shape; a SessionError's
// status is used as it is (404, 403, 409, 400).

export type SessionRouteDeps = {
  sessions: SessionManager | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type SessionView = {
  id: string
  title: string
  origin: SessionRecord['origin']
  owner: SessionRecord['owner']
  status: SessionRecord['status']
  parent_id: string | null
  turns: number
  cost_usd: number
  budget_usd: number
  running: boolean
  created_at: string
  updated_at: string
}

export function sessionView(s: SessionRecord): SessionView {
  return {
    id: s.id,
    title: s.title,
    origin: s.origin,
    owner: s.owner,
    status: s.status,
    parent_id: s.parentId,
    turns: s.turns,
    cost_usd: s.costUsd,
    budget_usd: s.budgetUsd,
    running: s.turnActive,
    created_at: s.createdAt,
    updated_at: s.updatedAt,
  }
}

const StartBody = z.strictObject({
  title: z.string().max(200).optional(),
  prompt: z.string().min(1).max(MESSAGE_MAX).optional(),
})
const SendBody = z.strictObject({ text: z.string().min(1).max(MESSAGE_MAX) })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

/** How long an idle event stream waits between SSE comments, so proxies keep it open. */
export const SSE_KEEPALIVE_MS = 20_000

type Parsed<T> = { ok: true; value: T } | { ok: false; response: Response }

async function jsonBody<T>(c: Context, schema: z.ZodType<T>, empty: T | undefined): Promise<Parsed<T>> {
  const raw = await c.req.text()
  if (raw.trim() === '' && empty !== undefined) return { ok: true, value: empty }
  const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
  if (type !== 'application/json') {
    return { ok: false, response: c.json({ detail: 'request body must be application/json' }, 415) }
  }
  try {
    return { ok: true, value: schema.parse(JSON.parse(raw)) }
  } catch (err) {
    const detail =
      err instanceof z.ZodError
        ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
        : 'body is not valid JSON'
    return { ok: false, response: c.json({ detail }, 400) }
  }
}

/** The `:id` path parameter; '' (not a session: 404) if a route ever lacks one. */
function idOf(c: Context): string {
  return c.req.param('id') ?? ''
}

function seqFrom(value: string | undefined): number | undefined {
  if (value === undefined || !/^\d{1,15}$/.test(value.trim())) return undefined
  return Number(value.trim())
}

export function registerSessionRoutes(app: Hono, deps: SessionRouteDeps): void {
  const base = '/api/v1/ai/sessions'

  async function manager(): Promise<SessionManager | string> {
    if (!deps.sessions) return NO_DATABASE
    return (await deps.ready()) ? deps.sessions : NOT_READY
  }

  /** Guard, then the manager, then `fn`; SessionErrors become their status. */
  function route(kind: 'read' | 'write', fn: (c: Context, sessions: SessionManager) => Promise<Response>) {
    return async (c: Context) => {
      const problem =
        kind === 'read'
          ? uiReadProblem(c, deps.origins, deps.remoteAddress, 'session reads')
          : uiRequestProblem(c, deps.origins, deps.remoteAddress, 'session changes')
      if (problem) return c.json({ detail: problem }, 403)
      const sessions = await manager()
      if (typeof sessions === 'string') return c.json({ detail: sessions }, 503)
      try {
        return await fn(c, sessions)
      } catch (err) {
        if (err instanceof SessionError) return c.json({ detail: err.message }, err.status)
        throw err
      }
    }
  }

  app.get(
    base,
    route('read', async (c, sessions) => {
      const status = c.req.query('status')
      if (status !== undefined && !(SESSION_STATUSES as readonly string[]).includes(status)) {
        return c.json({ detail: `status must be one of ${SESSION_STATUSES.join(', ')}` }, 400)
      }
      const limit = seqFrom(c.req.query('limit'))
      const list = await sessions.list(BROWSER_USER, {
        ...(status ? { status: status as SessionRecord['status'] } : {}),
        ...(limit ? { limit } : {}),
      })
      return c.json({ sessions: list.map(sessionView) })
    }),
  )

  // Bodies are capped before they are read (guard.ts JSON_BODY_MAX): 413.
  const limit = jsonBodyLimit()

  app.post(
    base,
    limit,
    route('write', async (c, sessions) => {
      const body = await jsonBody(c, StartBody, {})
      if (!body.ok) return body.response
      const { session, turn } = await sessions.start(BROWSER_USER, {
        origin: 'chat',
        ...(body.value.title ? { title: body.value.title } : {}),
        ...(body.value.prompt ? { prompt: body.value.prompt } : {}),
      })
      return c.json({ session: sessionView(session), ...(turn ? { turn_id: turn.turnId } : {}) }, 201)
    }),
  )

  app.get(
    `${base}/:id`,
    route('read', async (c, sessions) => c.json(sessionView(await sessions.get(idOf(c), BROWSER_USER)))),
  )

  app.post(
    `${base}/:id/messages`,
    limit,
    route('write', async (c, sessions) => {
      const body = await jsonBody(c, SendBody, undefined)
      if (!body.ok) return body.response
      const turn = await sessions.send(idOf(c), BROWSER_USER, body.value.text)
      return c.json({ turn_id: turn.turnId }, 202)
    }),
  )

  app.post(
    `${base}/:id/interrupt`,
    limit,
    route('write', async (c, sessions) =>
      c.json({ interrupted: await sessions.interrupt(idOf(c), BROWSER_USER) }),
    ),
  )

  app.post(
    `${base}/:id/handoff`,
    limit,
    route('write', async (c, sessions) =>
      c.json(sessionView(await sessions.handoff(idOf(c), BROWSER_USER, BROWSER_USER))),
    ),
  )

  app.get(
    `${base}/:id/events`,
    route('read', async (c, sessions) => {
      const id = idOf(c)
      const after = seqFrom(c.req.query('after')) ?? seqFrom(c.req.header('last-event-id')) ?? 0
      // Checks visibility before the stream starts, so an unknown id is a 404, not an empty stream.
      await sessions.get(id, BROWSER_USER)
      // No proxy in the path may buffer this (spec §8.4); X-Accel-Buffering
      // asks nginx-based ingresses not to.
      c.header('X-Accel-Buffering', 'no')
      c.header('Cache-Control', 'no-cache')
      return streamSSE(c, async (stream) => {
        const controller = new AbortController()
        stream.onAbort(() => controller.abort())
        const keepalive = setInterval(() => {
          void stream.write(': keepalive\n\n').catch(() => controller.abort())
        }, SSE_KEEPALIVE_MS)
        try {
          const events = await sessions.attach(id, BROWSER_USER, { afterSeq: after, signal: controller.signal })
          for await (const { seq, event } of events) {
            await stream.writeSSE({ id: String(seq), data: JSON.stringify(event) })
          }
        } finally {
          clearInterval(keepalive)
          controller.abort()
        }
      })
    }),
  )
}
