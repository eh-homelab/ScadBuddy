import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { originPolicy } from '../src/http/origins.js'
import type { SettingsRepo } from '../src/routes/headlessBrowser.js'
import { registerSessionModeRoutes, SESSION_MODE_PATH } from '../src/routes/sessionMode.js'
import { SETTING_SESSION_MODE } from '../src/sessions/manager.js'

// /api/v1/ai/settings/session-mode (plan 5d): the mode a new assistant session gets
// when its creator names none, behind the same UI guards as every other AI settings
// route (routes/guard.ts).

const URL_ = `https://scadbuddy.example${SESSION_MODE_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function setup(options: { ready?: boolean; settings?: boolean; stored?: unknown; unready?: string | null } = {}) {
  const values = new Map<string, unknown>()
  if (options.stored !== undefined) values.set(SETTING_SESSION_MODE, options.stored)
  const contexts: unknown[] = []
  const repo: SettingsRepo = {
    get: <T>(key: string) => Promise.resolve(values.get(key) as T),
    set: (key, value, context) => {
      contexts.push(context)
      values.set(key, value)
      return Promise.resolve()
    },
  }
  const app = new Hono()
  registerSessionModeRoutes(app, {
    settings: options.settings === false ? undefined : repo,
    ready: () => Promise.resolve(options.ready ?? true),
    // null: durable can run; a string: why not.
    durableUnready: () => Promise.resolve(options.unready === null ? undefined : (options.unready ?? 'no Temporal')),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, values, contexts }
}

const get = (app: Hono, headers: Record<string, string> = { host: UI.host, 'x-forwarded-proto': 'https' }) =>
  app.request(URL_, { headers })
const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

describe('the default session mode setting', () => {
  it('is durable until set, and reads back what was stored', async () => {
    const { app, values, contexts } = setup()
    const before = await get(app)
    expect(before.status).toBe(200)
    expect(await before.json()).toEqual({ mode: 'durable', durable_available: false, durable_unavailable_reason: 'no Temporal' })
    const saved = await put(app, { mode: 'classic' })
    expect(saved.status).toBe(200)
    expect(await saved.json()).toMatchObject({ mode: 'classic' })
    // Audited as the UI user, from the request's address.
    expect(contexts[0]).toMatchObject({ surface: 'http', clientIp: '10.0.0.7' })
    expect(values.get(SETTING_SESSION_MODE)).toBe('classic')
    expect(await (await get(app)).json()).toMatchObject({ mode: 'classic' })
  })

  it('says when durable sessions can run', async () => {
    const { app } = setup({ unready: null })
    expect(await (await get(app)).json()).toEqual({ mode: 'durable', durable_available: true })
  })

  it('stores durable while it cannot run, so an operator may set it before Temporal is up', async () => {
    const { app, values } = setup({ stored: 'classic' })
    expect((await put(app, { mode: 'durable' })).status).toBe(200)
    expect(values.get(SETTING_SESSION_MODE)).toBe('durable')
  })

  it.each([[{}], [{ mode: 'fast' }], [{ mode: 1 }], [{ mode: 'classic', extra: 1 }]])(
    'refuses the body %j, and stores nothing',
    async (body) => {
      const { app, values } = setup()
      expect((await put(app, body)).status).toBe(400)
      expect(values.size).toBe(0)
    },
  )

  it.each([['nonsense'], [null], [1]])('reads a stored %j it could not have written as durable', async (stored) => {
    const { app } = setup({ stored })
    expect(await (await get(app)).json()).toMatchObject({ mode: 'durable' })
  })

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, values } = setup()
    expect((await put(app, { mode: 'classic' }, headers as Record<string, string>)).status).toBe(403)
    expect(values.has(SETTING_SESSION_MODE)).toBe(false)
  })

  it('refuses a read over plain HTTP', async () => {
    const { app } = setup()
    expect((await get(app, { host: UI.host, 'x-forwarded-proto': 'http' })).status).toBe(403)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(setup({ settings: false }).app)).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { mode: 'classic' })).status).toBe(503)
  })
})
