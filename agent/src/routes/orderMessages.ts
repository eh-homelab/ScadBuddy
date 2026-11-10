import type { Context } from 'hono'
import type { ContentfulStatusCode } from 'hono/utils/http-status'
import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import { requestSession } from '../auth/browserSession.js'
import type { CustomerMessaging } from '../orders/messaging.js'
import { authored, authorHeaders } from '../tools/authorship.js'
import { type PersonSend, personSend, previewPersonSend } from '../tools/orders.js'
import { ToolError } from '../tools/registry.js'
import { uiRequestProblem } from './guard.js'
import type { RouteModule } from './module.js'

// The order page's "Send again" (#5660 PR 3, spec 2026-11-08-order-notify-customer-design.md
// §7.2): a person sends one of an order's template messages again through the bound messaging
// plugin, without the assistant.
//
//   POST /api/v1/ai/orders/:id/messages/preview  the message a send would send now, and its
//                                                digest; nothing is sent or recorded
//   POST /api/v1/ai/orders/:id/messages/send     sends it when the message composed now has the
//                                                previewed digest, else 409 message_changed
//
// The browser names the template, `resend_of` and the digest it saw; it never supplies the
// text. Both come only from the UI (guard.ts `uiRequestProblem`) with a person's signed-in
// session (auth/browserSession.ts `requestSession`), as the binding's test send: no tool, no
// flow and no /mcp client reaches them. A send is claimed and recorded in `order_messages` as
// the tool's are (tools/orders.ts `personSend`), under the request's `Idempotency-Key`.

declare module '../app.js' {
  interface AppDeps {
    /** What the order page's sends need (main.ts); without it both answer 503. */
    orderSends?: { backend: BackendClient; messaging: CustomerMessaging | undefined } | undefined
  }
}

const TOOL = 'order_notify_customer_send'
const ID = z.string().min(1).max(200)
const Body = z.strictObject({
  template: z.enum(['quoted', 'accepted', 'ready', 'shipped']),
  resend_of: ID.optional(),
})
const SendBody = Body.extend({ digest: z.string().regex(/^[0-9a-f]{64}$/) })
const KEY = /^[A-Za-z0-9_-]{8,128}$/

type Deps = {
  sends: { backend: BackendClient; messaging: CustomerMessaging | undefined } | undefined
} & Parameters<RouteModule['register']>[1]

async function parsed<T>(c: Context, schema: z.ZodType<T>): Promise<T | string> {
  if (c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase() !== 'application/json') {
    return 'request body must be application/json'
  }
  const result = schema.safeParse(await c.req.json().catch(() => undefined))
  return result.success ? result.data : result.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ')
}

/** The backend's own refusals the route forwards as they are; any other is the agent's problem. */
const FORWARDED = new Set([404, 409, 422])

/**
 * A refusal as the route's answer. The agent's own (`message_changed: …`) maps its slug: 409 for
 * the order's state, 503 for the plugin, else 422. A backend 404, 409 or 422 keeps its status and
 * problem slug (a claim that lost a race: 409 `already_notified`). Any other backend status (a 401
 * or 403 is the agent's service credential, not the person) is logged and answered 502
 * `upstream_error`.
 */
function refusal(c: Context, err: ToolError) {
  if (err.status !== undefined) {
    if (FORWARDED.has(err.status)) {
      return c.json({ detail: err.message, ...(err.problem ? { type: err.problem } : {}) }, err.status as ContentfulStatusCode)
    }
    console.error(`order message route: the backend answered HTTP ${err.status}: ${err.summary}`)
    return c.json({ detail: 'the backend refused the agent; nothing was sent', type: 'upstream_error' }, 502)
  }
  const slug = /^([a-z_]+):/.exec(err.message)?.[1]
  const conflict = slug === 'message_changed' || slug === 'already_notified' || slug === 'no_messaging_plugin' || slug === 'no_contact'
  const status = slug === 'messaging_plugin_unavailable' ? 503 : conflict ? 409 : 422
  return c.json({ detail: err.message, ...(slug ? { type: slug } : {}) }, status)
}

export function registerOrderMessageRoutes(app: Parameters<RouteModule['register']>[0], deps: Deps): void {
  const base = '/api/v1/ai/orders/:id/messages'

  /** The guards both routes share: the UI's origin, a person, the services; else the answer. */
  function guarded(c: Context, what: string) {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, what)
    if (problem) return { answer: c.json({ detail: problem }, 403) }
    const session = requestSession(c)
    if (!session) return { answer: c.json({ detail: `${what} needs a person signed in` }, 401) }
    if (!deps.sends) return { answer: c.json({ detail: 'this agent cannot reach plugins' }, 503) }
    const principal = { id: session.principal, kind: 'browser' as const, tiers: [] }
    return {
      ctx: {
        backend: authored(deps.sends.backend, authorHeaders(principal, undefined, TOOL)),
        messaging: deps.sends.messaging,
      },
    }
  }

  app.post(`${base}/preview`, async (c) => {
    const g = guarded(c, 'a message preview')
    if (g.answer) return g.answer
    const body = await parsed(c, Body)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    const args: PersonSend = { order_id: c.req.param('id'), ...body }
    try {
      return c.json(await previewPersonSend(g.ctx, args))
    } catch (err) {
      if (err instanceof ToolError) return refusal(c, err)
      throw err
    }
  })

  app.post(`${base}/send`, async (c) => {
    const g = guarded(c, 'a customer message')
    if (g.answer) return g.answer
    const key = c.req.header('idempotency-key')
    if (!key || !KEY.test(key)) return c.json({ detail: 'an Idempotency-Key header (8 to 128 of A-Z a-z 0-9 _ -) is required' }, 400)
    const body = await parsed(c, SendBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    const { digest, ...rest } = body
    const args: PersonSend = { order_id: c.req.param('id'), ...rest }
    try {
      // One key per open preview (the browser keeps it): a retry after a lost answer is
      // answered from its claim, never sent twice.
      return c.json(await personSend({ ...g.ctx, callKey: `order-page:${args.order_id}:${key}` }, args, digest))
    } catch (err) {
      if (err instanceof ToolError) return refusal(c, err)
      throw err
    }
  })
}

export const route: RouteModule = {
  register(app, deps) {
    registerOrderMessageRoutes(app, { ...deps, sends: deps.orderSends })
  },
}
