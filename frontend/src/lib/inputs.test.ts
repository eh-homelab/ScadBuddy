import { describe, expect, it } from 'vitest'
import { getPath, joinInputs, mergePatch, NO_EXTRA, setPath, splitInputs } from './inputs'

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
