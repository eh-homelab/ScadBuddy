import { describe, expect, it } from 'vitest'
import type { Spool } from '../../api/filament'
import { DEFAULT_COLUMNS, DEFAULT_SPOOLS_QUERY, filterSpools, groupSpools, hasUnknownMaterial, needsType, readQuery, sortSpools, writeQuery, type SpoolsQuery } from './spoolsQuery'

function spool(number: number, over: Partial<Spool> = {}): Spool {
  return {
    id: `s${number}`,
    number,
    type_id: 't',
    archived_at: null,
    grams_left: 1000,
    last_used_at: null,
    low_stock: false,
    effective: { label_weight_g: 1000 },
    names: { material: 'PLA', brand: 'Bambu Lab', insert_type: null, location: null, subtype: null, category: null, effect: null },
    ...over,
  } as Spool
}

describe('the spools query', () => {
  it('round-trips through the URL', () => {
    const q: SpoolsQuery = {
      view: 'cards',
      status: 'all',
      needsType: true,
      lowStock: true,
      used: true,
      fresh: false,
      loaded: true,
      material: 'PETG',
      brand: 'Sunlu',
      insertType: 'Cardboard spool',
      location: 'Shelf A',
      group: 'material',
      columns: ['number', 'colour', 'grams_left'],
      sort: '-grams_left',
    }
    expect(readQuery(writeQuery(q))).toEqual(q)
  })

  it('leaves the defaults out of the URL and reads an empty one as the defaults', () => {
    expect(writeQuery(DEFAULT_SPOOLS_QUERY).toString()).toBe('')
    expect(readQuery(new URLSearchParams())).toEqual(DEFAULT_SPOOLS_QUERY)
    expect(DEFAULT_SPOOLS_QUERY.columns).toEqual(DEFAULT_COLUMNS)
  })

  it('drops a column or a sort it does not know', () => {
    const q = readQuery(new URLSearchParams('cols=number,nope&sort=nope&status=gone'))
    expect(q.columns).toEqual(['number'])
    expect(q.sort).toBe('number')
    expect(q.status).toBe('active')
  })
})

describe('filterSpools', () => {
  const spools = [
    spool(1),
    spool(2, { low_stock: true, grams_left: 100 }),
    spool(3, { archived_at: '2026-11-01T00:00:00Z' }),
    spool(4, { type_id: null }),
    spool(5, { grams_left: 1000, last_used_at: '2026-11-01T00:00:00Z' }),
    spool(6, { names: { material: 'PETG', brand: 'Sunlu', location: 'Shelf A', insert_type: null, subtype: null, category: null, effect: null } }),
    // Printed from, but not under its low-stock line.
    spool(7, { grams_left: 900 }),
  ]
  const numbers = (q: Partial<SpoolsQuery>) => filterSpools(spools, { ...DEFAULT_SPOOLS_QUERY, ...q }).map((s) => s.number)

  it('keeps the low-stock rows the server marked', () => {
    expect(numbers({ lowStock: true })).toEqual([2])
  })

  it('filters by status, type, use and name', () => {
    expect(numbers({})).toEqual([1, 2, 4, 5, 6, 7])
    expect(numbers({ status: 'archived' })).toEqual([3])
    expect(numbers({ status: 'all' })).toEqual([1, 2, 3, 4, 5, 6, 7])
    expect(numbers({ needsType: true })).toEqual([4])
    expect(numbers({ used: true })).toEqual([2, 7])
    expect(numbers({ fresh: true })).toEqual([1, 4, 6])
    expect(numbers({ material: 'PETG' })).toEqual([6])
    expect(numbers({ brand: 'sunlu', location: 'Shelf A' })).toEqual([6])
  })
})

describe('sorting and grouping', () => {
  it('sorts by a column, descending with a minus', () => {
    const spools = [spool(1, { grams_left: 500 }), spool(2, { grams_left: 900 }), spool(3, { grams_left: null })]
    expect(sortSpools(spools, '-grams_left').map((s) => s.number)).toEqual([2, 1, 3])
    expect(sortSpools(spools, 'grams_left').map((s) => s.number)).toEqual([1, 2, 3])
  })

  it('groups by material, a spool with none last', () => {
    const none = { material: null, brand: null, location: null, insert_type: null, subtype: null, category: null, effect: null }
    const groups = groupSpools([spool(1), spool(2, { names: { ...none, material: 'PETG' } }), spool(3, { names: none }), spool(4)], 'material')
    expect(groups.map((g) => [g.label, g.spools.map((s) => s.number)])).toEqual([
      ['PETG', [2]],
      ['PLA', [1, 4]],
      ['No material', [3]],
    ])
    expect(groupSpools([spool(1)], 'none')).toEqual([{ key: '', label: '', spools: [spool(1)] }])
  })

  it('keeps needs-type to spools with no type, not one with a type and an Unknown material', () => {
    const typed = spool(1, { names: { material: 'Unknown' } as Spool['names'] })
    const none = spool(2, { type_id: null })
    expect(filterSpools([typed, none], { ...DEFAULT_SPOOLS_QUERY, needsType: true }).map((s) => s.number)).toEqual([2])
    expect(needsType(typed)).toBe(false)
    expect(hasUnknownMaterial(typed)).toBe(true)
    expect(hasUnknownMaterial(none)).toBe(false)
  })
})
