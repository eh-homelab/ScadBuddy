import { randomUUID } from 'node:crypto'
import type { Context } from 'hono'
import { z } from 'zod'
import type { AuditSink } from '../audit/log.js'
import { requestSession } from '../auth/browserSession.js'
import {
  bindingMismatch,
  type CustomerMessaging,
  type MessagingBinding,
  type MessagingConnection,
  messageArguments,
  MessagingUnavailableError,
} from '../orders/messaging.js'
import { uiRequestProblem } from './guard.js'
import type { RouteModule } from './module.js'

// The messaging binding's two checks for Settings → Orders (#5660, spec
// 2026-11-08-order-notify-customer-design.md §4.3). Neither writes the binding: the page
// saves it as the person with the backend's own PUT /api/v1/settings.
//
//   POST /api/v1/ai/orders/messaging-binding/check  is the plugin there, enabled, a remote MCP
//                                                    server, and does its tool take the binding?
//   POST /api/v1/ai/orders/messaging-binding/test   one real message to an address the person
//                                                    types, through the same client as a send
//
// Both come only from the UI (guard.ts `uiRequestProblem`). The test sends a real message, so
// it needs a person's signed-in session (auth/browserSession.ts `requestSession`): no tool, no
// flow and no /mcp client reaches it. It belongs to no order, so the agent's audit log keeps it
// (kind `plugin`, action `messaging.test_send`), never an order's record.

declare module '../app.js' {
  interface AppDeps {
    /** The messaging client (main.ts `toolServices.messaging`); without it both answer 503. */
    orderMessaging?: CustomerMessaging | undefined
  }
}

const ARG = z.string().regex(/^[A-Za-z_][A-Za-z0-9_]{0,63}$/)
const Binding = z.strictObject({
  source: z.string().max(300),
  tool: z.string().min(1).max(128),
  contact_kind: z.string().min(1).max(40),
  args: z.strictObject({
    to: ARG,
    to_shape: z.enum(['list', 'string']).default('list'),
    subject: ARG.nullable().optional(),
    text: ARG,
    idempotency_key: ARG.nullable().optional(),
  }),
  fixed_args: z.record(z.string(), z.union([z.string().max(500), z.number(), z.boolean()])).optional(),
})
const CheckBody = z.strictObject({ binding: Binding })
const TestBody = z.strictObject({ binding: Binding, to: z.string().min(1).max(320) })

export const TEST_SUBJECT = 'ScadBuddy test message'
export const TEST_TEXT = 'This is a test message from ScadBuddy. Customer messages will be sent this way.'

type Deps = { messaging: CustomerMessaging | undefined; audit?: AuditSink | undefined } & Parameters<RouteModule['register']>[1]

async function body<T>(c: Context, schema: z.ZodType<T>): Promise<T | string> {
  if (c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase() !== 'application/json') {
    return 'request body must be application/json'
  }
  const parsed = schema.safeParse(await c.req.json().catch(() => undefined))
  return parsed.success ? parsed.data : parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ')
}

/** Opens the binding's source and checks its tool; the connection, or why it cannot take the binding. */
async function checked(messaging: CustomerMessaging, binding: MessagingBinding): Promise<MessagingConnection | string> {
  let connection: MessagingConnection
  try {
    connection = await messaging.open(binding)
  } catch (err) {
    if (err instanceof MessagingUnavailableError) return err.message
    throw err
  }
  try {
    const tool = (await connection.tools()).find((t) => t.name === binding.tool)
    const problem = tool ? bindingMismatch(binding, tool.inputSchema) : `${binding.source} lists no tool ${binding.tool}`
    if (problem === undefined) return connection
    await connection.close()
    return problem
  } catch {
    await connection.close().catch(() => {})
    return `${binding.source} did not list its tools`
  }
}

export function registerMessagingBindingRoutes(app: Parameters<RouteModule['register']>[0], deps: Deps): void {
  const base = '/api/v1/ai/orders/messaging-binding'

  app.post(`${base}/check`, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'messaging binding checks')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.messaging) return c.json({ detail: 'this agent cannot reach plugins' }, 503)
    const parsed = await body(c, CheckBody)
    if (typeof parsed === 'string') return c.json({ detail: parsed }, 400)
    const result = await checked(deps.messaging, parsed.binding as MessagingBinding)
    if (typeof result === 'string') return c.json({ ok: false, detail: result }, 422)
    await result.close()
    return c.json({ ok: true })
  })

  app.post(`${base}/test`, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'a test message')
    if (problem) return c.json({ detail: problem }, 403)
    if (!requestSession(c)) return c.json({ detail: 'a test message needs a person signed in' }, 401)
    if (!deps.messaging) return c.json({ detail: 'this agent cannot reach plugins' }, 503)
    const parsed = await body(c, TestBody)
    if (typeof parsed === 'string') return c.json({ detail: parsed }, 400)
    const binding = parsed.binding as MessagingBinding
    const result = await checked(deps.messaging, binding)
    if (typeof result === 'string') return c.json({ ok: false, detail: result }, 422)
    const startedAt = new Date()
    let outcome
    try {
      outcome = await result.send(
        messageArguments(binding, {
          to: [{ value: parsed.to }],
          subject: binding.args.subject ? TEST_SUBJECT : null,
          text: TEST_TEXT,
          key: randomUUID(),
        }),
      )
    } finally {
      await result.close().catch(() => {})
    }
    await deps.audit?.record({
      kind: 'plugin',
      action: 'messaging.test_send',
      surface: 'http',
      actor: { kind: 'browser', id: requestSession(c)?.principal ?? 'browser', label: 'You' },
      clientIp: deps.remoteAddress(c),
      outcome: outcome.status === 'sent' ? 'ok' : 'error',
      inputSummary: `test message to ${parsed.to} through ${binding.source} ${binding.tool}`,
      detail: outcome.status === 'sent' ? 'sent' : `${outcome.status}: ${outcome.reason}`,
      startedAt,
      finishedAt: new Date(),
    })
    return c.json({ ok: outcome.status === 'sent', status: outcome.status, ...(outcome.status === 'sent' ? {} : { reason: outcome.reason }) })
  })
}

export const route: RouteModule = {
  register(app, deps) {
    registerMessagingBindingRoutes(app, { ...deps, messaging: deps.orderMessaging, audit: deps.audit })
  },
}
