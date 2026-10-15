import { describe, expect, it } from 'vitest'
import { nameParts, shortName } from './libraryNames'

describe('nameParts (#3634)', () => {
  it("parts ScadBuddy's copy suffixes and the sliced extension from the name", () => {
    expect(nameParts('tfic-creeper-keychain (ScadBuddy).3mf')).toEqual({
      stem: 'tfic-creeper-keychain',
      extension: '3mf',
      scadbuddy: true,
      copy: null,
    })
    expect(nameParts('tfic-creeper-keychain (ScadBuddy) (2).3mf')).toMatchObject({ scadbuddy: true, copy: 2 })
    expect(nameParts('tfic-creeper-keychain (ScadBuddy).gcode.3mf')).toMatchObject({
      stem: 'tfic-creeper-keychain',
      extension: 'gcode.3mf',
      scadbuddy: true,
    })
    expect(nameParts('AMS_2_Pro_Rear_Desiccant_Box_-_PETG(2).3mf')).toMatchObject({
      stem: 'AMS_2_Pro_Rear_Desiccant_Box_-_PETG',
      copy: 2,
      scadbuddy: false,
    })
  })

  it('keeps an early dot in the stem, and a name that is only a suffix whole', () => {
    expect(nameParts('v1.2_shelf.3mf')).toMatchObject({ stem: 'v1.2_shelf', extension: '3mf' })
    expect(nameParts('(2).3mf')).toMatchObject({ stem: '(2)', copy: null })
    expect(nameParts('README')).toMatchObject({ stem: 'README', extension: '' })
  })
})

describe('shortName (#3634)', () => {
  it('leaves a name that fits two lines whole', () => {
    expect(shortName('tfic-creeper-keychain')).toBe('tfic-creeper-keychain')
  })

  it('keeps the end of a generated name, which is what tells two apart (#935)', () => {
    expect(shortName('bag-clip-3155628dc2bb43d4940fad6dba164efc')).toBe('bag-clip-3155628dc2bb…6dba164efc')
  })

  it('cuts a long name in the middle, at word breaks, keeping both ends', () => {
    const name = 'v1.2_shelf_bracket_with_cable_channel_and_a_long_tail_for_the_desk'
    const short = shortName(name)
    expect(short.length).toBeLessThanOrEqual(32)
    expect(short).toMatch(/^v1\.2_shelf_bracket…/)
    expect(short.endsWith('…the_desk')).toBe(true)
    // No word is cut: the head ends and the tail starts at a break.
    const [head, tail] = short.split('…')
    expect(name.startsWith(head!)).toBe(true)
    expect(name[name.length - tail!.length - 1]).toBe('_')
  })
})
