import { describe, expect, it } from 'vitest'
import type { SpoolEvent } from '../../api/filament'
import { actorText, changeLines, eventNames, fromTypeText } from './eventText'

const event = (values: Partial<SpoolEvent>): SpoolEvent => ({
  id: 'e1',
  seq: 1,
  spool_id: 's1',
  at: '2026-11-10T10:00:00Z',
  kind: 'edited',
  source: null,
  printer_id: null,
  ams_id: null,
  tray_id: null,
  grams_before: null,
  grams_after: null,
  delta_g: null,
  ref_kind: null,
  ref_id: null,
  actor: null,
  data: {},
  ...values,
})

const names = eventNames(
  [
    [{ id: 'm-pla', name: 'PLA' }],
    [{ id: 'b-bambu', name: 'Bambu Lab' }],
  ],
  [
    { id: 't-black', code: 'BL-PLA-BASIC-BLACK' },
    { id: 't-pink', code: 'BL-PLA-BASIC-HOT-PINK' },
  ],
)

describe('actorText', () => {
  it.each([
    [{ actor: 'service:agent', data: { author: { principal: 'p@example.com', session: 's-1', tool: 'filament_update_spool' } } }, 'Assistant'],
    [{ actor: 'service:agent', data: { author: { flow_run: 'run-7' } } }, 'Flow run'],
    [{ actor: 'bambuddy' }, 'Bambuddy sync'],
    [{ actor: 'print' }, 'Printer'],
    [{ actor: 'system:printer-watch' }, 'Printer'],
    [{ actor: 'system:printer-merge' }, 'ScadBuddy'],
    [{ actor: 'system:capture-backstop' }, 'ScadBuddy'],
    [{ actor: 'person@example.com' }, 'person@example.com'],
    [{ actor: null }, 'Unknown'],
  ] as [Partial<SpoolEvent>, string][])('names %j as %s', (values, text) => {
    expect(actorText(event(values))).toBe(text)
  })

  it('does not read a loaded event\'s data.by as its author', () => {
    expect(actorText(event({ kind: 'loaded', actor: 'person@example.com', data: { by: 'tag' } }))).toBe('person@example.com')
    expect(actorText(event({ kind: 'loaded', actor: null, data: { by: 'tag' } }))).toBe('Unknown')
  })
})

describe('changeLines', () => {
  it('shows a cost as a decimal with its before and after', () => {
    expect(changeLines({ cost_per_kg: ['20.0000', '25.0000'] }, names)).toEqual(['Cost per kg: 20.00 → 25.00'])
  })

  it('names a type change by the codes of the types', () => {
    expect(changeLines({ type_id: ['t-black', 't-pink'] }, names)).toEqual(['Type: BL-PLA-BASIC-BLACK → BL-PLA-BASIC-HOT-PINK'])
  })

  it('resolves catalog ids, says "not set" for none and reads lists as one line each', () => {
    expect(changeLines({ material_id: [null, 'm-pla'], brand_id: ['b-bambu', null], presets: [[], [{}]], k_values: [[], []], weight_locked: [false, true] }, names)).toEqual([
      'Material: not set → PLA',
      'Brand: Bambu Lab → not set',
      'Slicer presets changed',
      'K values changed',
      'Weight lock: off → on',
    ])
  })

  it('says an edit a type wrote is from its type', () => {
    expect(fromTypeText(event({ data: { changes: { nozzle_temp_max: [230, 240] }, type_id: 't-black' } }), names)).toBe('from its type BL-PLA-BASIC-BLACK')
    expect(fromTypeText(event({ data: { changes: {} } }), names)).toBeNull()
  })
})
