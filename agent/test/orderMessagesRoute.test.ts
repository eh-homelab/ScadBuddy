import { Hono } from 'hono'
import type { Sql } from 'postgres'
import { describe, expect, it, vi } from 'vitest'
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

/** A binding that gives the plugin a dedupe key (§7.2 step 4: a replayed claim may be sent again). */
const KEYED: MessagingBinding = { ...BINDING, args: { ...BINDING.args, idempotency_key: 'key' } }

/**
 * The backend as a send reads and writes it. `customer.name`, `shop` (the order's number, the
 * binding, the contacts) may change between calls; `shop.outcomeStatus` fails the outcome post.
 */
function fakeBackend() {
  const customer = { name: 'Ada' }
  const shop = {
    number: 'ORD-2026-7',
    binding: BINDING as MessagingBinding | null,
    contacts: [{ kind: 'email', value: 'ada@example.com', primary: true }],
    outcomeStatus: 0,
  }
  /** What the backend answers the claim with instead of a claim (a rival claim won: 409). */
  const race: { refusal: { status: number; body: object } | null } = { refusal: null }
  const claims: { body: Record<string, unknown>; key: string | null; author: string | null }[] = []
  /** The check's `storefront_mail`: the storefront mails this news to the order itself. */
  const check = { storefront_mail: false }
  /** The claims by their Idempotency-Key, as the backend keeps them. */
  const byKey = new Map<string, Record<string, unknown>>()
  const backend = createBackendClient(BACKEND, async (input) => {
    const request = input as Request
    const path = new URL(request.url).pathname
    if (request.method === 'GET' && path === `/api/v1/orders/${ORDER}`) {
      return Response.json({
        order: { number: shop.number, currency: 'USD', minor_exponent: 2, customer_id: CUSTOMER },
        totals: { total_minor: 4250 },
      })
    }
    if (request.method === 'GET' && path === `/api/v1/customers/${CUSTOMER}`) {
      return Response.json({ display_name: customer.name, contacts: shop.contacts })
    }
    if (request.method === 'GET' && path === '/api/v1/settings') return Response.json({ orders_customer_messaging: shop.binding })
    if (request.method === 'GET' && path.endsWith('/messages/check')) {
      // As the backend's: a key that already claimed answers its row and no refusal.
      const claimed = byKey.get(request.headers.get('idempotency-key') ?? '')
      if (claimed) return Response.json({ already_notified: null, storefront_mail: false, claimed })
      return Response.json({ already_notified: null, ...check, claimed: null })
    }
    if (request.method === 'POST' && path === `/api/v1/orders/${ORDER}/messages`) {
      const body = (await request.json()) as Record<string, unknown>
      const key = request.headers.get('idempotency-key')
      claims.push({ body, key, author: request.headers.get('x-scadbuddy-agent-author') })
      if (race.refusal) return Response.json(race.refusal.body, { status: race.refusal.status })
      const seen = byKey.get(key ?? '')
      if (seen) return Response.json(seen, { status: 200 })
      const row = { id: 'm2', status: 'sending', ...body }
      byKey.set(key ?? '', row)
      return Response.json(row, { status: 201 })
    }
    if (request.method === 'POST' && path.endsWith('/outcome')) {
      const outcome = (await request.json()) as object
      if (shop.outcomeStatus) return Response.json({ detail: 'down' }, { status: shop.outcomeStatus })
      for (const row of byKey.values()) Object.assign(row, outcome)
      return Response.json({ id: 'm2', ...outcome })
    }
    return Response.json({ detail: `unexpected ${request.method} ${path}` }, { status: 500 })
  })
  return { backend, customer, race, claims, check, shop }
}

/** The plugin tool's inputSchema as BINDING expects it. */
const SEND_EMAIL = {
  type: 'object',
  properties: {
    to: { type: 'array', items: { type: 'string' } },
    subject: { type: 'string' },
    body: { type: 'string' },
    key: { type: 'string' },
  },
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
  const { backend, customer, race, claims, check, shop } = fakeBackend()
  const { messaging, sends } = fakeMessaging()
  const hono = new Hono()
  hono.use('/api/v1/ai/*', personSessions(sessions, 'off'))
  registerOrderMessageRoutes(hono, {
    sends: { backend, messaging },
    origins: ORIGINS,
    remoteAddress: () => LOOPBACK,
  } as never)
  return { hono, customer, race, claims, sends, check, shop }
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
    race.refusal = {
      status: 409,
      body: { type: 'https://scadbuddy.dev/problems/orders/already_notified', title: 'Conflict', detail: "ORD-2026-7's customer was already sent 'ready'" },
    }
    const lost = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0004' })
    expect(lost.status).toBe(409)
    expect(await lost.json()).toMatchObject({ type: 'already_notified' })
    expect(claims).toHaveLength(1)
    expect(sends).toEqual([])
  })

  it('answers storefront_mail as 409 on the preview and the send, and sends with the person’s yes', async () => {
    const { hono, claims, sends, check } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    check.storefront_mail = true
    const previewed = await post(hono, 'preview', AGAIN, COOKIE)
    expect(previewed.status).toBe(409)
    expect(await previewed.json()).toMatchObject({ type: 'storefront_mail' })
    const refused = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0010' })
    expect(refused.status).toBe(409)
    expect(await refused.json()).toMatchObject({ type: 'storefront_mail' })
    expect(claims).toEqual([])
    expect(sends).toEqual([])

    const also = { ...AGAIN, also_when_storefront_mails: true }
    const again = await post(hono, 'preview', also, COOKIE)
    expect(again.status).toBe(200)
    const sent = await post(hono, 'send', { ...also, digest }, { ...COOKIE, 'idempotency-key': 'click-0011' })
    expect(sent.status).toBe(200)
    expect(claims[0]!.body).toMatchObject({ also_when_storefront_mails: true })
    expect(sends).toHaveLength(1)
  })

  it('serves a replay under the same key from its first claim, after the storefront began to mail it', async () => {
    const { hono, claims, sends, check } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    const headers = { ...COOKIE, 'idempotency-key': 'click-0020' }
    expect((await post(hono, 'send', { ...AGAIN, digest }, headers)).status).toBe(200)
    expect(sends).toHaveLength(1)
    check.storefront_mail = true
    const replay = await post(hono, 'send', { ...AGAIN, digest }, headers)
    expect(replay.status).toBe(200)
    expect(await replay.json()).toMatchObject({ sent: true, message: { id: 'm2', status: 'sent' } })
    expect(sends).toHaveLength(1)
    // The check under the key answered the sent row: no second claim, no plugin.
    expect(claims).toHaveLength(1)
  })

  it('serves a replay under the same key from its first claim after the order changed (#5978)', async () => {
    const { hono, customer, sends } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    const headers = { ...COOKIE, 'idempotency-key': 'click-0030' }
    expect((await post(hono, 'send', { ...AGAIN, digest }, headers)).status).toBe(200)
    expect(sends).toHaveLength(1)
    // The answer was lost; the customer changes before the person clicks Send again.
    customer.name = 'Ada Lovelace'
    const replay = await post(hono, 'send', { ...AGAIN, digest }, headers)
    expect(replay.status).toBe(200)
    expect(await replay.json()).toMatchObject({ sent: true, message: { id: 'm2', status: 'sent' } })
    expect(sends).toHaveLength(1)
    // A new key still meets the digest check.
    const fresh = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0031' })
    expect(fresh.status).toBe(409)
    expect(await fresh.json()).toMatchObject({ type: 'message_changed' })
    expect(sends).toHaveLength(1)
  })

  // #5980 review: a claimed row is the record. A retry under its key never sends a message
  // composed now; a `sending` one goes again only with a dedupe key, as the row recorded it.
  for (const change of ['customer', 'order'] as const) {
    it(`sends a keyed retry of a claim with no outcome as first recorded, after the ${change} changed`, async () => {
      const { hono, customer, sends, shop } = app(signedIn())
      shop.binding = KEYED
      const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
      const headers = { ...COOKIE, 'idempotency-key': 'click-0040' }
      shop.outcomeStatus = 503
      const first = await post(hono, 'send', { ...AGAIN, digest }, headers)
      expect(first.status).toBe(200)
      expect(sends).toHaveLength(1)
      shop.outcomeStatus = 0
      if (change === 'customer') customer.name = 'Ada Lovelace'
      else shop.number = 'ORD-2026-8'
      const retry = await post(hono, 'send', { ...AGAIN, digest }, headers)
      expect(retry.status).toBe(200)
      expect(sends).toHaveLength(2)
      expect(sends[1]).toEqual(sends[0])
      expect(sends[1]).toMatchObject({ to: ['ada@example.com'], body: 'Hello Ada,\n\nOrder ORD-2026-7 is ready.', key: 'm2' })
    })
  }

  it('answers a sent claim with no plugin, even with the binding and the contact gone', async () => {
    const { hono, sends, shop } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    const headers = { ...COOKIE, 'idempotency-key': 'click-0041' }
    expect((await post(hono, 'send', { ...AGAIN, digest }, headers)).status).toBe(200)
    shop.binding = null
    shop.contacts = []
    const retry = await post(hono, 'send', { ...AGAIN, digest }, headers)
    expect(retry.status).toBe(200)
    expect(await retry.json()).toMatchObject({ sent: true, message: { id: 'm2', status: 'sent' } })
    expect(sends).toHaveLength(1)
  })

  it('sends a keyed retry to the recorded recipients after the contact was removed', async () => {
    const { hono, sends, shop } = app(signedIn())
    shop.binding = KEYED
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    const headers = { ...COOKIE, 'idempotency-key': 'click-0042' }
    shop.outcomeStatus = 503
    await post(hono, 'send', { ...AGAIN, digest }, headers)
    shop.outcomeStatus = 0
    shop.contacts = []
    const retry = await post(hono, 'send', { ...AGAIN, digest }, headers)
    expect(retry.status).toBe(200)
    expect(sends).toHaveLength(2)
    expect(sends[1]).toMatchObject({ to: ['ada@example.com'] })
  })

  it('refuses no_messaging_plugin for a retry that needs the plugin once the binding is gone', async () => {
    const { hono, sends, shop } = app(signedIn())
    shop.binding = KEYED
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    const headers = { ...COOKIE, 'idempotency-key': 'click-0043' }
    shop.outcomeStatus = 503
    await post(hono, 'send', { ...AGAIN, digest }, headers)
    shop.outcomeStatus = 0
    shop.binding = null
    const retry = await post(hono, 'send', { ...AGAIN, digest }, headers)
    expect(retry.status).toBe(409)
    expect(await retry.json()).toMatchObject({ type: 'no_messaging_plugin' })
    expect(sends).toHaveLength(1)
  })

  it('forwards the backend’s 404 as it is', async () => {
    const { hono, race, sends } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    race.refusal = { status: 404, body: { type: 'https://scadbuddy.dev/problems/orders/order_not_found', title: 'Not Found', detail: 'no order' } }
    const gone = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0005' })
    expect(gone.status).toBe(404)
    expect(await gone.json()).toMatchObject({ type: 'order_not_found' })
    expect(sends).toEqual([])
  })

  it('answers the backend’s 403 to the agent as 502 upstream_error, not as the person’s', async () => {
    const { hono, race, sends } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    race.refusal = { status: 403, body: { title: 'Forbidden', detail: 'the service credential lacks orders.write' } }
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const refused = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0006' })
      expect(refused.status).toBe(502)
      const body = (await refused.json()) as { type: string; detail: string }
      expect(body.type).toBe('upstream_error')
      expect(body.detail).not.toMatch(/service credential/)
      expect(logged).toHaveBeenCalledWith(expect.stringMatching(/HTTP 403/))
    } finally {
      logged.mockRestore()
    }
    expect(sends).toEqual([])
  })

  it('answers the backend’s 500 on the claim as 502 upstream_error', async () => {
    const { hono, race, sends } = app(signedIn())
    const { digest } = (await (await post(hono, 'preview', AGAIN, COOKIE)).json()) as { digest: string }
    race.refusal = { status: 500, body: { title: 'Internal Server Error', detail: 'the database went away' } }
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const failed = await post(hono, 'send', { ...AGAIN, digest }, { ...COOKIE, 'idempotency-key': 'click-0007' })
      expect(failed.status).toBe(502)
      const body = (await failed.json()) as { type: string; detail: string }
      expect(body.type).toBe('upstream_error')
      expect(body.detail).not.toMatch(/database/)
      expect(logged).toHaveBeenCalledWith(expect.stringMatching(/HTTP 500/))
    } finally {
      logged.mockRestore()
    }
    expect(sends).toEqual([])
  })

  it('takes no text from the browser and needs a key to send', async () => {
    const { hono } = app(signedIn())
    const withText = await post(hono, 'send', { ...AGAIN, digest: 'a'.repeat(64), text: 'mine' }, { ...COOKIE, 'idempotency-key': 'click-0003' })
    expect(withText.status).toBe(400)
    expect((await post(hono, 'send', { ...AGAIN, digest: 'a'.repeat(64) }, COOKIE)).status).toBe(400)
  })
})
