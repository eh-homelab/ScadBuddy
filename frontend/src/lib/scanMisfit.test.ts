import { describe, expect, it } from 'vitest'
import { canonicalCode, misfit } from './scanMisfit'

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
  it('matches a schema code in any form the server takes', () => {
    expect(misfit({ kind: 'spool', code: 'S-3' }, { codes: ['S3'] })).toBeNull()
    expect(misfit({ kind: 'spool', code: 'S-3' }, { codes: ['s-03'] })).toBeNull()
    expect(misfit({ kind: 'spool', code: 'S-4' }, { codes: ['S03'] })).toBe('Expected S-3')
  })
  it('says which kind it expected', () => {
    expect(misfit(spool, { kinds: ['location'] })).toBe('Expected a location')
  })
})

describe('canonicalCode', () => {
  it('writes a code as the server does', () => {
    expect(canonicalCode(' l007 ')).toBe('L-7')
    expect(canonicalCode('S-12')).toBe('S-12')
    expect(canonicalCode('nonsense')).toBe('NONSENSE')
  })
})
