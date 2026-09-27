import { describe, expect, it } from 'vitest'
import { isBuiltin } from './builtin'

describe('isBuiltin', () => {
  it('knows a built-in by its id', () => {
    expect(isBuiltin('builtin:name-keychain')).toBe(true)
  })

  it('takes a bare slug as a template of mine, even one that says builtin', () => {
    expect(isBuiltin('name-keychain')).toBe(false)
    expect(isBuiltin('builtin')).toBe(false)
    expect(isBuiltin('builtin-bin')).toBe(false)
  })
})
