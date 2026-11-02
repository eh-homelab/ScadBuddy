import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { registerSessionRuntimeRoutes, SESSION_RUNTIME_PATH } from '../src/routes/sessionRuntime.js'
import {
  readRuntimeSettings,
  resizeOnNotify,
  runtimeResizer,
  RUNTIME_BOUNDS,
  RUNTIME_DEFAULTS,
  RUNTIME_PAYLOAD,
  SETTINGS_CHANNEL,
} from '../src/sessions/runtimeSettings.js'
import { WARM_EVICT_CHANNEL } from '../src/sessions/warmEvict.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// /api/v1/ai/settings/session-runtime (unified runtime spec 2026-10-13 §13,
// plan Task 2.5): the four runtime settings, their bounds, the audit rows, and
// the NOTIFY that makes every replica resize its warm cache (and not evict it).

const URL_ = `https://scadbuddy.example${SESSION_RUNTIME_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

const get = (app: Hono, headers: Record<string, string> = { host: UI.host, 'x-forwarded-proto': 'https' }) =>
  app.request(URL_, { headers })
const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

describe.skipIf(!TEST_DATABASE_URL)(`the session runtime settings${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let app: Hono
  let store: SettingsStore
  let audit: AuditLog
  let settingsNotes: string[]
  let evictNotes: string[]
  let unlisten: (() => Promise<void>)[]

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    audit = new AuditLog({ sql: db.sql })
    store = new SettingsStore(db.sql, audit)
    app = new Hono()
    registerSessionRuntimeRoutes(app, {
      settings: store,
      writer: store,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    settingsNotes = []
    evictNotes = []
    const a = await db.sql.listen(SETTINGS_CHANNEL, (p) => settingsNotes.push(p))
    const b = await db.sql.listen(WARM_EVICT_CHANNEL, (p) => evictNotes.push(p))
    unlisten = [() => a.unlisten(), () => b.unlisten()]
  })
  afterEach(async () => {
    for (const u of unlisten) await u()
    await drop()
  })

  it('answers the defaults until something is set', async () => {
    const res = await get(app)
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ park_hold_seconds: 300, warm_max_per_replica: 8, warm_idle_seconds: 600, turn_max_seconds: 7200 })
    expect(RUNTIME_DEFAULTS.warm_max_per_replica).toBe(8)
  })

  it('stores a partial body, writes one audit row, and a GET returns it', async () => {
    const res = await put(app, { warm_max_per_replica: 2 })
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ ...RUNTIME_DEFAULTS, warm_max_per_replica: 2 })
    expect(await store.get('warm_max_per_replica')).toBe(2)
    expect(await store.get('warm_idle_seconds')).toBeUndefined()
    expect(await (await get(app)).json()).toEqual({ ...RUNTIME_DEFAULTS, warm_max_per_replica: 2 })
    const rows = await db.sql<{ kind: string; action: string; actor: string; client_ip: string }[]>`SELECT * FROM ai_audit WHERE kind = 'settings'`
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ action: 'warm_max_per_replica', client_ip: '10.0.0.7' })
  })

  it('writes every key sent, one audit row each', async () => {
    const body = { park_hold_seconds: 60, warm_max_per_replica: 0, warm_idle_seconds: 120, turn_max_seconds: 600 }
    expect(await (await put(app, body)).json()).toEqual({ ...body })
    expect(await db.sql`SELECT 1 FROM ai_audit WHERE kind = 'settings'`).toHaveLength(4)
  })

  // Plan 2026-10-13 Step 9.9: the activity projection is the only one, so the flag is gone.
  it('refuses tools_as_activities, which no longer exists', async () => {
    expect((await put(app, { tools_as_activities: true })).status).toBe(422)
    expect(await store.get('tools_as_activities')).toBeUndefined()
  })

  it('takes each bound itself and refuses one past it with 422, storing nothing', async () => {
    for (const [key, { min, max }] of Object.entries(RUNTIME_BOUNDS)) {
      expect((await put(app, { [key]: min })).status, `${key} ${min}`).toBe(200)
      expect((await put(app, { [key]: max })).status, `${key} ${max}`).toBe(200)
    }
    await db.sql`DELETE FROM ai_settings`
    for (const body of [
      { park_hold_seconds: 5 },
      { park_hold_seconds: 3601 },
      { warm_max_per_replica: -1 },
      { warm_max_per_replica: 33 },
      { warm_idle_seconds: 59 },
      { warm_idle_seconds: 86401 },
      { turn_max_seconds: 299 },
      { turn_max_seconds: 86401 },
      { warm_max_per_replica: 2.5 },
      { warm_max_per_replica: '2' },
      { extra: 1 },
    ]) {
      const res = await put(app, body)
      expect(res.status, JSON.stringify(body)).toBe(422)
      expect(await res.json()).toEqual({ detail: expect.any(String) })
    }
    expect(await db.sql`SELECT 1 FROM ai_settings`).toHaveLength(0)
  })

  it('answers 400 to a body that is not JSON', async () => {
    const res = await app.request(URL_, { method: 'PUT', headers: { ...UI, 'content-type': 'application/json' }, body: '{' })
    expect(res.status).toBe(400)
  })

  it('refuses a write from another origin with 403', async () => {
    expect((await put(app, { warm_max_per_replica: 2 }, { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect((await get(app, { host: UI.host, 'x-forwarded-proto': 'https', 'sec-fetch-site': 'cross-site' })).status).toBe(403)
    expect(await store.get('warm_max_per_replica')).toBeUndefined()
  })

  it('reads a stored value out of range, written by hand, as its default', async () => {
    await db.sql`INSERT INTO ai_settings (key, value) VALUES ('park_hold_seconds', '5'), ('warm_max_per_replica', '"many"'), ('warm_idle_seconds', '120')`
    expect(await readRuntimeSettings(store)).toEqual({ ...RUNTIME_DEFAULTS, warm_idle_seconds: 120 })
  })

  it('tells every replica to re-read, and never to evict', async () => {
    await put(app, { warm_idle_seconds: 120 })
    await expect.poll(() => settingsNotes).toContain(RUNTIME_PAYLOAD)
    await new Promise((r) => setTimeout(r, 200))
    expect(evictNotes).toEqual([])
  })

  it('sends nothing for a write to another key, and nothing for a body with no key', async () => {
    await store.setMany({ audit_retention_days: 30 })
    await put(app, {})
    await new Promise((r) => setTimeout(r, 200))
    expect(settingsNotes).toEqual([])
  })
})

describe('the settings notification consumer', () => {
  it('re-reads and resizes on `session-runtime`, and ignores any other payload', async () => {
    const resize = vi.fn()
    const values: Record<string, unknown> = { warm_max_per_replica: 3, warm_idle_seconds: 90 }
    const notified = resizeOnNotify(runtimeResizer({ get: <T>(key: string) => Promise.resolve(values[key] as T) }, { resize }, () => {}))
    notified('other')
    notified('')
    notified('session-runtime; DROP TABLE ai_settings')
    notified(RUNTIME_PAYLOAD)
    await vi.waitFor(() => expect(resize).toHaveBeenCalledTimes(1))
    expect(resize).toHaveBeenCalledWith(3, 90_000)
  })

  it('applies re-reads in the order asked: an older read that resolves last cannot resize to stale values', async () => {
    const resize = vi.fn()
    let reads = 0
    // The first read is slow and sees 2; the second is quick and sees 5.
    const settings = {
      get: async <T>(key: string): Promise<T | undefined> => {
        if (key !== 'warm_max_per_replica') return undefined
        const n = ++reads
        if (n === 1) await new Promise((r) => setTimeout(r, 80))
        return (n === 1 ? 2 : 5) as T
      },
    }
    const again = runtimeResizer(settings, { resize }, () => {})
    await Promise.all([again(), again()])
    expect(resize.mock.calls.map((c) => c[0])).toEqual([2, 5])
  })

  it('logs a failed read and still serves the next one', async () => {
    const resize = vi.fn()
    const log = vi.fn()
    let fail = true
    const again = runtimeResizer(
      { get: <T>() => (fail ? Promise.reject(new Error('down')) : Promise.resolve(4 as T)) },
      { resize },
      log,
    )
    await again()
    expect(log).toHaveBeenCalledWith(expect.stringContaining('down'))
    fail = false
    await again()
    expect(resize).toHaveBeenCalledTimes(1)
  })
})

describe('the routes without a database, or before it is ready', () => {
  const built = (options: { settings: boolean; ready: boolean }) => {
    const repo = { get: () => Promise.resolve(undefined), set: () => Promise.resolve() }
    const app = new Hono()
    registerSessionRuntimeRoutes(app, {
      settings: options.settings ? repo : undefined,
      writer: options.settings ? { setMany: () => Promise.resolve(true) } : undefined,
      ready: () => Promise.resolve(options.ready),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    return app
  }

  it('answers 503 to GET and PUT', async () => {
    for (const app of [built({ settings: false, ready: true }), built({ settings: true, ready: false })]) {
      expect((await get(app)).status).toBe(503)
      expect((await put(app, { warm_max_per_replica: 2 })).status).toBe(503)
    }
  })
})
