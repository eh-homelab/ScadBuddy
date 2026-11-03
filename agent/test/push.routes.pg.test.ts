import { createECDH } from 'node:crypto'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { SettingsRepo } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { PushDispatcher } from '../src/notify/dispatcher.js'
import { DEFAULT_PUSH_HOSTS, SETTING_PUSH_HOSTS } from '../src/notify/pushEgress.js'
import { PushStore } from '../src/notify/pushStore.js'
import { PUSH_KEY_PATH, PUSH_SERVICES_PATH, PUSH_SUBSCRIPTION_PATH, registerPushRoutes } from '../src/routes/push.js'
import { kekFromBase64 } from '../src/secrets.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// /api/v1/ai/push/* and the push-service allowlist (#815 §3).

const KEK = kekFromBase64(Buffer.alloc(32, 7).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const FCM = 'https://fcm.googleapis.com/fcm/send/1'

function subscription(endpoint: string) {
  const ecdh = createECDH('prime256v1')
  ecdh.generateKeys()
  return { endpoint, expirationTime: null, keys: { p256dh: ecdh.getPublicKey().toString('base64url'), auth: Buffer.alloc(16, 1).toString('base64url') } }
}

const RESOLVE: Record<string, string[]> = {
  'fcm.googleapis.com': ['142.250.1.95'],
  'private.push.apple.com': ['10.0.0.9'],
  'push.example.net': ['203.0.113.5'],
}

describe.skipIf(!TEST_DATABASE_URL)(`the push routes${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: PushStore
  let wakes: number
  let values: Map<string, unknown>
  let settings: SettingsRepo

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new PushStore(db.sql, KEK)
    wakes = 0
    values = new Map()
    settings = {
      get: <T>(key: string) => Promise.resolve(values.get(key) as T),
      set: (key, value) => {
        values.set(key, value)
        return Promise.resolve()
      },
    }
  })
  afterEach(async () => {
    await drop()
  })

  function app(withPush = true) {
    const a = new Hono()
    const dispatcher = new PushDispatcher({
      store,
      notices: () => Promise.resolve([]),
      subject: 'https://scadbuddy.example',
      hosts: () => Promise.resolve(DEFAULT_PUSH_HOSTS),
    })
    dispatcher.wake = () => void (wakes += 1)
    registerPushRoutes(a, {
      push: withPush ? { store, dispatcher } : undefined,
      settings,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      resolve: (host) => Promise.resolve(RESOLVE[host] ?? ['203.0.113.5']),
    })
    return a
  }
  const put = (a: Hono, path: string, body: unknown, headers: Record<string, string> = UI) =>
    a.request(path, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

  it('serves one VAPID public key, the same each time', async () => {
    const a = app()
    const first = await (await a.request(PUSH_KEY_PATH, { headers: UI_READ })).json()
    const second = await (await a.request(PUSH_KEY_PATH, { headers: UI_READ })).json()
    expect(first).toEqual(second)
    expect(Buffer.from((first as { public_key: string }).public_key, 'base64url')).toHaveLength(65)
  })

  it('stores a subscription for the browser user and wakes the dispatcher, then forgets it', async () => {
    const a = app()
    expect((await put(a, PUSH_SUBSCRIPTION_PATH, subscription(FCM))).status).toBe(204)
    expect(wakes).toBe(1)
    const [stored] = await store.subscriptions()
    expect(stored).toMatchObject({ endpoint: FCM, owner: { kind: 'browser' } })
    const del = await a.request(`${PUSH_SUBSCRIPTION_PATH}?endpoint=${encodeURIComponent(FCM)}`, { method: 'DELETE', headers: UI })
    expect(del.status).toBe(204)
    expect(await store.subscriptions()).toEqual([])
  })

  it('refuses a private-IP endpoint, a name resolving to a private address, a host off the allowlist, plain http, and bad keys', async () => {
    const a = app()
    for (const endpoint of [
      'https://10.0.0.5/push',
      'https://private.push.apple.com/push',
      'https://push.example.net/send/1',
      'http://fcm.googleapis.com/fcm/send/1',
      'https://metadata.google.internal/push',
    ]) {
      expect((await put(a, PUSH_SUBSCRIPTION_PATH, subscription(endpoint))).status, endpoint).toBe(400)
    }
    expect((await put(a, PUSH_SUBSCRIPTION_PATH, { ...subscription(FCM), keys: { p256dh: 'AAAA', auth: 'AAAA' } })).status).toBe(400)
    expect(await store.subscriptions()).toEqual([])
  })

  it("refuses an endpoint another person holds (409), and leaves theirs", async () => {
    await store.subscribe({ kind: 'oidc', id: 'alice', label: 'Alice' }, FCM, { p256dh: createECDH('prime256v1').generateKeys(), auth: Buffer.alloc(16) })
    expect((await put(app(), PUSH_SUBSCRIPTION_PATH, subscription(FCM))).status).toBe(409)
    expect((await store.subscriptions()).map((s) => s.owner.id)).toEqual(['alice'])
  })

  it('serves and saves the push-service allowlist, which then decides what may be stored', async () => {
    const a = app()
    expect(await (await a.request(PUSH_SERVICES_PATH, { headers: UI_READ })).json()).toEqual({ hosts: DEFAULT_PUSH_HOSTS, default: DEFAULT_PUSH_HOSTS })
    expect((await put(a, PUSH_SERVICES_PATH, { hosts: ['bad host'] })).status).toBe(400)
    const saved = await put(a, PUSH_SERVICES_PATH, { hosts: [' Push.Example.NET ', 'push.example.net'] })
    expect(await saved.json()).toEqual({ hosts: ['push.example.net'], default: DEFAULT_PUSH_HOSTS })
    expect(values.get(SETTING_PUSH_HOSTS)).toEqual(['push.example.net'])
    expect((await put(a, PUSH_SUBSCRIPTION_PATH, subscription('https://push.example.net/send/1'))).status).toBe(204)
    expect((await put(a, PUSH_SUBSCRIPTION_PATH, subscription(FCM))).status).toBe(400)
  })

  it('takes writes only from the UI, and is 503 without the KEK', async () => {
    const a = app()
    expect((await put(a, PUSH_SUBSCRIPTION_PATH, subscription(FCM), { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect((await put(a, PUSH_SERVICES_PATH, { hosts: [] }, { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect((await app(false).request(PUSH_KEY_PATH, { headers: UI_READ })).status).toBe(503)
  })
})
