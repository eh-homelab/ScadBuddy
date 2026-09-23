import { describe, expect, it } from 'vitest'
import { eligibilityIssues } from './problems'

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
