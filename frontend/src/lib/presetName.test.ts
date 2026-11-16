import { describe, expect, it } from 'vitest'
import { normalisePresetName } from './presetName'

describe('normalisePresetName (#6459)', () => {
  it("collapses and trims what Python's str.split() splits on, and nothing else", () => {
    expect(normalisePresetName('  a \t b\n\nc  ')).toBe('a b c')
    // Whitespace to Python, not to JS's \s.
    expect(normalisePresetName('a\u001cb\u001fc\u0085d')).toBe('a b c d')
    // Whitespace to JS's \s, not to Python: kept.
    expect(normalisePresetName('a\ufeffb')).toBe('a\ufeffb')
  })
})
