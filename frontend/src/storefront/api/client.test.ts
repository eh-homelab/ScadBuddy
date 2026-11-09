import { afterEach, describe, expect, it, vi } from 'vitest'
import { ActionKey } from './client'

const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

describe('ActionKey', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('keeps one key per action until it is done', () => {
    const keys = new ActionKey()
    const first = keys.for('a')
    expect(first).toMatch(UUID4)
    expect(keys.for('a')).toBe(first)
    expect(keys.for('b')).not.toBe(first)
    const b = keys.for('b')
    keys.done()
    expect(keys.for('b')).not.toBe(b)
  })

  it('makes a UUID v4 from random bytes where randomUUID is missing (an insecure context)', () => {
    const real = globalThis.crypto
    vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array) => real.getRandomValues(bytes) })
    const keys = new ActionKey()
    const one = keys.for('a')
    keys.done()
    const two = keys.for('a')
    expect(one).toMatch(UUID4)
    expect(two).toMatch(UUID4)
    expect(one).not.toBe(two)
  })
})
