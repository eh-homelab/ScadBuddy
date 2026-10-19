import type { Client } from '@temporalio/client'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { registerSessionModeRoutes, SESSION_MODE_PATH } from '../src/routes/sessionMode.js'
import { registerSessionRuntimeRoutes, SESSION_RUNTIME_PATH } from '../src/routes/sessionRuntime.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { UNIFIED_SESSIONS_SETTING } from '../src/temporal/session/names.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { manager, tempPaths } from './support/sessions.js'

// The flag that starts new durable sessions on AgentSession (`ai_settings.unified_sessions`,
// unified runtime spec 2026-10-13 §5.1) is set in the database, never through a route, a
// tool or the settings store, until fork works on that runtime (phase 6).

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const put = (app: Hono, path: string, body: unknown) =>
  app.request(`https://scadbuddy.example${path}`, { method: 'PUT', headers: UI, body: JSON.stringify(body) })

describe.skipIf(!TEST_DATABASE_URL)(`the unified_sessions flag is not writable${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
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

  const stored = () => db.sql`SELECT key FROM ai_settings WHERE key = ${UNIFIED_SESSIONS_SETTING}`

  it('is refused by the runtime settings route, which takes only its own four keys', async () => {
    expect((await put(app, SESSION_RUNTIME_PATH, { unified_sessions: true })).status).toBe(422)
    expect((await put(app, SESSION_RUNTIME_PATH, { unified_sessions: true, warm_max_per_replica: 2 })).status).toBe(422)
    expect(await stored()).toHaveLength(0)
    expect(await store.get('warm_max_per_replica')).toBeUndefined()
  })

  it('is not a session mode: the mode route takes classic or durable only', async () => {
    expect((await put(app, SESSION_MODE_PATH, { mode: 'unified' })).status).toBe(400)
    expect((await put(app, SESSION_MODE_PATH, { mode: 'durable', unified_sessions: true })).status).toBe(400)
    expect(await stored()).toHaveLength(0)
  })

  it('is refused by the settings store itself, which every route, tool and sweep writes through', async () => {
    await expect(store.set(UNIFIED_SESSIONS_SETTING, true)).rejects.toMatchObject({ name: 'CredentialError', status: 409 })
    await expect(store.set(UNIFIED_SESSIONS_SETTING, true)).rejects.toThrow(/cannot be written yet/)
    await expect(store.setMany({ [UNIFIED_SESSIONS_SETTING]: true, model: 'x' })).rejects.toThrow(/cannot be written yet/)
    expect(await stored()).toHaveLength(0)
    expect(await store.get('model')).toBeUndefined()
  })

  it('can still be read once set in the database', async () => {
    await db.sql`INSERT INTO ai_settings (key, value) VALUES (${UNIFIED_SESSIONS_SETTING}, ${db.sql.json(true)})`
    expect(await store.get(UNIFIED_SESSIONS_SETTING)).toBe(true)
  })
})

// A new session's readiness (SessionManager.durableUnready, which start() and the mode route
// ask) is checked for the runtime the flag would give it (spec 2026-10-13 §5.2): the queues an
// AgentSession needs with the flag set, agent-durable's `agent` without it.
describe.skipIf(!TEST_DATABASE_URL)(`a new session's readiness follows the flag${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  async function asked(flag: boolean | undefined): Promise<string[]> {
    if (flag) await db.sql`INSERT INTO ai_settings (key, value) VALUES (${UNIFIED_SESSIONS_SETTING}, ${db.sql.json(flag)})`
    const queues: string[] = []
    const client = {
      options: { namespace: 'default' },
      withDeadline: <R>(_deadline: number, fn: () => Promise<R>) => fn(),
      workflowService: {
        describeTaskQueue: ({ taskQueue, taskQueueType }: { taskQueue: { name: string }; taskQueueType: number }) => {
          queues.push(`${taskQueue.name}/${taskQueueType}`)
          return Promise.resolve({ pollers: [{ lastAccessTime: { seconds: Math.floor(Date.now() / 1000) } }] })
        },
      },
    } as unknown as Client
    const m = manager({ sql: db.sql, paths: await tempPaths() })
    m.durableTurns = new DurableTurns({ client, sql: db.sql, events: m.events, sessionHostQueue: 'agent-sessions.here' })
    expect(await m.durableUnready()).toBeUndefined()
    return queues
  }

  it('is agent-durable`s queue for a DurableSession, the flag unset', async () => {
    expect(await asked(undefined)).toEqual(['agent/1'])
  })

  it('is the three AgentSession queues with the flag set', async () => {
    expect(await asked(true)).toEqual(['agent-sessions/1', 'agent-tools/2', 'agent-sessions.here/2'])
  })
})
