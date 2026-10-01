import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { SETTING_HTTP_REQUEST } from '../src/harness/httpRequest.js'
import { originPolicy } from '../src/http/origins.js'
import type { SettingsRepo } from '../src/routes/headlessBrowser.js'
import { HTTP_REQUEST_SETTING_PATH, registerHttpRequestRoutes } from '../src/routes/httpRequest.js'

// /api/v1/ai/settings/http-request (#827): the switch for the assistant's
// http_request tool, ON by default, behind the same UI guard as every other AI
// settings write (routes/guard.ts).

const URL_ = `https://scadbuddy.example${HTTP_REQUEST_SETTING_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function setup(options: { ready?: boolean; settings?: boolean } = {}) {
  const values = new Map<string, unknown>()
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
  registerHttpRequestRoutes(app, {
    settings: options.settings === false ? undefined : repo,
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, values, contexts }
}

const get = (app: Hono) => app.request(URL_, { headers: { host: UI.host, 'x-forwarded-proto': 'https' } })
const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

describe('the http_request setting', () => {
  it('is on until turned off, and reads back what was stored', async () => {
    const { app, values, contexts } = setup()
    expect(await (await get(app)).json()).toEqual({ enabled: true })
    const off = await put(app, { enabled: false })
    expect(off.status).toBe(200)
    // Audited as the UI user, from the request's address.
    expect(contexts[0]).toMatchObject({ surface: 'http', clientIp: '10.0.0.7' })
    expect(values.get(SETTING_HTTP_REQUEST)).toBe(false)
    expect(await (await get(app)).json()).toEqual({ enabled: false })
    await put(app, { enabled: true })
    expect(await (await get(app)).json()).toEqual({ enabled: true })
  })

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, values } = setup()
    expect((await put(app, { enabled: false }, headers as Record<string, string>)).status).toBe(403)
    expect(values.has(SETTING_HTTP_REQUEST)).toBe(false)
  })

  it.each([[{}], [{ enabled: 'false' }], [{ enabled: false, extra: 1 }]])('refuses the body %j', async (body) => {
    const { app, values } = setup()
    expect((await put(app, body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(setup({ settings: false }).app)).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { enabled: false })).status).toBe(503)
  })
})
