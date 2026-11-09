import { describe, expect, it } from 'vitest'
import type { Quote, Revision } from '../../api/types'
import { oldRevision, setupChanges, setupGroups, setupPlates, setupStats } from './printSetups'

function revision(id: string, number: number, params: Record<string, unknown>): Revision {
  return {
    id,
    item_id: 'i',
    number,
    reference: { slug: 'keychain', commit: 'a'.repeat(40), inputs: { params, v: 1 }, template_name: 'Keychain', render_key: 'k' },
    colours: [],
    note: '',
    created_at: `2026-11-0${number}T10:00:00Z`,
    created_by: { kind: 'browser' },
  } as unknown as Revision
}

function quote(id: string, revisionId: string | null, at: string, extra: Partial<Quote> = {}): Quote {
  return {
    id,
    order_item_id: 'i',
    revision_id: revisionId,
    created_at: at,
    source: 'slice',
    accepted: false,
    plates: [],
    lines: [],
    choices: null,
    ...extra,
  } as unknown as Quote
}

const R1 = revision('r1', 1, { width: 12, text: 'Ada' })
const R2 = revision('r2', 2, { width: 14, text: 'Ada' })

// #4673 PR 4 — the drawer's print setups: grouped by revision, each with its numbers and
// what it changed from the accepted one.
describe('print setups', () => {
  it('groups by revision, the current one first, each oldest first', () => {
    const groups = setupGroups(
      [quote('b', 'r1', '2026-11-02T00:00:00Z'), quote('c', 'r2', '2026-11-03T00:00:00Z'), quote('a', 'r1', '2026-11-01T00:00:00Z')],
      [R1, R2],
      'r2',
    )
    expect(groups.map((g) => [g.revision?.number, g.current, g.quotes.map((q) => q.id)])).toEqual([
      [2, true, ['c']],
      [1, false, ['a', 'b']],
    ])
  })

  it("refuses Accept on an older revision's quote only", () => {
    expect(oldRevision(quote('a', 'r1', ''), 'r2')).toBe(true)
    expect(oldRevision(quote('a', 'r2', ''), 'r2')).toBe(false)
    expect(oldRevision(quote('a', null, ''), 'r2')).toBe(false)
  })

  it('sums the plates, time and grams', () => {
    const q = quote('a', 'r1', '', {
      plates: [
        { index: 1, print_seconds: 600, filaments: [{ slot: 0, used_g: '1.5' }, { slot: 1, used_g: '2' }] },
        { index: 2, print_seconds: 300, filaments: [{ slot: 0, used_g: '0.5' }] },
      ],
    })
    expect(setupStats(q)).toEqual({ plates: 2, seconds: 900, grams: 4 })
    expect(setupPlates(q)).toEqual([1, 2])
  })

  it('names the inputs and the setup that changed from the accepted one', () => {
    const accepted = quote('a', 'r1', '', {
      accepted: true,
      choices: { printer: null, printer_id: 1, choices: { tier: 'standard', nozzles: [] }, prime_tower: null, supports: null, print_sequence: null },
    })
    const next = quote('b', 'r2', '', {
      choices: { printer: null, printer_id: 1, choices: { tier: 'draft', nozzles: [] }, prime_tower: null, supports: null, print_sequence: 'by object' },
    })
    expect(setupChanges(next, accepted, [R1, R2])).toEqual(['width 12 → 14', 'quality standard → draft', 'print sequence none → by object'])
    expect(setupChanges(accepted, accepted, [R1, R2])).toEqual([])
    expect(setupChanges(next, undefined, [R1, R2])).toEqual([])
  })
})
