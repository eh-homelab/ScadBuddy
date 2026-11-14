import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6261: the filament catalogs (backend/scadbuddy/api/filament.py).

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const ROW = '8d6f3c1e-0000-4000-8000-0000000061a1'
const LIST = `${BACKEND}/api/v1/filament/catalog/brands`
const ONE = `${LIST}/${ROW}`

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

describe('filament catalog tools', () => {
  it.each([
    ['filament_catalog_list', 'read'],
    ['filament_catalog_upsert', 'write'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('lists a catalog, archived rows on request', async () => {
    server.use(
      http.get(LIST, ({ request }) => {
        expect(new URL(request.url).searchParams.get('include_archived')).toBe('true')
        return HttpResponse.json([{ id: ROW }])
      }),
    )
    const listed = await runTool(tool('filament_catalog_list'), { kind: 'brands', include_archived: true }, ctx())
    expect(firstText(listed)).toEqual([{ id: ROW }])
  })

  it('adds a row with the fields given', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(LIST, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: ROW, name: 'Acme' }, { status: 201 })
      }),
    )
    const result = await runTool(
      tool('filament_catalog_upsert'),
      { kind: 'brands', fields: { name: 'Acme' } },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ name: 'Acme' }])
  })

  it('edits and archives a row, sending a null to clear a field', async () => {
    const bodies: unknown[] = []
    server.use(
      http.patch(ONE, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: ROW })
      }),
    )
    const result = await runTool(
      tool('filament_catalog_upsert'),
      { kind: 'brands', row_id: ROW, fields: { name: null }, archived: true },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ name: null, archived: true }])
  })

  it('refuses to archive a row it has not named', async () => {
    const result = await runTool(tool('filament_catalog_upsert'), { kind: 'brands', archived: true }, ctx())
    expect(result.isError).toBe(true)
  })
})

const TYPES = `${BACKEND}/api/v1/filament/types`
const TYPE = `${TYPES}/${ROW}`

describe('filament type tools', () => {
  it.each([
    ['filament_type_list', 'read'],
    ['filament_type_get', 'read'],
    ['filament_type_upsert', 'write'],
    ['filament_type_set_presets', 'write'],
    ['filament_type_set_k_values', 'write'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('lists types, archived ones on request, and reads one', async () => {
    server.use(
      http.get(TYPES, ({ request }) => {
        expect(new URL(request.url).searchParams.get('include_archived')).toBe('true')
        return HttpResponse.json([{ id: ROW }])
      }),
      http.get(TYPE, () => HttpResponse.json({ id: ROW, presets: [], k_values: [] })),
    )
    const listed = await runTool(tool('filament_type_list'), { include_archived: true }, ctx())
    expect(firstText(listed)).toEqual([{ id: ROW }])
    const one = await runTool(tool('filament_type_get'), { type_id: ROW }, ctx())
    expect(firstText(one)).toEqual({ id: ROW, presets: [], k_values: [] })
  })

  it('adds a type, then edits and archives it', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(TYPES, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: ROW }, { status: 201 })
      }),
      http.patch(TYPE, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: ROW })
      }),
    )
    const fields = { code: 'PLA-RED', rgba: '#FF0000' }
    expect((await runTool(tool('filament_type_upsert'), { fields }, ctx())).isError).toBeFalsy()
    const edited = await runTool(
      tool('filament_type_upsert'),
      { type_id: ROW, fields: { note: null }, archived: true },
      ctx(),
    )
    expect(edited.isError).toBeFalsy()
    expect(bodies).toEqual([fields, { note: null, archived: true }])
    expect((await runTool(tool('filament_type_upsert'), { archived: true }, ctx())).isError).toBe(true)
  })

  it('replaces the presets and the K values', async () => {
    const bodies: unknown[] = []
    server.use(
      http.put(`${TYPE}/presets`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json([])
      }),
      http.put(`${TYPE}/k-values`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json([])
      }),
    )
    const presets = [{ printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00' }]
    const k_values = [{ printer_id: ROW, extruder: 0, nozzle_diameter: '0.4', k_value: '0.02' }]
    expect((await runTool(tool('filament_type_set_presets'), { type_id: ROW, presets }, ctx())).isError).toBeFalsy()
    expect((await runTool(tool('filament_type_set_k_values'), { type_id: ROW, k_values }, ctx())).isError).toBeFalsy()
    expect(bodies).toEqual([presets, k_values])
  })
})
