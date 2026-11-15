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

const SPOOLS = `${BACKEND}/api/v1/filament/spools`
const SPOOL = `${SPOOLS}/${ROW}`

describe('filament spool tools', () => {
  it.each([
    ['filament_spool_list', 'read'],
    ['filament_spool_get', 'read'],
    ['filament_spool_events', 'read'],
    ['filament_spool_upsert', 'write'],
    ['filament_spool_archive', 'write'],
    ['filament_spool_delete', 'write'],
    ['filament_spool_bind_tag', 'write'],
    ['filament_tag_unbind', 'write'],
    ['filament_spool_add_event', 'write'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('lists the spools that need a type, reads one and its ledger', async () => {
    server.use(
      http.get(SPOOLS, ({ request }) => {
        expect(new URL(request.url).searchParams.get('needs_type')).toBe('true')
        return HttpResponse.json([{ id: ROW }])
      }),
      http.get(SPOOL, () => HttpResponse.json({ id: ROW })),
      http.get(`${SPOOL}/events`, () => HttpResponse.json([{ kind: 'created' }])),
    )
    const listed = await runTool(tool('filament_spool_list'), { needs_type: true }, ctx())
    expect(firstText(listed)).toEqual([{ id: ROW }])
    expect(firstText(await runTool(tool('filament_spool_get'), { spool_id: ROW }, ctx()))).toEqual({ id: ROW })
    const events = await runTool(tool('filament_spool_events'), { spool_id: ROW }, ctx())
    expect(firstText(events)).toEqual([{ kind: 'created' }])
  })

  it('adds a spool, edits it, archives it and deletes another', async () => {
    const calls: unknown[] = []
    server.use(
      http.post(SPOOLS, async ({ request }) => {
        calls.push(['add', await request.json()])
        return HttpResponse.json({ id: ROW }, { status: 201 })
      }),
      http.patch(SPOOL, async ({ request }) => {
        calls.push(['edit', await request.json()])
        return HttpResponse.json({ id: ROW })
      }),
      http.post(`${SPOOL}/archive`, async ({ request }) => {
        calls.push(['archive', await request.json()])
        return HttpResponse.json({ id: ROW })
      }),
      http.delete(SPOOL, () => {
        calls.push(['delete'])
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const fields = { type_id: ROW, grams_left: 1000 }
    expect((await runTool(tool('filament_spool_upsert'), { fields }, ctx())).isError).toBeFalsy()
    const edit = { spool_id: ROW, fields: { rgba: null } }
    expect((await runTool(tool('filament_spool_upsert'), edit, ctx())).isError).toBeFalsy()
    expect((await runTool(tool('filament_spool_archive'), { spool_id: ROW }, ctx())).isError).toBeFalsy()
    const deleted = await runTool(tool('filament_spool_delete'), { spool_id: ROW }, ctx())
    expect(firstText(deleted)).toEqual({ deleted: ROW })
    expect(calls).toEqual([['add', fields], ['edit', { rgba: null }], ['archive', { archived: true }], ['delete']])
  })

  it('says why a spool that printed is not deleted', async () => {
    server.use(http.delete(SPOOL, () => HttpResponse.json({ detail: 'archive it instead' }, { status: 409 })))
    const refused = await runTool(tool('filament_spool_delete'), { spool_id: ROW }, ctx())
    expect(refused.isError).toBe(true)
  })

  it('binds and unbinds a tag, and records a weighing', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${SPOOL}/tags`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ tag_id: ROW }, { status: 201 })
      }),
      http.delete(`${BACKEND}/api/v1/filament/tags/${ROW}/binding`, () => HttpResponse.json({ tag_id: ROW })),
      http.post(`${SPOOL}/events`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ kind: 'weighed' }, { status: 201 })
      }),
    )
    const bind = { spool_id: ROW, tag_uid: 'AABBCCDD', kind: 'bambu_mifare' }
    expect((await runTool(tool('filament_spool_bind_tag'), bind, ctx())).isError).toBeFalsy()
    expect((await runTool(tool('filament_tag_unbind'), { tag_id: ROW }, ctx())).isError).toBeFalsy()
    const weigh = { spool_id: ROW, kind: 'weighed', grams: 640 }
    expect((await runTool(tool('filament_spool_add_event'), weigh, ctx())).isError).toBeFalsy()
    expect(bodies).toEqual([
      { tag_uid: 'AABBCCDD', kind: 'bambu_mifare', override: false },
      { kind: 'weighed', source: 'person', grams: 640 },
    ])
    expect((await runTool(tool('filament_spool_bind_tag'), { spool_id: ROW }, ctx())).isError).toBe(true)
  })
})

const LOCATIONS = `${BACKEND}/api/v1/filament/locations`

describe('filament location tools', () => {
  it.each([
    ['filament_location_list', 'read'],
    ['filament_location_upsert', 'write'],
    ['filament_spool_move', 'write'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('lists the tree, adds a place and edits another', async () => {
    const calls: unknown[] = []
    server.use(
      http.get(LOCATIONS, ({ request }) => {
        expect(new URL(request.url).searchParams.get('include_archived')).toBe('true')
        return HttpResponse.json([{ id: ROW, children: [] }])
      }),
      http.post(LOCATIONS, async ({ request }) => {
        calls.push(['add', await request.json()])
        return HttpResponse.json({ id: ROW }, { status: 201 })
      }),
      http.patch(`${LOCATIONS}/${ROW}`, async ({ request }) => {
        calls.push(['edit', await request.json()])
        return HttpResponse.json({ id: ROW })
      }),
    )
    const listed = await runTool(tool('filament_location_list'), { include_archived: true }, ctx())
    expect(firstText(listed)).toEqual([{ id: ROW, children: [] }])
    const fields = { name: 'Shelf A', kind_id: ROW }
    expect((await runTool(tool('filament_location_upsert'), { fields }, ctx())).isError).toBeFalsy()
    const edit = { location_id: ROW, fields: { note: null }, archived: true }
    expect((await runTool(tool('filament_location_upsert'), edit, ctx())).isError).toBeFalsy()
    expect(calls).toEqual([['add', fields], ['edit', { note: null, archived: true }]])
    expect((await runTool(tool('filament_location_upsert'), { archived: true }, ctx())).isError).toBe(true)
  })

  it('moves a spool, and takes one out of its place', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${SPOOLS}/${ROW}/move`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: ROW })
      }),
    )
    const to = { spool_id: ROW, location_id: ROW }
    expect((await runTool(tool('filament_spool_move'), to, ctx())).isError).toBeFalsy()
    const out = { spool_id: ROW, location_id: null }
    expect((await runTool(tool('filament_spool_move'), out, ctx())).isError).toBeFalsy()
    expect(bodies).toEqual([{ location_id: ROW }, { location_id: null }])
  })

  it("imports Bambuddy's spools and says what it did", async () => {
    expect(tool('filament_bambuddy_import').risk).toBe('write')
    expect(tool('filament_bambuddy_import').gated).toBe(false)
    const report = { created: 1, linked: 0, updated: 0, needs_type: 1, created_ids: [ROW] }
    let calls = 0
    server.use(
      http.post(`${BACKEND}/api/v1/filament/bambuddy/import`, () => {
        calls += 1
        return HttpResponse.json(report)
      }),
    )
    const imported = await runTool(tool('filament_bambuddy_import'), {}, ctx())
    expect(firstText(imported)).toEqual(report)
    expect(calls).toBe(1)
  })

  it('says when no Bambuddy is configured', async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/filament/bambuddy/import`, () =>
        HttpResponse.json({ detail: 'Bambuddy is not configured' }, { status: 409 }),
      ),
    )
    const refused = await runTool(tool('filament_bambuddy_import'), {}, ctx())
    expect(refused.isError).toBe(true)
  })
})
