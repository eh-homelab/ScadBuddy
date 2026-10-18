import { describe, expect, it } from 'vitest'
import { cut } from '../src/gate/limits.js'

// gate/limits.ts `cut`: at most `limit` code points, as Python's `text[:limit]`, walking no
// further than it needs to; the same answer as slicing the spread string.

const reference = (text: string, limit: number) => [...text].slice(0, limit).join('')

describe('cut', () => {
  it('cuts by code point, as slicing the spread string does, lone surrogates included', () => {
    const texts = ['', 'abc', '😀😀😀', `${'a'.repeat(199)}😀 and more`, 'a\ud83db', '\udc00\ud83d', 'x😀', '😀']
    for (const text of texts) for (const limit of [0, 1, 2, 3, 199, 200, 201]) expect(cut(text, limit)).toBe(reference(text, limit))
  })

  it('keeps a text within the limit as it is', () => {
    const text = 'short'
    expect(cut(text, 200)).toBe(text)
  })
})
