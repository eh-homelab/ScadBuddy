import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { tiersUpTo } from '../src/auth/principal.js'
import { TOUCHES_NOTHING } from '../src/sessions/touched.js'
import { toolActivities, toolCallActivities } from '../src/temporal/toolActivities.js'
import { toolCallActivity } from '../src/temporal/toolCall.js'
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
    expect(start).toMatch(/Approved first\.$/)
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
    expect(firstText(read)).toEqual({ run: RUN, gaps })
  })

  it('reads a type that never had a fetch', async () => {
    server.use(http.get(FETCH, () => HttpResponse.json({ run: null, gaps: [] })))
    const read = await runTool(tool('filament_type_profile_fetch'), { type_id: ROW }, ctx())
    expect(firstText(read)).toEqual({ run: null, gaps: [] })
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
    ['filament_ledger_list', 'read'],
    ['filament_summary', 'read'],
    ['filament_spool_upsert', 'write'],
    ['filament_spool_set_presets', 'write'],
    ['filament_spool_set_k_values', 'write'],
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
const STATUS = { override_mode: true, state: 'applied', devices: [] }

describe('filament RFID tools (#6264)', () => {
  it.each([
    ['filament_rfid_status', 'read', false],
    ['filament_rfid_scan', 'read', false],
    ['filament_spool_ntag_confirm', 'write', false],
    ['filament_spool_bind_tag', 'write', false],
    ['filament_tag_unbind', 'write', false],
    // Each changes Bambuddy itself (a setting, an AMS slot, a tag on a reader): outward and
    // approved first, as the virtual-printer tools are (printers.ts).
    ['filament_rfid_override_mode', 'outward', true],
    ['filament_rfid_reconcile', 'outward', true],
    ['filament_spool_ntag_write', 'outward', true],
    ['filament_spool_ntag_cancel', 'outward', true],
  ])('%s is %s tier, gated: %s', (name, risk, gated) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(gated)
  })

  it('never says "Writes ScadBuddy only" of a tool that can tell Bambuddy', () => {
    for (const name of ['filament_spool_bind_tag', 'filament_tag_unbind', 'filament_spool_ntag_confirm']) {
      expect(tool(name).description).not.toContain('Writes ScadBuddy only')
      expect(tool(name).description).toContain('Bambuddy')
    }
    expect(tool('filament_rfid_reconcile').description).toContain('slot')
    expect(tool('filament_rfid_reconcile').description).not.toContain('Bambuddy only')
  })

  it('refuses a device id that is not one path segment', () => {
    const t = tool('filament_spool_ntag_cancel')
    expect(() => t.parse({ spool_id: ROW, device_id: '../x' })).toThrow()
    expect(() => t.parse({ spool_id: ROW, device_id: 'sb-1a2b' })).not.toThrow()
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

  it('scans with the timeout and the device', async () => {
    const bodies: unknown[] = []
    const found = { status: 'tag', tag_uid: '04AABBCC', kind: 'ntag_opentag', sb_spool: null }
    server.use(
      http.post(`${RFID}/scan`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(found)
      }),
    )
    const result = await runTool(tool('filament_rfid_scan'), { device_id: 'sb-1' }, ctx())
    expect(firstText(result)).toEqual(found)
    expect(bodies).toEqual([{ timeout_s: 30, device_id: 'sb-1' }])
  })

  it('writes an NTAG with {device_id, replace: false}', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${SPOOL}/ntag/write`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ status: 'queued', warnings: [] })
      }),
    )
    const write = tool('filament_spool_ntag_write')
    const result = await write.execute(write.parse({ spool_id: ROW, device_id: 'sb-1' }), ctx('outward'))
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ device_id: 'sb-1', replace: false }])
  })

  it('confirms and cancels', async () => {
    const cancelled: unknown[] = []
    server.use(
      http.post(`${SPOOL}/ntag/confirm`, () =>
        HttpResponse.json({ bound: true, tag_uid: '04AABBCC' }),
      ),
      http.post(`${SPOOL}/ntag/cancel`, async ({ request }) => {
        cancelled.push(await request.json())
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const confirmed = await runTool(tool('filament_spool_ntag_confirm'), { spool_id: ROW }, ctx())
    expect(firstText(confirmed)).toEqual({ bound: true, tag_uid: '04AABBCC' })
    const cancel = tool('filament_spool_ntag_cancel')
    const result = await cancel.execute(cancel.parse({ spool_id: ROW, device_id: 'sb-1' }), ctx('outward'))
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toEqual({ cancel_requested: 'sb-1' })
    expect(cancelled).toEqual([{ device_id: 'sb-1' }])
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
    for (const name of ['filament_sync_status', 'filament_sync_run']) {
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
    expect(firstText(await runTool(tool('filament_type_fill_from_profile'), { refresh: true }, ctx()))).toEqual([fill])
    const shown = [{ printer_id: ROW, extruder: 0, nozzle_diameter: '0.4', k_value: '0.02' }]
    const one = await runTool(tool('filament_type_fill_from_profile'), { type_id: ROW, apply: { density_g_cm3: '1.32' }, k_values: true, shown_k_values: shown }, ctx())
    expect(firstText(one)).toEqual(fill)
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
    expect(firstText(done)).toEqual(result)
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
