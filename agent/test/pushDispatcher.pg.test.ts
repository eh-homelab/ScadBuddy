import { createDecipheriv, createECDH, hkdfSync } from 'node:crypto'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { NotificationDispatcher, type AttentionNotice, type NotificationDispatcherDeps } from '../src/notify/dispatcher.js'
import { DEFAULT_PUSH_HOSTS } from '../src/notify/pushEgress.js'
import { PushStore, SubscriptionTakenError } from '../src/notify/pushStore.js'
import { kekFromBase64 } from '../src/secrets.js'
import type { Owner } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #815 §3: a closed tab still alerts. Each attention request is pushed once to every browser
// subscribed for someone who may see it, only to an allowlisted push service at a public
// address, with nothing secret in it.

const KEK = kekFromBase64(Buffer.alloc(32, 7).toString('base64'))
const OTHER_KEK = kekFromBase64(Buffer.alloc(32, 9).toString('base64'))
const YOU: Owner = { kind: 'browser', id: 'browser', label: 'You' }

/** A browser's side of a subscription: its keys, and how it opens what is pushed to it. */
function browserKeys() {
  const ecdh = createECDH('prime256v1')
  ecdh.generateKeys()
  const auth = Buffer.alloc(16, 3)
  return {
    keys: { p256dh: ecdh.getPublicKey(), auth },
    open(body: Buffer): unknown {
      const salt = body.subarray(0, 16)
      const idlen = body.readUInt8(20)
      const asPublic = body.subarray(21, 21 + idlen)
      const record = body.subarray(21 + idlen)
      const shared = ecdh.computeSecret(asPublic)
      const ikm = Buffer.from(hkdfSync('sha256', shared, auth, Buffer.concat([Buffer.from('WebPush: info\0'), ecdh.getPublicKey(), asPublic]), 32))
      const cek = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: aes128gcm\0'), 16))
      const nonce = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: nonce\0'), 12))
      const decipher = createDecipheriv('aes-128-gcm', cek, nonce)
      decipher.setAuthTag(record.subarray(record.length - 16))
      const plain = Buffer.concat([decipher.update(record.subarray(0, record.length - 16)), decipher.final()])
      expect(plain.at(-1)).toBe(2)
      return JSON.parse(plain.subarray(0, -1).toString())
    },
  }
}

type Received = { path: string; headers: Record<string, string | string[] | undefined>; body: Buffer; hung?: true }

describe.skipIf(!TEST_DATABASE_URL)(`web push${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let server: Server
  let base: string
  let received: Received[]
  let status: number
  let hang: boolean
  let inFlight: number
  let maxInFlight: number

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    received = []
    status = 201
    hang = false
    inFlight = 0
    maxInFlight = 0
    server = createServer((req, res) => {
      const chunks: Buffer[] = []
      req.on('data', (c: Buffer) => chunks.push(c))
      req.on('end', () => {
        const entry: Received = { path: req.url ?? '', headers: req.headers, body: Buffer.concat(chunks) }
        received.push(entry)
        if (hang) {
          entry.hung = true
          inFlight += 1
          maxInFlight = Math.max(maxInFlight, inFlight)
          res.on('close', () => void (inFlight -= 1))
          return
        }
        res.statusCode = status
        res.end()
      })
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
  })
  afterEach(async () => {
    server.closeAllConnections()
    await new Promise((resolve) => server.close(resolve))
    await drop()
  })

  const notice = (id: string): AttentionNotice => ({ id, sessionId: '0b0e2c51-2f0a-4b8e-9c1e-1d6c1e1f2a3b', reason: 'tab_disconnected' })

  /** Sends to the fake push service on loopback, which only a test may pin to (`pinEndpoint`). */
  function dispatcher(store: PushStore, notices: () => AttentionNotice[], extra: Partial<NotificationDispatcherDeps> = {}) {
    return new NotificationDispatcher({
      store,
      notices: () => Promise.resolve(notices()),
      subject: 'https://scadbuddy.example',
      hosts: () => Promise.resolve(DEFAULT_PUSH_HOSTS),
      pinEndpoint: (endpoint) => Promise.resolve({ url: endpoint, address: '127.0.0.1' }),
      log: () => {},
      ...extra,
    })
  }

  /** The real check: no pin, each name resolved by `resolve`. */
  function strict(store: PushStore, resolve: (host: string) => Promise<string[]>, log: (m: string) => void = () => {}) {
    return new NotificationDispatcher({
      store,
      notices: () => Promise.resolve([notice('question:1')]),
      subject: 'https://scadbuddy.example',
      hosts: () => Promise.resolve(DEFAULT_PUSH_HOSTS),
      resolve,
      log,
    })
  }

  it('makes one VAPID key, keeps it sealed under the KEK, and gives the same one back', async () => {
    const store = new PushStore(db.sql, KEK)
    const first = await store.vapidKeys()
    expect((await store.vapidKeys()).publicKey.equals(first.publicKey)).toBe(true)
    const [row] = await db.sql<{ secret_sealed: Buffer }[]>`SELECT secret_sealed FROM ai_push_vapid`
    expect(row!.secret_sealed.includes(first.privateKey)).toBe(false)
    expect(row!.secret_sealed.includes(Buffer.from(first.privateKey.toString('base64url')))).toBe(false)
  })

  it('pushes each attention request once per subscription, encrypted, with only its ids and reason', async () => {
    const store = new PushStore(db.sql, KEK)
    const browser = browserKeys()
    await store.subscribe(YOU, `${base}/push/one`, browser.keys)
    let listed = [notice('question:1')]
    const d = dispatcher(store, () => listed)
    await d.pass()
    await d.pass()
    expect(received).toHaveLength(1)
    const [sent] = received
    expect(sent!.path).toBe('/push/one')
    expect(sent!.headers['content-encoding']).toBe('aes128gcm')
    expect(sent!.headers.ttl).toBeDefined()
    expect(String(sent!.headers.authorization)).toMatch(/^vapid t=.+, k=.+$/)
    expect(browser.open(sent!.body)).toEqual({ request_id: 'question:1', session_id: notice('x').sessionId, reason: 'tab_disconnected' })

    listed = [notice('question:1'), notice('question:2')]
    await d.pass()
    expect(received).toHaveLength(2)
    expect(browser.open(received[1]!.body)).toMatchObject({ request_id: 'question:2' })
  })

  it('sends one request once though two replicas pass at the same time', async () => {
    const store = new PushStore(db.sql, KEK)
    await store.subscribe(YOU, `${base}/push/one`, browserKeys().keys)
    const a = dispatcher(store, () => [notice('question:1')])
    const b = dispatcher(new PushStore(db.sql, KEK), () => [notice('question:1')])
    await Promise.all([a.pass(), b.pass()])
    expect(received).toHaveLength(1)
  })

  it('forgets a subscription the push service says is gone (404, 410)', async () => {
    const store = new PushStore(db.sql, KEK)
    await store.subscribe(YOU, `${base}/push/gone`, browserKeys().keys)
    status = 410
    await dispatcher(store, () => [notice('question:1')]).pass()
    expect(received).toHaveLength(1)
    expect(await store.subscriptions()).toEqual([])
  })

  it('never sends to a private address, a name resolving to one, or a host off the allowlist: checked at every push', async () => {
    const store = new PushStore(db.sql, KEK)
    // Stored directly: the route refuses each of them already (push.routes.pg.test.ts).
    await store.subscribe(YOU, 'https://10.0.0.5/push', browserKeys().keys)
    await store.subscribe(YOU, 'https://fcm.googleapis.com/fcm/send/x', browserKeys().keys)
    await store.subscribe(YOU, 'https://push.example.net/send/x', browserKeys().keys)
    const logged: string[] = []
    const resolve = (host: string) => Promise.resolve(host === 'fcm.googleapis.com' ? ['192.168.1.20'] : ['203.0.113.5'])
    await strict(store, resolve, (m) => logged.push(m)).pass()
    expect(received).toEqual([])
    const log = logged.join('\n')
    expect(log).toMatch(/10\.0\.0\.5 is not a known push service/)
    expect(log).toMatch(/fcm\.googleapis\.com resolves to 192\.168\.1\.20/)
    expect(log).toMatch(/push\.example\.net is not a known push service/)
  })

  it('refuses the loopback fake without the test-only pin: no plain http, no loopback', async () => {
    const store = new PushStore(db.sql, KEK)
    await store.subscribe(YOU, `${base}/push/one`, browserKeys().keys)
    await strict(store, (host) => Promise.resolve([host])).pass()
    expect(received).toEqual([])
  })

  it('has at most four pushes in flight, and starts none past the pass deadline', async () => {
    const store = new PushStore(db.sql, KEK)
    for (let i = 0; i < 6; i++) await store.subscribe({ ...YOU, id: `p${i}` }, `${base}/hang/${i}`, browserKeys().keys)
    hang = true
    const started = Date.now()
    await dispatcher(store, () => [notice('question:1')], { passDeadlineMs: 3_000 }).pass()
    expect(Date.now() - started).toBeLessThan(10_000)
    expect(maxInFlight).toBe(4)
    // The two never started are unclaimed, so the next pass sends them.
    hang = false
    await dispatcher(store, () => [notice('question:1')]).pass()
    expect(received.filter((r) => !r.hung)).toHaveLength(2)
  })

  it("never replaces another person's subscription, and keeps five per person", async () => {
    const store = new PushStore(db.sql, KEK)
    const mallory: Owner = { kind: 'oidc', id: 'mallory', label: 'Mallory' }
    await store.subscribe(YOU, 'https://fcm.googleapis.com/fcm/send/a', browserKeys().keys)
    await expect(store.subscribe(mallory, 'https://fcm.googleapis.com/fcm/send/a', browserKeys().keys)).rejects.toThrow(SubscriptionTakenError)
    expect(await store.unsubscribe('https://fcm.googleapis.com/fcm/send/a', mallory)).toBe(false)
    expect((await store.subscriptions()).map((s) => s.owner.id)).toEqual(['browser'])
    for (let i = 0; i < 6; i++) await store.subscribe(YOU, `https://fcm.googleapis.com/fcm/send/${i}`, browserKeys().keys)
    const kept = (await store.subscriptions()).map((s) => s.endpoint)
    expect(kept).toHaveLength(5)
    expect(kept).not.toContain('https://fcm.googleapis.com/fcm/send/a')
    expect(kept).toContain('https://fcm.googleapis.com/fcm/send/5')
  })

  it("re-wraps the VAPID key and the subscriptions' keys on rotation", async () => {
    const before = new PushStore(db.sql, OTHER_KEK)
    const vapid = await before.vapidKeys()
    await before.subscribe(YOU, `${base}/push/one`, browserKeys().keys)
    expect(await before.rewrapFrom(OTHER_KEK, KEK)).toEqual({ rewrapped: 2, failed: 0 })
    const after = new PushStore(db.sql, KEK)
    expect((await after.vapidKeys()).privateKey.equals(vapid.privateKey)).toBe(true)
    expect(await after.subscriptions()).toHaveLength(1)
  })
})
