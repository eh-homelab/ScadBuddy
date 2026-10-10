import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { originPolicy } from '../src/http/origins.js'
import type { SettingsRepo } from '../src/routes/headlessBrowser.js'
import { MODEL_SETTING_PATH, registerModelRoutes, storedModel } from '../src/routes/model.js'
import { SETTING_MODEL } from '../src/sessions/manager.js'

// /api/v1/ai/settings/model (#1917): the Claude model every assistant turn and
// the connection test use, behind the same UI guard as every other AI settings
// write (routes/guard.ts). `null` is Claude Code's own default.

const URL_ = `https://scadbuddy.example${MODEL_SETTING_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function setup(options: { ready?: boolean; settings?: boolean; stored?: unknown } = {}) {
  const values = new Map<string, unknown>()
  if (options.stored !== undefined) values.set(SETTING_MODEL, options.stored)
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
  registerModelRoutes(app, {
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

describe('the model setting', () => {
  it("is Claude Code's default until one is chosen, and reads back what was stored", async () => {
    const { app, values, contexts } = setup()
    expect(await (await get(app)).json()).toEqual({ model: null })
    const set = await put(app, { model: 'claude-sonnet-5' })
    expect(set.status).toBe(200)
    expect(await set.json()).toEqual({ model: 'claude-sonnet-5' })
    // Audited as the UI user, from the request's address.
    expect(contexts[0]).toMatchObject({ surface: 'http', clientIp: '10.0.0.7' })
    expect(values.get(SETTING_MODEL)).toBe('claude-sonnet-5')
    expect(await (await get(app)).json()).toEqual({ model: 'claude-sonnet-5' })
  })

  it('goes back to the default with null', async () => {
    const { app, values } = setup({ stored: 'opus' })
    expect(await (await put(app, { model: null })).json()).toEqual({ model: null })
    expect(values.get(SETTING_MODEL)).toBeNull()
    expect(await (await get(app)).json()).toEqual({ model: null })
  })

  it('trims the name it is given', async () => {
    const { app, values } = setup()
    await put(app, { model: '  haiku ' })
    expect(values.get(SETTING_MODEL)).toBe('haiku')
  })

  it.each([
    ['an alias', 'opus'],
    ['a dated id', 'claude-sonnet-4-5-20250929'],
    ['a 1M-context id', 'claude-opus-5-5[1m]'],
    ['a Bedrock id', 'us.anthropic.claude-sonnet-4-5-20250929-v1:0'],
  ])('takes %s', async (_label, model) => {
    const { app, values } = setup()
    expect((await put(app, { model })).status).toBe(200)
    expect(values.get(SETTING_MODEL)).toBe(model)
  })

  it('reads a stored value that is not a usable name as the default', async () => {
    expect(await (await get(setup({ stored: '' }).app)).json()).toEqual({ model: null })
    expect(await (await get(setup({ stored: 42 }).app)).json()).toEqual({ model: null })
  })

  it.each([
    [{}],
    [{ model: 42 }],
    [{ model: '' }],
    [{ model: '   ' }],
    [{ model: 'sonnet; rm -rf /' }],
    [{ model: '--dangerously-skip-permissions' }],
    [{ model: 'x'.repeat(129) }],
    [{ model: 'opus', extra: 1 }],
  ])('refuses the body %j', async (body) => {
    const { app, values } = setup()
    expect((await put(app, body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, values } = setup()
    expect((await put(app, { model: 'opus' }, headers as Record<string, string>)).status).toBe(403)
    expect(values.has(SETTING_MODEL)).toBe(false)
  })

  it('refuses a read over plain HTTP', async () => {
    expect((await get(setup().app, { host: UI.host, 'x-forwarded-proto': 'http' })).status).toBe(403)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(setup({ settings: false }).app)).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { model: 'opus' })).status).toBe(503)
  })
})

describe('storedModel', () => {
  it('is the name a turn and the connection test pass on, or undefined for the default', () => {
    expect(storedModel('opus')).toBe('opus')
    expect(storedModel(null)).toBeUndefined()
    expect(storedModel(undefined)).toBeUndefined()
    expect(storedModel('')).toBeUndefined()
    expect(storedModel(7)).toBeUndefined()
  })
})
