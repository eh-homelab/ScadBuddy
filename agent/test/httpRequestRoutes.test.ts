import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { BUILTIN_SOURCES } from '../src/access/model.js'
import { originPolicy } from '../src/http/origins.js'
import { HTTP_REQUEST_SETTING_PATH, registerHttpRequestRoutes } from '../src/routes/httpRequest.js'
import { memoryAccess } from './support/memoryAccess.js'

// /api/v1/ai/settings/http-request (#827): the switch for the assistant's
// http_request tool, ON by default, behind the same UI guard as every other AI
// settings write (routes/guard.ts). Since #2171 it is the `builtin:http` source's
// switch in `ai_access`.

const URL_ = `https://scadbuddy.example${HTTP_REQUEST_SETTING_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function setup(options: { ready?: boolean; access?: boolean } = {}) {
  const { access, rows } = memoryAccess()
  const contexts: unknown[] = []
  const setEnabled = access.setEnabled.bind(access)
  access.setEnabled = (source, enabled, context) => {
    contexts.push(context)
    return setEnabled(source, enabled, context)
  }
  const app = new Hono()
  registerHttpRequestRoutes(app, {
    access: options.access === false ? undefined : access,
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, rows, contexts }
}

const get = (app: Hono) => app.request(URL_, { headers: { host: UI.host, 'x-forwarded-proto': 'https' } })
const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

describe('the http_request setting', () => {
  it('is on until turned off, and reads back what was stored', async () => {
    const { app, contexts } = setup()
    expect(await (await get(app)).json()).toEqual({ enabled: true })
    const off = await put(app, { enabled: false })
    expect(off.status).toBe(200)
    // Audited as the UI user, from the request's address.
    expect(contexts[0]).toMatchObject({ surface: 'http', clientIp: '10.0.0.7' })
    expect(await (await get(app)).json()).toEqual({ enabled: false })
    await put(app, { enabled: true })
    expect(await (await get(app)).json()).toEqual({ enabled: true })
  })

  it("PUT false stores builtin:http '*' off in ai_access", async () => {
    const { app, rows } = setup()
    expect((await put(app, { enabled: false })).status).toBe(200)
    expect(rows).toEqual([{ source: BUILTIN_SOURCES.http, item: '*', access: 'off', updated_by: 'browser:browser' }])
    await put(app, { enabled: true })
    expect(rows).toEqual([{ source: BUILTIN_SOURCES.http, item: '*', access: 'on', updated_by: 'browser:browser' }])
  })

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, rows } = setup()
    expect((await put(app, { enabled: false }, headers as Record<string, string>)).status).toBe(403)
    expect(rows).toEqual([])
  })

  it.each([[{}], [{ enabled: 'false' }], [{ enabled: false, extra: 1 }]])('refuses the body %j', async (body) => {
    const { app, rows } = setup()
    expect((await put(app, body)).status).toBe(400)
    expect(rows).toEqual([])
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(setup({ access: false }).app)).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { enabled: false })).status).toBe(503)
  })
})
