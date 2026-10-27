import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { MAX_QUANTITY, parseQuantity } from './quantity'

/** `ItemCreate.quantity`'s and `ItemPatch.quantity`'s `maximum` in the spec `pnpm gen:api`
 * (run before `pnpm test`) exported: the backend's bound, read rather than restated. */
function specQuantityMax(schema: 'ItemCreate' | 'ItemPatch'): number {
  const path = process.env.SCADBUDDY_OPENAPI_JSON ?? `${import.meta.dirname}/../../../../backend/openapi.json`
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (cause) {
    throw new Error(`no API spec at ${path}: run \`pnpm gen:api\` first (pnpm test does)`, { cause })
  }
  type Quantity = { maximum?: unknown; anyOf?: { maximum?: unknown }[] }
  const spec = JSON.parse(text) as { components?: { schemas?: Record<string, { properties?: { quantity?: Quantity } }> } }
  const quantity = spec.components?.schemas?.[schema]?.properties?.quantity
  // ItemPatch's is optional, so its bound sits in the `anyOf` beside `null`.
  const max = quantity?.maximum ?? quantity?.anyOf?.find((each) => each.maximum !== undefined)?.maximum
  if (typeof max !== 'number') throw new Error(`${schema}.quantity.maximum not in the spec at ${path}`)
  return max
}

describe('parseQuantity (#4727, #4763)', () => {
  it('is bounded where the API is', () => {
    expect(MAX_QUANTITY).toBe(specQuantityMax('ItemCreate'))
    expect(MAX_QUANTITY).toBe(specQuantityMax('ItemPatch'))
  })

  it('takes a whole number from 1 to the bound', () => {
    expect(parseQuantity('1')).toBe(1)
    expect(parseQuantity(' 12 ')).toBe(12)
    expect(parseQuantity(String(MAX_QUANTITY))).toBe(MAX_QUANTITY)
  })

  it('refuses what Number reads as an integer but is not typed as one', () => {
    for (const text of ['1e9', '2.0', '0x10', '+3', '', '0', '-1', String(MAX_QUANTITY + 1)]) expect(parseQuantity(text), text).toBeNull()
  })
})
