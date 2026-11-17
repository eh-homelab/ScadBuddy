import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6266: suppliers, products and price points (backend/scadbuddy/api/filament_purchasing.py).

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const SUPPLIER = '8d6f3c1e-0000-4000-8000-000000006601'
const PRODUCT = '8d6f3c1e-0000-4000-8000-000000006602'
const TYPE = '8d6f3c1e-0000-4000-8000-000000006603'
const FILAMENT = `${BACKEND}/api/v1/filament`

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

describe('filament purchasing tools', () => {
  it.each([
    ['filament_supplier_list', 'read'],
    ['filament_supplier_upsert', 'write'],
    ['filament_product_list', 'read'],
    ['filament_product_upsert', 'write'],
    ['filament_price_list', 'read'],
    ['filament_price_add', 'write'],
    ['filament_price_delete', 'write'],
    ['filament_price_sheet', 'read'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('adds a manual price with the price as a string', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${FILAMENT}/products/${PRODUCT}/prices`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: PRODUCT, price: '21.4000' }, { status: 201 })
      }),
    )
    const made = await runTool(
      tool('filament_price_add'),
      { product_id: PRODUCT, price: '21.40', on_date: '2026-11-17' },
      ctx(),
    )
    expect(bodies).toEqual([{ price: '21.40', on_date: '2026-11-17' }])
    expect(firstText(made)).toEqual({ id: PRODUCT, price: '21.4000' })
  })

  it('reads one supplier or the list', async () => {
    server.use(
      http.get(`${FILAMENT}/suppliers/${SUPPLIER}`, () => HttpResponse.json({ id: SUPPLIER })),
      http.get(`${FILAMENT}/suppliers`, () => HttpResponse.json([{ id: SUPPLIER }])),
    )
    expect(firstText(await runTool(tool('filament_supplier_list'), { supplier_id: SUPPLIER }, ctx()))).toEqual({
      id: SUPPLIER,
    })
    expect(firstText(await runTool(tool('filament_supplier_list'), {}, ctx()))).toEqual([{ id: SUPPLIER }])
  })

  it('lists prices by product or by type, never neither', async () => {
    server.use(
      http.get(`${FILAMENT}/products/${PRODUCT}/prices`, () => HttpResponse.json([{ id: 'p' }])),
      http.get(`${FILAMENT}/types/${TYPE}/prices`, () => HttpResponse.json([{ id: 't' }])),
    )
    expect(firstText(await runTool(tool('filament_price_list'), { product_id: PRODUCT }, ctx()))).toEqual([{ id: 'p' }])
    expect(firstText(await runTool(tool('filament_price_list'), { type_id: TYPE }, ctx()))).toEqual([{ id: 't' }])
    const neither = await runTool(tool('filament_price_list'), {}, ctx())
    expect(neither.isError).toBe(true)
  })

  it('adds a product to a supplier and archives one', async () => {
    const seen: unknown[] = []
    server.use(
      http.post(`${FILAMENT}/suppliers/${SUPPLIER}/products`, async ({ request }) => {
        seen.push(await request.json())
        return HttpResponse.json({ id: PRODUCT }, { status: 201 })
      }),
      http.patch(`${FILAMENT}/products/${PRODUCT}`, async ({ request }) => {
        seen.push(await request.json())
        return HttpResponse.json({ id: PRODUCT })
      }),
    )
    await runTool(tool('filament_product_upsert'), { supplier_id: SUPPLIER, fields: { type_id: TYPE, sku: 'A' } }, ctx())
    await runTool(tool('filament_product_upsert'), { product_id: PRODUCT, fields: {}, archived: true }, ctx())
    expect(seen).toEqual([{ type_id: TYPE, sku: 'A' }, { archived: true }])
  })

  it('refuses archived without an id, and a product with no parent', async () => {
    const supplier = await runTool(tool('filament_supplier_upsert'), { fields: { name: 'A' }, archived: true }, ctx())
    expect(supplier.isError).toBe(true)
    const noParent = await runTool(tool('filament_product_upsert'), { fields: { sku: 'A' } }, ctx())
    expect(noParent.isError).toBe(true)
    const noArchive = await runTool(
      tool('filament_product_upsert'),
      { supplier_id: SUPPLIER, fields: {}, archived: true },
      ctx(),
    )
    expect(noArchive.isError).toBe(true)
  })

  it('deletes a manual price and answers its id', async () => {
    const PRICE = '8d6f3c1e-0000-4000-8000-000000006604'
    server.use(http.delete(`${FILAMENT}/prices/${PRICE}`, () => new HttpResponse(null, { status: 204 })))
    expect(firstText(await runTool(tool('filament_price_delete'), { price_id: PRICE }, ctx()))).toEqual({
      deleted: PRICE,
    })
  })
})
