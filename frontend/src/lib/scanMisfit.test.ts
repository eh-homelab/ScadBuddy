import { describe, expect, it } from 'vitest'
import { misfit } from './scanMisfit'

// #8062 — the server's rule (`tasks/answer_check.py` `_accepts`).
const spool = { kind: 'spool', code: 'S-1' }
const place = { kind: 'location', code: 'L-12' }

describe('misfit', () => {
  it('accepts anything for a field that names nothing', () => {
    expect(misfit(spool, undefined)).toBeNull()
    expect(misfit(spool, {})).toBeNull()
  })
  it('accepts a code by its kind or by its code when the field names both', () => {
    const expects = { kinds: ['location'], codes: ['S-1'] }
    expect(misfit(place, expects)).toBeNull()
    expect(misfit(spool, expects)).toBeNull()
    expect(misfit({ kind: 'spool', code: 'S-2' }, expects)).toMatch(/^Expected/)
  })
  it('matches a schema code in any case', () => {
    expect(misfit(place, { codes: ['l-12'] })).toBeNull()
    expect(misfit(spool, { codes: ['l-12'] })).toBe('Expected L-12')
  })
  it('says which kind it expected', () => {
    expect(misfit(spool, { kinds: ['location'] })).toBe('Expected a location')
  })
})
