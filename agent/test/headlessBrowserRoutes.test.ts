import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { SETTING_HEADLESS_BROWSER } from '../src/harness/headlessBrowser.js'
import { originPolicy } from '../src/http/origins.js'
import { HEADLESS_BROWSER_SETTING_PATH, registerHeadlessBrowserRoutes, type SettingsRepo } from '../src/routes/headlessBrowser.js'

// /api/v1/ai/settings/headless-browser (#349): the setting that turns the
// headless browser on, behind the same UI guard as every other AI settings
// write (routes/guard.ts).

const URL_ = `https://scadbuddy.example${HEADLESS_BROWSER_SETTING_PATH}`
/** The UI through the TLS ingress. */
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function setup(options: { ready?: boolean; settings?: boolean } = {}) {
  const values = new Map<string, unknown>()
  const repo: SettingsRepo = {
    get: <T>(key: string) => Promise.resolve(values.get(key) as T),
    set: (key, value) => {
      values.set(key, value)
      return Promise.resolve()
    },
  }
  const app = new Hono()
  registerHeadlessBrowserRoutes(app, {
    settings: options.settings === false ? undefined : repo,
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, values }
}

const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, {
    method: 'PUT',
    headers: { ...headers, 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })

describe('the headless-browser setting', () => {
  it('is off until turned on, and reads back what was stored', async () => {
    const { app, values } = setup()
    const get = () => app.request(URL_, { headers: { host: UI.host, 'x-forwarded-proto': 'https' } })
    expect(await (await get()).json()).toEqual({ enabled: false })

    const on = await put(app, { enabled: true })
    expect(on.status).toBe(200)
    expect(await on.json()).toEqual({ enabled: true })
    expect(values.get(SETTING_HEADLESS_BROWSER)).toBe(true)
    expect(await (await get()).json()).toEqual({ enabled: true })

    expect(await (await put(app, { enabled: false })).json()).toEqual({ enabled: false })
    expect(values.get(SETTING_HEADLESS_BROWSER)).toBe(false)
  })

  it('treats anything stored but `true` as off', async () => {
    const { app, values } = setup()
    values.set(SETTING_HEADLESS_BROWSER, 'yes')
    const res = await app.request(URL_, { headers: { host: UI.host, 'x-forwarded-proto': 'https' } })
    expect(await res.json()).toEqual({ enabled: false })
  })

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, values } = setup()
    const res = await put(app, { enabled: true }, headers as Record<string, string>)
    expect(res.status).toBe(403)
    expect(values.has(SETTING_HEADLESS_BROWSER)).toBe(false)
  })

  it('refuses a cross-site read', async () => {
    const { app } = setup()
    const res = await app.request(URL_, {
      headers: { host: UI.host, 'x-forwarded-proto': 'https', 'sec-fetch-site': 'cross-site' },
    })
    expect(res.status).toBe(403)
  })

  it.each([[{}], [{ enabled: 'true' }], [{ enabled: true, extra: 1 }]])('refuses the body %j', async (body) => {
    const { app, values } = setup()
    expect((await put(app, body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await put(setup({ settings: false }).app, { enabled: true })).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { enabled: true })).status).toBe(503)
  })
})
