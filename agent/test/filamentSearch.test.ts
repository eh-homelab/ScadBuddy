import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { matchesSearch, parseSearch, propertyTexts, spoolSearchDoc, typeSearchDoc } from '../src/tools/filamentSearch.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6894: the Filament page's text search in filament_spool_list and filament_type_list `q`.
// The matcher is a copy of frontend/src/lib/filamentSearch.ts; both run test-fixtures/filament/search-vectors.json.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

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

type Spool = Parameters<typeof spoolSearchDoc>[0]
type SpoolType = Parameters<typeof typeSearchDoc>[0]

const spool = (values: Partial<Spool>) =>
  ({
    id: 's87',
    number: 87,
    type_id: 't1',
    colour_name: null,
    rgba: null,
    names: { brand: 'Bambu Lab', material: 'PLA', subtype: 'Basic', location: 'Shelf A' },
    effective: { colour_name: 'Hot Pink', rgba: '#F5547CFF' },
    ...values,
  }) as Spool

type Cases = { query: string; match: boolean }[]
type Properties = Parameters<typeof propertyTexts>[0]
type Vector =
  | { name: string; kind: 'spool'; spool: Spool; extras: Parameters<typeof spoolSearchDoc>[1]; properties?: Properties; cases: Cases }
  | { name: string; kind: 'type'; type: SpoolType; names: Record<string, string>; skus: string[]; properties?: Properties; cases: Cases }

const { groups } = JSON.parse(readFileSync(`${import.meta.dirname}/../../test-fixtures/filament/search-vectors.json`, 'utf8')) as { groups: Vector[] }

describe('the filament search matcher', () => {
  for (const group of groups) {
    it(`matches the shared vectors: ${group.name}`, () => {
      const properties = propertyTexts(group.properties)
      const doc =
        group.kind === 'spool'
          ? spoolSearchDoc(group.spool, { ...group.extras, properties })
          : typeSearchDoc(group.type, new Map(Object.entries(group.names)), group.skus, { properties })
      for (const { query, match } of group.cases) expect(matchesSearch(doc, parseSearch(query)), JSON.stringify(query)).toBe(match)
    })
  }
})

const SUPPLIER = '8d6f3c1e-0000-4000-8000-00000000a001'

function backend() {
  server.use(
    http.get(`${BACKEND}/api/v1/filament/spools`, () =>
      HttpResponse.json([spool({}), spool({ id: 's4', number: 4, type_id: 't2', names: { brand: 'Elegoo', material: 'PLA' }, effective: { colour_name: 'Gold', rgba: '#D4AF37FF' } })]),
    ),
    http.get(`${BACKEND}/api/v1/filament/types`, () =>
      HttpResponse.json([
        { id: 't1', code: 'BL-PLA-BASIC-HOT-PINK', brand_id: 'b1', material_id: 'm1', subtype_id: null, colour_name: 'Hot Pink', rgba: '#F5547CFF' },
        { id: 't2', code: 'EL-PLA-SILK-GOLD', brand_id: 'b2', material_id: 'm1', subtype_id: null, colour_name: 'Gold', rgba: '#D4AF37FF' },
      ]),
    ),
    http.get(`${BACKEND}/api/v1/filament/catalog/:kind`, ({ params }) =>
      HttpResponse.json(
        params.kind === 'brands'
          ? [
              { id: 'b1', name: 'Bambu Lab' },
              { id: 'b2', name: 'Elegoo' },
            ]
          : params.kind === 'materials'
            ? [{ id: 'm1', name: 'PLA' }]
            : [],
      ),
    ),
    http.get(`${BACKEND}/api/v1/filament/suppliers`, () => HttpResponse.json([{ id: SUPPLIER }])),
    http.get(`${BACKEND}/api/v1/filament/suppliers/${SUPPLIER}/products`, () => HttpResponse.json([{ type_id: 't1', sku: 'A00-R3' }])),
    http.get(`${BACKEND}/api/v1/filament/inventory`, () =>
      HttpResponse.json({ spools: [], trays: [{ printer_id: 'p', printer_name: 'H2C', trays: [{ ams_id: 0, tray_id: 1, label: 'AMS-A slot 2', spool_id: 's4' }] }] }),
    ),
    http.get(`${BACKEND}/api/v1/filament/tags`, () =>
      HttpResponse.json([
        { id: 'g1', tag_uid: '04A1B2C3D4E5F6', tray_uuid: null, binding: { spool_id: 's4' }, spool_number: 4 },
        { id: 'g2', tag_uid: 'AABBCCDD', tray_uuid: null, binding: null, spool_number: null },
      ]),
    ),
    http.get(`${BACKEND}/api/v1/entity-kinds/:kind/properties`, ({ params }) =>
      HttpResponse.json({
        properties: params.kind === 'spool_type' ? { t2: { gtin: ['06975337041234'], td: 1.8 } } : { s87: { lot_number: 'L-2207' } },
      }),
    ),
  )
}

const propertiesDown = () =>
  server.use(http.get(`${BACKEND}/api/v1/entity-kinds/:kind/properties`, () => HttpResponse.json({ detail: 'down' }, { status: 500 })))

const numbers = (result: unknown) => (firstText(result) as { number: number }[]).map((one) => one.number)
const codes = (result: unknown) => (firstText(result) as { code: string }[]).map((one) => one.code)

describe('filament_spool_list q', () => {
  it('lists every spool with no q, reading nothing else', async () => {
    server.use(http.get(`${BACKEND}/api/v1/filament/spools`, () => HttpResponse.json([spool({})])))
    expect(numbers(await runTool(tool('filament_spool_list'), {}, ctx()))).toEqual([87])
  })

  it('finds a spool by number, colour, hex, SKU, type code and tray', async () => {
    backend()
    const run = async (q: string) => numbers(await runTool(tool('filament_spool_list'), { q }, ctx()))
    expect(await run('#4')).toEqual([4])
    expect(await run('87')).toEqual([87])
    expect(await run('gold')).toEqual([4])
    expect(await run('f5547c')).toEqual([87])
    expect(await run('a00-r3')).toEqual([87])
    expect(await run('el-pla-silk')).toEqual([4])
    expect(await run('h2c slot 2')).toEqual([4])
    // #7151: by its open tag's UID, as a reader prints it; a tag on no spool finds none.
    expect(await run('04:A1:B2:C3')).toEqual([4])
    expect(await run('aabbccdd')).toEqual([])
  })

  it('still searches the spools when the SKUs and trays cannot be read', async () => {
    backend()
    server.use(
      http.get(`${BACKEND}/api/v1/filament/suppliers`, () => HttpResponse.json({ detail: 'down' }, { status: 503 })),
      http.get(`${BACKEND}/api/v1/filament/inventory`, () => HttpResponse.json({ detail: 'down' }, { status: 503 })),
    )
    expect(numbers(await runTool(tool('filament_spool_list'), { q: 'pink' }, ctx()))).toEqual([87])
  })

  it('finds a spool by a value of its properties, and still lists when they cannot be read (#7026)', async () => {
    backend()
    expect(numbers(await runTool(tool('filament_spool_list'), { q: 'l-2207' }, ctx()))).toEqual([87])
    propertiesDown()
    expect(numbers(await runTool(tool('filament_spool_list'), { q: 'l-2207' }, ctx()))).toEqual([])
    expect(numbers(await runTool(tool('filament_spool_list'), { q: 'pink' }, ctx()))).toEqual([87])
  })
})

describe('filament_type_list q', () => {
  it('finds a type by brand, colour, hex and SKU', async () => {
    backend()
    const run = async (q: string) => codes(await runTool(tool('filament_type_list'), { q }, ctx()))
    expect(await run('elegoo')).toEqual(['EL-PLA-SILK-GOLD'])
    expect(await run('hot pink')).toEqual(['BL-PLA-BASIC-HOT-PINK'])
    expect(await run('#D4AF37')).toEqual(['EL-PLA-SILK-GOLD'])
    expect(await run('A00-R3')).toEqual(['BL-PLA-BASIC-HOT-PINK'])
    expect(await run('pla')).toEqual(['BL-PLA-BASIC-HOT-PINK', 'EL-PLA-SILK-GOLD'])
  })

  it('finds a type by a GTIN or a number in its properties, and still lists when they cannot be read (#7026)', async () => {
    backend()
    const run = async (q: string) => codes(await runTool(tool('filament_type_list'), { q }, ctx()))
    expect(await run('06975337041234')).toEqual(['EL-PLA-SILK-GOLD'])
    expect(await run('1.8')).toEqual(['EL-PLA-SILK-GOLD'])
    propertiesDown()
    expect(await run('06975337041234')).toEqual([])
    expect(await run('elegoo')).toEqual(['EL-PLA-SILK-GOLD'])
  })
})
