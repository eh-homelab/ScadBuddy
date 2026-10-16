import { describe, expect, it } from 'vitest'
import { fuzzyScore } from './fuzzy'

describe('fuzzyScore (#3651)', () => {
  it('matches every word typed, in any order', () => {
    expect(fuzzyScore('Gridfinity Bins', 'bins grid')).toBeGreaterThan(0)
    expect(fuzzyScore('Gridfinity Bins', 'bins tags')).toBe(0)
  })

  it('matches letters in order and one typo', () => {
    expect(fuzzyScore('Gridfinity Bins', 'gfb')).toBeGreaterThan(0)
    expect(fuzzyScore('Gridfinity Bins', 'gridfinty')).toBeGreaterThan(0)
    expect(fuzzyScore('Reagan Keychain', 'keyhcain')).toBeGreaterThan(0)
    expect(fuzzyScore('Reagan Keychain', 'zzzz')).toBe(0)
  })

  it('ranks a word prefix above a typo', () => {
    expect(fuzzyScore('Gridfinity Bins', 'grid')).toBeGreaterThan(fuzzyScore('Gridfinity Bins', 'gridfinty'))
  })

  it('matches everything with nothing typed', () => {
    expect(fuzzyScore('anything', '  ')).toBe(1)
  })
})
