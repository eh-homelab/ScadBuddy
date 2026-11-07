import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type { OrderStatus } from '../../api/types'
import { addMode, afterAcceptanceWarning, itemOpen, OPEN_FOR_EDITS, TAKES_ITEMS } from './afterAcceptance'

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

  it("opens an item past `quoted` only while it has no accepted quote", () => {
    expect(itemOpen({ status: 'draft' }, [{ accepted: true }])).toBe(true)
    expect(itemOpen({ status: 'accepted' }, [])).toBe(true)
    expect(itemOpen({ status: 'in_production' }, [{ accepted: false }])).toBe(true)
    expect(itemOpen({ status: 'ready' }, [{ accepted: true }])).toBe(false)
    expect(itemOpen({ status: 'shipped' }, [])).toBe(false)
  })
})
