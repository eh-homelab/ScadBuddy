import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { matchesSearch, parseSearch, spoolSearchDoc, typeSearchDoc } from '../src/tools/filamentSearch.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6894: the Filament page's text search in filament_spool_list and filament_type_list `q`.
// The matcher is a copy of frontend/src/lib/filamentSearch.ts; these cases are its tests' too.

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

const find = (query: string) =>
  matchesSearch(spoolSearchDoc(spool({}), { typeCode: 'BL-PLA-BASIC-HOT-PINK', skus: ['A00-R3'], tray: 'H2C AMS-A slot 2' }), parseSearch(query))

describe('the filament search matcher', () => {
  it('finds a spool by #number or number, and #87 only as a number', () => {
    expect(find('#87')).toBe(true)
    expect(find('87')).toBe(true)
    expect(find('#8')).toBe(false)
    const grey = spoolSearchDoc(spool({ number: 3, effective: { colour_name: 'Grey', rgba: '#878787FF' }, names: { location: 'Bin 87' } }))
    expect(matchesSearch(grey, parseSearch('#87'))).toBe(false)
    expect(matchesSearch(grey, parseSearch('87'))).toBe(true)
  })

  it('matches every field in any case, every token somewhere', () => {
    for (const query of ['bambu', 'BASIC', 'hot pink', 'shelf', 'bl-pla-basic', 'a00-r3', 'h2c slot', 'pink pla']) expect(find(query), query).toBe(true)
    expect(find('pink petg')).toBe(false)
  })

  it('matches a hex with or without #, from 3 characters', () => {
    expect(find('#F5547C')).toBe(true)
    expect(find('f5547c')).toBe(true)
    expect(find('#00FF00')).toBe(false)
    expect(find('f5')).toBe(false)
  })

  it('searches a type by code, catalog names, colour, hex and SKU', () => {
    const type = { id: 't', code: 'EL-PLA-SILK-GOLD', brand_id: 'b', material_id: 'm', subtype_id: 's', colour_name: 'Gold', rgba: '#D4AF37FF' } as SpoolType
    const names = new Map([
      ['b', 'Elegoo'],
      ['m', 'PLA'],
      ['s', 'Silk'],
    ])
    const doc = typeSearchDoc(type, names, ['ELG-SILK-GLD'])
    for (const query of ['elegoo silk', 'gold', 'd4af37', 'elg-silk', 'el-pla']) expect(matchesSearch(doc, parseSearch(query)), query).toBe(true)
  })
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
  )
}

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
  })

  it('still searches the spools when the SKUs and trays cannot be read', async () => {
    backend()
    server.use(
      http.get(`${BACKEND}/api/v1/filament/suppliers`, () => HttpResponse.json({ detail: 'down' }, { status: 503 })),
      http.get(`${BACKEND}/api/v1/filament/inventory`, () => HttpResponse.json({ detail: 'down' }, { status: 503 })),
    )
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
})
