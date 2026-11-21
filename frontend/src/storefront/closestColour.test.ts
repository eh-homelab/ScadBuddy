import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { closestColour, type Swatch } from './closestColour'

/** #6258: the vectors the backend's `closest_colour.py` is tested against too. */
const VECTORS = JSON.parse(readFileSync(`${import.meta.dirname}/../../../test-fixtures/storefront/closest-colour.json`, 'utf8')) as {
  case: string
  target: Swatch
  candidates: Swatch[]
  want: number | null
}[]

describe('closestColour (#6258)', () => {
  it('has vectors to check', () => {
    expect(VECTORS.length).toBeGreaterThan(0)
  })

  it.each(VECTORS.map((vector) => [vector.case, vector] as const))('%s', (_name, vector) => {
    expect(closestColour(vector.target, vector.candidates)).toBe(vector.want)
  })
})
