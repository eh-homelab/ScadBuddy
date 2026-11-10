import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { ErrorCode, McpError } from '@modelcontextprotocol/sdk/types.js'
import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createBackendClient } from '../src/api/backend.js'
import {
  bindingMismatch,
  type CustomerMessaging,
  type MessagingBinding,
  type MessagingConnection,
  messageArguments,
  MessagingUnavailableError,
  outcomeOfError,
  pluginMessaging,
  type SendOutcome,
} from '../src/orders/messaging.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import type { Owner } from '../src/sessions/protocol.js'
import { connectInProcess } from '../src/temporal/turnTools.js'
import { DESCRIBE_CALL_ACTIVITY, gateActivities, toolActivities } from '../src/temporal/toolActivities.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { runToolWithOutcome, type Tool, type ToolContext } from '../src/tools/registry.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #5660 (spec 2026-11-08-order-notify-customer-design.md §4-§7): a customer notice sent
// through the messaging plugin the shop bound, recorded on the order, once per claim.

const ORDER = '0e5a3c1e-1111-4222-8333-944455556666'
const CUSTOMER = 'c0ffee00-1111-4222-8333-944455556666'
const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }

const BINDING: MessagingBinding = {
  source: 'remote:mail',
  tool: 'send_email',
  contact_kind: 'email',
  args: { to: 'to', to_shape: 'list', subject: 'subject', text: 'body', idempotency_key: null },
  fixed_args: { from: 'shop@example.com' },
}

function tool(name: string): Tool {
  const found = ALL_TOOLS.find((t) => t.name === name)
  if (!found) throw new Error(`no tool ${name}`)
  return found
}

type Message = { id: string; status: string; [key: string]: unknown }

/** The backend as order_notify_customer_send reads and writes it, recording every write. */
function fakeBackend(opts: { binding?: MessagingBinding | null; claim?: { status: number; message: Message }; told?: Message | null } = {}) {
  const writes: { path: string; body: unknown; key: string | null }[] = []
  let message: Message = opts.claim?.message ?? { id: 'm1', status: 'sending' }
  const backend = createBackendClient(BACKEND, async (input) => {
    const request = input as Request
    const url = new URL(request.url)
    const path = url.pathname
    if (request.method === 'GET' && path === `/api/v1/orders/${ORDER}`) {
      return Response.json({
        order: { number: 'ORD-2026-7', currency: 'USD', minor_exponent: 2, customer_id: CUSTOMER },
        totals: { total_minor: 4250 },
      })
    }
    if (request.method === 'GET' && path === `/api/v1/customers/${CUSTOMER}`) {
      return Response.json({
        display_name: 'Ada',
        contacts: [
          { kind: 'phone', value: '+1 555 0100', primary: true },
          { kind: 'email', value: 'ada@example.com', primary: true },
          { kind: 'email', value: 'ada@work.example', primary: false },
        ],
      })
    }
    if (request.method === 'GET' && path === '/api/v1/settings') {
      return Response.json({ orders_customer_messaging: opts.binding === undefined ? BINDING : opts.binding })
    }
    if (request.method === 'GET' && path === `/api/v1/orders/${ORDER}/messages/check`) {
      return Response.json({ already_notified: opts.told ?? null })
    }
    if (request.method === 'POST' && path === `/api/v1/orders/${ORDER}/messages`) {
      writes.push({ path, body: await request.json(), key: request.headers.get('idempotency-key') })
      return Response.json(message, { status: opts.claim?.status ?? 201 })
    }
    if (request.method === 'POST' && path.endsWith('/outcome')) {
      const body = (await request.json()) as { status: string }
      writes.push({ path, body, key: null })
      message = { ...message, ...body }
      return Response.json(message)
    }
    return Response.json({ detail: `unexpected ${request.method} ${path}` }, { status: 500 })
  })
  return { backend, writes }
}

/** The plugin tool's `inputSchema` as the binding expects it. */
const SEND_EMAIL_SCHEMA = {
  type: 'object',
  properties: {
    from: { type: 'string' },
    to: { type: 'array', items: { type: 'string' } },
    subject: { type: 'string' },
    body: { type: 'string' },
    dedupe: { type: 'string' },
  },
  required: ['to', 'body'],
}

/** A messaging client that records each send and answers `outcome`; its tool takes `inputSchema`. */
function fakeMessaging(outcome: SendOutcome | 'unavailable' = { status: 'sent', providerMessageId: 'p-1' }, inputSchema: unknown = SEND_EMAIL_SCHEMA) {
  const sends: Record<string, unknown>[] = []
  const messaging: CustomerMessaging = {
    async open() {
      if (outcome === 'unavailable') throw new MessagingUnavailableError('the messaging plugin remote:mail is not installed and enabled')
      const connection: MessagingConnection = {
        tools: async () => [{ name: 'send_email', inputSchema }],
        send: async (args) => {
          sends.push(args)
          return outcome
        },
        close: async () => {},
      }
      return connection
    },
  }
  return { messaging, sends }
}

function ctx(over: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    callKey: 'call-1',
    ...over,
  }
}

const send = tool('order_notify_customer_send')
const notify = tool('order_notify_customer')
const ARGS = { order_id: ORDER, template: 'ready' as const }

describe('order_notify_customer with a binding (#5660)', () => {
  it('stays read and addresses only the bound kind', async () => {
    expect(notify.risk).toBe('read')
    const { backend } = fakeBackend()
    const answer = firstText(await notify.execute(ARGS, ctx({ backend }))) as Record<string, unknown>
    expect(answer).toMatchObject({
      to: [{ kind: 'email', value: 'ada@example.com' }],
      sent: false,
      channel: { source: 'remote:mail', contact_kind: 'email' },
      send_ready: true,
    })
  })

  it('says no channel and not ready with no binding', async () => {
    const { backend } = fakeBackend({ binding: null })
    const answer = firstText(await notify.execute(ARGS, ctx({ backend }))) as Record<string, unknown>
    expect(answer).toMatchObject({ channel: null, send_ready: false, to: [{ kind: 'phone', value: '+1 555 0100' }, { kind: 'email', value: 'ada@example.com' }] })
  })
})

describe('order_notify_customer_send (#5660)', () => {
  it('is outward', () => {
    expect(send.risk).toBe('outward')
  })

  it('sends once, maps the binding, and records the outcome', async () => {
    const { backend, writes } = fakeBackend()
    const { messaging, sends } = fakeMessaging()
    const answer = firstText(await send.execute(ARGS, ctx({ backend, messaging }))) as { sent: boolean; message: Message }
    expect(answer.sent).toBe(true)
    expect(answer.message).toMatchObject({ id: 'm1', status: 'sent', provider_message_id: 'p-1' })
    expect(sends).toEqual([
      { from: 'shop@example.com', to: ['ada@example.com'], subject: 'Order ORD-2026-7 is ready', body: 'Hello Ada,\n\nOrder ORD-2026-7 is ready.' },
    ])
    expect(writes.map((w) => w.path)).toEqual([`/api/v1/orders/${ORDER}/messages`, `/api/v1/orders/${ORDER}/messages/m1/outcome`])
    expect(writes[0]!.key).toMatch(/^[0-9a-f]{64}$/)
    expect(writes[0]!.body).toMatchObject({ template: 'ready', source: 'remote:mail', tool: 'send_email', recipients: [{ kind: 'email', value: 'ada@example.com' }] })
    expect(writes[1]!.body).toEqual({ status: 'sent', provider_message_id: 'p-1' })
  })

  it('records a plugin refusal as failed and an unanswered call as unknown', async () => {
    for (const outcome of [{ status: 'failed', reason: 'plugin_error' }, { status: 'unknown', reason: 'no_answer' }] as const) {
      const { backend, writes } = fakeBackend()
      const { messaging } = fakeMessaging(outcome)
      const answer = firstText(await send.execute(ARGS, ctx({ backend, messaging }))) as { sent: boolean }
      expect(answer.sent).toBe(false)
      expect(writes[1]!.body).toEqual(outcome)
    }
  })

  it('never sends again for a claim already made: an unknown one is recorded unknown', async () => {
    const { backend, writes } = fakeBackend({ claim: { status: 200, message: { id: 'm1', status: 'sending' } } })
    const { messaging, sends } = fakeMessaging()
    const answer = firstText(await send.execute(ARGS, ctx({ backend, messaging }))) as { sent: boolean; warning?: string }
    expect(answer.sent).toBe(false)
    expect(answer.warning).toMatch(/check the inbox, then "Mark sent"/)
    expect(sends).toEqual([])
    expect(writes[1]!.body).toEqual({ status: 'unknown', reason: 'no_answer' })
  })

  it('sends a retried claim again only with the dedupe key the plugin takes, the message id', async () => {
    const keyed = { ...BINDING, args: { ...BINDING.args, idempotency_key: 'dedupe' } }
    const { backend } = fakeBackend({ binding: keyed, claim: { status: 200, message: { id: 'm1', status: 'unknown' } } })
    const { messaging, sends } = fakeMessaging()
    await send.execute(ARGS, ctx({ backend, messaging }))
    expect(sends).toEqual([expect.objectContaining({ dedupe: 'm1' })])
  })

  it('answers a finished claim as it is and sends nothing', async () => {
    const { backend, writes } = fakeBackend({ claim: { status: 200, message: { id: 'm1', status: 'sent' } } })
    const { messaging, sends } = fakeMessaging()
    const answer = firstText(await send.execute(ARGS, ctx({ backend, messaging }))) as { sent: boolean }
    expect(answer.sent).toBe(true)
    expect(sends).toEqual([])
    expect(writes).toHaveLength(1)
  })

  it('refuses with the text when no plugin is bound, and records nothing', async () => {
    const { backend, writes } = fakeBackend({ binding: null })
    const { messaging, sends } = fakeMessaging()
    const refused = send.execute(ARGS, ctx({ backend, messaging }))
    await expect(refused).rejects.toThrow(/no_messaging_plugin[\s\S]*Hello Ada/)
    expect(sends).toEqual([])
    expect(writes).toEqual([])
  })

  it('refuses an unreachable plugin before it claims anything', async () => {
    const { backend, writes } = fakeBackend()
    const { messaging } = fakeMessaging('unavailable')
    await expect(send.execute(ARGS, ctx({ backend, messaging }))).rejects.toThrow('messaging_plugin_unavailable')
    expect(writes).toEqual([])
  })

  it('checks the binding against the tool as it is now, before it claims anything (§4.3)', async () => {
    const changed = [
      { ...SEND_EMAIL_SCHEMA, properties: { ...SEND_EMAIL_SCHEMA.properties, to: { type: 'string' } } },
      { ...SEND_EMAIL_SCHEMA, required: ['to', 'body', 'reply_to'] },
    ]
    for (const schema of changed) {
      const { backend, writes } = fakeBackend()
      const { messaging, sends } = fakeMessaging(undefined, schema)
      await expect(send.execute(ARGS, ctx({ backend, messaging }))).rejects.toThrow(/messaging_plugin_unavailable: .*nothing was sent/)
      expect(writes).toEqual([])
      expect(sends).toEqual([])
    }
    const { backend, writes } = fakeBackend({ binding: { ...BINDING, tool: 'send_mail' } })
    const { messaging } = fakeMessaging()
    await expect(send.execute(ARGS, ctx({ backend, messaging }))).rejects.toThrow('remote:mail lists no tool send_mail')
    expect(writes).toEqual([])
  })

  it('refuses a message that changed since its approval, and sends nothing', async () => {
    const { backend, writes } = fakeBackend()
    const { messaging, sends } = fakeMessaging()
    const approved = { source: 'remote:mail', tool: 'send_email', to: [{ kind: 'email', value: 'old@example.com' }], subject: 'x', text: 'y' }
    await expect(send.execute({ ...ARGS, approved }, ctx({ backend, messaging }))).rejects.toThrow('message_changed')
    expect(sends).toEqual([])
    expect(writes).toEqual([])
  })

  it('parks the exact message for a person over /mcp, and refuses one already told', async () => {
    const { backend } = fakeBackend()
    const pending = new PendingActionStore()
    const run = await runToolWithOutcome(send, ARGS, { ...ctx({ backend, messaging: fakeMessaging().messaging }), pending })
    expect(run.outcome).toBe('refused')
    const said = JSON.stringify(run.result)
    expect(said).toContain('pending_approval')
    expect(said).toContain('ada@example.com')
    expect(said).toContain('Order ORD-2026-7 is ready.')

    const told = fakeBackend({ told: { id: 'm0', status: 'sent' } })
    const again = await runToolWithOutcome(send, ARGS, { ...ctx({ backend: told.backend }), pending })
    expect(JSON.stringify(again.result)).toContain('already_notified')
  })
})

// Every tool works in every mode (#2171): the durable path is a ToolCall activity whose
// approval binds the parked message (#4442).
describe('order_notify_customer_send as a ToolCall activity (#5660)', () => {
  function env(activityId: string) {
    return new MockActivityEnvironment({
      activityId,
      workflowExecution: { workflowId: `session-${SESSION}`, runId: 'run-1' },
      activityType: 'order_notify_customer_send',
      taskQueue: 'agent-tools',
    })
  }
  const audit = {
    record: async () => {},
    hash: (name: string, input: Record<string, unknown>) => `${name}:${JSON.stringify(input)}`,
    summarise: (name: string) => `summary:${name}`,
  }
  const sessions = { ownerOf: async (id: string) => (id === SESSION ? BROWSER : undefined) }

  it('describes the exact message, runs once on the approval of that hash, and refuses a changed one', async () => {
    const { backend, writes } = fakeBackend()
    const { messaging, sends } = fakeMessaging()
    const svc = services({ backend, messaging })
    const describeCall = gateActivities({ audit, sessions, tools: [send], services: svc })[DESCRIBE_CALL_ACTIVITY]!
    const raw: unknown = await env('tool-toolu_01').run(describeCall, { tool: 'order_notify_customer_send', input: ARGS })
    const described = raw as { summary: string; input_hash: string }
    expect(described.summary).toContain('to ada@example.com through remote:mail')
    expect(described.input_hash).toContain('"approved"')
    const approvals = { approved: async (_id: string, call?: { inputHash: string }) => call?.inputHash === described.input_hash }
    const activities = toolActivities([send], { services: svc, sessions, approvals, audit })
    const content = await env('tool-toolu_01').run(activities.order_notify_customer_send!, ARGS)
    expect(JSON.parse(unwrapUntrusted(content as string))).toMatchObject({ sent: true })
    expect(sends).toHaveLength(1)
    expect(writes.filter((w) => w.path.endsWith('/messages'))).toHaveLength(1)

    // The customer's contact changed since: the hash no longer matches what was approved.
    const changed = fakeBackend({ binding: { ...BINDING, contact_kind: 'phone' } })
    const moved = toolActivities([send], { services: services({ backend: changed.backend, messaging }), sessions, approvals, audit })
    const err = await env('tool-toolu_01')
      .run(moved.order_notify_customer_send!, ARGS)
      .then(
        () => undefined,
        (e: unknown) => e,
      )
    expect(err).toBeInstanceOf(ApplicationFailure)
    expect((err as ApplicationFailure).type).toBe('NotApproved')
    expect(sends).toHaveLength(1)
  })
})

describe('the messaging client (#5660 §6.1)', () => {
  /** A mail plugin in process: send_email answers by its `body`. */
  function mailServer() {
    const calls: Record<string, unknown>[] = []
    const make = () => {
      const server = new McpServer({ name: 'mail', version: '1' })
      server.registerTool(
        'send_email',
        { inputSchema: { to: z.array(z.string()), subject: z.string(), body: z.string(), from: z.string() } },
        async (args) => {
          calls.push(args)
          if (args.body === 'refuse') return { isError: true, content: [{ type: 'text' as const, text: 'bounced: secret detail' }] }
          return { content: [{ type: 'text' as const, text: 'ok' }], structuredContent: { message_id: 'p-9' } }
        },
      )
      return server
    }
    return { make, calls }
  }
  const plugin = (over: Partial<{ disabledTools: string[] }> = {}) => ({ name: 'mail', url: 'mem://mail', toolTiers: {}, disabledTools: [], ...over })

  it('sends through a remote plugin and keeps only its id', async () => {
    const { make, calls } = mailServer()
    let released = 0
    const messaging = pluginMessaging({
      remote: async () => ({ plugins: [plugin()], release: () => void released++ }),
      connect: () => connectInProcess(make),
    })
    const connection = await messaging.open(BINDING)
    expect(bindingMismatch(BINDING, (await connection.tools()).find((t) => t.name === 'send_email')!.inputSchema)).toBeUndefined()
    const args = messageArguments(BINDING, { to: [{ value: 'ada@example.com' }], subject: 'S', text: 'T', key: 'k' })
    expect(await connection.send(args)).toEqual({ status: 'sent', providerMessageId: 'p-9' })
    expect(await connection.send({ ...args, body: 'refuse' })).toEqual({ status: 'failed', reason: 'plugin_error' })
    await connection.close()
    expect(calls).toHaveLength(2)
    expect(released).toBe(1)
  })

  it('refuses a source it cannot reach, a disabled tool, and a local server', async () => {
    const remote = async () => ({ plugins: [plugin({ disabledTools: ['send_email'] })], release: () => {} })
    const messaging = pluginMessaging({ remote, connect: () => connectInProcess(mailServer().make) })
    await expect(messaging.open({ source: 'remote:other', tool: 'send_email' })).rejects.toBeInstanceOf(MessagingUnavailableError)
    await expect(messaging.open(BINDING)).rejects.toThrow('switched off')
    await expect(messaging.open({ source: 'stdio:mail', tool: 'send_email' })).rejects.toThrow('not a remote MCP server')
    await expect(messaging.open({ source: 'package:kit/mail', tool: 'send_email' })).rejects.toThrow('not a remote MCP server')
    const down = pluginMessaging({
      remote: async () => ({ plugins: [plugin()], release: () => {} }),
      connect: async () => {
        throw new Error('ECONNREFUSED')
      },
    })
    await expect(down.open(BINDING)).rejects.toThrow('did not answer')
  })

  it('finds a package server by package and server', async () => {
    const messaging = pluginMessaging({
      packages: async () => ({ plugins: [plugin()], servers: [{ pkg: 'kit', server: 'mail' }], release: () => {} }),
      connect: () => connectInProcess(mailServer().make),
    })
    const connection = await messaging.open({ source: 'package:kit/mail', tool: 'send_email' })
    await connection.close()
  })

  it('reads a refusal the server answered as failed, and a timeout or a dropped link as unknown', () => {
    expect(outcomeOfError(new McpError(ErrorCode.InvalidParams, 'no such tool'))).toEqual({ status: 'failed', reason: 'plugin_error' })
    expect(outcomeOfError(new McpError(ErrorCode.RequestTimeout, 'timed out'))).toEqual({ status: 'unknown', reason: 'no_answer' })
    expect(outcomeOfError(new McpError(ErrorCode.ConnectionClosed, 'closed'))).toEqual({ status: 'unknown', reason: 'no_answer' })
    expect(outcomeOfError(new TypeError('fetch failed'))).toEqual({ status: 'unknown', reason: 'no_answer' })
  })

  it('maps a binding onto the tool and checks its schema', () => {
    const single = { ...BINDING, args: { ...BINDING.args, to_shape: 'string' as const, subject: null } }
    expect(messageArguments(single, { to: [{ value: 'a@x' }, { value: 'b@x' }], subject: 'S', text: 'T', key: 'k' })).toEqual({
      from: 'shop@example.com',
      to: 'a@x, b@x',
      body: 'T',
    })
    const schema = { type: 'object', properties: { to: { type: 'string' }, body: { type: 'string' } }, required: ['to', 'body', 'api_key'] }
    expect(bindingMismatch(BINDING, schema)).toContain('argument to is not an array of strings')
    expect(bindingMismatch(single, schema)).toContain('requires api_key')
    expect(bindingMismatch(BINDING, { properties: {} })).toContain('takes no argument to')
  })
})

