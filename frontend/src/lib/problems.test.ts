import { describe, expect, it } from 'vitest'
import { eligibilityIssues, refusedCheck } from './problems'

const base = { title: 'Conflict', status: 409 }

describe('eligibilityIssues', () => {
  it('reads the report Bambuddy sent through verbatim', () => {
    expect(
      eligibilityIssues({
        ...base,
        bambuddy_body: {
          ok: false,
          issues: [
            { kind: 'filament_type_mismatch', slot_index: 0, expected: 'PLA', actual: 'PETG' },
            { kind: 'printer_offline', slot_index: null },
          ],
        },
      }),
    ).toEqual([
      'filament type mismatch (slot 1): expected PLA, found PETG',
      'printer offline',
    ])
  })

  it('is empty when the problem carries no report', () => {
    expect(eligibilityIssues(base)).toEqual([])
    expect(eligibilityIssues({ ...base, bambuddy_body: { ok: false } })).toEqual([])
    expect(eligibilityIssues({ ...base, bambuddy_body: 'nope' })).toEqual([])
  })
})

describe('refusedCheck', () => {
  const refusal = {
    title: 'Unprocessable Content',
    status: 422,
    diagnostics: [
      { severity: 'error', message: 'Parser error: syntax error', line: 3, file: 'model.scad' },
      { severity: 'warning', message: "Can't find include file 'lib.scad'.", line: 1 },
    ],
    log_tail: ['ERROR: Parser error: syntax error in file model.scad, line 3'],
  }

  it('rebuilds the check that refused the save', () => {
    const check = refusedCheck(refusal)
    expect(check?.ok).toBe(false)
    expect(check?.diagnostics).toHaveLength(2)
    expect(check?.log_tail).toEqual(refusal.log_tail)
  })

  it('is undefined when the problem carries no diagnostics', () => {
    expect(refusedCheck({ title: 'Conflict', status: 409 })).toBeUndefined()
  })
})
