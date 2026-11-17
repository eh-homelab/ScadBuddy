import { describe, expect, it } from 'vitest'
import { alphaOf, CLEAR_ALPHA, isTranslucent, parseRgba, sameOpacity, TRANSLUCENT_ALPHA, withAlpha } from './rgba'

describe('parseRgba', () => {
  // The backend's `normalise_rgba` (`filament/colour.py`): six or eight hex digits,
  // with or without `#`, any case; six read as opaque.
  it.each([
    ['#ff8800', '#FF8800FF'],
    ['ff8800', '#FF8800FF'],
    ['#ff880080', '#FF880080'],
  ])('reads %s as %s', (value, rgba) => {
    expect(parseRgba(value)).toBe(rgba)
  })

  // `fullmatch` with no strip: spaces around a colour are refused, as the backend does.
  it.each(['', '#fff', '#ff88', '#ff88001', 'red', '#gg8800', '#ff8800ff00', '##ff8800', '  #FF880010 ', '#ff8800 '])('refuses %j', (value) => {
    expect(parseRgba(value)).toBeNull()
  })
})

describe('alpha', () => {
  it('reads and sets the alpha byte', () => {
    expect(alphaOf('#FF880080')).toBe(0x80)
    expect(alphaOf('#FF8800')).toBe(0xff)
    expect(withAlpha('#FF8800FF', 0x80)).toBe('#FF880080')
    expect(withAlpha('#ff8800', 0)).toBe('#FF880000')
    expect(withAlpha('#FF8800FF', 300)).toBe('#FF8800FF')
    expect(withAlpha('#FF8800FF', 15.6)).toBe('#FF880010')
  })

  it('is translucent below FF', () => {
    expect(isTranslucent('#FFFFFF80')).toBe(true)
    expect(isTranslucent('#FFFFFFFE')).toBe(true)
    expect(isTranslucent('#FFFFFFFF')).toBe(false)
    expect(isTranslucent('#FFFFFF')).toBe(false)
  })

  it('has the presets of spec §5.1: clear 00-20, translucent 80', () => {
    expect(CLEAR_ALPHA).toBe(0x10)
    expect(TRANSLUCENT_ALPHA).toBe(0x80)
  })
})

/** The backend's vectors (`tests/filament/test_colour.py` `SAME_OPACITY`), kept alike. */
const SAME_OPACITY: [string | null, string | null, boolean][] = [
  ['#FFFFFF', '#FFFFFF80', false],
  ['#FFFFFF', null, true],
  [null, null, true],
  ['#FFFFFFFF', '#FFFFFF', true],
  ['#FFFFFF80', '#00000010', true],
  [null, '#FFFFFF80', false],
  ['#FFFFFF80', null, false],
  ['#ffffff80', '#FFFFFFFE', true],
]

describe('sameOpacity', () => {
  it.each(SAME_OPACITY)('%s and %s: %s', (left, right, same) => {
    expect(sameOpacity(left, right)).toBe(same)
    expect(sameOpacity(right, left)).toBe(same)
  })
})
