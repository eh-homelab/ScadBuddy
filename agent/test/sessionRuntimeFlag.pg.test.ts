import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { registerSessionModeRoutes, SESSION_MODE_PATH } from '../src/routes/sessionMode.js'
import { registerSessionRuntimeRoutes, SESSION_RUNTIME_PATH } from '../src/routes/sessionRuntime.js'
import { SESSION_RUNTIME_SETTING } from '../src/temporal/session/names.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The flag that starts new durable sessions on AgentSession (`ai_settings.session_runtime`,
// unified runtime spec 2026-10-13 §5.1) is set in the database, never through a route, a
// tool or the settings store, until fork works on that runtime (phase 6).

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const put = (app: Hono, path: string, body: unknown) =>
  app.request(`https://scadbuddy.example${path}`, { method: 'PUT', headers: UI, body: JSON.stringify(body) })

describe.skipIf(!TEST_DATABASE_URL)(`the session_runtime flag is not writable${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let app: Hono
  let store: SettingsStore

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new SettingsStore(db.sql, new AuditLog({ sql: db.sql }))
    app = new Hono()
    const common = { settings: store, ready: () => Promise.resolve(true), remoteAddress: () => '10.0.0.7', origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8') }
    registerSessionRuntimeRoutes(app, { ...common, writer: store })
    registerSessionModeRoutes(app, { ...common, durableUnready: () => Promise.resolve(undefined) })
  })
  afterEach(async () => {
    await drop()
  })

  const stored = () => db.sql`SELECT key FROM ai_settings WHERE key = ${SESSION_RUNTIME_SETTING}`

  it('is refused by the runtime settings route, which takes only its own four keys', async () => {
    expect((await put(app, SESSION_RUNTIME_PATH, { session_runtime: 'unified' })).status).toBe(422)
    expect((await put(app, SESSION_RUNTIME_PATH, { session_runtime: 'unified', warm_max_per_replica: 2 })).status).toBe(422)
    expect(await stored()).toHaveLength(0)
    expect(await store.get('warm_max_per_replica')).toBeUndefined()
  })

  it('is not a session mode: the mode route takes classic or durable only', async () => {
    expect((await put(app, SESSION_MODE_PATH, { mode: 'unified' })).status).toBe(400)
    expect((await put(app, SESSION_MODE_PATH, { mode: 'durable', session_runtime: 'unified' })).status).toBe(400)
    expect(await stored()).toHaveLength(0)
  })

  it('is refused by the settings store itself, which every route, tool and sweep writes through', async () => {
    await expect(store.set(SESSION_RUNTIME_SETTING, 'unified')).rejects.toThrow(/cannot be written yet/)
    await expect(store.setMany({ [SESSION_RUNTIME_SETTING]: 'unified', model: 'x' })).rejects.toThrow(/cannot be written yet/)
    expect(await stored()).toHaveLength(0)
    expect(await store.get('model')).toBeUndefined()
  })

  it('can still be read once set in the database', async () => {
    await db.sql`INSERT INTO ai_settings (key, value) VALUES (${SESSION_RUNTIME_SETTING}, ${db.sql.json('unified')})`
    expect(await store.get(SESSION_RUNTIME_SETTING)).toBe('unified')
  })
})
