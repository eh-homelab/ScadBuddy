import { describe, expect, it } from 'vitest'
import type { OrderItemDetail } from '../../api/types'
import { groupItems } from './itemGroups'

const item = (id: string, status: OrderItemDetail['status']) => ({ id, status }) as OrderItemDetail
const ids = (rows: OrderItemDetail[]) => rows.map((row) => row.id)

describe('groupItems (#6334)', () => {
  it('puts the live lines first, then fulfilled, then cancelled, each in its order', () => {
    const grouped = groupItems([item('a', 'cancelled'), item('b', 'fulfilled'), item('c', 'approved'), item('d', 'cancelled'), item('e', 'printing')])
    expect(ids(grouped.main)).toEqual(['c', 'e'])
    expect(grouped.groups.map((g) => [g.key, ids(g.items)])).toEqual([
      ['fulfilled', ['b']],
      ['cancelled', ['a', 'd']],
    ])
  })

  it('lists the fulfilled lines plainly when none is live', () => {
    const grouped = groupItems([item('a', 'cancelled'), item('b', 'fulfilled')])
    expect(ids(grouped.main)).toEqual(['b'])
    expect(grouped.groups.map((g) => g.key)).toEqual(['cancelled'])
  })

  it('groups nothing when every line is one kind', () => {
    expect(groupItems([item('a', 'cancelled'), item('b', 'cancelled')])).toEqual({ main: [item('a', 'cancelled'), item('b', 'cancelled')], groups: [] })
    expect(groupItems([item('a', 'fulfilled')]).groups).toEqual([])
    expect(groupItems([])).toEqual({ main: [], groups: [] })
  })
})
