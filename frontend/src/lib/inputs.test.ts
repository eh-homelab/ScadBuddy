import { describe, expect, it } from 'vitest'
import { getPath, joinInputs, mergePatch, NO_EXTRA, sameJson, setPath, splitInputs } from './inputs'

describe('sameJson (#1471)', () => {
  it.each([
    ['reordered keys', { a: 1, b: { c: [1, 2] } }, { b: { c: [1, 2] }, a: 1 }],
    ['nested objects', { a: { b: { c: null } } }, { a: { b: { c: null } } }],
    ['scalars', 'x', 'x'],
    ['both missing', undefined, undefined],
  ])('equal: %s', (_name, a, b) => {
    expect(sameJson(a, b)).toBe(true)
    expect(sameJson(b, a)).toBe(true)
  })
  it.each([
    ['arrays of different lengths', [1, 2], [1, 2, 3]],
    ['arrays in a different order', [1, 2], [2, 1]],
    ['a nested difference', { a: { b: 1 } }, { a: { b: 2 } }],
    ['[] and {}', [], {}],
    ['null and {}', null, {}],
    ['undefined and {}', undefined, {}],
    ['a key present as null and a missing key', { a: 1, b: null }, { a: 1 }],
    ['different keys, same count', { a: null }, { b: null }],
    ['1 and "1"', 1, '1'],
  ])('not equal: %s', (_name, a, b) => {
    expect(sameJson(a, b)).toBe(false)
    expect(sameJson(b, a)).toBe(false)
  })
})

describe('splitInputs / joinInputs', () => {
  it('splits params from the UI state and joins them back', () => {
    const raw = { params: { width: 3 }, house: { storeys: 2 }, v: 0 }
    const { params, extra } = splitInputs(raw)
    expect(params).toEqual({ width: 3 })
    expect(extra).toEqual({ house: { storeys: 2 }, v: 0 })
    expect(joinInputs(params, extra)).toEqual(raw)
  })
  it('falls back to the given params when inputs are missing or malformed', () => {
    expect(splitInputs(undefined, { width: 1 })).toEqual({ params: { width: 1 }, extra: NO_EXTRA })
    expect(splitInputs({ params: 'x' }, { width: 1 }).params).toEqual({ width: 1 })
  })
})

describe('mergePatch', () => {
  it('merges objects, replaces the rest, deletes on null', () => {
    const target = { params: { width: 1, height: 2 }, tab: 'a', list: [1] }
    expect(mergePatch(target, { params: { width: 5 }, tab: null, list: [2] })).toEqual({
      params: { width: 5, height: 2 },
      list: [2],
    })
  })
})

describe('getPath / setPath', () => {
  it('reads and writes a dotted path without touching the original', () => {
    const root = { params: { width: 1 }, style: {} }
    const next = setPath(root, 'style.exterior', 'brick')
    expect(getPath(next, 'style.exterior')).toBe('brick')
    expect(getPath(root, 'style.exterior')).toBeUndefined()
    expect(getPath(next, 'params.width')).toBe(1)
    expect(setPath({}, 'a.b.c', 1)).toEqual({ a: { b: { c: 1 } } })
  })
})

describe('inputs from a template are data, never prototype', () => {
  it('mergePatch drops __proto__, constructor and prototype keys', () => {
    const patch = JSON.parse('{"__proto__": {"polluted": 1}, "constructor": 2, "prototype": 3, "tab": "a"}')
    const merged = mergePatch({ params: {} }, patch) as Record<string, unknown>
    expect(merged).toEqual({ params: {}, tab: 'a' })
    expect(Object.getPrototypeOf(merged)).toBe(Object.prototype)
    expect(({} as Record<string, unknown>)['polluted']).toBeUndefined()
  })
  it('setPath writes no __proto__, constructor or prototype segment (#1275)', () => {
    for (const path of ['__proto__', '__proto__.polluted', 'a.constructor', 'a.prototype.x']) {
      const root = { a: { b: 1 } }
      expect(setPath(root, path, 1)).toBe(root)
    }
    expect(({} as Record<string, unknown>)['polluted']).toBeUndefined()
  })
  it('getPath reads own keys only', () => {
    expect(getPath({ a: {} }, 'constructor')).toBeUndefined()
    expect(getPath({ a: {} }, 'a.toString')).toBeUndefined()
    expect(getPath({ a: {} }, '__proto__')).toBeUndefined()
  })
  it('getPath does not index arrays', () => {
    expect(getPath({ list: [1, 2] }, 'list.0')).toBeUndefined()
    expect(getPath({ list: [1, 2] }, 'list')).toEqual([1, 2])
  })
  it('splitInputs reads a non-object as no inputs', () => {
    expect(splitInputs([1, 2] as unknown as Record<string, unknown>, { width: 1 })).toEqual({
      params: { width: 1 },
      extra: NO_EXTRA,
    })
  })
})
