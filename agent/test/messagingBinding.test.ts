import { Hono } from 'hono'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import type { AuditEntry } from '../src/audit/log.js'
import { BrowserSessions, personSessions, SESSION_COOKIE } from '../src/auth/browserSession.js'
import type { CustomerMessaging, MessagingConnection, SendOutcome } from '../src/orders/messaging.js'
import { registerMessagingBindingRoutes, TEST_TEXT } from '../src/routes/messagingBinding.js'
import { LOOPBACK, ORIGINS } from './helpers/mcp.js'

// #5660 §4.3: Settings → Orders' binding check (writes nothing) and test send (a person's
// signed-in session only; the agent's audit log keeps it).

const TOKEN = 'a'.repeat(43)
const BINDING = {
  source: 'remote:mail',
  tool: 'send_email',
  contact_kind: 'email',
  args: { to: 'to', subject: 'subject', text: 'body' },
}
const SCHEMA = {
  type: 'object',
  properties: { to: { type: 'array', items: { type: 'string' } }, subject: { type: 'string' }, body: { type: 'string' } },
  required: ['to', 'body'],
}

function signedIn(): BrowserSessions {
  const sql = (() => Promise.resolve([{ principal: 'user:https://idp.test#alice', name: 'Alice', kind: 'login' }])) as unknown as Sql
  return new BrowserSessions(sql)
}

function fake(outcome: SendOutcome = { status: 'sent' }) {
  const sends: Record<string, unknown>[] = []
  const messaging: CustomerMessaging = {
    async open() {
      const connection: MessagingConnection = {
        tools: async () => [{ name: 'send_email', inputSchema: SCHEMA }],
        send: async (args) => (sends.push(args), outcome),
        close: async () => {},
      }
      return connection
    },
  }
  return { messaging, sends }
}

function app(messaging: CustomerMessaging, sessions?: BrowserSessions) {
  const audits: AuditEntry[] = []
  const hono = new Hono()
  hono.use('/api/v1/ai/*', personSessions(sessions, 'off'))
  registerMessagingBindingRoutes(hono, {
    messaging,
    audit: { record: async (entry: AuditEntry) => void audits.push(entry) },
    origins: ORIGINS,
    remoteAddress: () => LOOPBACK,
  } as never)
  return { hono, audits }
}

const post = (hono: Hono, path: string, body: unknown, cookie = false) =>
  hono.request(`http://127.0.0.1/api/v1/ai/orders/messaging-binding/${path}`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      origin: 'http://127.0.0.1',
      host: '127.0.0.1',
      ...(cookie ? { cookie: `${SESSION_COOKIE}=${TOKEN}` } : {}),
    },
    body: JSON.stringify(body),
  })

describe('the messaging binding routes (#5660)', () => {
  it('checks a binding against the tool and sends nothing', async () => {
    const { messaging, sends } = fake()
    const { hono } = app(messaging)
    const good = await post(hono, 'check', { binding: BINDING })
    expect(good.status).toBe(200)
    expect(await good.json()).toEqual({ ok: true })
    const bad = await post(hono, 'check', { binding: { ...BINDING, args: { ...BINDING.args, text: 'message' } } })
    expect(bad.status).toBe(422)
    expect(((await bad.json()) as { detail: string }).detail).toContain('takes no argument message')
    expect(sends).toEqual([])
  })

  it('refuses a test send without a signed-in person', async () => {
    const { messaging, sends } = fake()
    const { hono, audits } = app(messaging)
    const refused = await post(hono, 'test', { binding: BINDING, to: 'ada@example.com' })
    expect(refused.status).toBe(401)
    expect(sends).toEqual([])
    expect(audits).toEqual([])
  })

  it("sends one test message for a person and keeps it in the agent's audit log", async () => {
    const { messaging, sends } = fake()
    const { hono, audits } = app(messaging, signedIn())
    const sent = await post(hono, 'test', { binding: BINDING, to: 'ada@example.com' }, true)
    expect(sent.status).toBe(200)
    expect(await sent.json()).toEqual({ ok: true, status: 'sent' })
    expect(sends).toEqual([{ to: ['ada@example.com'], subject: 'ScadBuddy test message', body: TEST_TEXT }])
    expect(audits).toEqual([
      expect.objectContaining({
        kind: 'plugin',
        action: 'messaging.test_send',
        outcome: 'ok',
        inputSummary: 'test message to ada@example.com through remote:mail send_email',
      }),
    ])
  })

  it('refuses a request that is not the UI’s', async () => {
    const { messaging } = fake()
    const { hono } = app(messaging)
    const foreign = await hono.request('http://127.0.0.1/api/v1/ai/orders/messaging-binding/check', {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin: 'https://evil.example', host: '127.0.0.1' },
      body: JSON.stringify({ binding: BINDING }),
    })
    expect(foreign.status).toBe(403)
  })
})
