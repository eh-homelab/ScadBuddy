import { createECDH } from 'node:crypto'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { PushDispatcher } from '../src/notify/dispatcher.js'
import { PushStore } from '../src/notify/pushStore.js'
import { PUSH_KEY_PATH, PUSH_SUBSCRIPTION_PATH, registerPushRoutes } from '../src/routes/push.js'
import { kekFromBase64 } from '../src/secrets.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// /api/v1/ai/push/* (#815 §3): the VAPID key a browser subscribes with, and its subscription.

const KEK = kekFromBase64(Buffer.alloc(32, 7).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

function subscription(endpoint: string) {
  const ecdh = createECDH('prime256v1')
  ecdh.generateKeys()
  return { endpoint, expirationTime: null, keys: { p256dh: ecdh.getPublicKey().toString('base64url'), auth: Buffer.alloc(16, 1).toString('base64url') } }
}

describe.skipIf(!TEST_DATABASE_URL)(`the push routes${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: PushStore
  let wakes: number

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new PushStore(db.sql, KEK)
    wakes = 0
  })
  afterEach(async () => {
    await drop()
  })

  function app(withPush = true) {
    const a = new Hono()
    const dispatcher = new PushDispatcher({ store, notices: () => Promise.resolve([]), subject: 'https://scadbuddy.example' })
    dispatcher.wake = () => void (wakes += 1)
    registerPushRoutes(a, {
      push: withPush ? { store, dispatcher } : undefined,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      resolve: (host) => Promise.resolve(host === 'metadata.example' ? ['169.254.169.254'] : ['203.0.113.5']),
    })
    return a
  }
  const put = (a: Hono, body: unknown, headers: Record<string, string> = UI) =>
    a.request(PUSH_SUBSCRIPTION_PATH, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

  it('serves one VAPID public key, the same each time', async () => {
    const a = app()
    const first = await (await a.request(PUSH_KEY_PATH, { headers: UI_READ })).json()
    const second = await (await a.request(PUSH_KEY_PATH, { headers: UI_READ })).json()
    expect(first).toEqual(second)
    expect(Buffer.from((first as { public_key: string }).public_key, 'base64url')).toHaveLength(65)
  })

  it('stores a subscription for the browser user and wakes the dispatcher, then forgets it', async () => {
    const a = app()
    expect((await put(a, subscription('https://push.example.net/send/1'))).status).toBe(204)
    expect(wakes).toBe(1)
    const [stored] = await store.subscriptions()
    expect(stored).toMatchObject({ endpoint: 'https://push.example.net/send/1', owner: { kind: 'browser' } })
    const del = await a.request(`${PUSH_SUBSCRIPTION_PATH}?endpoint=${encodeURIComponent('https://push.example.net/send/1')}`, { method: 'DELETE', headers: UI })
    expect(del.status).toBe(204)
    expect(await store.subscriptions()).toEqual([])
  })

  it('refuses an endpoint the egress check refuses, plain http, and keys that are not keys', async () => {
    const a = app()
    expect((await put(a, subscription('https://metadata.example/push'))).status).toBe(400)
    expect((await put(a, subscription('http://push.example.net/send/1'))).status).toBe(400)
    expect((await put(a, { ...subscription('https://push.example.net/1'), keys: { p256dh: 'AAAA', auth: 'AAAA' } })).status).toBe(400)
    expect(await store.subscriptions()).toEqual([])
  })

  it('takes writes only from the UI, and is 503 without the KEK', async () => {
    const a = app()
    expect((await put(a, subscription('https://push.example.net/1'), { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect((await app(false).request(PUSH_KEY_PATH, { headers: UI_READ })).status).toBe(503)
  })
})
