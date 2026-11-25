import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { FILE } from '../mocks/features/storefrontPublic'
import type { Listing } from './api/client'
import { partFilaments, partsView, slotColours } from './fileParts'

/** #7523: the vectors the backend's `page_colours` is tested against too. */
const VECTORS = JSON.parse(readFileSync(`${import.meta.dirname}/../../../test-fixtures/storefront/file-page-colours.json`, 'utf8')) as {
  case: string
  materials: string[]
  filaments: Listing['colours']
  slots: { slot: number; default: string | null }[]
  want: Record<string, string> | null
}[]

describe('the colours the page opens on, which the shop prices at publish (#7523)', () => {
  it.each(VECTORS.map((vector) => [vector.case, vector] as const))('%s', (_name, vector) => {
    const slots = vector.slots.map((part, index) => ({ slot: part.slot, label: `Part ${index + 1}`, default: part.default }))
    const listing: Listing = { ...FILE, materials: vector.materials, colours: vector.filaments, slots }
    expect(partFilaments(partsView(listing), {})).toEqual(vector.want)
  })
})

describe("a file listing's parts (#6710 §6.2)", () => {
  const view = partsView(FILE)

  it('is one colour part per offered slot, starting at the shop’s filament', () => {
    expect(Object.keys(view.params)).toEqual(['colours.0', 'colours.1'])
    const first = view.params['colours.0']!
    expect(first).toMatchObject({ kind: 'choice', label: 'Part 1', colour: true, default: '#1B6CA8' })
    expect(first.kind === 'choice' && first.choices).toEqual(['#1B6CA8', '#E8532F', '#1C8C8C', '#C0502A'])
  })

  it("starts at the shop's filaments, and moves the other part when a pick fixes another material", () => {
    expect(partFilaments(view, {})).toEqual({ 0: 'f-blue', 1: 'f-orange' })
    // Rust comes in PETG only: the untouched part moves to the closest PETG colour.
    expect(partFilaments(view, { 'colours.0': '#C0502A' })).toEqual({ 0: 'f-petg-rust', 1: 'f-petg-rust' })
  })

  it('colours each slot with its filament, opaque as FF', () => {
    expect(slotColours(FILE, { 0: 'f-blue', 1: 'f-petg-teal' })).toEqual({ 0: '#1B6CA8FF', 1: '#1C8C8CFF' })
  })
})
