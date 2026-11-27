import { describe, expect, it } from 'vitest'
import { DOLLHOUSE, KEYCHAIN } from '../mocks/features/storefrontPublic'
import type { Listing } from './api/client'
import { colourParts, colourSections, usedColours } from './colourParts'

describe('colourParts (#8127)', () => {
  it('takes only the colour parameters, in the listing’s order', () => {
    expect(colourParts(KEYCHAIN).map((part) => part.name)).toEqual(['base_colour'])
  })

  it('groups a long list by its groups, each where its first part is', () => {
    const sections = colourSections(colourParts(DOLLHOUSE))!
    expect(sections.map((section) => [section.group, section.parts.length])).toEqual([
      ['Floors colours', 4],
      ['Roof, stairs & railing colours', 7],
      ['Windows & doors colours', 10],
      ['Walls outside colours', 4],
      ['Walls inside colours', 6],
    ])
    expect(sections[0]!.parts.map((part) => part.name)).toEqual(['rug_color', 'floor_color', 'rug_border_color', 'floor_accent_color'])
  })

  it('keeps a short list, or a list of one group, plain', () => {
    expect(colourSections(colourParts(DOLLHOUSE).slice(0, 6))).toBeNull()
    const oneGroup: Listing = {
      ...DOLLHOUSE,
      params: Object.fromEntries(Object.entries(DOLLHOUSE.params).map(([name, param]) => [name, { ...param, group: 'Colours' }])),
    }
    expect(colourSections(colourParts(oneGroup))).toBeNull()
  })

  it('lists each colour on screen once, casefolded, in the parts’ order', () => {
    const parts = colourParts(DOLLHOUSE).slice(0, 3)
    expect(usedColours(parts, { rug_color: '#C98E62', clip_color: '#ffffff', door_color: '#c98e62' })).toEqual(['#c98e62', '#ffffff'])
  })
})
