import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { EXTRACTORS } from '../src/sessions/touched.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, ToolError, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// The inventory tools (#7067, layer 5, Task 12): kinds, items, offers, lots, units and moves
// over the `/api/v1/inventory` routes.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const INV = `${BACKEND}/api/v1/inventory`
const ITEM = 'ab700000-0000-4000-8000-000000000001'
const UNIT = 'ab710000-0000-4000-8000-000000000001'
const LOT = 'ab720000-0000-4000-8000-000000000001'
const OFFER = 'ab730000-0000-4000-8000-000000000001'
const SUPPLIER = 'ab740000-0000-4000-8000-000000000001'
const PLACE = 'ab750000-0000-4000-8000-000000000001'
const props = `${BACKEND}/api/v1/entities/${encodeURIComponent(`item:${ITEM}`)}/properties`

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

const item = (over: Record<string, unknown> = {}) => ({ id: ITEM, kind_id: 'hardware', tracking: 'lot', name: 'M3 nut', on_hand: '10', ...over })

describe('inventory tools', () => {
  it('tiers each read as read and each write as write, and maps each to its routes', () => {
    const expected: Record<string, ['read' | 'write', string[]]> = {
      inventory_kind_list: ['read', ['GET /api/v1/inventory/kinds', 'GET /api/v1/inventory/kinds/{kind_id}/schema']],
      inventory_kind_upsert: [
        'write',
        [
          'POST /api/v1/inventory/kinds',
          'PATCH /api/v1/inventory/kinds/{kind_id}',
          'POST /api/v1/inventory/kinds/{kind_id}/archive',
          'POST /api/v1/inventory/kinds/{kind_id}/restore',
        ],
      ],
      inventory_item_list: ['read', ['GET /api/v1/inventory/items']],
      inventory_operation_get: ['read', ['GET /api/v1/inventory/operations/{operation_id}']],
      inventory_item_get: [
        'read',
        [
          'GET /api/v1/inventory/items/{item_id}',
          'GET /api/v1/inventory/items/{item_id}/offers',
          'GET /api/v1/inventory/items/{item_id}/lots',
          'GET /api/v1/inventory/items/{item_id}/units',
          'GET /api/v1/inventory/lots/{lot_id}',
          'GET /api/v1/inventory/units/{unit_id}',
          'GET /api/v1/inventory/moves',
        ],
      ],
      inventory_item_upsert: [
        'write',
        [
          'POST /api/v1/inventory/items',
          'PATCH /api/v1/inventory/items/{item_id}',
          'PATCH /api/v1/entities/{ref}/properties',
          'GET /api/v1/inventory/items/{item_id}',
        ],
      ],
      inventory_item_archive: ['write', ['POST /api/v1/inventory/items/{item_id}/archive', 'POST /api/v1/inventory/items/{item_id}/restore']],
      inventory_offer_list: [
        'read',
        ['GET /api/v1/inventory/items/{item_id}/offers', 'GET /api/v1/inventory/suppliers/{supplier_id}/offers', 'GET /api/v1/inventory/offers/{offer_id}/prices'],
      ],
      inventory_offer_upsert: [
        'write',
        ['POST /api/v1/inventory/offers', 'PATCH /api/v1/inventory/offers/{offer_id}', 'POST /api/v1/inventory/offers/{offer_id}/archive'],
      ],
      inventory_offer_price_add: ['write', ['POST /api/v1/inventory/offers/{offer_id}/prices']],
      inventory_lot_receive: ['write', ['POST /api/v1/inventory/items/{item_id}/lots']],
      inventory_lot_update: ['write', ['PATCH /api/v1/inventory/lots/{lot_id}']],
      inventory_lot_recost: ['write', ['POST /api/v1/inventory/lots/{lot_id}/recost']],
      inventory_unit_receive: ['write', ['POST /api/v1/inventory/items/{item_id}/units']],
      inventory_unit_update: ['write', ['PATCH /api/v1/inventory/units/{unit_id}']],
      inventory_unit_status: ['write', ['POST /api/v1/inventory/units/{unit_id}/status']],
      inventory_move_list: ['read', ['GET /api/v1/inventory/moves']],
      inventory_move_use: ['write', ['POST /api/v1/inventory/items/{item_id}/use']],
      inventory_move_count: ['write', ['POST /api/v1/inventory/items/{item_id}/count', 'POST /api/v1/inventory/lots/{lot_id}/adjust']],
    }
    const names = ALL_TOOLS.filter((t) => t.name.startsWith('inventory_')).map((t) => t.name)
    expect(names.sort()).toEqual(Object.keys(expected).sort())
    for (const [name, [risk, routes]] of Object.entries(expected)) {
      expect(tool(name).risk, name).toBe(risk)
      expect(tool(name).routes, name).toEqual(routes)
      if (risk === 'write') expect(EXTRACTORS[name], name).toBeDefined()
    }
  })

  it('says FIFO, snapshots, the re-cost of past orders and the kind:ref form in the descriptions', () => {
    expect(tool('inventory_move_use').description).toMatch(/FIFO/)
    expect(tool('inventory_move_use').description).toMatch(/snapshot/)
    expect(tool('inventory_move_use').description).toContain('`kind:ref`')
    expect(tool('inventory_lot_update').description).toMatch(/snapshot/)
    expect(tool('inventory_item_list').description).toContain('until inventory_lot_recost')
    expect(tool('inventory_lot_recost').description).toMatch(/past order/)
    expect(tool('inventory_unit_status').description).toContain('`kind:ref`')
    // An operation's answer is a status record, with no item or lot page of its own.
    for (const t of ALL_TOOLS.filter((x) => x.name.startsWith('inventory_') && x.name !== 'inventory_operation_get')) expect(t.description, t.name).toContain('ui_path')
  })

  it('adds an item with its properties in the same request, and answers it with its page (R-K1-33)', async () => {
    const calls: string[] = []
    server.use(
      http.post(`${INV}/items`, async ({ request }) => {
        calls.push(`POST item ${JSON.stringify(await request.json())}`)
        return HttpResponse.json(item(), { status: 201 })
      }),
      http.patch(props, async ({ request }) => {
        calls.push(`PATCH props ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ properties: [] })
      }),
    )
    const answer = firstText(
      await runTool(tool('inventory_item_upsert'), { fields: { kind_id: 'hardware', name: 'M3 nut' }, properties: { thread: 'M3' } }, ctx()),
    )
    expect(calls).toEqual(['POST item {"kind_id":"hardware","name":"M3 nut","properties":{"thread":"M3"}}'])
    expect(answer).toMatchObject({ id: ITEM, ui_path: `/inventory/items/${ITEM}`, kind_ui_path: '/inventory?kind=hardware' })
  })

  it('edits fields and properties in one PATCH', async () => {
    const calls: string[] = []
    server.use(
      http.patch(`${INV}/items/${ITEM}`, async ({ request }) => {
        calls.push(`PATCH item ${JSON.stringify(await request.json())}`)
        return HttpResponse.json(item())
      }),
    )
    await runTool(tool('inventory_item_upsert'), { item_id: ITEM, fields: { name: 'M3 nut' }, properties: { thread: null } }, ctx())
    expect(calls).toEqual(['PATCH item {"name":"M3 nut","properties":{"thread":null}}'])
  })

  it('names each missing and ill-fitting key when a required property is missing, and says nothing was written', async () => {
    server.use(
      http.post(`${INV}/items`, () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/entities/missing_required_properties',
            title: 'Unprocessable Content',
            status: 422,
            detail: 'missing required properties: material',
            code: 'missing_required_properties',
            missing: ['material'],
            invalid: [{ key: 'thread', reason: "does not fit 'type'" }],
          },
          { status: 422 },
        ),
      ),
    )
    const err = await tool('inventory_item_upsert')
      .execute({ fields: { kind_id: 'strict', name: 'M3 nut' }, properties: { thread: 3 } }, ctx())
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ToolError)
    expect((err as ToolError).message).toContain('missing: material')
    expect((err as ToolError).message).toContain('values that do not fit: thread')
    expect((err as ToolError).message).toContain('Nothing was written')
    expect((err as ToolError).problem).toBe('missing_required_properties')
  })

  it('says in its description that a missing required key writes nothing', () => {
    const text = tool('inventory_item_upsert').description
    expect(text).toMatch(/missing_required_properties/)
    expect(text).toMatch(/writes nothing/)
    expect(text).not.toMatch(/set on the item after it is saved/)
  })

  it('says nothing was saved when a properties-only edit is refused', async () => {
    server.use(
      http.patch(props, () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/entities/bad_property_value', title: 'Bad value', status: 422, detail: 'thread: not a number' },
          { status: 422 },
        ),
      ),
    )
    const err = await tool('inventory_item_upsert').execute({ item_id: ITEM, properties: { thread: 'M3' } }, ctx()).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ToolError)
    expect((err as ToolError).message).not.toContain('was saved')
    expect((err as ToolError).problem).toBe('bad_property_value')
  })

  it('edits only the fields given; a properties-only edit leaves the item row alone', async () => {
    const calls: string[] = []
    server.use(
      http.patch(`${INV}/items/${ITEM}`, async ({ request }) => {
        calls.push(`PATCH item ${JSON.stringify(await request.json())}`)
        return HttpResponse.json(item({ low_stock: '5' }))
      }),
      http.get(`${INV}/items/${ITEM}`, () => {
        calls.push('GET item')
        return HttpResponse.json(item())
      }),
      http.patch(props, async ({ request }) => {
        calls.push(`PATCH props ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ properties: [] })
      }),
    )
    await runTool(tool('inventory_item_upsert'), { item_id: ITEM, fields: { low_stock: '5', life_months: null } }, ctx())
    await runTool(tool('inventory_item_upsert'), { item_id: ITEM, properties: { thread: null } }, ctx())
    expect(calls).toEqual(['PATCH item {"low_stock":"5","life_months":null}', 'PATCH props {"set":{"thread":null}}', 'GET item'])
    await expect(tool('inventory_item_upsert').execute({ item_id: ITEM, fields: { kind_id: 'tool' } }, ctx())).rejects.toThrow('kind')
    await expect(tool('inventory_item_upsert').execute({ fields: { name: 'x' } }, ctx())).rejects.toThrow('kind_id')
    await expect(tool('inventory_item_upsert').execute({ item_id: ITEM }, ctx())).rejects.toThrow('nothing to change')
  })

  it("reads an item whole: its offers, its lots by its tracking and its last moves, from a lot's id too", async () => {
    const asked: string[] = []
    server.use(
      http.get(`${INV}/lots/${LOT}`, () => {
        asked.push('lot')
        return HttpResponse.json({ id: LOT, item_id: ITEM, on_hand: '4', location_id: PLACE })
      }),
      http.get(`${INV}/items/${ITEM}`, () => HttpResponse.json(item())),
      http.get(`${INV}/items/${ITEM}/offers`, () =>
        HttpResponse.json({ offers: [{ id: OFFER, item_id: ITEM, supplier_id: SUPPLIER, sku: 'N3', latest_price: { id: 'p1', price: '4.99' } }] }),
      ),
      http.get(`${INV}/items/${ITEM}/lots`, () => HttpResponse.json({ lots: [{ id: LOT, item_id: ITEM, on_hand: '4', location_id: PLACE }] })),
      http.get(`${INV}/moves`, ({ request }) => {
        asked.push(`moves ${new URL(request.url).search}`)
        return HttpResponse.json({ moves: [{ id: 'm1', item_id: ITEM, lot_id: LOT, kind: 'use', qty: '-1' }] })
      }),
    )
    const answer = firstText(await runTool(tool('inventory_item_get'), { lot_id: LOT }, ctx()))
    expect(asked).toEqual(['lot', `moves ?item_id=${ITEM}&limit=20`])
    expect(answer).toMatchObject({
      item: { id: ITEM, ui_path: `/inventory/items/${ITEM}` },
      lot: { id: LOT, ui_path: `/inventory/lots/${LOT}`, item_ui_path: `/inventory/items/${ITEM}`, location_ui_path: `/filament/locations/${PLACE}` },
      offers: [{ id: OFFER, ui_path: `/inventory/items/${ITEM}#offer-${OFFER}`, supplier_ui_path: `/filament/suppliers/${SUPPLIER}` }],
      lots: [{ id: LOT, ui_path: `/inventory/lots/${LOT}` }],
      moves: [{ id: 'm1', lot_ui_path: `/inventory/lots/${LOT}` }],
    })
    expect(answer).not.toHaveProperty('units')
    expect((answer as { moves: Record<string, unknown>[] }).moves[0]).not.toHaveProperty('ui_path')
  })

  it('reads a write that answered 202 by its operation id, in each of its three states', async () => {
    const OP = 'req:7c1a0000-0000-4000-8000-000000000001'
    const answers = [
      { id: OP, status: 'running' },
      { id: OP, status: 'succeeded', result: { id: ITEM, name: 'M3 nut' } },
      { id: OP, status: 'failed', error: { status: 409, title: 'item_exists' } },
    ]
    server.use(http.get(`${INV}/operations/${encodeURIComponent(OP)}`, () => HttpResponse.json(answers.shift())))
    const read = async () => firstText(await runTool(tool('inventory_operation_get'), { operation_id: OP }, ctx()))
    expect(await read()).toMatchObject({ status: 'running' })
    expect(await read()).toMatchObject({ status: 'succeeded', result: { id: ITEM } })
    expect(await read()).toMatchObject({ status: 'failed', error: { title: 'item_exists' } })
  })

  it("reads a unit item's units, and nothing of stock for a kind that is not counted", async () => {
    server.use(
      http.get(`${INV}/units/${UNIT}`, () => HttpResponse.json({ id: UNIT, item_id: ITEM, status: 'in_stock' })),
      http.get(`${INV}/items/${ITEM}`, () => HttpResponse.json(item({ tracking: 'unit' }))),
      http.get(`${INV}/items/${ITEM}/offers`, () => HttpResponse.json({ offers: [] })),
      http.get(`${INV}/items/${ITEM}/units`, () => HttpResponse.json({ units: [{ id: UNIT, item_id: ITEM }] })),
      http.get(`${INV}/moves`, () => HttpResponse.json({ moves: [] })),
    )
    const units = firstText(await runTool(tool('inventory_item_get'), { unit_id: UNIT, moves: 5 }, ctx()))
    expect(units).toMatchObject({ unit: { id: UNIT, ui_path: `/inventory/units/${UNIT}` }, units: [{ id: UNIT }] })
    expect(units).not.toHaveProperty('lots')

    server.use(http.get(`${INV}/items/${ITEM}`, () => HttpResponse.json(item({ tracking: 'none', kind_id: 'service' }))))
    const service = firstText(await runTool(tool('inventory_item_get'), { item_id: ITEM }, ctx()))
    expect(Object.keys(service as object).sort()).toEqual(['item', 'offers'])
    await expect(tool('inventory_item_get').execute({ item_id: ITEM, lot_id: LOT }, ctx())).rejects.toThrow('one of')
  })

  it('reads a filament item with its offers and no stock', async () => {
    const asked: string[] = []
    server.use(
      http.get(`${INV}/items/${ITEM}`, () => HttpResponse.json(item({ tracking: 'filament', kind_id: 'filament' }))),
      http.get(`${INV}/items/${ITEM}/offers`, () => (asked.push('offers'), HttpResponse.json({ offers: [] }))),
      http.get(`${INV}/items/${ITEM}/lots`, () => (asked.push('lots'), HttpResponse.json({ lots: [] }))),
      http.get(`${INV}/items/${ITEM}/units`, () => (asked.push('units'), HttpResponse.json({ units: [] }))),
      http.get(`${INV}/moves`, () => (asked.push('moves'), HttpResponse.json({ moves: [] }))),
    )
    const answer = firstText(await runTool(tool('inventory_item_get'), { item_id: ITEM }, ctx()))
    expect(asked).toEqual(['offers'])
    expect(Object.keys(answer as object).sort()).toEqual(['item', 'offers'])
    expect(tool('inventory_item_get').description).toContain('or a filament kind')
  })

  it('lists items with the filters given and a page for each', async () => {
    let search = ''
    server.use(
      http.get(`${INV}/items`, ({ request }) => {
        search = new URL(request.url).search
        return HttpResponse.json({ items: [item()] })
      }),
    )
    const answer = firstText(await runTool(tool('inventory_item_list'), { kind: 'hardware', q: 'nut', low: true }, ctx()))
    expect(search).toBe('?kind=hardware&q=nut&low=true&limit=200')
    expect(answer).toMatchObject({ items: [{ id: ITEM, ui_path: `/inventory/items/${ITEM}`, kind_ui_path: '/inventory?kind=hardware' }] })
  })

  it('reads the kinds, or one kind with its merged schema', async () => {
    server.use(
      http.get(`${INV}/kinds`, ({ request }) => HttpResponse.json({ kinds: [{ id: 'hardware', archived: new URL(request.url).search }] })),
      http.get(`${INV}/kinds/hardware/schema`, () => HttpResponse.json({ kind_id: 'hardware', schema: {}, own_schema: {}, keys_in_use: [] })),
    )
    expect(firstText(await runTool(tool('inventory_kind_list'), {}, ctx()))).toEqual({
      kinds: [{ id: 'hardware', archived: '?archived=false', ui_path: '/inventory?kind=hardware' }],
    })
    expect(firstText(await runTool(tool('inventory_kind_list'), { kind_id: 'hardware' }, ctx()))).toMatchObject({
      kind_id: 'hardware',
      kind_ui_path: '/inventory?kind=hardware',
    })
  })

  it('adds a numbered kind when no kind_id is given, and names the k-and-digits refusal', async () => {
    const posted: unknown[] = []
    server.use(
      http.post(`${INV}/kinds`, async ({ request }) => {
        posted.push(await request.clone().json())
        return HttpResponse.json({ id: 'k31', number: 31, code: 'K-31' }, { status: 201 })
      }),
    )
    const made = firstText(await runTool(tool('inventory_kind_upsert'), { create: true, fields: { name: 'Bearing', tracking: 'lot' } }, ctx()))
    expect(posted).toEqual([{ name: 'Bearing', tracking: 'lot' }])
    expect(made).toMatchObject({ id: 'k31', code: 'K-31' })
    await expect(tool('inventory_kind_upsert').execute({ fields: { name: 'B' } }, ctx())).rejects.toThrow('kind_id')
    expect(tool('inventory_kind_upsert').description).toContain('`k` and digits')
  })

  it('adds, edits, archives and restores a kind', async () => {
    const calls: string[] = []
    const kind = (request: Request, what: string) => {
      calls.push(what)
      return HttpResponse.json({ id: 'bearing', path: new URL(request.url).pathname })
    }
    server.use(
      http.post(`${INV}/kinds`, async ({ request }) => kind(request, `POST ${JSON.stringify(await request.clone().json())}`)),
      http.patch(`${INV}/kinds/bearing`, async ({ request }) => kind(request, `PATCH ${JSON.stringify(await request.clone().json())}`)),
      http.post(`${INV}/kinds/bearing/archive`, ({ request }) => kind(request, 'archive')),
      http.post(`${INV}/kinds/bearing/restore`, ({ request }) => kind(request, 'restore')),
    )
    await runTool(tool('inventory_kind_upsert'), { kind_id: 'bearing', create: true, fields: { name: 'Bearing', tracking: 'lot', parent_id: 'part' } }, ctx())
    await runTool(tool('inventory_kind_upsert'), { kind_id: 'bearing', fields: { parent_id: null }, archived: true }, ctx())
    await runTool(tool('inventory_kind_upsert'), { kind_id: 'bearing', archived: false }, ctx())
    expect(calls).toEqual([
      'POST {"name":"Bearing","tracking":"lot","parent_id":"part","id":"bearing"}',
      'PATCH {"parent_id":null}',
      'archive',
      'restore',
    ])
    await expect(tool('inventory_kind_upsert').execute({ kind_id: 'bearing', create: true, fields: { name: 'B' } }, ctx())).rejects.toThrow('tracking')
    await expect(tool('inventory_kind_upsert').execute({ kind_id: 'bearing' }, ctx())).rejects.toThrow('nothing to change')
    await expect(
      tool('inventory_kind_upsert').execute({ kind_id: 'bearing', create: true, fields: { name: 'B', tracking: 'lot' }, archived: false }, ctx()),
    ).rejects.toThrow('archived')
  })

  it("sends each write of a call its own Idempotency-Key, and a retried call's the same (#6949 K1)", async () => {
    const keys: string[] = []
    const kind = (request: Request) => {
      keys.push(`${new URL(request.url).pathname} ${request.headers.get('Idempotency-Key')}`)
      return HttpResponse.json({ id: 'bearing' })
    }
    server.use(
      http.patch(`${INV}/kinds/bearing`, ({ request }) => kind(request)),
      http.post(`${INV}/kinds/bearing/archive`, ({ request }) => kind(request)),
      http.post(`${INV}/items/${ITEM}/use`, ({ request }) => kind(request)),
    )
    const call = { ...ctx(), callKey: 'session-1:tool-1' }
    const edit = { kind_id: 'bearing', fields: { parent_id: null }, archived: true }
    await runTool(tool('inventory_kind_upsert'), edit, call)
    await runTool(tool('inventory_kind_upsert'), edit, call)
    await runTool(tool('inventory_move_use'), { item_id: ITEM, qty: '1' }, { ...ctx(), callKey: 'session-1:tool-2' })
    const [patch, archive, again, archiveAgain, use] = keys
    expect(patch).toMatch(/^\/api\/v1\/inventory\/kinds\/bearing [0-9a-f]{64}:edit$/)
    expect(archive).toMatch(/^\/api\/v1\/inventory\/kinds\/bearing\/archive [0-9a-f]{64}:archive$/)
    expect(patch!.split(' ')[1]!.split(':')[0]).toBe(archive!.split(' ')[1]!.split(':')[0])
    expect([again, archiveAgain]).toEqual([patch, archive])
    expect(use).toMatch(/ [0-9a-f]{64}:use$/)
    expect(use!.split(' ')[1]!.split(':')[0]).not.toBe(patch!.split(' ')[1]!.split(':')[0])
  })

  it('archives and restores an item', async () => {
    const calls: string[] = []
    server.use(
      http.post(`${INV}/items/${ITEM}/archive`, () => (calls.push('archive'), HttpResponse.json(item()))),
      http.post(`${INV}/items/${ITEM}/restore`, () => (calls.push('restore'), HttpResponse.json(item()))),
    )
    await runTool(tool('inventory_item_archive'), { item_id: ITEM }, ctx())
    await runTool(tool('inventory_item_archive'), { item_id: ITEM, restore: true }, ctx())
    expect(calls).toEqual(['archive', 'restore'])
  })

  it("lists an item's or a supplier's offers, or an offer's prices", async () => {
    const asked: string[] = []
    server.use(
      http.get(`${INV}/items/${ITEM}/offers`, ({ request }) => (asked.push(`item ${new URL(request.url).search}`), HttpResponse.json({ offers: [] }))),
      http.get(`${INV}/suppliers/${SUPPLIER}/offers`, () => (asked.push('supplier'), HttpResponse.json({ offers: [] }))),
      http.get(`${INV}/offers/${OFFER}/prices`, () => (asked.push('prices'), HttpResponse.json({ prices: [{ id: 'p1', price: '4.99' }] }))),
    )
    await runTool(tool('inventory_offer_list'), { item_id: ITEM, archived: true }, ctx())
    await runTool(tool('inventory_offer_list'), { supplier_id: SUPPLIER }, ctx())
    expect(firstText(await runTool(tool('inventory_offer_list'), { offer_id: OFFER }, ctx()))).toEqual({ prices: [{ id: 'p1', price: '4.99' }] })
    expect(asked).toEqual(['item ?archived=true', 'supplier', 'prices'])
    await expect(tool('inventory_offer_list').execute({}, ctx())).rejects.toThrow('one of')
  })

  it('adds, edits and archives an offer, and adds a pack price', async () => {
    const calls: string[] = []
    const offer = { id: OFFER, item_id: ITEM, supplier_id: SUPPLIER }
    server.use(
      http.post(`${INV}/offers`, async ({ request }) => (calls.push(`POST ${JSON.stringify(await request.json())}`), HttpResponse.json(offer))),
      http.patch(`${INV}/offers/${OFFER}`, async ({ request }) => (calls.push(`PATCH ${JSON.stringify(await request.json())}`), HttpResponse.json(offer))),
      http.post(`${INV}/offers/${OFFER}/archive`, () => (calls.push('archive'), HttpResponse.json(offer))),
      http.post(`${INV}/offers/${OFFER}/prices`, async ({ request }) => (calls.push(`price ${JSON.stringify(await request.json())}`), HttpResponse.json({ id: 'p1' }))),
    )
    const made = firstText(
      await runTool(tool('inventory_offer_upsert'), { fields: { item_id: ITEM, supplier_id: SUPPLIER, sku: 'N3', pack_size: '100' } }, ctx()),
    )
    expect(made).toMatchObject({ ui_path: `/inventory/items/${ITEM}#offer-${OFFER}`, item_ui_path: `/inventory/items/${ITEM}` })
    await runTool(tool('inventory_offer_upsert'), { offer_id: OFFER, fields: { url: null }, archived: true }, ctx())
    await runTool(tool('inventory_offer_price_add'), { offer_id: OFFER, price: '4.99', currency: 'EUR' }, ctx())
    expect(calls).toEqual([
      `POST {"item_id":"${ITEM}","supplier_id":"${SUPPLIER}","sku":"N3","pack_size":"100"}`,
      'PATCH {"url":null}',
      'archive',
      'price {"price":"4.99","currency":"EUR"}',
    ])
    await expect(tool('inventory_offer_upsert').execute({ fields: { sku: 'N3' } }, ctx())).rejects.toThrow('supplier_id')
    await expect(tool('inventory_offer_upsert').execute({ offer_id: OFFER, fields: { item_id: ITEM } }, ctx())).rejects.toThrow('do not change')
    await expect(tool('inventory_offer_upsert').execute({ offer_id: OFFER, fields: { supplier_id: SUPPLIER } }, ctx())).rejects.toThrow('do not change')
    await expect(
      tool('inventory_offer_upsert').execute({ fields: { item_id: ITEM, supplier_id: SUPPLIER, sku: 'N3' }, archived: true }, ctx()),
    ).rejects.toThrow('archived needs an offer_id')
    expect(calls).toHaveLength(4)
  })

  it("sends each offer write of a call its own Idempotency-Key, and a retried call's the same (#8437)", async () => {
    const keys: string[] = []
    const offer = { id: OFFER, item_id: ITEM, supplier_id: SUPPLIER }
    const keep = (request: Request, body: Record<string, unknown>) => {
      keys.push(`${request.method} ${new URL(request.url).pathname} ${request.headers.get('Idempotency-Key')}`)
      return HttpResponse.json(body)
    }
    server.use(
      http.post(`${INV}/offers`, ({ request }) => keep(request, offer)),
      http.patch(`${INV}/offers/${OFFER}`, ({ request }) => keep(request, offer)),
      http.post(`${INV}/offers/${OFFER}/archive`, ({ request }) => keep(request, offer)),
      http.post(`${INV}/offers/${OFFER}/prices`, ({ request }) => keep(request, { id: 'p1' })),
    )
    const add = { fields: { item_id: ITEM, supplier_id: SUPPLIER, sku: 'N3' } }
    const edit = { offer_id: OFFER, fields: { note: 'n' }, archived: true as const }
    const price = { offer_id: OFFER, price: '4.99' }
    const added = { ...ctx(), callKey: 'session-1:offer-1' }
    const edited = { ...ctx(), callKey: 'session-1:offer-2' }
    const priced = { ...ctx(), callKey: 'session-1:offer-3' }
    await runTool(tool('inventory_offer_upsert'), add, added)
    await runTool(tool('inventory_offer_upsert'), add, added)
    await runTool(tool('inventory_offer_upsert'), edit, edited)
    await runTool(tool('inventory_offer_upsert'), edit, edited)
    await runTool(tool('inventory_offer_price_add'), price, priced)
    await runTool(tool('inventory_offer_price_add'), price, priced)
    const [made, madeAgain, patch, archive, patchAgain, archiveAgain, first, firstAgain] = keys
    expect(made).toMatch(/^POST \/api\/v1\/inventory\/offers [0-9a-f]{64}:add$/)
    expect(patch).toMatch(/^PATCH \/api\/v1\/inventory\/offers\/[^ ]+ [0-9a-f]{64}:edit$/)
    expect(archive).toMatch(/^POST \/api\/v1\/inventory\/offers\/[^ ]+\/archive [0-9a-f]{64}:archive$/)
    expect(first).toMatch(/^POST \/api\/v1\/inventory\/offers\/[^ ]+\/prices [0-9a-f]{64}:price$/)
    // A retried call sends the same keys, so the API answers the first write and writes nothing.
    expect([madeAgain, patchAgain, archiveAgain, firstAgain]).toEqual([made, patch, archive, first])
    // Two writes of one call share the call's key and differ by step; two calls never share one.
    const callOf = (line: string | undefined) => line!.split(' ')[2]!.split(':')[0]
    expect(callOf(patch)).toBe(callOf(archive))
    expect(new Set([callOf(made), callOf(patch), callOf(first)]).size).toBe(3)
  })

  it('receives, edits, re-costs, uses and counts lots through their routes', async () => {
    const calls: string[] = []
    const body = async (what: string, request: Request) => calls.push(`${what} ${JSON.stringify(await request.json())}`)
    server.use(
      http.post(`${INV}/items/${ITEM}/lots`, async ({ request }) => (await body('receive', request), HttpResponse.json({ id: LOT, item_id: ITEM }))),
      http.patch(`${INV}/lots/${LOT}`, async ({ request }) => (await body('patch', request), HttpResponse.json({ id: LOT, item_id: ITEM }))),
      http.post(`${INV}/lots/${LOT}/recost`, () => (calls.push('recost'), HttpResponse.json({ item_id: ITEM, moves: 2, orders: [] }))),
      http.post(`${INV}/items/${ITEM}/use`, async ({ request }) => (await body('use', request), HttpResponse.json({ moves: [] }))),
      http.post(`${INV}/items/${ITEM}/count`, async ({ request }) => (await body('count', request), HttpResponse.json({ moves: [] }))),
      http.post(`${INV}/lots/${LOT}/adjust`, async ({ request }) => (await body('adjust', request), HttpResponse.json({ id: LOT, item_id: ITEM }))),
    )
    await runTool(tool('inventory_lot_receive'), { item_id: ITEM, qty: 100, total_cost_minor: 499, location_id: PLACE }, ctx())
    await runTool(tool('inventory_lot_update'), { lot_id: LOT, unit_cost_minor: '4.99', location_id: null }, ctx())
    expect(firstText(await runTool(tool('inventory_lot_recost'), { lot_id: LOT }, ctx()))).toMatchObject({ item_ui_path: `/inventory/items/${ITEM}` })
    await runTool(tool('inventory_move_use'), { item_id: ITEM, qty: '2', lot_id: LOT, entity: 'printer:7', reason: 'fan' }, ctx())
    await runTool(tool('inventory_move_count'), { item_id: ITEM, on_hand: 40, note: 'shelf' }, ctx())
    await runTool(tool('inventory_move_count'), { lot_id: LOT, on_hand: '3', note: 'shelf' }, ctx())
    expect(calls).toEqual([
      `receive {"qty":100,"total_cost_minor":499,"location_id":"${PLACE}"}`,
      'patch {"unit_cost_minor":"4.99","location_id":null}',
      'recost',
      `use {"qty":"2","lot_id":"${LOT}","entity":"printer:7","reason":"fan"}`,
      'count {"on_hand":40,"note":"shelf"}',
      'adjust {"on_hand":"3","note":"shelf"}',
    ])
    expect(() => tool('inventory_lot_receive').parse({ item_id: ITEM, qty: 1, unit_cost_minor: 1, total_cost_minor: 1 })).toThrow()
    expect(() => tool('inventory_move_use').parse({ item_id: ITEM, qty: '-1' })).toThrow()
    expect(() => tool('inventory_move_use').parse({ item_id: ITEM, qty: 0.1234567 })).toThrow()
    expect(() => tool('inventory_move_use').parse({ item_id: ITEM, qty: 0.123456 })).not.toThrow()
    expect(() => tool('inventory_move_use').parse({ item_id: ITEM, qty: 1e-7 })).toThrow()
    expect(() => tool('inventory_offer_price_add').parse({ offer_id: OFFER, price: 4.99999 })).toThrow()
    expect(() => tool('inventory_offer_price_add').parse({ offer_id: OFFER, price: 4.9999 })).not.toThrow()
    expect(() => tool('inventory_item_upsert').parse({ item_id: ITEM, fields: { life_hours: '1.255' } })).toThrow()
    expect(() => tool('inventory_item_upsert').parse({ item_id: ITEM, fields: { life_hours: '12345678901' } })).toThrow()
    expect(() => tool('inventory_item_upsert').parse({ item_id: ITEM, fields: { life_hours: 1234567890.25 } })).not.toThrow()
    expect(() => tool('inventory_item_upsert').parse({ item_id: ITEM, fields: { life_hours: 0 } })).toThrow()
    await expect(tool('inventory_move_count').execute({ item_id: ITEM, lot_id: LOT, on_hand: 1, note: 'x' }, ctx())).rejects.toThrow('one of')
    await expect(tool('inventory_lot_update').execute({ lot_id: LOT }, ctx())).rejects.toThrow('nothing to change')
  })

  it('says a write past the wait still runs, names its operation and sends it once (#6949 K1)', async () => {
    let uses = 0
    let patches = 0
    const accepted = (id: string) =>
      HttpResponse.json({ id, kind: 'inventory.Take', subject: `item:${ITEM}`, status: 'running', created_at: '2026-11-26T12:00:00Z', repeated: false }, { status: 202 })
    server.use(
      http.post(`${INV}/items/${ITEM}/use`, () => (uses++, accepted('req:op-1'))),
      http.post(`${INV}/items`, () => accepted('req:op-2')),
      http.patch(props, () => (patches++, HttpResponse.json({ properties: [] }))),
    )

    const used = firstText(await runTool(tool('inventory_move_use'), { item_id: ITEM, qty: '2' }, ctx()))
    const made = firstText(await runTool(tool('inventory_item_upsert'), { fields: { kind_id: 'hardware', name: 'M3 nut' }, properties: { thread: 'M3' } }, ctx()))

    expect(used).toMatchObject({ still_running: true, operation_id: 'req:op-1' })
    expect((used as { note: string }).note).toContain('still running, operation req:op-1')
    expect(uses).toBe(1)
    expect(made).toMatchObject({ still_running: true, operation_id: 'req:op-2' })
    expect((made as { note: string }).note).not.toContain('The properties were not set')
    expect(patches).toBe(0)
  })

  it("names Go's codes for a stock call on the wrong item", () => {
    expect(tool('inventory_lot_receive').description).toContain('`wrong_counting`')
    expect(tool('inventory_lot_receive').description).toContain('`step_not_applicable`')
    expect(tool('inventory_unit_receive').description).toContain('`wrong_counting`')
    for (const t of ALL_TOOLS.filter((x) => x.name.startsWith('inventory_'))) {
      expect(t.description, t.name).not.toMatch(/wrong_tracking|item_not_counted/)
    }
  })

  it('receives units, edits one and changes its status', async () => {
    const calls: string[] = []
    const body = async (what: string, request: Request) => calls.push(`${what} ${JSON.stringify(await request.json())}`)
    server.use(
      http.post(`${INV}/items/${ITEM}/units`, async ({ request }) => (await body('receive', request), HttpResponse.json({ units: [{ id: UNIT, item_id: ITEM }] }))),
      http.patch(`${INV}/units/${UNIT}`, async ({ request }) => (await body('patch', request), HttpResponse.json({ id: UNIT, item_id: ITEM }))),
      http.post(`${INV}/units/${UNIT}/status`, async ({ request }) => (await body('status', request), HttpResponse.json({ id: UNIT, item_id: ITEM }))),
    )
    const made = firstText(await runTool(tool('inventory_unit_receive'), { item_id: ITEM, count: 1, cost_minor: 2500, serials: ['S1'] }, ctx()))
    expect(made).toMatchObject({ units: [{ ui_path: `/inventory/units/${UNIT}` }] })
    await runTool(tool('inventory_unit_update'), { unit_id: UNIT, serial: null }, ctx())
    await runTool(tool('inventory_unit_status'), { unit_id: UNIT, status: 'in_service', entity: 'printer:7' }, ctx())
    expect(calls).toEqual([
      'receive {"count":1,"cost_minor":2500,"serials":["S1"]}',
      'patch {"serial":null}',
      'status {"status":"in_service","entity":"printer:7"}',
    ])
    await expect(tool('inventory_unit_update').execute({ unit_id: UNIT }, ctx())).rejects.toThrow('nothing to change')
  })

  it('lists moves by the filters given', async () => {
    let search = ''
    server.use(
      http.get(`${INV}/moves`, ({ request }) => {
        search = new URL(request.url).search
        return HttpResponse.json({ moves: [{ id: 'm1', item_id: ITEM, unit_id: UNIT }] })
      }),
    )
    const answer = firstText(await runTool(tool('inventory_move_list'), { order_item_id: 'oi1' }, ctx()))
    expect(search).toBe('?order_item_id=oi1&limit=50')
    expect(answer).toMatchObject({ moves: [{ item_ui_path: `/inventory/items/${ITEM}`, unit_ui_path: `/inventory/units/${UNIT}` }] })
  })
})
