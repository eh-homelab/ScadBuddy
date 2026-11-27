import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { filamentPaths, UI_PATH_NOTE, withUiPaths } from '../src/tools/filamentLinks.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6869 (R-6869-4): the filament tools answer each entity's page as `ui_path`.

interface Vector {
  kind: 'spool' | 'type' | 'supplier' | 'invoice' | 'location' | 'preset' | 'catalog'
  key: string
  path: string
}
// The frontend's `lib/filamentLinks.ts` is tested against the same vectors.
const vectors = JSON.parse(readFileSync(`${import.meta.dirname}/../../test-fixtures/filament/ui-paths.json`, 'utf8')) as Vector[]

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const FILAMENT = `${BACKEND}/api/v1/filament`
const SPOOL = '8d6f3c1e-0000-4000-8000-000000006869'
const TYPE = '8d6f3c1e-0000-4000-8000-000000006870'
const SUPPLIER = '8d6f3c1e-0000-4000-8000-000000006871'
const INVOICE = '8d6f3c1e-0000-4000-8000-000000006872'
const LINE = '8d6f3c1e-0000-4000-8000-000000006873'
const PRODUCT = '8d6f3c1e-0000-4000-8000-000000006874'

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

describe('filament ui paths (#6869)', () => {
  it.each(vectors)("$kind $key is the frontend's $path", ({ kind, key, path }) => {
    expect(filamentPaths[kind](key)).toBe(path)
  })

  it("gives a spool its page, its type's and its place's", async () => {
    server.use(
      http.get(`${FILAMENT}/spools/${SPOOL}`, () =>
        HttpResponse.json({ id: SPOOL, number: 12, type_id: TYPE, location_id: 'dry-box', names: { location: 'Dry box' }, invoice: { invoice_id: INVOICE, line_id: LINE, supplier_id: SUPPLIER } }),
      ),
    )
    expect(firstText(await runTool(tool('filament_spool_get'), { spool_id: SPOOL }, ctx()))).toEqual({
      id: SPOOL,
      ui_path: `/filament/spools/${SPOOL}`,
      number: 12,
      type_id: TYPE,
      type_ui_path: `/filament/types/${TYPE}`,
      location_id: 'dry-box',
      location_ui_path: '/filament/locations/dry-box',
      names: { location: 'Dry box' },
      invoice: {
        invoice_id: INVOICE,
        invoice_ui_path: `/filament/invoices/${INVOICE}`,
        line_id: LINE,
        supplier_id: SUPPLIER,
        supplier_ui_path: `/filament/suppliers/${SUPPLIER}`,
        invoice_line_ui_path: `/filament/invoices/${INVOICE}#line-${LINE}`,
      },
    })
  })

  it("gives an invoice's lines their anchors and their spools' pages", async () => {
    server.use(
      http.get(`${FILAMENT}/invoices/${INVOICE}`, () =>
        HttpResponse.json({ id: INVOICE, supplier_id: SUPPLIER, lines: [{ id: LINE, invoice_id: INVOICE, spool_ids: [SPOOL] }] }),
      ),
    )
    const invoice = firstText(await runTool(tool('filament_invoice_get'), { invoice_id: INVOICE }, ctx())) as {
      ui_path: string
      lines: Record<string, unknown>[]
    }
    expect(invoice.ui_path).toBe(`/filament/invoices/${INVOICE}`)
    expect(invoice.lines[0]).toMatchObject({
      ui_path: `/filament/invoices/${INVOICE}#line-${LINE}`,
      spool_ui_paths: [`/filament/spools/${SPOOL}`],
    })
  })

  it('places each node of the location tree at its own page', async () => {
    server.use(http.get(`${FILAMENT}/locations`, () => HttpResponse.json([{ id: 'a', name: 'Shelf', children: [{ id: 'b', name: 'Box 1', children: [] }] }])))
    const [shelf] = firstText(await runTool(tool('filament_location_list'), {}, ctx())) as { ui_path: string; children: { ui_path: string }[] }[]
    expect(shelf!.ui_path).toBe('/filament/locations/a')
    expect(shelf!.children[0]!.ui_path).toBe('/filament/locations/b')
  })

  it("gives one place, its path, its children and its spools their pages (#6955)", async () => {
    const PLACE = '0badc0de-0000-4000-8000-0000000000a1'
    server.use(
      http.get(`${FILAMENT}/locations/${PLACE}`, () =>
        HttpResponse.json({ id: PLACE, name: 'Box', path: [{ id: 'r', name: 'Shelf' }], children: [{ id: 'b', name: 'Bag' }], spools: [{ id: SPOOL, number: 12 }] }),
      ),
    )
    const place = firstText(await runTool(tool('filament_location_get'), { location_id: PLACE }, ctx())) as {
      ui_path: string
      path: { ui_path: string }[]
      children: { ui_path: string }[]
      spools: { ui_path: string }[]
    }
    expect(place.ui_path).toBe(`/filament/locations/${PLACE}`)
    expect(place.path[0]!.ui_path).toBe('/filament/locations/r')
    expect(place.children[0]!.ui_path).toBe('/filament/locations/b')
    expect(place.spools[0]!.ui_path).toBe(`/filament/spools/${SPOOL}`)
  })

  it("anchors a price sheet's product on its supplier's page", () => {
    const [row] = withUiPaths([{ product: { id: PRODUCT, supplier_id: SUPPLIER, type_id: TYPE } }], 'price_sheet_row') as { product: Record<string, unknown> }[]
    expect(row!.product.ui_path).toBe(`/filament/suppliers/${SUPPLIER}#product-${PRODUCT}`)
  })

  it.each(vectors.filter((one) => one.kind === 'catalog'))('gives a $key row from filament_catalog_list the frontend\'s $path', async ({ key, path }) => {
    server.use(http.get(`${FILAMENT}/catalog/${key}`, () => HttpResponse.json([{ id: 'row-1', name: 'Cardboard spool' }])))
    const [row] = firstText(await runTool(tool('filament_catalog_list'), { kind: key }, ctx())) as Record<string, unknown>[]
    expect(row!.ui_path).toBe(path)
  })

  it("links an automatic shopping item to the spool that ran out", () => {
    const [item] = withUiPaths([{ id: LINE, material: 'PLA', empty_spool_id: SPOOL, type_id: TYPE }], 'shopping_item') as Record<string, unknown>[]
    expect(item!.empty_spool_ui_path).toBe(`/filament/spools/${SPOOL}`)
    expect(item!.type_ui_path).toBe(`/filament/types/${TYPE}`)
  })

  it('links a sync error to its item, and a gone one to nothing', () => {
    const status = withUiPaths({
      errors: [
        { kind: 'filament', sb_id: TYPE, label: 'BL-PLA', error: 'internal' },
        { kind: 'spool', sb_id: SPOOL, label: null, error: 'internal' },
      ],
    }) as { errors: Record<string, unknown>[] }
    expect(status.errors[0]!.ui_path).toBe(`/filament/types/${TYPE}`)
    expect(status.errors[1]).not.toHaveProperty('ui_path')
  })

  it('tells the model to link names with the paths, on every filament tool', () => {
    const filament = ALL_TOOLS.filter((one) => one.name.startsWith('filament_'))
    expect(filament.length).toBeGreaterThan(40)
    for (const one of filament) expect(one.description.endsWith(UI_PATH_NOTE)).toBe(true)
    expect(UI_PATH_NOTE).toMatch(/link the name/)
  })
})
