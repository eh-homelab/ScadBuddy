import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import type { AuditContext } from '../src/audit/log.js'
import { UI_ACTOR } from '../src/audit/writes.js'
import { originPolicy } from '../src/http/origins.js'
import type { SettingsRepo } from '../src/credentials.js'
import { registerRenderLimitsRoutes, RENDER_LIMITS_PATH } from '../src/routes/renderLimits.js'
import { RenderLimiter, SETTING_RENDER_LIMITS, settingsRenderLimits } from '../src/tools/renderLimits.js'

// /api/v1/ai/settings/render-limits: the agent's render limits, per principal
// and in all, which the render limiter reads at runtime.

const URL_ = `https://scadbuddy.example${RENDER_LIMITS_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const READ = { host: UI.host, 'x-forwarded-proto': 'https' }

function setup(options: { ready?: boolean; settings?: boolean } = {}) {
  const values = new Map<string, unknown>()
  const contexts: AuditContext[] = []
  const repo: SettingsRepo = {
    get: <T>(key: string) => Promise.resolve(values.get(key) as T),
    set: (key, value, context) => {
      values.set(key, value)
      contexts.push(context)
      return Promise.resolve()
    },
  }
  const app = new Hono()
  registerRenderLimitsRoutes(app, {
    settings: options.settings === false ? undefined : repo,
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, values, contexts, repo }
}

const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

const LIMITS = { per_principal: 6, global: 12, per_window: 0, overrides: [{ principal: 'token:ci', concurrent: 10 }] }

describe('the render limits setting', () => {
  it('reads the defaults until set, then what was stored', async () => {
    const { app, values } = setup()
    const get = async () => (await app.request(URL_, { headers: READ })).json()
    expect(await get()).toEqual({ per_principal: 4, global: 0, per_window: 30, overrides: [] })
    const res = await put(app, LIMITS)
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual(LIMITS)
    expect(values.get(SETTING_RENDER_LIMITS)).toEqual(LIMITS)
    expect(await get()).toEqual(LIMITS)
  })

  it('applies to the limiter at runtime, with no restart', async () => {
    const { app, repo } = setup()
    const limiter = new RenderLimiter(settingsRenderLimits(repo))
    await put(app, { ...LIMITS, per_principal: 1, overrides: [] })
    await limiter.acquire('browser')
    expect(limiter.limits.concurrent).toBe(1)
    await put(app, { ...LIMITS, per_principal: 2, overrides: [] })
    await limiter.acquire('browser')
    expect(limiter.limits).toMatchObject({ concurrent: 2, global: 12 })
  })

  it('audits the write as the browser user', async () => {
    const { app, contexts } = setup()
    expect((await put(app, LIMITS)).status).toBe(200)
    expect(contexts).toEqual([{ actor: UI_ACTOR, surface: 'http', clientIp: '10.0.0.7' }])
  })

  it.each([
    ['a per-principal limit of 0', { ...LIMITS, per_principal: 0 }],
    ['a fractional global limit', { ...LIMITS, global: 1.5 }],
    ['an override past the most', { ...LIMITS, overrides: [{ principal: 'x', concurrent: 65 }] }],
    ['an empty principal', { ...LIMITS, overrides: [{ principal: ' ', concurrent: 2 }] }],
    ['two overrides for one principal', { ...LIMITS, overrides: [{ principal: 'x', concurrent: 2 }, { principal: 'x', concurrent: 3 }] }],
    ['an unknown field', { ...LIMITS, extra: 1 }],
  ])('refuses %s with 400', async (_label, body) => {
    const { app, values } = setup()
    const res = await put(app, body)
    expect(res.status).toBe(400)
    expect(values.size).toBe(0)
  })

  it('refuses a write from another origin', async () => {
    const { app, values } = setup()
    expect((await put(app, LIMITS, { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect(values.size).toBe(0)
  })

  it('answers 503 without the database', async () => {
    const { app } = setup({ settings: false })
    expect((await app.request(URL_, { headers: READ })).status).toBe(503)
  })
})
