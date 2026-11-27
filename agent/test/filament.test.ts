import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { z } from 'zod'
import type {
  FilamentPresetDetail,
  FilamentPresetDiff,
  FilamentPresetKeyChange,
  FilamentPresetRow,
  FilamentPresetVersion,
} from '../src/api/filamentPresets.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { TOUCHES_NOTHING } from '../src/sessions/touched.js'
import { toolActivities, toolCallActivities } from '../src/temporal/toolActivities.js'
import { toolCallActivity } from '../src/temporal/toolCall.js'
import { withUiPaths } from '../src/tools/filamentLinks.js'
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

function ctx(tier: 'write' | 'outward' = 'write'): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo(tier) },
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
    expect(firstText(listed)).toEqual([{ id: ROW, ui_path: '/filament/catalog?kind=brands' }])
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

describe('filament catalog aliases (#6706)', () => {
  const ALIASES = `${ONE}/aliases`
  const RESOLVE = `${LIST}/resolve`

  it.each([
    ['filament_catalog_set_aliases', 'write'],
    ['filament_catalog_resolve', 'read'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('puts the whole alias list of a row', async () => {
    const bodies: unknown[] = []
    server.use(
      http.put(ALIASES, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: ROW, name: 'Bambu Lab', aliases: ['Bambu', 'BL'] })
      }),
    )
    const result = await runTool(tool('filament_catalog_set_aliases'), { kind: 'brands', row_id: ROW, aliases: ['Bambu', 'BL'] }, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ aliases: ['Bambu', 'BL'] }])
    expect(firstText(result)).toMatchObject({ aliases: ['Bambu', 'BL'] })
  })

  it('says which row holds a taken alias', async () => {
    server.use(
      http.put(ALIASES, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: "the brand 'Sunlu' already has a name or alias that matches 'sun-lu'", brand_id: ROW, brand_name: 'Sunlu' },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const result = await runTool(tool('filament_catalog_set_aliases'), { kind: 'brands', row_id: ROW, aliases: ['sun-lu'] }, ctx())
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toContain("the brand 'Sunlu'")
  })

  it('takes only the catalogs that have aliases', async () => {
    const result = await runTool(tool('filament_catalog_set_aliases'), { kind: 'colours', row_id: ROW, aliases: ['x'] }, ctx())
    expect(result.isError).toBe(true)
  })

  it('resolves a name by key, a subtype within its material', async () => {
    server.use(
      http.get(RESOLVE, ({ request }) => {
        const query = new URL(request.url).searchParams
        expect([query.get('name'), query.get('material_id')]).toEqual(['bambu-lab', null])
        return HttpResponse.json({ id: ROW, name: 'Bambu Lab' })
      }),
    )
    const found = await runTool(tool('filament_catalog_resolve'), { kind: 'brands', name: 'bambu-lab' }, ctx())
    expect(firstText(found)).toMatchObject({ id: ROW })
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
    expect(firstText(listed)).toEqual([{ id: ROW, ui_path: `/filament/types/${ROW}` }])
    const one = await runTool(tool('filament_type_get'), { type_id: ROW }, ctx())
    expect(firstText(one)).toEqual({ id: ROW, ui_path: `/filament/types/${ROW}`, presets: [], k_values: [] })
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

  it('sends properties with the row on create, in one request, and answers them (#7703)', async () => {
    const calls: string[] = []
    const PROPS = `${BACKEND}/api/v1/entities/${encodeURIComponent(`spool_type:${ROW}`)}/properties`
    server.use(
      http.post(TYPES, async ({ request }) => {
        calls.push(`POST ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ROW }, { status: 201 })
      }),
      http.get(PROPS, () => {
        calls.push('GET props')
        return HttpResponse.json({ properties: [{ key: 'gtin', value: ['0123'], source: 'manual' }] })
      }),
    )
    const added = await runTool(tool('filament_type_upsert'), { fields: { code: 'PLA-RED' }, properties: { gtin: ['0123'], td: null } }, ctx())
    expect(added.isError).toBeFalsy()
    expect(firstText(added)).toMatchObject({ id: ROW, properties: [{ key: 'gtin', value: ['0123'] }] })
    expect(calls).toEqual(['POST {"code":"PLA-RED","properties":{"gtin":["0123"],"td":null}}', 'GET props'])
  })

  it('answers a refused value sent with the row as nothing saved (#7703)', async () => {
    server.use(
      http.patch(TYPE, () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/entities/bad_property_value', title: 'Bad property value', status: 422, detail: 'td: not a number' },
          { status: 422 },
        ),
      ),
    )
    const result = await runTool(tool('filament_type_upsert'), { type_id: ROW, fields: { note: 'x' }, properties: { td: 'thick' } }, ctx())
    expect(result.isError).toBe(true)
    const text = String(firstText(result))
    expect(text).toContain('td: not a number')
    expect(text).not.toContain('was saved')
  })

  it('names each missing key on a refusal, and each ill-fitting one with its reason (#7703)', async () => {
    const refusal = () =>
      HttpResponse.json(
        {
          type: 'https://scadbuddy.dev/problems/entities/missing_required_properties',
          title: 'Missing required properties',
          status: 422,
          code: 'missing_required_properties',
          detail: 'required properties are missing or do not fit',
          missing: ['mpn', 'gtin'],
          invalid: [{ key: 'td', reason: "'thick' is not of type 'number'" }],
        },
        { status: 422 },
      )
    server.use(
      http.post(TYPES, refusal),
      http.patch(TYPE, refusal),
      http.post(SPOOLS, refusal),
      http.patch(SPOOL, refusal),
      http.post(`${BACKEND}/api/v1/filament/spools/bulk`, refusal),
      http.post(LIST, refusal),
      http.patch(ONE, refusal),
    )
    for (const [name, args] of [
      ['filament_type_upsert', { fields: { code: 'PLA-RED' } }],
      ['filament_type_upsert', { type_id: ROW, fields: { note: 'x' } }],
      ['filament_spool_upsert', { fields: { type_id: ROW } }],
      ['filament_spool_upsert', { spool_id: ROW, fields: { note: 'x' } }],
      ['filament_spool_upsert', { quantity: 2, fields: { type_id: ROW } }],
      ['filament_catalog_upsert', { kind: 'brands', fields: { name: 'Acme' } }],
      ['filament_catalog_upsert', { kind: 'brands', row_id: ROW, fields: { name: 'Acme' } }],
    ] as const) {
      const result = await runTool(tool(name), args, ctx())
      expect(result.isError, name).toBe(true)
      const text = String(firstText(result))
      expect(text, name).toContain('missing: mpn, gtin')
      expect(text, name).toContain('td')
      expect(text, name).toContain("'thick' is not of type 'number'")
      expect(text, name).toContain('`properties`')
      expect(text, name).not.toContain('was saved')
    }
  })

  it('sends no type PATCH for a properties-only edit, and the properties inside the PATCH with fields (#7026, #7703)', async () => {
    const calls: string[] = []
    const PROPS = `${BACKEND}/api/v1/entities/${encodeURIComponent(`spool_type:${ROW}`)}/properties`
    server.use(
      http.patch(TYPE, async ({ request }) => {
        calls.push(`PATCH type ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ROW })
      }),
      http.get(TYPE, () => {
        calls.push('GET type')
        return HttpResponse.json({ id: ROW, code: 'PLA-RED' })
      }),
      http.patch(PROPS, async ({ request }) => {
        calls.push(`PATCH props ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ properties: [{ key: 'mpn', value: 'M1', source: 'manual' }] })
      }),
      http.get(PROPS, () => {
        calls.push('GET props')
        return HttpResponse.json({ properties: [{ key: 'mpn', value: 'M1', source: 'manual' }] })
      }),
    )
    const only = await runTool(tool('filament_type_upsert'), { type_id: ROW, properties: { mpn: 'M1' } }, ctx())
    expect(firstText(only)).toMatchObject({ id: ROW, code: 'PLA-RED', properties: [{ key: 'mpn' }] })
    expect(calls).toEqual(['PATCH props {"set":{"mpn":"M1"}}', 'GET type'])
    calls.length = 0
    await runTool(tool('filament_type_upsert'), { type_id: ROW, fields: { note: 'n' }, properties: { mpn: 'M1' } }, ctx())
    expect(calls).toEqual(['PATCH type {"note":"n","properties":{"mpn":"M1"}}', 'GET props'])
    expect(tool('filament_type_upsert').summarize({ type_id: ROW, properties: { mpn: 'M1', td: null } })).toBe(
      `Edit spool type ${ROW}; properties: set mpn; remove td`,
    )
    calls.length = 0
    await runTool(tool('filament_type_upsert'), { type_id: ROW, archived: true, properties: { mpn: 'M1' } }, ctx())
    expect(calls).toEqual(['PATCH type {"archived":true,"properties":{"mpn":"M1"}}', 'GET props'])
  })

  it('answers a refused properties-only edit with the API detail and no "was saved" lead (#7026)', async () => {
    const refusal = () =>
      HttpResponse.json(
        { type: 'https://scadbuddy.dev/problems/entities/bad_property_value', title: 'Bad property value', status: 422, detail: 'td: not a number' },
        { status: 422 },
      )
    server.use(
      http.patch(`${BACKEND}/api/v1/entities/${encodeURIComponent(`spool_type:${ROW}`)}/properties`, refusal),
      http.patch(`${BACKEND}/api/v1/entities/${encodeURIComponent(`spool:${ROW}`)}/properties`, refusal),
    )
    for (const [name, args] of [
      ['filament_type_upsert', { type_id: ROW, properties: { td: 'thick' } }],
      ['filament_spool_upsert', { spool_id: ROW, properties: { td: 'thick' } }],
    ] as const) {
      const result = await runTool(tool(name), args, ctx())
      expect(result.isError, name).toBe(true)
      const text = String(firstText(result))
      expect(text, name).toContain('td: not a number')
      expect(text, name).not.toContain('was saved')
    }
  })

  it('says the properties were set when the read after a properties-only edit fails (#7379)', async () => {
    server.use(
      http.patch(`${BACKEND}/api/v1/entities/:ref/properties`, () =>
        HttpResponse.json({ properties: [{ key: 'td', value: 1.5, source: 'manual' }] }),
      ),
      http.get(`${BACKEND}/api/v1/filament/types/${ROW}`, () => HttpResponse.json({ detail: 'down' }, { status: 503 })),
      http.get(`${BACKEND}/api/v1/filament/spools/${ROW}`, () => HttpResponse.json({ detail: 'down' }, { status: 503 })),
    )
    for (const [name, args] of [
      ['filament_type_upsert', { type_id: ROW, properties: { td: 1.5 } }],
      ['filament_spool_upsert', { spool_id: ROW, properties: { td: 1.5 } }],
    ] as const) {
      const result = await runTool(tool(name), args, ctx())
      expect(result.isError, name).toBe(true)
      expect(String(firstText(result)), name).toContain('the properties were set')
    }
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

describe('filament type 3DFP preset fetch tools (#6625)', () => {
  const FETCH = `${TYPE}/profile-fetch`
  const RUN = {
    id: '8d6f3c1e-0000-4000-8000-0000000066a1',
    type_id: ROW,
    tdfp_id: 1234,
    targets: [{ printer_model: 'H2C', nozzles: ['0.4'] }],
    status: 'running',
    missing: [],
    k_values: 0,
    notes: [],
    error: null,
    started_at: '2026-11-18T00:00:00Z',
    finished_at: null,
  }

  it.each([
    ['filament_type_fetch_profiles', 'outward', true, ['POST /api/v1/filament/types/{type_id}/profile-fetch']],
    // R-PR2: its read can end a stale run and cancel the run's workflow, so it is a write;
    // only `outward` is gated.
    ['filament_type_profile_fetch', 'write', false, ['GET /api/v1/filament/types/{type_id}/profile-fetch']],
  ])('%s is %s tier, gated %s, on its route', (name, risk, gated, routes) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(gated)
    expect(tool(name).routes).toEqual(routes)
  })

  it.each(['filament_type_fetch_profiles', 'filament_type_profile_fetch'])('%s takes a uuid type_id', (name) => {
    expect(() => tool(name).parse({ type_id: 'not-a-uuid' })).toThrow()
    expect(() => tool(name).parse({})).toThrow()
    expect(() => tool(name).parse({ type_id: ROW })).not.toThrow()
  })

  it('says the start reaches 3DFP and Bambuddy, and the upsert takes a tdfp_id', () => {
    const start = tool('filament_type_fetch_profiles').description
    expect(start).not.toContain('Writes ScadBuddy only')
    expect(start).toContain('Bambuddy')
    expect(start).toContain('3dfilamentprofiles.com')
    expect(tool('filament_type_upsert').description).toContain('tdfp_id')
    expect(start).toMatch(/Approved first\./)
  })

  it('takes the drying values in the upsert fields (#6720)', () => {
    const upsert = (fields: unknown) => tool('filament_type_upsert').parse({ fields })
    expect(upsert({ drying_temp_c: 55, drying_hours: '8' }).fields).toEqual({ drying_temp_c: 55, drying_hours: '8' })
    expect(upsert({ drying_temp_c: null, drying_hours: null }).fields).toEqual({ drying_temp_c: null, drying_hours: null })
    for (const bad of [-1, 1.5, 1001]) expect(() => upsert({ drying_temp_c: bad })).toThrow()
    expect(tool('filament_type_upsert').description).toContain('drying_hours')
  })

  it('records a drying with the spool defaults for what it leaves out (#6720)', () => {
    const parsed = tool('filament_spool_add_event').parse({ spool_id: ROW, kind: 'dried' })
    expect(parsed).not.toHaveProperty('temp_c')
    expect(parsed).not.toHaveProperty('hours')
    expect(tool('filament_spool_add_event').parse({ spool_id: ROW, kind: 'dried', temp_c: null }).temp_c).toBeNull()
    expect(tool('filament_spool_add_event').description).toContain('drying_defaults')
  })

  it("names a spool's drying defaults and its own drying values (#6856)", () => {
    const get = tool('filament_spool_get').description
    for (const word of ['drying_defaults', 'temp_from', 'hours_from']) expect(get).toContain(word)
    expect(tool('filament_spool_upsert').description).toContain('drying_temp_c')
    expect(tool('filament_type_fill_from_profile').description).toContain('earlier printer calibration')
  })

  it('bounds tdfp_id in the upsert fields to a positive integer below 2^31, or null', () => {
    const upsert = (tdfp_id: unknown) => tool('filament_type_upsert').parse({ fields: { tdfp_id } })
    for (const bad of [0, -1, 1.5, 2147483648, '12']) expect(() => upsert(bad)).toThrow()
    for (const good of [1, 2147483647, null]) expect(() => upsert(good)).not.toThrow()
    expect(() => tool('filament_type_upsert').parse({ fields: { code: 'X' } })).not.toThrow()
    expect(tool('filament_type_upsert').description).toContain('a positive integer below 2^31')
  })

  it('starts a fetch with POST and answers the run, its status and whether it joined', async () => {
    const calls: string[] = []
    server.use(
      http.post(FETCH, ({ request }) => {
        calls.push(request.method)
        return HttpResponse.json({ run: RUN, joined: true }, { status: 202 })
      }),
    )
    const started = await tool('filament_type_fetch_profiles').execute(
      tool('filament_type_fetch_profiles').parse({ type_id: ROW }),
      ctx('outward'),
    )
    expect(started.isError).toBeFalsy()
    expect(calls).toEqual(['POST'])
    const answer = firstText(started) as { run: { id: string; status: string }; joined: boolean }
    expect(answer.run.id).toBe(RUN.id)
    expect(answer.run.status).toBe('running')
    expect(answer.joined).toBe(true)
  })

  it("passes the backend's reason when a fetch cannot start", async () => {
    server.use(
      http.post(FETCH, () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/no_tdfp_id', title: 'No 3DFP id', detail: 'the type has no tdfp_id' },
          { status: 422 },
        ),
      ),
    )
    const start = tool('filament_type_fetch_profiles')
    await expect(start.execute(start.parse({ type_id: ROW }), ctx('outward'))).rejects.toThrow(/no tdfp_id/)
  })

  it('reads the last fetch and the gaps', async () => {
    const gaps = [{ printer_model: 'H2C', nozzle_diameter: '0.2', reason: 'the dialog offers no 0.2 mm nozzle' }]
    server.use(http.get(FETCH, () => HttpResponse.json({ run: RUN, gaps })))
    const read = await runTool(tool('filament_type_profile_fetch'), { type_id: ROW }, ctx())
    expect(firstText(read)).toEqual(withUiPaths({ run: RUN, gaps }))
  })

  it('reads a type that never had a fetch', async () => {
    server.use(http.get(FETCH, () => HttpResponse.json({ run: null, gaps: [] })))
    const read = await runTool(tool('filament_type_profile_fetch'), { type_id: ROW }, ctx())
    expect(firstText(read)).toEqual({ run: null, gaps: [] })
  })
})

describe('3DFP search and create tools (#7025)', () => {
  const SEARCH = `${BACKEND}/api/v1/filament/tdfp/search`
  const CREATE = `${BACKEND}/api/v1/filament/tdfp/filaments`
  const CANDIDATE = {
    tdfp_id: 38537,
    brand: 'SUNLU',
    material: 'PLA+/Pro',
    material_type: 'Silk',
    name: 'BRONZE (250g)',
    hexes: ['#779E7C'],
    nominal_weight_g: 250,
  }
  const ASK = { brand: 'SUNLU', material: 'PLA+/Pro', material_type: 'Silk', colour: 'Copper', hexes: ['#B87333'] }

  it.each([
    ['filament_tdfp_search', 'read', false, ['POST /api/v1/filament/tdfp/search']],
    ['filament_tdfp_create', 'outward', true, ['POST /api/v1/filament/tdfp/filaments']],
  ])('%s is %s tier, gated %s, on its route', (name, risk, gated, routes) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(gated)
    expect(tool(name).routes).toEqual(routes)
  })

  it('searches with the brand, material, type and colour and answers the candidates', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(SEARCH, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ query: 'SUNLU Silk Bronze', candidates: [CANDIDATE] })
      }),
    )
    const ask = { brand: 'SUNLU', material: 'PLA+/Pro', material_type: 'Silk', colour: 'Bronze' }
    const found = await runTool(tool('filament_tdfp_search'), ask, ctx())
    expect(bodies).toEqual([ask])
    expect(firstText(found)).toEqual({ query: 'SUNLU Silk Bronze', candidates: [CANDIDATE] })
  })

  it('needs a brand to search', () => {
    expect(() => tool('filament_tdfp_search').parse({ colour: 'Bronze' })).toThrow()
  })

  it('creates with a request key and answers what the site did', async () => {
    const seen: { key: string | null; body: unknown }[] = []
    server.use(
      http.post(CREATE, async ({ request }) => {
        seen.push({ key: request.headers.get('Idempotency-Key'), body: await request.json() })
        return HttpResponse.json({
          create: { status: 'created', tdfp_id: 40000, duplicates: [], duplicate_text: '', likely: [], sent: ['gtin'], not_sent: [] },
          type_id: ROW,
          type_tdfp_id_set: true,
        })
      }),
    )
    const create = tool('filament_tdfp_create')
    const made = await create.execute(create.parse({ ...ASK, gtin: '06975373321234', type_id: ROW }), ctx('outward'))
    expect(made.isError).toBeFalsy()
    expect(seen[0]?.key).toMatch(/^[0-9a-f]{32,64}$/)
    expect(seen[0]?.body).toEqual({ ...ASK, gtin: '06975373321234', type_id: ROW })
    expect((firstText(made) as { create: { tdfp_id: number } }).create.tdfp_id).toBe(40000)
  })

  it('checks the hexes and identifiers before it asks', () => {
    const create = tool('filament_tdfp_create')
    for (const bad of [{ hexes: [] }, { hexes: ['red'] }, { hexes: Array(5).fill('#000000') }, { gtin: '12' }]) {
      expect(() => create.parse({ ...ASK, ...bad })).toThrow()
    }
    expect(create.description).toMatch(/Approved first\./)
    expect(create.description).toContain('not_duplicates')
  })
})

const SPOOLS = `${BACKEND}/api/v1/filament/spools`
const SPOOL = `${SPOOLS}/${ROW}`

describe('filament spool tools', () => {
  it.each([
    ['filament_spool_list', 'read'],
    ['filament_spool_get', 'read'],
    ['filament_spool_events', 'read'],
    ['filament_tag_list', 'read'],
    ['filament_tag_get', 'read'],
    ['filament_tag_backups', 'read'],
    ['filament_ledger_list', 'read'],
    ['filament_summary', 'read'],
    ['filament_spool_upsert', 'write'],
    ['filament_spool_set_presets', 'write'],
    ['filament_spool_set_k_values', 'write'],
    ['filament_spool_archive', 'write'],
    ['filament_spool_closing', 'read'],
    ['filament_spool_delete', 'write'],
    ['filament_spool_bind_tag', 'write'],
    ['filament_tag_unbind', 'write'],
    ['filament_spool_add_event', 'write'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('lists the backups of a tag from its path (#7273)', async () => {
    const backups = [{ id: ROW, tag_id: ROW, uid: '04AA', records: [], reason: 'spool_write' }]
    const queries: string[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/filament/tags/${ROW}/backups`, ({ request }) => {
        queries.push(new URL(request.url).search)
        return HttpResponse.json(backups)
      }),
    )
    const result = await runTool(tool('filament_tag_backups'), { tag_id: ROW }, ctx())
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toEqual(backups)
    await runTool(tool('filament_tag_backups'), { tag_id: ROW, limit: 2, before: ROW }, ctx())
    expect(queries).toEqual(['?limit=50', `?limit=2&before=${ROW}`])
    expect(() => tool('filament_tag_backups').parse({ tag_id: ROW, limit: 201 })).toThrow()
    expect(() => tool('filament_tag_backups').parse({ tag_id: 'x' })).toThrow()
  })

  it('resets the used counter of spools with one POST (#7149)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${SPOOLS}/reset-consumed`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ reset: 1, not_found: [] })
      }),
    )
    const result = await runTool(tool('filament_spools_reset_consumed'), { spool_ids: [ROW] }, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ spool_ids: [ROW] }])
    expect(firstText(result)).toEqual({ reset: 1, not_found: [] })
    expect(() => tool('filament_spools_reset_consumed').parse({ spool_ids: [] })).toThrow()
    expect(tool('filament_spools_reset_consumed').risk).toBe('write')
    expect(tool('filament_spool_add_event').parse({ spool_id: ROW, kind: 'consumed_reset' }).kind).toBe('consumed_reset')
  })

  it('edits several spools with one POST, all or none (#7151, R-7151-1)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${SPOOLS}/bulk-update`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ changed: [ROW], unchanged: [], not_found: [] })
      }),
    )
    const input = { spool_ids: [ROW], patch: { note: 'June order', cost_per_kg: null } }
    const result = await runTool(tool('filament_spools_bulk_update'), input, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([input])
    expect(firstText(result)).toEqual({ changed: [ROW], unchanged: [], not_found: [] })
    expect(() => tool('filament_spools_bulk_update').parse({ spool_ids: [], patch: { note: 'x' } })).toThrow()
    expect(tool('filament_spools_bulk_update').risk).toBe('write')
  })

  it.each([
    [true, 'bulk-archive'],
    [false, 'bulk-restore'],
  ])('archived %s sends one POST to %s (#7151, R-7151-2)', async (archived, route) => {
    const bodies: unknown[] = []
    const answer = { changed: [ROW], unchanged: [], not_found: [] }
    server.use(
      http.post(`${SPOOLS}/${route}`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(answer)
      }),
    )
    const result = await runTool(tool('filament_spools_bulk_archive'), { spool_ids: [ROW], archived }, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ spool_ids: [ROW] }])
    expect(firstText(result)).toEqual(answer)
    expect(tool('filament_spools_bulk_archive').risk).toBe('write')
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
    expect(firstText(listed)).toEqual([{ id: ROW, ui_path: `/filament/spools/${ROW}` }])
    expect(firstText(await runTool(tool('filament_spool_get'), { spool_id: ROW }, ctx()))).toEqual({ id: ROW, ui_path: `/filament/spools/${ROW}` })
    const events = await runTool(tool('filament_spool_events'), { spool_id: ROW }, ctx())
    expect(firstText(events)).toEqual([{ kind: 'created' }])
  })

  it('reads the tags, a page of the ledger and the summary', async () => {
    const queries: string[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/filament/tags`, ({ request }) => {
        queries.push(new URL(request.url).search)
        return HttpResponse.json([{ id: ROW }])
      }),
      http.get(`${BACKEND}/api/v1/filament/tags/${ROW}`, () => HttpResponse.json({ id: ROW, history: [] })),
      http.get(`${BACKEND}/api/v1/filament/events`, ({ request }) => {
        queries.push(new URL(request.url).search)
        return HttpResponse.json({ events: [], next_cursor: null })
      }),
      http.get(`${BACKEND}/api/v1/filament/summary`, () => HttpResponse.json({ spools: 2 })),
    )
    const tags = await runTool(tool('filament_tag_list'), { include_unbound: true }, ctx())
    expect(firstText(tags)).toEqual([{ id: ROW }])
    expect(firstText(await runTool(tool('filament_tag_get'), { tag_id: ROW }, ctx()))).toEqual({ id: ROW, history: [] })
    const page = await runTool(tool('filament_ledger_list'), { spool_id: ROW, cursor: 'abc', limit: 2 }, ctx())
    expect(firstText(page)).toEqual({ events: [], next_cursor: null })
    expect(firstText(await runTool(tool('filament_summary'), {}, ctx()))).toEqual({ spools: 2 })
    expect(queries).toEqual(['?include_unbound=true', `?spool_id=${ROW}&cursor=abc&limit=2`])
  })

  it('reads the ledger between a since and an until', async () => {
    const queries: string[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/filament/events`, ({ request }) => {
        queries.push(new URL(request.url).search)
        return HttpResponse.json({ events: [], next_cursor: null })
      }),
    )
    await runTool(tool('filament_ledger_list'), { since: '2026-11-01T00:00:00Z', until: '2026-11-02T00:00:00Z', limit: 5 }, ctx())
    expect(queries).toEqual(['?since=2026-11-01T00%3A00%3A00Z&until=2026-11-02T00%3A00%3A00Z&limit=5'])
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

  it('archives a spool with a reason and a final weighing (#7684)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${SPOOL}/archive`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: ROW, archived_at: '2026-11-25T12:00:00Z' })
      }),
    )
    const call = { spool_id: ROW, reason: 'finished', final_reading_g: 230 }
    expect((await runTool(tool('filament_spool_archive'), call, ctx())).isError).toBeFalsy()
    await runTool(tool('filament_spool_archive'), { spool_id: ROW, reason: 'damaged', final_grams: 12 }, ctx())
    expect(bodies).toEqual([
      { archived: true, reason: 'finished', final_reading_g: 230 },
      { archived: true, reason: 'damaged', final_grams: 12 },
    ])
    expect(() => tool('filament_spool_archive').parse({ spool_id: ROW, reason: 'lost' })).toThrow()
  })

  it("previews a spool's closing with the query it was given (#7684)", async () => {
    const seen: string[] = []
    server.use(
      http.get(`${SPOOL}/closing`, ({ request }) => {
        seen.push(new URL(request.url).search)
        return HttpResponse.json({ closing: { reason: 'finished', drift_g: -3 }, notices: ['filament_left'] })
      }),
    )
    const out = await runTool(tool('filament_spool_closing'), { spool_id: ROW, reason: 'finished', final_reading_g: 230 }, ctx())
    expect(firstText(out)).toEqual({ closing: { reason: 'finished', drift_g: -3 }, notices: ['filament_left'] })
    await runTool(tool('filament_spool_closing'), { spool_id: ROW }, ctx())
    expect(seen).toEqual(['?reason=finished&final_reading_g=230', ''])
    expect(() => tool('filament_spool_closing').parse({ spool_id: ROW, reason: 'lost' })).toThrow()
  })

  it("sends a spool's properties inside its PATCH, and says when reading them back fails (#7703)", async () => {
    const calls: string[] = []
    const PROPS = `${BACKEND}/api/v1/entities/${encodeURIComponent(`spool:${ROW}`)}/properties`
    let down = false
    server.use(
      http.patch(SPOOL, async ({ request }) => {
        calls.push(`PATCH spool ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ROW })
      }),
      http.get(PROPS, () => {
        calls.push('GET props')
        if (down) return HttpResponse.json({ detail: 'down' }, { status: 503 })
        return HttpResponse.json({ properties: [{ key: 'lot_number', value: 'L-2207', source: 'manual' }] })
      }),
    )
    const edited = await runTool(tool('filament_spool_upsert'), { spool_id: ROW, fields: { note: 'n' }, properties: { lot_number: 'L-2207' } }, ctx())
    expect(firstText(edited)).toMatchObject({ id: ROW, properties: [{ key: 'lot_number', value: 'L-2207' }] })
    expect(calls).toEqual(['PATCH spool {"note":"n","properties":{"lot_number":"L-2207"}}', 'GET props'])
    down = true
    const unread = await runTool(tool('filament_spool_upsert'), { spool_id: ROW, fields: { note: 'n' }, properties: { lot_number: 'x' } }, ctx())
    expect(unread.isError).toBe(true)
    expect(String(firstText(unread))).toContain(`spool ${ROW} was saved with its properties`)
  })

  it('sends no spool PATCH for a properties-only edit (#7026)', async () => {
    const calls: string[] = []
    server.use(
      http.patch(SPOOL, () => {
        calls.push('PATCH spool')
        return HttpResponse.json({ id: ROW })
      }),
      http.get(SPOOL, () => {
        calls.push('GET spool')
        return HttpResponse.json({ id: ROW, number: 4 })
      }),
      http.patch(`${BACKEND}/api/v1/entities/${encodeURIComponent(`spool:${ROW}`)}/properties`, async ({ request }) => {
        calls.push(`PATCH props ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ properties: [] })
      }),
    )
    const only = await runTool(tool('filament_spool_upsert'), { spool_id: ROW, properties: { lot_number: null } }, ctx())
    expect(firstText(only)).toMatchObject({ id: ROW, number: 4, properties: [] })
    expect(calls).toEqual(['PATCH props {"set":{"lot_number":null}}', 'GET spool'])
    expect(tool('filament_spool_upsert').summarize({ spool_id: ROW, properties: { lot_number: null } })).toBe(
      `Edit spool ${ROW}; properties: remove lot_number`,
    )
  })

  it("replaces a spool's own presets and K values", async () => {
    const calls: unknown[] = []
    server.use(
      http.put(`${SPOOL}/presets`, async ({ request }) => {
        calls.push(['presets', await request.json()])
        return HttpResponse.json([])
      }),
      http.put(`${SPOOL}/k-values`, async ({ request }) => {
        calls.push(['k', await request.json()])
        return HttpResponse.json([])
      }),
    )
    const presets = [{ printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'P1' }]
    const k_values = [{ printer_id: ROW, extruder: 0, nozzle_diameter: '0.4', k_value: '0.02' }]
    expect((await runTool(tool('filament_spool_set_presets'), { spool_id: ROW, presets }, ctx())).isError).toBeFalsy()
    expect((await runTool(tool('filament_spool_set_k_values'), { spool_id: ROW, k_values }, ctx())).isError).toBeFalsy()
    expect(calls).toEqual([['presets', presets], ['k', k_values]])
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

describe('filament_spool_add_event with a new spool reading (#7280)', () => {
  const OFFER = {
    spool_id: ROW,
    insert_type_id: ROW,
    insert_name: 'Bambu insert',
    reading_g: 1150,
    label_weight_g: 1000,
    derived_g: 150,
    new_empty_weight_g: 150,
    current_empty_weight_g: 200,
    current_basis: 'placeholder',
    placeholder: true,
    sample_count: 1,
    checked_by_default: true,
  }
  const CHECK = { offer: OFFER, reason: null, insert_type_id: ROW, empty_weight_g: 200, filament_g: 950 }

  function mock(check: Record<string, unknown>, order: string[], bodies: unknown[]) {
    server.use(
      http.get(`${SPOOL}/insert-weight-offer`, ({ request }) => {
        order.push(`offer ${new URL(request.url).searchParams.get('reading_g')}`)
        return HttpResponse.json(check)
      }),
      http.post(`${SPOOL}/insert-weight`, async ({ request }) => {
        order.push('accept')
        bodies.push(await request.json())
        return HttpResponse.json({ insert_type: {}, empty_weight_g: 150, sample_count: 1, filament_g: 1000 })
      }),
      http.post(`${SPOOL}/events`, async ({ request }) => {
        order.push('event')
        bodies.push(await request.json())
        return HttpResponse.json({ kind: 'weighed' }, { status: 201 })
      }),
    )
  }

  it('with an offer and no decision, reads the offer and books nothing', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    mock(CHECK, order, bodies)
    const res = await runTool(tool('filament_spool_add_event'), { spool_id: ROW, kind: 'weighed', reading_g: 1150 }, ctx())
    expect(res.isError).toBeFalsy()
    expect(order).toEqual(['offer 1150'])
    expect(bodies).toEqual([])
    const out = firstText(res) as Record<string, unknown>
    expect(out.event).toBeNull()
    expect(out.filament_g).toBeNull()
    expect((out.insert_weight as { offer: unknown }).offer).toMatchObject(OFFER)
    expect(out.insert_weight_accepted).toBeNull()
    expect(JSON.stringify(out)).toContain('accept_insert_weight')
  })

  it('declines the offer and books the check filament', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    mock(CHECK, order, bodies)
    const res = await runTool(
      tool('filament_spool_add_event'),
      { spool_id: ROW, kind: 'weighed', reading_g: 1150, accept_insert_weight: false },
      ctx(),
    )
    expect(res.isError).toBeFalsy()
    expect(order).toEqual(['offer 1150', 'event'])
    expect(bodies).toEqual([{ kind: 'weighed', source: 'person', reading_g: 1150, grams: 950 }])
    const out = firstText(res) as Record<string, unknown>
    expect(out.filament_g).toBe(950)
    expect(out.event).toEqual({ kind: 'weighed' })
    expect(out.insert_weight_accepted).toBeNull()
  })

  it('with no offer, books the check filament at once', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    mock({ offer: null, reason: 'used', insert_type_id: ROW, empty_weight_g: 200, filament_g: 950 }, order, bodies)
    const res = await runTool(tool('filament_spool_add_event'), { spool_id: ROW, kind: 'weighed', reading_g: 1150 }, ctx())
    expect(res.isError).toBeFalsy()
    expect(order).toEqual(['offer 1150', 'event'])
    expect(bodies).toEqual([{ kind: 'weighed', source: 'person', reading_g: 1150, grams: 950 }])
    expect((firstText(res) as Record<string, unknown>).filament_g).toBe(950)
  })

  const BELOW = {
    offer: null,
    reason: 'below_label',
    insert_type_id: ROW,
    empty_weight_g: 200,
    filament_g: 700,
    shortfall_g: 100,
    resolutions: [
      { kind: 'tared', filament_g: 900, new_label_weight_g: null },
      { kind: 'short', filament_g: 700, new_label_weight_g: null },
      { kind: 'label_wrong', filament_g: 700, new_label_weight_g: 700 },
    ],
    default: 'tared',
  }

  it('with a reading under the label and no resolution, names the choices and books nothing (#7683)', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    mock(BELOW, order, bodies)
    const res = await runTool(tool('filament_spool_add_event'), { spool_id: ROW, kind: 'weighed', reading_g: 900 }, ctx())
    expect(res.isError).toBeFalsy()
    expect(order).toEqual(['offer 900'])
    expect(bodies).toEqual([])
    const out = firstText(res) as Record<string, unknown>
    expect(out.event).toBeNull()
    expect(out.below_label).toMatchObject({ shortfall_g: 100, default: 'tared' })
    expect(String(out.next)).toContain('resolution')
  })

  it('with a resolution, posts it with the reading alone (#7683)', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    mock(BELOW, order, bodies)
    const res = await runTool(
      tool('filament_spool_add_event'),
      { spool_id: ROW, kind: 'weighed', source: 'scale', reading_g: 900, resolution: 'short' },
      ctx(),
    )
    expect(res.isError).toBeFalsy()
    expect(order).toEqual(['event'])
    expect(bodies).toEqual([{ kind: 'weighed', source: 'scale', reading_g: 900, resolution: 'short' }])
    expect((firstText(res) as Record<string, unknown>).event).toEqual({ kind: 'weighed' })
    expect(tool('filament_spool_add_event').description).toContain('below_label')
  })

  it('sends the reading beside the grams, so the event keeps its breakdown (#7682)', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    mock({ offer: null, reason: 'used', insert_type_id: ROW, empty_weight_g: 200, filament_g: 980 }, order, bodies)
    const res = await runTool(tool('filament_spool_add_event'), { spool_id: ROW, kind: 'weighed', reading_g: 1180 }, ctx())
    expect(res.isError).toBeFalsy()
    expect(bodies).toEqual([{ kind: 'weighed', source: 'person', reading_g: 1180, grams: 980 }])
    expect(tool('filament_spool_add_event').description).toContain('drift')
  })

  it('says a retry is safe when the booking fails after the accept', async () => {
    const order: string[] = []
    mock(CHECK, order, [])
    server.use(http.post(`${SPOOL}/events`, () => HttpResponse.json({ detail: 'boom' }, { status: 500 })))
    const res = await runTool(
      tool('filament_spool_add_event'),
      { spool_id: ROW, kind: 'weighed', reading_g: 1150, accept_insert_weight: true, new_empty_weight_g: 150 },
      ctx(),
    )
    expect(res.isError).toBe(true)
    const text = JSON.stringify(res)
    expect(text).toContain("insert's empty weight was already set")
    expect(text).toContain('retry with the same reading')
  })

  it('accepts before it books, with the accepted filament', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    mock(CHECK, order, bodies)
    const res = await runTool(
      tool('filament_spool_add_event'),
      { spool_id: ROW, kind: 'weighed', reading_g: 1150, accept_insert_weight: true, new_empty_weight_g: 150 },
      ctx(),
    )
    expect(res.isError).toBeFalsy()
    expect(order).toEqual(['offer 1150', 'accept', 'event'])
    // #7450-2: the accept names the offer's weight, so a mean that moved meanwhile is refused.
    expect(bodies).toEqual([{ reading_g: 1150, new_empty_weight_g: 150 }, { kind: 'weighed', source: 'person', reading_g: 1150, grams: 1000 }])
    const out = firstText(res) as Record<string, { empty_weight_g: number }>
    expect(out.insert_weight_accepted?.empty_weight_g).toBe(150)
  })

  it('accepts the weight the person approved, not a newer check (#7450-2)', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    // The person approved 160 g; another spool's accept moved the mean to 155 g before this call.
    mock({ ...CHECK, offer: { ...OFFER, new_empty_weight_g: 155 } }, order, bodies)
    await runTool(
      tool('filament_spool_add_event'),
      { spool_id: ROW, kind: 'weighed', reading_g: 1150, accept_insert_weight: true, new_empty_weight_g: 160 },
      ctx(),
    )
    expect(bodies[0]).toEqual({ reading_g: 1150, new_empty_weight_g: 160 })
  })

  it('books nothing when accept has no offer', async () => {
    const order: string[] = []
    mock({ offer: null, reason: 'used', insert_type_id: ROW, empty_weight_g: 200, filament_g: 950 }, order, [])
    const res = await runTool(
      tool('filament_spool_add_event'),
      { spool_id: ROW, kind: 'weighed', reading_g: 1150, accept_insert_weight: true, new_empty_weight_g: 150 },
      ctx(),
    )
    expect(res.isError).toBe(true)
    expect(JSON.stringify(res)).toContain('used')
    expect(order).toEqual(['offer 1150'])
  })

  it('names a check as well as a booking in its approval text (#7451-2)', () => {
    const summarize = tool('filament_spool_add_event').summarize
    const base = { spool_id: ROW, kind: 'weighed', source: 'person' } as const
    expect(summarize({ ...base, reading_g: 1150 })).toBe(`Check, or record, a reading of 1150 g on spool ${ROW}`)
    expect(summarize({ ...base, reading_g: 1150, accept_insert_weight: false })).toBe(`Record a reading of 1150 g on spool ${ROW}`)
    expect(summarize({ ...base, reading_g: 1150, accept_insert_weight: true })).toBe(`Record a reading of 1150 g on spool ${ROW} and take its insert weight`)
  })

  it('refuses bad combinations before any call', async () => {
    const order: string[] = []
    mock(CHECK, order, [])
    const run = (input: Record<string, unknown>) => runTool(tool('filament_spool_add_event'), { spool_id: ROW, ...input }, ctx())
    expect((await run({ kind: 'weighed', reading_g: 1150, grams: 900 })).isError).toBe(true)
    expect((await run({ kind: 'dried', reading_g: 1150 })).isError).toBe(true)
    expect((await run({ kind: 'dried', accept_insert_weight: true })).isError).toBe(true)
    expect((await run({ kind: 'weighed', grams: 900, accept_insert_weight: true })).isError).toBe(true)
    // #7450-2: an accept names the weight the person approved.
    expect((await run({ kind: 'weighed', reading_g: 1150, accept_insert_weight: true })).isError).toBe(true)
    expect((await run({ kind: 'weighed', reading_g: 1150, accept_insert_weight: false, new_empty_weight_g: 150 })).isError).toBe(true)
    expect(order).toEqual([])
  })

  it('refuses a reading when no empty weight is known', async () => {
    const order: string[] = []
    mock({ offer: null, reason: 'no_insert', insert_type_id: null, empty_weight_g: null, filament_g: null }, order, [])
    const res = await runTool(tool('filament_spool_add_event'), { spool_id: ROW, kind: 'weighed', reading_g: 1150 }, ctx())
    expect(res.isError).toBe(true)
    expect(JSON.stringify(res)).toContain('pass grams')
    expect(order).toEqual(['offer 1150'])
  })

  it('books grams alone as before', async () => {
    const order: string[] = []
    const bodies: unknown[] = []
    mock(CHECK, order, bodies)
    const res = await runTool(tool('filament_spool_add_event'), { spool_id: ROW, kind: 'weighed', grams: 900 }, ctx())
    expect(res.isError).toBeFalsy()
    expect(order).toEqual(['event'])
    expect(firstText(res)).toEqual({ kind: 'weighed' })
  })
})

const LOCATIONS = `${BACKEND}/api/v1/filament/locations`

describe('filament location tools', () => {
  it.each([
    ['filament_location_list', 'read'],
    ['filament_code_resolve', 'read'],
    ['filament_location_get', 'read'],
    ['filament_printer_labels', 'read'],
    ['filament_label_templates', 'read'],
    ['filament_label_sheet_upsert', 'write'],
    ['filament_label_sheet_delete', 'write'],
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
    expect(firstText(listed)).toEqual([{ id: ROW, ui_path: `/filament/locations/${ROW}`, children: [] }])
    const fields = { name: 'Shelf A', kind_id: ROW }
    expect((await runTool(tool('filament_location_upsert'), { fields }, ctx())).isError).toBeFalsy()
    const edit = { location_id: ROW, fields: { note: null }, archived: true }
    expect((await runTool(tool('filament_location_upsert'), edit, ctx())).isError).toBeFalsy()
    expect(calls).toEqual([['add', fields], ['edit', { note: null, archived: true }]])
    expect((await runTool(tool('filament_location_upsert'), { archived: true }, ctx())).isError).toBe(true)
  })

  it('resolves a code, reads a place and lists printer labels', async () => {
    const bodies: unknown[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/codes/L-12`, () => HttpResponse.json({ kind: 'location', id: ROW })),
      http.get(`${LOCATIONS}/${ROW}`, () => HttpResponse.json({ id: ROW, code: 'L-12' })),
      http.post(`${BACKEND}/api/v1/filament/printer-labels`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ base_url: null, labels: [] })
      }),
    )
    expect(firstText(await runTool(tool('filament_code_resolve'), { code: 'L-12' }, ctx()))).toEqual({ kind: 'location', id: ROW })
    expect(firstText(await runTool(tool('filament_location_get'), { location_id: ROW }, ctx()))).toMatchObject({ code: 'L-12' })
    await runTool(tool('filament_printer_labels'), {}, ctx())
    await runTool(tool('filament_printer_labels'), { printer_ids: [ROW] }, ctx())
    expect(bodies).toEqual([{ printer_ids: null }, { printer_ids: [ROW] }])
  })

  it('saves a label sheet: POST without a sheet_id, PUT with one (#7216)', async () => {
    const SHEETS = `${BACKEND}/api/v1/filament/label-sheets`
    const layout = { page: 'a4', width_mm: 70, height_mm: 37, cols: 3, rows: 8, top_mm: 0, left_mm: 0 }
    const calls: unknown[] = []
    server.use(
      http.post(SHEETS, async ({ request }) => {
        calls.push(['post', await request.json()])
        return HttpResponse.json({ id: ROW, name: 'Avery', layout }, { status: 201 })
      }),
      http.put(`${SHEETS}/${ROW}`, async ({ request }) => {
        calls.push(['put', await request.json()])
        return HttpResponse.json({ id: ROW, name: 'Avery 2', layout })
      }),
    )
    const added = await runTool(tool('filament_label_sheet_upsert'), { name: 'Avery', layout }, ctx())
    expect(firstText(added)).toEqual({ id: ROW, name: 'Avery', layout })
    const edited = await runTool(tool('filament_label_sheet_upsert'), { sheet_id: ROW, name: 'Avery 2', layout }, ctx())
    expect(firstText(edited)).toEqual({ id: ROW, name: 'Avery 2', layout })
    const filled = { ...layout, col_gap_mm: 0, row_gap_mm: 0 }
    expect(calls).toEqual([['post', { name: 'Avery', layout: filled }], ['put', { name: 'Avery 2', layout: filled }]])
    expect(tool('filament_label_sheet_upsert').summarize?.({ name: 'Avery', layout } as never)).toBe('Save label sheet Avery')
  })

  it('surfaces a label sheet 409 and 404 answer, and refuses an out-of-bound input before a request (#7216)', async () => {
    const SHEETS = `${BACKEND}/api/v1/filament/label-sheets`
    const layout = { page: 'a4', width_mm: 70, height_mm: 37, cols: 3, rows: 8, top_mm: 0, left_mm: 0 }
    const sent: unknown[] = []
    server.use(
      http.post(SHEETS, async ({ request }) => {
        sent.push(await request.json())
        return HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: 'label_sheet_name_taken' },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
      http.put(`${SHEETS}/${ROW}`, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Not Found', status: 404, detail: 'label_sheet_not_found' },
          { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const taken = await runTool(tool('filament_label_sheet_upsert'), { name: 'Avery', layout }, ctx())
    expect(taken.isError).toBe(true)
    expect(JSON.stringify(taken.content)).toContain('label_sheet_name_taken')
    const missing = await runTool(tool('filament_label_sheet_upsert'), { sheet_id: ROW, name: 'Avery', layout }, ctx())
    expect(missing.isError).toBe(true)
    expect(JSON.stringify(missing.content)).toContain('label_sheet_not_found')
    sent.length = 0
    const refused = await runTool(tool('filament_label_sheet_upsert'), { name: 'Avery', layout: { ...layout, cols: 21 } }, ctx())
    expect(refused.isError).toBe(true)
    expect(sent).toEqual([])
  })

  it('trims a label sheet name before the length check and sends it trimmed (#7216)', async () => {
    const layout = { page: 'a4', width_mm: 70, height_mm: 37, cols: 3, rows: 8, top_mm: 0, left_mm: 0 }
    const names: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/filament/label-sheets`, async ({ request }) => {
        names.push(((await request.json()) as { name: string }).name)
        return HttpResponse.json({ id: ROW, name: 'x', layout }, { status: 201 })
      }),
    )
    const name = 'a'.repeat(100)
    const ok = await runTool(tool('filament_label_sheet_upsert'), { name: `${name} `, layout }, ctx())
    expect(ok.isError).toBeFalsy()
    expect(names).toEqual([name])
    expect((await runTool(tool('filament_label_sheet_upsert'), { name: `${name}a`, layout }, ctx())).isError).toBe(true)
    expect((await runTool(tool('filament_label_sheet_upsert'), { name: '   ', layout }, ctx())).isError).toBe(true)
    // 100 code points outside the BMP are 200 UTF-16 units; zod and the backend both count 100.
    const wide = '\u{1F9F5}'.repeat(100)
    expect((await runTool(tool('filament_label_sheet_upsert'), { name: wide, layout }, ctx())).isError).toBeFalsy()
    expect(names.at(-1)).toBe(wide)
  })

  it('says a deleted sheet makes the next print a 409 (#7216)', () => {
    expect(tool('filament_label_sheet_delete').description).toContain('label_sheet_gone')
    expect(tool('filament_label_sheet_delete').description).not.toContain('default size')
  })

  it('deletes a label sheet and reports its id (#7216)', async () => {
    const calls: string[] = []
    server.use(
      http.delete(`${BACKEND}/api/v1/filament/label-sheets/${ROW}`, () => {
        calls.push('delete')
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const res = await runTool(tool('filament_label_sheet_delete'), { sheet_id: ROW }, ctx())
    expect(firstText(res)).toEqual({ deleted: ROW })
    expect(calls).toEqual(['delete'])
  })

  it('lists the label templates', async () => {
    const answer = { templates: [{ id: 'roll_50x25', name: 'Roll label 50 x 25 mm', width_mm: 50, height_mm: 25, sheet: null }], custom: { min_width_mm: 40 } }
    server.use(http.get(`${BACKEND}/api/v1/filament/label-templates`, () => HttpResponse.json(answer)))
    expect(firstText(await runTool(tool('filament_label_templates'), {}, ctx()))).toEqual(answer)
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

  it('previews a CSV import without writing, as a read tool', async () => {
    expect(tool('filament_spool_import').risk).toBe('write')
    expect(tool('filament_spool_import').gated).toBe(false)
    expect(tool('filament_spool_import_preview').risk).toBe('read')
    const preview = { rows: [{ line: 2, action: 'create', number: null, changes: {}, error: null }] }
    const sent: { body: unknown; key: string | null }[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/filament/spools/import`, async ({ request }) => {
        sent.push({ body: await request.json(), key: request.headers.get('Idempotency-Key') })
        return HttpResponse.json(preview)
      }),
    )
    const rows = [{ material: 'PLA', brand: 'Sunlu' }]
    const result = await runTool(tool('filament_spool_import_preview'), { rows }, ctx())
    expect(firstText(result)).toEqual(preview)
    expect(sent).toEqual([{ body: { rows, dry_run: true }, key: null }])
  })

  it('imports after a clean preview, with one Idempotency-Key', async () => {
    const preview = { rows: [{ line: 2, action: 'create', number: null, changes: {}, error: null }] }
    const done = { rows: [{ line: 2, action: 'create', number: 7, changes: {}, error: null }] }
    const sent: { body: { dry_run: boolean }; key: string | null }[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/filament/spools/import`, async ({ request }) => {
        const body = (await request.json()) as { dry_run: boolean }
        sent.push({ body, key: request.headers.get('Idempotency-Key') })
        return HttpResponse.json(body.dry_run ? preview : done)
      }),
    )
    const result = await runTool(tool('filament_spool_import'), { rows: [{ material: 'PLA' }] }, ctx())
    expect(firstText(result)).toEqual(done)
    expect(sent.map((s) => s.body.dry_run)).toEqual([true, false])
    expect(sent[1]?.key).toMatch(/^[0-9a-f]{32,64}$/)
  })

  it('writes nothing when the preview holds an error', async () => {
    const preview = { rows: [{ line: 2, action: 'error', number: null, changes: {}, error: 'no spool #9' }] }
    let writes = 0
    server.use(
      http.post(`${BACKEND}/api/v1/filament/spools/import`, async ({ request }) => {
        const body = (await request.json()) as { dry_run: boolean }
        if (!body.dry_run) writes += 1
        return HttpResponse.json(preview)
      }),
    )
    const result = await runTool(tool('filament_spool_import'), { rows: [{ number: '9' }] }, ctx())
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toContain('line 2: no spool #9')
    expect(writes).toBe(0)
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

describe('filament sensor and AMS weight tools (#6268)', () => {
  const SENSORS = `${BACKEND}/api/v1/filament/sensors`

  it.each([
    ['filament_sync_ams_weights', 'write'],
    ['filament_sensors_list', 'read'],
    ['filament_sensor_readings', 'read'],
    ['filament_sensor_save', 'write'],
    ['filament_sensor_archive', 'write'],
    ['filament_ha_entities', 'read'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('says in its descriptions what the sync skips, that an AMS sensor is not made by hand, and when alerts are sent', () => {
    expect(tool('filament_sync_ams_weights').description).toMatch(/locked/)
    expect(tool('filament_sync_ams_weights').description).toMatch(/printing/)
    expect(tool('filament_sync_ams_weights').description).toMatch(/RFID/)
    // #6679: the agent can name which printers had no fresh report.
    expect(tool('filament_sync_ams_weights').description).toMatch(/`stale_printers`.*`name`/)
    expect(tool('filament_sensor_save').description).toMatch(/AMS sensor cannot be made by hand/)
    // #6646: notify sends through Bambuddy, once at the start and once at the end.
    expect(tool('filament_sensor_save').description).toMatch(/starts alerting and one when it stops/)
    // #6855 review: the hold before an alert closes, and notify off stops what is queued.
    expect(tool('filament_sensor_save').description).toMatch(/after 10 minutes back inside its band/)
    expect(tool('filament_sensor_save').description).toMatch(/turning `notify` off stops/)
    expect(tool('filament_sensors_list').description).toMatch(/`last_notification`/)
  })

  it('syncs the AMS weights with one POST and no body', async () => {
    const result = { booked: 1, locked: 0, printing: 0, no_remain: 0, stale: 0, stale_printers: [], spools: [ROW] }
    let calls = 0
    server.use(
      http.post(`${BACKEND}/api/v1/filament/spools/sync-ams-weights`, () => {
        calls += 1
        return HttpResponse.json(result)
      }),
    )
    expect(firstText(await runTool(tool('filament_sync_ams_weights'), {}, ctx()))).toEqual(result)
    expect(calls).toBe(1)
  })

  it('lists sensors, of one place when asked', async () => {
    const seen: (string | null)[] = []
    server.use(
      http.get(SENSORS, ({ request }) => {
        seen.push(new URL(request.url).searchParams.get('location_id'))
        return HttpResponse.json([{ id: ROW }])
      }),
    )
    await runTool(tool('filament_sensors_list'), {}, ctx())
    await runTool(tool('filament_sensors_list'), { location_id: ROW }, ctx())
    expect(seen).toEqual([null, ROW])
  })

  it('reads a sensor history with since and limit', async () => {
    const seen: string[] = []
    server.use(
      http.get(`${SENSORS}/${ROW}/readings`, ({ request }) => {
        seen.push(new URL(request.url).search)
        return HttpResponse.json([{ at: '2026-11-18T00:00:00Z', value: 41 }])
      }),
    )
    const out = await runTool(tool('filament_sensor_readings'), { sensor_id: ROW, since: '2026-11-18T00:00:00Z', limit: 10 }, ctx())
    expect(firstText(out)).toEqual([{ at: '2026-11-18T00:00:00Z', value: 41 }])
    expect(seen).toEqual(['?since=2026-11-18T00%3A00%3A00Z&limit=10'])
    expect((await runTool(tool('filament_sensor_readings'), { sensor_id: ROW, limit: 0 }, ctx())).isError).toBe(true)
  })

  it('adds a sensor, and patches one', async () => {
    const calls: unknown[] = []
    server.use(
      http.post(SENSORS, async ({ request }) => {
        calls.push(['add', await request.json()])
        return HttpResponse.json({ id: ROW }, { status: 201 })
      }),
      http.patch(`${SENSORS}/${ROW}`, async ({ request }) => {
        calls.push(['edit', await request.json()])
        return HttpResponse.json({ id: ROW })
      }),
    )
    const fields = { location_id: ROW, source: 'home_assistant', kind: 'humidity', entity_id: 'sensor.box', name: 'Box' }
    expect((await runTool(tool('filament_sensor_save'), { fields }, ctx())).isError).toBeFalsy()
    const edit = { sensor_id: ROW, fields: { alert_above: null, notify: true } }
    expect((await runTool(tool('filament_sensor_save'), edit, ctx())).isError).toBeFalsy()
    expect(calls).toEqual([['add', fields], ['edit', { alert_above: null, notify: true }]])
    expect(tool('filament_sensor_save').summarize?.({ fields } as never)).toContain('Box')
  })

  it('archives a sensor on a 204', async () => {
    let calls = 0
    server.use(
      http.post(`${SENSORS}/${ROW}/archive`, () => {
        calls += 1
        return new HttpResponse(null, { status: 204 })
      }),
    )
    expect((await runTool(tool('filament_sensor_archive'), { sensor_id: ROW }, ctx())).isError).toBeFalsy()
    expect(calls).toBe(1)
  })

  it('lists Home Assistant entities with a search', async () => {
    let search: string | null = null
    server.use(
      http.get(`${SENSORS}/entities`, ({ request }) => {
        search = new URL(request.url).searchParams.get('search')
        return HttpResponse.json([{ entity_id: 'sensor.box', state: '41' }])
      }),
    )
    const out = await runTool(tool('filament_ha_entities'), { search: 'box' }, ctx())
    expect(firstText(out)).toEqual([{ entity_id: 'sensor.box', state: '41' }])
    expect(search).toBe('box')
  })
})

const RFID = `${BACKEND}/api/v1/filament/rfid`
const STATUS = { override_mode: true, state: 'applied' }

describe('filament RFID tools (#6264)', () => {
  it.each([
    ['filament_rfid_status', 'read', false],
    ['filament_spool_bind_tag', 'write', false],
    ['filament_tag_unbind', 'write', false],
    // Each changes Bambuddy itself (a setting, an AMS slot): outward and
    // approved first, as the virtual-printer tools are (printers.ts).
    ['filament_rfid_override_mode', 'outward', true],
    ['filament_rfid_reconcile', 'outward', true],
  ])('%s is %s tier, gated: %s', (name, risk, gated) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(gated)
  })

  it('never says "Writes ScadBuddy only" of a tool that can tell Bambuddy', () => {
    for (const name of ['filament_spool_bind_tag', 'filament_tag_unbind']) {
      expect(tool(name).description).not.toContain('Writes ScadBuddy only')
      expect(tool(name).description).toContain('Bambuddy')
    }
    expect(tool('filament_rfid_reconcile').description).toContain('slot')
    expect(tool('filament_rfid_reconcile').description).not.toContain('Bambuddy only')
  })

  it('has no SpoolBuddy tools', () => {
    const names = new Set(ALL_TOOLS.map((t) => t.name))
    for (const gone of [
      'filament_rfid_scan',
      'filament_spool_ntag_write',
      'filament_spool_ntag_confirm',
      'filament_spool_ntag_cancel',
    ]) {
      expect(names.has(gone)).toBe(false)
    }
  })

  it('reads the status', async () => {
    server.use(http.get(`${RFID}/status`, () => HttpResponse.json(STATUS)))
    expect(firstText(await runTool(tool('filament_rfid_status'), {}, ctx()))).toEqual(STATUS)
  })

  it('turns the mode on with PUT and {enabled: true}', async () => {
    const bodies: unknown[] = []
    server.use(
      http.put(`${RFID}/override-mode`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(STATUS)
      }),
    )
    const t = tool('filament_rfid_override_mode')
    const result = await t.execute(t.parse({ enabled: true }), ctx('outward'))
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ enabled: true }])
  })

  it('reconciles once', async () => {
    const report = { mode: STATUS, tags: { pushed: 1, moved: 0, cleared: 0, unchanged: 0, failed: 0 }, slots: null }
    server.use(http.post(`${RFID}/reconcile`, () => HttpResponse.json(report)))
    const t = tool('filament_rfid_reconcile')
    expect(firstText(await t.execute(t.parse({}), ctx('outward')))).toEqual(report)
  })
})

const SYNC = `${BACKEND}/api/v1/filament/sync`

describe('filament Bambuddy sync tools (#6265)', () => {
  it.each([
    ['filament_sync_status', 'read', false],
    // #6905: the pass history reads ScadBuddy only.
    ['filament_sync_passes', 'read', false],
    // It writes Bambuddy, as filament_rfid_reconcile does: outward and approved first.
    ['filament_sync_run', 'outward', true],
    // #6773: it deletes the links and asks for a pass that writes Bambuddy.
    ['filament_sync_reset_links', 'outward', true],
  ])('%s is %s tier, gated: %s', (name, risk, gated) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(gated)
  })

  it('classifies the run and the reset as touching no recorded resource', () => {
    expect(TOUCHES_NOTHING.has('filament_sync_run')).toBe(true)
    expect(TOUCHES_NOTHING.has('filament_sync_reset_links')).toBe(true)
  })

  it('resets the links of the instance it names, and needs one', async () => {
    let body: unknown
    const deleted = { spools: 3, suppliers: 1, locations: 0, insert_types: 0, filaments: 2, intents: 0, errors: 1, ignored: 0 }
    server.use(
      http.post(`${SYNC}/reset-links`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ instance: '0ld0ld0ld0ld', kept: false, deleted })
      }),
    )
    const t = tool('filament_sync_reset_links')
    expect(() => t.parse({})).toThrow()
    const result = await t.execute(t.parse({ confirm_instance: '0ld0ld0ld0ld' }), ctx('outward'))
    expect(result.isError).toBeFalsy()
    expect(body).toEqual({ confirm_instance: '0ld0ld0ld0ld', expect_kept: false })
    expect(firstText(result)).toEqual({ instance: '0ld0ld0ld0ld', kept: false, deleted })
  })

  it('sends expect_kept, and says a 409 proof_changed deleted nothing (#6845 R-6845-6)', async () => {
    let body: unknown
    const none = { spools: 0, suppliers: 0, locations: 0, insert_types: 0, filaments: 0, intents: 0, errors: 0, ignored: 0 }
    server.use(
      http.post(`${SYNC}/reset-links`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ instance: '0ld0ld0ld0ld', kept: true, deleted: none })
      }),
    )
    const t = tool('filament_sync_reset_links')
    const result = await t.execute(t.parse({ confirm_instance: '0ld0ld0ld0ld', expect_kept: true }), ctx('outward'))
    expect(result.isError).toBeFalsy()
    expect(body).toEqual({ confirm_instance: '0ld0ld0ld0ld', expect_kept: true })
    const description = t.description
    expect(description).toContain('`expect_kept: true`')
    expect(description).toContain('`proof_changed`')
  })

  it('says the links are kept when the Bambuddy is the same one at a new address (#6845)', () => {
    const description = tool('filament_sync_reset_links').description
    expect(description).toContain('`kept: true`')
    expect(description).toContain('After a delete (`kept: false`)')
  })

  it('fails on a 409 and says why nothing was deleted', async () => {
    const detail = 'confirm_instance is not the instance the links belong to'
    server.use(
      http.post(`${SYNC}/reset-links`, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail, code: 'confirm_mismatch' },
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const t = tool('filament_sync_reset_links')
    await expect(t.execute(t.parse({ confirm_instance: 'abcabcabcabc' }), ctx('outward'))).rejects.toThrow(detail)
  })

  it('both run in durable mode: activities of the agent-tools worker', () => {
    const deps = { services: services(), sessions: { ownerOf: async () => undefined } }
    const activities = toolActivities(ALL_TOOLS, deps)
    const calls = toolCallActivities(ALL_TOOLS, deps)
    for (const name of ['filament_sync_status', 'filament_sync_passes', 'filament_sync_run']) {
      expect(activities[name]).toBeTypeOf('function')
      expect(calls[toolCallActivity(name)]).toBeTypeOf('function')
    }
  })

  it('says tag sync pushes with sb_to_bb or two_way, never sb_to_bb alone', () => {
    for (const t of ALL_TOOLS) expect(t.description).not.toContain('`filament_sync.tags` `sb_to_bb`')
    expect(tool('filament_rfid_reconcile').description).toContain('`two_way`')
    expect(tool('link_tray_spool').description).toContain('`two_way`')
  })

  it('reads the status', async () => {
    const status = { instance: null, errors: [], skipped: 'no_bambuddy' }
    server.use(http.get(`${SYNC}/status`, () => HttpResponse.json(status)))
    expect(firstText(await runTool(tool('filament_sync_status'), {}, ctx()))).toEqual(status)
  })

  it('reads the pass history a page at a time (#6905)', async () => {
    const queries: Record<string, string>[] = []
    const page = {
      passes: [{ id: ROW, trigger: 'timer', error: 'crud_unavailable:transport', written: 0 }],
      next_cursor: 'abc',
    }
    server.use(
      http.get(`${SYNC}/passes`, ({ request }) => {
        queries.push(Object.fromEntries(new URL(request.url).searchParams))
        return HttpResponse.json(page)
      }),
    )
    const t = tool('filament_sync_passes')
    expect(firstText(await runTool(t, {}, ctx()))).toEqual(page)
    await runTool(t, { cursor: 'abc', limit: 5 }, ctx())
    expect(queries).toEqual([{ limit: '20' }, { cursor: 'abc', limit: '5' }])
    expect(() => t.parse({ limit: 201 })).toThrow()
  })

  it('asks for a full pass with POST', async () => {
    let posted = 0
    server.use(
      http.post(`${SYNC}/run`, () => {
        posted += 1
        return HttpResponse.json({ started: true }, { status: 202 })
      }),
    )
    const t = tool('filament_sync_run')
    const result = await t.execute(t.parse({}), ctx('outward'))
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toEqual({ started: true })
    expect(posted).toBe(1)
  })

  it('fails on the 503 when nothing started, and says so', async () => {
    const detail = 'could not ask the sync workflow for a pass: internal'
    server.use(
      http.post(`${SYNC}/run`, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Service Unavailable', status: 503, detail },
          { status: 503, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    expect(tool('filament_sync_run').description).toContain('503')
    const t = tool('filament_sync_run')
    await expect(t.execute(t.parse({}), ctx('outward'))).rejects.toThrow(detail)
  })

  it('fails on the 409 with no Bambuddy, naming why', async () => {
    server.use(
      http.post(`${SYNC}/run`, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: 'no Bambuddy is configured', code: 'no_bambuddy' },
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const t = tool('filament_sync_run')
    await expect(t.execute(t.parse({}), ctx('outward'))).rejects.toThrow('no Bambuddy is configured')
  })
})

describe('filament history and metrics tools', () => {
  const FIL = `${BACKEND}/api/v1/filament`

  it.each([
    ['filament_spool_history'],
    ['filament_forecast'],
    ['filament_metrics'],
    ['filament_purchasing_metrics'],
    ['filament_material_number_stats'],
    ['filament_cost_per_kg'],
  ])('%s is read tier', (name) => {
    expect(tool(name).risk).toBe('read')
    expect(tool(name).gated).toBe(false)
  })

  it('drops the series and keeps the newest 50 of 80 events', async () => {
    const events = Array.from({ length: 80 }, (_, i) => ({ seq: 80 - i }))
    server.use(
      http.get(`${SPOOL}/history`, () =>
        HttpResponse.json({ events, series: [{ g: 1 }], metrics: { used_g: 5 }, currency: 'USD', window_days: 30 }),
      ),
    )
    const t = tool('filament_spool_history')
    const out = firstText(await runTool(t, { spool_id: ROW }, ctx())) as { events: { seq: number }[] }
    expect(out).not.toHaveProperty('series')
    expect(out).toMatchObject({ metrics: { used_g: 5 }, currency: 'USD', window_days: 30, truncated: true })
    expect(out.events).toHaveLength(50)
    expect(out.events[0]?.seq).toBe(80)
    expect(out.events[49]?.seq).toBe(31)
  })

  it('does not mark a short history truncated', async () => {
    server.use(http.get(`${SPOOL}/history`, () => HttpResponse.json({ events: [{ seq: 1 }], series: [] })))
    expect(firstText(await runTool(tool('filament_spool_history'), { spool_id: ROW }, ctx()))).toMatchObject({
      truncated: false,
    })
  })

  it('reads the forecast', async () => {
    server.use(http.get(`${FIL}/forecast`, () => HttpResponse.json({ spools: [] })))
    expect(firstText(await runTool(tool('filament_forecast'), {}, ctx()))).toEqual({ spools: [] })
  })

  it('sends the metrics query it was given', async () => {
    const seen: string[] = []
    server.use(
      http.get(`${FIL}/metrics`, ({ request }) => {
        seen.push(new URL(request.url).search)
        return HttpResponse.json({ ok: true })
      }),
    )
    await runTool(tool('filament_metrics'), { dimension: 'brand' }, ctx())
    await runTool(tool('filament_metrics'), { bucket: 'week', since: '2026-01-01', until: '2026-02-01' }, ctx())
    expect(seen).toEqual(['?dimension=brand', '?bucket=week&since=2026-01-01&until=2026-02-01'])
  })

  it('sends the purchasing range', async () => {
    server.use(
      http.get(`${FIL}/metrics/purchasing`, ({ request }) => {
        expect(new URL(request.url).search).toBe('?since=2026-01-01')
        return HttpResponse.json({ ok: true })
      }),
    )
    await runTool(tool('filament_purchasing_metrics'), { since: '2026-01-01' }, ctx())
  })

  it('reads the material number stats for the range it was given (#7152)', async () => {
    const seen: string[] = []
    server.use(
      http.get(`${FIL}/metrics/material-numbers`, ({ request }) => {
        seen.push(new URL(request.url).search)
        return HttpResponse.json({ currency: 'USD', rows: [{ key: 'PLA-WHT', used_g: 45 }] })
      }),
    )
    const out = firstText(await runTool(tool('filament_material_number_stats'), { since: '2026-11-01', until: '2026-11-30' }, ctx()))
    expect(out).toEqual({ currency: 'USD', rows: [{ key: 'PLA-WHT', used_g: 45 }] })
    await runTool(tool('filament_material_number_stats'), {}, ctx())
    expect(seen).toEqual(['?since=2026-11-01&until=2026-11-30', ''])
    expect(() => tool('filament_material_number_stats').parse({ since: '2026-02-30' })).toThrow()
  })

  it('asks the backend for a cost per kg with money as decimal strings (#7152)', async () => {
    const seen: string[] = []
    server.use(
      http.get(`${FIL}/cost-per-kg`, ({ request }) => {
        seen.push(new URL(request.url).search)
        return HttpResponse.json({ currency: 'USD', cost_per_kg: '32.1333' })
      }),
    )
    const t = tool('filament_cost_per_kg')
    const out = firstText(await runTool(t, { price: '40', tax: '3.20', shipping: '5', quantity: 2, net_weight_g: 750 }, ctx()))
    expect(out).toEqual({ currency: 'USD', cost_per_kg: '32.1333' })
    expect(seen).toEqual(['?price=40&tax=3.20&shipping=5&quantity=2&net_weight_g=750'])
    expect(() => t.parse({ price: 40, net_weight_g: 750 })).toThrow()
    expect(() => t.parse({ price: '1.00001', net_weight_g: 750 })).toThrow()
    expect(() => t.parse({ price: '1', net_weight_g: 0 })).toThrow()
  })

  it('refuses a dimension the route does not know', () => {
    expect(() => tool('filament_metrics').parse({ dimension: 'colour' })).toThrow()
  })

  it.each([['2026-13-45'], ['2026-02-30']])('refuses the impossible date %s', (day) => {
    expect(() => tool('filament_metrics').parse({ since: day })).toThrow()
    expect(() => tool('filament_purchasing_metrics').parse({ until: day })).toThrow()
  })

  it('accepts a leap day', () => {
    expect(() => tool('filament_metrics').parse({ since: '2028-02-29' })).not.toThrow()
  })
})

describe('filament_type_fill_from_profile (#6714)', () => {
  const TYPES = `${BACKEND}/api/v1/filament/types`
  const fill = { type_id: ROW, code: 'X', filled: { density_g_cm3: '1.32' }, previous: {}, skipped: {}, k_values: [], written: true }

  it('is write tier, never gated', () => {
    expect(tool('filament_type_fill_from_profile').risk).toBe('write')
    expect(tool('filament_type_fill_from_profile').gated).toBe(false)
    expect(tool('filament_type_fill_from_profile').description).toMatch(/empty/)
  })

  it('fills one type, or every type, and sends refresh and apply as given', async () => {
    const sent: { url: string; body: unknown }[] = []
    server.use(
      http.post(`${TYPES}/fill-from-profile`, async ({ request }) => {
        sent.push({ url: request.url, body: await request.json() })
        return HttpResponse.json([fill])
      }),
      http.post(`${TYPES}/:id/fill-from-profile`, async ({ request }) => {
        sent.push({ url: request.url, body: await request.json() })
        return HttpResponse.json(fill)
      }),
    )
    expect(firstText(await runTool(tool('filament_type_fill_from_profile'), { refresh: true }, ctx()))).toEqual(withUiPaths([fill]))
    const shown = [{ printer_id: ROW, extruder: 0, nozzle_diameter: '0.4', k_value: '0.02' }]
    const one = await runTool(tool('filament_type_fill_from_profile'), { type_id: ROW, apply: { density_g_cm3: '1.32' }, k_values: true, shown_k_values: shown }, ctx())
    expect(firstText(one)).toEqual(withUiPaths(fill))
    expect(sent).toEqual([
      { url: `${TYPES}/fill-from-profile`, body: { refresh: true } },
      { url: `${TYPES}/${ROW}/fill-from-profile`, body: { refresh: false, apply: { density_g_cm3: '1.32' }, k_values: true, shown_k_values: shown } },
    ])
  })

  it('applies the drying values and says it reads the printers K calibrations (#6720)', () => {
    const parsed = tool('filament_type_fill_from_profile').parse({ type_id: ROW, apply: { drying_temp_c: 55, drying_hours: '8' } })
    expect(parsed.apply).toEqual({ drying_temp_c: 55, drying_hours: '8' })
    const description = tool('filament_type_fill_from_profile').description
    expect(description).toMatch(/drying/)
    expect(description).toMatch(/K calibrations/)
    expect(description).toMatch(/never overwritten/)
  })

  it('refuses apply without a type', async () => {
    const refused = await runTool(tool('filament_type_fill_from_profile'), { apply: { density_g_cm3: '1.32' } }, ctx())
    expect(refused.isError).toBe(true)
  })

  it('refuses k_values without the K values the refresh showed (R-6714-i)', async () => {
    const refused = await runTool(tool('filament_type_fill_from_profile'), { type_id: ROW, apply: {}, k_values: true }, ctx())
    expect(refused.isError).toBe(true)
    expect(JSON.stringify(refused)).toMatch(/shown_k_values/)
  })
})

describe('filament type proposal tools (#6701)', () => {
  const PROPOSALS = `${BACKEND}/api/v1/filament/type-proposals`
  const MATERIAL = '8d6f3c1e-0000-4000-8000-0000000067a1'
  const SPOOL = '8d6f3c1e-0000-4000-8000-0000000067b1'
  const TYPE = '8d6f3c1e-0000-4000-8000-0000000067c1'

  it.each([
    ['filament_type_proposals', 'read'],
    ['filament_type_proposals_confirm', 'write'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('tells the assistant to confirm only after the user says yes', () => {
    expect(tool('filament_type_proposals_confirm').description).toMatch(/only after the user says yes/i)
  })

  it('lists the proposals', async () => {
    const proposals = { groups: [{ code: 'PLA-FF0000', count: 2 }], ungrouped: [] }
    server.use(http.get(PROPOSALS, () => HttpResponse.json(proposals)))
    const listed = await runTool(tool('filament_type_proposals'), {}, ctx())
    expect(firstText(listed)).toEqual(proposals)
  })

  it('confirms the groups given, with their edited codes', async () => {
    const group = {
      material_id: MATERIAL,
      brand_id: null,
      subtype_id: null,
      rgba: '#FF0000FF',
      label_weight_g: 1000,
      material_number: null,
      code: 'PLA Red',
      spool_ids: [SPOOL],
    }
    const result = { groups: [{ code: 'PLA Red', type_id: TYPE, made: true, new: true, typed: [SPOOL], skipped: [] }] }
    const bodies: unknown[] = []
    server.use(
      http.post(`${PROPOSALS}/confirm`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(result)
      }),
    )
    const done = await runTool(tool('filament_type_proposals_confirm'), { groups: [group] }, ctx())
    expect(firstText(done)).toEqual(withUiPaths(result))
    expect(bodies).toEqual([{ groups: [group] }])
    expect(tool('filament_type_proposals_confirm').summarize({ groups: [group] })).toBe(
      'Make 1 spool type for 1 spool',
    )
  })

  it("bounds a group's label weight and spools as the backend does (R-6729-4)", () => {
    const input = z.object(tool('filament_type_proposals_confirm').shape)
    const group = { material_id: MATERIAL, rgba: '#FF0000', label_weight_g: 100_000, code: 'X', spool_ids: [SPOOL] }
    expect(input.safeParse({ groups: [group] }).success).toBe(true)
    expect(input.safeParse({ groups: [{ ...group, label_weight_g: 100_001 }] }).success).toBe(false)
    expect(input.safeParse({ groups: [{ ...group, spool_ids: Array(5000).fill(SPOOL) }] }).success).toBe(true)
    expect(input.safeParse({ groups: [{ ...group, spool_ids: Array(5001).fill(SPOOL) }] }).success).toBe(false)
  })

  it("keeps shown_k_values' item keys equal to the generated TypeKValueCreate (#6762)", () => {
    // The spec `pnpm gen:api` exported, as in coverage.test.ts.
    const spec = JSON.parse(readFileSync(new URL('../../backend/openapi.json', import.meta.url), 'utf8')) as {
      components: { schemas: Record<string, { properties: Record<string, unknown>; required?: string[] }> }
    }
    const theirs = spec.components.schemas.TypeKValueCreate!
    type Item = { properties: Record<string, unknown>; required?: string[] }
    const json = z.toJSONSchema(z.object(tool('filament_type_fill_from_profile').shape), { io: 'input' }) as unknown as {
      properties: { shown_k_values: { items: Item } }
    }
    const ours = json.properties.shown_k_values.items
    expect(Object.keys(ours.properties).sort()).toEqual(Object.keys(theirs.properties).sort())
    expect([...(ours.required ?? [])].sort()).toEqual([...(theirs.required ?? [])].sort())
  })

  it("bounds a group's material_number at 128 characters as the backend does (R-6729-6)", () => {
    const input = z.object(tool('filament_type_proposals_confirm').shape)
    const group = { material_id: MATERIAL, rgba: '#FF0000', label_weight_g: 1000, code: 'X', spool_ids: [SPOOL] }
    expect(input.safeParse({ groups: [{ ...group, material_number: 'N'.repeat(128) }] }).success).toBe(true)
    expect(input.safeParse({ groups: [{ ...group, material_number: 'N'.repeat(129) }] }).success).toBe(false)
  })

  it('says when a code is taken', async () => {
    server.use(
      http.post(`${PROPOSALS}/confirm`, () =>
        HttpResponse.json({ detail: 'a spool type already has one of these codes' }, { status: 409 }),
      ),
    )
    const refused = await runTool(
      tool('filament_type_proposals_confirm'),
      { groups: [{ material_id: MATERIAL, rgba: '#FF0000', label_weight_g: 1000, code: 'X', spool_ids: [SPOOL] }] },
      ctx(),
    )
    expect(refused.isError).toBe(true)
  })
})

describe('the colour catalog syncs (#6907)', () => {
  const SYNCS = `${BACKEND}/api/v1/filament/colour-syncs`
  const RUN = {
    id: ROW,
    source: 'bambu',
    state: 'done',
    pages_read: 1,
    pages_total: 1,
    total: 4,
    added: 4,
    updated: 0,
    unchanged: 0,
    kept: 0,
    skipped: 0,
    error: null,
    started_at: '2026-11-21T12:00:00Z',
    finished_at: '2026-11-21T12:00:01Z',
  }

  it.each([
    ['filament_colour_sync', 'write'],
    ['filament_colour_sync_status', 'read'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('starts a sync with a key and answers the run', async () => {
    const seen: { body: unknown; key: string | null }[] = []
    server.use(
      http.post(SYNCS, async ({ request }) => {
        seen.push({ body: await request.json(), key: request.headers.get('Idempotency-Key') })
        return HttpResponse.json(RUN)
      }),
    )
    const result = await runTool(tool('filament_colour_sync'), { source: 'bambu' }, ctx())
    expect(result.isError).toBeFalsy()
    expect(seen).toHaveLength(1)
    expect(seen[0]!.body).toEqual({ source: 'bambu', force: false })
    expect(seen[0]!.key).toBeTruthy()
    expect(firstText(result)).toMatchObject({ state: 'done', added: 4 })
  })

  it('reads the newest runs', async () => {
    server.use(http.get(SYNCS, () => HttpResponse.json([RUN])))
    const result = await runTool(tool('filament_colour_sync_status'), {}, ctx())
    expect(firstText(result)).toEqual([RUN])
  })

  it('passes a colour search to the catalog list', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/filament/catalog/colours`, ({ request }) => {
        expect(new URL(request.url).searchParams.get('q')).toBe('jade white')
        return HttpResponse.json([])
      }),
    )
    const result = await runTool(tool('filament_catalog_list'), { kind: 'colours', q: 'jade white' }, ctx())
    expect(result.isError).toBeFalsy()
  })
})

// #7150: bulk create and copy through filament_spool_upsert.
describe('filament_spool_upsert quantity and copy_of', () => {
  const BULK = `${SPOOLS}/bulk`
  const SOURCE_ID = '8d6f3c1e-0000-4000-8000-0000000061b2'

  it('makes N spools through the bulk route, with a key derived from the call', async () => {
    const seen: { body: unknown; key: string | null }[] = []
    server.use(
      http.post(BULK, async ({ request }) => {
        seen.push({ body: await request.json(), key: request.headers.get('Idempotency-Key') })
        return HttpResponse.json([{ id: ROW, number: 20 }, { id: ROW, number: 21 }], { status: 201 })
      }),
    )
    const call = { ...ctx(), callKey: 'durable:s1:run-1:toolu_9' }
    const input = { fields: { material_id: ROW }, quantity: 2 }
    expect((await runTool(tool('filament_spool_upsert'), input, call)).isError).toBeFalsy()
    await runTool(tool('filament_spool_upsert'), input, call)
    expect(seen[0]!.body).toEqual({ spool: { material_id: ROW }, quantity: 2 })
    expect(seen[0]!.key).toMatch(/^[0-9a-f]{64}$/)
    // The same call sent again carries the same key, so the backend makes no second set.
    expect(seen[1]!.key).toBe(seen[0]!.key)
  })

  it('starts from the copy draft and lays the fields over it', async () => {
    const bodies: unknown[] = []
    server.use(
      http.get(`${SPOOLS}/${SOURCE_ID}/copy`, () =>
        HttpResponse.json({ spool: { type_id: ROW, colour_name: 'Teal', weight_locked: false }, location_id: null, presets: [], k_values: [], copy_of: SOURCE_ID }),
      ),
      http.post(BULK, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json([{ id: ROW, number: 22 }], { status: 201 })
      }),
    )
    const result = await runTool(tool('filament_spool_upsert'), { copy_of: SOURCE_ID, fields: { note: 'second' } }, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([
      // R-7150-13: the draft names its source (the route answers `copy_of`), so the backend keeps a copied migrated row so.
      { spool: { type_id: ROW, colour_name: 'Teal', weight_locked: false, note: 'second' }, location_id: null, presets: [], k_values: [], quantity: 1, copy_of: SOURCE_ID },
    ])
  })

  it('passes continued_from through for a respooled spool (#7684)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.get(`${SPOOLS}/${SOURCE_ID}/copy`, () =>
        HttpResponse.json({ spool: { type_id: ROW, weight_locked: false }, location_id: null, presets: [], k_values: [], copy_of: SOURCE_ID }),
      ),
      http.post(BULK, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json([{ id: ROW, number: 22 }], { status: 201 })
      }),
    )
    const call = { copy_of: SOURCE_ID, continued_from: SOURCE_ID, fields: { grams_left: 480, added_full: false } }
    expect((await runTool(tool('filament_spool_upsert'), call, ctx())).isError).toBeFalsy()
    expect(bodies).toEqual([
      { spool: { type_id: ROW, weight_locked: false, grams_left: 480, added_full: false }, location_id: null, presets: [], k_values: [], quantity: 1, copy_of: SOURCE_ID, continued_from: SOURCE_ID },
    ])
    const refused = await runTool(tool('filament_spool_upsert'), { spool_id: ROW, continued_from: SOURCE_ID }, ctx())
    expect(refused.isError).toBe(true)
  })

  // R-7150-8: a resend reads the copy again, so its key names the draft it read too.
  const TEAL = { spool: { type_id: ROW, colour_name: 'Teal' }, location_id: null, presets: [], k_values: [] }
  function copyResends(drafts: (typeof TEAL)[]) {
    const keys: (string | null)[] = []
    let read = 0
    server.use(
      http.get(`${SPOOLS}/${SOURCE_ID}/copy`, () => HttpResponse.json(drafts[Math.min(read++, drafts.length - 1)])),
      http.post(BULK, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.json([{ id: ROW, number: 22 }], { status: 201 })
      }),
    )
    return keys
  }

  it('sends the same key when a copy is resent with its source unchanged (R-7150-8)', async () => {
    const keys = copyResends([TEAL, TEAL])
    const call = { ...ctx(), callKey: 'durable:s1:run-1:toolu_copy' }
    await runTool(tool('filament_spool_upsert'), { copy_of: SOURCE_ID }, call)
    await runTool(tool('filament_spool_upsert'), { copy_of: SOURCE_ID }, call)
    expect(keys).toHaveLength(2)
    expect(keys[0]).toMatch(/^[0-9a-f]{64}$/)
    expect(keys[1]).toBe(keys[0])
  })

  it('sends a new key when a copy is resent after its source changed (R-7150-8)', async () => {
    const keys = copyResends([TEAL, { ...TEAL, spool: { ...TEAL.spool, colour_name: 'Jade' } }])
    const call = { ...ctx(), callKey: 'durable:s1:run-1:toolu_copy' }
    await runTool(tool('filament_spool_upsert'), { copy_of: SOURCE_ID }, call)
    await runTool(tool('filament_spool_upsert'), { copy_of: SOURCE_ID }, call)
    expect(keys).toHaveLength(2)
    expect(keys[1]).toMatch(/^[0-9a-f]{64}$/)
    expect(keys[1]).not.toBe(keys[0])
  })

  it('sends properties inside a bulk add, and answers each spool with them (#7703)', async () => {
    const bodies: unknown[] = []
    const read: string[] = []
    server.use(
      http.post(BULK, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json([{ id: ROW, number: 1 }, { id: SOURCE_ID, number: 2 }], { status: 201 })
      }),
      http.get(`${BACKEND}/api/v1/entities/:ref/properties`, ({ params }) => {
        read.push(String(params.ref))
        return HttpResponse.json({ properties: [{ key: 'mpn', value: 'M1', source: 'manual' }] })
      }),
    )
    const result = await runTool(tool('filament_spool_upsert'), { quantity: 2, fields: { type_id: ROW }, properties: { mpn: 'M1' } }, ctx())
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toMatchObject([
      { id: ROW, properties: [{ key: 'mpn' }] },
      { id: SOURCE_ID, properties: [{ key: 'mpn' }] },
    ])
    expect(bodies).toEqual([{ spool: { type_id: ROW, properties: { mpn: 'M1' } }, quantity: 2 }])
    expect(read).toEqual([`spool:${ROW}`, `spool:${SOURCE_ID}`])
  })

  it('catalog upsert takes properties: in the row body, then read back, or alone through the property patch (#7703)', async () => {
    const calls: string[] = []
    const PROPS = `${BACKEND}/api/v1/entities/${encodeURIComponent(`brand:${ROW}`)}/properties`
    server.use(
      http.post(LIST, async ({ request }) => {
        calls.push(`POST ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ROW, name: 'Acme' }, { status: 201 })
      }),
      http.patch(ONE, async ({ request }) => {
        calls.push(`PATCH row ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ROW, name: 'Acme' })
      }),
      http.get(PROPS, () => {
        calls.push('GET props')
        return HttpResponse.json({ properties: [{ key: 'site', value: 'acme.test', source: 'manual' }] })
      }),
      http.patch(PROPS, async ({ request }) => {
        calls.push(`PATCH props ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ properties: [] })
      }),
    )
    const added = await runTool(tool('filament_catalog_upsert'), { kind: 'brands', fields: { name: 'Acme' }, properties: { site: 'acme.test' } }, ctx())
    expect(added.isError).toBeFalsy()
    expect(firstText(added)).toMatchObject({ id: ROW, properties: [{ key: 'site', value: 'acme.test' }] })
    expect(calls).toEqual(['POST {"name":"Acme","properties":{"site":"acme.test"}}', 'GET props'])
    calls.length = 0
    const only = await runTool(tool('filament_catalog_upsert'), { kind: 'brands', row_id: ROW, properties: { site: null } }, ctx())
    expect(only.isError).toBeFalsy()
    expect(firstText(only)).toMatchObject({ kind: 'brands', id: ROW, properties: [] })
    expect(calls).toEqual(['PATCH props {"set":{"site":null}}'])
    expect(tool('filament_catalog_upsert').summarize({ kind: 'brands', row_id: ROW, fields: {}, properties: { site: null } })).toBe(
      `Edit brands row ${ROW}; properties: remove site`,
    )
  })

  it('refuses quantity or copy_of on an edit before calling the backend', async () => {
    const result = await runTool(tool('filament_spool_upsert'), { spool_id: ROW, quantity: 2 }, ctx())
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toContain('leave out `spool_id`')
  })

  it('names the bulk and copy routes', () => {
    expect(tool('filament_spool_upsert').routes).toEqual(
      expect.arrayContaining(['POST /api/v1/filament/spools/bulk', 'GET /api/v1/filament/spools/{spool_id}/copy']),
    )
  })
})

describe('filament preset read tools (#7204)', () => {
  const wire = JSON.parse(readFileSync(`${import.meta.dirname}/../../test-fixtures/filament/preset-wire.json`, 'utf8')) as {
    row: Record<string, unknown>
    detail: Record<string, unknown>
    diff: Record<string, unknown>
  }
  const PRESETS = `${BACKEND}/api/v1/filament/presets`
  const NAMES = ['filament_preset_list', 'filament_preset_get', 'filament_preset_diff']

  it.each(NAMES)('%s is read tier, never gated, with no OpenAPI route', (name) => {
    expect(tool(name).risk).toBe('read')
    expect(tool(name).gated).toBe(false)
    expect(tool(name).routes).toEqual([])
  })

  it('keeps the hand-written wire types to the keys Go writes (N8)', () => {
    const keys = <T,>(all: Record<keyof T, true>) => Object.keys(all).sort()
    const row = keys<FilamentPresetRow>({
      id: true, ref: true, name: true, source: true, version: true, type_id: true, printer_model: true,
      nozzle_diameter: true, filament_type: true, filament_colour: true, compatible_printers: true, sha256: true,
      bb_preset_id: true, bb_instance: true, mirror: true, mirror_error: true, mirror_error_at: true,
      used_by_types: true, used_by_spools: true, created_at: true, updated_at: true,
    })
    const detail = keys<FilamentPresetDetail>({
      id: true, ref: true, name: true, source: true, version: true, type_id: true, printer_model: true,
      nozzle_diameter: true, filament_type: true, filament_colour: true, compatible_printers: true, sha256: true,
      bb_preset_id: true, bb_instance: true, mirror: true, mirror_error: true, mirror_error_at: true,
      used_by_types: true, used_by_spools: true, created_at: true, updated_at: true,
      versions: true, document: true, document_error: true,
    })
    const version = keys<FilamentPresetVersion>({
      version: true, sha256: true, made_by: true, made_at: true, fetch_run_id: true, bb_updated_at: true,
    })
    const diff = keys<FilamentPresetDiff>({ preset_id: true, from_version: true, to_version: true, changes: true })
    const change = keys<FilamentPresetKeyChange>({ key: true, before: true, after: true })
    const sorted = (o: object) => Object.keys(o).sort()
    const versions = wire.detail.versions as object[]
    const changes = wire.diff.changes as object[]
    expect(sorted(wire.row)).toEqual(row)
    expect(sorted(wire.detail)).toEqual(detail)
    expect(sorted(versions[0]!)).toEqual(version)
    expect(sorted(wire.diff)).toEqual(diff)
    expect(sorted(changes[0]!)).toEqual(change)
  })

  it('lists presets with the filters it was given', async () => {
    const TYPE = '5d0c8f1e-0000-4000-8000-000000007204'
    server.use(
      http.get(PRESETS, ({ request }) => {
        const url = new URL(request.url)
        expect(url.searchParams.get('source')).toBe('manual')
        expect(url.searchParams.get('type_id')).toBe(TYPE)
        return HttpResponse.json([wire.row])
      }),
    )
    const listed = await runTool(tool('filament_preset_list'), { source: 'manual', type_id: TYPE }, ctx())
    expect(firstText(listed)).toMatchObject([{ id: 7, ui_path: '/filament/presets/7', type_ui_path: expect.any(String) }])
  })

  it('lower-cases the type id before it is sent', async () => {
    const TYPE = '5d0c8f1e-0000-4000-8000-00000000ABCD'
    let sent: string | null = null
    server.use(
      http.get(PRESETS, ({ request }) => {
        sent = new URL(request.url).searchParams.get('type_id')
        return HttpResponse.json([wire.row])
      }),
    )
    await runTool(tool('filament_preset_list'), { type_id: TYPE }, ctx())
    expect(sent).toBe(TYPE.toLowerCase())
  })

  it('reads a preset and diffs two versions', async () => {
    server.use(
      http.get(`${PRESETS}/7`, () => HttpResponse.json(wire.detail)),
      http.get(`${PRESETS}/7/diff`, ({ request }) => {
        const url = new URL(request.url)
        expect(url.searchParams.get('from_version')).toBe('2')
        expect(url.searchParams.get('to_version')).toBe('3')
        return HttpResponse.json(wire.diff)
      }),
    )
    const one = await runTool(tool('filament_preset_get'), { preset_id: 7 }, ctx())
    expect(firstText(one)).toMatchObject({ id: 7, ui_path: '/filament/presets/7', document: wire.detail.document })
    const diff = await runTool(tool('filament_preset_diff'), { preset_id: 7, from_version: 2, to_version: 3 }, ctx())
    expect(firstText(diff)).toMatchObject({ preset_id: 7, changes: wire.diff.changes })
  })

  it('says the presets routes are off on a bare 404', async () => {
    server.use(
      http.get(PRESETS, () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })),
      http.get(`${PRESETS}/8`, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Not Found', status: 404, code: 'preset_not_found' },
          { status: 404, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const off = await runTool(tool('filament_preset_list'), {}, ctx())
    expect(off.isError).toBe(true)
    expect(JSON.stringify(off.content)).toContain('the presets routes are not on yet on this server (core area filament-presets)')
    const missing = await runTool(tool('filament_preset_get'), { preset_id: 8 }, ctx())
    expect(missing.isError).toBe(true)
    expect(JSON.stringify(missing.content)).toContain('HTTP 404')
    expect(JSON.stringify(missing.content)).not.toContain('not on yet')
  })

  it('the preset tools run in durable mode', () => {
    const deps = { services: services(), sessions: { ownerOf: async () => undefined } }
    const activities = toolActivities(ALL_TOOLS, deps)
    const calls = toolCallActivities(ALL_TOOLS, deps)
    for (const name of NAMES) {
      expect(activities[name]).toBeTypeOf('function')
      expect(calls[toolCallActivity(name)]).toBeTypeOf('function')
    }
  })
})
