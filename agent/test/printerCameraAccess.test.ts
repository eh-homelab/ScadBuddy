import { Hono } from 'hono'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { originPolicy } from '../src/http/origins.js'
import { BUILTIN_SOURCES } from '../src/access/model.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
import { PRINTER_CAMERA_SETTING_PATH, registerPrinterCameraRoutes } from '../src/routes/printerCamera.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runToolWithOutcome, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'
import { memoryAccess } from './support/memoryAccess.js'

// #1911: get_printer_camera is privacy-sensitive (#251), so Settings can turn
// it off. A switched-off tool is refused in runToolWithOutcome, the one entry
// point every projection takes (the harness, /mcp, a durable session's
// activities), before the handler or the backend is reached.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

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

const CAMERA = BUILTIN_SOURCES.scadbuddy
const OFF = { source: CAMERA, item: 'get_printer_camera', access: 'off', updated_by: 'browser:browser' }
const jpeg = () =>
  new HttpResponse(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), { headers: { 'content-type': 'image/jpeg' } })

describe('get_printer_camera access', () => {
  it('refuses get_printer_camera once it is off, before the backend is reached', async () => {
    let fetched = false
    server.use(
      http.get(`${BACKEND}/api/v1/print/printers/7/camera`, () => {
        fetched = true
        return jpeg()
      }),
    )
    const { access } = memoryAccess([OFF])
    const run = await runToolWithOutcome(
      tool('get_printer_camera'),
      { printer_id: 7 },
      ctx({ toolOverrides: new BuiltInTools(access).registryOverrides() }),
    )
    expect(run.outcome).toBe('refused')
    expect(run.result.isError).toBe(true)
    expect(run.detail).toMatch(/disabled in Settings/)
    expect(fetched).toBe(false)
  })

  it('refuses get_printer_camera when its access cannot be read', async () => {
    let fetched = false
    server.use(
      http.get(`${BACKEND}/api/v1/print/printers/7/camera`, () => {
        fetched = true
        return jpeg()
      }),
    )
    const access = { rows: () => Promise.reject(new Error('db down')) }
    const run = await runToolWithOutcome(
      tool('get_printer_camera'),
      { printer_id: 7 },
      ctx({ toolOverrides: new BuiltInTools(access).registryOverrides() }),
    )
    expect(run.outcome).toBe('refused')
    expect(run.detail).toMatch(/cannot be read/)
    expect(fetched).toBe(false)
  })

  it('runs get_printer_camera at its own tier without a database', async () => {
    server.use(http.get(`${BACKEND}/api/v1/print/printers/7/camera`, jpeg))
    const run = await runToolWithOutcome(
      tool('get_printer_camera'),
      { printer_id: 7 },
      ctx({ toolOverrides: new BuiltInTools(undefined).registryOverrides() }),
    )
    expect(run.outcome).toBe('ok')
  })
})

const URL_ = `https://scadbuddy.example${PRINTER_CAMERA_SETTING_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function app(options: { ready?: boolean; access?: boolean } = {}) {
  const r = memoryAccess()
  const a = new Hono()
  registerPrinterCameraRoutes(a, {
    access: options.access === false ? undefined : r.access,
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
    const { app: a, rows } = app()
    expect(await (await get(a)).json()).toEqual({ enabled: true })
    expect((await put(a, { enabled: false })).status).toBe(200)
    expect(rows).toEqual([expect.objectContaining({ source: CAMERA, item: 'get_printer_camera', access: 'off' })])
    expect(await (await get(a)).json()).toEqual({ enabled: false })
    await put(a, { enabled: true })
    expect(rows).toEqual([])
    expect(await (await get(a)).json()).toEqual({ enabled: true })
  })

  it('PUT true leaves a raised get_printer_camera tier in place', async () => {
    const { app: a, rows } = app()
    rows.push({ source: CAMERA, item: 'get_printer_camera', access: 'outward', updated_by: 'ui:x' })
    expect((await put(a, { enabled: true })).status).toBe(200)
    expect(rows).toEqual([expect.objectContaining({ item: 'get_printer_camera', access: 'outward' })])
    expect(await (await get(a)).json()).toEqual({ enabled: true })
  })

  it('PUT true keeps a tier set while it ran: it deletes only an off row, in one statement (review of #4838)', async () => {
    const r = memoryAccess([{ source: CAMERA, item: 'get_printer_camera', access: 'off', updated_by: 'ui:x' }])
    // Another write sets a tier while the PUT runs: after any read, before any delete.
    let landed = false
    const land = () => {
      if (landed) return
      landed = true
      r.rows.splice(0, r.rows.length, { source: CAMERA, item: 'get_printer_camera', access: 'outward', updated_by: 'ui:y' })
    }
    const raced = {
      ...r.access,
      rows: async (sources: readonly string[]) => {
        const before = await r.access.rows(sources)
        land()
        return before
      },
      resetItem: async (...args: Parameters<typeof r.access.resetItem>) => {
        land()
        return r.access.resetItem(...args)
      },
      resetItemIf: async (...args: Parameters<typeof r.access.resetItemIf>) => {
        land()
        return r.access.resetItemIf(...args)
      },
    }
    const a = new Hono()
    registerPrinterCameraRoutes(a, {
      access: raced,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    expect((await put(a, { enabled: true })).status).toBe(200)
    expect(r.rows).toEqual([expect.objectContaining({ item: 'get_printer_camera', access: 'outward' })])
  })

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app: a, rows } = app()
    expect((await put(a, { enabled: false }, headers as Record<string, string>)).status).toBe(403)
    expect(rows).toEqual([])
  })

  it.each([[{}], [{ enabled: 'false' }], [{ enabled: false, extra: 1 }]])('refuses the body %j', async (body) => {
    const { app: a, rows } = app()
    expect((await put(a, body)).status).toBe(400)
    expect(rows).toEqual([])
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(app({ access: false }).app)).status).toBe(503)
    expect((await put(app({ ready: false }).app, { enabled: false })).status).toBe(503)
  })
})
