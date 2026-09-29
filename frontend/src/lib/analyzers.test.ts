import { describe, expect, it } from 'vitest'
import type { AnalyzerDiagnostic } from '../api/types'
import {
  SCOPE_ORDER,
  atOrNarrower,
  describeLocation,
  partition,
  scopeLabel,
  scopesForFinding,
  widerThanTemplate,
} from './analyzers'

function diagnostic(over: Partial<AnalyzerDiagnostic>): AnalyzerDiagnostic {
  return {
    id: 'SB1003',
    key: 'SB1003',
    title: 'Overhangs past the support threshold',
    severity: 'info',
    category: 'geometry',
    message: 'm',
    sources: [],
    status: 'open',
    ...over,
  }
}

describe('describeLocation', () => {
  it('names the part, its colour, the region and the located edges of a mesh finding', () => {
    expect(
      describeLocation({
        kind: 'mesh',
        part: 2,
        colour: 'Red',
        bbox: { min: [0, 0, 0], max: [10, 4, 2], size: [10, 4, 2] },
        edges: [
          { part: 2, colour: 'Red', kind: 'open', faces: 1, start: [0, 0, 0], end: [1, 0, 0] },
        ],
        edges_truncated: true,
      }),
    ).toBe('Part 2 (Red) · a 10.0 × 4.0 × 2.0 mm region · 1+ located edges')
  })

  it('names a slot, a setting and the analyzer itself', () => {
    expect(describeLocation({ kind: 'filament_slot', slot_id: 3, edges_truncated: false })).toBe(
      'Filament slot 3',
    )
    expect(
      describeLocation({ kind: 'choices', setting: 'bed_type', edges_truncated: false }),
    ).toBe('Print choice bed_type')
    expect(describeLocation({ kind: 'analyzer', edges_truncated: false })).toBe(
      'The analyzer itself',
    )
    expect(describeLocation({ kind: 'plate', edges_truncated: false })).toBe('The plate')
  })
})

describe('scopeLabel', () => {
  it('says what each scope covers, broadest to narrowest', () => {
    expect(scopeLabel({ kind: 'global', key: '' })).toBe('Every print')
    expect(scopeLabel({ kind: 'material', key: 'pla/silk' })).toBe('Every pla/silk print')
    expect(scopeLabel({ kind: 'printer', key: 'model:h2c' })).toBe('Every h2c printer')
    expect(scopeLabel({ kind: 'printer', key: 'id:1' })).toBe('This printer (#1)')
    expect(scopeLabel({ kind: 'template', key: 'name-keychain' })).toBe('This template')
    expect(scopeLabel({ kind: 'template_version', key: 'k@abc' })).toBe('This template version')
    expect(scopeLabel({ kind: 'configuration', key: 'k#0123' })).toBe('These parameters')
    expect(scopeLabel({ kind: 'print', key: 'a'.repeat(32) })).toBe('This print')
  })
})

describe('scopesForFinding', () => {
  const scopes = [
    { kind: 'global', key: '' },
    { kind: 'material', key: 'pla' },
    { kind: 'material', key: 'petg' },
    { kind: 'print', key: 'a'.repeat(32) },
  ] as const
  it("offers every material for a whole-print finding and none for a slot's", () => {
    expect(scopesForFinding([...scopes], diagnostic({ slots: [] }))).toEqual(scopes)
    expect(scopesForFinding([...scopes], diagnostic({ slots: [2] })).map((s) => s.kind)).toEqual([
      'global',
      'print',
    ])
  })
})

describe('SCOPE_ORDER', () => {
  it("is the backend's, broadest first (analyzers/model.py SCOPE_ORDER)", () => {
    expect(SCOPE_ORDER).toEqual([
      'global',
      'material',
      'printer',
      'template',
      'template_version',
      'configuration',
      'print',
    ])
  })
})

describe('atOrNarrower', () => {
  const offered = [
    { kind: 'global', key: '' },
    { kind: 'printer', key: 'model:h2c' },
    { kind: 'printer', key: 'id:1' },
    { kind: 'template', key: 'k' },
    { kind: 'print', key: 'a'.repeat(32) },
  ] as const
  it('keeps the scope itself and the ones after it, ranked by position within a kind', () => {
    expect(atOrNarrower([...offered], { kind: 'printer', key: 'id:1' }).map((s) => s.key)).toEqual([
      'id:1',
      'k',
      'a'.repeat(32),
    ])
    expect(atOrNarrower([...offered], { kind: 'print', key: 'a'.repeat(32) })).toHaveLength(1)
  })
  it('places a scope the report does not list by its kind', () => {
    expect(atOrNarrower([...offered], { kind: 'material', key: 'pla' }).map((s) => s.kind)).toEqual([
      'printer',
      'printer',
      'template',
      'print',
    ])
  })
})

describe('widerThanTemplate', () => {
  it('is true for the scopes that reach other templates', () => {
    expect(widerThanTemplate({ kind: 'global', key: '' })).toBe(true)
    expect(widerThanTemplate({ kind: 'material', key: 'pla' })).toBe(true)
    expect(widerThanTemplate({ kind: 'printer', key: 'id:1' })).toBe(true)
    expect(widerThanTemplate({ kind: 'template', key: 'k' })).toBe(false)
    expect(widerThanTemplate({ kind: 'print', key: 'a'.repeat(32) })).toBe(false)
  })
})

describe('partition', () => {
  it('shows open and accepted findings, and sets aside hidden, suppressed and ignored ones', () => {
    const open = diagnostic({ key: 'SB1003' })
    const accepted = diagnostic({ key: 'SB2001', status: 'accepted' })
    const hidden = diagnostic({ key: 'SB9999', severity: 'hidden' })
    const suppressed = diagnostic({ key: 'SB1002:part-1', status: 'suppressed' })
    const ignored = diagnostic({ key: 'SB1002:part-2', status: 'ignored' })
    const crashed = diagnostic({ id: 'SB0001', key: 'SB0001:SB1003', severity: 'warning' })
    expect(partition([open, accepted, hidden, suppressed, ignored, crashed])).toEqual({
      shown: [open, accepted, crashed],
      setAside: [hidden, suppressed, ignored],
    })
  })
})
