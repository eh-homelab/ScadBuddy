import { describe, expect, it } from 'vitest'
import { listCatalog } from '../../api/filament'
import type { Filament } from '../../api/types'
import { E, M, ST, B } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { makeTypeFromBook } from './makeType'

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
