import { describe, expect, it } from 'vitest'
import type { OrderStatus } from '../../api/types'
import { addMode, itemOpen } from './afterAcceptance'

// #5349: the backend's `_takes_item` and `item_open`, as the order page reads them.
describe('afterAcceptance', () => {
  it.each<[OrderStatus, boolean, ReturnType<typeof addMode>]>([
    ['draft', false, 'open'],
    ['quoted', true, 'open'],
    ['accepted', false, 'after_acceptance'],
    ['in_production', false, 'after_acceptance'],
    ['ready', false, 'after_acceptance'],
    ['accepted', true, null],
    ['ready', true, null],
    ['shipped', false, null],
    ['closed', false, null],
    ['cancelled', false, null],
  ])('adds to a %s order (on hold: %s) by %s', (status, onHold, mode) => {
    expect(addMode({ status, on_hold: onHold })).toBe(mode)
  })

  it("opens an item past `quoted` only while it has no accepted quote", () => {
    expect(itemOpen({ status: 'draft' }, [{ accepted: true }])).toBe(true)
    expect(itemOpen({ status: 'accepted' }, [])).toBe(true)
    expect(itemOpen({ status: 'in_production' }, [{ accepted: false }])).toBe(true)
    expect(itemOpen({ status: 'ready' }, [{ accepted: true }])).toBe(false)
    expect(itemOpen({ status: 'shipped' }, [])).toBe(false)
  })
})
