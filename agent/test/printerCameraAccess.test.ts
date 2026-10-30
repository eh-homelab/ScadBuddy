import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { BUILTIN_SOURCES } from '../src/access/model.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
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
