import { createServer, type IncomingHttpHeaders, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { ChannelStore } from '../src/notify/channels.js'
import { NotificationDispatcher, type AttentionNotice } from '../src/notify/dispatcher.js'
import { PushStore } from '../src/notify/pushStore.js'
import { NOTIFY_CHANNELS_PATH, registerNotifyChannelRoutes } from '../src/routes/notifyChannels.js'
import { kekFromBase64 } from '../src/secrets.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #815 §3, out of the browser: ntfy and Home Assistant, configured in Settings (where the
// agent cannot write), told of each attention request once, through the egress check, with
// the reason and a link and never the message. Both run against fakes on loopback here; no
// real ntfy server or Home Assistant was used.

const KEK = kekFromBase64(Buffer.alloc(32, 7).toString('base64'))
const OTHER_KEK = kekFromBase64(Buffer.alloc(32, 9).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const SESSION = '0b0e2c51-2f0a-4b8e-9c1e-1d6c1e1f2a3b'

type Received = { method: string; path: string; headers: IncomingHttpHeaders; body: string }

describe.skipIf(!TEST_DATABASE_URL)(`notification channels${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let server: Server
  let base: string
  let received: Received[]
  let channels: ChannelStore

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    channels = new ChannelStore(db.sql, KEK)
    received = []
    server = createServer((req, res) => {
      const chunks: Buffer[] = []
      req.on('data', (c: Buffer) => chunks.push(c))
      req.on('end', () => {
        received.push({ method: req.method ?? '', path: req.url ?? '', headers: req.headers, body: Buffer.concat(chunks).toString() })
        res.statusCode = req.url?.includes('fail') ? 500 : 200
        res.end('{}')
      })
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
  })
  afterEach(async () => {
    await new Promise((resolve) => server.close(resolve))
    await drop()
  })

  const notice: AttentionNotice = { id: 'question:1', sessionId: SESSION, reason: 'tab_disconnected' }

  function dispatcher(notices: AttentionNotice[] = [notice]) {
    return new NotificationDispatcher({
      store: new PushStore(db.sql, KEK),
      channels,
      notices: () => Promise.resolve(notices),
      subject: 'https://scadbuddy.example',
      hosts: () => Promise.resolve([]),
      publicUrl: 'https://scadbuddy.example',
      log: () => {},
    })
  }

  it('posts to an ntfy topic once per request, with a title, a link and the token, and never the message', async () => {
    await channels.create({ kind: 'ntfy', name: 'Phone', url: `${base}/scadbuddy`, token: 'tk_secret_ntfy_token' })
    const d = dispatcher()
    await d.pass()
    await d.pass()
    expect(received).toHaveLength(1)
    const [sent] = received
    expect(sent).toMatchObject({ method: 'POST', path: '/scadbuddy' })
    expect(sent!.headers.title).toBe('The assistant lost its ScadBuddy tab')
    expect(sent!.headers.click).toBe(`https://scadbuddy.example/?assistant-session=${SESSION}`)
    expect(sent!.headers.authorization).toBe('Bearer tk_secret_ntfy_token')
    expect(sent!.body).toBe('Open ScadBuddy to answer it.')
  })

  it("calls a Home Assistant notify service with its long-lived token", async () => {
    await channels.create({ kind: 'home_assistant', name: 'HA', url: base, target: 'mobile_app_pixel', token: 'ha_long_lived_token' })
    await dispatcher().pass()
    expect(received).toHaveLength(1)
    const [sent] = received
    expect(sent).toMatchObject({ method: 'POST', path: '/api/services/notify/mobile_app_pixel' })
    expect(sent!.headers.authorization).toBe('Bearer ha_long_lived_token')
    expect(JSON.parse(sent!.body)).toEqual({
      title: 'The assistant lost its ScadBuddy tab',
      message: 'Open ScadBuddy to answer it.',
      data: { url: `https://scadbuddy.example/?assistant-session=${SESSION}`, clickAction: `https://scadbuddy.example/?assistant-session=${SESSION}` },
    })
  })

  it('keeps tokens sealed, shows only their last four, records a failure, and skips a disabled channel', async () => {
    const id = await channels.create({ kind: 'ntfy', name: 'Fails', url: `${base}/fail`, token: 'tk_secret_ntfy_token' })
    await channels.create({ kind: 'ntfy', name: 'Off', url: `${base}/off`, enabled: false })
    const [row] = await db.sql<{ secret_sealed: Buffer }[]>`SELECT secret_sealed FROM ai_notify_channels WHERE id = ${id}`
    expect(row!.secret_sealed.includes(Buffer.from('tk_secret_ntfy_token'))).toBe(false)
    await dispatcher().pass()
    expect(received.map((r) => r.path)).toEqual(['/fail'])
    const views = await channels.list()
    expect(views.find((v: { id: string }) => v.id === id)).toMatchObject({ has_token: true, token_last4: 'oken', last_error: 'HTTP 500' })
    expect(JSON.stringify(views)).not.toContain('tk_secret')
  })

  it('refuses plain http to a public address and any address the egress check refuses', async () => {
    const resolve = (host: string) => Promise.resolve(host === 'meta.example' ? ['169.254.169.254'] : ['203.0.113.5'])
    const strict = new ChannelStore(db.sql, KEK, resolve)
    await expect(strict.create({ kind: 'ntfy', name: 'x', url: 'http://ntfy.example/t' })).rejects.toThrow(/https/)
    await expect(strict.create({ kind: 'ntfy', name: 'x', url: 'https://meta.example/t' })).rejects.toThrow(/link-local|metadata/)
    await expect(strict.create({ kind: 'home_assistant', name: 'x', url: 'https://ha.example', target: 'phone' })).rejects.toThrow(/token/)
    await expect(strict.create({ kind: 'home_assistant', name: 'x', url: 'https://ha.example', target: '../x', token: 't' })).rejects.toThrow(/service/)
    expect(await strict.list()).toEqual([])
  })

  it('drops the token when the URL it was bound to changes, and re-wraps on rotation', async () => {
    const before = new ChannelStore(db.sql, OTHER_KEK)
    const id = await before.create({ kind: 'ntfy', name: 'Phone', url: `${base}/a`, token: 'tk_secret_ntfy_token' })
    expect(await before.rewrapFrom(OTHER_KEK, KEK)).toEqual({ rewrapped: 1, failed: 0 })
    expect((await channels.list())[0]).toMatchObject({ has_token: true })
    await channels.update(id, { url: `${base}/b` })
    expect((await channels.list())[0]).toMatchObject({ url: `${base}/b`, has_token: false })
  })

  describe('the routes', () => {
    function app() {
      const a = new Hono()
      registerNotifyChannelRoutes(a, {
        channels,
        notify: { publicUrl: 'https://scadbuddy.example' },
        ready: () => Promise.resolve(true),
        remoteAddress: () => '10.0.0.7',
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      })
      return a
    }
    const json = (method: string, path: string, body: unknown, headers: Record<string, string> = UI) =>
      app().request(path, { method, headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

    it('creates, lists, tests, edits and deletes a channel from the UI only', async () => {
      const created = await json('POST', NOTIFY_CHANNELS_PATH, { kind: 'ntfy', name: 'Phone', url: `${base}/t`, token: 'tk_secret_ntfy_token' })
      expect(created.status).toBe(201)
      const { id } = (await created.json()) as { id: string }
      const listed = (await (await app().request(NOTIFY_CHANNELS_PATH, { headers: UI_READ })).json()) as { channels: unknown[] }
      expect(listed.channels).toEqual([expect.objectContaining({ id, kind: 'ntfy', name: 'Phone', has_token: true, enabled: true })])

      const tested = await json('POST', `${NOTIFY_CHANNELS_PATH}/${id}/test`, {})
      expect(await tested.json()).toEqual({ ok: true })
      expect(received[0]!.headers.title).toBe('ScadBuddy test notification')

      expect((await json('PUT', `${NOTIFY_CHANNELS_PATH}/${id}`, { enabled: false })).status).toBe(200)
      expect((await channels.list())[0]).toMatchObject({ enabled: false })
      expect((await app().request(`${NOTIFY_CHANNELS_PATH}/${id}`, { method: 'DELETE', headers: UI })).status).toBe(204)
      expect(await channels.list()).toEqual([])

      const cross = await json('POST', NOTIFY_CHANNELS_PATH, { kind: 'ntfy', name: 'x', url: `${base}/t` }, { ...UI, origin: 'https://evil.example' })
      expect(cross.status).toBe(403)
      expect((await json('POST', NOTIFY_CHANNELS_PATH, { kind: 'email', name: 'x', url: `${base}/t` })).status).toBe(400)
    })
  })
})
