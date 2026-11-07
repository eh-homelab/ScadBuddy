import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type { OrderStatus } from '../../api/types'
import { addMode, afterAcceptanceWarning, chargesWarning, itemOpen, OPEN_FOR_EDITS, priceOpen, reviseMode, reviseWarning, TAKES_ITEMS } from './afterAcceptance'

type Extension = 'x-open-for-edits' | 'x-takes-items'

/** One of the state sets the spec `pnpm gen:api` (run before `pnpm test`) exported carries
 * on `Order.status`: the backend's own (`orders/models.py`), read, not restated. */
function specStates(extension: Extension): string[] {
  const path = process.env.SCADBUDDY_OPENAPI_JSON ?? `${import.meta.dirname}/../../../../backend/openapi.json`
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (cause) {
    throw new Error(`no API spec at ${path}: run \`pnpm gen:api\` first (pnpm test does)`, { cause })
  }
  const spec = JSON.parse(text) as {
    components?: { schemas?: { Order?: { properties?: { status?: Partial<Record<Extension, unknown>> } } } }
  }
  const states = spec.components?.schemas?.Order?.properties?.status?.[extension]
  if (!Array.isArray(states)) throw new Error(`Order.status.${extension} not in the spec at ${path}`)
  return states as string[]
}

// #5349: the backend's `_takes_item` and `item_open`, as the order page reads them.
describe('afterAcceptance', () => {
  it.each([
    ['x-open-for-edits', OPEN_FOR_EDITS],
    ['x-takes-items', TAKES_ITEMS],
  ] as const)('matches the backend: %s', (extension, states) => {
    expect([...states].sort()).toEqual([...specStates(extension)].sort())
  })

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

  it.each<[OrderStatus, RegExp]>([
    ['accepted', /^ORD-2026-2 is accepted\. Adding an item changes what the customer gets\. The new item/],
    ['in_production', /^ORD-2026-2 is in production\. Adding an item changes what the customer gets\. The new item/],
    ['ready', /^ORD-2026-2 is ready\. Adding an item changes what the customer gets\. The order goes back to in production until the new item is made\./],
  ])('names a %s order in the confirm', (status, text) => {
    expect(afterAcceptanceWarning({ number: 'ORD-2026-2', status })).toMatch(text)
  })

  it.each<[OrderStatus, string]>([
    ['accepted', 'accepted'],
    ['in_production', 'in production'],
    ['ready', 'ready'],
  ])('names a %s order in the charges confirm (#5351)', (status, text) => {
    expect(chargesWarning({ number: 'ORD-2026-2', status })).toMatch(
      new RegExp(`^ORD-2026-2 is ${text}\\. Changing the charges changes what the customer pays\\.`),
    )
  })

  it('revises an accepted item on its own route, never on hold or mid-print (#5350)', () => {
    const accepted = [{ accepted: true }]
    const item = { status: 'approved' as const, revised_after_acceptance: false }
    expect(reviseMode({ status: 'draft', on_hold: false }, item, accepted)).toBe('open')
    expect(reviseMode({ status: 'accepted', on_hold: false }, item, [])).toBe('open')
    expect(reviseMode({ status: 'accepted', on_hold: false }, item, accepted)).toBe('after_acceptance')
    expect(reviseMode({ status: 'in_production', on_hold: false }, { ...item, status: 'failed' }, accepted)).toBe('after_acceptance')
    expect(reviseMode({ status: 'accepted', on_hold: true }, item, accepted)).toBeNull()
    expect(reviseMode({ status: 'in_production', on_hold: false }, { ...item, status: 'printing' }, accepted)).toBeNull()
    expect(reviseMode({ status: 'in_production', on_hold: false }, { ...item, status: 'queued' }, accepted)).toBeNull()
    expect(reviseMode({ status: 'shipped', on_hold: false }, item, accepted)).toBeNull()
    // Once revised, the item is open again for its design, on hold too, as the backend's plain route is.
    expect(reviseMode({ status: 'accepted', on_hold: true }, { status: 'draft', revised_after_acceptance: true }, accepted)).toBe('open')
  })

  it('takes a quote of a revised accepted item until it is accepted (#5350)', () => {
    const accepted = [{ accepted: true }]
    expect(priceOpen({ status: 'accepted' }, { revised_after_acceptance: false }, accepted)).toBe(false)
    expect(priceOpen({ status: 'in_production' }, { revised_after_acceptance: true }, accepted)).toBe(true)
    expect(priceOpen({ status: 'shipped' }, { revised_after_acceptance: true }, accepted)).toBe(false)
    expect(priceOpen({ status: 'quoted' }, { revised_after_acceptance: false }, accepted)).toBe(true)
    // The quantity stays pinned: `itemOpen` still says no.
    expect(itemOpen({ status: 'accepted' }, accepted)).toBe(false)
  })

  it.each<[OrderStatus, string]>([
    ['accepted', 'accepted'],
    ['in_production', 'in production'],
  ])('names a %s order in the revision confirm (#5350)', (status, text) => {
    expect(reviseWarning({ number: 'ORD-2026-2', status })).toMatch(
      new RegExp(`^ORD-2026-2 is ${text}\\. A new design changes what the customer gets\\.`),
    )
  })

  it("opens an item past `quoted` only while it has no accepted quote", () => {
    expect(itemOpen({ status: 'draft' }, [{ accepted: true }])).toBe(true)
    expect(itemOpen({ status: 'accepted' }, [])).toBe(true)
    expect(itemOpen({ status: 'in_production' }, [{ accepted: false }])).toBe(true)
    expect(itemOpen({ status: 'ready' }, [{ accepted: true }])).toBe(false)
    expect(itemOpen({ status: 'shipped' }, [])).toBe(false)
  })
})
