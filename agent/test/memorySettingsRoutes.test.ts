import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { originPolicy } from '../src/http/origins.js'
import {
  DEFAULT_RECALL_MAX_QUERY_CHARS,
  MAX_RECALL_MAX_QUERY_CHARS,
  SETTING_RECALL_MAX_QUERY_CHARS,
} from '../src/memory/hindsight.js'
import type { SettingsRepo } from '../src/routes/module.js'
import { MEMORY_SETTINGS_PATH, registerMemorySettingsRoutes } from '../src/routes/memorySettings.js'

// /api/v1/ai/settings/memory: the automatic recall query's cap (the Claude Code Hindsight
// plugin's recallMaxQueryChars), behind the same UI guards as every other AI settings route.

const URL_ = `https://scadbuddy.example${MEMORY_SETTINGS_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function setup(options: { ready?: boolean; settings?: boolean; stored?: unknown } = {}) {
  const values = new Map<string, unknown>()
  if (options.stored !== undefined) values.set(SETTING_RECALL_MAX_QUERY_CHARS, options.stored)
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
  registerMemorySettingsRoutes(app, {
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

const viewOf = (chars: number) => ({ recall_max_query_chars: chars, default: 800, max: MAX_RECALL_MAX_QUERY_CHARS })

describe('the memory recall query cap setting', () => {
  it('is 800 characters until set, and reads back what was stored', async () => {
    expect(DEFAULT_RECALL_MAX_QUERY_CHARS).toBe(800)
    const { app, values, contexts } = setup()
    const before = await get(app)
    expect(before.status).toBe(200)
    expect(await before.json()).toEqual(viewOf(800))
    const saved = await put(app, { recall_max_query_chars: 2000 })
    expect(saved.status).toBe(200)
    expect(await saved.json()).toEqual(viewOf(2000))
    expect(contexts[0]).toMatchObject({ surface: 'http', clientIp: '10.0.0.7' })
    expect(values.get(SETTING_RECALL_MAX_QUERY_CHARS)).toBe(2000)
    expect(await (await get(app)).json()).toEqual(viewOf(2000))
  })

  it('takes 0 (no cut) and the maximum', async () => {
    const { app, values } = setup()
    expect((await put(app, { recall_max_query_chars: 0 })).status).toBe(200)
    expect(values.get(SETTING_RECALL_MAX_QUERY_CHARS)).toBe(0)
    expect((await put(app, { recall_max_query_chars: MAX_RECALL_MAX_QUERY_CHARS })).status).toBe(200)
  })

  it.each([
    [{}],
    [{ recall_max_query_chars: '800' }],
    [{ recall_max_query_chars: 800.5 }],
    [{ recall_max_query_chars: -1 }],
    [{ recall_max_query_chars: MAX_RECALL_MAX_QUERY_CHARS + 1 }],
    [{ recall_max_query_chars: 800, extra: 1 }],
  ])('refuses the body %j, and stores nothing', async (body) => {
    const { app, values } = setup()
    expect((await put(app, body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it.each([[-5], [MAX_RECALL_MAX_QUERY_CHARS + 1], ['2000'], [12.5]])(
    'reads a stored %j it could not have written as the default',
    async (stored) => {
      const { app } = setup({ stored })
      expect(await (await get(app)).json()).toEqual(viewOf(800))
    },
  )

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, values } = setup()
    expect((await put(app, { recall_max_query_chars: 100 }, headers as Record<string, string>)).status).toBe(403)
    expect(values.has(SETTING_RECALL_MAX_QUERY_CHARS)).toBe(false)
  })

  it('refuses a read over plain HTTP', async () => {
    const { app } = setup()
    expect((await get(app, { host: UI.host, 'x-forwarded-proto': 'http' })).status).toBe(403)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(setup({ settings: false }).app)).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { recall_max_query_chars: 100 })).status).toBe(503)
  })
})
