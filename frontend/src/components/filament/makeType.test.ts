import { describe, expect, it } from 'vitest'
import { listCatalog, setCatalogAliases, type SpoolType } from '../../api/filament'
import type { Filament } from '../../api/types'
import { E, M, ST, B, T } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { linksBookRow, linkTypeToBook, makeTypeFromBook, rankCandidates, typeCandidates } from './makeType'

const book = (over: Partial<Filament>): Filament =>
  ({
    material: 'PLA',
    subtype: null,
    brand: null,
    colour_name: 'Red',
    colour: '#FF0000',
    alpha: 255,
    effect: null,
    extra_colours: [],
    density_g_cm3: null,
    cost_per_kg: '20.0000',
    ...over,
  }) as Filament

describe('makeTypeFromBook resolves names through the API (#6706)', () => {
  it('finds a catalog row by any spelling of its name or alias, and adds none', async () => {
    const before = await Promise.all((['materials', 'subtypes', 'brands', 'effects'] as const).map((kind) => listCatalog(kind, { includeArchived: true })))
    const made = await makeTypeFromBook(book({ material: 'p.l.a', subtype: 'MATTE', brand: 'bambu-lab', effect: 'transparent' as Filament['effect'] }))
    expect([made.material_id, made.subtype_id, made.brand_id, made.effect_id]).toEqual([M.PLA, ST.MATTE, B.BAMBU, E.TRANSLUCENT])
    const after = await Promise.all((['materials', 'subtypes', 'brands', 'effects'] as const).map((kind) => listCatalog(kind, { includeArchived: true })))
    expect(after.map((rows) => rows.length)).toEqual(before.map((rows) => rows.length))
  })

  it('adds a material it cannot resolve, and leaves an unknown effect empty', async () => {
    let resolved = 0
    server.events.on('request:start', ({ request }) => {
      if (request.url.includes('/resolve')) resolved += 1
    })
    const made = await makeTypeFromBook(book({ material: 'Wax', effect: 'galaxy' }))
    expect(made.effect_id).toBeNull()
    expect((await listCatalog('materials')).find((row) => row.id === made.material_id)?.name).toBe('Wax')
    expect(resolved).toBe(2)
    server.events.removeAllListeners()
  })
})

const typeOf = (over: Partial<SpoolType>): SpoolType =>
  ({
    id: over.code ?? 'T',
    code: 'T',
    brand_id: B.SUNLU,
    material_id: M.PLA,
    subtype_id: null,
    effect_id: null,
    rgba: '#FF0000FF',
    label_weight_g: 1000,
    archived_at: null,
    ...over,
  }) as SpoolType

describe('rankCandidates (#7159)', () => {
  const ids = { brandId: B.SUNLU, materialId: M.PLA, subtypeId: ST.SILK, effectId: null } as const

  // The prod case: the row said PLA and Silk, the types PLA+ and Silk Dual.
  it('takes every type of the brand and RGB, whatever its material, subtype, effect or alpha', () => {
    const plus = typeOf({ code: 'SUNLU-PLA+-SILK-RED', material_id: 'pla-plus', subtype_id: ST.SILK })
    const dual = typeOf({ code: 'SUNLU-PLA-SILK-DUAL-RED', subtype_id: 'silk-dual', effect_id: E.SILK })
    const glass = typeOf({ code: 'SUNLU-PLA-RED-GLASS', rgba: '#ff000080', subtype_id: ST.SILK })
    const found = rankCandidates(book({ colour: '#ff0000', alpha: 255 }), ids, [plus, dual, glass])
    expect(found.map((one) => one.type.code).sort()).toEqual(['SUNLU-PLA+-SILK-RED', 'SUNLU-PLA-RED-GLASS', 'SUNLU-PLA-SILK-DUAL-RED'])
    expect(found.every((one) => !one.exact)).toBe(true)
  })

  it('leaves out archived types, other brands and other colours', () => {
    const types = [
      typeOf({ code: 'A', archived_at: '2026-11-23T05:49:00Z' }),
      typeOf({ code: 'B', brand_id: B.ELEGOO }),
      typeOf({ code: 'C', brand_id: null }),
      typeOf({ code: 'D', rgba: '#FF0001FF' }),
    ]
    expect(rankCandidates(book({}), ids, types)).toEqual([])
  })

  it('puts the exact match on material, subtype, colour and effect first', () => {
    const near = typeOf({ code: 'A-NEAR', subtype_id: null })
    const exact = typeOf({ code: 'Z-EXACT', subtype_id: ST.SILK })
    const found = rankCandidates(book({}), ids, [near, exact])
    expect(found.map((one) => [one.type.code, one.exact])).toEqual([
      ['Z-EXACT', true],
      ['A-NEAR', false],
    ])
  })

  it('matches no type for a brand the catalog lacks, and a row with no brand only types with none', () => {
    const types = [typeOf({ code: 'A' }), typeOf({ code: 'B', brand_id: null })]
    expect(rankCandidates(book({}), { ...ids, brandId: false }, types)).toEqual([])
    expect(rankCandidates(book({}), { ...ids, brandId: null }, types).map((one) => one.type.code)).toEqual(['B'])
  })
})

describe('typeCandidates (#7159)', () => {
  it('resolves the names by key and spelling, and adds no catalog row', async () => {
    const before = await Promise.all((['materials', 'subtypes', 'brands', 'effects'] as const).map((kind) => listCatalog(kind, { includeArchived: true })))
    const gold = typeOf({ code: 'EL-PLA-SILK-GOLD', brand_id: B.ELEGOO, subtype_id: ST.SILK, effect_id: E.SILK, rgba: '#D4AF37FF' })
    const plain = typeOf({ code: 'EL-PLA-GOLD', brand_id: B.ELEGOO, rgba: '#d4af37ff' })
    const found = await typeCandidates(book({ brand: 'ELEGOO', material: 'p.l.a', subtype: 'silk', effect: 'silk' as Filament['effect'], colour: '#D4AF37' }), [plain, gold])
    expect(found.map((one) => [one.type.code, one.exact])).toEqual([
      ['EL-PLA-SILK-GOLD', true],
      ['EL-PLA-GOLD', false],
    ])
    const unknown = await typeCandidates(book({ brand: 'Elegoo', material: 'Wax', subtype: 'Dual', colour: '#D4AF37' }), [gold])
    expect(unknown.map((one) => one.exact)).toEqual([false])
    const after = await Promise.all((['materials', 'subtypes', 'brands', 'effects'] as const).map((kind) => listCatalog(kind, { includeArchived: true })))
    expect(after.map((rows) => rows.length)).toEqual(before.map((rows) => rows.length))
  })
})

describe('typeCandidates by a brand alias (#7159 R-7159-1)', () => {
  it('finds the type of a brand the row names by its alias', async () => {
    await setCatalogAliases('brands', B.ELEGOO, ['Elegoo 3D'])
    const gold = typeOf({ code: 'EL-PLA-SILK-GOLD', brand_id: B.ELEGOO, rgba: '#D4AF37FF' })
    const found = await typeCandidates(book({ brand: 'elegoo3d', colour: '#D4AF37' }), [gold])
    expect(found.map((one) => one.type.code)).toEqual(['EL-PLA-SILK-GOLD'])
  })
})

describe('linksBookRow (#7159): the server links a book row by names, RGB and alpha', () => {
  const names = new Map([
    [M.PLA, 'PLA'],
    [ST.SILK, 'Silk'],
    [B.ELEGOO, 'Elegoo'],
  ])
  const gold = typeOf({ brand_id: B.ELEGOO, subtype_id: ST.SILK, effect_id: E.SILK, rgba: '#D4AF37FF' })
  const row = { material: 'pla', subtype: 'SILK', brand: 'elegoo', colour: '#d4af37', alpha: 255 }

  it('links a row of the same names in any case, whatever the effect', () => {
    expect(linksBookRow(row, gold, names)).toBe(true)
    expect(linksBookRow(row, { ...gold, effect_id: null }, names)).toBe(true)
  })

  it('does not link a row whose names differ, even by an alias spelling, or whose alpha differs', () => {
    expect(linksBookRow({ ...row, subtype: null }, gold, names)).toBe(false)
    expect(linksBookRow({ ...row, material: 'p.l.a' }, gold, names)).toBe(false)
    expect(linksBookRow({ ...row, alpha: 128 }, gold, names)).toBe(false)
  })

  it('does not link when a name of the type is unknown', () => {
    expect(linksBookRow(row, gold, new Map([[M.PLA, 'PLA']]))).toBe(false)
  })
})

describe('linkTypeToBook (#7159 R-7159-4)', () => {
  it('sends only the row cost per kg', async () => {
    const bodies: Promise<unknown>[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'PATCH') bodies.push(request.clone().json())
    })
    try {
      const linked = await linkTypeToBook(typeOf({ id: T.GOLD, code: 'EL-PLA-SILK-GOLD' }), { cost_per_kg: '22.5000' })
      expect(await Promise.all(bodies)).toEqual([{ cost_per_kg: '22.5000' }])
      expect(linked.id).toBe(T.GOLD)
      expect(linked.filament_id).not.toBeNull()
    } finally {
      server.events.removeAllListeners()
    }
  })
})
