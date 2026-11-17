import { describe, expect, it } from 'vitest'
import { rowPieces } from '../src/mcp/sessionRelay.js'

describe('rowPieces', () => {
  it('cuts a long answer into pieces that join back whole', () => {
    const text = 'ab'.repeat(10) + 'c'
    const pieces = rowPieces(text, 4)
    expect(pieces.every((p) => p.length <= 4)).toBe(true)
    expect(pieces.join('')).toBe(text)
  })

  it('never cuts a surrogate pair', () => {
    const text = 'abc😀def'
    const pieces = rowPieces(text, 4)
    expect(pieces.join('')).toBe(text)
    for (const piece of pieces) expect(piece.isWellFormed()).toBe(true)
  })

  it('gives nothing for an empty answer', () => {
    expect(rowPieces('')).toEqual([])
  })
})
