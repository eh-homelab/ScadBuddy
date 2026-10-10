import { Hono } from 'hono'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { originPolicy } from '../src/http/origins.js'
import type { SettingsRepo } from '../src/routes/headlessBrowser.js'
import { PRINTER_CAMERA_SETTING_PATH, registerPrinterCameraRoutes } from '../src/routes/printerCamera.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runToolWithOutcome, type Tool, type ToolContext } from '../src/tools/registry.js'
import { SETTING_PRINTER_CAMERA, toolSwitches } from '../src/tools/switches.js'
import { BACKEND, services } from './helpers/mcp.js'

// #1911: get_printer_camera is privacy-sensitive (#251), so Settings can turn
// it off. A switched-off tool is refused in runToolWithOutcome, the one entry
// point every projection takes (the harness, /mcp, a durable session's
// activities), before the handler or the backend is reached.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function repo(initial: Record<string, unknown> = {}) {
  const values = new Map<string, unknown>(Object.entries(initial))
  const reads: string[] = []
  const contexts: unknown[] = []
  const settings: SettingsRepo = {
    get: <T>(key: string) => {
      reads.push(key)
      return Promise.resolve(values.get(key) as T)
    },
    set: (key, value, context) => {
      contexts.push(context)
      values.set(key, value)
      return Promise.resolve()
    },
  }
  return { settings, values, reads, contexts }
}

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(overrides: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

describe('toolSwitches', () => {
  it('leaves the camera on until it is stored false', async () => {
    const { settings, values } = repo()
    const off = toolSwitches(settings)
    expect(await off('get_printer_camera')).toBeUndefined()
    values.set(SETTING_PRINTER_CAMERA, true)
    expect(await off('get_printer_camera')).toBeUndefined()
    values.set(SETTING_PRINTER_CAMERA, false)
    expect(await off('get_printer_camera')).toMatch(/turned off in Settings/)
  })

  it('reads no setting for a tool without a switch', async () => {
    const { settings, reads } = repo({ [SETTING_PRINTER_CAMERA]: false })
    expect(await toolSwitches(settings)('get_print_targets')).toBeUndefined()
    expect(reads).toEqual([])
  })

  it('refuses when the setting cannot be read: a privacy switch fails closed', async () => {
    const off = toolSwitches({ get: () => Promise.reject(new Error('db down')) })
    expect(await off('get_printer_camera')).toMatch(/cannot be read/)
  })

  it('is on without a database: there is nowhere to turn it off', async () => {
    expect(await toolSwitches(undefined)('get_printer_camera')).toBeUndefined()
  })
})

describe('a switched-off tool', () => {
  it('is refused before the backend is reached', async () => {
    let fetched = false
    server.use(
      http.get(`${BACKEND}/api/v1/print/printers/7/camera`, () => {
        fetched = true
        return new HttpResponse(new Uint8Array([0xff, 0xd8]), { headers: { 'content-type': 'image/jpeg' } })
      }),
    )
    const { settings } = repo({ [SETTING_PRINTER_CAMERA]: false })
    const run = await runToolWithOutcome(
      tool('get_printer_camera'),
      { printer_id: 7 },
      ctx({ switchedOff: toolSwitches(settings) }),
    )
    expect(run.outcome).toBe('refused')
    expect(run.result.isError).toBe(true)
    expect(run.detail).toMatch(/get_printer_camera is turned off in Settings/)
    expect(fetched).toBe(false)
  })

  it('runs once it is back on', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/print/printers/7/camera`, () =>
        new HttpResponse(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), { headers: { 'content-type': 'image/jpeg' } }),
      ),
    )
    const { settings } = repo({ [SETTING_PRINTER_CAMERA]: true })
    const run = await runToolWithOutcome(
      tool('get_printer_camera'),
      { printer_id: 7 },
      ctx({ switchedOff: toolSwitches(settings) }),
    )
    expect(run.outcome).toBe('ok')
  })
})

const URL_ = `https://scadbuddy.example${PRINTER_CAMERA_SETTING_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function app(options: { ready?: boolean; settings?: boolean } = {}) {
  const r = repo()
  const a = new Hono()
  registerPrinterCameraRoutes(a, {
    settings: options.settings === false ? undefined : r.settings,
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app: a, ...r }
}

const get = (a: Hono) => a.request(URL_, { headers: { host: UI.host, 'x-forwarded-proto': 'https' } })
const put = (a: Hono, body: unknown, headers: Record<string, string> = UI) =>
  a.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

describe('/api/v1/ai/settings/printer-camera', () => {
  it('is on until turned off, and reads back what was stored', async () => {
    const { app: a, values, contexts } = app()
    expect(await (await get(a)).json()).toEqual({ enabled: true })
    expect((await put(a, { enabled: false })).status).toBe(200)
    expect(values.get(SETTING_PRINTER_CAMERA)).toBe(false)
    expect(contexts[0]).toMatchObject({ surface: 'http', clientIp: '10.0.0.7' })
    expect(await (await get(a)).json()).toEqual({ enabled: false })
    await put(a, { enabled: true })
    expect(await (await get(a)).json()).toEqual({ enabled: true })
  })

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app: a, values } = app()
    expect((await put(a, { enabled: false }, headers as Record<string, string>)).status).toBe(403)
    expect(values.size).toBe(0)
  })

  it.each([[{}], [{ enabled: 'false' }], [{ enabled: false, extra: 1 }]])('refuses the body %j', async (body) => {
    const { app: a, values } = app()
    expect((await put(a, body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(app({ settings: false }).app)).status).toBe(503)
    expect((await put(app({ ready: false }).app, { enabled: false })).status).toBe(503)
  })
})
