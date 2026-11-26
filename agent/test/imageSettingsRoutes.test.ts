import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { originPolicy } from '../src/http/origins.js'
import type { SettingsRepo } from '../src/credentials.js'
import {
  DEFAULT_TOOL_IMAGE_INLINE_MAX,
  MAX_TOOL_IMAGE_INLINE_MAX,
  MIN_TOOL_IMAGE_INLINE_MAX,
  SETTING_TOOL_IMAGE_INLINE_MAX,
} from '../src/tools/imageLimits.js'
import {
  DEFAULT_IMAGE_LONG_EDGE,
  IMAGE_SETTINGS_PATH,
  MAX_IMAGE_LONG_EDGE,
  MIN_IMAGE_LONG_EDGE,
  registerImageSettingsRoutes,
  SETTING_IMAGE_LONG_EDGE,
} from '../src/routes/imageSettings.js'

// /api/v1/ai/settings/images: the long edge the assistant panel scales an
// attached image to before sending it, behind the same UI guards as every other
// AI settings route (routes/guard.ts).

const INLINE = {
  tool_inline_max_bytes: DEFAULT_TOOL_IMAGE_INLINE_MAX,
  tool_inline_min: MIN_TOOL_IMAGE_INLINE_MAX,
  tool_inline_max: MAX_TOOL_IMAGE_INLINE_MAX,
}
const URL_ = `https://scadbuddy.example${IMAGE_SETTINGS_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function setup(options: { ready?: boolean; settings?: boolean; stored?: unknown } = {}) {
  const values = new Map<string, unknown>()
  if (options.stored !== undefined) values.set(SETTING_IMAGE_LONG_EDGE, options.stored)
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
  registerImageSettingsRoutes(app, {
    settings: options.settings === false ? undefined : repo,
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, values, contexts }
}

const get = (app: Hono, headers: Record<string, string> = { host: UI.host, 'x-forwarded-proto': 'https' }) =>
  app.request(URL_, { headers })
const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

describe('the image long-edge setting', () => {
  it('is 1568 px until set, and reads back what was stored', async () => {
    expect(DEFAULT_IMAGE_LONG_EDGE).toBe(1568)
    const { app, values, contexts } = setup()
    const before = await get(app)
    expect(before.status).toBe(200)
    expect(await before.json()).toEqual({ long_edge: 1568, min: MIN_IMAGE_LONG_EDGE, max: MAX_IMAGE_LONG_EDGE, ...INLINE })
    const saved = await put(app, { long_edge: 2000 })
    expect(saved.status).toBe(200)
    expect(await saved.json()).toEqual({ long_edge: 2000, min: MIN_IMAGE_LONG_EDGE, max: MAX_IMAGE_LONG_EDGE, ...INLINE })
    // Audited as the UI user, from the request's address.
    expect(contexts[0]).toMatchObject({ surface: 'http', clientIp: '10.0.0.7' })
    expect(values.get(SETTING_IMAGE_LONG_EDGE)).toBe(2000)
    expect(((await (await get(app)).json()) as { long_edge: number }).long_edge).toBe(2000)
  })

  it("stores a tool image's inline limit apart, and leaves the long edge alone", async () => {
    const { app, values } = setup()
    const saved = await put(app, { tool_inline_max_bytes: 100_000 })
    expect(saved.status).toBe(200)
    expect(await saved.json()).toMatchObject({ long_edge: 1568, tool_inline_max_bytes: 100_000 })
    expect(values.get(SETTING_TOOL_IMAGE_INLINE_MAX)).toBe(100_000)
    expect(values.has(SETTING_IMAGE_LONG_EDGE)).toBe(false)
    expect((await put(app, { tool_inline_max_bytes: MIN_TOOL_IMAGE_INLINE_MAX })).status).toBe(200)
    expect((await put(app, { tool_inline_max_bytes: MAX_TOOL_IMAGE_INLINE_MAX })).status).toBe(200)
    expect((await put(app, { tool_inline_max_bytes: MAX_TOOL_IMAGE_INLINE_MAX + 1 })).status).toBe(400)
    expect((await put(app, { tool_inline_max_bytes: MIN_TOOL_IMAGE_INLINE_MAX - 1 })).status).toBe(400)
  })

  it('takes the bounds themselves', async () => {
    const { app } = setup()
    expect((await put(app, { long_edge: MIN_IMAGE_LONG_EDGE })).status).toBe(200)
    expect((await put(app, { long_edge: MAX_IMAGE_LONG_EDGE })).status).toBe(200)
  })

  it.each([[{}], [{ long_edge: '1568' }], [{ long_edge: 1568.5 }], [{ long_edge: MIN_IMAGE_LONG_EDGE - 1 }], [
    { long_edge: MAX_IMAGE_LONG_EDGE + 1 },
  ], [{ long_edge: 1568, extra: 1 }]])('refuses the body %j, and stores nothing', async (body) => {
    const { app, values } = setup()
    expect((await put(app, body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it.each([[0], [-5], [99_999], ['2000'], [null], [1234.5]])(
    'reads a stored %j it could not have written as the default',
    async (stored) => {
      const { app } = setup({ stored })
      expect(((await (await get(app)).json()) as { long_edge: number }).long_edge).toBe(DEFAULT_IMAGE_LONG_EDGE)
    },
  )

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, values } = setup()
    expect((await put(app, { long_edge: 2000 }, headers as Record<string, string>)).status).toBe(403)
    expect(values.has(SETTING_IMAGE_LONG_EDGE)).toBe(false)
  })

  it('refuses a read over plain HTTP', async () => {
    const { app } = setup()
    expect((await get(app, { host: UI.host, 'x-forwarded-proto': 'http' })).status).toBe(403)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(setup({ settings: false }).app)).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { long_edge: 2000 })).status).toBe(503)
  })
})
