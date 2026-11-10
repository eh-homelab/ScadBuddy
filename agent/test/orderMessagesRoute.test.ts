import { Hono } from 'hono'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { BrowserSessions, personSessions, SESSION_COOKIE } from '../src/auth/browserSession.js'
import type { CustomerMessaging, MessagingBinding, MessagingConnection } from '../src/orders/messaging.js'
import { registerOrderMessageRoutes } from '../src/routes/orderMessages.js'
import { BACKEND, LOOPBACK, ORIGINS } from './helpers/mcp.js'

// #5660 PR 3: the order page's "Send again". A person previews the message, then sends it by
// its digest; the server composes it again and refuses one that changed. Only the UI, with a
// person's session, reaches either route.

const ORDER = '0e5a3c1e-1111-4222-8333-944455556666'
const CUSTOMER = 'c0ffee00-1111-4222-8333-944455556666'
const TOKEN = 'a'.repeat(43)
const BINDING: MessagingBinding = {
  source: 'remote:mail',
  tool: 'send_email',
  contact_kind: 'email',
  args: { to: 'to', to_shape: 'list', subject: 'subject', text: 'body', idempotency_key: null },
}

function signedIn(): BrowserSessions {
  const sql = (() => Promise.resolve([{ principal: 'user:https://idp.test#alice', name: 'Alice', kind: 'login' }])) as unknown as Sql
  return new BrowserSessions(sql)
}

/** The backend as a send reads and writes it; `customer.name` may change between calls. */
function fakeBackend() {
  const customer = { name: 'Ada' }
  /** A rival claim won between the preview and this send: the backend answers 409. */
  const race = { lost: false }
  const claims: { body: Record<string, unknown>; key: string | null; author: string | null }[] = []
  const backend = createBackendClient(BACKEND, async (input) => {
    const request = input as Request
    const path = new URL(request.url).pathname
    if (request.method === 'GET' && path === `/api/v1/orders/${ORDER}`) {
      return Response.json({
        order: { number: 'ORD-2026-7', currency: 'USD', minor_exponent: 2, customer_id: CUSTOMER },
        totals: { total_minor: 4250 },
      })
    }
    if (request.method === 'GET' && path === `/api/v1/customers/${CUSTOMER}`) {
      return Response.json({ display_name: customer.name, contacts: [{ kind: 'email', value: 'ada@example.com', primary: true }] })
    }
    if (request.method === 'GET' && path === '/api/v1/settings') return Response.json({ orders_customer_messaging: BINDING })
    if (request.method === 'GET' && path.endsWith('/messages/check')) return Response.json({ already_notified: null })
    if (request.method === 'POST' && path === `/api/v1/orders/${ORDER}/messages`) {
      const body = (await request.json()) as Record<string, unknown>
      claims.push({ body, key: request.headers.get('idempotency-key'), author: request.headers.get('x-scadbuddy-agent-author') })
      if (race.lost) {
        return Response.json(
          { type: 'https://scadbuddy.dev/problems/orders/already_notified', title: 'Conflict', detail: "ORD-2026-7's customer was already sent 'ready'" },
          { status: 409 },
        )
      }
      return Response.json({ id: 'm2', status: 'sending', ...body }, { status: 201 })
    }
    if (request.method === 'POST' && path.endsWith('/outcome')) {
      return Response.json({ id: 'm2', ...((await request.json()) as object) })
    }
    return Response.json({ detail: `unexpected ${request.method} ${path}` }, { status: 500 })
  })
  return { backend, customer, race, claims }
}

/** The plugin tool's inputSchema as BINDING expects it. */
const SEND_EMAIL = {
  type: 'object',
  properties: { to: { type: 'array', items: { type: 'string' } }, subject: { type: 'string' }, body: { type: 'string' } },
  required: ['to', 'body'],
}

function fakeMessaging() {
  const sends: Record<string, unknown>[] = []
  const messaging: CustomerMessaging = {
    async open() {
      const connection: MessagingConnection = {
        tools: async () => [{ name: 'send_email', inputSchema: SEND_EMAIL }],
        send: async (args) => (sends.push(args), { status: 'sent' }),
        close: async () => {},
      }
      return connection
    },
  }
  return { messaging, sends }
}

function app(sessions?: BrowserSessions) {
  const { backend, customer, race, claims } = fakeBackend()
  const { messaging, sends } = fakeMessaging()
  const hono = new Hono()
  hono.use('/api/v1/ai/*', personSessions(sessions, 'off'))
  registerOrderMessageRoutes(hono, {
    sends: { backend, messaging },
    origins: ORIGINS,
    remoteAddress: () => LOOPBACK,
  } as never)
  return { hono, customer, race, claims, sends }
}

const post = (hono: Hono, path: string, body: unknown, headers: Record<string, string> = {}) =>
  hono.request(`http://127.0.0.1/api/v1/ai/orders/${ORDER}/messages/${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', origin: 'http://127.0.0.1', host: '127.0.0.1', ...headers },
    body: JSON.stringify(body),
  })
const COOKIE = { cookie: `${SESSION_COOKIE}=${TOKEN}` }
const AGAIN = { template: 'ready', resend_of: 'm1' }

describe('the order page’s Send again (#5660)', () => {
  it('refuses a preview and a send without a signed-in person', async () => {
    const { hono, claims, sends } = app()
    expect((await post(hono, 'preview', AGAIN)).status).toBe(401)
    expect((await post(hono, 'send', { ...AGAIN, digest: 'a'.repeat(64) }, { 'idempotency-key': 'click-0001' })).status).toBe(401)
    expect(claims).toEqual([])
    expect(sends).toEqual([])
  })

  it('refuses both from another origin', async () => {
    const { hono } = app(signedIn())
    for (const path of ['preview', 'send']) {
      const foreign = await post(hono, path, AGAIN, { ...COOKIE, origin: 'https://evil.example' })
      expect(foreign.status).toBe(403)
    }
  })

  it('sends the previewed message once, composed by the server, as the person', async () => {
    const { hono, claims, sends } = app(signedIn())
    const preview = await post(hono, 'preview', AGAIN, COOKIE)
    expect(preview.status).toBe(200)
    const { message, digest } = (await preview.json()) as { message: { text: string }; digest: string }
    expect(message.text).toContain('Order ORD-2026-7 is ready.')
    expect(claims).toEqual([])

    const sent = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0001' })
    expect(sent.status).toBe(200)
    expect(await sent.json()).toMatchObject({ sent: true })
    expect(sends).toHaveLength(1)
    expect(sends[0]).toMatchObject({ to: ['ada@example.com'], body: message.text })
    expect(claims).toHaveLength(1)
    expect(claims[0]!.body).toMatchObject({ resend_of: 'm1', text: message.text })
    expect(claims[0]!.author).toBe('user:https://idp.test#alice')
  })

  it('refuses message_changed when the message composed now is not the one previewed', async () => {
    const { hono, customer, claims, sends } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    customer.name = 'Ada Lovelace'
    const changed = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0002' })
    expect(changed.status).toBe(409)
    expect(await changed.json()).toMatchObject({ type: 'message_changed' })
    expect(claims).toEqual([])
    expect(sends).toEqual([])
  })

  it('answers a claim that lost a race with the backend’s 409 already_notified', async () => {
    const { hono, race, claims, sends } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    race.lost = true
    const lost = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0004' })
    expect(lost.status).toBe(409)
    expect(await lost.json()).toMatchObject({ type: 'already_notified' })
    expect(claims).toHaveLength(1)
    expect(sends).toEqual([])
  })

  it('takes no text from the browser and needs a key to send', async () => {
    const { hono } = app(signedIn())
    const withText = await post(hono, 'send', { ...AGAIN, digest: 'a'.repeat(64), text: 'mine' }, { ...COOKIE, 'idempotency-key': 'click-0003' })
    expect(withText.status).toBe(400)
    expect((await post(hono, 'send', { ...AGAIN, digest: 'a'.repeat(64) }, COOKIE)).status).toBe(400)
  })
})
