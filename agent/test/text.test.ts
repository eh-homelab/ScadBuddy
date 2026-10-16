import { describe, expect, it } from 'vitest'
import { codePoints } from '../src/tools/text.js'

describe('codePoints', () => {
  it("counts as Python's len() does: an astral character is one, not two UTF-16 units", () => {
    expect(codePoints('')).toBe(0)
    expect(codePoints('Reagan')).toBe(6)
    expect(codePoints('Zoë 🦄 ß')).toBe(7)
    expect('🦄'.length).toBe(2)
    expect(codePoints('🦄')).toBe(1)
  })
})
