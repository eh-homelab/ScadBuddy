import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { originPolicy } from '../src/http/origins.js'
import type { SettingsRepo } from '../src/routes/headlessBrowser.js'
import { registerSessionModeRoutes, SESSION_MODE_PATH } from '../src/routes/sessionMode.js'
import { SETTING_SESSION_MODE } from '../src/sessions/manager.js'

// /api/v1/ai/settings/session-mode (#1056): the mode a new session gets when its
// start does not choose one, `classic` until Settings says otherwise, behind the
// same UI guard as every other AI settings write (routes/guard.ts).

const URL_ = `https://scadbuddy.example${SESSION_MODE_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: UI.host, 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

function setup(options: { ready?: boolean; settings?: boolean; stored?: unknown } = {}) {
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
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, values, contexts }
}

const get = (app: Hono, headers: Record<string, string> = UI_READ) => app.request(URL_, { headers })
const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

describe('the session_mode setting', () => {
  it('is classic until set, and reads back what was stored, audited as the UI user', async () => {
    const { app, values, contexts } = setup()
    expect(await (await get(app)).json()).toEqual({ mode: 'classic' })
    const res = await put(app, { mode: 'durable' })
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ mode: 'durable' })
    expect(values.get(SETTING_SESSION_MODE)).toBe('durable')
    expect(contexts[0]).toMatchObject({ actor: { kind: 'browser' }, surface: 'http', clientIp: '10.0.0.7' })
    expect(await (await get(app)).json()).toEqual({ mode: 'durable' })
  })

  it('reads an invalid stored value as classic', async () => {
    expect(await (await get(setup({ stored: 'turbo' }).app)).json()).toEqual({ mode: 'classic' })
  })

  it.each([[{ mode: 'turbo' }], [{}], [{ mode: 'durable', extra: 1 }], [{ mode: 1 }]])(
    'refuses the body %j with 400 and stores nothing',
    async (body) => {
      const { app, values } = setup()
      expect((await put(app, body)).status).toBe(400)
      expect(values.size).toBe(0)
    },
  )

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, values } = setup()
    expect((await put(app, { mode: 'durable' }, headers as Record<string, string>)).status).toBe(403)
    expect(values.has(SETTING_SESSION_MODE)).toBe(false)
  })

  it('refuses a read from another site', async () => {
    const { app } = setup()
    expect((await get(app, { ...UI_READ, 'sec-fetch-site': 'cross-site' })).status).toBe(403)
    expect((await get(app, { ...UI_READ, 'x-forwarded-proto': 'http' })).status).toBe(403)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(setup({ settings: false }).app)).status).toBe(503)
    expect((await put(setup({ settings: false }).app, { mode: 'durable' })).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { mode: 'durable' })).status).toBe(503)
  })
})
