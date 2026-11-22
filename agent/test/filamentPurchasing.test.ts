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
    ['filament_invoice_list', 'read'],
    ['filament_invoice_get', 'read'],
    ['filament_bambu_colour', 'read'],
    ['filament_invoice_candidates', 'read'],
    ['filament_invoice_upsert', 'write'],
    ['filament_invoice_delete', 'write'],
    ['filament_shopping_list', 'read'],
    ['filament_shopping_add', 'write'],
    ['filament_shopping_update', 'write'],
    ['filament_shopping_remove', 'write'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('filament_invoice_confirm is outward tier, and a gated call', () => {
    expect(tool('filament_invoice_confirm').risk).toBe('outward')
    expect(tool('filament_invoice_confirm').gated).toBe(true)
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
      ui_path: `/filament/suppliers/${SUPPLIER}`,
    })
    expect(firstText(await runTool(tool('filament_supplier_list'), {}, ctx()))).toEqual([{ id: SUPPLIER, ui_path: `/filament/suppliers/${SUPPLIER}` }])
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

  it('adds an invoice with its lines and edits one by header then lines', async () => {
    const INVOICE = '8d6f3c1e-0000-4000-8000-000000006605'
    const seen: unknown[] = []
    server.use(
      http.post(`${FILAMENT}/invoices`, async ({ request }) => {
        seen.push(['POST', await request.json()])
        return HttpResponse.json({ id: INVOICE }, { status: 201 })
      }),
      http.patch(`${FILAMENT}/invoices/${INVOICE}`, async ({ request }) => {
        seen.push(['PATCH', await request.json()])
        return HttpResponse.json({ id: INVOICE })
      }),
      http.put(`${FILAMENT}/invoices/${INVOICE}/lines`, async ({ request }) => {
        seen.push(['PUT', await request.json()])
        return HttpResponse.json({ id: INVOICE, lines: [] })
      }),
    )
    const line = { description: 'PLA', quantity: 1, unit_price: '40.00' }
    const made = await runTool(
      tool('filament_invoice_upsert'),
      { fields: { supplier_id: SUPPLIER, total: '40.00' }, lines: [line] },
      ctx(),
    )
    expect(firstText(made)).toEqual({ id: INVOICE, ui_path: `/filament/invoices/${INVOICE}` })
    await runTool(
      tool('filament_invoice_upsert'),
      { invoice_id: INVOICE, fields: { shipping: '5.00' }, lines: [line] },
      ctx(),
    )
    expect(seen).toEqual([
      ['POST', { supplier_id: SUPPLIER, total: '40.00', lines: [line] }],
      ['PATCH', { shipping: '5.00' }],
      ['PUT', [line]],
    ])
    const nothing = await runTool(tool('filament_invoice_upsert'), { invoice_id: INVOICE }, ctx())
    expect(nothing.isError).toBe(true)
  })

  it('never lets the model set the import link (#6267)', async () => {
    const INVOICE = '8d6f3c1e-0000-4000-8000-000000006605'
    const seen: unknown[] = []
    server.use(
      http.post(`${FILAMENT}/invoices`, async ({ request }) => {
        seen.push(await request.json())
        return HttpResponse.json({ id: INVOICE })
      }),
      http.patch(`${FILAMENT}/invoices/${INVOICE}`, async ({ request }) => {
        seen.push(await request.json())
        return HttpResponse.json({ id: INVOICE })
      }),
    )
    for (const fields of [{ import_run_id: 's-9' }, { if_import_run_id: null, note: 'x' }]) {
      const edit = await runTool(tool('filament_invoice_upsert'), { invoice_id: INVOICE, fields }, ctx())
      expect(edit.isError).toBe(true)
      const add = await runTool(tool('filament_invoice_upsert'), { fields: { supplier_id: SUPPLIER, ...fields } }, ctx())
      expect(add.isError).toBe(true)
    }
    expect(seen).toEqual([])
    expect(tool('filament_invoice_upsert').description).not.toContain('`import_run_id`')
  })

  it('deletes an invoice draft and answers its id', async () => {
    const INVOICE = '8d6f3c1e-0000-4000-8000-000000006605'
    server.use(http.delete(`${FILAMENT}/invoices/${INVOICE}`, () => new HttpResponse(null, { status: 204 })))
    expect(firstText(await runTool(tool('filament_invoice_delete'), { invoice_id: INVOICE }, ctx()))).toEqual({
      deleted: INVOICE,
    })
  })

  it('confirms an invoice by posting to its confirm route', async () => {
    const INVOICE = '8d6f3c1e-0000-4000-8000-000000006606'
    const posted: string[] = []
    server.use(
      http.post(`${FILAMENT}/invoices/${INVOICE}/confirm`, () => {
        posted.push(INVOICE)
        return HttpResponse.json({ id: INVOICE, status: 'confirmed' })
      }),
    )
    // An outward call parks for approval; lowered to write, it runs and reaches the route.
    const lowered = {
      of: async () => ({ tier: 'write' as const, disabled: false }),
      disabled: async () => new Set<string>(),
    }
    const result = await runTool(tool('filament_invoice_confirm'), { invoice_id: INVOICE }, {
      ...ctx(),
      ...services({ toolOverrides: lowered }),
    })
    expect(result.isError).toBeFalsy()
    expect(posted).toEqual([INVOICE])
    expect(firstText(result)).toMatchObject({ status: 'confirmed' })
  })

  it('reads, adds and changes shopping list items', async () => {
    const ITEM = '8d6f3c1e-0000-4000-8000-000000006606'
    let seen: string | null = null
    server.use(
      http.get(`${FILAMENT}/shopping-list`, ({ request }) => {
        seen = new URL(request.url).searchParams.get('status')
        return HttpResponse.json([])
      }),
      http.post(`${FILAMENT}/shopping-list`, async ({ request }) => {
        expect(await request.json()).toEqual({ type_id: TYPE, quantity_spools: 2 })
        return HttpResponse.json({ id: ITEM }, { status: 201 })
      }),
      http.patch(`${FILAMENT}/shopping-list/${ITEM}`, async ({ request }) => {
        expect(await request.json()).toEqual({ status: 'purchased', note: null })
        return HttpResponse.json({ id: ITEM })
      }),
    )
    expect(firstText(await runTool(tool('filament_shopping_list'), { status: 'pending' }, ctx()))).toEqual([])
    expect(seen).toBe('pending')
    expect(
      firstText(await runTool(tool('filament_shopping_add'), { type_id: TYPE, quantity_spools: 2 }, ctx())),
    ).toEqual({ id: ITEM, ui_path: '/filament/shopping-list' })
    expect(
      firstText(
        await runTool(tool('filament_shopping_update'), { item_id: ITEM, status: 'purchased', note: null }, ctx()),
      ),
    ).toEqual({ id: ITEM, ui_path: '/filament/shopping-list' })
  })

  it('removes one shopping list item or clears the list', async () => {
    const ITEM = '8d6f3c1e-0000-4000-8000-000000006607'
    let purchasedOnly: string | null = null
    server.use(
      http.delete(`${FILAMENT}/shopping-list/${ITEM}`, () => new HttpResponse(null, { status: 204 })),
      http.delete(`${FILAMENT}/shopping-list`, ({ request }) => {
        purchasedOnly = new URL(request.url).searchParams.get('purchased_only')
        return HttpResponse.json({ removed: 2 })
      }),
    )
    expect(firstText(await runTool(tool('filament_shopping_remove'), { item_id: ITEM }, ctx()))).toEqual({
      removed: 1,
    })
    expect(
      firstText(await runTool(tool('filament_shopping_remove'), { clear: 'purchased' }, ctx())),
    ).toEqual({ removed: 2 })
    expect(purchasedOnly).toBe('true')
    expect(firstText(await runTool(tool('filament_shopping_remove'), { clear: 'all' }, ctx()))).toEqual({
      removed: 2,
    })
    expect(purchasedOnly).toBe('false')
    const both = await runTool(tool('filament_shopping_remove'), { item_id: ITEM, clear: 'all' }, ctx())
    expect(both.isError).toBe(true)
  })

  it('never clears the shopping list when no item_id and no clear is given', async () => {
    let requests = 0
    server.use(
      http.delete(`${FILAMENT}/shopping-list`, () => {
        requests += 1
        return HttpResponse.json({ removed: 9 })
      }),
    )
    const none = await runTool(tool('filament_shopping_remove'), {}, ctx())
    expect(none.isError).toBe(true)
    const old = await runTool(tool('filament_shopping_remove'), { purchased_only: true }, ctx())
    expect(old.isError).toBe(true)
    expect(requests).toBe(0)
  })

  it('says a confirmed invoice still takes a note-only edit', () => {
    expect(tool('filament_invoice_upsert').description).toContain('note-only edit')
  })

  it('reads the stats, a price sheet, the invoices and one invoice, and the products', async () => {
    const INVOICE = '8d6f3c1e-0000-4000-8000-000000006609'
    let invoiceQuery = ''
    let archived: string | null = null
    server.use(
      http.get(`${FILAMENT}/suppliers/stats`, () => HttpResponse.json([{ supplier_id: SUPPLIER }])),
      http.get(`${FILAMENT}/suppliers/${SUPPLIER}/price-sheet`, () => HttpResponse.json([{ change_pct: null }])),
      http.get(`${FILAMENT}/suppliers/${SUPPLIER}/products`, ({ request }) => {
        archived = new URL(request.url).searchParams.get('include_archived')
        return HttpResponse.json([{ id: PRODUCT }])
      }),
      http.get(`${FILAMENT}/invoices/${INVOICE}`, () => HttpResponse.json({ id: INVOICE, lines: [] })),
      http.get(`${FILAMENT}/invoices`, ({ request }) => {
        invoiceQuery = new URL(request.url).search
        return HttpResponse.json([{ id: INVOICE }])
      }),
    )
    expect(firstText(await runTool(tool('filament_supplier_stats'), {}, ctx()))).toEqual([
      { supplier_id: SUPPLIER, supplier_ui_path: `/filament/suppliers/${SUPPLIER}` },
    ])
    expect(firstText(await runTool(tool('filament_price_sheet'), { supplier_id: SUPPLIER }, ctx()))).toEqual([
      { change_pct: null },
    ])
    expect(
      firstText(await runTool(tool('filament_product_list'), { supplier_id: SUPPLIER, include_archived: true }, ctx())),
    ).toEqual([{ id: PRODUCT }])
    expect(archived).toBe('true')
    expect(
      firstText(await runTool(tool('filament_invoice_list'), { supplier_id: SUPPLIER, status: 'draft' }, ctx())),
    ).toEqual([{ id: INVOICE, ui_path: `/filament/invoices/${INVOICE}` }])
    expect(invoiceQuery).toContain(`supplier_id=${SUPPLIER}`)
    expect(invoiceQuery).toContain('status=draft')
    expect(firstText(await runTool(tool('filament_invoice_get'), { invoice_id: INVOICE }, ctx()))).toEqual({
      id: INVOICE,
      ui_path: `/filament/invoices/${INVOICE}`,
      lines: [],
    })
  })

  it('adds a supplier with its fields and edits one with archived', async () => {
    const seen: unknown[] = []
    server.use(
      http.post(`${FILAMENT}/suppliers`, async ({ request }) => {
        seen.push(await request.json())
        return HttpResponse.json({ id: SUPPLIER }, { status: 201 })
      }),
      http.patch(`${FILAMENT}/suppliers/${SUPPLIER}`, async ({ request }) => {
        seen.push(await request.json())
        return HttpResponse.json({ id: SUPPLIER })
      }),
    )
    await runTool(
      tool('filament_supplier_upsert'),
      { fields: { name: 'Acme', website: 'https://acme.test', note: null } },
      ctx(),
    )
    await runTool(tool('filament_supplier_upsert'), { supplier_id: SUPPLIER, fields: { note: 'x' }, archived: true }, ctx())
    expect(seen).toEqual([{ name: 'Acme', website: 'https://acme.test', note: null }, { note: 'x', archived: true }])
  })

  it('looks a Bambu colour code up by the `code` query', async () => {
    let seen: string | null = null
    server.use(
      http.get(`${FILAMENT}/bambu-colour-codes`, ({ request }) => {
        seen = new URL(request.url).searchParams.get('code')
        return HttpResponse.json({ code: '10100', name: 'Jade White', rgba: ['#FFFFFFFF'], fila_type: 'PLA', fila_id: 'GFA00' })
      }),
    )
    const result = await runTool(tool('filament_bambu_colour'), { code: '10100' }, ctx())
    expect(seen).toBe('10100')
    expect(firstText(result)).toMatchObject({ name: 'Jade White', fila_id: 'GFA00' })
    expect((await runTool(tool('filament_bambu_colour'), {}, ctx())).isError).toBe(true)
  })

  it('explains a colour code the table does not have', async () => {
    server.use(http.get(`${FILAMENT}/bambu-colour-codes`, () => HttpResponse.json({ detail: 'no such code' }, { status: 404 })))
    const result = await runTool(tool('filament_bambu_colour'), { code: '99999' }, ctx())
    expect(result.isError).toBe(true)
  })

  it("reads an invoice's line candidates from its candidates route", async () => {
    const INVOICE = '8d6f3c1e-0000-4000-8000-000000006610'
    server.use(
      http.get(`${FILAMENT}/invoices/${INVOICE}/candidates`, () => HttpResponse.json([{ position: 1, uncosted: [], costed: [] }])),
    )
    const result = await runTool(tool('filament_invoice_candidates'), { invoice_id: INVOICE }, ctx())
    expect(firstText(result)).toMatchObject([{ position: 1 }])
    expect((await runTool(tool('filament_invoice_candidates'), { invoice_id: 'nope' }, ctx())).isError).toBe(true)
  })
})
